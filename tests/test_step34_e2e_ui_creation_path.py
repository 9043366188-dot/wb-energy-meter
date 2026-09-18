"""Шаг 34 (партия 5): ОБЯЗАТЕЛЬНЫЙ сквозной автотест на чистой БД —
"именно тот путь, который пользователь не смог пройти руками" (см.
docs/TZ-batch5-make-v2-usable.md §0/§5): место -> точка учёта ->
привязка к прибору -> узел-источник + щит + узел нагрузки -> связи ->
публикация топологии -> «Обзор» показывает итог объекта через
НАЗНАЧЕННЫЙ ВВОД, а не сумму всех приборов.

Каждый шаг — ТОЛЬКО через HTTP /api/v2 (никаких прямых вызовов
репозиториев для доменных мутаций), чтобы тест реально доказывал, что
весь путь теперь наполняем через интерфейс, а не только через сервисный
слой напрямую. Единственное исключение — заполнение почасового агрегата
энергии для расчёта: у /api/v2 нет (и не должно быть, это не в задании)
ручки "вписать историческое показание" — агрегаты в норме пишет фоновый
агрегатор из MQTT; это ограничение теста, а не системы, и уже принято в
test_step27_overview_summary.py.

Самостоятельный скрипт (не pytest):
    python tests/test_step34_e2e_ui_creation_path.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from wb_energy_meter.db import Database
from wb_energy_meter.repo import GroupRepo, MeterRepo
from wb_energy_meter.api import create_app, _AppState
from wb_energy_meter.model import MeterRegistry
from wb_energy_meter.aggregates_repo import AggregateRepo, HourlyAggregate
from wb_energy_meter.point_repo import MeterSourceRepo

HOUR = 3600


def make_client():
    fd, path = tempfile.mkstemp(suffix=".sqlite3")
    os.close(fd)
    os.unlink(path)
    db = Database(path=path)
    db.open()
    groups_repo = GroupRepo(db)
    meters_repo = MeterRepo(db, groups_repo)
    registry = MeterRegistry()
    state = _AppState(
        registry=registry, meters_repo=meters_repo, groups_repo=groups_repo,
        is_mqtt_connected=lambda: False, mqtt_message_count=lambda: 0,
        mqtt_error_count=lambda: 0, wb_db_client=None,
        consumption_service=None, started_at=time.time(), db=db,
    )
    app = create_app(state)
    return app.test_client(), db, path


def current_rev(client):
    return client.get("/api/v2/revision").get_json()["configuration_revision"]


def seed_energy(db, device_id, kwh):
    """Единственный не-HTTP шаг: почасовой агрегат для расчёта (см.
    докстринг модуля выше) — находит meter по device_id, вписывает
    показание за час [0, HOUR)."""
    meters = MeterRepo(db, GroupRepo(db))
    m = meters.get_by_device_id(device_id)
    assert m is not None, f"meter {device_id} должен уже существовать (создан через HTTP)"
    AggregateRepo(db).upsert(HourlyAggregate(
        meter_id=m.id, period_start=0, period_end=HOUR,
        ap_energy_start=0.0, ap_energy_end=kwh, ap_energy_delta=kwh,
        p_avg=None, p_max=None, samples_count=1, quality_flag="ok",
        computed_at=0))


def test_e2e_clean_db_location_to_overview_via_input():
    """Сквозной сценарий с чистой БД (см. докстринг модуля)."""
    client, db, path = make_client()
    try:
        # 1) Место (§8.3/задача 1: дерево мест) --------------------------
        rev = current_rev(client)
        r = client.post("/api/v2/locations", json={
            "name": "Цех №1", "kind": "room", "expected_revision": rev,
        })
        assert r.status_code == 201, r.get_json()
        loc = r.get_json()

        # 2) Точка учёта (код, имя, место установки) ---------------------
        rev = current_rev(client)
        r = client.post("/api/v2/points", json={
            "code": "vvod-1", "name": "Ввод объекта",
            "installation_location_id": loc["id"], "expected_revision": rev,
        })
        assert r.status_code == 201, r.get_json()
        p_input = r.get_json()
        assert p_input["installation_location_id"] == loc["id"]

        # ещё одна точка учёта — на нагрузке ВНУТРИ объекта, измеряется
        # ниже ввода по схеме; её прибор ДОЛЖЕН быть исключён из "Итога
        # объекта" (это и есть проверка "не сумма всех приборов").
        rev = current_rev(client)
        r = client.post("/api/v2/points", json={
            "code": "nagruzka-1", "name": "Нагрузка (станок)",
            "installation_location_id": loc["id"], "expected_revision": rev,
        })
        assert r.status_code == 201, r.get_json()
        p_load = r.get_json()

        # 3) Привязка к прибору (задача 1: "плюс привязка к прибору") —
        #    именно та ручка, которой раньше не было ни у одной из этих
        #    точек (см. test_step33 и §0 ТЗ партии 5).
        rev = current_rev(client)
        r = client.post(f"/api/v2/points/{p_input['id']}/bindings", json={
            "new_meter": {"controller_key": "wb8-main", "device_id": "dev-vvod-1",
                          "display_name": "Счётчик ввода"},
            "channel_profile": "total_3p", "valid_from": 0, "expected_revision": rev,
        })
        assert r.status_code == 201, r.get_json()

        rev = current_rev(client)
        r = client.post(f"/api/v2/points/{p_load['id']}/bindings", json={
            "new_meter": {"controller_key": "wb8-main", "device_id": "dev-nagruzka-1",
                          "display_name": "Счётчик станка"},
            "channel_profile": "total_3p", "valid_from": 0, "expected_revision": rev,
        })
        assert r.status_code == 201, r.get_json()

        # 4) Узлы сети: источник, щит/панель, нагрузка --------------------
        rev = current_rev(client)
        n_src = client.post("/api/v2/topology/nodes", json={
            "code": "SRC", "name": "Ввод сети", "kind": "source",
            "expected_revision": rev}).get_json()
        rev = current_rev(client)
        n_panel = client.post("/api/v2/topology/nodes", json={
            "code": "PANEL", "name": "ГРЩ", "kind": "panel",
            "expected_revision": rev}).get_json()
        rev = current_rev(client)
        n_load = client.post("/api/v2/topology/nodes", json={
            "code": "LOAD", "name": "Станок", "kind": "load",
            "expected_revision": rev}).get_json()
        for n in (n_src, n_panel, n_load):
            assert "id" in n, n

        # 5) Связи: источник -> щит (измерение — ввод объекта), щит ->
        #    нагрузка (измерение — счётчик станка) -----------------------
        rev = current_rev(client)
        e1 = client.post("/api/v2/topology/edges", json={
            "from_node_id": n_src["id"], "to_node_id": n_panel["id"],
            "primary_point_id": p_input["id"], "expected_revision": rev,
        }).get_json()
        rev = current_rev(client)
        e2 = client.post("/api/v2/topology/edges", json={
            "from_node_id": n_panel["id"], "to_node_id": n_load["id"],
            "primary_point_id": p_load["id"], "expected_revision": rev,
        }).get_json()
        assert e1.get("state") == "draft" and e2.get("state") == "draft"

        # 5а) Публикация — явное отдельное действие (задача 1: "Публикация
        #     топологии" отдельно от создания черновиков), с проверкой
        #     через validate ПЕРЕД публикацией — как и должен делать UI.
        r = client.post("/api/v2/topology/validate",
                         json={"edge_ids": [e1["id"], e2["id"]]})
        assert r.status_code == 200 and r.get_json()["ok"] is True, r.get_json()

        rev = current_rev(client)
        r = client.post("/api/v2/topology/publish", json={
            "edge_ids": [e1["id"], e2["id"]],
            "expected_configuration_revision": rev,
        })
        assert r.status_code == 200, r.get_json()
        published = r.get_json()["edges"]
        assert all(e["state"] == "published" for e in published)

        # 6) Данные за период (см. докстринг: единственный не-HTTP шаг)
        seed_energy(db, "dev-vvod-1", 100.0)
        seed_energy(db, "dev-nagruzka-1", 20.0)

        # 7) «Обзор»: итог объекта = ввод (100), а НЕ 100+20 -------------
        r = client.post("/api/v2/overview/summary",
                         json={"from": 0, "to": HOUR, "timezone": "UTC"})
        assert r.status_code == 200, r.get_json()
        body = r.get_json()
        assert body["object_input_point_ids"] == [p_input["id"]], body
        assert body["object_total"]["value"] == 100.0, (
            "итог объекта должен браться через назначенный ввод, а не "
            "быть суммой всех приборов (100 vvod + 20 nagruzka = 120 было "
            "бы неверно)", body["object_total"])
        assert body["object_total_unavailable_reason"] is None

        # 8) Побочная проверка того же пути: /api/v2/structure/points
        #    видит обе точки с привязанными приборами и путём размещения
        #    (то, что реально покажет экран «Структура» после этого
        #    сценария) — и НЕ падает на форме ответа (см. test_step32 и
        #    §4 ТЗ партии 5 про сверку формы ответа).
        r = client.get("/api/v2/structure/points")
        assert r.status_code == 200
        sp = r.get_json()
        assert isinstance(sp, dict) and "points" in sp, (
            "structure/points должна отдавать объект {points:[...]}", sp)
        by_code = {it["code"]: it for it in sp["points"]}
        assert by_code["vvod-1"]["bound"] is True
        assert by_code["vvod-1"]["location_path"]
        assert by_code["nagruzka-1"]["bound"] is True

        # 9) /api/v2/validation больше не должен ругаться на отсутствие
        #    прибора у этих двух точек (задача 1 закрывает то, что задача
        #    4 партии 3 умела только диагностировать).
        r = client.get("/api/v2/validation")
        assert r.status_code == 200
        no_meter_codes = {it["code"] for it in r.get_json()["points_without_meter"]}
        assert "vvod-1" not in no_meter_codes
        assert "nagruzka-1" not in no_meter_codes

        print("[OK] сквозной сценарий партии 5 на чистой БД: место -> точка "
              "-> привязка к прибору -> источник/щит/нагрузка -> связи -> "
              "публикация -> «Обзор» через назначенный ввод (100, не 120) — "
              "полностью через HTTP /api/v2, ни одной прямой мутации репозитория")
    finally:
        db.close(); os.unlink(path)


if __name__ == "__main__":
    test_e2e_clean_db_location_to_overview_via_input()
    print("\nСквозной тест партии 5 (Шаг 34) пройден.")

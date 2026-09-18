"""Тесты Шага 33 (партия 5, задача 1, backend): POST
/api/v2/points/<id>/bindings — открытие ПЕРВОЙ (или дополнительной)
привязки точки к прибору через HTTP.

До этой партии открыть первую привязку точки через /api/v2 было
физически невозможно: `replace-meter` требует уже открытую основную
привязку (см. binding_service.PointBindingRepo.replace_meter — кидает
ValueError, если её нет), а нужный для первой привязки метод,
open_binding, был доступен только изнутри legacy_migration.py — ни один
HTTP-маршрут его не вызывал (проверено по полному списку @app.route в
api_v2.py). Это и есть причина, по которой из интерфейса v2 нельзя было
завести точку с прибором "с нуля" (см. docs/TZ-batch5-make-v2-usable.md
§0).

Самостоятельный скрипт (не pytest):
    python tests/test_step33_open_binding_route.py

Методология партии 3/5: у каждой проверки «отказ» есть парная проверка
«легитимный запрос работает»."""

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
from wb_energy_meter.point_repo import MeteringPointRepo, MeterSourceRepo
from wb_energy_meter.binding_service import PointBindingRepo

HOUR = 3600


def current_rev(client):
    r = client.get("/api/v2/revision")
    assert r.status_code == 200, r.get_json()
    return r.get_json()["configuration_revision"]


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


def test_open_first_binding_with_new_meter_creates_meter_and_source():
    """Позитивный сценарий: у только что созданной точки нет ни одной
    привязки — POST .../bindings с new_meter заводит meter+meter_source
    и открывает основную привязку одной транзакцией с ревизией."""
    client, db, path = make_client()
    try:
        r = client.post("/api/v2/points", json={"code": "p33.1", "name": "Точка 33.1"})
        assert r.status_code == 201, r.get_json()
        point_id = r.get_json()["id"]

        # ДО привязки: GET .../bindings отдаёт пустой список (форма
        # ответа не изменилась — партия 5 требует не менять существующие
        # форматы, только добавлять).
        r = client.get(f"/api/v2/points/{point_id}/bindings")
        assert r.status_code == 200 and r.get_json() == [], r.get_json()

        rev = current_rev(client)
        r = client.post(f"/api/v2/points/{point_id}/bindings", json={
            "new_meter": {"controller_key": "wb8-main", "device_id": "dev-33.1",
                          "display_name": "Прибор 33.1"},
            "channel_profile": "total_3p", "expected_revision": rev,
        })
        assert r.status_code == 201, r.get_json()
        body = r.get_json()
        assert body["point_id"] == point_id
        assert body["role"] == "primary"
        assert body["channel_profile"] == "total_3p"
        assert body["valid_to"] is None
        assert isinstance(body["configuration_revision"], int)
        source_id = body["meter_source_id"]
        assert isinstance(source_id, int)

        meters = MeterRepo(db, GroupRepo(db))
        sources = MeterSourceRepo(db)
        created_meter = meters.get_by_device_id("dev-33.1")
        assert created_meter is not None
        created_source = sources.get_by_id(source_id)
        assert created_source.meter_id == created_meter.id

        # форма ответа GET .../bindings после привязки — та точка, ради
        # которой в партии 3 добавили регрессионный тест на форму ответа
        # structure/points (см. test_step32): здесь список, а не объект.
        r = client.get(f"/api/v2/points/{point_id}/bindings")
        assert r.status_code == 200
        assert isinstance(r.get_json(), list) and len(r.get_json()) == 1
        print("[OK] POST points/<id>/bindings с new_meter открывает первую "
              "привязку (создаёт meter+meter_source)")
    finally:
        db.close(); os.unlink(path)


def test_open_first_binding_with_existing_meter_source_id():
    """Легитимный путь с ГОТОВЫМ meter_source_id (прибор уже заведён
    раньше, например через отдельный вызов) — тот же маршрут, второй
    вариант тела, как и у replace-meter."""
    client, db, path = make_client()
    try:
        meters = MeterRepo(db, GroupRepo(db))
        sources = MeterSourceRepo(db)
        m = meters.add("dev-33.2", "Прибор 33.2")
        src = sources.open_source(m.id, "wb8-main", "dev-33.2")

        r = client.post("/api/v2/points", json={"code": "p33.2", "name": "Точка 33.2"})
        point_id = r.get_json()["id"]
        rev = current_rev(client)
        r = client.post(f"/api/v2/points/{point_id}/bindings", json={
            "meter_source_id": src.id, "expected_revision": rev,
        })
        assert r.status_code == 201, r.get_json()
        assert r.get_json()["meter_source_id"] == src.id
        print("[OK] POST points/<id>/bindings с готовым meter_source_id работает")
    finally:
        db.close(); os.unlink(path)


def test_open_binding_missing_meter_address_rejected_but_legit_still_works():
    """ОТКАЗ: ни meter_source_id, ни new_meter — 400. Парная проверка:
    следом легитимный запрос по тому же маршруту проходит."""
    client, db, path = make_client()
    try:
        r = client.post("/api/v2/points", json={"code": "p33.3", "name": "Точка 33.3"})
        point_id = r.get_json()["id"]
        rev = current_rev(client)
        r = client.post(f"/api/v2/points/{point_id}/bindings",
                         json={"expected_revision": rev})
        assert r.status_code == 400, r.get_json()
        assert r.get_json()["code"] == "bad_request"

        rev = current_rev(client)
        r = client.post(f"/api/v2/points/{point_id}/bindings", json={
            "new_meter": {"device_id": "dev-33.3"}, "expected_revision": rev,
        })
        assert r.status_code == 201, r.get_json()
        print("[OK] ОТКАЗ без адреса прибора 400 + легитимный запрос с "
              "new_meter (без явного controller_key — берётся значение по "
              "умолчанию, как у мастера переноса legacy) проходит")
    finally:
        db.close(); os.unlink(path)


def test_open_binding_without_expected_revision_409_but_with_it_succeeds():
    """Протокол ревизий (§6.1/§9.2): без expected_revision — 409, ничего
    не создаётся; с правильной ревизией — 201."""
    client, db, path = make_client()
    try:
        r = client.post("/api/v2/points", json={"code": "p33.4", "name": "Точка 33.4"})
        point_id = r.get_json()["id"]

        r = client.post(f"/api/v2/points/{point_id}/bindings", json={
            "new_meter": {"device_id": "dev-33.4"},
        })
        assert r.status_code == 409, r.get_json()
        meters = MeterRepo(db, GroupRepo(db))
        assert meters.get_by_device_id("dev-33.4") is None, (
            "конфликт ревизии обязан откатить всю транзакцию, включая уже "
            "созданный до отказа meter")

        rev = current_rev(client)
        r = client.post(f"/api/v2/points/{point_id}/bindings", json={
            "new_meter": {"device_id": "dev-33.4"}, "expected_revision": rev,
        })
        assert r.status_code == 201, r.get_json()
        print("[OK] ОТКАЗ без expected_revision -> 409 (атомарно) + "
              "легитимный запрос с ревизией -> 201")
    finally:
        db.close(); os.unlink(path)


def test_open_second_binding_on_point_with_open_primary_rejected_as_conflict():
    """ОТКАЗ: у точки уже есть открытая основная привязка — открыть ещё
    одну primary-привязку на пересекающийся период нельзя (для этого есть
    replace-meter, а не bindings). Парная проверка: check-роль с другим
    физическим прибором (независимый контроль) проходит на том же
    маршруте."""
    client, db, path = make_client()
    try:
        meters = MeterRepo(db, GroupRepo(db))
        sources = MeterSourceRepo(db)
        m1 = meters.add("dev-33.5a", "Прибор A")
        src1 = sources.open_source(m1.id, "wb8-main", "dev-33.5a")
        bindings = PointBindingRepo(db)

        r = client.post("/api/v2/points", json={"code": "p33.5", "name": "Точка 33.5"})
        point_id = r.get_json()["id"]
        bindings.open_binding(point_id, src1.id, "total_3p", role="primary", valid_from=0)

        rev = current_rev(client)
        r = client.post(f"/api/v2/points/{point_id}/bindings", json={
            "new_meter": {"device_id": "dev-33.5b"}, "expected_revision": rev,
        })
        assert r.status_code == 409, r.get_json()
        assert r.get_json()["code"] == "double_counting"

        rev = current_rev(client)
        r = client.post(f"/api/v2/points/{point_id}/bindings", json={
            "new_meter": {"device_id": "dev-33.5c"}, "role": "check",
            "expected_revision": rev,
        })
        assert r.status_code == 201, r.get_json()
        assert r.get_json()["role"] == "check"
        print("[OK] ОТКАЗ второй primary-привязки поверх открытой (409 "
              "double_counting) + легитимная независимая check-привязка "
              "на том же маршруте проходит")
    finally:
        db.close(); os.unlink(path)


def test_point_not_found_404():
    client, db, path = make_client()
    try:
        r = client.post("/api/v2/points/999/bindings", json={
            "new_meter": {"device_id": "x"}, "expected_revision": 0,
        })
        assert r.status_code == 404, r.get_json()
        assert r.get_json()["code"] == "not_found"
        print("[OK] несуществующая точка -> 404")
    finally:
        db.close(); os.unlink(path)


if __name__ == "__main__":
    test_open_first_binding_with_new_meter_creates_meter_and_source()
    test_open_first_binding_with_existing_meter_source_id()
    test_open_binding_missing_meter_address_rejected_but_legit_still_works()
    test_open_binding_without_expected_revision_409_but_with_it_succeeds()
    test_open_second_binding_on_point_with_open_primary_rejected_as_conflict()
    test_point_not_found_404()
    print("\nВсе тесты открытия привязки через HTTP (Шаг 33, партия 5, "
          "задача 1, backend) пройдены.")

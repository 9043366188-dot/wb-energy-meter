"""Тесты Шага 8: группы/зоны (ТЗ v0.8.0, задача 1).

Самостоятельный скрипт (не pytest), запускается:
    python tests/test_step8_groups.py

Поднимает Flask-приложение через app.test_client() с временной SQLite БД
и реальными репозиториями — без сети, без MQTT. Покрывает регрессию A1
(синхронизация БД -> in-memory реестр) и связанные дефекты A2-A5.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wb_energy_meter.api import create_app, _AppState
from wb_energy_meter.background import BackgroundTasks
from wb_energy_meter.db import Database
from wb_energy_meter.model import MeterRegistry
from wb_energy_meter.repo import GroupRepo, MeterRepo


def make_env():
    """Свежая временная БД + реальные репозитории + registry + test_client."""
    tmpdir = tempfile.mkdtemp(prefix="wbem_test_groups_")
    db = Database(path=os.path.join(tmpdir, "state.db"))
    db.open()
    groups_repo = GroupRepo(db)
    meters_repo = MeterRepo(db, groups_repo)
    registry = MeterRegistry()
    state = _AppState(
        registry=registry, meters_repo=meters_repo,
        groups_repo=groups_repo, alert_repo=None,
        is_mqtt_connected=lambda: True,
        mqtt_message_count=lambda: 0, mqtt_error_count=lambda: 0,
        wb_db_client=None, consumption_service=None,
        started_at=0.0, aggregates_repo=None, aggregator=None,
    )
    app = create_app(state)
    client = app.test_client()
    return tmpdir, db, meters_repo, groups_repo, registry, client


def teardown(tmpdir, db):
    try: db.close()
    except Exception: pass
    shutil.rmtree(tmpdir, ignore_errors=True)


def status_group(client, device_id):
    d = json.loads(client.get("/api/status").get_data(as_text=True))
    row = next((x for x in d["meters"] if x["device_id"] == device_id), None)
    assert row is not None, f"{device_id} не найден в /api/status"
    return row


# ---- A1: назначение зоны сразу видно в /api/status (регрессия ядра ТЗ) ----

def test_group_assign_syncs_to_status_immediately():
    tmpdir, db, meters_repo, groups_repo, registry, client = make_env()
    try:
        meters_repo.add(device_id="wb-map3e_21", display_name="Счётчик 21")
        # До назначения зоны счётчика ещё нет в in-memory реестре (не было
        # ни MQTT, ни ручной синхронизации) — /api/status его не покажет.
        r = client.patch(
            "/api/registry/meters/wb-map3e_21",
            data=json.dumps({"group": "Цех1"}), content_type="application/json")
        assert r.status_code == 200, r.get_data(as_text=True)

        row = status_group(client, "wb-map3e_21")
        assert row["group"] == "Цех1", f"группа не синхронизировалась: {row}"
        # Счётчик появился в памяти только через реестр БД, MQTT-данных
        # не было — статус обязан остаться unknown, а не "ok".
        assert row["status"] == "unknown", f"неожиданный статус: {row}"
        print("[OK] назначение зоны сразу видно в /api/status (A1)")
    finally:
        teardown(tmpdir, db)


# ---- A2: снятие зоны ----

def test_group_clear_via_empty_string():
    tmpdir, db, meters_repo, groups_repo, registry, client = make_env()
    try:
        meters_repo.add(device_id="wb-map3e_22", display_name="Счётчик 22",
                        group="Цех1")
        r = client.patch(
            "/api/registry/meters/wb-map3e_22",
            data=json.dumps({"group": ""}), content_type="application/json")
        assert r.status_code == 200, r.get_data(as_text=True)
        d = json.loads(r.get_data(as_text=True))
        assert d["group"] is None

        m = meters_repo.get_by_device_id("wb-map3e_22")
        assert m.group_name is None, "группа в БД не снялась"

        row = status_group(client, "wb-map3e_22")
        assert row["group"] is None, f"группа не снялась в /api/status: {row}"
        print("[OK] PATCH {group:''} снимает зону (A2)")
    finally:
        teardown(tmpdir, db)


def test_group_clear_via_null():
    tmpdir, db, meters_repo, groups_repo, registry, client = make_env()
    try:
        meters_repo.add(device_id="wb-map3e_22b", display_name="Счётчик 22b",
                        group="Цех1")
        r = client.patch(
            "/api/registry/meters/wb-map3e_22b",
            data=json.dumps({"group": None}), content_type="application/json")
        assert r.status_code == 200, r.get_data(as_text=True)
        m = meters_repo.get_by_device_id("wb-map3e_22b")
        assert m.group_name is None
        print("[OK] PATCH {group:null} тоже снимает зону (A2)")
    finally:
        teardown(tmpdir, db)


# ---- A3: переименование зоны сохраняет id ----

def test_group_rename_keeps_id_and_created_at():
    tmpdir, db, meters_repo, groups_repo, registry, client = make_env()
    try:
        g = groups_repo.create("Цех3")
        meters_repo.add(device_id="wb-map3e_23", display_name="Счётчик 23",
                        group="Цех3")
        r = client.patch(
            f"/api/registry/groups/{g.id}",
            data=json.dumps({"name": "Зона Цех3"}), content_type="application/json")
        assert r.status_code == 200, r.get_data(as_text=True)
        d = json.loads(r.get_data(as_text=True))
        assert d["id"] == g.id

        g2 = groups_repo.get_by_id(g.id)
        assert g2.name == "Зона Цех3"
        assert g2.created_at == g.created_at, "created_at не должен меняться"

        m = meters_repo.get_by_device_id("wb-map3e_23")
        assert m.group_name == "Зона Цех3", "счётчик должен остаться привязан"

        row = status_group(client, "wb-map3e_23")
        assert row["group"] == "Зона Цех3", f"дашборд не обновился: {row}"
        print("[OK] переименование зоны сохраняет id, счётчики на месте (A3)")
    finally:
        teardown(tmpdir, db)


# ---- A4: «Цех1» и «ЦЕХ1» — одна зона (кириллица, casefold) ----

def test_cyrillic_case_insensitive_unique_on_create():
    tmpdir, db, meters_repo, groups_repo, registry, client = make_env()
    try:
        groups_repo.create("Цех1")
        raised = False
        try:
            groups_repo.create("ЦЕХ1")
        except ValueError:
            raised = True
        assert raised, "ЦЕХ1 не должна была создаться отдельной зоной"
        assert len(groups_repo.list_all()) == 1
        print("[OK] «Цех1» и «ЦЕХ1» — одна зона, вторая не создаётся (A4)")
    finally:
        teardown(tmpdir, db)


def test_cyrillic_case_insensitive_unique_via_api():
    tmpdir, db, meters_repo, groups_repo, registry, client = make_env()
    try:
        r1 = client.post("/api/registry/groups",
            data=json.dumps({"name": "Цех2"}), content_type="application/json")
        assert r1.status_code == 201, r1.get_data(as_text=True)
        r2 = client.post("/api/registry/groups",
            data=json.dumps({"name": "ЦЕХ2"}), content_type="application/json")
        assert r2.status_code == 409, r2.get_data(as_text=True)
        d2 = json.loads(r2.get_data(as_text=True))
        assert "existing_id" in d2
        print("[OK] POST /api/registry/groups с именем 'ЦЕХ2' -> 409 (A4 через API)")
    finally:
        teardown(tmpdir, db)


# ---- A5: переименование в занятое имя -> 409, merge:true -> объединение ----

def test_group_rename_conflict_needs_explicit_merge():
    tmpdir, db, meters_repo, groups_repo, registry, client = make_env()
    try:
        ga = groups_repo.create("Цех1")
        gb = groups_repo.create("ЗонаB")
        meters_repo.add(device_id="wb-map3e_25", display_name="Счётчик 25",
                        group="ЗонаB")

        r = client.patch(f"/api/registry/groups/{gb.id}",
            data=json.dumps({"name": "Цех1"}), content_type="application/json")
        assert r.status_code == 409, r.get_data(as_text=True)
        d = json.loads(r.get_data(as_text=True))
        assert d.get("existing_id") == ga.id
        # Без merge зона не должна была слиться молча
        assert len(groups_repo.list_all()) == 2, "зоны не должны были слиться без merge:true"

        r2 = client.patch(f"/api/registry/groups/{gb.id}",
            data=json.dumps({"name": "Цех1", "merge": True}),
            content_type="application/json")
        assert r2.status_code == 200, r2.get_data(as_text=True)
        assert len(groups_repo.list_all()) == 1, "после merge:true должна остаться одна зона"

        m = meters_repo.get_by_device_id("wb-map3e_25")
        assert m.group_name == "Цех1", "счётчик должен перепривязаться на существующую зону"
        print("[OK] переименование в занятое имя -> 409, merge:true -> объединение (A5)")
    finally:
        teardown(tmpdir, db)


# ---- Удаление зоны -> счётчики уходят в «без зоны», видно в /api/status ----

def test_group_delete_clears_meters_and_syncs():
    tmpdir, db, meters_repo, groups_repo, registry, client = make_env()
    try:
        g = groups_repo.create("Цех6")
        meters_repo.add(device_id="wb-map3e_26", display_name="Счётчик 26",
                        group="Цех6")
        # Заранее засветим счётчик в /api/status (как будто он уже был
        # виден по MQTT), чтобы убедиться что удаление зоны реально его
        # обновляет, а не просто "не находит".
        client.get("/api/status")

        r = client.delete(f"/api/registry/groups/{g.id}")
        assert r.status_code == 200, r.get_data(as_text=True)

        m = meters_repo.get_by_device_id("wb-map3e_26")
        assert m.group_name is None

        row = status_group(client, "wb-map3e_26")
        assert row["group"] is None, f"счётчик не ушёл в 'без зоны': {row}"
        print("[OK] удаление зоны переводит счётчики в «без зоны», видно в /api/status")
    finally:
        teardown(tmpdir, db)


# ---- Периодическая пересинхронизация (BackgroundTasks._sync_groups) ----

def test_background_sync_groups():
    tmpdir, db, meters_repo, groups_repo, registry, client = make_env()
    try:
        meters_repo.add(device_id="wb-map3e_27", display_name="Счётчик 27",
                        group="ЦехX")
        assert registry.get("wb-map3e_27") is None, \
            "счётчик не должен быть в памяти до какой-либо синхронизации"

        bg = BackgroundTasks(registry, meters_repo, interval_s=9999)
        bg._sync_groups()

        m = registry.get("wb-map3e_27")
        assert m is not None, "BackgroundTasks._sync_groups должен создать MeterState"
        assert m.group == "ЦехX"
        assert m.status.value == "unknown", \
            "счётчик без MQTT-данных должен остаться unknown, не 'ok'"
        print("[OK] BackgroundTasks._sync_groups подтягивает зону из БД (§3.1 п.2)")
    finally:
        teardown(tmpdir, db)


if __name__ == "__main__":
    test_group_assign_syncs_to_status_immediately()
    test_group_clear_via_empty_string()
    test_group_clear_via_null()
    test_group_rename_keeps_id_and_created_at()
    test_cyrillic_case_insensitive_unique_on_create()
    test_cyrillic_case_insensitive_unique_via_api()
    test_group_rename_conflict_needs_explicit_merge()
    test_group_delete_clears_meters_and_syncs()
    test_background_sync_groups()
    print("\nВсе тесты Шага 8 (группы/зоны) пройдены.")

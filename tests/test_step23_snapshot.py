"""Тесты Шага 23 (этап E): GET /api/v2/snapshot (ТЗ §8.2/§9.2/§10).

Самостоятельный скрипт (не pytest):
    python tests/test_step23_snapshot.py

Проверяет пакет ТЕКУЩИХ значений для "Обзора" — без обращения к
исторической агрегации, источник данных `state.registry` (то же, что
использует фоновый status.py::StatusEngine). Покрывает все ветки
классификации точки: unbound / never_seen / живое устройство, плюс
фильтрацию по point_ids и архивные точки."""

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
from wb_energy_meter.model import MeterRegistry, ControlState, MeterStatus
from wb_energy_meter.point_repo import MeteringPointRepo, MeterSourceRepo
from wb_energy_meter.binding_service import PointBindingRepo


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
    return app.test_client(), db, path, registry


def make_bound_point(db, code, name, registry_device_id=None):
    """Точка + прибор + источник + открытая primary-привязка (как в
    test_step17_accounting_service.py::make_point_with_meter) — но без
    записи её в registry, чтобы вызывающий сам решил, симулировать ли
    "живое" устройство."""
    meters = MeterRepo(db, GroupRepo(db))
    sources = MeterSourceRepo(db)
    points = MeteringPointRepo(db)
    bindings = PointBindingRepo(db)

    device_id = registry_device_id or code
    m = meters.add(code, name)
    src = sources.open_source(m.id, "wb8-main", device_id)
    p = points.add(code, name)
    bindings.open_binding(p.id, src.id, "total_3p", valid_from=0)
    return p, device_id


def populate_live_meter(registry, device_id, *, power_w=42.6, energy_kwh=318.0,
                         status=MeterStatus.OK, status_reason=""):
    meter = registry.get_or_create(device_id)
    now = time.time()
    meter.last_any_ts = now
    meter.last_measurement_ts = now
    meter.status = status
    meter.status_reason = status_reason
    meter.controls["Total P"] = ControlState(
        name="Total P", value=power_w, meta={"type": "power"}, last_update_ts=now)
    meter.controls["Total AP energy"] = ControlState(
        name="Total AP energy", value=energy_kwh,
        meta={"type": "power_consumption"}, last_update_ts=now)
    meter.controls["Frequency"] = ControlState(
        name="Frequency", value=50.0, meta={"type": "value"}, last_update_ts=now)
    for ph, u, i in (("L1", 230.1, 1.2), ("L2", 229.8, 0.9), ("L3", 231.0, 1.1)):
        meter.controls[f"Urms {ph}"] = ControlState(
            name=f"Urms {ph}", value=u, meta={"type": "voltage"}, last_update_ts=now)
        meter.controls[f"Irms {ph}"] = ControlState(
            name=f"Irms {ph}", value=i, meta={"type": "current"}, last_update_ts=now)
    return meter


def test_snapshot_unbound_point():
    client, db, path, registry = make_client()
    try:
        points = MeteringPointRepo(db)
        points.add("unbound1", "Точка без привязки")

        r = client.get("/api/v2/snapshot")
        assert r.status_code == 200, r.get_json()
        body = r.get_json()
        assert "as_of" in body and body["as_of"]
        items = {it["code"]: it for it in body["points"]}
        assert "unbound1" in items
        it = items["unbound1"]
        assert it["binding_status"] == "unbound"
        assert it["device_status"] is None
        assert it["power_w"] is None
        assert it["energy_total_kwh"] is None
        assert it["voltage_v"] is None
        print("[OK] test_snapshot_unbound_point")
    finally:
        db.close(); os.unlink(path)


def test_snapshot_never_seen_device():
    """ТЗ A38: устройство зарегистрировано в БД как источник, но MQTT его
    ещё ни разу не видел — отдельное от no_connection состояние."""
    client, db, path, registry = make_client()
    try:
        point, device_id = make_bound_point(db, "neverseen1", "Никогда не видели")
        # НЕ добавляем device_id в registry — как если бы демон ни разу не
        # получил ни одного MQTT-сообщения от этого устройства.

        r = client.get("/api/v2/snapshot")
        assert r.status_code == 200, r.get_json()
        items = {it["code"]: it for it in r.get_json()["points"]}
        it = items["neverseen1"]
        assert it["binding_status"] == "bound"
        assert it["device_status"] == "never_seen"
        assert it["device_status_reason"]
        assert it["power_w"] is None
        print("[OK] test_snapshot_never_seen_device")
    finally:
        db.close(); os.unlink(path)


def test_snapshot_live_device_full_values():
    client, db, path, registry = make_client()
    try:
        point, device_id = make_bound_point(db, "live1", "Живой прибор")
        populate_live_meter(registry, device_id, power_w=42.6, energy_kwh=318.0)

        r = client.get("/api/v2/snapshot")
        assert r.status_code == 200, r.get_json()
        items = {it["code"]: it for it in r.get_json()["points"]}
        it = items["live1"]
        assert it["binding_status"] == "bound"
        assert it["device_status"] == "ok"
        assert it["power_w"] == 42.6
        assert it["energy_total_kwh"] == 318.0
        assert it["frequency_hz"] == 50.0
        assert it["voltage_v"] == {"L1": 230.1, "L2": 229.8, "L3": 231.0}
        assert it["current_a"] == {"L1": 1.2, "L2": 0.9, "L3": 1.1}
        assert it["last_update_age_s"] is not None and it["last_update_age_s"] >= 0
        assert it["last_measurement_age_s"] is not None
        print("[OK] test_snapshot_live_device_full_values")
    finally:
        db.close(); os.unlink(path)


def test_snapshot_no_connection_status_passed_through():
    """meter.status уже посчитан фоновым StatusEngine — snapshot не должен
    пересчитывать классификацию заново, только читать её как есть."""
    client, db, path, registry = make_client()
    try:
        point, device_id = make_bound_point(db, "dead1", "Пропала связь")
        populate_live_meter(
            registry, device_id, status=MeterStatus.NO_CONNECTION,
            status_reason="Нет сообщений от устройства 10 мин")

        r = client.get("/api/v2/snapshot")
        items = {it["code"]: it for it in r.get_json()["points"]}
        it = items["dead1"]
        assert it["device_status"] == "no_connection"
        assert "10 мин" in it["device_status_reason"]
        print("[OK] test_snapshot_no_connection_status_passed_through")
    finally:
        db.close(); os.unlink(path)


def test_snapshot_point_ids_filter():
    client, db, path, registry = make_client()
    try:
        points = MeteringPointRepo(db)
        p1 = points.add("f1", "Первая")
        p2 = points.add("f2", "Вторая")

        r = client.get(f"/api/v2/snapshot?point_ids={p1.id}")
        assert r.status_code == 200, r.get_json()
        codes = {it["code"] for it in r.get_json()["points"]}
        assert codes == {"f1"}
        print("[OK] test_snapshot_point_ids_filter")
    finally:
        db.close(); os.unlink(path)


def test_snapshot_point_ids_not_found():
    client, db, path, registry = make_client()
    try:
        r = client.get("/api/v2/snapshot?point_ids=99999")
        assert r.status_code == 404, r.get_json()
        body = r.get_json()
        assert body["code"] == "not_found"
        assert 99999 in body["ids"]
        print("[OK] test_snapshot_point_ids_not_found")
    finally:
        db.close(); os.unlink(path)


def test_snapshot_bad_point_ids_format():
    client, db, path, registry = make_client()
    try:
        r = client.get("/api/v2/snapshot?point_ids=abc,def")
        assert r.status_code == 400, r.get_json()
        assert r.get_json()["code"] == "bad_request"
        print("[OK] test_snapshot_bad_point_ids_format")
    finally:
        db.close(); os.unlink(path)


def test_snapshot_excludes_archived_by_default():
    client, db, path, registry = make_client()
    try:
        points = MeteringPointRepo(db)
        p1 = points.add("arch1", "Будет заархивирована")
        points.archive(p1.id)
        points.add("keep1", "Останется")

        r = client.get("/api/v2/snapshot")
        codes = {it["code"] for it in r.get_json()["points"]}
        assert "arch1" not in codes
        assert "keep1" in codes
        print("[OK] test_snapshot_excludes_archived_by_default")
    finally:
        db.close(); os.unlink(path)


if __name__ == "__main__":
    test_snapshot_unbound_point()
    test_snapshot_never_seen_device()
    test_snapshot_live_device_full_values()
    test_snapshot_no_connection_status_passed_through()
    test_snapshot_point_ids_filter()
    test_snapshot_point_ids_not_found()
    test_snapshot_bad_point_ids_format()
    test_snapshot_excludes_archived_by_default()
    print("[ALL OK] test_step23_snapshot")

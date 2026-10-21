"""Тесты партии 14 (A31, A32, A43): загрузка линии в API v2.

Самостоятельный скрипт (не pytest):
    python tests/test_step68_line_load.py

Одна формула на все экраны: load_percent = максимум фазного тока /
rated_current_a × 100 считается только в wb_energy_meter/line_load.py и
отдаётся GET /api/v2/topology/edge-loads. Мощность вместо тока не
подставляется (A32)."""

from __future__ import annotations

import os
import sys
import tempfile
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from wb_energy_meter.api import create_app, _AppState
from wb_energy_meter.binding_service import PointBindingRepo
from wb_energy_meter.db import Database
from wb_energy_meter.line_load import compute_line_load
from wb_energy_meter.model import ControlState, MeterRegistry, MeterStatus
from wb_energy_meter.point_repo import MeteringPointRepo, MeterSourceRepo
from wb_energy_meter.repo import GroupRepo, MeterRepo
from wb_energy_meter.topology_service import ElectricalEdgeRepo, ElectricalNodeRepo


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
    return create_app(state).test_client(), db, path, registry


def make_line(db, registry, code, *, rated, currents, profile="total_3p",
              power_w=None, status=MeterStatus.OK, live=True):
    """Линия «щит → нагрузка» с точкой учёта; currents — {L1: .., L2: .., L3: ..},
    отсутствующая фаза = нет контрола Irms."""
    nodes = ElectricalNodeRepo(db)
    a = nodes.add(f"{code}-a", f"{code} щит", "panel")
    b = nodes.add(f"{code}-b", f"{code} нагрузка", "load")
    meters = MeterRepo(db, GroupRepo(db))
    sources = MeterSourceRepo(db)
    points = MeteringPointRepo(db)
    m = meters.add(code, code)
    src = sources.open_source(m.id, "wb8-main", code)
    p = points.add(code, code)
    PointBindingRepo(db).open_binding(p.id, src.id, profile, valid_from=0)
    edge = ElectricalEdgeRepo(db).add_draft(
        a.id, b.id, code=f"E-{code}", name=f"Линия {code}",
        primary_point_id=p.id, rated_current_a=rated)
    if live:
        meter = registry.get_or_create(code)
        now = time.time()
        meter.last_any_ts = now
        meter.last_measurement_ts = now
        meter.status = status
        if power_w is not None:
            meter.controls["Total P"] = ControlState(
                name="Total P", value=power_w, meta={"type": "power"},
                last_update_ts=now)
            for ph in ("L1", "L2", "L3"):
                meter.controls[f"P {ph}"] = ControlState(
                    name=f"P {ph}", value=power_w / 3, meta={"type": "power"},
                    last_update_ts=now)
        for ph, val in currents.items():
            meter.controls[f"Irms {ph}"] = ControlState(
                name=f"Irms {ph}", value=val, meta={"type": "current"},
                last_update_ts=now)
    return edge, p


def load_of(client, edge_id):
    r = client.get(f"/api/v2/topology/edge-loads?edge_ids={edge_id}")
    assert r.status_code == 200, r.get_json()
    items = r.get_json()["edges"]
    assert len(items) == 1 and items[0]["edge_id"] == edge_id
    return items[0]


def test_a31_max_phase_current_over_rating():
    """A31: токи 10/80/20 А, номинал 100 А → 80 %."""
    client, db, path, registry = make_client()
    try:
        edge, _ = make_line(db, registry, "a31", rated=100.0,
                            currents={"L1": 10.0, "L2": 80.0, "L3": 20.0})
        it = load_of(client, edge.id)
        assert it["load_percent"] == 80.0, it
        assert it["current_a"] == 80.0
        assert it["load_unavailable_reason"] is None
        assert it["rated_current_a"] == 100.0
        assert it["phase_currents"] == {"L1": 10.0, "L2": 80.0, "L3": 20.0}
        print("[OK] test_a31_max_phase_current_over_rating")
    finally:
        db.close(); os.unlink(path)


def test_a31_missing_required_phase_is_unknown():
    """A31: пропала нужная фаза → общий процент неизвестен (null), а не
    максимум по оставшимся."""
    client, db, path, registry = make_client()
    try:
        edge, _ = make_line(db, registry, "a31m", rated=100.0,
                            currents={"L1": 10.0, "L3": 20.0})
        it = load_of(client, edge.id)
        assert it["load_percent"] is None, it
        assert it["load_unavailable_reason"] == "phase_missing"
        assert it["load_unavailable_text"]
        assert it["missing_phases"] == ["L2"]
        assert it["current_a"] == 20.0  # показ имеющегося, но не процент
        print("[OK] test_a31_missing_required_phase_is_unknown")
    finally:
        db.close(); os.unlink(path)


def test_a31_single_phase_profile_needs_only_own_phase():
    """Профиль phase_l2: чужие фазы прибора не нужны; своя фаза есть → процент
    считается по ней, своей нет → phase_missing."""
    client, db, path, registry = make_client()
    try:
        edge, _ = make_line(db, registry, "a31p", rated=50.0, profile="phase_l2",
                            currents={"L1": 99.0, "L2": 25.0, "L3": 99.0})
        it = load_of(client, edge.id)
        assert it["load_percent"] == 50.0, it
        assert it["current_a"] == 25.0

        edge2, _ = make_line(db, registry, "a31q", rated=50.0, profile="phase_l2",
                             currents={"L1": 5.0, "L3": 5.0})
        it2 = load_of(client, edge2.id)
        assert it2["load_percent"] is None
        # снимок phase_l2 содержит только свою фазу: нет её — нет тока вообще
        assert it2["load_unavailable_reason"] == "no_current", it2
        print("[OK] test_a31_single_phase_profile_needs_only_own_phase")
    finally:
        db.close(); os.unlink(path)


def test_a32_no_rating():
    """A32: нет номинала → null и no_rating; мощность процент не заменяет."""
    client, db, path, registry = make_client()
    try:
        edge, _ = make_line(db, registry, "a32r", rated=None,
                            currents={"L1": 10.0, "L2": 80.0, "L3": 20.0},
                            power_w=9000.0)
        it = load_of(client, edge.id)
        assert it["load_percent"] is None
        assert it["load_unavailable_reason"] == "no_rating"
        assert it["current_a"] == 80.0  # измеренный ток номинал не прячет
        assert it["power_w"] == 9000.0
        print("[OK] test_a32_no_rating")
    finally:
        db.close(); os.unlink(path)


def test_a32_no_current_power_present():
    """A32: тока нет, мощность есть (в том числе у соседней линии) → процента
    нет, причина no_current; мощность в процент не пересчитывается."""
    client, db, path, registry = make_client()
    try:
        edge, _ = make_line(db, registry, "a32c", rated=100.0, currents={},
                            power_w=23000.0)
        neighbour, _ = make_line(db, registry, "a32n", rated=100.0,
                                 currents={"L1": 30.0, "L2": 30.0, "L3": 30.0},
                                 power_w=20000.0)
        it = load_of(client, edge.id)
        assert it["load_percent"] is None, it
        assert it["load_unavailable_reason"] == "no_current"
        assert it["power_w"] == 23000.0
        assert load_of(client, neighbour.id)["load_percent"] == 30.0
        print("[OK] test_a32_no_current_power_present")
    finally:
        db.close(); os.unlink(path)


def test_no_point_never_seen_and_offline():
    client, db, path, registry = make_client()
    try:
        nodes = ElectricalNodeRepo(db)
        a = nodes.add("np-a", "щит", "panel")
        b = nodes.add("np-b", "нагрузка", "load")
        bare = ElectricalEdgeRepo(db).add_draft(a.id, b.id, rated_current_a=40.0)
        it = load_of(client, bare.id)
        assert it["load_percent"] is None
        assert it["load_unavailable_reason"] == "no_current"
        assert it["primary_point_id"] is None

        unseen, _ = make_line(db, registry, "never", rated=40.0, currents={},
                              live=False)
        assert load_of(client, unseen.id)["load_unavailable_reason"] == "no_current"

        offline, _ = make_line(db, registry, "off", rated=40.0,
                               currents={"L1": 4.0, "L2": 4.0, "L3": 4.0},
                               status=MeterStatus.NO_CONNECTION)
        it3 = load_of(client, offline.id)
        assert it3["load_percent"] is None
        assert it3["load_unavailable_reason"] == "no_current"
        print("[OK] test_no_point_never_seen_and_offline")
    finally:
        db.close(); os.unlink(path)


def test_list_all_and_errors():
    client, db, path, registry = make_client()
    try:
        e1, _ = make_line(db, registry, "l1", rated=100.0,
                          currents={"L1": 1.0, "L2": 2.0, "L3": 3.0})
        e2, _ = make_line(db, registry, "l2", rated=10.0,
                          currents={"L1": 1.0, "L2": 2.0, "L3": 3.0})
        r = client.get("/api/v2/topology/edge-loads")
        assert r.status_code == 200
        got = {it["edge_id"]: it for it in r.get_json()["edges"]}
        assert got[e1.id]["load_percent"] == 3.0
        assert got[e2.id]["load_percent"] == 30.0
        assert client.get("/api/v2/topology/edge-loads?edge_ids=999999").status_code == 404
        assert client.get("/api/v2/topology/edge-loads?edge_ids=x").status_code == 400
        print("[OK] test_list_all_and_errors")
    finally:
        db.close(); os.unlink(path)


def test_pure_function_boundaries():
    snap = {"binding_status": "bound", "device_status": "ok",
            "channel_profile": "total_3p", "power_w": 5000.0,
            "current_a": {"L1": 0.0, "L2": 0.0, "L3": 0.0}}
    r = compute_line_load(100.0, snap)
    assert r["load_percent"] == 0.0 and r["load_unavailable_reason"] is None
    assert compute_line_load(0, snap)["load_unavailable_reason"] == "no_rating"
    assert compute_line_load(-5, snap)["load_unavailable_reason"] == "no_rating"
    assert compute_line_load(100.0, None)["load_unavailable_reason"] == "no_current"
    snap2 = dict(snap, current_a={"L1": 120.0, "L2": 0.0, "L3": 0.0})
    assert compute_line_load(100.0, snap2)["load_percent"] == 120.0
    print("[OK] test_pure_function_boundaries")


if __name__ == "__main__":
    test_a31_max_phase_current_over_rating()
    test_a31_missing_required_phase_is_unknown()
    test_a31_single_phase_profile_needs_only_own_phase()
    test_a32_no_rating()
    test_a32_no_current_power_present()
    test_no_point_never_seen_and_offline()
    test_list_all_and_errors()
    test_pure_function_boundaries()
    print("[ALL OK] test_step68_line_load")

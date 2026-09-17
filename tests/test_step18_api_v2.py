"""Тесты Шага 18 (этап C): HTTP-слой /api/v2 (ТЗ §9.2).

Самостоятельный скрипт (не pytest):
    python tests/test_step18_api_v2.py

Проверяет, что api_v2.py правильно транслирует исключения сервисов
(BindingConflict/TopologyConflict/AccountingConflict/ValueError) в HTTP
409/400/404 с envelope {"code","message",...} — это самая ответственная
часть слоя, написанная лично."""

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
from wb_energy_meter.aggregates_repo import AggregateRepo, HourlyAggregate

HOUR = 3600


def current_rev(client):
    """Партия 2, задача 1 (ТЗ §6.1/§9.2): текущая глобальная ревизия
    конфигурации — точка отсчёта для expected_revision в PATCH/публикации."""
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


def test_points_crud():
    client, db, path = make_client()
    try:
        r = client.post("/api/v2/points", json={"code": "p1", "name": "Точка 1"})
        assert r.status_code == 201, r.get_json()
        point_id = r.get_json()["id"]

        r = client.get(f"/api/v2/points/{point_id}")
        assert r.status_code == 200 and r.get_json()["code"] == "p1"

        r = client.get("/api/v2/points")
        assert r.status_code == 200 and len(r.get_json()) == 1

        r = client.patch(f"/api/v2/points/{point_id}",
                          json={"enabled": False, "expected_revision": current_rev(client)})
        assert r.status_code == 200 and r.get_json()["enabled"] is False
        assert isinstance(r.get_json()["configuration_revision"], int)

        r = client.get("/api/v2/points/999999")
        assert r.status_code == 404
        assert r.get_json()["code"] == "not_found"

        r = client.post("/api/v2/points", json={"code": "p1", "name": "Дубликат"})
        assert r.status_code == 400
        print("[OK] points CRUD: create/get/list/patch/404/400-дубликат")
    finally:
        db.close(); os.unlink(path)


def test_locations_crud_and_cycle_409():
    client, db, path = make_client()
    try:
        r = client.post("/api/v2/locations", json={"name": "A", "kind": "object"})
        a_id = r.get_json()["id"]
        r = client.post("/api/v2/locations", json={"name": "B", "kind": "building",
                                                     "parent_id": a_id})
        b_id = r.get_json()["id"]

        r = client.patch(f"/api/v2/locations/{a_id}",
                          json={"parent_id": b_id, "expected_revision": current_rev(client)})
        assert r.status_code == 409, r.get_json()
        assert r.get_json()["code"] == "cycle_conflict"
        print("[OK] locations: перенос в собственного потомка -> 409 cycle_conflict")
    finally:
        db.close(); os.unlink(path)


def test_topology_publish_cycle_409_and_structure_unchanged():
    client, db, path = make_client()
    try:
        na = client.post("/api/v2/topology/nodes",
                          json={"code": "N-A", "name": "A", "kind": "panel"}).get_json()
        nb = client.post("/api/v2/topology/nodes",
                          json={"code": "N-B", "name": "B", "kind": "panel"}).get_json()
        nc = client.post("/api/v2/topology/nodes",
                          json={"code": "N-C", "name": "C", "kind": "panel"}).get_json()

        e1 = client.post("/api/v2/topology/edges",
                          json={"from_node_id": na["id"], "to_node_id": nb["id"]}).get_json()
        e2 = client.post("/api/v2/topology/edges",
                          json={"from_node_id": nb["id"], "to_node_id": nc["id"]}).get_json()
        e3 = client.post("/api/v2/topology/edges",
                          json={"from_node_id": nc["id"], "to_node_id": na["id"]}).get_json()

        r = client.post("/api/v2/topology/validate",
                         json={"edge_ids": [e1["id"], e2["id"], e3["id"]]})
        assert r.status_code == 200
        body = r.get_json()
        assert body["ok"] is False
        assert any(v["kind"] == "cycle" for v in body["violations"])

        r = client.post("/api/v2/topology/publish",
                         json={"edge_ids": [e1["id"], e2["id"], e3["id"]],
                               "expected_configuration_revision": current_rev(client)})
        assert r.status_code == 409, r.get_json()
        assert r.get_json()["code"] == "topology_conflict"
        assert "path" in r.get_json()

        r = client.get("/api/v2/topology/edges?state=published")
        assert r.get_json() == []
        print("[OK] A24 через HTTP: validate/publish цикла -> 409 topology_conflict, "
              "структура не изменилась")

        # теперь публикуем валидное дерево A->B, B->C
        r = client.post("/api/v2/topology/publish",
                         json={"edge_ids": [e1["id"], e2["id"]],
                               "expected_configuration_revision": current_rev(client)})
        assert r.status_code == 200, r.get_json()
        assert isinstance(r.get_json()["configuration_revision"], int)
        r = client.get("/api/v2/topology/edges?state=published")
        assert len(r.get_json()) == 2
        print("[OK] topology/publish валидного набора -> 200, связи активны")
    finally:
        db.close(); os.unlink(path)


def test_metrics_query_sum_overlap_409():
    client, db, path = make_client()
    try:
        meters = MeterRepo(db, GroupRepo(db))
        sources = MeterSourceRepo(db)
        points = MeteringPointRepo(db)
        aggregates = AggregateRepo(db)

        def make_point(code):
            m = meters.add(code, code)
            src = sources.open_source(m.id, "wb8-main", code)
            p = points.add(code, code)
            return p, m, src

        p_a, m_a, src_a = make_point("v2.a")
        p_d, m_d, src_d = make_point("v2.d")

        # открыть привязки напрямую через binding_service (проще, чем
        # городить ещё один маршрут привязки для теста)
        from wb_energy_meter.binding_service import PointBindingRepo
        bindings = PointBindingRepo(db)
        bindings.open_binding(p_a.id, src_a.id, "total_3p", valid_from=0)
        bindings.open_binding(p_d.id, src_d.id, "total_3p", valid_from=0)

        na = client.post("/api/v2/topology/nodes",
                          json={"code": "N-SRC", "name": "Ввод", "kind": "source"}).get_json()
        nb = client.post("/api/v2/topology/nodes",
                          json={"code": "N-B2", "name": "B", "kind": "panel"}).get_json()
        nd = client.post("/api/v2/topology/nodes",
                          json={"code": "N-D2", "name": "D", "kind": "load"}).get_json()

        e1 = client.post("/api/v2/topology/edges",
                          json={"from_node_id": na["id"], "to_node_id": nb["id"],
                                "primary_point_id": p_a.id}).get_json()
        e2 = client.post("/api/v2/topology/edges",
                          json={"from_node_id": nb["id"], "to_node_id": nd["id"],
                                "primary_point_id": p_d.id}).get_json()
        r = client.post("/api/v2/topology/publish",
                         json={"edge_ids": [e1["id"], e2["id"]],
                               "expected_configuration_revision": current_rev(client)})
        assert r.status_code == 200, r.get_json()

        aggregates.upsert(HourlyAggregate(
            meter_id=m_a.id, period_start=0, period_end=HOUR,
            ap_energy_start=0.0, ap_energy_end=100.0, ap_energy_delta=100.0,
            p_avg=None, p_max=None, samples_count=1, quality_flag="ok", computed_at=0))
        aggregates.upsert(HourlyAggregate(
            meter_id=m_d.id, period_start=0, period_end=HOUR,
            ap_energy_start=0.0, ap_energy_end=20.0, ap_energy_delta=20.0,
            p_avg=None, p_max=None, samples_count=1, quality_flag="ok", computed_at=0))

        r = client.post("/api/v2/metrics/query", json={
            "mode": "sum", "point_ids": [p_a.id, p_d.id],
            "from": 0, "to": HOUR, "timezone": "UTC",
            "structure_mode": "current",
        })
        assert r.status_code == 409, r.get_json()
        assert r.get_json()["code"] == "double_counting"
        print(f"[OK] A04 через HTTP: metrics/query sum с перекрытием -> "
              f"409 double_counting: {r.get_json()['message']}")

        r2 = client.post("/api/v2/metrics/query", json={
            "mode": "measured", "point_ids": [p_a.id],
            "from": 0, "to": HOUR, "timezone": "UTC",
            "structure_mode": "current",
        })
        assert r2.status_code == 200
        assert r2.get_json()["value"] == 100.0
        print("[OK] metrics/query measured -> 200, value=100.0")
    finally:
        db.close(); os.unlink(path)


def test_metrics_query_bad_mode_400():
    client, db, path = make_client()
    try:
        r = client.post("/api/v2/metrics/query", json={
            "mode": "nonsense", "from": 0, "to": HOUR, "timezone": "UTC",
        })
        assert r.status_code == 400
        assert r.get_json()["code"] == "bad_request"
        print("[OK] metrics/query неизвестный mode -> 400 bad_request")
    finally:
        db.close(); os.unlink(path)


def test_balance_scopes_crud_and_balance_query():
    client, db, path = make_client()
    try:
        meters = MeterRepo(db, GroupRepo(db))
        sources = MeterSourceRepo(db)
        points = MeteringPointRepo(db)
        aggregates = AggregateRepo(db)
        from wb_energy_meter.binding_service import PointBindingRepo
        bindings = PointBindingRepo(db)

        def make_point(code, kwh):
            m = meters.add(code, code)
            src = sources.open_source(m.id, "wb8-main", code)
            p = points.add(code, code)
            bindings.open_binding(p.id, src.id, "total_3p", valid_from=0)
            aggregates.upsert(HourlyAggregate(
                meter_id=m.id, period_start=0, period_end=HOUR,
                ap_energy_start=0.0, ap_energy_end=kwh, ap_energy_delta=kwh,
                p_avg=None, p_max=None, samples_count=1, quality_flag="ok", computed_at=0))
            return p

        p_in = make_point("bal.in", 100.0)
        p_o1 = make_point("bal.o1", 70.0)
        p_o2 = make_point("bal.o2", 40.0)

        r = client.post("/api/v2/balance-scopes", json={
            "name": "Объект", "input_point_ids": [p_in.id],
            "output_point_ids": [p_o1.id, p_o2.id],
        })
        assert r.status_code == 201, r.get_json()
        scope_id = r.get_json()["id"]

        r = client.get(f"/api/v2/balance-scopes/{scope_id}")
        assert sorted(r.get_json()["output_point_ids"]) == sorted([p_o1.id, p_o2.id])

        r = client.post("/api/v2/metrics/query", json={
            "mode": "balance", "scope": scope_id,
            "from": 0, "to": HOUR, "timezone": "UTC",
        })
        assert r.status_code == 200, r.get_json()
        body = r.get_json()
        assert body["value"] == -10.0, body
        assert body["explanation"]["percentage"] == -10.0
        print(f"[OK] A08 через HTTP: balance-scopes + metrics/query balance -> "
              f"{body['value']} ({body['explanation']['percentage']}%)")
    finally:
        db.close(); os.unlink(path)


if __name__ == "__main__":
    test_points_crud()
    test_locations_crud_and_cycle_409()
    test_topology_publish_cycle_409_and_structure_unchanged()
    test_metrics_query_sum_overlap_409()
    test_metrics_query_bad_mode_400()
    test_balance_scopes_crud_and_balance_query()
    print("\nВсе тесты api_v2 (Шаг 18) пройдены.")

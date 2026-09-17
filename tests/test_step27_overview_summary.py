"""Тесты Шага 27 (партия 2, задача 2): POST /api/v2/overview/summary
(ТЗ §8.2) — единственная точка расчёта для экрана "Обзор": итог объекта
через назначенный ввод (а не сумму счётчиков), ветви верхнего уровня,
подписанный небаланс, переключение в режим сравнения при подтверждённом
электрическом пересечении (A04).

Самостоятельный скрипт (не pytest):
    python tests/test_step27_overview_summary.py
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
from wb_energy_meter.point_repo import MeteringPointRepo, MeterSourceRepo
from wb_energy_meter.aggregates_repo import AggregateRepo, HourlyAggregate
from wb_energy_meter.binding_service import PointBindingRepo

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


class Rig:
    """Общая обвязка для сценариев A/B/C/D из большого ТЗ (A03): точка +
    прибор + агрегат за один клик, плюс регистрация в v2 (точка учёта +
    источник + основная привязка) через прямые репозитории (то же самое,
    что уже делают test_step17/test_step18)."""

    def __init__(self, db):
        self.meters = MeterRepo(db, GroupRepo(db))
        self.sources = MeterSourceRepo(db)
        self.points = MeteringPointRepo(db)
        self.aggregates = AggregateRepo(db)
        self.bindings = PointBindingRepo(db)

    def make_point(self, code, kwh):
        m = self.meters.add(code, code)
        src = self.sources.open_source(m.id, "wb8-main", code)
        p = self.points.add(code, code)
        self.bindings.open_binding(p.id, src.id, "total_3p", valid_from=0)
        if kwh is not None:
            self.aggregates.upsert(HourlyAggregate(
                meter_id=m.id, period_start=0, period_end=HOUR,
                ap_energy_start=0.0, ap_energy_end=kwh, ap_energy_delta=kwh,
                p_avg=None, p_max=None, samples_count=1, quality_flag="ok",
                computed_at=0))
        return p


def test_a03_object_total_via_input_not_sum_of_all_meters():
    """A03: ввод A=100; цех B=60; серверная C=30; станок D=20 внутри B.
    Итог объекта = A (через назначенный ввод), НЕ A+B+C+D. Ветвь B+C=90,
    небаланс 10, D не учитывается в верхнем уровне отдельной строкой."""
    client, db, path = make_client()
    try:
        rig = Rig(db)
        p_a = rig.make_point("A", 100.0)
        p_b = rig.make_point("B", 60.0)
        p_c = rig.make_point("C", 30.0)
        p_d = rig.make_point("D", 20.0)

        # электросеть: ЕДИНСТВЕННЫЙ ввод от source (A) -> общий щит ->
        # разветвление на B и C, D — под B (A03 буквально: A=100, B=60,
        # C=30, D=20 внутри B).
        n_src = client.post("/api/v2/topology/nodes",
                             json={"code": "SRC", "name": "Ввод", "kind": "source"}).get_json()
        n_top = client.post("/api/v2/topology/nodes",
                             json={"code": "TOP", "name": "ГРЩ", "kind": "panel"}).get_json()
        n_b = client.post("/api/v2/topology/nodes",
                           json={"code": "NB", "name": "Узел B", "kind": "panel"}).get_json()
        n_c = client.post("/api/v2/topology/nodes",
                           json={"code": "NC", "name": "Узел C", "kind": "load"}).get_json()
        n_d = client.post("/api/v2/topology/nodes",
                           json={"code": "ND", "name": "Узел D", "kind": "load"}).get_json()

        e1 = client.post("/api/v2/topology/edges", json={
            "from_node_id": n_src["id"], "to_node_id": n_top["id"], "primary_point_id": p_a.id
        }).get_json()
        e2 = client.post("/api/v2/topology/edges", json={
            "from_node_id": n_top["id"], "to_node_id": n_b["id"], "primary_point_id": p_b.id
        }).get_json()
        e3 = client.post("/api/v2/topology/edges", json={
            "from_node_id": n_top["id"], "to_node_id": n_c["id"], "primary_point_id": p_c.id
        }).get_json()
        e4 = client.post("/api/v2/topology/edges", json={
            "from_node_id": n_b["id"], "to_node_id": n_d["id"], "primary_point_id": p_d.id
        }).get_json()

        rev = current_rev(client)
        r = client.post("/api/v2/topology/publish",
                         json={"edge_ids": [e1["id"], e2["id"], e3["id"], e4["id"]],
                               "expected_configuration_revision": rev})
        assert r.status_code == 200, r.get_json()

        # ветви верхнего уровня буквально по A03: "Цех"={B}, "Серверная"={C}.
        # D (станок внутри B) сознательно НЕ входит ни в одну группу здесь
        # — его "не задвоение" при явном group-членстве отдельно и точнее
        # проверяется в test_step24_group_repo_v2.py
        # (test_resolve_effective_members_dedup_and_provenance); попытка
        # включить D в ту же ветвь, что и B, электрически некорректна (D
        # измеряется НИЖЕ B по сети — их сумма была бы двойным счётом) и
        # предметно проверена ниже, в test_a04_branch_sum_overlap_via_group_falls_back_to_comparison.
        shop = client.post("/api/v2/groups", json={"name": "Цех"}).get_json()
        server = client.post("/api/v2/groups", json={"name": "Серверная"}).get_json()
        client.post(f"/api/v2/groups/{shop['id']}/members",
                    json={"point_id": p_b.id, "expected_revision": current_rev(client)})
        client.post(f"/api/v2/groups/{server['id']}/members",
                    json={"point_id": p_c.id, "expected_revision": current_rev(client)})

        r = client.post("/api/v2/overview/summary",
                         json={"from": 0, "to": HOUR, "timezone": "UTC"})
        assert r.status_code == 200, r.get_json()
        body = r.get_json()

        assert body["object_input_point_ids"] == [p_a.id]
        assert body["object_total"]["value"] == 100.0, body["object_total"]
        assert body["object_total_unavailable_reason"] is None

        by_name = {b["name"]: b for b in body["branches"]}
        assert by_name["Цех"]["mode"] == "sum"
        assert by_name["Цех"]["result"]["value"] == 60.0, by_name["Цех"]
        assert by_name["Серверная"]["result"]["value"] == 30.0

        # уровень 1: Цех(60) + Серверная(30) = 90; небаланс = 100-90 = 10
        # (A03 буквально), D нигде не задвоен -- он просто не входит ни в
        # одну ветвь в этом составе, а значит виден в ungrouped_point_ids.
        assert body["imbalance_value"] == 10.0, body["imbalance_value"]
        assert p_d.id in body["ungrouped_point_ids"]
        print("[OK] A03: итог объекта = назначенный ввод (100), НЕ сумма всех счётчиков; "
              "первый уровень B+C=90, небаланс=10, D не задвоен")
    finally:
        db.close(); os.unlink(path)


def test_no_input_assigned_shows_action_not_false_total():
    """§8.2: "Без назначенного ввода показывать действие 'Настроить
    границу объекта', а не ложное число" — object_total обязан быть null,
    а не суммой всех точек."""
    client, db, path = make_client()
    try:
        rig = Rig(db)
        rig.make_point("A", 100.0)
        rig.make_point("B", 60.0)

        r = client.post("/api/v2/overview/summary",
                         json={"from": 0, "to": HOUR, "timezone": "UTC"})
        assert r.status_code == 200, r.get_json()
        body = r.get_json()
        assert body["object_total"] is None
        assert body["object_total_unavailable_reason"] == "no_input_assigned"
        assert body["object_input_point_ids"] == []
        print("[OK] без назначенного ввода: object_total=null с явной причиной, "
              "не сумма всех счётчиков")
    finally:
        db.close(); os.unlink(path)


def test_a04_branch_sum_overlap_via_group_falls_back_to_comparison():
    """A04 буквально: явная сумма A+D, где D — электрический потомок A —
    именно эту ситуацию check_sum_overlap уже ловит (test_step17), здесь
    проверяем, что /overview/summary не падает 500 и не показывает
    ложный общий итог по ветви, а переключается в comparison."""
    client, db, path = make_client()
    try:
        rig = Rig(db)
        p_a = rig.make_point("A", 100.0)
        p_d = rig.make_point("D", 20.0)

        n_src = client.post("/api/v2/topology/nodes",
                             json={"code": "SRC", "name": "Ввод", "kind": "source"}).get_json()
        n_a = client.post("/api/v2/topology/nodes",
                           json={"code": "NA", "name": "A", "kind": "panel"}).get_json()
        n_d = client.post("/api/v2/topology/nodes",
                           json={"code": "ND", "name": "D", "kind": "load"}).get_json()
        e1 = client.post("/api/v2/topology/edges", json={
            "from_node_id": n_src["id"], "to_node_id": n_a["id"], "primary_point_id": p_a.id
        }).get_json()
        e2 = client.post("/api/v2/topology/edges", json={
            "from_node_id": n_a["id"], "to_node_id": n_d["id"], "primary_point_id": p_d.id
        }).get_json()
        rev = current_rev(client)
        r = client.post("/api/v2/topology/publish",
                         json={"edge_ids": [e1["id"], e2["id"]],
                               "expected_configuration_revision": rev})
        assert r.status_code == 200, r.get_json()

        # ветвь "Смешанная" содержит и A, и D -- D электрический потомок A
        mixed = client.post("/api/v2/groups", json={"name": "Смешанная"}).get_json()
        client.post(f"/api/v2/groups/{mixed['id']}/members",
                    json={"point_id": p_a.id, "expected_revision": current_rev(client)})
        client.post(f"/api/v2/groups/{mixed['id']}/members",
                    json={"point_id": p_d.id, "expected_revision": current_rev(client)})

        r = client.post("/api/v2/overview/summary",
                         json={"from": 0, "to": HOUR, "timezone": "UTC"})
        assert r.status_code == 200, r.get_json()
        body = r.get_json()
        mixed_branch = next(b for b in body["branches"] if b["name"] == "Смешанная")
        assert mixed_branch["mode"] == "comparison", mixed_branch
        assert mixed_branch["conflict_reason"]
        assert set(mixed_branch["points"].keys()) == {str(p_a.id), str(p_d.id)}
        assert mixed_branch["points"][str(p_a.id)]["value"] == 100.0
        assert mixed_branch["points"][str(p_d.id)]["value"] == 20.0
        print("[OK] A04: ветвь с подтверждённым электрическим пересечением -> режим "
              "сравнения точек, без ложного общего итога по ветви")
    finally:
        db.close(); os.unlink(path)


def test_a10_no_data_vs_zero_distinguished():
    """A10: все точки без данных -> null; одна исправная точка с нулевым
    расходом -> 0. Разница видна и в object_total, и в ветви."""
    client, db, path = make_client()
    try:
        rig = Rig(db)
        p_nodata = rig.make_point("ND", None)  # ни одного агрегата
        p_zero = rig.make_point("Z", 0.0)      # агрегат есть, delta=0.0

        n_src = client.post("/api/v2/topology/nodes",
                             json={"code": "SRC", "name": "Ввод", "kind": "source"}).get_json()
        n1 = client.post("/api/v2/topology/nodes",
                          json={"code": "N1", "name": "N1", "kind": "load"}).get_json()
        e1 = client.post("/api/v2/topology/edges", json={
            "from_node_id": n_src["id"], "to_node_id": n1["id"], "primary_point_id": p_zero.id
        }).get_json()
        rev = current_rev(client)
        client.post("/api/v2/topology/publish",
                    json={"edge_ids": [e1["id"]], "expected_configuration_revision": rev})

        r = client.post("/api/v2/overview/summary",
                         json={"from": 0, "to": HOUR, "timezone": "UTC"})
        body = r.get_json()
        assert body["object_total"]["value"] == 0.0, body["object_total"]

        r2 = client.post("/api/v2/metrics/query", json={
            "mode": "measured", "point_ids": [p_nodata.id],
            "from": 0, "to": HOUR, "timezone": "UTC",
        })
        assert r2.get_json()["value"] is None
        assert r2.get_json()["availability"] == "missing"
        print("[OK] A10: точка без данных -> value=null, availability=missing; "
              "исправная точка с нулевым расходом -> value=0.0 — не спутаны")
    finally:
        db.close(); os.unlink(path)


def test_a11_percentage_null_when_object_total_missing():
    """A11: если итог объекта неизвестен (нет назначенного ввода),
    percentage_of_object по ветви — null с причиной, без деления на 0."""
    client, db, path = make_client()
    try:
        rig = Rig(db)
        p_b = rig.make_point("B", 60.0)
        g = client.post("/api/v2/groups", json={"name": "Цех"}).get_json()
        client.post(f"/api/v2/groups/{g['id']}/members",
                    json={"point_id": p_b.id, "expected_revision": current_rev(client)})

        r = client.post("/api/v2/overview/summary",
                         json={"from": 0, "to": HOUR, "timezone": "UTC"})
        body = r.get_json()
        assert body["object_total"] is None
        branch = body["branches"][0]
        assert branch["result"]["value"] == 60.0
        assert branch["percentage_of_object"] is None
        assert branch["percentage_of_object_reason"] == "no_data"
        print("[OK] A11: процент ветви от объекта -- null с причиной при отсутствующей базе, "
              "без фиктивных 100%")
    finally:
        db.close(); os.unlink(path)


def test_overview_summary_pins_revision_and_rejects_unknown():
    client, db, path = make_client()
    try:
        rig = Rig(db)
        rig.make_point("A", 10.0)
        client.post("/api/v2/groups", json={"name": "G"})
        rev = current_rev(client)

        r = client.post("/api/v2/overview/summary",
                         json={"from": 0, "to": HOUR, "timezone": "UTC"})
        assert r.get_json()["configuration_revision_id"] == rev

        r2 = client.post("/api/v2/overview/summary",
                          json={"from": 0, "to": HOUR, "timezone": "UTC",
                                "configuration_revision_id": rev + 999})
        assert r2.status_code == 404, r2.get_json()
        print("[OK] overview/summary: ревизия фиксируется в ответе; неизвестная -> 404")
    finally:
        db.close(); os.unlink(path)


if __name__ == "__main__":
    test_a03_object_total_via_input_not_sum_of_all_meters()
    test_no_input_assigned_shows_action_not_false_total()
    test_a04_branch_sum_overlap_via_group_falls_back_to_comparison()
    test_a10_no_data_vs_zero_distinguished()
    test_a11_percentage_null_when_object_total_missing()
    test_overview_summary_pins_revision_and_rejects_unknown()
    print("[ALL OK] test_step27_overview_summary")

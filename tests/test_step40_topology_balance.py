"""Тесты Шага 40 (партия 7, Этап 2, Э2.7): баланс по электрической схеме —
`wb_energy_meter/overview_service.py` (`first_measurements`,
`balance_node`, `object_summary`), `POST /api/v2/overview/summary`
(переписанный расчёт итога/небаланса объекта), новая ручка
`POST /api/v2/topology/nodes/<id>/balance` (Э2.5) и `dimension="branch"`
в `/api/v2/reports/query` (Э2.6).

Главная находка ревью 23.09.2026 (docs/review-2026-09-23.md, №1): до этой
партии небаланс объекта считался как "итог по назначенному вводу минус
сумма корневых УЧЁТНЫХ ГРУПП" — двух независимых источников числа. Если
схема была собрана без единой группы, небаланс показывал 100%; если точка
входила в две группы, уходил в минус из-за двойного счёта. Таблица T1-T12
ниже — сценарии Э2.7 задания (docs/TZ-batch7-review-fixes.md).

Самостоятельный скрипт (не pytest):
    python tests/test_step40_topology_balance.py
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
    """Та же обвязка, что в test_step27_overview_summary.py: точка +
    прибор + агрегат за один вызов."""

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


def make_node(client, code, name, kind):
    r = client.post("/api/v2/topology/nodes", json={"code": code, "name": name, "kind": kind})
    assert r.status_code == 201, r.get_json()
    return r.get_json()


def make_edge(client, from_node, to_node, point=None):
    body = {"from_node_id": from_node["id"], "to_node_id": to_node["id"]}
    if point is not None:
        body["primary_point_id"] = point.id
    r = client.post("/api/v2/topology/edges", json=body)
    assert r.status_code == 201, r.get_json()
    return r.get_json()


def publish(client, edges):
    rev = current_rev(client)
    r = client.post("/api/v2/topology/publish", json={
        "edge_ids": [e["id"] for e in edges],
        "expected_configuration_revision": rev,
    })
    assert r.status_code == 200, r.get_json()


def overview(client):
    r = client.post("/api/v2/overview/summary",
                     json={"from": 0, "to": HOUR, "timezone": "UTC"})
    assert r.status_code == 200, r.get_json()
    return r.get_json()


def node_balance(client, node_id, **extra):
    body = {"from": 0, "to": HOUR, "timezone": "UTC"}
    body.update(extra)
    return client.post(f"/api/v2/topology/nodes/{node_id}/balance", json=body)


# ---------------------------------------------------------------------
# T1 (A03): Ввод→ГРЩ-1 A=100; ГРЩ-1→Цех B=60; ГРЩ-1→Серверная C=30;
# Цех→Станок D=20.
# ---------------------------------------------------------------------

def test_t1_object_and_node_balance_with_nested_unbranched_consumer():
    client, db, path = make_client()
    try:
        rig = Rig(db)
        p_a = rig.make_point("A", 100.0)
        p_b = rig.make_point("B", 60.0)
        p_c = rig.make_point("C", 30.0)
        p_d = rig.make_point("D", 20.0)

        n_src = make_node(client, "SRC", "Ввод", "source")
        n_top = make_node(client, "TOP", "ГРЩ-1", "panel")
        n_ceh = make_node(client, "CEH", "Цех", "panel")
        n_srv = make_node(client, "SRV", "Серверная", "load")
        n_st = make_node(client, "ST", "Станок", "load")

        e_a = make_edge(client, n_src, n_top, p_a)
        e_b = make_edge(client, n_top, n_ceh, p_b)
        e_c = make_edge(client, n_top, n_srv, p_c)
        e_d = make_edge(client, n_ceh, n_st, p_d)
        publish(client, [e_a, e_b, e_c, e_d])

        body = overview(client)
        assert body["object_total"]["value"] == 100.0, body["object_total"]
        assert body["object_total_unavailable_reason"] is None

        by_name = {nb["name"]: nb for nb in body["network_branches"]}
        assert set(by_name.keys()) == {"Цех", "Серверная"}, by_name
        assert by_name["Цех"]["result"]["value"] == 60.0
        assert by_name["Серверная"]["result"]["value"] == 30.0
        # Станок (D) — на уровень глубже Цеха, не на уровне 1.
        assert not any(nb["edge_id"] == e_d["id"] for nb in body["network_branches"])

        assert body["imbalance_value"] == 10.0, body
        assert body["imbalance_percent"] == 10.0, body
        assert body["boundary_coverage"] == "verified"
        assert body["unmetered_branches"] == []

        # Узел ГРЩ-1: 10 / 10 % — тот же расчёт, что и объект целиком,
        # поскольку ГРЩ-1 и есть e_in.to_node объекта.
        r = node_balance(client, n_top["id"])
        assert r.status_code == 200, r.get_json()
        nb_top = r.get_json()
        assert nb_top["value"] == 10.0, nb_top
        assert round(nb_top["explanation"]["percentage"], 2) == 10.0, nb_top
        assert nb_top["unavailable_reason"] is None
        assert {o["edge_id"] for o in nb_top["outputs"]} == {e_b["id"], e_c["id"]}

        # Узел Цех: вход B=60, выход D=20, небаланс 40, 66.67%, verified.
        r = node_balance(client, n_ceh["id"])
        assert r.status_code == 200, r.get_json()
        nb_ceh = r.get_json()
        assert nb_ceh["value"] == 40.0, nb_ceh
        assert round(nb_ceh["explanation"]["percentage"], 2) == 66.67, nb_ceh
        assert nb_ceh["boundary_coverage"] == "verified"
        assert nb_ceh["input"]["point_id"] == p_b.id
        assert [o["point_id"] for o in nb_ceh["outputs"]] == [p_d.id]

        # T11 (A43): dimension=branch за тот же период даёт те же числа,
        # что network_branches на Обзоре.
        r = client.post("/api/v2/reports/query", json={
            "dimension": "branch", "from": 0, "to": HOUR, "timezone": "UTC"})
        assert r.status_code == 200, r.get_json()
        rows = r.get_json()["rows"]
        by_name_rows = {row["name"]: row["result"]["value"] for row in rows}
        by_name_nb = {nb["name"]: nb["result"]["value"] for nb in body["network_branches"]}
        assert by_name_rows == by_name_nb, (by_name_rows, by_name_nb)

        print("[OK] T1: итог объекта 100, ветви уровня 1 {Цех:60, Серверная:30}, "
              "небаланс 10/10%, Станок не на уровне 1; узел ГРЩ-1 10/10%, "
              "узел Цех 40/66.67% (verified); dimension=branch совпадает с Обзором")
    finally:
        db.close(); os.unlink(path)


# ---------------------------------------------------------------------
# T2: как T1 без D, без единой группы — раньше показывало 100%.
# ---------------------------------------------------------------------

def test_t2_no_groups_at_all_no_longer_shows_100_percent():
    client, db, path = make_client()
    try:
        rig = Rig(db)
        p_a = rig.make_point("A", 100.0)
        p_b = rig.make_point("B", 60.0)
        p_c = rig.make_point("C", 30.0)

        n_src = make_node(client, "SRC", "Ввод", "source")
        n_top = make_node(client, "TOP", "ГРЩ-1", "panel")
        n_b = make_node(client, "NB", "Цех", "load")
        n_c = make_node(client, "NC", "Серверная", "load")
        e_a = make_edge(client, n_src, n_top, p_a)
        e_b = make_edge(client, n_top, n_b, p_b)
        e_c = make_edge(client, n_top, n_c, p_c)
        publish(client, [e_a, e_b, e_c])

        body = overview(client)
        assert body["imbalance_value"] == 10.0, (
            "на 0.17.0 без единой группы небаланс молча показывал 100%", body)
        assert body["imbalance_percent"] == 10.0, body
        assert body["branches"] == [], body["branches"]
        print("[OK] T2: без единой учётной группы небаланс всё равно 10/10% "
              "(было 100/100% до партии 7)")
    finally:
        db.close(); os.unlink(path)


# ---------------------------------------------------------------------
# T3: T2 + группы «Цех»={B,C} и «Арендатор»={B} — точка в двух группах,
# раньше уводило небаланс в -50%.
# ---------------------------------------------------------------------

def test_t3_point_in_two_groups_no_longer_double_counts_imbalance():
    client, db, path = make_client()
    try:
        rig = Rig(db)
        p_a = rig.make_point("A", 100.0)
        p_b = rig.make_point("B", 60.0)
        p_c = rig.make_point("C", 30.0)

        n_src = make_node(client, "SRC", "Ввод", "source")
        n_top = make_node(client, "TOP", "ГРЩ-1", "panel")
        n_b = make_node(client, "NB", "Цех", "load")
        n_c = make_node(client, "NC", "Серверная", "load")
        e_a = make_edge(client, n_src, n_top, p_a)
        e_b = make_edge(client, n_top, n_b, p_b)
        e_c = make_edge(client, n_top, n_c, p_c)
        publish(client, [e_a, e_b, e_c])

        g_ceh = client.post("/api/v2/groups", json={"name": "Цех"}).get_json()
        g_arend = client.post("/api/v2/groups", json={"name": "Арендатор"}).get_json()
        for pid in (p_b.id, p_c.id):
            client.post(f"/api/v2/groups/{g_ceh['id']}/members",
                        json={"point_id": pid, "expected_revision": current_rev(client)})
        client.post(f"/api/v2/groups/{g_arend['id']}/members",
                    json={"point_id": p_b.id, "expected_revision": current_rev(client)})

        body = overview(client)
        assert body["imbalance_value"] == 10.0, (
            "на 0.17.0 точка B в двух группах уводила небаланс в -50%", body)
        assert body["imbalance_percent"] == 10.0, body

        by_name = {b["name"]: b["result"]["value"] for b in body["branches"]}
        assert by_name == {"Цех": 90.0, "Арендатор": 60.0}, by_name
        print("[OK] T3: точка в двух учётных группах — группы показывают 90/60 "
              "как и раньше, но небаланс объекта остался 10/10% (было -50%)")
    finally:
        db.close(); os.unlink(path)


# ---------------------------------------------------------------------
# T4: ЩР-1→Станки 70, →Освещение 40, →Розетки без счётчика.
# ---------------------------------------------------------------------

def test_t4_unmetered_branch_visible_and_named():
    client, db, path = make_client()
    try:
        rig = Rig(db)
        p_a = rig.make_point("A", 100.0)
        p_b = rig.make_point("B", 70.0)
        p_c = rig.make_point("C", 40.0)

        n_src = make_node(client, "SRC", "Ввод", "source")
        n_shr = make_node(client, "SHR", "ЩР-1", "panel")
        n_st = make_node(client, "ST", "Станки", "load")
        n_osv = make_node(client, "OSV", "Освещение", "load")
        n_roz = make_node(client, "ROZ", "Розетки", "load")

        e_a = make_edge(client, n_src, n_shr, p_a)
        e_st = make_edge(client, n_shr, n_st, p_b)
        e_osv = make_edge(client, n_shr, n_osv, p_c)
        e_roz = make_edge(client, n_shr, n_roz)  # без счётчика
        publish(client, [e_a, e_st, e_osv, e_roz])

        body = overview(client)
        assert body["imbalance_value"] == -10.0, body
        assert body["imbalance_percent"] == -10.0, body
        assert body["boundary_coverage"] == "has_unmetered_branches"
        assert len(body["unmetered_branches"]) == 1
        assert body["unmetered_branches"][0]["name"] == "Розетки"
        assert body["unmetered_branches"][0]["edge_id"] == e_roz["id"]
        print("[OK] T4: небаланс -10/-10% при неизмеренной ветви «Розетки», "
              "boundary_coverage=has_unmetered_branches")
    finally:
        db.close(); os.unlink(path)


# ---------------------------------------------------------------------
# T5: Ввод→ЩР-1 100; ЩР-1→ЩР-2 без счётчика; ЩР-2→X 50, ЩР-2→Y 30.
# ---------------------------------------------------------------------

def test_t5_unmetered_intermediate_line_does_not_block_deeper_boundaries():
    client, db, path = make_client()
    try:
        rig = Rig(db)
        p_in = rig.make_point("IN", 100.0)
        p_x = rig.make_point("X", 50.0)
        p_y = rig.make_point("Y", 30.0)

        n_src = make_node(client, "SRC", "Ввод", "source")
        n_shr1 = make_node(client, "SHR1", "ЩР-1", "panel")
        n_shr2 = make_node(client, "SHR2", "ЩР-2", "panel")
        n_x = make_node(client, "NX", "X", "load")
        n_y = make_node(client, "NY", "Y", "load")

        e_in = make_edge(client, n_src, n_shr1, p_in)
        e_mid = make_edge(client, n_shr1, n_shr2)  # без счётчика
        e_x = make_edge(client, n_shr2, n_x, p_x)
        e_y = make_edge(client, n_shr2, n_y, p_y)
        publish(client, [e_in, e_mid, e_x, e_y])

        body = overview(client)
        by_name = {nb["name"] for nb in body["network_branches"]}
        assert by_name == {"X", "Y"}, by_name
        assert body["imbalance_value"] == 20.0, body
        assert body["imbalance_percent"] == 20.0, body
        assert body["boundary_coverage"] == "verified"
        assert body["unmetered_branches"] == [], (
            "линия ЩР-1→ЩР-2 не должна попадать в unmetered_branches — "
            "в её поддереве ЕСТЬ измеряемые линии (X, Y)", body)

        r = node_balance(client, n_shr2["id"])
        nb = r.get_json()
        assert nb["unavailable_reason"] == "input_unmetered", nb
        assert {o["point_id"] for o in nb["outputs"]} == {p_x.id, p_y.id}
        print("[OK] T5: неизмеренная промежуточная линия ЩР-1→ЩР-2 не мешает "
              "найти X/Y глубже; узел ЩР-2 сам — input_unmetered, но выходы видны")
    finally:
        db.close(); os.unlink(path)


# ---------------------------------------------------------------------
# T6: Ввод→ЩР-1 без счётчика; ЩР-1→X 50.
# ---------------------------------------------------------------------

def test_t6_unmetered_single_input_makes_object_total_null():
    client, db, path = make_client()
    try:
        rig = Rig(db)
        p_x = rig.make_point("X", 50.0)

        n_src = make_node(client, "SRC", "Ввод", "source")
        n_shr = make_node(client, "SHR", "ЩР-1", "panel")
        n_x = make_node(client, "NX", "X", "load")
        e_in = make_edge(client, n_src, n_shr)  # без счётчика
        e_x = make_edge(client, n_shr, n_x, p_x)
        publish(client, [e_in, e_x])

        body = overview(client)
        assert body["object_total"] is not None, body
        assert body["object_total"]["value"] is None, body["object_total"]
        assert body["object_total"]["known_value"] is None, body["object_total"]
        assert body["object_total"]["availability"] == "missing", body["object_total"]
        assert body["object_total_unavailable_reason"] == "unmetered_input", body
        assert body["imbalance_value"] is None, body
        assert body["imbalance_percent_reason"] == "object_total_incomplete", body
        assert len(body["unmetered_inputs"]) == 1
        assert body["unmetered_inputs"][0]["edge_id"] == e_in["id"]
        print("[OK] T6: единственный неизмеренный ввод -> object_total.value=null "
              "(unmetered_input), небаланс null (object_total_incomplete)")
    finally:
        db.close(); os.unlink(path)


# ---------------------------------------------------------------------
# T7: S1→P1 100 (счётчик), S2→P2 без счётчика; P1→L 60.
# ---------------------------------------------------------------------

def test_t7_partially_metered_inputs_give_known_value_but_no_total():
    client, db, path = make_client()
    try:
        rig = Rig(db)
        p1 = rig.make_point("P1", 100.0)
        p_l = rig.make_point("L", 60.0)

        n_s1 = make_node(client, "S1", "S1", "source")
        n_s2 = make_node(client, "S2", "S2", "source")
        n_p1 = make_node(client, "NP1", "P1", "panel")
        n_p2 = make_node(client, "NP2", "P2", "load")
        n_l = make_node(client, "NL", "L", "load")

        e1 = make_edge(client, n_s1, n_p1, p1)
        e2 = make_edge(client, n_s2, n_p2)  # без счётчика
        e3 = make_edge(client, n_p1, n_l, p_l)
        publish(client, [e1, e2, e3])

        body = overview(client)
        assert body["object_total"]["value"] is None, body["object_total"]
        assert body["object_total"]["known_value"] == 100.0, body["object_total"]
        assert body["object_total"]["availability"] == "partial", body["object_total"]
        assert body["object_total_unavailable_reason"] == "unmetered_input", body
        assert [u["edge_id"] for u in body["unmetered_inputs"]] == [e2["id"]]
        assert body["imbalance_value"] is None, body
        assert body["imbalance_percent_reason"] == "object_total_incomplete", body
        # Несмотря на неполный итог объекта, ветвь L под измеряемым вводом
        # P1 всё равно видна (первый уровень исследуется по КАЖДОМУ
        # измеряемому вводу независимо).
        assert any(nb["name"] == "L" and nb["result"]["value"] == 60.0
                   for nb in body["network_branches"]), body["network_branches"]
        print("[OK] T7: один вход измерен (100), другой нет -> known_value=100, "
              "partial, unmetered_inputs=[S2→P2]; небаланс null "
              "(object_total_incomplete); ветвь под измеряемым вводом всё равно видна")
    finally:
        db.close(); os.unlink(path)


# ---------------------------------------------------------------------
# T8 (A10): ввод 100; X=0 (данные есть, ноль), Y=60 -> небаланс 40.
# Вариант: у Y данных нет совсем.
# ---------------------------------------------------------------------

def test_t8_zero_data_vs_missing_data_distinguished_in_network_balance():
    client, db, path = make_client()
    try:
        rig = Rig(db)
        p_in = rig.make_point("IN", 100.0)
        p_x = rig.make_point("X", 0.0)
        p_y = rig.make_point("Y", 60.0)

        n_src = make_node(client, "SRC", "Ввод", "source")
        n_top = make_node(client, "TOP", "ЩР", "panel")
        n_x = make_node(client, "NX", "X", "load")
        n_y = make_node(client, "NY", "Y", "load")
        e_in = make_edge(client, n_src, n_top, p_in)
        e_x = make_edge(client, n_top, n_x, p_x)
        e_y = make_edge(client, n_top, n_y, p_y)
        publish(client, [e_in, e_x, e_y])

        body = overview(client)
        assert body["imbalance_value"] == 40.0, body
        print("[OK] T8a: X=0 (данные есть) + Y=60 -> небаланс 100-60=40")
        db.close(); os.unlink(path)
    except Exception:
        db.close(); os.unlink(path)
        raise

    # Вариант: у Y данных нет совсем (ни одного агрегата).
    client, db, path = make_client()
    try:
        rig = Rig(db)
        p_in = rig.make_point("IN", 100.0)
        p_x = rig.make_point("X", 0.0)
        p_y = rig.make_point("Y", None)  # ни одного агрегата

        n_src = make_node(client, "SRC", "Ввод", "source")
        n_top = make_node(client, "TOP", "ЩР", "panel")
        n_x = make_node(client, "NX", "X", "load")
        n_y = make_node(client, "NY", "Y", "load")
        e_in = make_edge(client, n_src, n_top, p_in)
        e_x = make_edge(client, n_top, n_x, p_x)
        e_y = make_edge(client, n_top, n_y, p_y)
        publish(client, [e_in, e_x, e_y])

        body = overview(client)
        assert body["imbalance_value"] is None, body
        assert body["imbalance"]["availability"] == "partial", body["imbalance"]
        assert str(p_y.id) in body["imbalance"]["missing_ids"], body["imbalance"]
        print("[OK] T8b: Y без данных совсем -> imbalance.value=null, partial, "
              "Y в missing_ids")
    finally:
        db.close(); os.unlink(path)


# ---------------------------------------------------------------------
# T9 (A11): ввод 0, выход 0 -> небаланс 0, процент null (zero_or_negative_base).
# ---------------------------------------------------------------------

def test_t9_zero_input_and_output_gives_zero_imbalance_and_null_percent():
    client, db, path = make_client()
    try:
        rig = Rig(db)
        p_in = rig.make_point("IN", 0.0)
        p_out = rig.make_point("OUT", 0.0)

        n_src = make_node(client, "SRC", "Ввод", "source")
        n_top = make_node(client, "TOP", "ЩР", "panel")
        n_out = make_node(client, "NOUT", "Выход", "load")
        e_in = make_edge(client, n_src, n_top, p_in)
        e_out = make_edge(client, n_top, n_out, p_out)
        publish(client, [e_in, e_out])

        body = overview(client)
        assert body["imbalance_value"] == 0.0, body
        assert body["imbalance_percent"] is None, body
        assert body["imbalance_percent_reason"] == "zero_or_negative_base", body
        print("[OK] T9: ввод=0 и выход=0 -> небаланс 0, процент null "
              "(zero_or_negative_base), без деления на ноль и фиктивных 100%")
    finally:
        db.close(); os.unlink(path)


# ---------------------------------------------------------------------
# T10: баланс узла-листа и узла-источника; 404 на несуществующий узел;
# 400 на to<=from.
# ---------------------------------------------------------------------

def test_t10_leaf_and_source_node_balance_plus_404_and_400():
    client, db, path = make_client()
    try:
        rig = Rig(db)
        p_in = rig.make_point("IN", 10.0)

        n_src = make_node(client, "SRC", "Ввод", "source")
        n_leaf = make_node(client, "LEAF", "Тупик", "load")
        e_in = make_edge(client, n_src, n_leaf, p_in)
        publish(client, [e_in])

        # Узел-лист с измеряемым входом, но без исходящих линий вообще.
        r = node_balance(client, n_leaf["id"])
        assert r.status_code == 200, r.get_json()
        nb_leaf = r.get_json()
        assert nb_leaf["unavailable_reason"] == "no_outgoing_lines", nb_leaf
        assert nb_leaf["value"] is None, nb_leaf

        # Узел-источник — у него по определению нет входящей линии
        # (source не бывает приёмником, topology_service.validate_forest).
        r = node_balance(client, n_src["id"])
        assert r.status_code == 200, r.get_json()
        nb_src = r.get_json()
        assert nb_src["unavailable_reason"] == "no_incoming_line", nb_src
        assert nb_src["value"] is None, nb_src

        # 404 — неизвестный узел.
        r = node_balance(client, 999999)
        assert r.status_code == 404, r.get_json()

        # 400 — период to <= from.
        r = client.post(f"/api/v2/topology/nodes/{n_leaf['id']}/balance",
                         json={"from": HOUR, "to": 0, "timezone": "UTC"})
        assert r.status_code == 400, r.get_json()

        print("[OK] T10: узел-лист -> no_outgoing_lines, узел-источник -> "
              "no_incoming_line, неизвестный узел -> 404, to<=from -> 400")
    finally:
        db.close(); os.unlink(path)


# ---------------------------------------------------------------------
# T12 (A43-ревизия): configuration_revision_id — эхо в ответе,
# неизвестная ревизия -> 404 (для новой ручки баланса узла).
# ---------------------------------------------------------------------

def test_t12_node_balance_pins_revision_and_rejects_unknown():
    client, db, path = make_client()
    try:
        rig = Rig(db)
        p_in = rig.make_point("IN", 10.0)
        n_src = make_node(client, "SRC", "Ввод", "source")
        n_out = make_node(client, "OUT", "Выход", "load")
        e_in = make_edge(client, n_src, n_out, p_in)
        publish(client, [e_in])
        rev = current_rev(client)

        r = node_balance(client, n_out["id"])
        assert r.get_json()["configuration_revision_id"] == rev

        r2 = node_balance(client, n_out["id"], configuration_revision_id=rev + 999)
        assert r2.status_code == 404, r2.get_json()

        r3 = node_balance(client, n_out["id"], configuration_revision_id=rev)
        assert r3.status_code == 200, r3.get_json()
        assert r3.get_json()["configuration_revision_id"] == rev

        print("[OK] T12: баланс узла фиксирует запрошенную ревизию в ответе; "
              "неизвестная ревизия -> 404")
    finally:
        db.close(); os.unlink(path)


# ---------------------------------------------------------------------
# docs/review-repro/repro_imbalance.py -> тест: все три сценария дают
# 10 / 10 % (до партии 7: 100%, 10% "случайно", -50% из-за двойного счёта).
# ---------------------------------------------------------------------

def _repro_scenario(with_group, overlap=False):
    client, db, path = make_client()
    try:
        p_in = None
        rig = Rig(db)
        p_in = rig.make_point("IN", 100.0)
        p1 = rig.make_point("P1", 60.0)
        p2 = rig.make_point("P2", 30.0)

        n_src = make_node(client, "SRC", "Ввод", "source")
        n_shr = make_node(client, "SHR", "ЩР-1", "panel")
        n_1 = make_node(client, "N1", "Станки", "load")
        n_2 = make_node(client, "N2", "Освещение", "load")
        e_in = make_edge(client, n_src, n_shr, p_in)
        e_1 = make_edge(client, n_shr, n_1, p1)
        e_2 = make_edge(client, n_shr, n_2, p2)
        publish(client, [e_in, e_1, e_2])

        if with_group:
            groups = [("Цех", [p1, p2])]
            if overlap:
                groups.append(("Арендатор А", [p1]))
            for name, members in groups:
                g = client.post("/api/v2/groups", json={
                    "name": name, "expected_revision": current_rev(client)}).get_json()
                for p in members:
                    client.post(f"/api/v2/groups/{g['id']}/members", json={
                        "point_id": p.id, "expected_revision": current_rev(client)})

        return overview(client)
    finally:
        db.close(); os.unlink(path)


def test_repro_imbalance_all_three_scenarios_give_10_percent():
    """docs/review-repro/repro_imbalance.py, переведённый в постоянный
    тест: на 0.17.0 три сценария давали 100%/10%("случайно")/-50%; после
    партии 7 все три обязаны давать одно и то же корректное 10/10%,
    потому что небаланс больше не зависит от состава учётных групп."""
    for with_group, overlap in ((False, False), (True, False), (True, True)):
        body = _repro_scenario(with_group, overlap)
        assert body["object_total"]["value"] == 100.0, (with_group, overlap, body)
        assert body["imbalance_value"] == 10.0, (
            "groups=%r overlap=%r должен давать 10.0, а не %r"
            % (with_group, overlap, body["imbalance_value"]))
        assert body["imbalance_percent"] == 10.0, (with_group, overlap, body)
    print("[OK] repro_imbalance: все три сценария (без групп / с группой / "
          "с пересекающимися группами) теперь дают одинаковые 10.0/10.0%")


if __name__ == "__main__":
    test_t1_object_and_node_balance_with_nested_unbranched_consumer()
    test_t2_no_groups_at_all_no_longer_shows_100_percent()
    test_t3_point_in_two_groups_no_longer_double_counts_imbalance()
    test_t4_unmetered_branch_visible_and_named()
    test_t5_unmetered_intermediate_line_does_not_block_deeper_boundaries()
    test_t6_unmetered_single_input_makes_object_total_null()
    test_t7_partially_metered_inputs_give_known_value_but_no_total()
    test_t8_zero_data_vs_missing_data_distinguished_in_network_balance()
    test_t9_zero_input_and_output_gives_zero_imbalance_and_null_percent()
    test_t10_leaf_and_source_node_balance_plus_404_and_400()
    test_t12_node_balance_pins_revision_and_rejects_unknown()
    test_repro_imbalance_all_three_scenarios_give_10_percent()
    print("[ALL OK] test_step40_topology_balance")

"""Тесты Шага 49 (партия 11, этап 11.2b): сценарии приёмки §13 ТЗ, для
которых в тестах не было даже метки (docs/acceptance-matrix.md): A01, A04
(объяснение пути перекрытия), A05, A06, A14, A15, A25, A38 (вторая
половина), A39, A44.

Правило этапа (docs/TZ-finish-plan.md, §11.2b): ожидаемые числа посчитаны
вручную ДО запуска и записаны в таблице ниже; если тест краснеет,
ожидание не меняется — это найденная ошибка (в матрицу, не в тест).

  A01  Одиночный прибор без групп/планов/сети: точка 7.5 кВт·ч за час 0.
       measured=7.5 (complete); Обзор без топологии отвечает 200 и не
       выдаёт итог объекта; отчёт по точке = 7.5; плана нет — список []
  A04  Сеть: Ввод→ГРЩ[A=100]; ГРЩ→Цех[B=60]; ГРЩ→Сервер[C=30];
       Цех→Станок[D=20]. Сумма A+B и B+D — 409 double_counting с путём
       «от узла … до узла …»; C+D независимы = 50; сравнение A,B даёт
       100 и 60 без общего итога
  A05  Группа Объект{D} и дочерняя Цех{D,E}; D=20, E=5. Отчёт по группе
       Объект = 25 (D один раз), состав [D,E], via(D)=[Объект,Цех],
       via(E)=[Цех]
  A06  G1={a=10,b=20}, G2={b=20,c=5}. Строки отчёта: 30 и 25 (без
       конфликта). Итог по объединению = sum_points(a,b,c) = 35, а не
       30+25=55
  A14  Накопитель 100.0 в начале часа 0 и 160.0 в середине часа 4, между
       ними записей нет. Часы 0..4: дельты 0,0,0,0,60; ни один не «ok»
       (все edge_approx); сумма = 60 = 160−100; равномерных долей 12 нет
  A15  Энергия не менялась 2 ч, но сообщения (Uptime) идут: статус OK.
       Тишина 700 с (> no_connection_timeout_s=600): NO_CONNECTION;
       тишина 300 с (между stale_warning=120 и 600): WARNING, не OK
  A25  Щит ЩР без прибора: есть в сети (GET /topology/nodes) и на
       однолинейной схеме (план, элемент kind=node); баланс узла —
       unavailable_reason=input_unmetered, значения по узлу нет
  A38б MQTT видит прибор «ghost» без регистрации: его нет ни в snapshot,
       ни в списке точек; зарегистрированная точка (7.5) считается как
       обычно
  A39  P(hours {0:10, 10:7}) и Q({0:3, 10:4}) в группе G с часа 0; P
       архивирована в 5ч. as_was[0,1ч)=13; as_was[10ч,11ч)=4 (остаточные
       7 по P не возвращаются); current[0,1ч)=3; история P за час 0 = 10;
       членство P закрыто valid_to=5ч, строка сохранена
  A44  Сутки в зоне с переводом часов (Europe/Berlin, 2026): весна 23 ч
       и осень 25 ч. Каждый час 1.0 кВт·ч, первый час следующих суток
       (граница) = 100.0. Сутки1=23, Сутки2=123, Сутки1+Сутки2=146
       (= объединённый период); осенью 25 ч → 25.0

Самостоятельный скрипт (не pytest):
    python tests/test_step49_acceptance_gaps.py
"""

from __future__ import annotations

import io
import os
import struct
import sys
import tempfile
import time
from datetime import datetime, timezone

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from wb_energy_meter.db import Database
from wb_energy_meter.repo import GroupRepo, MeterRepo
from wb_energy_meter.api import create_app, _AppState
from wb_energy_meter.model import MeterRegistry, MeterState, MeterStatus, ControlState
from wb_energy_meter.config import StatusConfig
from wb_energy_meter.status import StatusEngine
from wb_energy_meter.point_repo import MeteringPointRepo, MeterSourceRepo
from wb_energy_meter.aggregates_repo import AggregateRepo, HourlyAggregate
from wb_energy_meter.aggregator import compute_hourly_aggregate
from wb_energy_meter.binding_service import PointBindingRepo
from wb_energy_meter.group_repo_v2 import GroupRepoV2
from wb_energy_meter.wb_db_client import HistoryPoint

HOUR = 3600
TOL = 1e-6


def approx(a, b, tol=TOL):
    return a is not None and b is not None and abs(a - b) < tol


def make_png(width, height):
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr_data = struct.pack(">II", width, height) + b"\x08\x06\x00\x00\x00"
    ihdr = struct.pack(">I", len(ihdr_data)) + b"IHDR" + ihdr_data + b"\x00\x00\x00\x00"
    return sig + ihdr


def make_client(with_plans=False):
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
        plans_dir=tempfile.mkdtemp() if with_plans else None,
    )
    app = create_app(state)
    return app.test_client(), db, path, registry


def current_rev(client):
    return client.get("/api/v2/revision").get_json()["configuration_revision"]


class Rig:
    """Точка + прибор + агрегаты за один вызов (та же обвязка, что в
    test_step28_reports_query.py)."""

    def __init__(self, db):
        self.meters = MeterRepo(db, GroupRepo(db))
        self.sources = MeterSourceRepo(db)
        self.points = MeteringPointRepo(db)
        self.aggregates = AggregateRepo(db)
        self.bindings = PointBindingRepo(db)

    def add_hour_at(self, meter_id, start, kwh):
        self.aggregates.upsert(HourlyAggregate(
            meter_id=meter_id, period_start=start, period_end=start + HOUR,
            ap_energy_start=0.0, ap_energy_end=kwh, ap_energy_delta=kwh,
            p_avg=None, p_max=None, samples_count=1, quality_flag="ok",
            computed_at=start))

    def make_point_at(self, code, hours_kwh, device_id=None):
        """`hours_kwh` — {номер_часа: кВт·ч}."""
        m = self.meters.add(code, code)
        src = self.sources.open_source(m.id, "wb8-main", device_id or code)
        p = self.points.add(code, code)
        self.bindings.open_binding(p.id, src.id, "total_3p", valid_from=0)
        for h, kwh in hours_kwh.items():
            self.add_hour_at(m.id, h * HOUR, kwh)
        return p, m

    def make_point(self, code, kwh):
        return self.make_point_at(code, {0: kwh})[0]


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
    r = client.post("/api/v2/topology/publish", json={
        "edge_ids": [e["id"] for e in edges],
        "expected_configuration_revision": current_rev(client),
    })
    assert r.status_code == 200, r.get_json()


def metrics(client, mode, point_ids, t_from=0, t_to=HOUR, **extra):
    body = {"mode": mode, "point_ids": point_ids, "from": t_from, "to": t_to,
            "timezone": "UTC"}
    body.update(extra)
    return client.post("/api/v2/metrics/query", json=body)


def add_member(client, group_id, point_id, **extra):
    body = {"point_id": point_id, "expected_revision": current_rev(client)}
    body.update(extra)
    r = client.post(f"/api/v2/groups/{group_id}/members", json=body)
    assert r.status_code in (200, 201), r.get_json()
    return r.get_json()


# ---------------------------------------------------------------------
# A01: одиночный прибор, нет групп/планов/сети
# ---------------------------------------------------------------------

def test_a01_single_meter_without_groups_plans_or_topology():
    client, db, path, registry = make_client(with_plans=True)
    try:
        rig = Rig(db)
        p = rig.make_point("SOLO", 7.5)

        r = client.get("/api/v2/points")
        assert r.status_code == 200, r.get_json()
        body = r.get_json()
        points = body["points"] if isinstance(body, dict) else body
        assert [x["code"] for x in points] == ["SOLO"], points

        # расход точки
        r = metrics(client, "measured", [p.id])
        assert r.status_code == 200, r.get_json()
        res = r.get_json()
        assert res["value"] == 7.5, res
        assert res["availability"] == "complete", res

        # отчёт по точке
        r = client.post("/api/v2/reports/query", json={
            "dimension": "point", "scope_ids": [p.id],
            "from": 0, "to": HOUR, "timezone": "UTC"})
        assert r.status_code == 200, r.get_json()
        assert approx(r.get_json()["rows"][0]["result"]["value"], 7.5)

        # групп, планов, сети нет — и это не ошибка
        r = client.get("/api/v2/groups")
        assert r.status_code == 200 and r.get_json() in ([], {"groups": []}), r.get_json()
        r = client.get("/api/v2/plans")
        assert r.status_code == 200 and r.get_json() == [], r.get_json()

        # Обзор без топологии и групп отвечает штатно и не выдумывает итог
        r = client.post("/api/v2/overview/summary",
                        json={"from": 0, "to": HOUR, "timezone": "UTC"})
        assert r.status_code == 200, r.get_json()
        ov = r.get_json()
        assert ov["object_total"] is None or ov["object_total"]["value"] is None, ov
        assert ov["branches"] == [], ov["branches"]

        # текущие значения: точка видна в snapshot (привязана, прибор не виден)
        r = client.get("/api/v2/snapshot")
        assert r.status_code == 200, r.get_json()
        items = {it["code"]: it for it in r.get_json()["points"]}
        assert items["SOLO"]["binding_status"] == "bound"
        print("[OK] A01: одиночный прибор без групп/планов/сети — расход 7.5, "
              "отчёт, snapshot, Обзор работают; итог объекта не выдуман")
    finally:
        db.close(); os.unlink(path)


# ---------------------------------------------------------------------
# A04: 409 с объяснением пути; сравнение без общего итога
# ---------------------------------------------------------------------

def _build_a04_network(client, rig):
    p_a = rig.make_point("A", 100.0)
    p_b = rig.make_point("B", 60.0)
    p_c = rig.make_point("C", 30.0)
    p_d = rig.make_point("D", 20.0)

    n_src = make_node(client, "SRC", "Ввод", "source")
    n_grsh = make_node(client, "GRSH", "ГРЩ", "panel")
    n_shop = make_node(client, "SHOP", "Цех", "panel")
    n_srv = make_node(client, "SRV", "Сервер", "load")
    n_mach = make_node(client, "MACH", "Станок", "load")
    edges = [
        make_edge(client, n_src, n_grsh, p_a),
        make_edge(client, n_grsh, n_shop, p_b),
        make_edge(client, n_grsh, n_srv, p_c),
        make_edge(client, n_shop, n_mach, p_d),
    ]
    publish(client, edges)
    return p_a, p_b, p_c, p_d


def test_a04_overlap_409_explains_path_and_comparison_allowed():
    client, db, path, registry = make_client()
    try:
        rig = Rig(db)
        p_a, p_b, p_c, p_d = _build_a04_network(client, rig)

        for label, ids in (("A+B", [p_a.id, p_b.id]), ("B+D", [p_b.id, p_d.id])):
            r = metrics(client, "sum", ids)
            assert r.status_code == 409, (label, r.status_code, r.get_json())
            err = r.get_json()
            assert err["code"] == "double_counting", err
            msg = err["message"]
            assert "путь от узла" in msg and "до узла" in msg, (
                f"{label}: 409 без объяснения пути перекрытия: {msg!r}")

        # C и D независимы (разные ветви) — сумма разрешена: 30 + 20 = 50
        r = metrics(client, "sum", [p_c.id, p_d.id])
        assert r.status_code == 200, r.get_json()
        assert approx(r.get_json()["value"], 50.0), r.get_json()

        # сравнение A и B разрешено без общего итога: две строки, суммы нет
        r = metrics(client, "comparison", [p_a.id, p_b.id])
        assert r.status_code == 200, r.get_json()
        cmp_body = r.get_json()
        assert set(cmp_body.keys()) == {str(p_a.id), str(p_b.id)}, cmp_body
        assert approx(cmp_body[str(p_a.id)]["value"], 100.0)
        assert approx(cmp_body[str(p_b.id)]["value"], 60.0)
        print("[OK] A04: A+B и B+D → 409 с путём «от узла … до узла …»; C+D=50; "
              "сравнение A=100, B=60 без общего итога")
    finally:
        db.close(); os.unlink(path)


# ---------------------------------------------------------------------
# A05: точка в группе и в дочерней группе — один раз, источник виден
# ---------------------------------------------------------------------

def test_a05_point_in_group_and_child_group_counted_once_with_provenance():
    client, db, path, registry = make_client()
    try:
        rig = Rig(db)
        p_d = rig.make_point("D", 20.0)
        p_e = rig.make_point("E", 5.0)

        root = client.post("/api/v2/groups", json={"name": "Объект"}).get_json()
        child = client.post("/api/v2/groups", json={
            "name": "Цех", "parent_id": root["id"]}).get_json()
        add_member(client, root["id"], p_d.id, valid_from=0)
        add_member(client, child["id"], p_d.id, valid_from=0)
        add_member(client, child["id"], p_e.id, valid_from=0)

        # composition_mode=current: связь «Цех → Объект» создана «сейчас»,
        # состав на сейчас включает дочернюю группу; показания — часа 0
        r = client.post("/api/v2/reports/query", json={
            "dimension": "group", "scope_ids": [root["id"]],
            "from": 0, "to": HOUR, "timezone": "UTC", "composition_mode": "current"})
        assert r.status_code == 200, r.get_json()
        row = r.get_json()["rows"][0]
        assert sorted(row["member_point_ids"]) == sorted([p_d.id, p_e.id]), row
        assert approx(row["result"]["value"], 25.0), row["result"]  # 20 + 5, не 45

        r = client.get(f"/api/v2/groups/{root['id']}/effective-members")
        assert r.status_code == 200, r.get_json()
        body = r.get_json()
        via = {m["point_id"]: sorted(m["via"]) for m in body["points"]}
        assert via[p_d.id] == sorted([root["id"], child["id"]]), via
        assert via[p_e.id] == [child["id"]], via
        print("[OK] A05: D в группе и в дочерней — Объект = 25 (D один раз), "
              "источники включения видны: D via [Объект, Цех], E via [Цех]")
    finally:
        db.close(); os.unlink(path)


# ---------------------------------------------------------------------
# A06: пересекающиеся группы — строки сравнимы, итог по объединению
# ---------------------------------------------------------------------

def test_a06_overlapping_groups_rows_comparable_total_by_union():
    client, db, path, registry = make_client()
    try:
        rig = Rig(db)
        p_a = rig.make_point("a", 10.0)
        p_b = rig.make_point("b", 20.0)
        p_c = rig.make_point("c", 5.0)

        g1 = client.post("/api/v2/groups", json={"name": "G1"}).get_json()
        g2 = client.post("/api/v2/groups", json={"name": "G2"}).get_json()
        add_member(client, g1["id"], p_a.id, valid_from=0)
        add_member(client, g1["id"], p_b.id, valid_from=0)
        add_member(client, g2["id"], p_b.id, valid_from=0)
        add_member(client, g2["id"], p_c.id, valid_from=0)

        r = client.post("/api/v2/reports/query", json={
            "dimension": "group", "scope_ids": [g1["id"], g2["id"]],
            "from": 0, "to": HOUR, "timezone": "UTC"})
        assert r.status_code == 200, r.get_json()
        rows = {row["id"]: row for row in r.get_json()["rows"]}
        assert approx(rows[g1["id"]]["result"]["value"], 30.0), rows[g1["id"]]
        assert approx(rows[g2["id"]]["result"]["value"], 25.0), rows[g2["id"]]
        assert rows[g1["id"]]["conflict_reason"] is None
        assert rows[g2["id"]]["conflict_reason"] is None

        # итог — по разрешённому множеству (объединению), не сумма строк
        union = set(rows[g1["id"]]["member_point_ids"]) | set(rows[g2["id"]]["member_point_ids"])
        assert union == {p_a.id, p_b.id, p_c.id}
        r = metrics(client, "sum", sorted(union))
        assert r.status_code == 200, r.get_json()
        total = r.get_json()["value"]
        assert approx(total, 35.0), total
        assert not approx(total, 30.0 + 25.0), "итог посчитан суммой строк — задвоение b"

        # и даже если точку b передать дважды, она учитывается один раз
        r = metrics(client, "sum", [p_a.id, p_b.id, p_b.id, p_c.id])
        assert approx(r.get_json()["value"], 35.0), r.get_json()
        print("[OK] A06: строки G1=30 и G2=25 сравнимы; итог по объединению {a,b,c} = 35, "
              "а не 30+25=55")
    finally:
        db.close(); os.unlink(path)


# ---------------------------------------------------------------------
# A14: между граничными накопителями нет записей — доли не выдуманы
# ---------------------------------------------------------------------

def test_a14_no_intermediate_samples_total_known_hourly_shares_not_invented():
    h0 = 1700002800  # кратно 3600
    p0 = HistoryPoint(timestamp=h0, value=100.0)
    p1 = HistoryPoint(timestamp=h0 + 4 * HOUR + 1800, value=160.0)
    points = [p0, p1]

    aggs = [compute_hourly_aggregate(1, h0 + k * HOUR, points) for k in range(5)]
    deltas = [a.ap_energy_delta for a in aggs]
    flags = [a.quality_flag for a in aggs]
    assert deltas == [0.0, 0.0, 0.0, 0.0, 60.0], deltas
    assert all(f == "edge_approx" for f in flags), flags  # ни один час не «ok»
    assert approx(sum(deltas), 160.0 - 100.0)
    assert 12.0 not in deltas, "расход размазан поровну по часам — доли выдуманы"

    # через учёт: расход за весь период известен и помечен edge_approx
    db_fd, db_path = tempfile.mkstemp(suffix=".sqlite3")
    os.close(db_fd); os.unlink(db_path)
    db = Database(path=db_path); db.open()
    try:
        from wb_energy_meter.accounting_service import measured_point
        rig = Rig(db)
        m = rig.meters.add("flat", "flat")
        src = rig.sources.open_source(m.id, "wb8-main", "flat")
        pt = rig.points.add("flat", "flat")
        rig.bindings.open_binding(pt.id, src.id, "total_3p", valid_from=0)
        for a in aggs:
            a.meter_id = m.id
            rig.aggregates.upsert(a)
        res = measured_point(rig.bindings, rig.aggregates, rig.sources, pt.id,
                             h0, h0 + 5 * HOUR)
        assert approx(res.value, 60.0), res.to_dict()
        assert "edge_approx" in res.quality_flags, res.quality_flags
        print("[OK] A14: часы 0..4 = [0,0,0,0,60], все edge_approx, сумма 60 = 160−100, "
              "равномерных долей нет; учёт видит 60 с флагом edge_approx")
    finally:
        db.close(); os.unlink(db_path)


# ---------------------------------------------------------------------
# A15: неизменная энергия при живом Uptime ≠ отказ; затем тишина
# ---------------------------------------------------------------------

def _alive_meter(registry, *, silence_s, energy_age_s=7200):
    meter = registry.get_or_create("wb-map3e_15")
    now = time.time()
    meter.last_any_ts = now - silence_s
    meter.last_measurement_ts = now - silence_s
    meter.controls["Total AP energy"] = ControlState(
        name="Total AP energy", value=318.0, meta={"type": "power_consumption"},
        last_update_ts=now - energy_age_s)
    meter.controls["Total P"] = ControlState(
        name="Total P", value=42.6, meta={"type": "power"}, last_update_ts=now - silence_s)
    meter.controls["Frequency"] = ControlState(
        name="Frequency", value=50.0, meta={"type": "value"}, last_update_ts=now - silence_s)
    for ph in ("L1", "L2", "L3"):
        meter.controls[f"Urms {ph}"] = ControlState(
            name=f"Urms {ph}", value=230.0, meta={"type": "voltage"},
            last_update_ts=now - silence_s)
        meter.controls[f"Irms {ph}"] = ControlState(
            name=f"Irms {ph}", value=1.0, meta={"type": "current"},
            last_update_ts=now - silence_s)
    return meter


def test_a15_unchanged_energy_with_live_uptime_is_ok_then_silence_is_not_green():
    cfg = StatusConfig()
    assert cfg.no_connection_timeout_s == 600 and cfg.stale_warning_timeout_s == 120

    # энергия не менялась 2 часа, но прибор шлёт сообщения (Uptime) — это не отказ
    registry = MeterRegistry()
    engine = StatusEngine(registry, cfg)
    meter = _alive_meter(registry, silence_s=5)
    status, _reason = engine._classify(meter)
    assert status == MeterStatus.OK, (status, _reason)

    # тишина 300 с (120 < 300 < 600): уже не зелёный, но и не «нет связи»
    registry = MeterRegistry()
    engine = StatusEngine(registry, cfg)
    status, reason = engine._classify(_alive_meter(registry, silence_s=300))
    assert status == MeterStatus.WARNING and "назад" in reason, (status, reason)

    # тишина 700 с (> 600): нет связи, зелёного «сейчас» нет
    registry = MeterRegistry()
    engine = StatusEngine(registry, cfg)
    status, reason = engine._classify(_alive_meter(registry, silence_s=700))
    assert status == MeterStatus.NO_CONNECTION, (status, reason)
    assert status != MeterStatus.OK
    print("[OK] A15: энергия не менялась 2 ч при живых сообщениях — OK; тишина 300 с — WARNING; "
          "700 с — NO_CONNECTION (не зелёный)")


# ---------------------------------------------------------------------
# A25: щит без прибора — в сети и на схеме, значения не выдуманы
# ---------------------------------------------------------------------

def test_a25_panel_without_meter_exists_in_network_and_on_plan_without_invented_values():
    client, db, path, registry = make_client(with_plans=True)
    try:
        rig = Rig(db)
        p_in = rig.make_point("IN", 100.0)
        p_x = rig.make_point("X", 50.0)
        p_y = rig.make_point("Y", 30.0)

        n_src = make_node(client, "SRC", "Ввод", "source")
        n_shr1 = make_node(client, "SHR1", "ЩР-1", "panel")
        n_shr2 = make_node(client, "SHR2", "ЩР-2 (без прибора)", "panel")
        n_x = make_node(client, "NX", "X", "load")
        n_y = make_node(client, "NY", "Y", "load")
        edges = [
            make_edge(client, n_src, n_shr1, p_in),
            make_edge(client, n_shr1, n_shr2),  # линия к щиту без счётчика
            make_edge(client, n_shr2, n_x, p_x),
            make_edge(client, n_shr2, n_y, p_y),
        ]
        publish(client, edges)

        # в сети щит существует
        r = client.get("/api/v2/topology/nodes")
        assert r.status_code == 200, r.get_json()
        body = r.get_json()
        nodes = body["nodes"] if isinstance(body, dict) else body
        assert n_shr2["id"] in {n["id"] for n in nodes}

        # на однолинейной схеме для него можно создать элемент (kind=node)
        r = client.post("/api/v2/plans", data={
            "name": "Однолинейная", "plan_kind": "single_line",
            "canvas_width": "2000", "canvas_height": "1200",
        }, content_type="multipart/form-data")
        assert r.status_code == 201, r.get_json()
        plan_id = r.get_json()["id"]
        r = client.post(f"/api/v2/plans/{plan_id}/items", json={
            "kind": "node", "node_id": n_shr2["id"],
            "geometry": {"x": 300, "y": 200}, "coord_space": "image_px_xy_v2"})
        assert r.status_code == 201, r.get_json()
        r = client.get(f"/api/v2/plans/{plan_id}")
        items = r.get_json()["items"]
        assert any(i.get("node_id") == n_shr2["id"] for i in items), items

        # собственного измеренного расхода у щита нет, и он не выдуман
        r = client.post(f"/api/v2/topology/nodes/{n_shr2['id']}/balance",
                        json={"from": 0, "to": HOUR, "timezone": "UTC"})
        assert r.status_code == 200, r.get_json()
        nb = r.get_json()
        assert nb["unavailable_reason"] == "input_unmetered", nb
        assert nb["value"] is None and nb["known_value"] is None, nb
        assert nb["availability"] == "missing", nb
        assert nb["input"]["point_id"] is None and nb["input"]["result"] is None, nb["input"]
        assert {o["point_id"] for o in nb["outputs"]} == {p_x.id, p_y.id}
        print("[OK] A25: щит без прибора есть в сети и на схеме; баланс узла — "
              "input_unmetered, собственный расход/ток не выдуман, выходы X, Y видны")
    finally:
        db.close(); os.unlink(path)


# ---------------------------------------------------------------------
# A38 (вторая половина): MQTT видит незарегистрированное устройство
# ---------------------------------------------------------------------

def test_a38_unregistered_mqtt_device_is_not_part_of_accounting():
    client, db, path, registry = make_client()
    try:
        rig = Rig(db)
        p = rig.make_point("REG", 7.5)

        ghost = registry.get_or_create("wb-map3e_ghost")
        ghost.last_any_ts = time.time()
        ghost.controls["Total P"] = ControlState(
            name="Total P", value=999.0, meta={"type": "power"}, last_update_ts=time.time())

        r = client.get("/api/v2/snapshot")
        assert r.status_code == 200, r.get_json()
        codes = [it["code"] for it in r.get_json()["points"]]
        assert codes == ["REG"], codes

        r = client.get("/api/v2/points")
        body = r.get_json()
        points = body["points"] if isinstance(body, dict) else body
        assert [x["code"] for x in points] == ["REG"], points

        r = metrics(client, "measured", [p.id])
        assert r.get_json()["value"] == 7.5
        print("[OK] A38: незарегистрированное устройство видно MQTT, но не в точках/"
              "snapshot и не входит в учёт; REG считается (7.5)")
    finally:
        db.close(); os.unlink(path)


# ---------------------------------------------------------------------
# A39: архивирование точки — состав с даты, история цела
# ---------------------------------------------------------------------

def test_a39_archived_point_leaves_current_composition_history_kept():
    client, db, path, registry = make_client()
    try:
        rig = Rig(db)
        p_p, _m = rig.make_point_at("P", {0: 10.0, 10: 7.0})
        p_q, _m2 = rig.make_point_at("Q", {0: 3.0, 10: 4.0})

        g = client.post("/api/v2/groups", json={"name": "G"}).get_json()
        add_member(client, g["id"], p_p.id, valid_from=0)
        add_member(client, g["id"], p_q.id, valid_from=0)

        archive_at = 5 * HOUR
        rig.points.archive(p_p.id, at=archive_at)

        def group_report(t_from, t_to, mode=None):
            body = {"dimension": "group", "scope_ids": [g["id"]],
                    "from": t_from, "to": t_to, "timezone": "UTC"}
            if mode:
                body["composition_mode"] = mode
            r = client.post("/api/v2/reports/query", json=body)
            assert r.status_code == 200, r.get_json()
            return r.get_json()["rows"][0]

        # прошлое «как было»: до архивации P в составе — 10 + 3
        row = group_report(0, HOUR)
        assert sorted(row["member_point_ids"]) == sorted([p_p.id, p_q.id]), row
        assert approx(row["result"]["value"], 13.0), row["result"]

        # после архивации: остаточные 7 по P за час 10 в итог не возвращаются
        row = group_report(10 * HOUR, 11 * HOUR)
        assert row["member_point_ids"] == [p_q.id], row
        assert approx(row["result"]["value"], 4.0), row["result"]

        # текущий состав: P нет, число за старый час — по Q
        row = group_report(0, HOUR, "current")
        assert row["member_point_ids"] == [p_q.id], row
        assert approx(row["result"]["value"], 3.0), row["result"]

        # история самой точки цела
        r = metrics(client, "measured", [p_p.id])
        assert approx(r.get_json()["value"], 10.0), r.get_json()

        # членство закрыто датой архивации, строка не удалена
        with db.read() as c:
            ms = c.execute(
                "SELECT valid_from, valid_to FROM group_memberships "
                "WHERE group_id = ? AND point_id = ?", (g["id"], p_p.id)).fetchall()
        assert len(ms) == 1 and ms[0]["valid_to"] == archive_at, [dict(x) for x in ms]

        eff = GroupRepoV2(db).resolve_effective_members(g["id"])
        assert [m["point_id"] for m in eff] == [p_q.id], eff
        print("[OK] A39: P архивирована в 5ч — as_was[0,1ч)=13, as_was[10ч,11ч)=4 (остаточные 7 "
              "не вернулись), current[0,1ч)=3; история P=10; членство закрыто valid_to=5ч")
    finally:
        db.close(); os.unlink(path)


# ---------------------------------------------------------------------
# A44: сутки с переводом часов: 23/25 ч, граница не задваивается
# ---------------------------------------------------------------------

def _epoch(y, mo, d, h):
    return int(datetime(y, mo, d, h, tzinfo=timezone.utc).timestamp())


def test_a44_dst_days_have_23_and_25_hours_and_boundary_not_counted_twice():
    client, db, path, registry = make_client()
    try:
        rig = Rig(db)
        m = rig.meters.add("dst", "dst")
        src = rig.sources.open_source(m.id, "wb8-main", "dst")
        p = rig.points.add("dst", "dst")
        rig.bindings.open_binding(p.id, src.id, "total_3p", valid_from=0)

        def fill(t_from, t_to, boundary_hour_start):
            t = t_from
            while t < t_to:
                rig.add_hour_at(m.id, t, 100.0 if t == boundary_hour_start else 1.0)
                t += HOUR

        # весна: Europe/Berlin, 29.03.2026, 23 часа (CET→CEST в 01:00 UTC)
        d1_from = _epoch(2026, 3, 28, 23)       # 29.03 00:00 CET
        d2_from = _epoch(2026, 3, 29, 22)       # 30.03 00:00 CEST
        d2_to = _epoch(2026, 3, 30, 22)         # 31.03 00:00 CEST
        assert d2_from - d1_from == 23 * HOUR
        fill(d1_from, d2_to, d2_from)

        def q(t_from, t_to):
            r = metrics(client, "measured", [p.id], t_from, t_to)
            assert r.status_code == 200, r.get_json()
            return r.get_json()["value"]

        day1, day2, both = q(d1_from, d2_from), q(d2_from, d2_to), q(d1_from, d2_to)
        assert approx(day1, 23.0), day1                     # 23 часа по 1.0
        assert approx(day2, 123.0), day2                    # 100 (граничный час) + 23
        assert approx(both, 146.0), both
        assert approx(day1 + day2, both), "граничный час учтён дважды или потерян"

        # осень: 25.10.2026, 25 часов (CEST→CET в 01:00 UTC)
        a1_from = _epoch(2026, 10, 24, 22)      # 25.10 00:00 CEST
        a2_from = _epoch(2026, 10, 25, 23)      # 26.10 00:00 CET
        a2_to = _epoch(2026, 10, 26, 23)
        assert a2_from - a1_from == 25 * HOUR
        fill(a1_from, a2_to, a2_from)
        assert approx(q(a1_from, a2_from), 25.0), q(a1_from, a2_from)
        assert approx(q(a1_from, a2_from) + q(a2_from, a2_to), q(a1_from, a2_to))
        print("[OK] A44: весенние сутки 23 ч = 23, следующие 123 (граничный час только в них), "
              "сумма 146 = объединённый период; осенние сутки 25 ч = 25")
    finally:
        db.close(); os.unlink(path)


if __name__ == "__main__":
    test_a01_single_meter_without_groups_plans_or_topology()
    test_a04_overlap_409_explains_path_and_comparison_allowed()
    test_a05_point_in_group_and_child_group_counted_once_with_provenance()
    test_a06_overlapping_groups_rows_comparable_total_by_union()
    test_a14_no_intermediate_samples_total_known_hourly_shares_not_invented()
    test_a15_unchanged_energy_with_live_uptime_is_ok_then_silence_is_not_green()
    test_a25_panel_without_meter_exists_in_network_and_on_plan_without_invented_values()
    test_a38_unregistered_mqtt_device_is_not_part_of_accounting()
    test_a39_archived_point_leaves_current_composition_history_kept()
    test_a44_dst_days_have_23_and_25_hours_and_boundary_not_counted_twice()
    print("\nВсе тесты Шага 49 (дыры приёмки §13) пройдены.")

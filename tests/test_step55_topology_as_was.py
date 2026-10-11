#!/usr/bin/env python3
"""Партия 12, этап 12.2: структура «как было» (`structure_mode`),
docs/design-history.md §2, docs/TZ-finish-plan.md §5.

Числа посчитаны вручную по фикстурам; «независимая сумма двух половин»
считается на ДРУГИХ БД с готовой «до»/«после» структурой, а не той же
функцией на той же БД.

Шкала времени: журнал начинается в 50-й час, данные — часы 100..139,
структура меняется в 120-й час (часы заданы `H(n) = n * 3600`).
Время записи журнала (`recorded_at`) управляется часами
`change_journal._clock`.

Что проверяется:
  1. A21: точка перенесена из группы А в группу Б с 120-го часа — `as_was`
     делит расход по дате; отчёт за период до переноса не меняется;
  2. A22: тот же отчёт без `structure_mode` подписан `current`; в режиме
     состава `current` физические приборы исторически верны;
  3. A23: период до начала журнала — `assumed_legacy`, историю не выдумываем;
  4. замена измерителя линии в середине периода: баланс щита равен сумме двух
     независимо посчитанных половин; граница часа не делится (изменение в
     середине 120-го часа действует с 121-го);
  5. узел сменил вид `panel -> source` в середине периода: вводы объекта до и
     после различаются, итог — сумма по интервалам;
  6. A39 (сеть): архивная точка и точка с `enabled=0` — измеритель линии
     выбывает с даты: итог объекта неполный, небаланс щита по выходу растёт;
  7. восстановление снимка структуры (`structure_at`) и защита от лавины
     интервалов.

Запуск: python tests/test_step55_topology_as_was.py
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from wb_energy_meter import change_journal, topology_history  # noqa: E402
from wb_energy_meter.group_repo_v2 import GroupRepoV2  # noqa: E402
from wb_energy_meter.topology_service import (  # noqa: E402
    ElectricalEdgeRepo, ElectricalNodeRepo,
)

from test_step28_reports_query import Rig, make_client, approx  # noqa: E402

HOUR = 3600
JOURNAL_START = 50


def H(n):
    return n * HOUR


CLK = {"t": 0}


class Env:
    """БД + HTTP-клиент + репозитории; часы журнала под контролем теста."""

    def __init__(self, start=H(JOURNAL_START)):
        self._orig_clock = change_journal._clock
        CLK["t"] = start
        change_journal._clock = lambda: CLK["t"]
        self.client, self.db, self.path = make_client()
        self.rig = Rig(self.db)
        self.points = self.rig.points
        self.nodes = ElectricalNodeRepo(self.db)
        self.edges = ElectricalEdgeRepo(self.db)
        self.groups = GroupRepoV2(self.db)

    def at(self, t):
        CLK["t"] = t

    def close(self):
        change_journal._clock = self._orig_clock
        self.db.close()
        os.unlink(self.path)

    def point(self, code, per_hour, first=100, last=139):
        return self.rig.make_point_at(
            code, {h: per_hour for h in range(first, last + 1)})

    def edge(self, a, b, point=None, published_at=H(60)):
        e = self.edges.add_draft(a.id, b.id, primary_point_id=point.id if point else None)
        self.edges.publish_edges([e.id], at=published_at)
        return self.edges.get_by_id(e.id)

    def api(self, url, **body):
        r = self.client.post(url, json=body)
        return r.status_code, r.get_json()

    def report(self, dimension, scope_ids, f, t, **extra):
        body = {"dimension": dimension, "from": f, "to": t, "timezone": "UTC"}
        if scope_ids is not None:
            body["scope_ids"] = scope_ids
        body.update(extra)
        status, data = self.api("/api/v2/reports/query", **body)
        assert status == 200, (status, data)
        return data

    def overview(self, f, t, **extra):
        status, data = self.api("/api/v2/overview/summary",
                                **dict({"from": f, "to": t, "timezone": "UTC"}, **extra))
        assert status == 200, (status, data)
        return data

    def node_balance(self, node_id, f, t, **extra):
        status, data = self.api(f"/api/v2/topology/nodes/{node_id}/balance",
                                **dict({"from": f, "to": t, "timezone": "UTC"}, **extra))
        assert status == 200, (status, data)
        return data


def row_by_id(report, gid):
    return next(r for r in report["rows"] if r["id"] == gid)


# ----------------------------------------------------- 1-2. A21 и A22

def build_move_env():
    """P1 (1 кВт·ч/ч) состоял в группе А, с 120-го часа — в группе Б;
    P2 (2 кВт·ч/ч) остаётся в А."""
    env = Env()
    p1 = env.point("P1", 1.0)
    p2 = env.point("P2", 2.0)
    env.at(H(60))
    ga = env.groups.add("Арендатор А")
    gb = env.groups.add("Арендатор Б")
    env.groups.add_member(ga.id, p1.id, valid_from=0)
    env.groups.add_member(ga.id, p2.id, valid_from=0)
    env.at(H(120))
    env.groups.remove_member(ga.id, p1.id, at=H(120))
    env.groups.add_member(gb.id, p1.id, valid_from=H(120))
    return env, p1, p2, ga, gb


def test_a21_a22_group_move_split_by_date():
    env, p1, p2, ga, gb = build_move_env()
    try:
        f, t = H(100), H(140)

        # A21: as_was делит расход по дате переноса
        rep = env.report("group", [ga.id, gb.id], f, t, structure_mode="as_was")
        assert rep["structure_mode"] == "as_was"
        assert rep["composition_mode"] == "as_was"
        assert rep["assumed_legacy"] is False
        assert rep["journal_started_at"] == H(JOURNAL_START)
        a, b = row_by_id(rep, ga.id), row_by_id(rep, gb.id)
        # А: до 120-го (P1+P2)=3/ч * 20 ч = 60, после только P2 = 2/ч * 20 ч = 40
        assert approx(a["result"]["value"], 100.0), a["result"]
        # Б: до 120-го группа пуста, после P1 = 1/ч * 20 ч = 20
        assert approx(b["result"]["value"], 20.0), b["result"]
        assert sorted(a["member_point_ids"]) == sorted([p1.id, p2.id])
        assert b["member_point_ids"] == [p1.id]
        ivs = a["result"]["explanation"]["structure_intervals"]
        assert [(i["from"], i["to"]) for i in ivs] == [(H(100), H(120)), (H(120), H(140))], ivs

        # без structure_mode: режим подписан current и считается по-старому —
        # состав на начало периода (P1+P2 на все 40 ч = 120) — это и есть A21
        old = env.report("group", [ga.id, gb.id], f, t)
        assert old["structure_mode"] == "current"
        assert approx(row_by_id(old, ga.id)["result"]["value"], 120.0)
        assert row_by_id(old, gb.id)["result"] is None
        assert old["assumed_legacy"] is False

        # отчёт за период ДО переноса не меняется ни в одном режиме
        before_new = env.report("group", [ga.id, gb.id], f, H(120), structure_mode="as_was")
        before_old = env.report("group", [ga.id, gb.id], f, H(120))
        for rep_ in (before_new, before_old):
            assert approx(row_by_id(rep_, ga.id)["result"]["value"], 60.0), rep_
            assert row_by_id(rep_, gb.id)["result"] is None

        # сравнение периодов: основной после переноса, второй до него
        cmp_rep = env.report("group", [ga.id], H(120), t, structure_mode="as_was",
                             compare={"from": f, "to": H(120)})
        row = row_by_id(cmp_rep, ga.id)
        assert approx(row["result"]["value"], 40.0)
        assert approx(row["compare_result"]["value"], 60.0)
        assert row["composition_changed"] is True
        assert approx(row["delta_value"], -20.0)

        # A22: current-структура и текущий состав группы, приборы исторически верны
        cur = env.report("group", [ga.id, gb.id], f, t,
                         structure_mode="current", composition_mode="current")
        assert cur["structure_mode"] == "current" and cur["composition_mode"] == "current"
        assert approx(row_by_id(cur, ga.id)["result"]["value"], 80.0)   # сейчас в А только P2
        assert approx(row_by_id(cur, gb.id)["result"]["value"], 40.0)   # P1 за весь период

        # несовместимое сочетание и неверное значение — 400
        status, data = env.api("/api/v2/reports/query", dimension="group",
                               scope_ids=[ga.id], **{"from": f, "to": t},
                               structure_mode="as_was", composition_mode="current")
        assert status == 400 and "structure_mode" in data["fields"], data
        status, data = env.api("/api/v2/reports/query", dimension="group",
                               scope_ids=[ga.id], **{"from": f, "to": t},
                               structure_mode="вчера")
        assert status == 400 and data["fields"] == ["structure_mode"], data
        print("[OK] 1-2. A21/A22: as_was делит расход по дате (А=100, Б=20), "
              "прошлый период не меняется, current подписан, 400 на ошибки")
    finally:
        env.close()


# ------------------------------------------------------------- 3. A23

def test_a23_period_before_journal_is_assumed_legacy():
    env = Env()
    try:
        m0 = env.point("M0", 1.0, first=20, last=139)
        env.at(H(60))
        src = env.nodes.add("SRC", "Ввод", "source")
        pnl = env.nodes.add("PNL", "Щит", "panel")
        # связь опубликована ДО начала журнала (как в БД, обновлённой до 0.22.0)
        env.edge(src, pnl, m0, published_at=H(10))

        # период целиком до журнала
        ov = env.overview(H(20), H(60), structure_mode="as_was")
        assert ov["structure_mode"] == "as_was"
        assert ov["assumed_legacy"] is True
        assert ov["journal_started_at"] == H(JOURNAL_START)
        assert ov["object_input_point_ids"] == [m0.id]
        total = ov["object_total"]
        assert approx(total["value"], 40.0), total          # 1 кВт·ч/ч * 40 ч
        assert total["structure_quality"] == "assumed_legacy", total
        # период пересекает начало журнала — тоже предположение
        assert env.overview(H(40), H(60), structure_mode="as_was")["assumed_legacy"] is True
        # период после начала журнала — настоящая история
        ov2 = env.overview(H(100), H(140), structure_mode="as_was")
        assert ov2["assumed_legacy"] is False
        assert ov2["object_total"]["structure_quality"] == "verified"
        # режим current ничего не знает о «предположении»
        cur = env.overview(H(20), H(60))
        assert cur["structure_mode"] == "current" and cur["assumed_legacy"] is False

        # снимок: до начала журнала структура берётся на момент начала журнала
        snap = topology_history.structure_at(env.db, H(20), "as_was")
        assert snap.assumed_legacy is True and snap.effective_at == H(JOURNAL_START)
        assert [e.id for e in snap.edges] != []
        snap2 = topology_history.structure_at(env.db, H(100), "as_was")
        assert snap2.assumed_legacy is False and snap2.effective_at == H(100)
        print("[OK] 3. A23: период до начала журнала помечен assumed_legacy, "
              "после — нет; структура на момент начала журнала")
    finally:
        env.close()


# ----------------------------------------- 4. замена измерителя линии

def build_swap_env(swap_at=H(120), use_b_from_start=None):
    """Щит PNL получает питание по линии Ein (измеритель Pa, 5 кВт·ч/ч до
    замены); с `swap_at` линия измеряется Pb (4 кВт·ч/ч). Выходы щита: Pc
    (3 кВт·ч/ч) и Pd (1 кВт·ч/ч). Данные — часы 100..139.

    use_b_from_start: None — замена в `swap_at`; False/True — без замены, линия
    сразу с Pa/Pb (эталонные БД для независимого расчёта половин)."""
    env = Env()
    pa = env.point("Pa", 5.0)
    pb = env.point("Pb", 4.0)
    pc = env.point("Pc", 3.0)
    pd = env.point("Pd", 1.0)
    env.at(H(60))
    src = env.nodes.add("SRC", "Ввод", "source")
    pnl = env.nodes.add("PNL", "Щит", "panel")
    l1 = env.nodes.add("L1", "Нагрузка 1", "load")
    l2 = env.nodes.add("L2", "Нагрузка 2", "load")
    first = pb if use_b_from_start else pa
    e_in = env.edge(src, pnl, first)
    env.edge(pnl, l1, pc)
    env.edge(pnl, l2, pd)
    if use_b_from_start is None:
        env.at(swap_at)
        env.edges.set_primary_point(e_in.id, pb.id, at=swap_at)
    return env, (src, pnl), (pa, pb, pc, pd)


def test_meter_swap_on_line_balance_is_sum_of_halves():
    env, (src, pnl), (pa, pb, pc, pd) = build_swap_env()
    try:
        f, t = H(100), H(140)
        mid = H(120)

        # независимые половины: две другие БД без замены, режим current
        env_before, (_, pnl_b), _ = build_swap_env(use_b_from_start=False)
        env_after, (_, pnl_a), _ = build_swap_env(use_b_from_start=True)
        try:
            half1 = env_before.node_balance(pnl_b.id, f, mid)       # вход Pa
            half2 = env_after.node_balance(pnl_a.id, mid, t)        # вход Pb
        finally:
            env_before.close()
            env_after.close()
        # вручную: до: 5*20 - (3+1)*20 = 20; после: 4*20 - 80 = 0
        assert approx(half1["value"], 20.0) and approx(half2["value"], 0.0), (half1, half2)

        got = env.node_balance(pnl.id, f, t, structure_mode="as_was")
        assert got["structure_mode"] == "as_was"
        assert approx(got["value"], half1["value"] + half2["value"]), got
        assert approx(got["input"]["result"]["value"], 5.0 * 20 + 4.0 * 20)
        assert [(i["from"], i["to"]) for i in got["structure_intervals"]] == \
            [(f, mid), (mid, t)], got["structure_intervals"]
        assert got["unavailable_reason"] is None
        assert got["assumed_legacy"] is False

        # режим current считает всё на нынешней структуре (вход Pb): 0
        cur = env.node_balance(pnl.id, f, t)
        assert cur["structure_mode"] == "current"
        assert approx(cur["value"], 0.0), cur
        assert len(cur["structure_intervals"]) == 1
        print("[OK] 4a. замена измерителя линии: баланс щита = сумма двух "
              "независимо посчитанных половин (20 + 0); current даёт 0")
    finally:
        env.close()


def test_change_inside_hour_belongs_to_structure_at_hour_start():
    """Замена в 120:30 — час 120 целиком на старой структуре (Pa), с 121-го — Pb."""
    swap_at = H(120) + 1800
    env, (src, pnl), (pa, pb, pc, pd) = build_swap_env(swap_at=swap_at)
    try:
        got = env.node_balance(pnl.id, H(100), H(140), structure_mode="as_was")
        assert [(i["from"], i["to"]) for i in got["structure_intervals"]] == \
            [(H(100), H(121)), (H(121), H(140))], got["structure_intervals"]
        # вход: Pa за часы 100..120 (21 ч) = 105, Pb за 121..139 (19 ч) = 76
        assert approx(got["input"]["result"]["value"], 105.0 + 76.0), got["input"]
        # выходы 4/ч * 40 ч = 160; небаланс 181 - 160 = 21
        assert approx(got["value"], 21.0), got
        print("[OK] 4b. изменение внутри часа действует с ближайшей границы часа "
              "(час 120 — на старой структуре)")
    finally:
        env.close()


# ------------------------------------------- 5. узел сменил вид

def test_node_kind_change_source_inputs_differ():
    env = Env()
    try:
        m0 = env.point("M0", 10.0)
        mx = env.point("MX", 3.0)
        env.at(H(60))
        s0 = env.nodes.add("S0", "Ввод", "source")
        n0 = env.nodes.add("N0", "Щит 0", "panel")
        x = env.nodes.add("X", "Узел X", "panel")       # пока щит, не ввод
        y = env.nodes.add("Y", "Нагрузка Y", "load")
        env.edge(s0, n0, m0)
        env.edge(x, y, mx)
        env.at(H(120))
        env.nodes.update_fields(x.id, kind="source")

        f, t = H(100), H(140)
        before = env.overview(f, H(120), structure_mode="as_was")
        after = env.overview(H(120), t, structure_mode="as_was")
        assert before["object_input_point_ids"] == [m0.id], before
        assert sorted(after["object_input_point_ids"]) == sorted([m0.id, mx.id]), after
        assert approx(before["object_total"]["value"], 10.0 * 20)
        assert approx(after["object_total"]["value"], 10.0 * 20 + 3.0 * 20)

        whole = env.overview(f, t, structure_mode="as_was")
        assert sorted(whole["object_input_point_ids"]) == sorted([m0.id, mx.id])
        assert approx(whole["object_total"]["value"], 200.0 + 260.0), whole["object_total"]
        assert [(i["from"], i["to"]) for i in whole["structure_intervals"]] == \
            [(f, H(120)), (H(120), t)]
        # current: X — ввод на весь период (в 520 входит и нагрузка X до 120-го)
        cur = env.overview(f, t)
        assert approx(cur["object_total"]["value"], 400.0 + 120.0), cur["object_total"]
        assert whole["object_total"]["value"] != cur["object_total"]["value"]

        snap_before = topology_history.structure_at(env.db, H(110), "as_was")
        snap_after = topology_history.structure_at(env.db, H(130), "as_was")
        assert snap_before.node_kinds == {x.id: "panel"} and snap_after.node_kinds == {}
        print("[OK] 5. смена вида узла panel->source: вводы до/после различаются, "
              "итог объекта 200+260=460 (current: 520)")
    finally:
        env.close()


# ----------------------------------------------- 6. A39 (сеть)

def build_net_env():
    """Ввод SRC -(Pin, 6/ч)-> щит PNL -(Pout1, 4/ч)-> L1, -(Pout2, 2/ч)-> L2."""
    env = Env()
    pin = env.point("Pin", 6.0)
    p1 = env.point("Pout1", 4.0)
    p2 = env.point("Pout2", 2.0)
    env.at(H(60))
    src = env.nodes.add("SRC", "Ввод", "source")
    pnl = env.nodes.add("PNL", "Щит", "panel")
    l1 = env.nodes.add("L1", "Нагрузка 1", "load")
    l2 = env.nodes.add("L2", "Нагрузка 2", "load")
    e_in = env.edge(src, pnl, pin)
    env.edge(pnl, l1, p1)
    env.edge(pnl, l2, p2)
    return env, pnl, e_in, (pin, p1, p2)


def deactivate(env, point, how, at):
    env.at(at)
    if how == "archive":
        env.points.archive(point.id, at=at)
    else:
        env.points.set_enabled(point.id, False, at=at)


def test_a39_net_input_point_deactivated_line_becomes_unmetered():
    for how in ("archive", "enabled0"):
        # контроль: без выбытия ввод измеряется весь период
        env, pnl, e_in, (pin, p1, p2) = build_net_env()
        try:
            f, t = H(100), H(140)
            ctl = env.overview(f, t)
            assert approx(ctl["object_total"]["value"], 240.0), ctl["object_total"]
            assert ctl["unmetered_inputs"] == []
            deactivate(env, pin, how, H(120))
            for mode in ("current", "as_was"):
                ov = env.overview(f, t, structure_mode=mode)
                tot = ov["object_total"]
                # до 120-го ввод измерен (6*20=120), после — линия «без счётчика»
                assert tot["value"] is None, (how, mode, tot)
                assert approx(tot["known_value"], 120.0), (how, mode, tot)
                assert tot["availability"] == "partial"
                assert len(ov["unmetered_inputs"]) == 1, ov["unmetered_inputs"]
                assert ov["object_total_unavailable_reason"] == "unmetered_input"
            # период целиком до выбытия — ничего не меняется
            before = env.overview(f, H(120))
            assert approx(before["object_total"]["value"], 120.0)
            # период целиком после выбытия — линия без счётчика весь период
            after = env.overview(H(120), t)
            assert after["object_total"]["value"] is None
            assert after["object_total"]["known_value"] is None
            # баланс щита: после выбытия ввода входа нет, первая половина известна
            nb = env.node_balance(pnl.id, f, t)
            assert nb["value"] is None and approx(nb["known_value"], 0.0), nb
            assert len(nb["structure_intervals"]) == 2
        finally:
            env.close()
    print("[OK] 6a. A39 (сеть): архивная точка и enabled=0 на вводе — линия без "
          "счётчика с даты выбытия, история до неё сохранена")


def test_a39_net_output_point_deactivated_unmetered_branch_grows_imbalance():
    for how in ("archive", "enabled0"):
        env, pnl, e_in, (pin, p1, p2) = build_net_env()
        try:
            f, t = H(100), H(140)
            base = env.node_balance(pnl.id, f, t)
            assert approx(base["value"], 0.0), base            # 120+120 - (80+40+80+40)
            deactivate(env, p2, how, H(120))
            nb = env.node_balance(pnl.id, f, t)
            # до 120-го: 120 - 120 = 0; после: 120 - 80 (Pout2 выбыл) = 40
            assert approx(nb["value"], 40.0), (how, nb)
            assert len(nb["unmetered_branches"]) == 1, nb["unmetered_branches"]
            assert nb["boundary_coverage"] == "has_unmetered_branches"
            assert nb["structure_mode"] == "current"
            # история до выбытия не затронута
            early = env.node_balance(pnl.id, f, H(120))
            assert approx(early["value"], 0.0) and early["unmetered_branches"] == []
        finally:
            env.close()
    print("[OK] 6b. A39 (сеть): выбывший выход щита становится неизмеренной "
          "ветвью, небаланс 0 -> 40 с даты выбытия")


# --------------------------------------------- 7. снимок и защита

def test_structure_at_snapshots():
    env, (src, pnl), (pa, pb, pc, pd) = build_swap_env()
    try:
        s_before = topology_history.structure_at(env.db, H(110), "as_was")
        s_after = topology_history.structure_at(env.db, H(130), "as_was")
        pp_before = {e.to_node_id: e.primary_point_id for e in s_before.edges}
        pp_after = {e.to_node_id: e.primary_point_id for e in s_after.edges}
        assert pp_before[pnl.id] == pa.id and pp_after[pnl.id] == pb.id
        assert not s_before.assumed_legacy and not s_after.assumed_legacy
        # live-снимок в режиме current без выбывших измерителей — не копия
        live = topology_history.structure_at(env.db, H(130), "current")
        assert live.is_live and live.edges is None
        # режим current + выбывший измеритель
        env.points.archive(pb.id, at=H(135))
        masked = topology_history.structure_at(env.db, H(136), "current")
        assert not masked.is_live and masked.masked_point_ids == [pb.id]
        assert {e.to_node_id: e.primary_point_id for e in masked.edges}[pnl.id] is None
        # неверный режим
        try:
            topology_history.structure_at(env.db, H(130), "вчера")
        except ValueError:
            pass
        else:
            raise AssertionError("ожидали ValueError на неверный режим")
        print("[OK] 7a. снимок структуры: откат журнала возвращает измеритель, "
              "live-снимок не копируется, выбывший измеритель снят")
    finally:
        env.close()


def test_too_many_changes_rejected_not_computed():
    env, (src, pnl), (pa, pb, pc, pd) = build_swap_env(use_b_from_start=False)
    try:
        e_in = next(e for e in env.edges.list_active_published()
                    if e.to_node_id == pnl.id)
        pts = [pa, pb]
        for i in range(topology_history.MAX_INTERVALS + 5):
            env.at(H(61) + i * HOUR)
            env.edges.set_primary_point(e_in.id, pts[(i + 1) % 2].id, at=H(61) + i * HOUR)
        status, data = env.api(
            f"/api/v2/topology/nodes/{pnl.id}/balance", **{
                "from": H(60), "to": H(61) + (topology_history.MAX_INTERVALS + 10) * HOUR,
                "timezone": "UTC", "structure_mode": "as_was"})
        assert status == 400 and "слишком много" in data["message"], (status, data)
        # тот же период в current — считается как раньше
        status, data = env.api(
            f"/api/v2/topology/nodes/{pnl.id}/balance", **{
                "from": H(100), "to": H(140), "timezone": "UTC"})
        assert status == 200, data
        print("[OK] 7b. лавина изменений: as_was отвечает 400, current не затронут")
    finally:
        env.close()


if __name__ == "__main__":
    test_a21_a22_group_move_split_by_date()
    test_a23_period_before_journal_is_assumed_legacy()
    test_meter_swap_on_line_balance_is_sum_of_halves()
    test_change_inside_hour_belongs_to_structure_at_hour_start()
    test_node_kind_change_source_inputs_differ()
    test_a39_net_input_point_deactivated_line_becomes_unmetered()
    test_a39_net_output_point_deactivated_unmetered_branch_grows_imbalance()
    test_structure_at_snapshots()
    test_too_many_changes_rejected_not_computed()
    print("\nТопология «как было» (Шаг 55) — все проверки пройдены.")

#!/usr/bin/env python3
"""Шаг 64 (партия находок 0.22.3, П9): расчёт выбирает источник итогов по
профилю привязки (docs/design-phase-aggregates.md §6).

Фикстура (числа заданы вручную до запуска; 24 часа, периоды [0, 86400)):
  прибор `wb-map3e_16`: итог Total AP energy 3.5 кВт·ч/ч; фазы L1 = 1.0,
  L2 = 2.0, L3 = 0.5 кВт·ч/ч (сумма фаз = 3.5);
  прибор `wb-input`: итог 3.6 кВт·ч/ч (ввод щита);
  Ввод → Щит (линию измеряет `wb-input`, total_3p) →
  три нагрузки: «Нагрузка L1/L2/L3» (каждую линию измеряет СВОЯ фаза
  прибора wb-map3e_16: phase_l1, phase_l2, phase_l3).

Ожидания:
  - L1 = 24.0, L2 = 48.0, L3 = 12.0 кВт·ч (до П9 каждая точка получила бы 84.0
    — расход всего прибора);
  - сумма трёх фаз = 84.0 = итог прибора за период (допуск 1e-6); режим `sum`
    по трём точкам — 84.0, а не 252.0 (тройной счёт);
  - баланс щита: вход 86.4, выходы 24.0 + 48.0 + 12.0 = 84.0, небаланс 2.4;
    «Обзор»: итог объекта 86.4, небаланс 2.4;
  - отчёт `reports/query` по трём точкам: те же 24.0 / 48.0 / 12.0;
  - сброс на одной фазе (час 5 фазы L2 — `reset`, известная часть 1.2): L2
    неполная (флаг reset, значение неизвестно, известно 23 × 2.0 + 1.2 = 47.2),
    L1 и L3 — полные;
  - живой снимок: точка на L1 показывает `P L1` и `AP energy L1`, а не
    `Total P`/`Total AP energy`; напряжение и ток — только её фазы; точка
    `total_3p` — как раньше.

Самостоятельный скрипт (не pytest):
    python tests/test_step64_phase_accounting.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from wb_energy_meter.aggregates_repo import AggregateRepo, HourlyAggregate  # noqa: E402
from wb_energy_meter.api import _AppState, create_app  # noqa: E402
from wb_energy_meter.binding_service import PointBindingRepo  # noqa: E402
from wb_energy_meter.db import Database  # noqa: E402
from wb_energy_meter.model import ControlState, MeterRegistry  # noqa: E402
from wb_energy_meter.point_repo import MeteringPointRepo, MeterSourceRepo  # noqa: E402
from wb_energy_meter.repo import GroupRepo, MeterRepo  # noqa: E402

HOUR = 3600
DAY = 24 * HOUR
TOL = 1e-6
BODY = {"from": 0, "to": DAY, "timezone": "UTC"}


def approx(a, b, tol=TOL):
    return a is not None and b is not None and abs(a - b) <= tol


class World:
    def __init__(self):
        fd, self.path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(fd)
        os.unlink(self.path)
        self.db = Database(path=self.path)
        self.db.open()
        groups = GroupRepo(self.db)
        self.meters = MeterRepo(self.db, groups)
        self.registry = MeterRegistry()
        state = _AppState(
            registry=self.registry, meters_repo=self.meters, groups_repo=groups,
            is_mqtt_connected=lambda: False, mqtt_message_count=lambda: 0,
            mqtt_error_count=lambda: 0, wb_db_client=None, consumption_service=None,
            started_at=time.time(), db=self.db, plans_dir=tempfile.mkdtemp())
        self.client = create_app(state).test_client()
        self.agg = AggregateRepo(self.db)
        sources, points, bindings = (MeterSourceRepo(self.db), MeteringPointRepo(self.db),
                                     PointBindingRepo(self.db))

        self.map3e = self.meters.add("wb-map3e_16", "Прибор 16")
        s_map = sources.open_source(self.map3e.id, "wb8-main", "wb-map3e_16")
        self.inp = self.meters.add("wb-input", "Вводной")
        s_in = sources.open_source(self.inp.id, "wb8-main", "wb-input")
        self._hours(self.agg, self.map3e.id, 3.5)
        self._hours(self.agg, self.inp.id, 3.6)
        for ph, kwh in (("L1", 1.0), ("L2", 2.0), ("L3", 0.5)):
            self._hours(self.agg.for_profile("phase_" + ph.lower()), self.map3e.id, kwh)

        self.p_in = points.add("IN", "Ввод щита")
        bindings.open_binding(self.p_in.id, s_in.id, "total_3p", valid_from=0)
        self.p = {}
        for ph in ("L1", "L2", "L3"):
            pt = points.add("P" + ph, "Нагрузка " + ph)
            bindings.open_binding(pt.id, s_map.id, "phase_" + ph.lower(), valid_from=0)
            self.p[ph] = pt

        self.source = self.node("Ввод", "source")
        self.panel = self.node("Щит", "panel")
        self.loads = {ph: self.node("Нагрузка " + ph, "load") for ph in self.p}
        self.edge(self.source, self.panel, self.p_in)
        for ph in self.p:
            self.edge(self.panel, self.loads[ph], self.p[ph])

    @staticmethod
    def _hours(repo, meter_id, kwh):
        for h in range(24):
            repo.upsert(HourlyAggregate(
                meter_id=meter_id, period_start=h * HOUR, period_end=(h + 1) * HOUR,
                ap_energy_start=0.0, ap_energy_end=kwh, ap_energy_delta=kwh,
                p_avg=None, p_max=None, samples_count=1, quality_flag="ok",
                computed_at=1))

    def rev(self):
        return self.client.get("/api/v2/revision").get_json()["configuration_revision"]

    def node(self, name, kind):
        r = self.client.post("/api/v2/topology/nodes", json={
            "code": "n-%s-%d" % (kind, time.time_ns()), "name": name, "kind": kind,
            "expected_revision": self.rev()})
        assert r.status_code == 201, r.get_json()
        return r.get_json()

    def edge(self, a, b, point):
        r = self.client.post("/api/v2/topology/edges/connect", json={
            "from_node_id": a["id"], "to_node_id": b["id"], "expected_revision": self.rev()})
        assert r.status_code == 201, r.get_json()
        e = r.get_json()
        r = self.client.patch("/api/v2/topology/edges/%d" % e["id"], json={
            "primary_point_id": point.id, "expected_revision": self.rev()})
        assert r.status_code == 200, r.get_json()
        return e

    def metric(self, mode, ids):
        r = self.client.post("/api/v2/metrics/query", json=dict(
            BODY, mode=mode, point_ids=ids))
        assert r.status_code == 200, r.get_json()
        return r.get_json()

    def close(self):
        self.db.close()
        for ext in ("", "-wal", "-shm"):
            try:
                os.unlink(self.path + ext)
            except OSError:
                pass


def test_each_phase_point_gets_its_own_energy_and_phases_sum_to_meter_total():
    w = World()
    try:
        got = {ph: w.metric("measured", [w.p[ph].id]) for ph in w.p}
        assert approx(got["L1"]["value"], 24.0), got["L1"]
        assert approx(got["L2"]["value"], 48.0), got["L2"]
        assert approx(got["L3"]["value"], 12.0), got["L3"]
        assert all(g["availability"] == "complete" and g["quality_flags"] == []
                   for g in got.values())
        meter_total = w.agg.sum_kwh(w.map3e.id, 0, DAY)
        assert approx(meter_total, 84.0), meter_total
        phases_sum = sum(g["value"] for g in got.values())
        assert approx(phases_sum, meter_total), (phases_sum, meter_total)
        # ни одна фаза не получила расход всего прибора (прежняя ошибка)
        assert all(not approx(g["value"], 84.0) for g in got.values())

        total = w.metric("sum", [w.p["L1"].id, w.p["L2"].id, w.p["L3"].id])
        assert approx(total["value"], 84.0), total
        assert not approx(total["value"], 252.0)
    finally:
        w.close()
    print("[OK] расход фаз 24/48/12, сумма = итог прибора 84.0; sum по трём точкам 84.0, "
          "а не 252.0")


def test_shield_balance_and_overview_with_phase_loads():
    w = World()
    try:
        r = w.client.post("/api/v2/topology/nodes/%d/balance" % w.panel["id"], json=BODY)
        assert r.status_code == 200, r.get_json()
        nb = r.get_json()
        assert approx(nb["input"]["result"]["value"], 86.4), nb["input"]
        out = {o["name"]: o["result"]["value"] for o in nb["outputs"]}
        assert approx(out["Нагрузка L1"], 24.0) and approx(out["Нагрузка L2"], 48.0) \
            and approx(out["Нагрузка L3"], 12.0), out
        assert approx(nb["value"], 2.4), nb["value"]
        assert approx(nb["explanation"]["output_value"], 84.0), nb["explanation"]

        r = w.client.post("/api/v2/overview/summary", json=BODY)
        assert r.status_code == 200, r.get_json()
        ov = r.get_json()
        assert approx(ov["object_total"]["value"], 86.4), ov["object_total"]
        assert approx(ov["imbalance_value"], 2.4), ov["imbalance_value"]
    finally:
        w.close()
    print("[OK] баланс щита: вход 86.4, выходы 84.0, небаланс 2.4; «Обзор»: итог объекта 86.4")


def test_reports_query_uses_phase_totals():
    w = World()
    try:
        r = w.client.post("/api/v2/reports/query", json=dict(
            BODY, dimension="point", scope_ids=[w.p[ph].id for ph in ("L1", "L2", "L3")]))
        assert r.status_code == 200, r.get_json()
        rows = {row["name"]: row["result"]["value"] for row in r.get_json()["rows"]}
        assert approx(rows["Нагрузка L1"], 24.0) and approx(rows["Нагрузка L2"], 48.0) \
            and approx(rows["Нагрузка L3"], 12.0), rows
    finally:
        w.close()
    print("[OK] отчёт по точкам: 24.0 / 48.0 / 12.0")


def test_reset_on_one_phase_does_not_touch_the_others():
    w = World()
    try:
        w.agg.for_profile("phase_l2").upsert(HourlyAggregate(
            meter_id=w.map3e.id, period_start=5 * HOUR, period_end=6 * HOUR,
            ap_energy_start=50.0, ap_energy_end=2.0, ap_energy_delta=1.2, p_avg=None,
            p_max=None, samples_count=3, quality_flag="reset", computed_at=1))
        l1 = w.metric("measured", [w.p["L1"].id])
        l2 = w.metric("measured", [w.p["L2"].id])
        l3 = w.metric("measured", [w.p["L3"].id])
        assert l1["availability"] == "complete" and approx(l1["value"], 24.0), l1
        assert l3["availability"] == "complete" and approx(l3["value"], 12.0), l3
        assert l2["value"] is None and l2["availability"] == "partial", l2
        assert "reset" in l2["quality_flags"], l2["quality_flags"]
        # известно: 23 полных часа × 2.0 = 46.0 + известная часть часа со сбросом 1.2
        assert approx(l2["known_value"], 47.2), l2["known_value"]
        assert approx(l2["explanation"]["reset_known_kwh"], 1.2), l2["explanation"]
    finally:
        w.close()
    print("[OK] reset на фазе L2: L2 неполная (известно 47.2 = 46.0 + 1.2), "
          "L1 и L3 полные")


def _set_controls(meter, **values):
    for name, v in values.items():
        meter.controls[name.replace("_", " ")] = ControlState(
            name=name.replace("_", " "), value=v, raw_value=str(v),
            meta={"type": "value"}, last_update_ts=time.time(), first_seen_ts=time.time())


def test_live_snapshot_follows_the_binding_profile():
    w = World()
    try:
        m = w.registry.get_or_create("wb-map3e_16")
        _set_controls(m, Total_P=3000.0, P_L1=1000.0, P_L2=1500.0, P_L3=500.0,
                      Total_AP_energy=100.0, AP_energy_L1=11.0, AP_energy_L2=22.0,
                      Urms_L1=231.0, Urms_L2=232.0, Urms_L3=233.0,
                      Irms_L1=4.3, Irms_L2=6.5, Irms_L3=2.2)
        mi = w.registry.get_or_create("wb-input")
        _set_controls(mi, Total_P=3600.0, Total_AP_energy=7.0, Urms_L1=230.0)
        r = w.client.get("/api/v2/snapshot?point_ids=%d,%d,%d" % (
            w.p["L1"].id, w.p["L2"].id, w.p_in.id))
        assert r.status_code == 200, r.get_json()
        snap = {p["point_id"]: p for p in r.get_json()["points"]}
        l1, l2, tot = snap[w.p["L1"].id], snap[w.p["L2"].id], snap[w.p_in.id]
        assert l1["channel_profile"] == "phase_l1"
        assert approx(l1["power_w"], 1000.0) and approx(l1["energy_total_kwh"], 11.0), l1
        assert l1["voltage_v"] == {"L1": 231.0, "L2": None, "L3": None}, l1["voltage_v"]
        assert l1["current_a"] == {"L1": 4.3, "L2": None, "L3": None}, l1["current_a"]
        assert approx(l2["power_w"], 1500.0) and approx(l2["energy_total_kwh"], 22.0), l2
        assert tot["channel_profile"] == "total_3p"
        assert approx(tot["power_w"], 3600.0) and approx(tot["energy_total_kwh"], 7.0), tot
        assert tot["voltage_v"]["L1"] == 230.0
    finally:
        w.close()
    print("[OK] снимок: точка на L1 — P L1 = 1000 и AP energy L1 = 11, напряжение/ток только "
          "L1; total_3p — как раньше")


def test_fake_repo_without_phase_support_fails_loudly():
    """Репозиторий агрегатов без for_profile не должен молча отдать фазе итог
    прибора: это ошибка, а не «нет данных»."""
    from wb_energy_meter.accounting_service import _repo_for_profile

    class OldRepo:
        pass

    assert _repo_for_profile(OldRepo(), "total_3p").__class__ is OldRepo
    try:
        _repo_for_profile(OldRepo(), "phase_l1")
        raise AssertionError("ожидался ValueError")
    except ValueError as e:
        assert "пофазные" in str(e)
    print("[OK] репозиторий без пофазных итогов: total_3p работает, phase_lN — явная ошибка")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print("\nВСЁ ЗЕЛЁНОЕ: %d проверок" % len(tests))

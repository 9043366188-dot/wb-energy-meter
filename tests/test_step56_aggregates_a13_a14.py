#!/usr/bin/env python3
"""Партия 12, этап 12.4: сброс накопителя внутри часа (A13) и редкие показания
(A14) — решения Кира от 10.10.2026, docs/TZ-finish-plan.md §10 / 12.4.

Числа посчитаны вручную по фикстурам ДО запуска.

A13. Накопитель 100 → 0 → 150 внутри часа:
  - агрегат: quality_flag='reset', ap_energy_delta = сумма возрастающих
    участков (0 → 150) = 150, а НЕ 150 − 100 = 50;
  - учёт (measured_point): расход часа неизвестен (value=None), известная часть
    идёт в known_value, только если в периоде есть хоть один полностью
    известный час (инвариант §5.3), иначе — в explanation.reset_known_kwh;
    число 50 нигде не выдаётся;
  - сумма точек не берёт известную часть неполной точки (инвариант §5.3);
  - строка почасового API: consumption_kwh=None, known_kwh=150, reason.

A14. 100.0 в 00:00 и 160.0 в 04:30, между ними записей нет:
  - часы 0..4: ap_energy_delta=NULL, quality_flag='sparse' (раньше 0,0,0,0,60);
  - период, целиком содержащий интервал: 60 (+ остальные часы), флаг edge_approx,
    пояснение «расход за ЧЧ:ММ–ЧЧ:ММ известен только целиком»;
  - период, режущий интервал: расход неизвестен (а не «часть от 60»);
  - разрыв короче 2 часов и одинаковые показания по краям — не sparse;
  - почасовой воркер, считающий час за часом «по состоянию на сейчас»,
    задним числом помечает ранние часы интервала sparse.

Самостоятельный скрипт (не pytest):
    python tests/test_step56_aggregates_a13_a14.py
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from wb_energy_meter.accounting_service import measured_point, sum_points  # noqa: E402
from wb_energy_meter.aggregates_repo import AggregateRepo, HourlyAggregate  # noqa: E402
from wb_energy_meter.aggregator import (  # noqa: E402
    Aggregator, AggregatorConfig, compute_hourly_aggregate,
)
from wb_energy_meter.api import _sparse_notes  # noqa: E402
from wb_energy_meter.consumption import calculate_from_points  # noqa: E402
from wb_energy_meter.db import Database  # noqa: E402
from wb_energy_meter.periods import Period  # noqa: E402
from wb_energy_meter.topology_service import ElectricalEdgeRepo  # noqa: E402
from wb_energy_meter.wb_db_client import HistoryPoint  # noqa: E402

from test_step49_acceptance_gaps import Rig, approx  # noqa: E402

HOUR = 3600
T0 = 1_700_002_800  # кратно 3600


def new_db():
    fd, path = tempfile.mkstemp(suffix=".sqlite3")
    os.close(fd)
    os.unlink(path)
    db = Database(path=path)
    db.open()
    return db, path


def cleanup(db, path):
    db.close()
    for ext in ("", "-shm", "-wal"):
        try:
            os.unlink(path + ext)
        except OSError:
            pass


def pt(offset_s, value):
    return HistoryPoint(timestamp=T0 + offset_s, value=value)


class World:
    """БД + одна точка с прибором; агрегаты кладём как есть."""

    def __init__(self, code="P"):
        self.db, self.path = new_db()
        self.rig = Rig(self.db)
        self.edges = ElectricalEdgeRepo(self.db)
        self.point, self.meter = self.rig.make_point_at(code, {})

    def put(self, aggs):
        for a in aggs:
            a.meter_id = self.meter.id
            self.rig.aggregates.upsert(a)

    def measured(self, ts_from, ts_to, point=None):
        p = point or self.point
        return measured_point(self.rig.bindings, self.rig.aggregates,
                              self.rig.sources, p.id, ts_from, ts_to)

    def close(self):
        cleanup(self.db, self.path)


def agg(hour_index, delta, flag, start=None, end=None):
    return HourlyAggregate(
        meter_id=0, period_start=T0 + hour_index * HOUR,
        period_end=T0 + (hour_index + 1) * HOUR,
        ap_energy_start=start, ap_energy_end=end, ap_energy_delta=delta,
        p_avg=None, p_max=None, samples_count=1, quality_flag=flag,
        computed_at=T0)


# --------------------------------------------------------------------- A13

def test_a13_aggregate_detects_internal_reset():
    # 100 -> 0 -> 150: раньше delta=50 'ok'
    a = compute_hourly_aggregate(1, T0, [pt(0, 100.0), pt(1800, 0.0), pt(3600, 150.0)])
    assert a.quality_flag == "reset", a
    assert a.ap_energy_delta == 150.0, a.ap_energy_delta        # 0 -> 150, а не 150-100
    assert (a.ap_energy_start, a.ap_energy_end) == (100.0, 150.0)

    # сброс в середине + рост до и после: 100 -> 120 (+20), 120 -> 0 (спад),
    # 0 -> 30 (+30) => известная часть 50
    b = compute_hourly_aggregate(1, T0, [pt(0, 100.0), pt(900, 120.0),
                                         pt(1800, 0.0), pt(3000, 30.0),
                                         pt(3600, 30.0)])
    assert b.quality_flag == "reset" and approx(b.ap_energy_delta, 50.0), b

    # спад без единого возрастающего участка: известной части нет — None (как раньше)
    c = compute_hourly_aggregate(1, T0, [pt(0, 500.0), pt(3600, 0.5)])
    assert c.quality_flag == "reset" and c.ap_energy_delta is None, c
    d = compute_hourly_aggregate(1, T0, [pt(0, 500.0), pt(1000, 0.0), pt(3600, 0.5)])
    assert d.quality_flag == "reset" and d.ap_energy_delta == 0.5, d

    # обычный монотонный час не затронут
    ok = compute_hourly_aggregate(1, T0, [pt(0, 100.0), pt(1800, 110.0), pt(3600, 120.0)])
    assert ok.quality_flag == "ok" and ok.ap_energy_delta == 20.0, ok
    print("[OK] A13 агрегат: 100->0->150 = reset с известной частью 150 (не 50); "
          "монотонный час без изменений")


def test_a13_measured_point_known_part_and_no_fifty():
    w = World()
    try:
        # час 0 — обычный 10, час 1 — сброс с известной частью 150
        w.put([agg(0, 10.0, "ok", 0.0, 10.0),
               agg(1, 150.0, "reset", 100.0, 150.0)])
        r = w.measured(T0, T0 + 2 * HOUR)
        assert r.value is None, r.to_dict()
        assert approx(r.known_value, 160.0), r.to_dict()          # 10 + 150
        assert r.availability == "partial" and "reset" in r.quality_flags
        assert r.valid_count == 1 and r.expected_count == 2
        assert r.explanation["reset_known_kwh"] == 150.0
        for bad in (r.value, r.known_value):
            assert bad != 50.0

        # только сброс: валидных частей нет, known_value обязан быть null (§5.3),
        # известная часть — в пояснении
        only = w.measured(T0 + HOUR, T0 + 2 * HOUR)
        assert only.value is None and only.known_value is None, only.to_dict()
        assert only.availability == "missing" and "reset" in only.quality_flags
        assert only.explanation["reset_known_kwh"] == 150.0

        # старый формат строки (delta=None у reset) работает как раньше
        w.put([agg(2, None, "reset", 100.0, 20.0)])
        old = w.measured(T0 + 2 * HOUR, T0 + 3 * HOUR)
        assert old.value is None and old.known_value is None
        assert "reset_known_kwh" not in old.explanation
        print("[OK] A13 учёт: расход часа неизвестен, известная часть 150 — в known_value "
              "(160 при часе 10) или в пояснении; 50 не выдаётся нигде")
    finally:
        w.close()


def test_a13_sum_does_not_leak_known_part():
    w = World("P1")
    try:
        p2, m2 = w.rig.make_point_at("P2", {})
        w.put([agg(0, 150.0, "reset", 100.0, 150.0), agg(1, 5.0, "ok", 0.0, 5.0)])
        w.rig.aggregates.upsert(agg_for(m2.id, 0, 7.0, "ok"))
        w.rig.aggregates.upsert(agg_for(m2.id, 1, 3.0, "ok"))
        s = sum_points(w.rig.bindings, w.rig.aggregates, w.rig.sources, w.edges,
                       [w.point.id, p2.id], T0, T0 + 2 * HOUR)
        # P1 неполна (час 0 со сбросом), P2 полна = 10; итог неполон, известна только P2
        assert s.value is None and approx(s.known_value, 10.0), s.to_dict()
        assert s.availability == "partial"
        print("[OK] A13 сумма: известная часть неполной точки не протекает в итог "
              "(known_value = 10 от полной P2)")
    finally:
        w.close()


def agg_for(meter_id, hour_index, kwh, flag):
    a = agg(hour_index, kwh, flag, 0.0, kwh)
    a.meter_id = meter_id
    return a


def test_a13_hourly_row_never_calls_known_part_consumption():
    row = agg(1, 150.0, "reset", 100.0, 150.0).to_dict()
    assert row["consumption_kwh"] is None, row
    assert row["known_kwh"] == 150.0 and "сброс" in row["reason"], row
    ok_row = agg(0, 10.0, "ok", 0.0, 10.0).to_dict()
    assert ok_row["consumption_kwh"] == 10.0 and "known_kwh" not in ok_row
    print("[OK] A13 строка почасового API: consumption_kwh=None, known_kwh=150, причина названа")


def test_a13_legacy_period_calculation():
    points = [HistoryPoint(T0, 100.0), HistoryPoint(T0 + 1800, 0.0),
              HistoryPoint(T0 + 3600, 150.0)]
    period = Period(ts_from=T0, ts_to=T0 + 3600, label="t", description="t")
    r = calculate_from_points(points, period, "x")
    assert r.quality == "reset" and r.consumption_kwh is None, r
    assert any("150" in w for w in r.warnings), r.warnings
    print("[OK] A13 прежний расчёт периода: reset, расхода нет, известная часть 150 в предупреждении")


# --------------------------------------------------------------------- A14

def test_a14_sparse_classification_boundaries():
    # 100 в 00:00 и 160 в 04:30: часы 0..4 — sparse
    pts = [pt(0, 100.0), pt(4 * HOUR + 1800, 160.0)]
    flags = [compute_hourly_aggregate(1, T0 + k * HOUR, pts).quality_flag for k in range(6)]
    assert flags == ["sparse"] * 5 + ["edge_approx"], flags     # час 5: после показания

    # разрыв ровно 2 часа: часы 0 и 1 — sparse (начало часа внутри [a, b)), час 2 — обычный
    two = [pt(0, 100.0), pt(2 * HOUR, 110.0)]
    f2 = [compute_hourly_aggregate(1, T0 + k * HOUR, two) for k in range(3)]
    assert [a.quality_flag for a in f2][:2] == ["sparse", "sparse"], f2
    assert f2[2].quality_flag != "sparse"

    # разрыв 1 ч 50 мин — короче порога, часы считаются как раньше
    short = [pt(0, 100.0), pt(HOUR + 3000, 110.0)]
    assert all(compute_hourly_aggregate(1, T0 + k * HOUR, short).quality_flag != "sparse"
               for k in range(2))

    # одинаковые показания через 5 часов: расхода не было ровно 0 — не sparse
    flat = [pt(0, 100.0), pt(5 * HOUR, 100.0)]
    fl = [compute_hourly_aggregate(1, T0 + k * HOUR, flat) for k in range(5)]
    assert all(a.quality_flag == "edge_approx" and a.ap_energy_delta == 0.0 for a in fl), fl

    # часы с показаниями каждые 10 минут — как раньше 'ok'
    dense = [pt(i * 600, 100.0 + i) for i in range(0, 13)]
    d0 = compute_hourly_aggregate(1, T0, dense)
    assert d0.quality_flag == "ok" and d0.ap_energy_delta == 6.0, d0
    print("[OK] A14 классификация: sparse только при разрыве ≥ 2 ч с ростом накопителя; "
          "короткие разрывы, нулевой расход и плотные данные — как раньше")


def test_a14_period_inside_cutting_and_mixed():
    w = World()
    try:
        pts = [pt(0, 100.0), pt(4 * HOUR + 1800, 160.0)]
        aggs = [compute_hourly_aggregate(1, T0 + k * HOUR, pts) for k in range(5)]
        # до интервала — обычный час 10 (час -1), после — час 5: 160 -> 166 = 6
        before = agg(-1, 10.0, "ok", 90.0, 100.0)
        after = agg(5, 6.0, "ok", 160.0, 166.0)
        w.put([before] + aggs + [after])

        full = w.measured(T0 - HOUR, T0 + 6 * HOUR)
        assert approx(full.value, 10.0 + 60.0 + 6.0), full.to_dict()
        assert full.availability == "complete" and "edge_approx" in full.quality_flags
        wo = full.explanation["whole_only_intervals"]
        assert len(wo) == 1 and wo[0]["from"] == T0 and wo[0]["to"] == T0 + 5 * HOUR
        assert approx(wo[0]["value"], 60.0), wo
        assert "известен только целиком" in full.explanation["notes"][0]

        # период режет интервал справа: часы 0..2 — расход неизвестен
        cut_right = w.measured(T0, T0 + 3 * HOUR)
        assert cut_right.value is None and cut_right.known_value is None, cut_right.to_dict()
        assert cut_right.availability == "missing" and "sparse" in cut_right.quality_flags
        assert cut_right.explanation["sparse_unknown_intervals"][0]["to"] == T0 + 5 * HOUR

        # слева: из часа 3 (внутри интервала) до часа 6 + обычный час 5
        cut_left = w.measured(T0 + 3 * HOUR, T0 + 6 * HOUR)
        assert cut_left.value is None and approx(cut_left.known_value, 6.0), cut_left.to_dict()
        assert cut_left.availability == "partial"
        print("[OK] A14 учёт: интервал целиком в периоде даёт 10+60+6=76 (edge_approx, пояснение), "
              "период, режущий интервал, — неизвестен, известна только обычная часть (6)")
    finally:
        w.close()


def test_a14_replaced_meter_does_not_merge_runs():
    """Ряд sparse, пересекающий замену прибора (границу сегмента), целиком не
    учитывается: неизвестно, кому принадлежит расход."""
    w = World()
    try:
        # sparse-часы 0..4 принадлежат старому прибору; замена в часе 3
        w.rig.bindings.replace_meter(
            w.point.id, w.rig.sources.open_source(
                w.rig.meters.add("new", "new").id, "wb8-main", "new").id,
            at=T0 + 3 * HOUR)
        pts = [pt(0, 100.0), pt(4 * HOUR + 1800, 160.0)]
        w.put([compute_hourly_aggregate(1, T0 + k * HOUR, pts) for k in range(5)])
        r = w.measured(T0, T0 + 5 * HOUR)
        assert r.value is None, r.to_dict()                      # ряд пересекает границу
        print("[OK] A14: ряд, пересекающий замену прибора, не складывается в число")
    finally:
        w.close()


# ------------------------------------------- A14: почасовой воркер «в реальном времени»

class FakeDbClient:
    def __init__(self, points):
        self._points = points

    def get_values(self, device, control, ts_from, ts_to, limit=0, timeout_s=0):
        if control != "Total AP energy":
            return []
        return [p for p in self._points if ts_from <= p.timestamp <= ts_to]


def test_a14_realtime_worker_marks_earlier_hours_sparse():
    w = World()
    try:
        repo = w.rig.aggregates
        # час -1 подтверждён реальными точками: накопитель 100 к T0
        repo.upsert(HourlyAggregate(
            meter_id=w.meter.id, period_start=T0 - HOUR, period_end=T0,
            ap_energy_start=95.0, ap_energy_end=100.0, ap_energy_delta=5.0,
            p_avg=None, p_max=None, samples_count=6, quality_flag="ok", computed_at=T0))
        points = [pt(0, 100.0), pt(4 * HOUR + 1800, 160.0)]
        # окно RPC воркера — [начало-1ч, конец+5мин]: по состоянию «на сейчас»
        worker = Aggregator(AggregatorConfig(), FakeDbClient(points),
                            w.rig.meters, repo)
        for k in range(5):
            worker._compute_one_hour_for_meter(w.meter.id, "P", T0 + k * HOUR)
            if k < 4:
                row = repo.get(w.meter.id, T0 + k * HOUR)
                assert row.quality_flag == "edge_approx", (k, row)   # «на тот момент» — 0
        rows = repo.list_range(w.meter.id, T0, T0 + 5 * HOUR)
        assert [r.quality_flag for r in rows] == ["sparse"] * 5, [r.quality_flag for r in rows]
        assert all(r.ap_energy_delta is None for r in rows)
        # сумма периода осталась верной: 60
        r = w.measured(T0, T0 + 5 * HOUR)
        assert approx(r.value, 60.0), r.to_dict()
        # следующий час — обычный, интервал не расширяется
        worker._compute_one_hour_for_meter(w.meter.id, "P", T0 + 5 * HOUR)
        assert repo.get(w.meter.id, T0 + 5 * HOUR).quality_flag == "edge_approx"
        assert repo.get(w.meter.id, T0 + 4 * HOUR).quality_flag == "sparse"
        notes = _sparse_notes(repo.list_range(w.meter.id, T0 - HOUR, T0 + 6 * HOUR))
        assert len(notes) == 1 and notes[0]["from"] == T0 and notes[0]["to"] == T0 + 5 * HOUR
        assert approx(notes[0]["known_kwh"], 60.0) and "известен только целиком" in notes[0]["text"]
        print("[OK] A14 воркер по часам: ранние часы интервала задним числом помечены sparse, "
              "сумма периода 60, примечание для API")
    finally:
        w.close()


def test_a14_legacy_hybrid_falls_back_to_rpc_on_sparse():
    """Старый расчёт периода по агрегатам не складывает часы-пустышки."""
    from wb_energy_meter.consumption import ConsumptionService

    w = World()
    try:
        pts = [pt(0, 100.0), pt(4 * HOUR + 1800, 160.0)]
        w.put([compute_hourly_aggregate(1, T0 + k * HOUR, pts) for k in range(5)])
        svc = ConsumptionService(None, w.rig.aggregates, w.rig.meters)
        period = Period(ts_from=T0, ts_to=T0 + 5 * HOUR, label="t", description="t")
        got = svc._calculate_hybrid("P", period)
        assert got is None, got            # fallback на честный расчёт по краям
        print("[OK] A14 прежний расчёт: с sparse-часами переходит на расчёт по граничным показаниям")
    finally:
        w.close()


if __name__ == "__main__":
    test_a13_aggregate_detects_internal_reset()
    test_a13_measured_point_known_part_and_no_fifty()
    test_a13_sum_does_not_leak_known_part()
    test_a13_hourly_row_never_calls_known_part_consumption()
    test_a13_legacy_period_calculation()
    test_a14_sparse_classification_boundaries()
    test_a14_period_inside_cutting_and_mixed()
    test_a14_replaced_meter_does_not_merge_runs()
    test_a14_realtime_worker_marks_earlier_hours_sparse()
    test_a14_legacy_hybrid_falls_back_to_rpc_on_sparse()
    print("\nA13/A14 (Шаг 56) — все проверки пройдены.")

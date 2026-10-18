#!/usr/bin/env python3
"""Шаг 63 (партия находок 0.22.3, П9): агрегатор считает часы ФАЗ
(docs/design-phase-aggregates.md §5) на поддельном RPC к wb-mqtt-db.

Числа заданы вручную до запуска.

1. Почасовой расчёт фазы: `AP energy L1` 10.0 → 11.0 за час даёт дельту 1.0 в
   `phase_aggregates` (L1), а итог прибора `Total AP energy` 100 → 103.5 даёт
   3.5 в `period_aggregates`; мощность — из `P L1` (точки 100 и 300 Вт →
   p_avg 200, p_max 300). Строки двух таблиц не смешиваются.
2. Нагрузка на wb-mqtt-db: при целях {прибор: {L1, L3}} запрашиваются ровно
   каналы Total AP energy, Total P, AP energy L1, P L1, AP energy L3, P L3 —
   ни одного запроса по L2; без целей — только два канала, как до П9.
3. Флаги на фазах: A13 (100 → 0 → 150 внутри часа) — `reset`, известная часть
   150; A14 (100.0 в 00:00 и 160.0 в 04:30) — часы 0..4 `sparse`, дельта NULL;
   итог прибора при этом не тронут.
4. Перенос вперёд для фазы берёт якорь из таблицы ФАЗЫ (10.0), а не итога
   прибора (100.0).
5. Catch-up и латальщик догоняют фазы только у приборов с целями.
6. Наблюдатель: пара (прибор, фаза), появившаяся после старта, получает
   историю (24 часа при catchup_days=1) один раз; повторный проход ничего не
   запрашивает; настоящий поток подхватывает привязку, созданную в БД.
7. `open_phase_targets`: только ОТКРЫТЫЕ привязки с профилем `phase_lN`.
8. CLI: `aggregates recompute --phase L2|all`, `aggregates show --phase`,
   `aggregates status` с разделом пофазных итогов.

Самостоятельный скрипт (не pytest):
    python tests/test_step63_phase_aggregator.py
"""

from __future__ import annotations

import argparse
import contextlib
import io
import os
import sys
import tempfile
import threading
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from wb_energy_meter import cli  # noqa: E402
from wb_energy_meter.aggregates_repo import AggregateRepo, HourlyAggregate, align_hour_down  # noqa: E402
from wb_energy_meter.aggregator import Aggregator, AggregatorConfig  # noqa: E402
from wb_energy_meter.binding_service import PointBindingRepo, open_phase_targets  # noqa: E402
from wb_energy_meter.db import Database  # noqa: E402
from wb_energy_meter.point_repo import MeteringPointRepo, MeterSourceRepo  # noqa: E402
from wb_energy_meter.repo import GroupRepo, KvRepo, MeterRepo  # noqa: E402
from wb_energy_meter.wb_db_client import HistoryPoint  # noqa: E402

HOUR = 3600
T0 = 1_700_002_800  # кратно 3600
DEV = "wb-map3e_16"


class FakeRpc:
    """Поддельный wb-mqtt-db: ряды по (устройство, канал), журнал запросов."""

    def __init__(self, series=None):
        self.series = series or {}
        self.calls = []

    def get_values(self, device, control, ts_from, ts_to, limit=0, timeout_s=0):
        self.calls.append((device, control))
        pts = self.series.get((device, control), [])
        return [p for p in pts if ts_from <= p.timestamp <= ts_to]

    def channels(self):
        return sorted({c for _d, c in self.calls})


def pts(*pairs):
    return [HistoryPoint(timestamp=t, value=v) for t, v in pairs]


class World:
    def __init__(self, rpc, targets=None, **cfg):
        fd, self.path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(fd)
        os.unlink(self.path)
        self.db = Database(path=self.path)
        self.db.open()
        self.meters = MeterRepo(self.db, GroupRepo(self.db))
        self.meter = self.meters.add(DEV, "Прибор 16")
        self.total = AggregateRepo(self.db)
        self.rpc = rpc
        self.targets = targets if targets is not None else {}
        base = dict(catchup_start_delay_s=0, inter_meter_pause_s=0,
                    rpc_backoff_s=0, rpc_max_retries=1, catchup_days=1,
                    recompute_recent_h=6, phase_watch_interval_s=0.05)
        base.update(cfg)
        self.aggr = Aggregator(AggregatorConfig(**base), rpc, self.meters, self.total,
                               phase_targets=(lambda: self.targets) if targets is not None else None)

    def phase(self, ph):
        return self.total.for_profile("phase_" + ph.lower())

    def close(self):
        self.db.close()
        for ext in ("", "-wal", "-shm"):
            try:
                os.unlink(self.path + ext)
            except OSError:
                pass


def approx(a, b, eps=1e-9):
    return a is not None and abs(a - b) <= eps


def ok_row(meter_id, start, end_value, flag="ok", delta=0.0):
    return HourlyAggregate(
        meter_id=meter_id, period_start=start, period_end=start + HOUR,
        ap_energy_start=end_value - delta, ap_energy_end=end_value,
        ap_energy_delta=delta, p_avg=None, p_max=None, samples_count=2,
        quality_flag=flag, computed_at=start)


# ---------------------------------------------------------------------
# 1–2. почасовой расчёт и нагрузка на RPC
# ---------------------------------------------------------------------

def test_hourly_phase_calc_is_separate_from_total_and_loads_rpc_minimally():
    h = align_hour_down(time.time()) - HOUR
    series = {
        (DEV, "Total AP energy"): pts((h - 600, 100.0), (h + 3500, 103.5)),
        (DEV, "Total P"): pts((h + 100, 3000.0), (h + 200, 4000.0)),
        (DEV, "AP energy L1"): pts((h - 600, 10.0), (h + 3500, 11.0)),
        (DEV, "AP energy L2"): pts((h - 600, 20.0), (h + 3500, 22.0)),
        (DEV, "AP energy L3"): pts((h - 600, 30.0), (h + 3500, 30.5)),
        (DEV, "P L1"): pts((h + 100, 100.0), (h + 200, 300.0)),
    }
    rpc = FakeRpc(series)
    w = World(rpc, targets={})
    w.targets = {w.meter.id: {"L1", "L3"}}
    try:
        w.aggr._compute_previous_hour_for_all()
        total = w.total.get(w.meter.id, h)
        l1, l2, l3 = (w.phase(p).get(w.meter.id, h) for p in ("L1", "L2", "L3"))
        assert total is not None and approx(total.ap_energy_delta, 3.5), total
        assert l1 is not None and approx(l1.ap_energy_delta, 1.0) and l1.quality_flag == "ok", l1
        assert approx(l1.p_avg, 200.0) and approx(l1.p_max, 300.0), l1
        assert l3 is not None and approx(l3.ap_energy_delta, 0.5), l3
        assert l2 is None, "L2 без пофазной привязки не считается"
        assert rpc.channels() == sorted([
            "Total AP energy", "Total P", "AP energy L1", "P L1",
            "AP energy L3", "P L3"]), rpc.channels()
        assert not any("L2" in c for c in rpc.channels())
        # итог прибора не смешан с фазами
        assert w.total.count_for_meter(w.meter.id) == 1
        assert w.phase("L1").count_for_meter(w.meter.id) == 1
    finally:
        w.close()

    rpc2 = FakeRpc(series)
    w = World(rpc2)                       # без целей и без функции целей
    try:
        w.aggr._compute_previous_hour_for_all()
        assert rpc2.channels() == ["Total AP energy", "Total P"], rpc2.channels()
        assert w.phase("L1").count_for_meter(w.meter.id) == 0
    finally:
        w.close()
    print("[OK] почасовой расчёт фазы: L1=1.0, L3=0.5, итог 3.5 отдельно; RPC — ровно "
          "6 каналов (нет L2), без целей — как до П9")


# ---------------------------------------------------------------------
# 3. флаги A13/A14 на фазах
# ---------------------------------------------------------------------

def test_a13_reset_on_phase_only():
    series = {
        (DEV, "AP energy L2"): pts((T0 - 600, 100.0), (T0 + 600, 0.0), (T0 + 3000, 150.0)),
        (DEV, "Total AP energy"): pts((T0 - 600, 500.0), (T0 + 3000, 503.0)),
    }
    w = World(FakeRpc(series), targets={})
    try:
        agg = w.aggr._compute_one_hour_for_meter(w.meter.id, DEV, T0, phase="L2")
        assert agg.quality_flag == "reset" and approx(agg.ap_energy_delta, 150.0), agg
        total = w.aggr._compute_one_hour_for_meter(w.meter.id, DEV, T0)
        assert total.quality_flag == "ok" and approx(total.ap_energy_delta, 3.0), total
        assert w.phase("L2").get(w.meter.id, T0).quality_flag == "reset"
        assert w.total.get(w.meter.id, T0).quality_flag == "ok"
    finally:
        w.close()
    print("[OK] A13 на фазе: сброс 100→0→150 → reset, известная часть 150; итог прибора ok")


def test_a14_sparse_on_phase_marks_earlier_hours():
    series = {(DEV, "AP energy L1"): pts((T0 + 4 * HOUR + 1800, 160.0))}
    w = World(FakeRpc(series), targets={})
    try:
        ph = w.phase("L1")
        ph.upsert(ok_row(w.meter.id, T0 - HOUR, 100.0, delta=5.0))   # якорь: 100.0 к T0
        for k in (1, 2, 3):                                           # час за часом «в реальном времени»
            ph.upsert(ok_row(w.meter.id, T0 + k * HOUR, 100.0, flag="edge_approx"))
        agg = w.aggr._compute_one_hour_for_meter(
            w.meter.id, DEV, T0 + 4 * HOUR, phase="L1")
        assert agg.quality_flag == "sparse" and agg.ap_energy_delta is None, agg
        for k in range(0, 5):
            row = ph.get(w.meter.id, T0 + k * HOUR)
            assert row is not None and row.quality_flag == "sparse" \
                and row.ap_energy_delta is None, (k, row)
        assert w.total.count_for_meter(w.meter.id) == 0, "итог прибора не тронут"
    finally:
        w.close()
    print("[OK] A14 на фазе: часы 0..4 sparse с дельтой NULL, итог прибора не тронут")


# ---------------------------------------------------------------------
# 4. перенос вперёд берёт якорь той же фазы
# ---------------------------------------------------------------------

def test_carry_forward_uses_the_phase_anchor():
    w = World(FakeRpc({}), targets={})
    try:
        w.total.upsert(ok_row(w.meter.id, T0 - HOUR, 100.0, delta=1.0))
        w.phase("L1").upsert(ok_row(w.meter.id, T0 - HOUR, 10.0, delta=0.5))
        agg = w.aggr._compute_one_hour_for_meter(w.meter.id, DEV, T0, phase="L1")
        assert approx(agg.ap_energy_start, 10.0) and approx(agg.ap_energy_end, 10.0), agg
        assert approx(agg.ap_energy_delta, 0.0) and agg.quality_flag == "edge_approx", agg
        agg_total = w.aggr._compute_one_hour_for_meter(w.meter.id, DEV, T0)
        assert approx(agg_total.ap_energy_start, 100.0), agg_total
        # якоря фазы нет — перенос не берёт итог прибора
        agg_l2 = w.aggr._compute_one_hour_for_meter(w.meter.id, DEV, T0, phase="L2")
        assert agg_l2.quality_flag == "no_data", agg_l2
    finally:
        w.close()
    print("[OK] перенос вперёд: якорь фазы 10.0, итога прибора 100.0; без якоря фазы — no_data")


# ---------------------------------------------------------------------
# 5. catch-up и латальщик
# ---------------------------------------------------------------------

def _ramp(device, now_h, rates):
    """Накопители, растущие на rate кВт·ч в час, точка на КАЖДОЙ границе часа."""
    series = {}
    for channel, rate in rates.items():
        series[(device, channel)] = [
            HistoryPoint(timestamp=now_h + k * HOUR, value=1000.0 + rate * k)
            for k in range(-30, 2)]
    return series


def test_catchup_and_patcher_cover_phases_with_targets_only():
    now_h = align_hour_down(time.time())
    rates = {"Total AP energy": 3.5, "AP energy L1": 1.0, "AP energy L2": 2.0,
             "AP energy L3": 0.5}
    rpc = FakeRpc(_ramp(DEV, now_h, rates))
    w = World(rpc, targets={})
    w.targets = {w.meter.id: {"L1", "L2"}}
    try:
        w.aggr._do_catchup()
        for ph, rate in (("L1", 1.0), ("L2", 2.0)):
            rows = w.phase(ph).list_range(w.meter.id, now_h - 24 * HOUR, now_h)
            assert len(rows) == 24, (ph, len(rows))
            assert all(approx(r.ap_energy_delta, rate, 1e-6) and r.quality_flag == "ok"
                       for r in rows), (ph, rows[:2])
        assert w.phase("L3").count_for_meter(w.meter.id) == 0, "L3 без цели не считается"
        assert w.total.count_for_meter(w.meter.id) == 24
        assert "AP energy L3" not in rpc.channels(), rpc.channels()

        # латальщик: no_data на фазе пересчитывается, отсутствующие часы дописываются
        recent = [now_h - k * HOUR for k in range(1, 7)]
        for hh in recent[:3]:
            w.phase("L1").upsert(HourlyAggregate(
                meter_id=w.meter.id, period_start=hh, period_end=hh + HOUR,
                ap_energy_start=None, ap_energy_end=None, ap_energy_delta=None,
                p_avg=None, p_max=None, samples_count=0, quality_flag="no_data",
                computed_at=1))
        w.aggr._do_patch()
        for hh in recent:
            row = w.phase("L1").get(w.meter.id, hh)
            assert row is not None and row.quality_flag == "ok" \
                and approx(row.ap_energy_delta, 1.0, 1e-6), (hh, row)
    finally:
        w.close()
    print("[OK] catch-up: 24 часа для L1 (1.0) и L2 (2.0), L3 не считается; "
          "латальщик чинит no_data на фазе")


# ---------------------------------------------------------------------
# 6. наблюдатель новых пар
# ---------------------------------------------------------------------

def test_watcher_backfills_new_pair_once():
    now_h = align_hour_down(time.time())
    rpc = FakeRpc(_ramp(DEV, now_h, {"AP energy L1": 1.0, "AP energy L2": 2.0}))
    w = World(rpc, targets={})
    try:
        assert w.aggr.check_new_phase_targets() == 0 and rpc.calls == []
        w.targets = {w.meter.id: {"L1"}}
        assert w.aggr.check_new_phase_targets() == 1
        assert w.phase("L1").count_for_meter(w.meter.id) == 24
        calls = len(rpc.calls)
        assert calls >= 1 and set(rpc.channels()) == {"AP energy L1"}, rpc.channels()
        assert w.aggr.check_new_phase_targets() == 0 and len(rpc.calls) == calls, \
            "известная пара не запрашивается повторно"

        w.targets = {w.meter.id: {"L1", "L2"}}
        assert w.aggr.check_new_phase_targets() == 1
        assert w.phase("L2").count_for_meter(w.meter.id) == 24
        assert set(rpc.channels()) == {"AP energy L1", "AP energy L2"}

        # привязку закрыли и открыли снова: пара «новая», но дыр нет — запросов нет
        w.targets = {}
        w.aggr.check_new_phase_targets()
        before = len(rpc.calls)
        w.targets = {w.meter.id: {"L1"}}
        assert w.aggr.check_new_phase_targets() == 1
        assert len(rpc.calls) == before, "дозаполнять нечего — RPC не нужен"
        assert w.aggr.status()["phase_backfilled_pairs"] == 3
    finally:
        w.close()
    print("[OK] наблюдатель: новая пара догоняется один раз (24 часа), повтор — без RPC")


def test_watcher_thread_picks_up_real_binding():
    now_h = align_hour_down(time.time())
    rpc = FakeRpc(_ramp(DEV, now_h, {"AP energy L3": 0.5}))
    w = World(rpc)
    try:
        sources, points, bindings = (MeterSourceRepo(w.db), MeteringPointRepo(w.db),
                                     PointBindingRepo(w.db))
        w.aggr._phase_targets_fn = lambda: open_phase_targets(w.db)
        t = threading.Thread(target=w.aggr._run_phase_watch, daemon=True)
        t.start()
        time.sleep(0.2)                          # поток крутится, целей нет
        assert w.phase("L3").count_for_meter(w.meter.id) == 0
        src = sources.open_source(w.meter.id, "wb8-main", DEV)
        p = points.add("L3-load", "Нагрузка на L3")
        bindings.open_binding(p.id, src.id, "phase_l3", valid_from=0)
        deadline = time.time() + 10
        while time.time() < deadline and w.phase("L3").count_for_meter(w.meter.id) < 24:
            time.sleep(0.05)
        assert w.phase("L3").count_for_meter(w.meter.id) == 24, "поток не догнал историю фазы"
        w.aggr._stop.set()
        t.join(timeout=5)
        assert not t.is_alive()
    finally:
        w.close()
    print("[OK] поток-наблюдатель увидел привязку phase_l3 в БД и догнал 24 часа")


def test_open_phase_targets_only_open_phase_bindings():
    w = World(FakeRpc({}))
    try:
        sources, points, bindings = (MeterSourceRepo(w.db), MeteringPointRepo(w.db),
                                     PointBindingRepo(w.db))
        m2 = w.meters.add("wb-map3e_17", "Прибор 17")
        m3 = w.meters.add("wb-map3e_18", "Прибор 18")
        s1 = sources.open_source(w.meter.id, "wb8-main", DEV)
        s2 = sources.open_source(m2.id, "wb8-main", "wb-map3e_17")
        s3 = sources.open_source(m3.id, "wb8-main", "wb-map3e_18")
        a, b, c, d = (points.add(x, x) for x in ("a", "b", "c", "d"))
        bindings.open_binding(a.id, s1.id, "phase_l1", valid_from=0)
        bindings.open_binding(b.id, s1.id, "phase_l3", valid_from=0)
        bindings.open_binding(c.id, s2.id, "total_3p", valid_from=0)
        closed = bindings.open_binding(d.id, s3.id, "phase_l2", valid_from=0)
        bindings.close_binding(closed.id, at=100)
        assert open_phase_targets(w.db) == {w.meter.id: {"L1", "L3"}}, open_phase_targets(w.db)
    finally:
        w.close()
    print("[OK] open_phase_targets: только открытые phase_lN, total_3p и закрытые не в счёт")


# ---------------------------------------------------------------------
# 8. CLI
# ---------------------------------------------------------------------

def _args(**kw):
    base = dict(device_id=DEV, period=None, from_date=None, to_date=None, phase=None,
                config="x")
    base.update(kw)
    return argparse.Namespace(**base)


def test_cli_recompute_show_status_with_phases():
    now_h = align_hour_down(time.time())
    rates = {"Total AP energy": 3.5, "AP energy L1": 1.0, "AP energy L2": 2.0,
             "AP energy L3": 0.5}
    w = World(FakeRpc(_ramp(DEV, now_h, rates)))
    saved = cli._make_wb_db_client
    cli._make_wb_db_client = lambda args: w.rpc
    kv = KvRepo(w.db)
    try:
        def run(fn, **kw):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = fn(_args(**kw), w.db, w.meters, GroupRepo(w.db), kv)
            return rc, buf.getvalue()

        rc, out = run(cli.cmd_aggregates_recompute, period="last_24h", phase="L2")
        assert rc == 0, out
        assert w.phase("L2").count_for_meter(w.meter.id) > 0
        assert w.phase("L1").count_for_meter(w.meter.id) == 0
        assert w.total.count_for_meter(w.meter.id) == 0, "без all итог прибора не трогаем"
        assert set(w.rpc.channels()) == {"AP energy L2"}, w.rpc.channels()
        row = w.phase("L2").list_range(w.meter.id, now_h - 24 * HOUR, now_h)[-1]
        assert approx(row.ap_energy_delta, 2.0, 1e-6), row

        rc, out = run(cli.cmd_aggregates_recompute, period="last_24h", phase="all")
        assert rc == 0, out
        for ph in ("L1", "L2", "L3"):
            assert w.phase(ph).count_for_meter(w.meter.id) > 0, ph
        assert w.total.count_for_meter(w.meter.id) > 0

        rc, out = run(cli.cmd_aggregates_recompute, period="last_24h")
        assert rc == 0 and "L1" not in out, "без --phase — только итог прибора"

        rc, out = run(cli.cmd_aggregates_show, period="last_24h", phase="L1")
        assert rc == 0 and "фаза L1" in out and "Сумма:" in out, out
        rc, out = run(cli.cmd_aggregates_status)
        assert rc == 0 and "Пофазные итоги" in out and "L3" in out, out
    finally:
        cli._make_wb_db_client = saved
        w.close()
    print("[OK] CLI: recompute --phase L2/all, show --phase L1, status с пофазным разделом")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print("\nВСЁ ЗЕЛЁНОЕ: %d проверок" % len(tests))

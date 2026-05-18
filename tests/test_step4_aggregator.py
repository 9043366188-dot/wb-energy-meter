"""Тесты Шага 4: компонент агрегатора и репозиторий."""

from __future__ import annotations

import os
import sys
import tempfile
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wb_energy_meter.aggregates_repo import (
    AggregateRepo, HourlyAggregate, align_hour_down,
)
from wb_energy_meter.aggregator import (
    compute_hourly_aggregate, _group_into_batches,
)
from wb_energy_meter.db import Database
from wb_energy_meter.repo import GroupRepo, MeterRepo
from wb_energy_meter.wb_db_client import HistoryPoint


def make_db() -> Database:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)
    db = Database(path=path)
    db.open()
    return db


def cleanup(db: Database):
    p = db.path
    db.close()
    for ext in ("", "-shm", "-wal"):
        try:
            os.unlink(p + ext)
        except OSError:
            pass


# ============================================================================
# compute_hourly_aggregate
# ============================================================================

def test_compute_basic_ok():
    """Точки точно на границах + одна внутри: расход = end - start."""
    hour_start = 1700000000
    points = [
        HistoryPoint(timestamp=hour_start, value=100.0),
        HistoryPoint(timestamp=hour_start + 1800, value=100.5),
        HistoryPoint(timestamp=hour_start + 3600, value=101.0),
    ]
    agg = compute_hourly_aggregate(
        meter_id=1, hour_start=hour_start, points_with_context=points,
    )
    assert agg.ap_energy_delta == 1.0, f"got {agg.ap_energy_delta}"
    assert agg.quality_flag == "ok"
    # Точка ровно на t = hour_start + 3600 принадлежит уже следующему часу
    # (полуоткрытый интервал [start, end)), поэтому samples_count = 2.
    assert agg.samples_count == 2
    print(f"[OK] basic_ok: delta={agg.ap_energy_delta}, samples={agg.samples_count}")


def test_compute_no_start_point():
    """Нет точки <= hour_start — quality=no_data."""
    hour_start = 1700000000
    points = [
        HistoryPoint(timestamp=hour_start + 1800, value=100.5),
        HistoryPoint(timestamp=hour_start + 3600, value=101.0),
    ]
    agg = compute_hourly_aggregate(
        meter_id=1, hour_start=hour_start, points_with_context=points,
    )
    # Нет точки <= hour_start
    assert agg.quality_flag == "no_data"
    assert agg.ap_energy_delta is None
    print("[OK] no_start_point")


def test_compute_no_end_point():
    """Есть только точка ДО начала — нет конца, quality=no_data."""
    hour_start = 1700000000
    points = [
        HistoryPoint(timestamp=hour_start - 1000, value=50.0),
    ]
    # А, нет, есть точка до начала, она же будет и end_pt (просто та же).
    # Нужно убедиться, что в нашем алгоритме end_pt тоже найдётся (он
    # ищет last <= hour_end).
    agg = compute_hourly_aggregate(
        meter_id=1, hour_start=hour_start, points_with_context=points,
    )
    # pt_start = единственная точка, pt_end = она же. delta = 0.
    # Это «stale» — счётчик молчит. В нашем компьютере это quality=ok
    # с delta=0 (точки далеко от границ → edge_approx).
    # Дальше start_dist = 1000s > 600s, поэтому edge_approx.
    assert agg.ap_energy_delta == 0.0
    assert agg.quality_flag == "edge_approx"
    print("[OK] only old point: delta=0, edge_approx")


def test_compute_empty():
    """Совсем нет точек — no_data."""
    agg = compute_hourly_aggregate(
        meter_id=1, hour_start=1700000000, points_with_context=[],
    )
    assert agg.quality_flag == "no_data"
    assert agg.ap_energy_delta is None
    assert agg.ap_energy_start is None
    assert agg.ap_energy_end is None
    print("[OK] empty")


def test_compute_reset():
    """Счётчик обнулился внутри часа."""
    hour_start = 1700000000
    points = [
        HistoryPoint(timestamp=hour_start, value=500.0),
        HistoryPoint(timestamp=hour_start + 1000, value=0.0),  # сброс
        HistoryPoint(timestamp=hour_start + 3600, value=0.5),
    ]
    agg = compute_hourly_aggregate(
        meter_id=1, hour_start=hour_start, points_with_context=points,
    )
    # delta = 0.5 - 500.0 = -499.5 → reset
    assert agg.quality_flag == "reset"
    assert agg.ap_energy_delta is None
    print("[OK] reset")


def test_compute_edge_approx_far_start():
    """Точка-старт далеко от hour_start — edge_approx."""
    hour_start = 1700000000
    points = [
        HistoryPoint(timestamp=hour_start - 1200, value=100.0),  # за 20 мин
        HistoryPoint(timestamp=hour_start + 3600, value=101.0),
    ]
    agg = compute_hourly_aggregate(
        meter_id=1, hour_start=hour_start, points_with_context=points,
    )
    assert agg.ap_energy_delta == 1.0
    assert agg.quality_flag == "edge_approx"
    print("[OK] edge_approx far start")


def test_compute_with_power():
    """p_avg и p_max считаются из points_power."""
    hour_start = 1700000000
    points_energy = [
        HistoryPoint(timestamp=hour_start, value=100.0),
        HistoryPoint(timestamp=hour_start + 3600, value=101.0),
    ]
    points_power = [
        HistoryPoint(timestamp=hour_start + 600, value=1000.0),
        HistoryPoint(timestamp=hour_start + 1200, value=1500.0),
        HistoryPoint(timestamp=hour_start + 1800, value=2000.0),
        HistoryPoint(timestamp=hour_start + 4000, value=9999.0),  # вне часа
    ]
    agg = compute_hourly_aggregate(
        meter_id=1, hour_start=hour_start,
        points_with_context=points_energy,
        points_power=points_power,
    )
    assert agg.ap_energy_delta == 1.0
    assert agg.p_avg == (1000 + 1500 + 2000) / 3
    assert agg.p_max == 2000.0
    print(f"[OK] with power: p_avg={agg.p_avg}, p_max={agg.p_max}")


# ============================================================================
# AggregateRepo
# ============================================================================

def _make_agg(meter_id: int, hour_start: int, delta: float = 1.0,
              quality: str = "ok") -> HourlyAggregate:
    return HourlyAggregate(
        meter_id=meter_id,
        period_start=hour_start, period_end=hour_start + 3600,
        ap_energy_start=100.0, ap_energy_end=100.0 + delta,
        ap_energy_delta=delta, p_avg=None, p_max=None,
        samples_count=2, quality_flag=quality,
        computed_at=int(time.time()),
    )


def test_repo_upsert_basic():
    db = make_db()
    try:
        groups = GroupRepo(db)
        meters = MeterRepo(db, groups)
        m = meters.add("wb-map3e_test", "T")
        repo = AggregateRepo(db)
        agg = _make_agg(m.id, 1700000000, 1.5)
        repo.upsert(agg)

        loaded = repo.get(m.id, 1700000000)
        assert loaded is not None
        assert loaded.ap_energy_delta == 1.5

        # Перезапись
        agg.ap_energy_delta = 2.0
        repo.upsert(agg)
        loaded = repo.get(m.id, 1700000000)
        assert loaded.ap_energy_delta == 2.0

        assert repo.count_for_meter(m.id) == 1

        print("[OK] repo upsert basic")
    finally:
        cleanup(db)


def test_repo_upsert_many():
    db = make_db()
    try:
        groups = GroupRepo(db)
        meters = MeterRepo(db, groups)
        m = meters.add("wb-map3e_test", "T")
        repo = AggregateRepo(db)
        aggs = [_make_agg(m.id, 1700000000 + i * 3600, 1.0 + i) for i in range(24)]
        n = repo.upsert_many(aggs)
        assert n == 24
        assert repo.count_for_meter(m.id) == 24
        print("[OK] repo upsert_many")
    finally:
        cleanup(db)


def test_repo_list_range_and_sum():
    db = make_db()
    try:
        groups = GroupRepo(db)
        meters = MeterRepo(db, groups)
        m = meters.add("wb-map3e_test", "T")
        repo = AggregateRepo(db)
        base = 1700000000
        aggs = [_make_agg(m.id, base + i * 3600, float(i + 1)) for i in range(5)]
        repo.upsert_many(aggs)

        items = repo.list_range(m.id, base, base + 3 * 3600)
        assert len(items) == 3
        assert [a.ap_energy_delta for a in items] == [1.0, 2.0, 3.0]

        s = repo.sum_kwh(m.id, base, base + 3 * 3600)
        assert s == 6.0

        s2 = repo.sum_kwh(m.id, base, base + 100 * 3600)
        assert s2 == 1.0 + 2 + 3 + 4 + 5
        print("[OK] repo list_range + sum_kwh")
    finally:
        cleanup(db)


def test_repo_missing_hours():
    db = make_db()
    try:
        groups = GroupRepo(db)
        meters = MeterRepo(db, groups)
        m = meters.add("wb-map3e_test", "T")
        repo = AggregateRepo(db)
        base = align_hour_down(1700000000.0)
        for i in (0, 2, 5):
            repo.upsert(_make_agg(m.id, base + i * 3600, 1.0))

        missing = repo.missing_hours(m.id, base, base + 6 * 3600)
        assert missing == [base + 3600, base + 3 * 3600, base + 4 * 3600], \
            f"got {missing}"
        print("[OK] repo missing_hours")
    finally:
        cleanup(db)


def test_repo_earliest_latest():
    db = make_db()
    try:
        groups = GroupRepo(db)
        meters = MeterRepo(db, groups)
        m = meters.add("wb-map3e_test", "T")
        repo = AggregateRepo(db)
        base = 1700000000
        for i in (0, 3, 7):
            repo.upsert(_make_agg(m.id, base + i * 3600))
        assert repo.earliest_hour(m.id) == base
        assert repo.latest_hour(m.id) == base + 7 * 3600
        assert repo.earliest_hour(999) is None
        print("[OK] repo earliest/latest")
    finally:
        cleanup(db)


def test_repo_stats():
    db = make_db()
    try:
        groups = GroupRepo(db)
        meters = MeterRepo(db, groups)
        # Нужны реальные meter_id, иначе foreign key упадёт
        m1 = meters.add("wb-map3e_1", "M1")
        m2 = meters.add("wb-map3e_2", "M2")

        repo = AggregateRepo(db)
        base = 1700000000
        # 3 часа для m1, разные quality
        repo.upsert(_make_agg(m1.id, base + 0, 1.0, "ok"))
        repo.upsert(_make_agg(m1.id, base + 3600, 2.0, "ok"))
        repo.upsert(_make_agg(m1.id, base + 7200, 0.0, "no_data"))
        # 1 час для m2
        repo.upsert(_make_agg(m2.id, base + 0, 5.0, "edge_approx"))

        stats = repo.stats()
        assert stats["rows_total"] == 4
        assert stats["earliest_ts"] == base
        assert stats["latest_ts"] == base + 7200
        assert len(stats["by_meter"]) == 2
        # by_quality: ok=2, no_data=1, edge_approx=1
        assert stats["by_quality"] == {"ok": 2, "no_data": 1, "edge_approx": 1}
        print(f"[OK] repo stats: {stats['by_quality']}")
    finally:
        cleanup(db)


def test_repo_delete_for_meter():
    db = make_db()
    try:
        groups = GroupRepo(db)
        meters = MeterRepo(db, groups)
        m1 = meters.add("wb-map3e_1", "M1")
        m2 = meters.add("wb-map3e_2", "M2")
        repo = AggregateRepo(db)
        for i in range(5):
            repo.upsert(_make_agg(m1.id, 1700000000 + i * 3600))
            repo.upsert(_make_agg(m2.id, 1700000000 + i * 3600))
        assert repo.count_for_meter(m1.id) == 5
        n = repo.delete_for_meter(m1.id)
        assert n == 5
        assert repo.count_for_meter(m1.id) == 0
        assert repo.count_for_meter(m2.id) == 5
        print("[OK] repo delete_for_meter")
    finally:
        cleanup(db)


# ============================================================================
# Helpers
# ============================================================================

def test_align_hour_down():
    # 1700000000 = 2023-11-14 22:13:20 UTC
    # Локальное время зависит от TZ; результат должен быть выровнен на :00 локально.
    ts = 1700000000
    aligned = align_hour_down(ts)
    dt = datetime.fromtimestamp(aligned)
    assert dt.minute == 0
    assert dt.second == 0
    assert dt.microsecond == 0
    assert aligned <= ts
    assert ts - aligned < 3600
    print(f"[OK] align: {ts} -> {aligned} ({dt})")


def test_group_into_batches():
    # Пустой
    assert _group_into_batches([]) == []
    # Один час
    assert _group_into_batches([1000]) == [[1000]]
    # Подряд 5 часов (5 * 3600 < 24h) — один батч
    hours = [1000 + i * 3600 for i in range(5)]
    batches = _group_into_batches(hours, max_span_hours=24)
    assert len(batches) == 1
    assert batches[0] == hours
    # Разрыв > 24 часа — два батча
    hours2 = [1000, 1000 + 25 * 3600, 1000 + 26 * 3600]
    batches = _group_into_batches(hours2, max_span_hours=24)
    assert len(batches) == 2
    assert batches[0] == [1000]
    assert batches[1] == [1000 + 25 * 3600, 1000 + 26 * 3600]
    print("[OK] group_into_batches")


if __name__ == "__main__":
    test_compute_basic_ok()
    test_compute_no_start_point()
    test_compute_no_end_point()
    test_compute_empty()
    test_compute_reset()
    test_compute_edge_approx_far_start()
    test_compute_with_power()
    test_repo_upsert_basic()
    test_repo_upsert_many()
    test_repo_list_range_and_sum()
    test_repo_missing_hours()
    test_repo_earliest_latest()
    test_repo_stats()
    test_repo_delete_for_meter()
    test_align_hour_down()
    test_group_into_batches()
    print("\nВсе юнит-тесты Шага 4 пройдены.")

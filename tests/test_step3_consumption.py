"""Тесты Шага 3 — юнит, без MQTT."""

from __future__ import annotations

import os, sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wb_energy_meter.consumption import (
    calculate_from_points, INTERNAL_GAP_THRESHOLD_S,
)
from wb_energy_meter.periods import (
    PERIOD_PRESETS, Period, build_period, parse_user_datetime,
)
from wb_energy_meter.wb_db_client import HistoryPoint


def test_period_presets_basic():
    fake_now = datetime(2026, 5, 4, 14, 30, 15)
    for preset in PERIOD_PRESETS:
        p = build_period(preset, now=fake_now)
        assert p.ts_to > p.ts_from
        assert p.label == preset
    print("[OK] period presets")


def test_period_today():
    n = datetime(2026, 5, 4, 14, 30)
    p = build_period("today", now=n)
    assert p.ts_from == datetime(2026, 5, 4).timestamp()
    assert p.ts_to == n.timestamp()
    print("[OK] today")


def test_period_yesterday():
    n = datetime(2026, 5, 4, 14, 30)
    p = build_period("yesterday", now=n)
    assert p.ts_from == datetime(2026, 5, 3).timestamp()
    assert p.ts_to == datetime(2026, 5, 4).timestamp()
    print("[OK] yesterday")


def test_period_this_month():
    n = datetime(2026, 5, 4, 14, 30)
    p = build_period("this_month", now=n)
    assert p.ts_from == datetime(2026, 5, 1).timestamp()
    print("[OK] this_month")


def test_period_last_month():
    n = datetime(2026, 5, 4)
    p = build_period("last_month", now=n)
    assert p.ts_from == datetime(2026, 4, 1).timestamp()
    assert p.ts_to == datetime(2026, 5, 1).timestamp()

    n2 = datetime(2026, 1, 15)
    p2 = build_period("last_month", now=n2)
    assert p2.ts_from == datetime(2025, 12, 1).timestamp()
    assert p2.ts_to == datetime(2026, 1, 1).timestamp()
    print("[OK] last_month")


def test_period_custom():
    p = build_period(ts_from=1700000000, ts_to=1700100000)
    assert p.label == "custom"
    try:
        build_period(ts_from=100, ts_to=50); assert False
    except ValueError: pass
    try:
        build_period("bad_preset", now=datetime.now()); assert False
    except ValueError: pass
    print("[OK] custom")


def test_parse_user_datetime():
    assert parse_user_datetime("2026-05-04") == datetime(2026, 5, 4).timestamp()
    assert parse_user_datetime("2026-05-04 14:30") == datetime(2026, 5, 4, 14, 30).timestamp()
    try:
        parse_user_datetime("garbage"); assert False
    except ValueError: pass
    print("[OK] parse_user_datetime")


def _per(t0, t1):
    return Period(ts_from=t0, ts_to=t1, label="custom", description="x")


def test_consumption_simple_ok():
    p = _per(1000, 2000)
    pts = [
        HistoryPoint(1000, 100.0),
        HistoryPoint(1500, 110.0),
        HistoryPoint(2000, 120.0),
    ]
    r = calculate_from_points(pts, p, "x")
    assert r.consumption_kwh == 20.0
    assert r.quality == "ok"
    assert r.samples_in_period == 3
    print("[OK] simple ok")


def test_consumption_edge_approx():
    p = _per(1000, 2000)
    pts = [
        HistoryPoint(1000 - 600, 100.0),  # 10 мин до начала
        HistoryPoint(1500, 110.0),
        HistoryPoint(1800, 115.0),
    ]
    r = calculate_from_points(pts, p, "x")
    assert r.consumption_kwh == 15.0
    assert r.quality == "edge_approx", f"got {r.quality}"
    print("[OK] edge approx")


def test_consumption_no_data():
    p = _per(1000, 2000)
    r = calculate_from_points([], p, "x")
    assert r.consumption_kwh is None
    assert r.quality == "no_data"
    print("[OK] no_data")


def test_consumption_reset():
    p = _per(1000, 2000)
    pts = [
        HistoryPoint(1000, 500.0),
        HistoryPoint(1500, 510.0),
        HistoryPoint(1700, 0.5),
        HistoryPoint(2000, 2.0),
    ]
    r = calculate_from_points(pts, p, "x")
    assert r.consumption_kwh is None
    assert r.quality == "reset"
    print("[OK] reset")


def test_consumption_internal_gap():
    p = _per(1000, 1000 + 4 * 3600)
    pts = [
        HistoryPoint(1000, 100.0),
        HistoryPoint(1100, 101.0),
        HistoryPoint(1100 + INTERNAL_GAP_THRESHOLD_S + 60, 110.0),
        HistoryPoint(1000 + 4 * 3600, 120.0),
    ]
    r = calculate_from_points(pts, p, "x")
    assert r.consumption_kwh == 20.0
    assert r.quality == "gap"
    print("[OK] internal gap")


def test_consumption_stale():
    p = _per(86400, 86400 + 3600)
    pts = [HistoryPoint(100, 42.0)]
    r = calculate_from_points(pts, p, "x")
    assert r.consumption_kwh == 0.0
    assert r.quality == "stale"
    print("[OK] stale")


def test_consumption_user_case():
    """Реальный кейс: нет точек в истории."""
    p = _per(1000, 2000)
    r = calculate_from_points([], p, "wb-map3e_16")
    assert r.consumption_kwh is None
    assert r.quality == "no_data"
    print("[OK] user case")


def test_to_dict():
    p = _per(1000, 2000)
    pts = [HistoryPoint(1000, 100.0), HistoryPoint(2000, 120.0)]
    r = calculate_from_points(pts, p, "x")
    d = r.to_dict()
    import json
    s = json.dumps(d, ensure_ascii=False)
    assert "20.0" in s
    print("[OK] to_dict")


if __name__ == "__main__":
    test_period_presets_basic()
    test_period_today()
    test_period_yesterday()
    test_period_this_month()
    test_period_last_month()
    test_period_custom()
    test_parse_user_datetime()
    test_consumption_simple_ok()
    test_consumption_edge_approx()
    test_consumption_no_data()
    test_consumption_reset()
    test_consumption_internal_gap()
    test_consumption_stale()
    test_consumption_user_case()
    test_to_dict()
    print("\nВсе юнит-тесты Шага 3 пройдены.")

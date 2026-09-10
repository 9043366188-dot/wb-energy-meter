"""Перенос вперёд последнего известного значения (aggregator.py,
aggregates_repo.py) — Wiren Board публикует control в MQTT только по
изменению значения (retained, publish-on-delta): час без новых точек в
RPC-окне не означает "прибора нет", может означать "значение не
изменилось дольше часа". Без переноса такой час ошибочно считался
no_data. Найдено пользователем на реальном дампе (dry-run переноса
v0.11.1 -> v2, meter 2 — напряжение приходит, а "Total AP energy" ни
разу не менялся за 2202 часа истории)."""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wb_energy_meter.aggregates_repo import AggregateRepo, HourlyAggregate
from wb_energy_meter.aggregator import inject_carry_forward_point
from wb_energy_meter.db import Database
from wb_energy_meter.repo import GroupRepo, MeterRepo
from wb_energy_meter.wb_db_client import HistoryPoint


def make_db():
    fd, path = tempfile.mkstemp(suffix=".db")
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


H = 3600
T0 = 1_700_000_000 // H * H  # выровнено на час


# --------------------------------------------------------------------- pure fn

def test_inject_carry_forward_point_used_when_no_anchor_in_window():
    hour_start = T0
    points = [HistoryPoint(timestamp=hour_start + 1800, value=100.0)]  # только внутри часа, нет <= hour_start
    carried = HistoryPoint(timestamp=hour_start - 7200, value=100.0)
    out = inject_carry_forward_point(hour_start, points, carried)
    assert carried in out
    assert len(out) == 2
    print("[OK] inject_carry_forward_point_used_when_no_anchor_in_window")


def test_inject_carry_forward_point_skipped_when_anchor_exists():
    hour_start = T0
    points = [HistoryPoint(timestamp=hour_start, value=100.0)]  # уже есть точка <= hour_start
    carried = HistoryPoint(timestamp=hour_start - 7200, value=50.0)
    out = inject_carry_forward_point(hour_start, points, carried)
    assert out == points, "не должен подмешивать carried, если якорь уже есть в окне"
    print("[OK] inject_carry_forward_point_skipped_when_anchor_exists")


def test_inject_carry_forward_point_noop_when_no_carried():
    hour_start = T0
    points = [HistoryPoint(timestamp=hour_start + 1800, value=100.0)]
    out = inject_carry_forward_point(hour_start, points, None)
    assert out == points
    print("[OK] inject_carry_forward_point_noop_when_no_carried")


# --------------------------------------------------------------------- repo

def test_last_known_value_before_finds_real_ok_row():
    db, path = make_db()
    try:
        MeterRepo(db, GroupRepo(db)).add("wb-map3e_1", "M1")
        repo = AggregateRepo(db)
        repo.upsert(HourlyAggregate(
            meter_id=1, period_start=T0, period_end=T0 + H,
            ap_energy_start=10.0, ap_energy_end=12.0, ap_energy_delta=2.0,
            p_avg=None, p_max=None, samples_count=5,
            quality_flag="ok", computed_at=T0,
        ))
        anchor = repo.last_known_value_before(1, T0 + 50 * H)
        assert anchor is not None
        assert anchor.value == 12.0
        assert anchor.timestamp == T0 + H
        print("[OK] last_known_value_before_finds_real_ok_row")
    finally:
        cleanup(db, path)


def test_last_known_value_before_ignores_carried_and_no_data_rows():
    """Ключевой инвариант: якорем для ДАЛЬНЕЙШЕГО переноса может быть
    только строка с quality_flag='ok' — иначе перенесённое значение
    протягивалось бы бесконечно, никогда не упираясь в max_lookback_s."""
    db, path = make_db()
    try:
        MeterRepo(db, GroupRepo(db)).add("wb-map3e_1", "M1")
        repo = AggregateRepo(db)
        repo.upsert(HourlyAggregate(
            meter_id=1, period_start=T0, period_end=T0 + H,
            ap_energy_start=10.0, ap_energy_end=12.0, ap_energy_delta=2.0,
            p_avg=None, p_max=None, samples_count=5,
            quality_flag="ok", computed_at=T0,
        ))
        # более свежая строка, но с приближённой/перенесённой границей —
        # не должна считаться "настоящим" якорем
        repo.upsert(HourlyAggregate(
            meter_id=1, period_start=T0 + H, period_end=T0 + 2 * H,
            ap_energy_start=12.0, ap_energy_end=12.0, ap_energy_delta=0.0,
            p_avg=None, p_max=None, samples_count=0,
            quality_flag="edge_approx", computed_at=T0,
        ))
        repo.upsert(HourlyAggregate(
            meter_id=1, period_start=T0 + 2 * H, period_end=T0 + 3 * H,
            ap_energy_start=None, ap_energy_end=None, ap_energy_delta=None,
            p_avg=None, p_max=None, samples_count=0,
            quality_flag="no_data", computed_at=T0,
        ))
        anchor = repo.last_known_value_before(1, T0 + 3 * H)
        assert anchor is not None
        assert anchor.value == 12.0
        assert anchor.timestamp == T0 + H, (
            "должен найти именно ok-строку (T0), а не более свежую "
            "edge_approx/no_data — иначе цепочка переноса никогда не "
            "упрётся в max_lookback_s"
        )
        print("[OK] last_known_value_before_ignores_carried_and_no_data_rows")
    finally:
        cleanup(db, path)


def test_last_known_value_before_respects_max_lookback():
    db, path = make_db()
    try:
        MeterRepo(db, GroupRepo(db)).add("wb-map3e_1", "M1")
        repo = AggregateRepo(db)
        repo.upsert(HourlyAggregate(
            meter_id=1, period_start=T0, period_end=T0 + H,
            ap_energy_start=10.0, ap_energy_end=12.0, ap_energy_delta=2.0,
            p_avg=None, p_max=None, samples_count=5,
            quality_flag="ok", computed_at=T0,
        ))
        far_future = T0 + 40 * 86400  # 40 дней спустя
        assert repo.last_known_value_before(1, far_future, max_lookback_s=30 * 86400) is None, (
            "якорь старше 30 дней не должен переноситься как будто "
            "ничего не произошло"
        )
        assert repo.last_known_value_before(1, far_future, max_lookback_s=None) is not None, (
            "без ограничения якорь должен находиться"
        )
        print("[OK] last_known_value_before_respects_max_lookback")
    finally:
        cleanup(db, path)


def test_last_known_value_before_none_when_no_history():
    db, path = make_db()
    try:
        MeterRepo(db, GroupRepo(db)).add("wb-map3e_1", "M1")
        repo = AggregateRepo(db)
        assert repo.last_known_value_before(1, T0) is None
        print("[OK] last_known_value_before_none_when_no_history")
    finally:
        cleanup(db, path)


# --------------------------------------------------------------------- сквозной сценарий

def test_end_to_end_flat_meter_gets_zero_delta_not_no_data():
    """Ровно сценарий пользователя: meter, у которого 'Total AP energy'
    не менялся уже больше часа (RPC-окно за текущий час пусто), но
    есть достоверная история. Раньше это был бы no_data навсегда,
    теперь — известный delta=0.0, помечен edge_approx (не свежая точка)."""
    db, path = make_db()
    try:
        MeterRepo(db, GroupRepo(db)).add("wb-map3e_1", "M1")
        MeterRepo(db, GroupRepo(db)).add("wb-map3e_2", "M2")
        repo = AggregateRepo(db)
        repo.upsert(HourlyAggregate(
            meter_id=2, period_start=T0, period_end=T0 + H,
            ap_energy_start=5.0, ap_energy_end=5.0, ap_energy_delta=0.0,
            p_avg=0.0, p_max=0.0, samples_count=2,
            quality_flag="ok", computed_at=T0,
        ))

        next_hour = T0 + H
        # RPC вернул пусто для этого часа — значение не менялось
        points_from_rpc = []
        anchor = repo.last_known_value_before(2, next_hour, max_lookback_s=30 * 86400)
        points = inject_carry_forward_point(next_hour, points_from_rpc, anchor)

        from wb_energy_meter.aggregator import compute_hourly_aggregate
        agg = compute_hourly_aggregate(
            meter_id=2, hour_start=next_hour, points_with_context=points,
        )
        assert agg.ap_energy_delta == 0.0, (
            "delta должен быть 0.0 (известно), а не None/no_data"
        )
        assert agg.quality_flag == "edge_approx", (
            "перенесённая точка не свежая (> 10 мин от границы часа) — "
            "штатная проверка start_dist должна пометить это прозрачно"
        )
        print("[OK] end_to_end_flat_meter_gets_zero_delta_not_no_data")
    finally:
        cleanup(db, path)


if __name__ == "__main__":
    test_inject_carry_forward_point_used_when_no_anchor_in_window()
    test_inject_carry_forward_point_skipped_when_anchor_exists()
    test_inject_carry_forward_point_noop_when_no_carried()
    test_last_known_value_before_finds_real_ok_row()
    test_last_known_value_before_ignores_carried_and_no_data_rows()
    test_last_known_value_before_respects_max_lookback()
    test_last_known_value_before_none_when_no_history()
    test_end_to_end_flat_meter_gets_zero_delta_not_no_data()
    print("[ALL OK] test_step20_carry_forward")

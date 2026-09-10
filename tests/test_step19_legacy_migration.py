"""Перенос данных v0.11.1 -> v2 (wb_energy_meter/legacy_migration.py).

Проверяет docs/migration-plan-v2.md §5 (перенос данных) и §6
(идемпотентность/устойчивость к сбою) на реалистичной легаси-БД
(tests/fixtures/legacy_db_0_11_1.py, сценарий §5.2 ТЗ), и — главное —
что после переноса расчётный сервис (accounting_service.measured_point)
читает СТАРЫЕ строки period_aggregates через НОВУЮ точку учёта без
потери значений: это и есть де-риск переноса реальных данных перед
тем, как строить UI поверх новой схемы.
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.fixtures.legacy_db_0_11_1 import build_legacy_db, SCENARIO_ENERGY_KWH

from wb_energy_meter.legacy_migration import (
    migrate_meters_and_groups, _point_code_for_meter,
)
from wb_energy_meter.point_repo import MeteringPointRepo, MeterSourceRepo
from wb_energy_meter.binding_service import PointBindingRepo
from wb_energy_meter.aggregates_repo import AggregateRepo, HourlyAggregate
from wb_energy_meter.accounting_service import measured_point
from wb_energy_meter.accounting_contract import AVAILABILITY_COMPLETE

H_START = 1_700_000_000 // 3600 * 3600
H_END = H_START + 3600


def _group_id(db, name):
    with db.read() as c:
        row = c.execute("SELECT id FROM meter_groups WHERE name=?", (name,)).fetchone()
        return row["id"] if row else None


def _membership_point_ids(db, group_id):
    with db.read() as c:
        rows = c.execute(
            "SELECT point_id FROM group_memberships "
            "WHERE group_id=? AND valid_to IS NULL", (group_id,)
        ).fetchall()
        return {r["point_id"] for r in rows}


def _migration_map_count(db, legacy_table="meters", new_table="metering_points"):
    with db.read() as c:
        row = c.execute(
            "SELECT COUNT(*) AS n FROM migration_map "
            "WHERE legacy_table=? AND new_table=?", (legacy_table, new_table)
        ).fetchone()
        return row["n"]


# --------------------------------------------------------------------- A41

def test_migrate_creates_points_sources_bindings():
    fx = build_legacy_db()
    try:
        points_repo = MeteringPointRepo(fx.db)
        sources_repo = MeterSourceRepo(fx.db)
        bindings_repo = PointBindingRepo(fx.db)

        report = migrate_meters_and_groups(fx.db)

        assert len(report.migrated_points) == 5
        assert report.skipped_points == []
        assert report.recovered_points == []

        for key, meter in fx.meters.items():
            code = _point_code_for_meter(meter.device_id)
            point = points_repo.get_by_code(code)
            assert point is not None, f"точка для {key} не создана"

            expected_enabled = (key != "retired")
            assert bool(point.enabled) == expected_enabled, key

            src = sources_repo.get_current(meter.id)
            assert src is not None and src.device_id == meter.device_id

            binding = bindings_repo.get_open_primary(point.id)
            assert binding is not None
            assert binding.channel_profile == "total_3p"
            assert binding.meter_source_id == src.id
            assert binding.valid_from == 0, (
                "легаси-точка должна покрывать всю историю (valid_from=0), "
                "иначе прошлые period_aggregates станут фиктивным разрывом"
            )
        print("[OK] test_migrate_creates_points_sources_bindings")
    finally:
        fx.cleanup()


def test_migrate_group_memberships():
    fx = build_legacy_db()
    try:
        points_repo = MeteringPointRepo(fx.db)
        migrate_meters_and_groups(fx.db)

        tsex_group = _group_id(fx.db, "Цех")
        server_group = _group_id(fx.db, "Серверная")
        assert tsex_group is not None and server_group is not None

        tsex_point = points_repo.get_by_code(_point_code_for_meter(fx.meters["tsex"].device_id))
        stanok_point = points_repo.get_by_code(_point_code_for_meter(fx.meters["stanok"].device_id))
        server_point = points_repo.get_by_code(_point_code_for_meter(fx.meters["server"].device_id))
        retired_point = points_repo.get_by_code(_point_code_for_meter(fx.meters["retired"].device_id))
        input_point = points_repo.get_by_code(_point_code_for_meter(fx.meters["input"].device_id))

        assert _membership_point_ids(fx.db, tsex_group) == {tsex_point.id, stanok_point.id}
        assert _membership_point_ids(fx.db, server_group) == {server_point.id, retired_point.id}

        # "Ввод ГРЩ-1" в легаси-схеме без группы — членства не должно быть нигде
        with fx.db.read() as c:
            row = c.execute(
                "SELECT COUNT(*) AS n FROM group_memberships WHERE point_id=?",
                (input_point.id,)
            ).fetchone()
            assert row["n"] == 0
        print("[OK] test_migrate_group_memberships")
    finally:
        fx.cleanup()


# --------------------------------------------------------------------- A42 (идемпотентность)

def test_migrate_idempotent_rerun():
    fx = build_legacy_db()
    try:
        sources_repo = MeterSourceRepo(fx.db)
        bindings_repo = MeteringPointRepo(fx.db)  # noqa: используется ниже как points_repo
        points_repo = bindings_repo
        binding_repo2 = PointBindingRepo(fx.db)

        report1 = migrate_meters_and_groups(fx.db)
        assert len(report1.migrated_points) == 5

        report2 = migrate_meters_and_groups(fx.db)
        assert report2.migrated_points == []
        assert len(report2.skipped_points) == 5
        assert report2.recovered_points == []
        assert report2.memberships_created == 0

        assert _migration_map_count(fx.db) == 5

        for key, meter in fx.meters.items():
            point = points_repo.get_by_code(_point_code_for_meter(meter.device_id))
            # ровно один открытый источник и ровно одна открытая привязка —
            # повторный прогон не создал дублей
            with fx.db.read() as c:
                n_sources = c.execute(
                    "SELECT COUNT(*) AS n FROM meter_sources WHERE meter_id=?",
                    (meter.id,)
                ).fetchone()["n"]
                n_bindings = c.execute(
                    "SELECT COUNT(*) AS n FROM point_bindings WHERE point_id=? AND valid_to IS NULL",
                    (point.id,)
                ).fetchone()["n"]
            assert n_sources == 1, key
            assert n_bindings == 1, key
        print("[OK] test_migrate_idempotent_rerun")
    finally:
        fx.cleanup()


def test_migrate_recovers_from_partial_failure():
    """Имитация падения между созданием точки и записью migration_map
    (docs/migration-plan-v2.md §6) — самый вероятный вид сбоя на реальных
    данных при обрыве питания/процесса посередине прогона."""
    fx = build_legacy_db()
    try:
        points_repo = MeteringPointRepo(fx.db)

        crashed_meter = fx.meters["server"]
        code = _point_code_for_meter(crashed_meter.device_id)
        # "прошлый прогон" создал точку, но ничего больше (ни source, ни
        # binding, ни migration_map) — ровно то состояние, которое оставил
        # бы crash сразу после point_repo.add() внутри migrate_*.
        pre_created = points_repo.add(code, crashed_meter.display_name)

        report = migrate_meters_and_groups(fx.db)

        assert len(report.recovered_points) == 1
        assert report.recovered_points[0] == crashed_meter.id
        assert len(report.migrated_points) == 5, (
            "восстановленная точка должна тоже попасть в итоговый migrated_points"
        )

        # не задвоили точку
        with fx.db.read() as c:
            n = c.execute(
                "SELECT COUNT(*) AS n FROM metering_points WHERE code=?", (code,)
            ).fetchone()["n"]
        assert n == 1

        point = points_repo.get_by_code(code)
        assert point.id == pre_created.id, "должна быть довооружена ТА ЖЕ точка, не новая"

        sources_repo = MeterSourceRepo(fx.db)
        bindings_repo = PointBindingRepo(fx.db)
        assert sources_repo.get_current(crashed_meter.id) is not None
        binding = bindings_repo.get_open_primary(point.id)
        assert binding is not None and binding.channel_profile == "total_3p"
        print("[OK] test_migrate_recovers_from_partial_failure")
    finally:
        fx.cleanup()


# --------------------------------------------------------------------- A43 (непрерывность истории)

def test_migrate_preserves_aggregate_continuity():
    """Ключевая проверка де-риска: старые period_aggregates (ключ —
    легаси meter_id) должны читаться через НОВУЮ точку учёта после
    переноса без потери значения — ровно числа §5.2 ТЗ."""
    fx = build_legacy_db()
    try:
        agg_repo = AggregateRepo(fx.db)
        now = int(time.time())
        for key, energy in SCENARIO_ENERGY_KWH.items():
            meter = fx.meters[key]
            agg_repo.upsert(HourlyAggregate(
                meter_id=meter.id, period_start=H_START, period_end=H_END,
                ap_energy_start=0.0, ap_energy_end=energy, ap_energy_delta=energy,
                p_avg=None, p_max=None, samples_count=12,
                quality_flag="ok", computed_at=now,
            ))

        migrate_meters_and_groups(fx.db)

        points_repo = MeteringPointRepo(fx.db)
        sources_repo = MeterSourceRepo(fx.db)
        bindings_repo = PointBindingRepo(fx.db)

        for key, energy in SCENARIO_ENERGY_KWH.items():
            meter = fx.meters[key]
            point = points_repo.get_by_code(_point_code_for_meter(meter.device_id))
            result = measured_point(
                bindings_repo, agg_repo, sources_repo, point.id, H_START, H_END,
            )
            assert result.availability == AVAILABILITY_COMPLETE, key
            assert result.value == energy, (key, result.value, energy)
        print("[OK] test_migrate_preserves_aggregate_continuity")
    finally:
        fx.cleanup()


def test_migrate_leaves_db_integrity_clean():
    """docs/migration-plan-v2.md §1/§3: PRAGMA integrity_check/
    foreign_key_check должны оставаться чистыми после переноса."""
    fx = build_legacy_db()
    try:
        migrate_meters_and_groups(fx.db)
        with fx.db.read() as c:
            fk_violations = c.execute("PRAGMA foreign_key_check").fetchall()
            integrity = c.execute("PRAGMA integrity_check").fetchall()
        assert fk_violations == [], fk_violations
        assert len(integrity) == 1 and integrity[0][0] == "ok", integrity
        print("[OK] test_migrate_leaves_db_integrity_clean")
    finally:
        fx.cleanup()


if __name__ == "__main__":
    test_migrate_creates_points_sources_bindings()
    test_migrate_group_memberships()
    test_migrate_idempotent_rerun()
    test_migrate_recovers_from_partial_failure()
    test_migrate_preserves_aggregate_continuity()
    test_migrate_leaves_db_integrity_clean()
    print("[ALL OK] test_step19_legacy_migration")

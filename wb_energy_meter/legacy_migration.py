"""Перенос данных из легаси-схемы (meters/meter_groups, миграции 001-004)
в доменную модель v2 (ТЗ §11.1, docs/migration-plan-v2.md §5).

Пишется и проверяется лично (пользователь: "самые ответственные части
делай сам") — это прямое DML-преобразование существующих
производственных данных: ошибка здесь либо теряет историю, либо
задваивает точку учёта в будущих расчётах.

Что переносит этот модуль (docs/migration-plan-v2.md §5, пункты 1-2):

  1. Каждая строка `meters` (включая `enabled=0` — архивные приборы НЕ
     пропускаются, ТЗ §6.2 "Ограничения": "существующие использованные
     точки/приборы архивируются, а не удаляются") -> один `meter_source`
     (тот же физический адрес, тот же `meters.id`, адресация не меняется)
     + один новый `metering_point` + одна `point_binding`
     (`channel_profile='total_3p'`, `role='primary'`). `valid_from=0`:
     легаси-история не разбита на интервалы, поэтому точка считается
     "существовавшей всегда" — это нужно, чтобы ВСЕ прошлые строки
     `period_aggregates` (которые НЕ переносятся построчно, см. п.4
     ниже) остались видны через сегментацию `binding_service`, а не
     стали фиктивным разрывом (`AVAILABILITY_MISSING`) только из-за даты
     самой миграции.
  2. `meters.group_id` -> `group_memberships`. Новая сущность "группа"
     не заводится — используется та же строка `meter_groups` (ТЗ §2:
     "используем существующие meter_groups", см. заголовок миграции 005).

Что этот модуль ЯВНО НЕ переносит (чтобы не фабриковать полноту):

  - `plan_zones`/`plan_links` -> `plan_items`/аннотации плана (docs §5
    п.3). Визуальный перенос плана естественно связан с этапом D
    (редактор плана v2) — делается отдельным шагом там, а не здесь.
  - `period_aggregates` построчно (docs §5 п.4 — явно "не переносятся",
    остаются легаси-данными существующего канала).
  - kv-протокол отката self-update.sh: `domain_model_generation`/
    `minimum_reader_generation`/`model_v2_first_write_revision`
    (docs §7) — это часть self-update.sh, этап F, не эта функция.
  - Версионируемая иерархия самих групп (`group_parent_bindings`) —
    `meter_groups.parent_id` сегодня всегда NULL в проде (не
    используется приложением), выдумывать историю нечем (docs §5 п.2:
    "переносить как есть, не выдумывать историю иерархии").

Идемпотентность (docs §6): перед созданием новой сущности проверяется
`migration_map(legacy_table, legacy_id, new_table)`. Дополнительно —
устойчивость к прогону, упавшему МЕЖДУ созданием сущности и записью в
`migration_map` (наиболее вероятная точка сбоя на реальных данных):
если точка с ожидаемым кодом уже существует, но карты миграции для неё
нет, модуль не создаёт дубль и не падает — он восстанавливает связь и
достраивает недостающие шаги (source/binding/membership) идемпотентно.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import List, Optional

from .point_repo import MeterSourceRepo, MeteringPointRepo
from .binding_service import PointBindingRepo

log = logging.getLogger(__name__)

# Тот же условный контроллер, что и в test_step15_binding_service.py —
# легаси-инсталляция всегда однокотроллерная (один WB8), поэтому
# фиксированное значение, а не поле в старой схеме (которого там нет).
DEFAULT_CONTROLLER_KEY = "wb8-main"

# Легаси-приборы измеряли только суммарную мощность/энергию без разбивки
# по фазам — ближайший валидный профиль канала для "Total AP energy".
CHANNEL_PROFILE_LEGACY = "total_3p"

# Версия миграции схемы, вводящей целевые таблицы (005_v2_domain_schema) —
# записывается в migration_map.migration_version, не является версией
# ЭТОГО модуля.
SCHEMA_MIGRATION_VERSION = 5


@dataclass
class LegacyMigrationReport:
    migrated_points: List[int] = field(default_factory=list)
    skipped_points: List[int] = field(default_factory=list)  # legacy meters.id, уже мигрирован
    recovered_points: List[int] = field(default_factory=list)  # legacy meters.id, достроен после сбоя
    memberships_created: int = 0
    warnings: List[str] = field(default_factory=list)


def _map_get(db, legacy_table, legacy_id, new_table) -> Optional[int]:
    with db.read() as c:
        row = c.execute(
            "SELECT new_id FROM migration_map "
            "WHERE legacy_table=? AND legacy_id=? AND new_table=?",
            (legacy_table, legacy_id, new_table),
        ).fetchone()
        return row["new_id"] if row else None


def _map_put(db, legacy_table, legacy_id, new_table, new_id, version) -> None:
    now = int(time.time())
    with db.transaction() as c:
        c.execute(
            "INSERT OR IGNORE INTO migration_map "
            "(legacy_table, legacy_id, new_table, new_id, migration_version, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (legacy_table, legacy_id, new_table, new_id, version, now),
        )


def _point_code_for_meter(device_id: str) -> str:
    return f"legacy:{device_id}"


def _add_group_membership(db, group_id: int, point_id: int) -> None:
    """UNIQUE(group_id, point_id) WHERE valid_to IS NULL сама даёт
    идемпотентность — повторная вставка той же пары молча игнорируется."""
    now = int(time.time())
    with db.transaction() as c:
        c.execute(
            "INSERT OR IGNORE INTO group_memberships "
            "(group_id, point_id, valid_from, valid_to, created_at) "
            "VALUES (?, ?, 0, NULL, ?)",
            (group_id, point_id, now),
        )


def migrate_meters_and_groups(db, controller_key: str = DEFAULT_CONTROLLER_KEY) -> LegacyMigrationReport:
    """Перенести все строки `meters` (включая отключённые) в
    metering_point/meter_source/point_binding + членство в группах.

    Безопасно перезапускать в любой момент, в т.ч. после падения
    посередине предыдущего прогона (см. docstring модуля)."""
    point_repo = MeteringPointRepo(db)
    source_repo = MeterSourceRepo(db)
    binding_repo = PointBindingRepo(db)
    report = LegacyMigrationReport()

    with db.read() as c:
        meter_rows = c.execute(
            "SELECT id, device_id, display_name, group_id, role, enabled, notes "
            "FROM meters ORDER BY id"
        ).fetchall()

    for row in meter_rows:
        legacy_id = row["id"]
        code = _point_code_for_meter(row["device_id"])

        point_id = _map_get(db, "meters", legacy_id, "metering_points")
        if point_id is not None:
            report.skipped_points.append(legacy_id)
            continue

        existing = point_repo.get_by_code(code)
        if existing is not None:
            # Прошлый прогон создал точку, но упал до записи в
            # migration_map (либо до, либо между source/binding) — не
            # плодим дубль по UNIQUE(code), достраиваем недостающее.
            point = existing
            report.recovered_points.append(legacy_id)
            report.warnings.append(
                f"meters.id={legacy_id}: точка {code!r} уже существовала без "
                f"записи migration_map — связь восстановлена, шаги достроены"
            )
            want_enabled = bool(row["enabled"])
            if bool(point.enabled) != want_enabled:
                point_repo.set_enabled(point.id, want_enabled)
        else:
            point = point_repo.add(
                code, row["display_name"], installation_note=row["notes"],
            )
            if not row["enabled"]:
                point_repo.set_enabled(point.id, False)

        _map_put(db, "meters", legacy_id, "metering_points", point.id, SCHEMA_MIGRATION_VERSION)

        source = source_repo.get_current(legacy_id)
        if source is None:
            source = source_repo.open_source(legacy_id, controller_key, row["device_id"])

        if binding_repo.get_open_primary(point.id) is None:
            binding_repo.open_binding(
                point.id, source.id, CHANNEL_PROFILE_LEGACY, role="primary",
                valid_from=0,
                replacement_note=f"перенесено из v0.11.1 (meters.id={legacy_id})",
            )

        if row["group_id"] is not None:
            _add_group_membership(db, row["group_id"], point.id)
            report.memberships_created += 1

        report.migrated_points.append(point.id)

    return report

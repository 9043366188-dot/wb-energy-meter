"""Контракт качества расчёта (ТЗ docs/TZ-metering-architecture-dashboard.md,
§5.3 «Время, качество и пропуски», §9.2 «Контракты новых маршрутов»).

Общий envelope результата расчёта показателя, который должны использовать
все будущие сервисы (`accounting_service`, `metrics_service`) и экраны —
обзор, `/plans/live`, отчёты, CSV. Модуль сам по себе не читает БД и не
считает энергию: он фиксирует форму результата и инварианты null-vs-0,
обязательные независимо от источника числа (ТЗ §5: «Не реализовывать
разные формулы в дашборде, /plans/live, отчётах и JavaScript»).

Этап A (фикстуры/контракт/план миграции, §12 ТЗ) не подключает этот
контракт к существующим маршрутам — это задача этапов B/C. Модуль
существует, чтобы последующие этапы имели готовый, проверенный тип
результата вместо разных импровизированных словарей на каждом экране.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Sequence

MODE_MEASURED = "measured"
MODE_SUM = "sum"
MODE_BALANCE = "balance"
MODE_COMPARISON = "comparison"
VALID_MODES = (MODE_MEASURED, MODE_SUM, MODE_BALANCE, MODE_COMPARISON)

AVAILABILITY_COMPLETE = "complete"
AVAILABILITY_PARTIAL = "partial"
AVAILABILITY_MISSING = "missing"
VALID_AVAILABILITY = (AVAILABILITY_COMPLETE, AVAILABILITY_PARTIAL, AVAILABILITY_MISSING)

STRUCTURE_VERIFIED = "verified"
STRUCTURE_UNVERIFIED = "unverified"
STRUCTURE_ASSUMED_LEGACY = "assumed_legacy"
VALID_STRUCTURE_QUALITY = (
    STRUCTURE_VERIFIED, STRUCTURE_UNVERIFIED, STRUCTURE_ASSUMED_LEGACY)

SOURCE_MEASURED = "measured"
SOURCE_CALCULATED = "calculated"
SOURCE_ESTIMATED = "estimated"
VALID_SOURCE = (SOURCE_MEASURED, SOURCE_CALCULATED, SOURCE_ESTIMATED)

# quality_flags — открытый список (§5.3 «и другие объяснимые причины»),
# здесь только явно названные в ТЗ значения.
QF_EDGE_APPROX = "edge_approx"
QF_GAP = "gap"
QF_RESET = "reset"
QF_STALE = "stale"
QF_ESTIMATED = "estimated"
QF_CHANNEL_ERROR = "channel_error"
QF_UNSYNCHRONISED = "unsynchronised"
QF_MISSING_SOURCE = "missing_source"
QF_HELD = "held"


class ContractViolation(ValueError):
    """Результат нарушает инвариант §5.3 — такой объект не разрешён."""


@dataclass
class ResultPeriod:
    ts_from: str  # ISO-8601 UTC, например "2026-09-08T21:00:00Z"
    ts_to: str
    timezone: str

    def to_dict(self):
        return {"from": self.ts_from, "to": self.ts_to,
                "timezone": self.timezone}


@dataclass
class MetricResult:
    """Envelope результата расчёта — форма из примера §9.2 ТЗ.

    Инварианты (проверяются в `__post_init__`, поэтому неправильный
    результат невозможно создать, а не только «не рекомендуется»):

    - §5.3: «known_value=0 при valid_count=0 также должно быть null» —
      здесь обобщено на любой known_value: без валидных частей известной
      суммы не существует.
    - §5.3/A9/A12: `value` — числом, только когда `valid_count` покрывает
      весь `expected_count`; иначе используется `known_value`, а `value`
      обязан быть `None`.
    - `availability=missing` не совместим с числовым `value`.
    - `availability=complete` при `expected_count > 0` требует числового
      `value` (иначе это `partial`/`missing`, а не `complete`).
    """

    metric: str
    unit: str
    mode: str
    value: Optional[float]
    known_value: Optional[float]
    availability: str
    quality_flags: list = field(default_factory=list)
    structure_quality: str = STRUCTURE_UNVERIFIED
    source: str = SOURCE_CALCULATED
    expected_count: int = 0
    valid_count: int = 0
    missing_ids: list = field(default_factory=list)
    configuration_revision_id: Optional[int] = None
    configuration_revision_ids: list = field(default_factory=list)
    as_of: Optional[str] = None
    period: Optional[ResultPeriod] = None
    actual_boundaries: Optional[dict] = None
    explanation: Optional[dict] = None

    def __post_init__(self):
        self._validate()

    def _validate(self):
        if self.mode not in VALID_MODES:
            raise ContractViolation(f"mode={self.mode!r} не из {VALID_MODES}")
        if self.availability not in VALID_AVAILABILITY:
            raise ContractViolation(
                f"availability={self.availability!r} не из "
                f"{VALID_AVAILABILITY}")
        if self.structure_quality not in VALID_STRUCTURE_QUALITY:
            raise ContractViolation(
                f"structure_quality={self.structure_quality!r} не из "
                f"{VALID_STRUCTURE_QUALITY}")
        if self.source not in VALID_SOURCE:
            raise ContractViolation(
                f"source={self.source!r} не из {VALID_SOURCE}")
        if self.valid_count < 0 or self.expected_count < 0:
            raise ContractViolation("expected_count/valid_count не могут быть отрицательными")
        if self.valid_count > self.expected_count:
            raise ContractViolation("valid_count не может превышать expected_count")

        if self.valid_count == 0 and self.known_value is not None:
            raise ContractViolation(
                "known_value должен быть null при valid_count=0 (§5.3)")

        if self.value is not None and self.valid_count < self.expected_count:
            raise ContractViolation(
                "value не может быть числом при valid_count < expected_count "
                "— используйте known_value для неполной суммы (§5.3, A9/A12)")

        if self.availability == AVAILABILITY_MISSING and self.value is not None:
            raise ContractViolation(
                "availability=missing несовместим с числовым value")

        if (self.availability == AVAILABILITY_COMPLETE
                and self.value is None and self.expected_count > 0):
            raise ContractViolation(
                "availability=complete требует числового value при "
                "expected_count > 0")

    def to_dict(self):
        d = {
            "metric": self.metric, "unit": self.unit, "mode": self.mode,
            "value": self.value, "known_value": self.known_value,
            "availability": self.availability,
            "quality_flags": list(self.quality_flags),
            "structure_quality": self.structure_quality,
            "source": self.source,
            "expected_count": self.expected_count,
            "valid_count": self.valid_count,
            "missing_ids": list(self.missing_ids),
            "configuration_revision_id": self.configuration_revision_id,
            "configuration_revision_ids": list(self.configuration_revision_ids),
            "as_of": self.as_of,
        }
        if self.period is not None:
            d["period"] = self.period.to_dict()
        if self.actual_boundaries is not None:
            d["actual_boundaries"] = self.actual_boundaries
        if self.explanation is not None:
            d["explanation"] = self.explanation
        return d


def resolve_percentage(numerator: Optional[float], base: Optional[float]):
    """§5.3/A11: процент — только при определённом ненулевом положительном
    знаменателе; иначе `(None, причина)`, без деления на ноль и без
    фиктивных 100%."""
    if numerator is None or base is None:
        return None, "no_data"
    if base <= 0:
        return None, "zero_or_negative_base"
    return round(numerator / base * 100, 2), None


def known_sum(parts: Sequence[Optional[float]], expected_ids: Sequence[str],
              valid_ids: Sequence[str]):
    """Собрать `(value, known_value, expected_count, valid_count,
    missing_ids)` по правилам A9/A10/A12:

    - `value` — числом, только если `valid_ids` покрывает весь
      `expected_ids` (полная сумма);
    - иначе `value=None`, а `known_value` — сумма известных частей,
      подписанная отдельно;
    - если валидных частей нет вовсе — оба поля `None` (не `0.0`).

    `parts` должны быть в том же порядке, что и `expected_ids`; элемент
    `None` в `parts` считается неизвестным независимо от того, есть ли
    его id в `valid_ids`.
    """
    if len(parts) != len(expected_ids):
        raise ValueError("parts и expected_ids должны быть одной длины")
    expected_count = len(expected_ids)
    valid_set = set(valid_ids)
    missing_ids = [i for i in expected_ids if i not in valid_set]
    valid_count = expected_count - len(missing_ids)
    known_parts = [p for p, i in zip(parts, expected_ids)
                   if i in valid_set and p is not None]
    if valid_count == 0 or not known_parts:
        return None, None, expected_count, valid_count, missing_ids
    known = round(sum(known_parts), 6)
    if valid_count >= expected_count and not missing_ids:
        return known, known, expected_count, valid_count, missing_ids
    return None, known, expected_count, valid_count, missing_ids

"""Тесты Шага 13 — контракт качества расчёта (ТЗ §5.3/§9.2, этап A).

Самостоятельный скрипт (не pytest):
    python tests/test_step13_accounting_contract.py

Чистые тесты без БД/Flask/MQTT — проверяют инварианты null-vs-0 из §5.3
и числовые сценарии A08/A09/A10/A11/A12 из §13 ТЗ на уровне контракта,
до того как он подключён к сервисам (этапы B/C).
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wb_energy_meter.accounting_contract import (
    MetricResult, ContractViolation, ResultPeriod,
    resolve_percentage, known_sum,
    AVAILABILITY_COMPLETE, AVAILABILITY_PARTIAL, AVAILABILITY_MISSING,
    MODE_SUM, SOURCE_CALCULATED, STRUCTURE_VERIFIED,
)


def test_zero_consumption_is_not_missing():
    """A10: исправное измерение с нулевым расходом = 0, а не null."""
    value, known, expected, valid, missing = known_sum([0.0], ["p1"], ["p1"])
    assert value == 0.0 and known == 0.0 and missing == []
    print("[OK] нулевой расход при valid_count=1 — число 0.0, не None")


def test_all_missing_is_null_not_zero():
    """A10: точка без данных -> null, а не 0."""
    value, known, expected, valid, missing = known_sum([None], ["p1"], [])
    assert value is None and known is None and missing == ["p1"]
    print("[OK] valid_count=0 -> value и known_value оба None")


def test_known_value_never_set_without_valid_parts():
    """§5.3: «known_value=0 при valid_count=0 также должно быть null»."""
    try:
        MetricResult(
            metric="energy_import", unit="kWh", mode=MODE_SUM,
            value=None, known_value=0.0,
            availability=AVAILABILITY_MISSING,
            expected_count=1, valid_count=0)
    except ContractViolation:
        print("[OK] known_value с valid_count=0 отклонён контрактом")
    else:
        raise AssertionError("ожидалось ContractViolation")


def test_partial_sum_example_a12():
    """A12: 9 из 10 часовых агрегатов -> value=null, known_value подписан."""
    parts = [10.0] * 9 + [None]
    expected_ids = [f"h{i}" for i in range(10)]
    valid_ids = [f"h{i}" for i in range(9)]
    value, known, expected, valid, missing = known_sum(parts, expected_ids, valid_ids)
    assert value is None, "неполная сумма не должна выдаваться за полный расход"
    assert known == 90.0 and expected == 10 and valid == 9
    assert missing == ["h9"]
    print("[OK] A12: неполная сумма -> value=None, known_value=90.0")


def test_full_sum_gives_value():
    value, known, expected, valid, missing = known_sum(
        [10.0, 20.0, 30.0], ["a", "b", "c"], ["a", "b", "c"])
    assert value == 60.0 and known == 60.0 and missing == []
    print("[OK] полная сумма -> value == known_value")


def test_value_number_requires_full_coverage():
    """value числом при valid_count < expected_count запрещено контрактом."""
    try:
        MetricResult(
            metric="energy_import", unit="kWh", mode=MODE_SUM,
            value=90.0, known_value=90.0,
            availability=AVAILABILITY_PARTIAL,
            expected_count=10, valid_count=9, missing_ids=["h9"])
    except ContractViolation:
        print("[OK] value=число при неполном составе отклонён контрактом")
    else:
        raise AssertionError("ожидалось ContractViolation")


def test_percentage_a11():
    """A11: ввод равен нулю/отрицателен или база сравнения отсутствует ->
    null с причиной, без деления на ноль и фиктивных 100%."""
    pct, reason = resolve_percentage(-10.0, 0.0)
    assert pct is None and reason == "zero_or_negative_base"
    pct, reason = resolve_percentage(None, 100.0)
    assert pct is None and reason == "no_data"
    pct, reason = resolve_percentage(-10.0, 100.0)
    assert pct == -10.0 and reason is None
    print("[OK] A11: деления на ноль и фиктивных 100% нет")


def test_negative_imbalance_keeps_sign_a08():
    """A08: R = ΣE_in − ΣE_out; ввод 100, выходы 70 и 40 -> -10, -10%,
    знак небаланса сохраняется (не «генерация»)."""
    r = round(100.0 - 70.0 - 40.0, 6)
    assert r == -10.0
    pct, reason = resolve_percentage(r, 100.0)
    assert pct == -10.0 and reason is None
    print("[OK] A08: отрицательный небаланс сохраняет знак")


def test_example_json_shape_matches_tz_9_2():
    """Форма/числа из примера §9.2 ТЗ («неполная сумма, а не нулевое
    потребление»)."""
    result = MetricResult(
        metric="energy_import", unit="kWh", mode=MODE_SUM,
        value=None, known_value=90.0,
        availability=AVAILABILITY_PARTIAL,
        quality_flags=["missing_source"],
        structure_quality=STRUCTURE_VERIFIED,
        source=SOURCE_CALCULATED,
        expected_count=3, valid_count=2, missing_ids=["point-c"],
        configuration_revision_id=17, configuration_revision_ids=[17],
        as_of="2026-09-09T11:32:05Z",
        period=ResultPeriod(ts_from="2026-09-08T21:00:00Z",
                             ts_to="2026-09-09T11:32:05Z",
                             timezone="Europe/Moscow"),
        explanation={"included_point_ids": ["point-a", "point-b", "point-c"],
                     "formula": "A + B + C"},
    )
    d = result.to_dict()
    assert d["value"] is None and d["known_value"] == 90.0
    assert d["missing_ids"] == ["point-c"]
    assert d["period"]["timezone"] == "Europe/Moscow"
    print("[OK] форма envelope совпадает с примером §9.2 ТЗ")


def test_availability_complete_requires_value():
    try:
        MetricResult(
            metric="energy_import", unit="kWh", mode=MODE_SUM,
            value=None, known_value=None,
            availability=AVAILABILITY_COMPLETE,
            expected_count=3, valid_count=0)
    except ContractViolation:
        print("[OK] availability=complete без value отклонён контрактом")
    else:
        raise AssertionError("ожидалось ContractViolation")


if __name__ == "__main__":
    test_zero_consumption_is_not_missing()
    test_all_missing_is_null_not_zero()
    test_known_value_never_set_without_valid_parts()
    test_partial_sum_example_a12()
    test_full_sum_gives_value()
    test_value_number_requires_full_coverage()
    test_percentage_a11()
    test_negative_imbalance_keeps_sign_a08()
    test_example_json_shape_matches_tz_9_2()
    test_availability_complete_requires_value()
    print("\nВсе тесты контракта качества расчёта пройдены.")

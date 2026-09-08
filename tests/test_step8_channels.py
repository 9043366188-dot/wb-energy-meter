"""Тесты Шага 8: словарь каналов (channels.py, ТЗ v0.8.0, задача 3).

Самостоятельный скрипт (не pytest):
    python tests/test_step8_channels.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wb_energy_meter.channels import CATEGORIES, CHANNEL_INFO, get_channel_info


def test_categories_consistent():
    ids = {c["id"] for c in CATEGORIES}
    assert "other" in ids, "в CATEGORIES обязана быть 'other'"
    assert "main" in ids, "в CATEGORIES обязана быть 'main'"
    for name, info in CHANNEL_INFO.items():
        assert info["category"] in ids, \
            f"{name!r}: категория {info['category']!r} отсутствует в CATEGORIES"
        assert info["label"], f"{name!r}: пустой label"
    print("[OK] все category из CHANNEL_INFO есть в CATEGORIES, label не пустые")


def test_unknown_channel_falls_back_to_other():
    info = get_channel_info("Some Unknown Channel XYZ")
    assert info["category"] == "other"
    assert info["label"] == "Some Unknown Channel XYZ", \
        "незнакомый канал должен отображаться под своим именем как есть"
    assert info["hint"] is None
    print("[OK] незнакомый канал не теряется, попадает в 'other' под своим именем")


def test_empty_and_none_name_do_not_crash():
    for name in (None, ""):
        info = get_channel_info(name)
        assert info["category"] == "other"
        assert info["label"] == ""
    print("[OK] пустое/None имя канала не роняет get_channel_info")


def test_known_channels_have_units_and_hint():
    for name in ("Urms L1", "Total P", "Total AP energy", "Frequency"):
        info = get_channel_info(name)
        assert info["units"], f"{name}: нет единиц измерения"
        assert info["hint"], f"{name}: нет подсказки"
        assert info["label"] != name, f"{name}: label не переведён"
    print("[OK] основные каналы имеют units, hint и переведённый label")


def test_phase_channels_generated_for_all_phases():
    for base in ("Urms", "Irms", "P", "Q", "S", "PF", "Upeak", "Ipeak",
                 "AP energy", "Phase angle", "Voltage angle"):
        for p in ("L1", "L2", "L3"):
            name = f"{base} {p}"
            assert name in CHANNEL_INFO, f"{name!r} отсутствует в словаре"
            assert p in CHANNEL_INFO[name]["label"], \
                f"{name!r}: фаза не отражена в label {CHANNEL_INFO[name]['label']!r}"
    print("[OK] пофазные каналы (L1/L2/L3) сгенерированы по шаблонам без ручного дублирования")


def test_main_channels_present_and_flagged():
    main_names = {n for n, i in CHANNEL_INFO.items() if i["main"]}
    for expected in ("Total P", "Total AP energy", "Frequency",
                     "Urms L1", "Urms L2", "Urms L3",
                     "Irms L1", "Irms L2", "Irms L3"):
        assert expected in main_names, f"{expected} должен быть main=True"
    print("[OK] основные каналы (main-панель) помечены main=True")


def test_units_meta_override_precedence_documented():
    # Сама приоритезация (meta.units важнее словаря) реализована в
    # api.py::_meter_detail — здесь только проверяем, что словарь всегда
    # отдаёт какое-то разумное значение units для известных каналов, а
    # для 'Serial'/'MCU Temperature' единицы содержательны или пусты
    # осознанно (не мусор).
    assert get_channel_info("Serial")["units"] is None
    assert get_channel_info("MCU Temperature")["units"] == "°C"
    print("[OK] единицы измерения в словаре осмысленны (Serial без units, MCU Temperature — °C)")


def test_phase_fallback_for_unlisted_variant():
    # Гипотетический канал, которого нет в словаре явно, но совпадает по
    # шаблону "<base> L2" с известным "<base> L1"/"<base> L3" — не должен
    # использоваться в проде (все 3 фазы уже сгенерированы), но фолбэк
    # обязан быть безопасным для будущих неучтённых комбинаций.
    info = get_channel_info("Nonexistent Metric L2")
    assert info["category"] == "other"
    assert info["label"] == "Nonexistent Metric L2"
    print("[OK] неизвестная пофазная метрика без шаблона уходит в 'other' как есть")


if __name__ == "__main__":
    test_categories_consistent()
    test_unknown_channel_falls_back_to_other()
    test_empty_and_none_name_do_not_crash()
    test_known_channels_have_units_and_hint()
    test_phase_channels_generated_for_all_phases()
    test_main_channels_present_and_flagged()
    test_units_meta_override_precedence_documented()
    test_phase_fallback_for_unlisted_variant()
    print("\nВсе тесты Шага 8 (словарь каналов) пройдены.")

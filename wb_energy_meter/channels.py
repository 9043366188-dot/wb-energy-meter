"""Словарь каналов WB-MAP3E: русские названия, единицы, подсказки, категории.

Единый источник правды для модалки деталей счётчика, CSV-выгрузок и
графика истории (§5 ТЗ v0.8.0). Незнакомый канал НИКОГДА не теряется —
get_channel_info() всегда возвращает словарь, для неопознанных каналов
label = имя канала как есть, category = "other". Прошивки WB-MAP3E
различаются, набор каналов плавает — это ключевое требование.
"""

from __future__ import annotations

import re
from typing import Optional

PHASES = ("L1", "L2", "L3")

CATEGORIES = [
    {"id": "main", "label": "Основные", "open": True},
    {"id": "voltage", "label": "Напряжение", "open": False},
    {"id": "current", "label": "Ток", "open": False},
    {"id": "power", "label": "Мощность", "open": False},
    {"id": "energy", "label": "Энергия", "open": False},
    {"id": "quality", "label": "Качество сети", "open": False},
    {"id": "service", "label": "Служебные", "open": False},
    {"id": "other", "label": "Прочее", "open": False},
]

# Шаблоны для пофазных каналов (L1/L2/L3) — генерируются программно,
# чтобы не дублировать по три записи руками (§5.2 ТЗ).
_PHASE_TEMPLATES = (
    # (шаблон имени, шаблон названия, единицы, шаблон подсказки, категория, main)
    ("Urms {p}", "Напряжение {p}", "В",
     "Действующее (среднеквадратичное) напряжение фазы {p} относительно "
     "нейтрали. Норма 198–253 В.", "voltage", True),
    ("Upeak {p}", "Пиковое напряжение {p}", "В",
     "Максимальное мгновенное значение напряжения за период измерения.",
     "voltage", False),
    ("Irms {p}", "Ток {p}", "А",
     "Действующий ток фазы. Считается с учётом коэффициента трансформации.",
     "current", True),
    ("Ipeak {p}", "Пиковый ток {p}", "А",
     "Максимальное мгновенное значение тока — показывает пусковые броски.",
     "current", False),
    ("P {p}", "Активная мощность {p}", "кВт",
     "Мощность, которая реально совершает работу.", "power", False),
    ("Q {p}", "Реактивная мощность {p}", "квар",
     "Мощность обмена с индуктивной и ёмкостной нагрузкой, работы не "
     "совершает.", "power", False),
    ("S {p}", "Полная мощность {p}", "кВА",
     "Геометрическая сумма активной и реактивной мощности.", "power", False),
    ("PF {p}", "Коэффициент мощности {p}", "—",
     "cos φ: отношение активной мощности к полной. Ниже 0,9 — плохо.",
     "power", False),
    ("AP energy {p}", "Активная энергия {p}", "кВт·ч",
     "Пофазный нарастающий счётчик активной энергии.", "energy", False),
    ("Phase angle {p}", "Угол фазы {p}", "°",
     "Сдвиг между током и напряжением фазы.", "quality", False),
    ("Voltage angle {p}", "Угол напряжения {p}", "°",
     "Взаимный сдвиг фазных напряжений, в норме около 120°.",
     "quality", False),
)

# Каналы без разбивки по фазам.
_FLAT_CHANNELS = {
    "Total P": ("Активная мощность, сумма", "кВт",
        "Суммарная активная мощность по трём фазам. Основной показатель "
        "нагрузки.", "power", True),
    "Total Q": ("Реактивная мощность, сумма", "квар",
        "Суммарная реактивная мощность. Высокие значения — повод для "
        "компенсации.", "power", False),
    "Total S": ("Полная мощность, сумма", "кВА",
        "По ней выбирают сечение кабеля и номинал трансформатора.",
        "power", False),
    "Total PF": ("Коэффициент мощности, сумма", "—",
        "Общий cos φ по объекту.", "power", False),
    "Frequency": ("Частота сети", "Гц",
        "Норма 50 ± 0,2 Гц. Отклонения — признак проблем в питающей сети.",
        "quality", True),
    "Total AP energy": ("Активная энергия, потреблено", "кВт·ч",
        "Нарастающий счётчик потреблённой активной энергии. Основа для "
        "расчёта расхода.", "energy", True),
    "Total AN energy": ("Активная энергия, отдано", "кВт·ч",
        "Нарастающий счётчик отданной в сеть энергии (генерация, "
        "рекуперация).", "energy", False),
    "Total RP energy": ("Реактивная энергия, потреблено", "квар·ч",
        "Нарастающий счётчик потреблённой реактивной энергии.",
        "energy", False),
    "Total RN energy": ("Реактивная энергия, отдано", "квар·ч",
        "Нарастающий счётчик отданной реактивной энергии.",
        "energy", False),
    "Serial": ("Серийный номер", None,
        "Заводской номер счётчика, используется для сверки с актом "
        "установки.", "service", False),
    "Uptime": ("Время работы", "с",
        "Сколько счётчик работает без перезагрузки.", "service", False),
    "Uptime (s)": ("Время работы", "с",
        "Сколько счётчик работает без перезагрузки.", "service", False),
    "MCU Temperature": ("Температура контроллера", "°C",
        "Температура процессора счётчика.", "service", False),
    "MCU Voltage": ("Напряжение питания МК", "В",
        "Внутреннее напряжение питания счётчика.", "service", False),
}


def _build_channel_info() -> dict:
    info: dict = {}
    for name_t, label_t, units, hint_t, category, main in _PHASE_TEMPLATES:
        for p in PHASES:
            info[name_t.format(p=p)] = {
                "label": label_t.format(p=p),
                "units": units,
                "hint": hint_t.format(p=p),
                "category": category,
                "main": main,
            }
    for name, (label, units, hint, category, main) in _FLAT_CHANNELS.items():
        info[name] = {
            "label": label, "units": units, "hint": hint,
            "category": category, "main": main,
        }
    return info


CHANNEL_INFO: dict = _build_channel_info()

_PHASE_SUFFIX_RE = re.compile(r"^(?P<base>.+) (?P<phase>L[1-3])$")


def get_channel_info(name: Optional[str]) -> dict:
    """Вернуть {label, units, hint, category, main} для канала.

    Точное совпадение по имени — приоритет. Если его нет, но имя похоже
    на пофазный канал ("<base> L1/L2/L3"), пробуем найти шаблон по
    другим фазам того же канала. Если ничего не подошло — канал не
    теряется: label = имя как есть, category = "other", hint = None.
    """
    name = name or ""
    info = CHANNEL_INFO.get(name)
    if info is not None:
        return dict(info)
    m = _PHASE_SUFFIX_RE.match(name)
    if m:
        base, phase = m.group("base"), m.group("phase")
        for other_phase in PHASES:
            alt = CHANNEL_INFO.get(f"{base} {other_phase}")
            if alt is not None:
                out = dict(alt)
                out["label"] = re.sub(r"L[1-3]$", phase, out["label"])
                if not out["label"].endswith(phase):
                    out["label"] = f"{out['label']} {phase}"
                return out
    return {"label": name, "units": None, "hint": None,
            "category": "other", "main": False}

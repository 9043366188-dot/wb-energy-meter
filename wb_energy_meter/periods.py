"""Стандартные периоды (локальная timezone)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time as dt_time, timedelta
from typing import Optional


PERIOD_PRESETS = (
    "today", "yesterday", "this_month", "last_month",
    "last_24h", "last_7d", "last_30d",
)


@dataclass
class Period:
    ts_from: float
    ts_to: float
    label: str
    description: str

    def to_dict(self):
        return {
            "label": self.label,
            "description": self.description,
            "ts_from": self.ts_from,
            "ts_to": self.ts_to,
            "from": _fmt_dt(self.ts_from),
            "to": _fmt_dt(self.ts_to),
            "duration_s": self.ts_to - self.ts_from,
        }


def _fmt_dt(epoch):
    return datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M:%S")


def _start_of_day(d): return datetime.combine(d, dt_time(0, 0, 0))


def _start_of_month(year, month): return datetime(year, month, 1)


def _to_epoch(dt): return dt.timestamp()


def build_period(preset=None, *, ts_from=None, ts_to=None, now=None):
    if now is None:
        now = datetime.now()
    if preset and preset != "custom":
        return _preset_period(preset, now)
    if ts_from is None or ts_to is None:
        raise ValueError("Для произвольного периода нужны ts_from и ts_to")
    if ts_to <= ts_from:
        raise ValueError("ts_to должен быть больше ts_from")
    return Period(
        ts_from=float(ts_from), ts_to=float(ts_to),
        label="custom",
        description=f"С {_fmt_dt(ts_from)} по {_fmt_dt(ts_to)}",
    )


def _preset_period(preset, now):
    today = now.date()

    if preset == "today":
        start = _start_of_day(today)
        return Period(ts_from=_to_epoch(start), ts_to=_to_epoch(now),
                      label="today",
                      description=f"Сегодня (с {start.strftime('%H:%M')})")

    if preset == "yesterday":
        yesterday = today - timedelta(days=1)
        start = _start_of_day(yesterday)
        end = _start_of_day(today)
        return Period(ts_from=_to_epoch(start), ts_to=_to_epoch(end),
                      label="yesterday",
                      description=f"Вчера ({yesterday.strftime('%d.%m.%Y')})")

    if preset == "this_month":
        start = _start_of_month(today.year, today.month)
        return Period(ts_from=_to_epoch(start), ts_to=_to_epoch(now),
                      label="this_month",
                      description=f"Текущий месяц "
                                  f"({_ru_month_name(today.month)} {today.year})")

    if preset == "last_month":
        end = _start_of_month(today.year, today.month)
        if today.month == 1:
            prev_year, prev_month = today.year - 1, 12
        else:
            prev_year, prev_month = today.year, today.month - 1
        start = _start_of_month(prev_year, prev_month)
        return Period(ts_from=_to_epoch(start), ts_to=_to_epoch(end),
                      label="last_month",
                      description=f"Прошлый месяц "
                                  f"({_ru_month_name(prev_month)} {prev_year})")

    if preset == "last_24h":
        end = now; start = end - timedelta(hours=24)
        return Period(ts_from=_to_epoch(start), ts_to=_to_epoch(end),
                      label="last_24h", description="Последние 24 часа")

    if preset == "last_7d":
        end = now; start = end - timedelta(days=7)
        return Period(ts_from=_to_epoch(start), ts_to=_to_epoch(end),
                      label="last_7d", description="Последние 7 дней")

    if preset == "last_30d":
        end = now; start = end - timedelta(days=30)
        return Period(ts_from=_to_epoch(start), ts_to=_to_epoch(end),
                      label="last_30d", description="Последние 30 дней")

    raise ValueError(f"Неизвестный пресет: {preset!r}. "
                     f"Допустимые: {PERIOD_PRESETS}")


_RU_MONTHS = (
    "январь", "февраль", "март", "апрель", "май", "июнь",
    "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь",
)


def _ru_month_name(m):
    if 1 <= m <= 12: return _RU_MONTHS[m - 1]
    return str(m)


def parse_user_datetime(s):
    s = s.strip()
    if not s: raise ValueError("Пустая строка даты")
    for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M",
                "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            dt = datetime.strptime(s, fmt)
            return dt.timestamp()
        except ValueError:
            continue
    raise ValueError(f"Не распарсил дату {s!r}. Формат: YYYY-MM-DD или "
                     f"'YYYY-MM-DD HH:MM'")

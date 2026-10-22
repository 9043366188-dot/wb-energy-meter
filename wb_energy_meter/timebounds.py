"""Границы периода и подписи времени в часовом поясе запроса (партия 15, A44).

Находка партии 12: даты вида «ГГГГ-ММ-ДД» в запросах расчётов разбирались в часовом поясе
ПРОЦЕССА службы (`datetime.timestamp()` от наивной даты), а поле `timezone` запроса только
возвращалось подписью. Интерфейс присылает `ГГГГ-ММ-ДД` из поля даты и `timezone` браузера,
то есть оператор имеет в виду сутки в СВОЁМ поясе; на контроллере с другим поясом процесса
границы сдвигались, а в зоне с переходом на летнее время сутки 23 и 25 часов считались по
чужим часам.

Решение (выбрано вместо «даты не принимаются, границы строит клиент»): сервер сам строит
границы по `timezone` запроса. Причины: интерфейс уже шлёт даты, а не unix-секунды (иначе
пришлось бы переписывать все экраны и CSV); оператор не обязан считать переходы часов; одно
правило для интерфейса и для скриптов — «строка-дата читается в поясе `timezone`». Unix-секунды
по-прежнему принимаются и означают момент времени независимо от пояса.

Правила разбора строки-даты в поясе `timezone` (имя IANA, по умолчанию `UTC`):

* `ГГГГ-ММ-ДД` — полночь этой даты в поясе; `ГГГГ-ММ-ДД ЧЧ:ММ[:СС]` (пробел или `T`) —
  местное время в поясе;
* несуществующее местное время (пропуск при переходе на летнее время, например 02:30
  29.03.2026 в Europe/Berlin) читается по смещению ДО перехода; неоднозначное (повтор часа при
  переходе на зимнее время) — по первому вхождению, то есть до перехода;
* неизвестное имя пояса при строке-дате — ошибка (`ValueError` → `400`, поле `timezone`);
  при unix-секундах пояс остаётся подписью и не проверяется, как раньше.

Подписи периода в ответах: `period.from_local` / `period.to_local` — ISO 8601 со смещением в
поясе отчёта (`2026-03-29T00:00:00+01:00`); у суток перехода часов видно, что `to_local` уже в
другом смещении. Это дополнение; `from`/`to` остаются как были.

CLI и классические маршруты `/api/consumption…` по-прежнему читают даты в поясе процесса
службы (`periods.parse_user_datetime`): это локальные инструменты оператора на контроллере.
"""

from __future__ import annotations

from datetime import datetime, timezone as _tz
from typing import Optional

DATE_FORMATS = ("%Y-%m-%d", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M",
                "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S")
DEFAULT_ZONE = "UTC"


class UnknownTimezone(ValueError):
    """Имя часового пояса не распознано (нужно имя IANA, например Europe/Moscow)."""


def resolve_zone(name):
    """`tzinfo` по имени IANA. Пустое имя — UTC. Неизвестное — `UnknownTimezone`."""
    if name is None or name == "":
        name = DEFAULT_ZONE
    if not isinstance(name, str):
        raise UnknownTimezone("Часовой пояс должен быть строкой с именем IANA, например Europe/Moscow")
    if name == "UTC":
        return _tz.utc          # без базы часовых поясов на устройстве
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:  # noqa: BLE001 — нет имени в базе, нет самой базы, недопустимый ключ
        raise UnknownTimezone(
            "Неизвестный часовой пояс %r: нужно имя IANA, например Europe/Moscow или UTC" % (name,))


def parse_boundary(text, tz_name=None) -> int:
    """Строка-дата → unix-секунды границы в поясе `tz_name` (см. правила в докстринге модуля)."""
    s = (text or "").strip()
    if not s:
        raise ValueError("Пустая строка даты")
    tz = resolve_zone(tz_name)
    for fmt in DATE_FORMATS:
        try:
            naive = datetime.strptime(s, fmt)
        except ValueError:
            continue
        return int(naive.replace(tzinfo=tz).timestamp())    # fold=0: см. правила
    raise ValueError("Не распарсил дату %r. Формат: ГГГГ-ММ-ДД или 'ГГГГ-ММ-ДД ЧЧ:ММ'" % (s,))


def local_iso(ts, tz_name=None) -> Optional[str]:
    """Момент `ts` (unix-секунды, число или строка) как ISO 8601 со смещением в поясе отчёта.
    `None`, если `ts` не число. Неизвестный пояс — UTC (подпись не должна ронять ответ)."""
    try:
        sec = int(float(ts))
    except (TypeError, ValueError, OverflowError):
        return None
    try:
        tz = resolve_zone(tz_name)
    except UnknownTimezone:
        tz = _tz.utc
    try:
        return datetime.fromtimestamp(sec, tz).isoformat()
    except (OverflowError, OSError, ValueError):
        return None

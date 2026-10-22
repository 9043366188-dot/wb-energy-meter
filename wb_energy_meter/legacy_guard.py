"""Страж старых записывающих маршрутов и команд CLI (партия 15, этап 15.2; решения R2 и «CLI»).

После поколения домена 2 (`domain_model_generation >= 2`, см. `domain_generation.py`) учёт идёт
по новой модели (`/api/v2`: точки учёта, схема, учётные группы). Старый реестр — таблицы `meters`
и `meter_groups` — остаётся источником для чтения и для приборов, привязанных к точкам, но писать
в него «мимо» новой модели нельзя: правка имени, группы или роли через старый маршрут молча
расходится со схемой (имя точки, состав учётной группы), а удаление счётчика рвёт привязки.

Что делает страж:

* **Маршруты.** Любая запись (`POST`/`PUT`/`PATCH`/`DELETE`) под `/api/registry/` после поколения 2
  отвечает `409` с `code="moved_to_v2"`, русским сообщением и подсказкой нового маршрута; до
  обработчика дело не доходит — в БД ничего не меняется. Чтение (`GET /api/registry/*`) работает
  как раньше: ещё один переходный цикл (§9.3 плана), в `docs/api-v2.md` помечено устаревшим.
  Правило общее: новый записывающий маршрут под `/api/registry/` закрыт по умолчанию.
* **CLI.** Пишущие команды (`meter add/rename/group/role/enable/disable/remove`, `group remove`,
  `scan --add-all`) завершаются с кодом `EXIT_MOVED_TO_V2` (3) и подсказкой, что делать в
  интерфейсе v2. Диагностические (`meter list/show`, `group list`, `scan` без `--add-all`, `db`,
  `config`, `consumption`, `history`, `aggregates`) работают как раньше.
* **До поколения 2** (свежая или ещё не перенесённая БД) поведение не меняется ни в чём.
"""

from __future__ import annotations

import re
from typing import Optional, Tuple

from .domain_generation import CURRENT_GENERATION, get_domain_generation
from .repo import KvRepo

CODE = "moved_to_v2"
LEGACY_PREFIX = "/api/registry/"
WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
# Код выхода CLI: отличается от 1 («не найдено / отменено»), 2 («ошибка») и 130 (Ctrl+C).
EXIT_MOVED_TO_V2 = 3

# (метод, шаблон пути, что делать в v2, маршрут v2) — самое точное совпадение первым.
_ROUTE_HINTS = (
    ("POST", r"/api/registry/meters",
     "Счётчики заводятся в интерфейсе v2: «Структура» или «План v3» → «+ Счётчик».",
     "POST /api/v2/points, затем POST /api/v2/points/{id}/bindings (new_meter)"),
    ("PATCH", r"/api/registry/meters/[^/]+/role",
     "Роль счётчика задаётся его местом в схеме: ввод щита или отходящая линия.",
     "PATCH /api/v2/topology/edges/{id}"),
    ("PATCH", r"/api/registry/meters/[^/]+",
     "Описание точки меняется в «Структуре» (карточка точки), состав учётной группы — там же.",
     "PATCH /api/v2/points/{id}; POST /api/v2/groups/{id}/members"),
    ("DELETE", r"/api/registry/meters/[^/]+",
     "Счётчик не удаляется: точку архивируют в «Структуре» (история остаётся).",
     "PATCH /api/v2/points/{id} с archived=true"),
    ("POST", r"/api/registry/groups",
     "Учётные группы создаются в интерфейсе v2: «План v3» → «＋ Зона» → «＋ Новая группа».",
     "POST /api/v2/groups"),
    ("PATCH", r"/api/registry/groups/[^/]+",
     "Состав учётной группы меняется в карточке точки («Структура»); переименование и "
     "объединение групп в v2 не реализованы.",
     "POST /api/v2/groups/{id}/members; DELETE /api/v2/groups/{id}/members/{point_id}"),
    ("DELETE", r"/api/registry/groups/[^/]+",
     "Учётные группы в v2 не удаляются (история составов сохраняется); точку можно вывести "
     "из группы в карточке точки.",
     "DELETE /api/v2/groups/{id}/members/{point_id}"),
)
_COMPILED = tuple((m, re.compile(p + r"/?$"), text, route) for m, p, text, route in _ROUTE_HINTS)

_GENERIC_ROUTE_TEXT = "Запись через старый реестр закрыта: используйте интерфейс v2."
_GENERIC_ROUTE = "/api/v2/…"


def writes_closed(db) -> bool:
    """Запись в старый реестр закрыта: БД объявила поколение домена 2 или выше. Нет БД или не
    удалось прочитать поколение — не закрываем (обработчик всё равно ответит своей ошибкой)."""
    if db is None:
        return False
    try:
        return get_domain_generation(KvRepo(db)) >= CURRENT_GENERATION
    except Exception:
        return False


def is_legacy_write(method: str, path: str) -> bool:
    return method.upper() in WRITE_METHODS and path.startswith(LEGACY_PREFIX)


def route_hint(method: str, path: str) -> Tuple[str, str]:
    """(что делать в v2, маршрут v2) для старого записывающего маршрута."""
    for m, rx, text, route in _COMPILED:
        if m == method.upper() and rx.match(path):
            return text, route
    return _GENERIC_ROUTE_TEXT, _GENERIC_ROUTE


def route_response_body(method: str, path: str) -> dict:
    """Тело ответа 409: `code` и `message` — как в конверте ошибок v2; `error` — для старых
    клиентов и классического интерфейса, которые показывают именно это поле."""
    text, route = route_hint(method, path)
    message = ("Запись через старый маршрут %s закрыта: данные ведутся в новой модели учёта. %s "
               "Маршрут v2: %s." % (path, text, route))
    return {"error": message, "code": CODE, "message": message,
            "replacement": route, "fields": []}


# ---- CLI ----

# (команда, подкоманда) пишущих команд; `scan` — только с --add-all (см. is_cli_write).
_CLI_WRITES = {
    ("meter", "add"), ("meter", "rename"), ("meter", "group"), ("meter", "role"),
    ("meter", "enable"), ("meter", "disable"), ("meter", "remove"),
    ("group", "remove"),
}

_CLI_HINTS = {
    ("meter", "add"): "Добавьте счётчик в интерфейсе v2: «Структура» или «План v3» → «+ Счётчик».",
    ("meter", "rename"): "Имя меняется в «Структуре» (карточка точки или элемента схемы).",
    ("meter", "group"): "Состав учётной группы меняется в карточке точки («Структура»).",
    ("meter", "role"): "Роль задаётся местом счётчика в схеме: ввод щита или отходящая линия.",
    ("meter", "enable"): "Включение и отключение счётчика в v2 нет: точку архивируют в «Структуре».",
    ("meter", "disable"): "Включение и отключение счётчика в v2 нет: точку архивируют в «Структуре».",
    ("meter", "remove"): "Счётчик не удаляется: архивируйте точку в «Структуре» (история остаётся).",
    ("group", "remove"): "Учётные группы в v2 не удаляются: выведите точки из группы в карточке точки.",
    ("scan", "--add-all"): "Новые приборы ставятся в интерфейсе v2: «Структура» → «Приборы без точки "
                           "учёта» → «+ Счётчик → куда?». Просмотр без --add-all работает как раньше.",
}


def cli_write_key(args) -> Optional[Tuple[str, str]]:
    """Ключ пишущей команды CLI или None, если команда диагностическая."""
    cmd = getattr(args, "cmd", None)
    sub = getattr(args, "subcmd", None)
    if cmd == "scan":
        return ("scan", "--add-all") if getattr(args, "add_all", False) else None
    if (cmd, sub) in _CLI_WRITES:
        return (cmd, sub)
    return None


def cli_message(key: Tuple[str, str]) -> str:
    name = key[0] if key[0] == "scan" else "%s %s" % key
    shown = "scan --add-all" if key == ("scan", "--add-all") else name
    return ("Команда «%s» закрыта: база уже ведётся в новой модели учёта (поколение домена 2), "
            "запись в старый реестр мимо неё расходится со схемой. Что делать в интерфейсе v2: %s"
            % (shown, _CLI_HINTS.get(key, "используйте интерфейс v2.")))

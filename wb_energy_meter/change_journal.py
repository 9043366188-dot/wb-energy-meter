"""Журнал изменений `change_log` — партия 12, этап 12.1
(docs/design-history.md, решение R7: история топологии из журнала, без новой
миграции).

Как это работает. При `Database.open()` (после миграций) на соединении-
писателе создаются ВРЕМЕННЫЕ (`TEMP`) теневые таблицы и триггеры
`AFTER INSERT/UPDATE/DELETE` на предметных таблицах (`JOURNALED`). Триггер
складывает старую и новую строку в теневую таблицу — обычным SQL, без JSON1
и пользовательских функций, поэтому работает на SQLite/Python контроллера
(Debian bullseye, Python 3.9). Внешний `Database.transaction()` перед
`COMMIT` вызывает `ChangeCapture.drain()`: теневые строки схлопываются по
строке таблицы и переносятся в `change_log` В ТОЙ ЖЕ транзакции. Откат
транзакции откатывает и журнал, и временные таблицы.

Почему на уровне соединения, а не в каждом репозитории: ни один путь записи
(мастер переноса, `migrate-legacy`, старые маршруты, CLI) не может обойти
журнал — он исчерпывающ по построению, и его не нужно сверять со списком
операций при каждой новой правке.

«Кто». Аутентификации нет, пользователя не выдумываем: в `reason` пишется
JSON `{"op", "origin", "client"}` — операция (`POST /api/v2/...`),
происхождение (`web`/`wizard`/`migration`/`cli`/`system`) и адрес клиента.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Any, Dict, Iterable, List, Optional, Tuple

log = logging.getLogger(__name__)

# Ключ kv: момент первого открытия БД кодом с журналом. Раньше него истории
# топологии нет — расчёт «как было» помечается assumed_legacy (A23).
STARTED_KEY = "change_log_started_at"

# таблица -> (entity_type, ключевые поля, шумовые поля)
#   ключевые поля — попадают в old/new любой записи update (по ним
#   восстанавливается структура на момент T, topology_history.py);
#   шумовые — их изменение само по себе записи не создаёт.
_NOISE_COMMON = frozenset({"updated_at"})

JOURNALED: Dict[str, Tuple[str, Tuple[str, ...], frozenset]] = {
    "metering_points": ("metering_point", (), _NOISE_COMMON),
    "point_state_versions": ("point_state", (), _NOISE_COMMON),
    "point_bindings": ("point_binding", (), _NOISE_COMMON),
    "meter_sources": ("meter_source", (), _NOISE_COMMON),
    "point_locations": ("point_location", (), _NOISE_COMMON),
    "point_served_locations": ("point_served_location", (), _NOISE_COMMON),
    "locations": ("location", (), _NOISE_COMMON),
    "location_parent_bindings": ("location_parent", (), _NOISE_COMMON),
    "meter_groups": ("group", (), _NOISE_COMMON),
    "group_parent_bindings": ("group_parent", (), _NOISE_COMMON),
    "group_memberships": ("group_member", (), _NOISE_COMMON),
    "electrical_nodes": ("electrical_node", ("kind",), _NOISE_COMMON),
    "electrical_edges": (
        "electrical_edge",
        ("primary_point_id", "from_node_id", "to_node_id", "state",
         "valid_from", "valid_to"),
        _NOISE_COMMON),
    "balance_scopes": ("balance_scope", (), _NOISE_COMMON),
    "balance_members": ("balance_member", (), _NOISE_COMMON),
    # canvas_revision растёт при каждом сохранении раскладки — само сохранение
    # пишется явной записью plan_layout (record()), а не строкой плана
    "site_plans": ("plan", (), _NOISE_COMMON | {"canvas_revision"}),
}
REVISIONS_TABLE = "configuration_revisions"

# тип сущности -> таблица (обратный индекс для API и topology_history)
ENTITY_TABLE = {v[0]: k for k, v in JOURNALED.items()}
EXPLICIT_ENTITY_TYPES = ("plan_layout",)

# Связанные записи для истории «карточки» (GET /change-log?related=1):
# (entity_type -> [(entity_type подчинённой сущности, поле со ссылкой)]).
RELATED = {
    "metering_point": [
        ("point_binding", "point_id"), ("point_state", "point_id"),
        ("point_location", "point_id"), ("point_served_location", "point_id"),
        ("group_member", "point_id"), ("electrical_edge", "primary_point_id"),
        ("balance_member", "point_id"),
    ],
    "electrical_node": [
        ("electrical_edge", "from_node_id"), ("electrical_edge", "to_node_id"),
    ],
    "group": [("group_member", "group_id"), ("group_parent", "group_id")],
    "location": [("location_parent", "location_id"),
                 ("point_location", "location_id"),
                 ("point_served_location", "location_id")],
    "plan": [("plan_layout", None)],
}

_clock = time.time


def now() -> int:
    """Время записи журнала. Отдельная функция, чтобы тесты могли подменить
    часы (`change_journal._clock`) и получить предсказуемые `recorded_at`."""
    return int(_clock())


# ---------------------------------------------------------------------
# Контекст операции (кто/откуда)
# ---------------------------------------------------------------------

_tls = threading.local()
_default_origin = "system"


def set_default_origin(origin: str) -> None:
    """Происхождение по умолчанию для процесса (CLI ставит `cli`)."""
    global _default_origin
    _default_origin = origin


def set_context(op: Optional[str] = None, origin: Optional[str] = None,
                client: Optional[str] = None) -> None:
    _tls.ctx = {"op": op, "origin": origin, "client": client}


def clear_context() -> None:
    _tls.ctx = None


@contextmanager
def context(op: Optional[str] = None, origin: Optional[str] = None,
            client: Optional[str] = None):
    previous = getattr(_tls, "ctx", None)
    set_context(op, origin, client)
    try:
        yield
    finally:
        _tls.ctx = previous


def current_context() -> Dict[str, Optional[str]]:
    ctx = getattr(_tls, "ctx", None) or {}
    return {
        "op": ctx.get("op"),
        "origin": ctx.get("origin") or _default_origin,
        "client": ctx.get("client"),
    }


def origin_for_path(path: str) -> str:
    """Происхождение операции по адресу маршрута."""
    if "/admin/migrate-legacy" in path:
        return "migration"
    if "/migration/legacy-links" in path:
        return "wizard"
    if not path.startswith("/api/v2/"):
        return "legacy-api"                 # старые /api/registry/*, /api/plans/*
    return "web"


def _dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


# ---------------------------------------------------------------------
# Установка перехвата
# ---------------------------------------------------------------------

class ChangeCapture:
    """Перехват изменений на ОДНОМ соединении-писателе."""

    def __init__(self, tables: Dict[str, List[str]], has_revisions: bool):
        self._tables = tables          # таблица -> список колонок
        self._has_revisions = has_revisions

    @property
    def tables(self) -> Dict[str, List[str]]:
        return dict(self._tables)

    def drain(self, conn) -> int:
        """Перенести накопленное в `change_log` (вызывается внутри
        транзакции, перед COMMIT). Возвращает число записей журнала."""
        entries, revision_id = self._collect(conn)
        self._clear(conn)
        if not entries:
            return 0
        recorded_at = now()
        ctx = current_context()
        reason = _dumps({k: ctx[k] for k in ("op", "origin", "client")
                         if ctx.get(k) is not None})
        rows = []
        for e in entries:
            eff = e.get("effective_from")
            rows.append((
                e["entity_type"], e["entity_id"], e["action"],
                _dumps(e["old"]) if e["old"] is not None else None,
                _dumps(e["new"]) if e["new"] is not None else None,
                reason, revision_id,
                int(eff) if eff is not None else recorded_at, recorded_at))
        conn.executemany(
            "INSERT INTO change_log (entity_type, entity_id, action, old_value, "
            "new_value, reason, revision_id, effective_from, recorded_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)", rows)
        return len(rows)

    # -- внутреннее ----------------------------------------------------

    def _clear(self, conn) -> None:
        for table in self._tables:
            conn.execute(f"DELETE FROM temp._cl_{table}")
        if self._has_revisions:
            conn.execute(f"DELETE FROM temp._cl_{REVISIONS_TABLE}")
        conn.execute("DELETE FROM temp._cl_explicit")

    def _collect(self, conn):
        revision_id = None
        if self._has_revisions:
            row = conn.execute(
                f"SELECT n_id FROM temp._cl_{REVISIONS_TABLE} "
                f"ORDER BY seq DESC LIMIT 1").fetchone()
            if row is not None:
                revision_id = row["n_id"]
        entries: List[dict] = []
        for table, cols in self._tables.items():
            etype, keys, noise = JOURNALED[table]
            rows = conn.execute(
                f"SELECT * FROM temp._cl_{table} ORDER BY seq").fetchall()
            if not rows:
                continue
            per_pk: Dict[Any, dict] = {}
            order: List[Any] = []
            for r in rows:
                op = r["op"]
                pk = r["n_id"] if op != "D" else r["o_id"]
                st = per_pk.get(pk)
                old = {c: r["o_" + c] for c in cols} if op != "I" else None
                new = {c: r["n_" + c] for c in cols} if op != "D" else None
                if st is None:
                    st = {"existed": op != "I", "old": old, "new": new,
                          "exists": op != "D"}
                    per_pk[pk] = st
                    order.append(pk)
                else:
                    st["new"] = new
                    st["exists"] = op != "D"
            for pk in order:
                st = per_pk[pk]
                entry = self._entry(table, etype, keys, noise, pk, st)
                if entry is not None:
                    entries.append(entry)
        for r in conn.execute(
                "SELECT * FROM temp._cl_explicit ORDER BY seq").fetchall():
            entries.append({
                "entity_type": r["entity_type"], "entity_id": r["entity_id"],
                "action": r["action"],
                "old": json.loads(r["old_value"]) if r["old_value"] else None,
                "new": json.loads(r["new_value"]) if r["new_value"] else None,
                "effective_from": r["effective_from"],
            })
        return entries, revision_id

    @staticmethod
    def _entry(table, etype, keys, noise, pk, st) -> Optional[dict]:
        existed, exists = st["existed"], st["exists"]
        old, new = st["old"], st["new"]
        if not existed and not exists:
            return None                      # создано и удалено в одной транзакции
        if not existed:
            eff = new.get("valid_from")
            if eff is None:
                eff = new.get("created_at")
            return {"entity_type": etype, "entity_id": pk, "action": "create",
                    "old": None, "new": new, "effective_from": eff}
        if not exists:
            return {"entity_type": etype, "entity_id": pk, "action": "delete",
                    "old": old, "new": None, "effective_from": None}
        changed = [c for c in new if c not in noise and old.get(c) != new.get(c)]
        if not changed:
            return None
        action, eff = "update", None
        if "archived_at" in changed:
            if old.get("archived_at") is None:
                action, eff = "archive", new.get("archived_at")
            else:
                action = "restore"
        elif "valid_to" in changed and old.get("valid_to") is None:
            action, eff = "close", new.get("valid_to")
        elif (table == "electrical_edges" and "state" in changed
                and new.get("state") == "published"):
            action, eff = "publish", new.get("valid_from")
        fields = list(changed) + [k for k in keys if k not in changed]
        return {"entity_type": etype, "entity_id": pk, "action": action,
                "old": {c: old[c] for c in fields},
                "new": {c: new[c] for c in fields}, "effective_from": eff}


def install(conn) -> Optional[ChangeCapture]:
    """Создать временные таблицы и триггеры на соединении-писателе. Вызывать
    ПОСЛЕ миграций. Возвращает None, если схема ещё без журнала (< 005)."""
    def _exists(name):
        return conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?",
            (name,)).fetchone() is not None

    if not _exists("change_log"):
        return None
    tables: Dict[str, List[str]] = {}
    for table in JOURNALED:
        if not _exists(table):
            continue
        cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({table})")]
        if "id" not in cols:
            continue
        tables[table] = cols
        _create_capture(conn, table, cols)
    has_revisions = _exists(REVISIONS_TABLE)
    if has_revisions:
        cols = [r["name"] for r in conn.execute(
            f"PRAGMA table_info({REVISIONS_TABLE})")]
        _create_capture(conn, REVISIONS_TABLE, cols, insert_only=True)
    conn.execute("DROP TABLE IF EXISTS temp._cl_explicit")
    conn.execute(
        "CREATE TEMP TABLE _cl_explicit (seq INTEGER PRIMARY KEY AUTOINCREMENT, "
        "entity_type TEXT NOT NULL, entity_id INTEGER NOT NULL, "
        "action TEXT NOT NULL, old_value TEXT, new_value TEXT, "
        "effective_from INTEGER)")
    _ensure_started_marker(conn)
    return ChangeCapture(tables, has_revisions)


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _create_capture(conn, table, cols, insert_only=False) -> None:
    shadow = f"_cl_{table}"
    conn.execute(f"DROP TABLE IF EXISTS temp.{shadow}")
    old_cols = ", ".join(_q("o_" + c) for c in cols)
    new_cols = ", ".join(_q("n_" + c) for c in cols)
    conn.execute(
        f"CREATE TEMP TABLE {shadow} (seq INTEGER PRIMARY KEY AUTOINCREMENT, "
        f"op TEXT NOT NULL, {old_cols}, {new_cols})")
    new_vals = ", ".join("NEW." + _q(c) for c in cols)
    old_vals = ", ".join("OLD." + _q(c) for c in cols)
    for suffix in ("i", "u", "d"):
        conn.execute(f"DROP TRIGGER IF EXISTS temp._clt_{table}_{suffix}")
    conn.execute(
        f"CREATE TEMP TRIGGER _clt_{table}_i AFTER INSERT ON main.{table} "
        f"BEGIN INSERT INTO {shadow} (op, {new_cols}) "
        f"VALUES ('I', {new_vals}); END")
    if insert_only:
        return
    conn.execute(
        f"CREATE TEMP TRIGGER _clt_{table}_u AFTER UPDATE ON main.{table} "
        f"BEGIN INSERT INTO {shadow} (op, {old_cols}, {new_cols}) "
        f"VALUES ('U', {old_vals}, {new_vals}); END")
    conn.execute(
        f"CREATE TEMP TRIGGER _clt_{table}_d AFTER DELETE ON main.{table} "
        f"BEGIN INSERT INTO {shadow} (op, {old_cols}) "
        f"VALUES ('D', {old_vals}); END")


def _ensure_started_marker(conn) -> None:
    """Метка начала журнала: ставится один раз, не перезаписывается."""
    try:
        if conn.execute("SELECT 1 FROM kv WHERE key = ?",
                        (STARTED_KEY,)).fetchone() is not None:
            return                              # уже стоит — писать незачем
        t = now()
        conn.execute(
            "INSERT OR IGNORE INTO kv (key, value, updated_at) VALUES (?,?,?)",
            (STARTED_KEY, str(t), t))
    except sqlite3.Error:
        log.exception("Не удалось поставить метку начала журнала изменений")


def record(conn, entity_type: str, entity_id: int, action: str,
           old: Optional[dict] = None, new: Optional[dict] = None,
           effective_from: Optional[int] = None) -> None:
    """Явная запись журнала (для операций, которые не сводятся к строкам
    таблицы, — раскладка плана). Вызывать внутри открытой транзакции:
    запись попадёт в `change_log` вместе с остальными при COMMIT."""
    conn.execute(
        "INSERT INTO temp._cl_explicit (entity_type, entity_id, action, "
        "old_value, new_value, effective_from) VALUES (?,?,?,?,?,?)",
        (entity_type, entity_id, action,
         _dumps(old) if old is not None else None,
         _dumps(new) if new is not None else None, effective_from))


def started_at(conn) -> Optional[int]:
    row = conn.execute("SELECT value FROM kv WHERE key = ?",
                       (STARTED_KEY,)).fetchone()
    if row is None:
        return None
    try:
        return int(json.loads(row["value"]))
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------
# Чтение
# ---------------------------------------------------------------------

MAX_LIMIT = 500
DEFAULT_LIMIT = 100


def row_to_dict(row) -> dict:
    """Строка `change_log` -> словарь ответа API."""
    meta = {}
    reason = row["reason"]
    if reason:
        try:
            parsed = json.loads(reason)
            if isinstance(parsed, dict):
                meta = parsed
        except (TypeError, ValueError):
            meta = {}

    def _j(v):
        return json.loads(v) if v else None

    return {
        "id": row["id"], "entity_type": row["entity_type"],
        "entity_id": row["entity_id"], "action": row["action"],
        "old_value": _j(row["old_value"]), "new_value": _j(row["new_value"]),
        "operation": meta.get("op"), "origin": meta.get("origin"),
        "client": meta.get("client"),
        "revision_id": row["revision_id"],
        "effective_from": row["effective_from"],
        "recorded_at": row["recorded_at"],
    }


def query(conn, *, entity_type: Optional[str] = None,
          entity_id: Optional[int] = None, action: Optional[str] = None,
          ts_from: Optional[int] = None, ts_to: Optional[int] = None,
          limit: int = DEFAULT_LIMIT, before_id: Optional[int] = None,
          related: bool = False) -> Tuple[List[dict], Optional[int]]:
    """Записи журнала, новые сверху. Возвращает (записи, курсор следующей
    страницы | None). `related` расширяет запрос записями подчинённых
    сущностей (RELATED) — для истории в карточке."""
    limit = max(1, min(int(limit or DEFAULT_LIMIT), MAX_LIMIT))
    where, params = [], []
    if action:
        where.append("action = ?")
        params.append(action)
    if ts_from is not None:
        where.append("recorded_at >= ?")
        params.append(int(ts_from))
    if ts_to is not None:
        where.append("recorded_at < ?")
        params.append(int(ts_to))
    if before_id is not None:
        where.append("id < ?")
        params.append(int(before_id))

    def _fetch(extra_where, extra_params, cap):
        sql = "SELECT * FROM change_log"
        conds = where + extra_where
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY id DESC LIMIT ?"
        return conn.execute(sql, params + extra_params + [cap]).fetchall()

    if not (related and entity_type and entity_id is not None
            and entity_type in RELATED):
        ew, ep = [], []
        if entity_type:
            ew.append("entity_type = ?")
            ep.append(entity_type)
        if entity_id is not None:
            ew.append("entity_id = ?")
            ep.append(int(entity_id))
        rows = _fetch(ew, ep, limit + 1)
        page = rows[:limit]
        nxt = page[-1]["id"] if len(rows) > limit and page else None
        return [row_to_dict(r) for r in page], nxt

    # related: сама сущность + подчинённые, у которых нужное поле == entity_id
    own = _fetch(["entity_type = ?", "entity_id = ?"],
                 [entity_type, int(entity_id)], limit + 1)
    merged = {r["id"]: r for r in own}
    needle = int(entity_id)
    for sub_type, field in RELATED[entity_type]:
        if field is None:                       # plan_layout: entity_id == plan_id
            for r in _fetch(["entity_type = ?", "entity_id = ?"],
                            [sub_type, needle], limit + 1):
                merged[r["id"]] = r
            continue
        # грубый отбор по тексту JSON, точная проверка — после разбора
        like1 = f'%"{field}":{needle},%'
        like2 = f'%"{field}":{needle}}}%'
        sql_extra = ["entity_type = ?",
                     "(old_value LIKE ? OR old_value LIKE ? "
                     "OR new_value LIKE ? OR new_value LIKE ?)"]
        for r in _fetch(sql_extra, [sub_type, like1, like2, like1, like2],
                        limit * 4 + 1):
            hit = False
            for col in ("old_value", "new_value"):
                if r[col]:
                    try:
                        if json.loads(r[col]).get(field) == needle:
                            hit = True
                    except (TypeError, ValueError):
                        pass
            if hit:
                merged[r["id"]] = r
    ordered = sorted(merged.values(), key=lambda r: r["id"], reverse=True)
    page = ordered[:limit]
    nxt = page[-1]["id"] if len(ordered) > limit and page else None
    return [row_to_dict(r) for r in page], nxt

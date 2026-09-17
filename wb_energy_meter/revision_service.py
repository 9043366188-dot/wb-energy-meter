"""Глобальный протокол ревизий конфигурации (ТЗ §6.1, §9.2, §11) —
партия 2, задача 1.

Написано лично (не делегировано) — это защита от потери правок при
параллельном редактировании, самая ответственная часть партии (см.
AGENTS.md — конвенция проекта: то, что "стоило рабочего контроллера" или
могло бы его стоить, не делегируется).

Идея (§6.1): "Каждая предметная транзакция ... создаёт одну глобальную
ревизию всего согласованного состояния." Одна общая монотонно растущая
последовательность `configuration_revisions.id` на ВСЮ конфигурацию
(точки, места, группы, сеть, границы баланса — НЕ телеметрию), а не по
одной ревизии на сущность. Клиент обязан прислать `expected_revision`
— ревизию, которую он последний раз видел; если она устарела (кто-то
уже записал более новую) или отсутствует вовсе — 409, и доменная
запись НЕ происходит вообще (ни частично, ни полностью).

Это разумно строже A35 из большого ТЗ (там пример — конфликт по одному
плану, canvas_revision, уже реализовано в plan_service_v2.py и туда не
трогается) — здесь общий счётчик специально грубее: 100 точек, до 3
одновременных клиентов (§10), и код проще и надёжнее одного счётчика,
чем N счётчиков по каждой сущности/связи.

Композиция без переписывания репозиториев: `Database.transaction()`
теперь реентрантна в пределах потока (db.py) — `with_revision_check()`
открывает ОДНУ внешнюю транзакцию, вызывает переданную функцию записи
(которая сама держит уже существующие `with self.db.transaction():` в
репозиториях — они присоединяются к внешней, а не открывают вторую), и
только при успехе доменной записи добавляет строку в
`configuration_revisions` — всё это атомарно одним BEGIN/COMMIT.
Откат (исключение доменного сервиса — ValueError/Conflict) откатывает
ВСЁ, включая несостоявшуюся ревизию.

Что сознательно НЕ делает этот модуль (см. отчёт по партии):
- не хранит и не восстанавливает состояние "как было на ревизии N" —
  `configuration_revision_id` в запросах на чтение (metrics/query)
  пока лишь помечает результат и используется как точка отсчёта для
  повторного запроса group-состава "as of" её времени создания
  (см. api_v2.py::_resolve_scope_point_ids), НЕ для электрической
  топологии (она читается всегда текущей — задокументированное
  ограничение первой поставки, электросеть меняется существенно реже
  состава групп);
- не пишет `change_log` (отдельная, более крупная задача — сейчас важна
  именно защита от потери правок, а не полный аудит "кто/что/когда").
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any, Optional, Sequence, Tuple


class RevisionConflict(ValueError):
    """§9.2: "409 — конфликт ревизии". `expected` — то, что прислал клиент
    (может быть None, если поле вовсе не передано), `actual` — реальная
    текущая глобальная ревизия на момент проверки."""

    def __init__(self, expected: Optional[int], actual: int):
        self.expected = expected
        self.actual = actual
        if expected is None:
            msg = (
                f"требуется expected_revision (текущая глобальная ревизия "
                f"конфигурации: {actual}) — без неё запись отклонена, чтобы "
                f"не затереть чужие изменения молча"
            )
        else:
            msg = (
                f"ревизия конфигурации устарела: ожидалась {expected}, сейчас "
                f"{actual} — кто-то уже сохранил более новые изменения; "
                f"перечитайте текущее состояние и повторите"
            )
        super().__init__(msg)


def current_revision_id(conn) -> int:
    """Текущая глобальная ревизия конфигурации. 0 — ни одной предметной
    транзакции ещё не было (конфигурация "с нуля", например сразу после
    миграции 005 на пустых новых таблицах)."""
    row = conn.execute(
        "SELECT MAX(id) AS m FROM configuration_revisions").fetchone()
    m = row["m"] if row is not None else None
    return int(m) if m is not None else 0


def get_revision_created_at(conn, revision_id: int) -> Optional[int]:
    """`created_at` (unix ts) конкретной ревизии — используется, чтобы
    повторить групповой состав "as of" момента её создания
    (`configuration_revision_id` в metrics/query для отчётов/Обзора).
    None, если такой ревизии нет (revision_id=0 "с нуля" либо неверный id)."""
    if not revision_id:
        return None
    row = conn.execute(
        "SELECT created_at FROM configuration_revisions WHERE id = ?",
        (revision_id,)).fetchone()
    return int(row["created_at"]) if row is not None else None


def revision_exists(conn, revision_id: int) -> bool:
    if not revision_id:
        return True  # 0 = "с нуля", всегда допустимо как базовая точка
    row = conn.execute(
        "SELECT 1 FROM configuration_revisions WHERE id = ?",
        (revision_id,)).fetchone()
    return row is not None


def create_revision(conn, schema_version: int,
                     touched: Optional[Sequence[Tuple[str, Any]]] = None) -> int:
    """Создаёт новую глобальную ревизию ВНУТРИ уже открытой транзакции
    вызывающего кода (не открывает свою — см. модульный docstring).
    `touched` — список (entity_type, entity_id) только для
    диагностики/snapshot_json, в проверку конфликта не входит."""
    now = int(time.time())
    snapshot = json.dumps(
        {"touched": [[t, i] for t, i in (touched or [])]},
        ensure_ascii=False, sort_keys=True)
    content_hash = hashlib.sha256(snapshot.encode("utf-8")).hexdigest()[:16]
    cur = conn.execute(
        "INSERT INTO configuration_revisions "
        "(snapshot_json, schema_version, content_hash, created_at) "
        "VALUES (?, ?, ?, ?)",
        (snapshot, schema_version, content_hash, now))
    return cur.lastrowid


def check_expected_revision(conn, expected_revision) -> int:
    """Сверяет присланную ревизию с текущей. Бросает RevisionConflict
    (ничего не меняя) при отсутствии поля или устаревшем значении.
    Возвращает текущую (== expected) ревизию при успехе."""
    actual = current_revision_id(conn)
    if expected_revision is None:
        raise RevisionConflict(None, actual)
    try:
        expected_int = int(expected_revision)
    except (TypeError, ValueError):
        raise RevisionConflict(expected_revision, actual)
    if expected_int != actual:
        raise RevisionConflict(expected_int, actual)
    return actual


def bump_revision(db, mutate_fn, touched=None):
    """Как with_revision_check(), но БЕЗ проверки expected_revision —
    для POST-create независимых сущностей (см. отчёт партии 2: создание
    новой точки/места/группы/узла/связи-черновика/границы баланса не
    может затереть чужую работу, поэтому предварительное условие не
    нужно), но ревизия всё равно продвигается той же атомарной
    транзакцией, чтобы последующий PATCH знал актуальный expected_revision
    (см. GET /api/v2/revision и поле configuration_revision в ответах)."""
    with db.transaction() as c:
        result = mutate_fn()
        new_revision = create_revision(c, db.current_schema_version(), touched=touched)
    return result, new_revision


def with_revision_check(db, expected_revision, mutate_fn,
                         touched: Optional[Sequence[Tuple[str, Any]]] = None):
    """§6.1/§9.2: проверка + доменная запись + создание новой ревизии —
    одна транзакция. `mutate_fn` — функция без аргументов (репозитории уже
    замкнуты на нужные ID/данные вызывающим кодом в api_v2.py); её
    собственные `with db.transaction():` присоединяются к этой внешней
    транзакции (db.py: Database.transaction() реентрантна).

    При RevisionConflict/любом исключении доменного слоя транзакция
    целиком откатывается (в т.ч. ревизия не создаётся).

    Возвращает (результат mutate_fn, id новой ревизии).
    """
    with db.transaction() as c:
        check_expected_revision(c, expected_revision)
        result = mutate_fn()
        new_revision = create_revision(c, db.current_schema_version(), touched=touched)
    return result, new_revision

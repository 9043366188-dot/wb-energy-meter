"""Протокол поколений домена (docs/migration-plan-v2.md §7, ТЗ §11.2).

Три kv-ключа (через `repo.KvRepo`, таблица `kv`: key/value/updated_at,
value — JSON):

  domain_model_generation   — поколение домена, объявленное на ЭТОЙ БД.
                               Отсутствие ключа == легаси-БД без объявленного
                               поколения == поколение 1 (LEGACY_GENERATION),
                               ровно как сказано в docs/migration-plan-v2.md §7
                               п.2: "по умолчанию 1 для БД без ключа".
  minimum_reader_generation — минимальное поколение КОДА, которому разрешено
                               работать с этой БД. Растёт вместе с
                               domain_model_generation, никогда не убывает
                               (поднимается только в mark_v2_domain_write).
  model_v2_first_write_revision — метка первой предметной/геометрической
                               записи через /api/v2 в ЭТУ БД (значение —
                               строка-ревизия/таймстамп, содержимое не
                               интерпретируется, только факт наличия). Пишется
                               В ТОЙ ЖЕ транзакции, что и сама запись — см.
                               mark_v2_domain_write(). После установки —
                               self-update.sh обязан ЗАПРЕТИТЬ откат к коду
                               поколения 1 для этой БД (см. scripts/
                               self-update.sh::check_rollback_generation, и
                               __code_generation__ в этом же файле выше).

Почему поднимать minimum_reader_generation именно в момент первой v2-записи,
а не сразу при накатывании схемы 005+: до первой записи новые таблицы просто
пустые, старый (поколения 1) код их не видит и не трогает — читать эту БД
код поколения 1 всё ещё безопасно. Ровно с первой записи в v2-домен старый
UI начинает быть НЕПОЛНЫМ (не показывает то, что видно через /api/v2) — вот
тогда откат к нему уже способен молча потерять видимость данных, и его надо
блокировать, а не раньше.
"""

from __future__ import annotations

import json
import time
from typing import Optional

LEGACY_GENERATION = 1
# Поколение 2 — этап F (docs/migration-plan-v2.md, ТЗ §11.2): первое,
# понимающее /api/v2 и доменную модель metering_point/topology/plan_items.
CURRENT_GENERATION = 2
# Поколение 3 — партия находок 0.22.3 (П9, docs/design-phase-aggregates.md):
# код понимает пофазные привязки (`phase_l1`…`phase_l3`) и считает их по
# пофазным часовым итогам. Код поколения 2 такую точку посчитал бы по итогу
# всего прибора — молча неверно, поэтому с ПЕРВОЙ пофазной привязки БД
# требует читателя поколения 3 (`mark_phase_binding`), а откат на 0.22.2
# блокируется стражем (`scripts/check-generation.sh`, `self-update.sh`).
# До первой пофазной привязки БД остаётся поколения 2 и откат разрешён.
PHASE_GENERATION = 3
# Что понимает САМ код (то же число — `__code_generation__` в `__init__.py`).
CODE_GENERATION = PHASE_GENERATION

KEY_DOMAIN_GENERATION = "domain_model_generation"
KEY_MIN_READER_GENERATION = "minimum_reader_generation"
KEY_V2_FIRST_WRITE = "model_v2_first_write_revision"


def _coerce_generation(raw) -> int:
    """kv.get() уже делает json.loads, но значение может прийти и как
    голая строка (ручная правка/старые записи) — не падаем, откатываемся
    к LEGACY_GENERATION при любой чепухе, это самое безопасное значение
    по умолчанию (никого не заблокирует лишний раз)."""
    if raw is None:
        return LEGACY_GENERATION
    try:
        return int(raw)
    except (TypeError, ValueError):
        return LEGACY_GENERATION


def get_domain_generation(kv) -> int:
    return _coerce_generation(kv.get(KEY_DOMAIN_GENERATION))


def get_min_reader_generation(kv) -> int:
    return _coerce_generation(kv.get(KEY_MIN_READER_GENERATION))


def get_v2_first_write_marker(kv) -> Optional[str]:
    v = kv.get(KEY_V2_FIRST_WRITE)
    return v if isinstance(v, str) else None


def mark_v2_domain_write(c, revision: Optional[str] = None) -> bool:
    """Отметить первую предметную/геометрическую запись через /api/v2.

    ВАЖНО: принимает уже открытый курсор транзакции (`c`, как в
    `with db.transaction() as c:`), а НЕ `db` и НЕ `KvRepo` — вызывающий
    обязан вызвать это ВНУТРИ того же `db.transaction()` блока, что и сама
    доменная запись (INSERT/UPDATE metering_point/plan_item/topology/...),
    чтобы маркер и данные попали в БД атомарно одной транзакцией (ровно то,
    что требует докстринг модуля и docs/migration-plan-v2.md §7 п.2).

    Идемпотентно: если маркер уже стоит — ничего не делает и возвращает
    False (первая запись должна и остаться первой; дата/ревизия не должны
    "уезжать" при каждой новой записи). Возвращает True, если это был
    настоящий первый раз (маркер только что установлен) — вызывающему коду
    это обычно неинтересно, но пригождается тестам.
    """
    existing = c.execute(
        "SELECT value FROM kv WHERE key = ?", (KEY_V2_FIRST_WRITE,)
    ).fetchone()
    if existing is not None:
        return False

    now = int(time.time())
    rev = revision if revision is not None else str(now)

    def _upsert(key, value):
        c.execute(
            "INSERT INTO kv (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
            "updated_at = excluded.updated_at",
            (key, json.dumps(value, ensure_ascii=False), now),
        )

    _upsert(KEY_V2_FIRST_WRITE, rev)
    _upsert(KEY_DOMAIN_GENERATION, CURRENT_GENERATION)
    _upsert(KEY_MIN_READER_GENERATION, CURRENT_GENERATION)
    return True


def mark_phase_binding(c) -> bool:
    """Отметить первую пофазную привязку: поднять поколение БД до
    `PHASE_GENERATION`. Как и `mark_v2_domain_write`, принимает курсор уже
    открытой транзакции и вызывается внутри того же `db.transaction()`, что и
    запись привязки, — маркер и данные попадают в БД атомарно.

    Идемпотентно: если БД уже требует читателя поколения 3 и выше — ничего не
    делает и возвращает False. Поколение не убывает. Если v2-записей ещё не
    было, сначала ставит их маркер (`mark_v2_domain_write`), чтобы набор
    ключей поколения всегда был согласован."""
    row = c.execute(
        "SELECT value FROM kv WHERE key = ?", (KEY_MIN_READER_GENERATION,)
    ).fetchone()
    current = LEGACY_GENERATION
    if row is not None:
        try:
            current = _coerce_generation(json.loads(row[0]))
        except (TypeError, ValueError):
            current = _coerce_generation(row[0])
    if current >= PHASE_GENERATION:
        return False

    mark_v2_domain_write(c)
    now = int(time.time())
    for key in (KEY_DOMAIN_GENERATION, KEY_MIN_READER_GENERATION):
        c.execute(
            "INSERT INTO kv (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
            "updated_at = excluded.updated_at",
            (key, json.dumps(PHASE_GENERATION), now),
        )
    return True

"""Сервис привязок точек учёта к приборам — point_bindings (ТЗ §4.1/§5.4/§6.1).

Пишется лично (не делегировано Haiku) — по явному указанию пользователя:
"интервальную логику point_bindings + замена прибора... критичная
финансовая корректность". Покрывает:

- непересечение профилей каналов WB-MAP3E: total_3p физически перекрывается
  с каждой из трёх фаз, сами фазы между собой независимы (A17);
- запрет назначить один физический scope (meter_source_id + пересекающийся
  канал) двум точкам одновременно, и запрет "check", который на самом деле
  измеряет тот же физический scope, что и уже существующая привязка той же
  точки — такой check не независим (§4.1: "нельзя использовать как якобы
  независимый контроль самого себя");
- непересечение интервалов в пределах (point_id, role) — §6.1: "в пределах
  каждой сущности/роли интервалы не пересекаются";
- сегментацию периода при замене прибора без пропорционального дробления
  агрегатов, попавших на границу замены (A18/A19) — resolve_primary_segments
  строит непересекающиеся сегменты по истории привязок точки, а
  classify_aggregate_against_segment даёт вызывающему коду (будущий
  accounting_service, отдельная задача) чёткое правило: агрегат, целиком
  попавший в сегмент, суммируется; агрегат, пересекающий границу сегмента,
  НЕ делится по доле времени — вызывающий код обязан использовать точные
  граничные показания либо вернуть partial.

Конкурентность (A20): Database.transaction() сериализует запись через
threading.RLock на весь процесс (см. db.py) — проверка конфликтов внутри
транзакции гарантированно видит уже зафиксированные параллельные записи,
второй одновременный запрос увидит первую вставку и получит BindingConflict.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import List, Optional

log = logging.getLogger(__name__)

VALID_CHANNEL_PROFILES = ("total_3p", "phase_l1", "phase_l2", "phase_l3")
VALID_ROLES = ("primary", "check")


class BindingConflict(ValueError):
    """Отказ из-за пересечения по §4.1/§6.1 (не из-за неверных входных
    данных) — API-слой должен транслировать это в HTTP 409, а не 400."""


def profiles_overlap(a, b):
    """total_3p пересекается с любой фазой (WB-MAP3E: либо одна трёхфазная,
    либо три независимые однофазные нагрузки — не то и другое разом).
    Разные фазы l1/l2/l3 между собой физически независимы."""
    if a not in VALID_CHANNEL_PROFILES:
        raise ValueError(f"неизвестный профиль канала: {a!r}")
    if b not in VALID_CHANNEL_PROFILES:
        raise ValueError(f"неизвестный профиль канала: {b!r}")
    if a == b:
        return True
    if a == "total_3p" or b == "total_3p":
        return True
    return False


def intervals_overlap(a_from, a_to, b_from, b_to):
    """Полуоткрытые интервалы [from, to). None у *_to значит "открыт сейчас"
    (+inf). Общая граница (a_to == b_from) пересечением НЕ считается —
    §4.1: "Соседние интервалы с общей границей допустимы"."""
    a_to_eff = a_to if a_to is not None else float("inf")
    b_to_eff = b_to if b_to is not None else float("inf")
    return a_from < b_to_eff and b_from < a_to_eff


@dataclass
class PointBinding:
    id: int
    point_id: int
    meter_source_id: int
    channel_profile: str
    role: str
    valid_from: int
    valid_to: Optional[int]
    replacement_note: Optional[str]
    created_at: int

    @classmethod
    def from_row(cls, row):
        return cls(
            id=row["id"],
            point_id=row["point_id"],
            meter_source_id=row["meter_source_id"],
            channel_profile=row["channel_profile"],
            role=row["role"],
            valid_from=row["valid_from"],
            valid_to=row["valid_to"],
            replacement_note=row["replacement_note"],
            created_at=row["created_at"],
        )


class PointBindingRepo:
    def __init__(self, db):
        self._db = db

    def get_by_id(self, binding_id):
        with self._db.read() as c:
            row = c.execute(
                "SELECT * FROM point_bindings WHERE id = ?", (binding_id,)
            ).fetchone()
            return PointBinding.from_row(row) if row else None

    def list_for_point(self, point_id, include_closed=True):
        with self._db.read() as c:
            if include_closed:
                rows = c.execute(
                    "SELECT * FROM point_bindings WHERE point_id = ? "
                    "ORDER BY valid_from, id", (point_id,)
                ).fetchall()
            else:
                rows = c.execute(
                    "SELECT * FROM point_bindings WHERE point_id = ? AND valid_to IS NULL "
                    "ORDER BY valid_from, id", (point_id,)
                ).fetchall()
            return [PointBinding.from_row(r) for r in rows]

    def get_open_primary(self, point_id):
        with self._db.read() as c:
            row = c.execute(
                "SELECT * FROM point_bindings WHERE point_id = ? AND role = 'primary' "
                "AND valid_to IS NULL", (point_id,)
            ).fetchone()
            return PointBinding.from_row(row) if row else None

    # -- проверка конфликтов -------------------------------------------

    def _find_conflicts(self, c, point_id, meter_source_id, channel_profile, role,
                         valid_from, valid_to, exclude_id=None):
        """Возвращает список (причина, sqlite3.Row) конфликтующих привязок.
        Пустой список = вставка допустима. Вызывается ВНУТРИ уже открытой
        транзакции (курсор c) — своей транзакции не открывает, поэтому
        безопасно комбинировать с другими проверками в одной атомарной
        операции (см. open_binding/replace_meter)."""
        conflicts = []

        # (а) физический scope: пересекающийся профиль на том же
        # meter_source, принадлежащий ДРУГОЙ точке — "один физический scope
        # нельзя одновременно назначить двум точкам".
        rows = c.execute(
            "SELECT * FROM point_bindings WHERE meter_source_id = ? AND point_id != ?",
            (meter_source_id, point_id)
        ).fetchall()
        for row in rows:
            if exclude_id is not None and row["id"] == exclude_id:
                continue
            if not profiles_overlap(row["channel_profile"], channel_profile):
                continue
            if not intervals_overlap(valid_from, valid_to, row["valid_from"], row["valid_to"]):
                continue
            conflicts.append(("cross_point_scope", row))

        # (б) псевдо-независимый check: та же точка, тот же meter_source,
        # пересекающийся профиль, любая существующая привязка (primary или
        # check) — check не может "проверять сам себя" тем же источником.
        if role == "check":
            rows = c.execute(
                "SELECT * FROM point_bindings WHERE meter_source_id = ? AND point_id = ?",
                (meter_source_id, point_id)
            ).fetchall()
            for row in rows:
                if exclude_id is not None and row["id"] == exclude_id:
                    continue
                if not profiles_overlap(row["channel_profile"], channel_profile):
                    continue
                if not intervals_overlap(valid_from, valid_to, row["valid_from"], row["valid_to"]):
                    continue
                conflicts.append(("self_check_not_independent", row))

        # (в) непересечение интервалов в пределах (point_id, role) — §6.1.
        rows = c.execute(
            "SELECT * FROM point_bindings WHERE point_id = ? AND role = ?",
            (point_id, role)
        ).fetchall()
        for row in rows:
            if exclude_id is not None and row["id"] == exclude_id:
                continue
            if not intervals_overlap(valid_from, valid_to, row["valid_from"], row["valid_to"]):
                continue
            conflicts.append(("role_interval_overlap", row))

        return conflicts

    @staticmethod
    def _format_conflicts(conflicts):
        reasons = {
            "cross_point_scope":
                "физический scope этого прибора/канала уже назначен другой точке "
                "на пересекающийся период",
            "self_check_not_independent":
                "check-привязка использует тот же физический scope, что и "
                "существующая привязка этой же точки — это не независимый контроль",
            "role_interval_overlap":
                "у точки уже есть привязка этой роли на пересекающийся период",
        }
        parts = []
        for reason, row in conflicts:
            parts.append(
                f"{reasons.get(reason, reason)} "
                f"(конфликтующая привязка id={row['id']}, point_id={row['point_id']}, "
                f"meter_source_id={row['meter_source_id']}, "
                f"channel_profile={row['channel_profile']}, role={row['role']}, "
                f"valid_from={row['valid_from']}, valid_to={row['valid_to']})"
            )
        return "; ".join(parts)

    # -- мутации ---------------------------------------------------------

    def open_binding(self, point_id, meter_source_id, channel_profile, role="primary",
                      valid_from=None, replacement_note=None):
        """Открыть новую привязку. Проверяет существование точки/источника
        и все инварианты пересечения атомарно в одной транзакции."""
        if channel_profile not in VALID_CHANNEL_PROFILES:
            raise ValueError(f"неизвестный профиль канала: {channel_profile!r}")
        if role not in VALID_ROLES:
            raise ValueError(f"неизвестная роль привязки: {role!r}")

        now = int(time.time())
        vf = valid_from if valid_from is not None else now

        with self._db.transaction() as c:
            if c.execute(
                "SELECT 1 FROM metering_points WHERE id = ?", (point_id,)
            ).fetchone() is None:
                raise ValueError(f"Точка {point_id} не найдена")
            if c.execute(
                "SELECT 1 FROM meter_sources WHERE id = ?", (meter_source_id,)
            ).fetchone() is None:
                raise ValueError(f"Источник {meter_source_id} не найден")

            conflicts = self._find_conflicts(
                c, point_id, meter_source_id, channel_profile, role, vf, None
            )
            if conflicts:
                raise BindingConflict(self._format_conflicts(conflicts))

            cur = c.execute(
                "INSERT INTO point_bindings "
                "(point_id, meter_source_id, channel_profile, role, valid_from, valid_to, "
                "replacement_note, created_at) VALUES (?, ?, ?, ?, ?, NULL, ?, ?)",
                (point_id, meter_source_id, channel_profile, role, vf,
                 replacement_note, now)
            )
            binding_id = cur.lastrowid

        log.info(
            "Открыта привязка точки %d: source=%d profile=%s role=%s (id=%d)",
            point_id, meter_source_id, channel_profile, role, binding_id
        )
        return self.get_by_id(binding_id)

    def close_binding(self, binding_id, at=None):
        """Закрыть привязку, проставив valid_to."""
        now = at if at is not None else int(time.time())
        with self._db.transaction() as c:
            row = c.execute(
                "SELECT * FROM point_bindings WHERE id = ?", (binding_id,)
            ).fetchone()
            if row is None:
                raise ValueError(f"Привязка {binding_id} не найдена")
            if row["valid_to"] is not None:
                raise ValueError(f"Привязка {binding_id} уже закрыта")
            if now <= row["valid_from"]:
                raise ValueError(
                    "Момент закрытия должен быть позже начала привязки"
                )
            c.execute(
                "UPDATE point_bindings SET valid_to = ? WHERE id = ?", (now, binding_id)
            )
        log.info("Закрыта привязка %d", binding_id)

    def replace_meter(self, point_id, new_meter_source_id, at=None,
                       channel_profile=None, replacement_note=None):
        """Замена прибора у точки (A18): атомарно закрывает текущую
        открытую основную привязку в момент `at` и открывает новую на
        new_meter_source_id с того же момента — тот же point_id, новый
        meter_source_id (у нового физического прибора всегда новый
        meter_id/meter_source, см. §4.1). Старые агрегаты прежнего
        источника не переписываются и не делятся пропорционально; их
        корректная сегментация — дело вызывающего кода расчёта через
        resolve_primary_segments/classify_aggregate_against_segment ниже."""
        now = int(time.time())
        at = at if at is not None else now

        with self._db.transaction() as c:
            current = c.execute(
                "SELECT * FROM point_bindings WHERE point_id = ? AND role = 'primary' "
                "AND valid_to IS NULL", (point_id,)
            ).fetchone()
            if current is None:
                raise ValueError(
                    f"У точки {point_id} нет открытой основной привязки для замены"
                )
            if at <= current["valid_from"]:
                raise ValueError(
                    "Момент замены должен быть позже начала текущей привязки"
                )
            profile = channel_profile or current["channel_profile"]

            conflicts = self._find_conflicts(
                c, point_id, new_meter_source_id, profile, "primary", at, None,
                exclude_id=current["id"],
            )
            if conflicts:
                raise BindingConflict(self._format_conflicts(conflicts))

            c.execute(
                "UPDATE point_bindings SET valid_to = ? WHERE id = ?",
                (at, current["id"])
            )
            cur = c.execute(
                "INSERT INTO point_bindings "
                "(point_id, meter_source_id, channel_profile, role, valid_from, valid_to, "
                "replacement_note, created_at) VALUES (?, ?, ?, 'primary', ?, NULL, ?, ?)",
                (point_id, new_meter_source_id, profile, at, replacement_note, now)
            )
            new_id = cur.lastrowid

        log.info(
            "Точка %d: замена прибора на meter_source_id=%d с %d (новая привязка id=%d, "
            "закрыта id=%d)", point_id, new_meter_source_id, at, new_id, current["id"]
        )
        return self.get_by_id(new_id)


# ---------------------------------------------------------------------
# Сегментация периода и правило неделимости агрегата на границе (A18/A19).
# Чистые функции без обращения к БД — используются будущим accounting_service
# (отдельная задача) как контракт между "какие сегменты были у точки" и
# "какие агрегаты можно суммировать без искажения".
# ---------------------------------------------------------------------

@dataclass
class Segment:
    meter_source_id: int
    channel_profile: str
    ts_from: int
    ts_to: int  # всегда конкретное число — сегмент обрезан по запрошенному периоду


def resolve_primary_segments(bindings: List[PointBinding], ts_from: int, ts_to: int) -> List[Segment]:
    """Строит непересекающиеся сегменты основных (role='primary') привязок
    точки, обрезанные по запрошенному периоду [ts_from, ts_to). Время точки
    без открытой основной привязки в сегменты не попадает — вызывающий код
    обязан трактовать такие промежутки как отсутствие данных, а не как
    "0 расхода"."""
    segs = []
    for b in bindings:
        if b.role != "primary":
            continue
        b_to = b.valid_to if b.valid_to is not None else ts_to
        lo = max(b.valid_from, ts_from)
        hi = min(b_to, ts_to)
        if lo < hi:
            segs.append(Segment(b.meter_source_id, b.channel_profile, lo, hi))
    segs.sort(key=lambda s: s.ts_from)
    return segs


AGG_FULLY_INSIDE = "inside"
AGG_STRADDLES = "straddles"
AGG_OUTSIDE = "outside"


def classify_aggregate_against_segment(agg_ts_from: int, agg_ts_to: int, segment: Segment) -> str:
    """A18/A19: сырой агрегат [agg_ts_from, agg_ts_to) относительно
    сегмента точки:

    - AGG_FULLY_INSIDE — агрегат целиком внутри сегмента: суммировать
      как есть, он принадлежит одному прибору целиком;
    - AGG_STRADDLES — агрегат пересекает границу сегмента (например,
      часовой агрегат 12:00-13:00 при замене прибора в 12:20, A19):
      делить по доле времени ЗАПРЕЩЕНО. Вызывающий код обязан либо найти
      точные граничные показания в исходной истории и пересчитать
      сегменты по ним, либо вернуть availability=partial за этот агрегат
      для обоих смежных сегментов — но не выдумывать пропорцию;
    - AGG_OUTSIDE — агрегат не имеет отношения к этому сегменту."""
    if agg_ts_from >= segment.ts_from and agg_ts_to <= segment.ts_to:
        return AGG_FULLY_INSIDE
    if agg_ts_to <= segment.ts_from or agg_ts_from >= segment.ts_to:
        return AGG_OUTSIDE
    return AGG_STRADDLES

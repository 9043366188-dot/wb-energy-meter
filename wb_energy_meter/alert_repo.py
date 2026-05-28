"""Репозиторий событий доступности (alert_events).

Логика хранения:
- При переходе в "плохой" статус (no_connection, device_error) —
  открывается запись: started_at=now, ended_at=NULL, status='active'.
- При возврате в "нормальный" статус — запись закрывается:
  ended_at=now, status='resolved'.
- Незакрытые записи (status='active') означают, что прямо сейчас
  счётчик недоступен.

Интервалы недоступности вычисляются из пар started_at / ended_at.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional

from .db import Database


log = logging.getLogger(__name__)

# Статусы которые считаем "недоступностью" и записываем в историю
BAD_STATUSES = frozenset({"no_connection", "device_error"})


@dataclass
class AlertEvent:
    id: int
    rule_type: str
    meter_id: Optional[int]
    started_at: int
    ended_at: Optional[int]
    status: str        # 'active' | 'resolved'
    detail: Optional[str]

    @property
    def duration_s(self) -> Optional[int]:
        if self.ended_at is None:
            return None
        return self.ended_at - self.started_at

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "rule_type": self.rule_type,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "duration_s": self.duration_s,
            "status": self.status,
            "detail": self.detail,
        }

    @classmethod
    def from_row(cls, row) -> "AlertEvent":
        return cls(
            id=row["id"],
            rule_type=row["rule_type"],
            meter_id=row["meter_id"],
            started_at=row["started_at"],
            ended_at=row["ended_at"],
            status=row["status"],
            detail=row["detail"],
        )


class AlertRepo:
    def __init__(self, db: Database):
        self._db = db

    # ---------------------------------------------------------------- write

    def open_event(self, meter_id: int, rule_type: str,
                   detail: Optional[str] = None) -> int:
        """Открыть новое событие. Возвращает id."""
        now = int(time.time())
        with self._db.transaction() as c:
            cur = c.execute(
                "INSERT INTO alert_events "
                "(rule_type, meter_id, started_at, ended_at, status, detail) "
                "VALUES (?, ?, ?, NULL, 'active', ?)",
                (rule_type, meter_id, now, detail),
            )
            return cur.lastrowid

    def close_event(self, meter_id: int, rule_type: str) -> bool:
        """Закрыть активное событие для счётчика. Возвращает True если нашли."""
        now = int(time.time())
        with self._db.transaction() as c:
            cur = c.execute(
                "UPDATE alert_events SET ended_at=?, status='resolved' "
                "WHERE meter_id=? AND rule_type=? AND status='active'",
                (now, meter_id, rule_type),
            )
            return cur.rowcount > 0

    def record_transition(
        self,
        meter_id: int,
        old_status: str,
        new_status: str,
        detail: Optional[str] = None,
    ) -> None:
        """
        Вызывается при смене статуса счётчика.

        Правила:
        - Если новый статус "плохой" (no_connection / device_error) и
          старый был "нормальным" — открываем событие.
        - Если новый статус "нормальный" и старый был "плохим" —
          закрываем активное событие.
        - Если оба "плохие" но разные (нечасто) — закрываем старое,
          открываем новое.
        """
        old_bad = old_status in BAD_STATUSES
        new_bad = new_status in BAD_STATUSES

        if old_bad and not new_bad:
            # Счётчик восстановился — закрываем событие
            closed = self.close_event(meter_id, old_status)
            if closed:
                log.debug("Закрыто событие %s для meter_id=%d", old_status, meter_id)

        elif new_bad and not old_bad:
            # Счётчик упал — открываем событие
            self.open_event(meter_id, new_status, detail)
            log.debug("Открыто событие %s для meter_id=%d", new_status, meter_id)

        elif new_bad and old_bad and new_status != old_status:
            # Сменился тип проблемы (редко) — переоткрываем
            self.close_event(meter_id, old_status)
            self.open_event(meter_id, new_status, detail)

    # ---------------------------------------------------------------- read

    def get_active(self, meter_id: int) -> list[AlertEvent]:
        """Текущие активные события для счётчика."""
        with self._db.read() as c:
            rows = c.execute(
                "SELECT * FROM alert_events "
                "WHERE meter_id=? AND status='active' "
                "ORDER BY started_at DESC",
                (meter_id,),
            ).fetchall()
            return [AlertEvent.from_row(r) for r in rows]

    def list_events(
        self,
        meter_id: int,
        ts_from: int,
        ts_to: int,
        rule_types: Optional[list] = None,
    ) -> list[AlertEvent]:
        """
        События для счётчика, которые начались или активны в [ts_from, ts_to].
        """
        sql = (
            "SELECT * FROM alert_events "
            "WHERE meter_id=? "
            "AND started_at < ? "
            "AND (ended_at IS NULL OR ended_at >= ?) "
        )
        params: list = [meter_id, ts_to, ts_from]
        if rule_types:
            placeholders = ",".join("?" * len(rule_types))
            sql += f"AND rule_type IN ({placeholders}) "
            params.extend(rule_types)
        sql += "ORDER BY started_at"
        with self._db.read() as c:
            rows = c.execute(sql, params).fetchall()
            return [AlertEvent.from_row(r) for r in rows]

    def availability_stats(
        self,
        meter_id: int,
        ts_from: int,
        ts_to: int,
    ) -> dict:
        """
        Статистика доступности за период [ts_from, ts_to].

        Возвращает:
        - total_s: длина периода в секундах
        - unavailable_s: суммарное время недоступности
        - availability_pct: процент времени на связи
        - incidents: количество инцидентов
        - intervals: список интервалов недоступности
        """
        total_s = ts_to - ts_from
        if total_s <= 0:
            return {
                "total_s": 0, "unavailable_s": 0,
                "availability_pct": 100.0, "incidents": 0, "intervals": [],
            }

        events = self.list_events(
            meter_id, ts_from, ts_to,
            rule_types=["no_connection", "device_error"],
        )

        unavailable_s = 0
        intervals = []
        for ev in events:
            # Обрезаем по границам периода
            start = max(ev.started_at, ts_from)
            end = min(ev.ended_at if ev.ended_at else ts_to, ts_to)
            dur = max(0, end - start)
            unavailable_s += dur
            intervals.append({
                "started_at": ev.started_at,
                "ended_at": ev.ended_at,
                "duration_s": ev.duration_s,
                "rule_type": ev.rule_type,
                "status": ev.status,
                "detail": ev.detail,
            })

        unavailable_s = min(unavailable_s, total_s)
        avail_pct = round((1 - unavailable_s / total_s) * 100, 2)

        return {
            "total_s": total_s,
            "unavailable_s": unavailable_s,
            "availability_pct": avail_pct,
            "incidents": len(events),
            "intervals": intervals,
        }

    def summary_all_meters(
        self,
        meter_ids: list[int],
        ts_from: int,
        ts_to: int,
    ) -> dict[int, dict]:
        """Быстрая статистика по нескольким счётчикам сразу."""
        result = {}
        for mid in meter_ids:
            result[mid] = self.availability_stats(mid, ts_from, ts_to)
        return result

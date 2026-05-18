"""Repository для `period_aggregates`.

Архитектура: одна строка = один час для одного счётчика.

`(meter_id, period_type, period_start)` — primary key, поэтому повторная
запись для уже посчитанного часа просто перезатирает старое значение.
Это удобно для catch-up и латальщика: можно запускать повторно без боязни
сделать дубль.

Time format: `period_start` всегда align'нут к началу часа в local time
(`datetime.fromtimestamp(ts).replace(minute=0, second=0, microsecond=0)`).
`period_end = period_start + 3600`.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Iterable, Optional

from .db import Database

log = logging.getLogger(__name__)


PERIOD_TYPE_HOUR = "hour"


@dataclass
class HourlyAggregate:
    meter_id: int
    period_start: int          # unix epoch, выровнен на час
    period_end: int            # period_start + 3600
    ap_energy_start: Optional[float]
    ap_energy_end: Optional[float]
    ap_energy_delta: Optional[float]
    p_avg: Optional[float]
    p_max: Optional[float]
    samples_count: int
    quality_flag: str
    computed_at: int

    @classmethod
    def from_row(cls, row) -> "HourlyAggregate":
        return cls(
            meter_id=row["meter_id"],
            period_start=row["period_start"],
            period_end=row["period_end"],
            ap_energy_start=row["ap_energy_start"],
            ap_energy_end=row["ap_energy_end"],
            ap_energy_delta=row["ap_energy_delta"],
            p_avg=row["p_avg"],
            p_max=row["p_max"],
            samples_count=row["samples_count"] or 0,
            quality_flag=row["quality_flag"],
            computed_at=row["computed_at"],
        )

    def to_dict(self) -> dict:
        return {
            "period_start": self.period_start,
            "period_end": self.period_end,
            "ap_energy_start": self.ap_energy_start,
            "ap_energy_end": self.ap_energy_end,
            "consumption_kwh": self.ap_energy_delta,
            "p_avg": self.p_avg,
            "p_max": self.p_max,
            "samples_count": self.samples_count,
            "quality": self.quality_flag,
            "computed_at": self.computed_at,
        }


class AggregateRepo:
    """CRUD над `period_aggregates`."""

    def __init__(self, db: Database):
        self._db = db

    # ---------------------------------------------------------------- write

    def upsert(self, agg: HourlyAggregate) -> None:
        """Записать или перезаписать строку часа."""
        with self._db.transaction() as c:
            c.execute(
                """
                INSERT INTO period_aggregates
                  (meter_id, period_type, period_start, period_end,
                   ap_energy_start, ap_energy_end, ap_energy_delta,
                   p_avg, p_max, samples_count, quality_flag, computed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(meter_id, period_type, period_start) DO UPDATE SET
                  period_end       = excluded.period_end,
                  ap_energy_start  = excluded.ap_energy_start,
                  ap_energy_end    = excluded.ap_energy_end,
                  ap_energy_delta  = excluded.ap_energy_delta,
                  p_avg            = excluded.p_avg,
                  p_max            = excluded.p_max,
                  samples_count    = excluded.samples_count,
                  quality_flag     = excluded.quality_flag,
                  computed_at      = excluded.computed_at
                """,
                (agg.meter_id, PERIOD_TYPE_HOUR, agg.period_start, agg.period_end,
                 agg.ap_energy_start, agg.ap_energy_end, agg.ap_energy_delta,
                 agg.p_avg, agg.p_max, agg.samples_count,
                 agg.quality_flag, agg.computed_at),
            )

    def upsert_many(self, aggregates: Iterable[HourlyAggregate]) -> int:
        """Записать несколько часов одной транзакцией. Возвращает кол-во."""
        count = 0
        rows = [
            (a.meter_id, PERIOD_TYPE_HOUR, a.period_start, a.period_end,
             a.ap_energy_start, a.ap_energy_end, a.ap_energy_delta,
             a.p_avg, a.p_max, a.samples_count,
             a.quality_flag, a.computed_at)
            for a in aggregates
        ]
        if not rows:
            return 0
        with self._db.transaction() as c:
            c.executemany(
                """
                INSERT INTO period_aggregates
                  (meter_id, period_type, period_start, period_end,
                   ap_energy_start, ap_energy_end, ap_energy_delta,
                   p_avg, p_max, samples_count, quality_flag, computed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(meter_id, period_type, period_start) DO UPDATE SET
                  period_end       = excluded.period_end,
                  ap_energy_start  = excluded.ap_energy_start,
                  ap_energy_end    = excluded.ap_energy_end,
                  ap_energy_delta  = excluded.ap_energy_delta,
                  p_avg            = excluded.p_avg,
                  p_max            = excluded.p_max,
                  samples_count    = excluded.samples_count,
                  quality_flag     = excluded.quality_flag,
                  computed_at      = excluded.computed_at
                """,
                rows,
            )
            count = len(rows)
        return count

    # ---------------------------------------------------------------- read

    def get(self, meter_id: int, period_start: int) -> Optional[HourlyAggregate]:
        with self._db.read() as c:
            row = c.execute(
                "SELECT * FROM period_aggregates "
                "WHERE meter_id=? AND period_type=? AND period_start=?",
                (meter_id, PERIOD_TYPE_HOUR, period_start),
            ).fetchone()
            return HourlyAggregate.from_row(row) if row else None

    def list_range(
        self, meter_id: int, ts_from: int, ts_to: int
    ) -> list[HourlyAggregate]:
        """Все часы для счётчика с `period_start` в [ts_from, ts_to)."""
        with self._db.read() as c:
            rows = c.execute(
                "SELECT * FROM period_aggregates "
                "WHERE meter_id=? AND period_type=? "
                "AND period_start >= ? AND period_start < ? "
                "ORDER BY period_start",
                (meter_id, PERIOD_TYPE_HOUR, ts_from, ts_to),
            ).fetchall()
            return [HourlyAggregate.from_row(r) for r in rows]

    def sum_kwh(self, meter_id: int, ts_from: int, ts_to: int) -> Optional[float]:
        """
        Суммарный расход (`ap_energy_delta`) по часам, попадающим в
        [ts_from, ts_to). NULL значения пропускаются.
        """
        with self._db.read() as c:
            row = c.execute(
                "SELECT SUM(ap_energy_delta) AS s FROM period_aggregates "
                "WHERE meter_id=? AND period_type=? "
                "AND period_start >= ? AND period_start < ? "
                "AND ap_energy_delta IS NOT NULL",
                (meter_id, PERIOD_TYPE_HOUR, ts_from, ts_to),
            ).fetchone()
            return row["s"] if row else None

    def earliest_hour(self, meter_id: int) -> Optional[int]:
        """Самая старая посчитанная строка для счётчика."""
        with self._db.read() as c:
            row = c.execute(
                "SELECT MIN(period_start) AS m FROM period_aggregates "
                "WHERE meter_id=? AND period_type=?",
                (meter_id, PERIOD_TYPE_HOUR),
            ).fetchone()
            return row["m"] if row and row["m"] is not None else None

    def latest_hour(self, meter_id: int) -> Optional[int]:
        """Самая свежая посчитанная строка для счётчика."""
        with self._db.read() as c:
            row = c.execute(
                "SELECT MAX(period_start) AS m FROM period_aggregates "
                "WHERE meter_id=? AND period_type=?",
                (meter_id, PERIOD_TYPE_HOUR),
            ).fetchone()
            return row["m"] if row and row["m"] is not None else None

    def count_for_meter(self, meter_id: int) -> int:
        with self._db.read() as c:
            row = c.execute(
                "SELECT COUNT(*) AS n FROM period_aggregates "
                "WHERE meter_id=? AND period_type=?",
                (meter_id, PERIOD_TYPE_HOUR),
            ).fetchone()
            return int(row["n"])

    def missing_hours(
        self,
        meter_id: int,
        ts_from: int,
        ts_to: int,
    ) -> list[int]:
        """
        Найти `period_start` (выровненные на час), которые отсутствуют в
        БД для счётчика в диапазоне [ts_from, ts_to).
        """
        # Выровняем границы
        ts_from = _align_hour_down(ts_from)
        ts_to = _align_hour_down(ts_to)
        if ts_to <= ts_from:
            return []
        with self._db.read() as c:
            rows = c.execute(
                "SELECT period_start FROM period_aggregates "
                "WHERE meter_id=? AND period_type=? "
                "AND period_start >= ? AND period_start < ? "
                "ORDER BY period_start",
                (meter_id, PERIOD_TYPE_HOUR, ts_from, ts_to),
            ).fetchall()
            existing = {r["period_start"] for r in rows}
        expected = set(range(ts_from, ts_to, 3600))
        return sorted(expected - existing)

    def stats(self) -> dict:
        """Общая статистика по таблице, для CLI/UI."""
        with self._db.read() as c:
            total = c.execute(
                "SELECT COUNT(*) AS n FROM period_aggregates"
            ).fetchone()["n"]
            min_ts = c.execute(
                "SELECT MIN(period_start) AS m FROM period_aggregates"
            ).fetchone()["m"]
            max_ts = c.execute(
                "SELECT MAX(period_start) AS m FROM period_aggregates"
            ).fetchone()["m"]
            by_meter = c.execute(
                "SELECT meter_id, COUNT(*) AS n, "
                "MIN(period_start) AS min_ts, MAX(period_start) AS max_ts, "
                "SUM(ap_energy_delta) AS sum_kwh "
                "FROM period_aggregates "
                "WHERE period_type=? "
                "GROUP BY meter_id",
                (PERIOD_TYPE_HOUR,),
            ).fetchall()
            by_quality = c.execute(
                "SELECT quality_flag, COUNT(*) AS n FROM period_aggregates "
                "WHERE period_type=? GROUP BY quality_flag",
                (PERIOD_TYPE_HOUR,),
            ).fetchall()
            return {
                "rows_total": int(total),
                "earliest_ts": min_ts,
                "latest_ts": max_ts,
                "by_meter": [
                    {
                        "meter_id": r["meter_id"],
                        "rows": r["n"],
                        "earliest_ts": r["min_ts"],
                        "latest_ts": r["max_ts"],
                        "sum_kwh": r["sum_kwh"],
                    }
                    for r in by_meter
                ],
                "by_quality": {r["quality_flag"]: r["n"] for r in by_quality},
            }

    def delete_for_meter(self, meter_id: int) -> int:
        """Удалить все агрегаты счётчика. Используется при удалении счётчика."""
        with self._db.transaction() as c:
            cur = c.execute(
                "DELETE FROM period_aggregates WHERE meter_id=?",
                (meter_id,),
            )
            return cur.rowcount


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _align_hour_down(ts: int) -> int:
    """Выровнять unix timestamp на начало часа (в local time)."""
    from datetime import datetime
    dt = datetime.fromtimestamp(int(ts))
    aligned = dt.replace(minute=0, second=0, microsecond=0)
    return int(aligned.timestamp())


def align_hour_down(ts: float) -> int:
    """Public-доступная версия — для использования из aggregator.py."""
    return _align_hour_down(int(ts))

"""Repository для `period_aggregates` и `phase_aggregates`.

Архитектура: одна строка = один час для одного счётчика.

`(meter_id, period_type, period_start)` — primary key, поэтому повторная
запись для уже посчитанного часа просто перезатирает старое значение.
Это удобно для catch-up и латальщика: можно запускать повторно без боязни
сделать дубль.

Пофазные итоги (партия находок 0.22.3, П9, docs/design-phase-aggregates.md)
лежат в отдельной таблице `phase_aggregates` с ключом `(meter_id, phase,
period_start)` и теми же колонками. Тот же класс обслуживает обе таблицы:
`AggregateRepo(db)` — итог прибора (`period_aggregates`), как и раньше;
`repo.for_profile("phase_l2")` — репозиторий той же формы над фазой L2.
`period_aggregates` не менялась: прежний код пишет в неё по прежнему ключу.

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
from .wb_db_client import HistoryPoint

log = logging.getLogger(__name__)


PERIOD_TYPE_HOUR = "hour"

# Профиль привязки -> фаза таблицы phase_aggregates. total_3p — итог прибора.
PROFILE_PHASE = {"phase_l1": "L1", "phase_l2": "L2", "phase_l3": "L3"}
PHASES = ("L1", "L2", "L3")


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
        # A13 (партия 12): у часа со сбросом `ap_energy_delta` — только сумма
        # возрастающих участков (нижняя оценка), а не расход часа. Расходом
        # (`consumption_kwh`) её выдавать нельзя, она идёт отдельным полем
        # `known_kwh`; при `sparse` (A14) расход часа неизвестен (NULL).
        is_reset = self.quality_flag == "reset"
        d = {
            "period_start": self.period_start,
            "period_end": self.period_end,
            "ap_energy_start": self.ap_energy_start,
            "ap_energy_end": self.ap_energy_end,
            "consumption_kwh": None if is_reset else self.ap_energy_delta,
            "p_avg": self.p_avg,
            "p_max": self.p_max,
            "samples_count": self.samples_count,
            "quality": self.quality_flag,
            "computed_at": self.computed_at,
        }
        if is_reset:
            d["known_kwh"] = self.ap_energy_delta
            d["reason"] = ("сброс счётчика внутри часа: расход часа неизвестен, "
                           "известна только сумма возрастающих участков")
        elif self.quality_flag == "sparse":
            d["reason"] = ("показания редкие: расход по часам внутри интервала "
                           "неизвестен, известен только за интервал целиком")
        return d


class AggregateRepo:
    """CRUD над `period_aggregates` (итог прибора) или над одной фазой
    `phase_aggregates` (`phase="L1"|"L2"|"L3"`)."""

    def __init__(self, db: Database, phase: Optional[str] = None):
        if phase is not None and phase not in PHASES:
            raise ValueError(f"неизвестная фаза: {phase!r}")
        self._db = db
        self._phase = phase
        if phase is None:
            self._table, self._key_col, self._key_val = (
                "period_aggregates", "period_type", PERIOD_TYPE_HOUR)
        else:
            self._table, self._key_col, self._key_val = (
                "phase_aggregates", "phase", phase)

    @property
    def phase(self) -> Optional[str]:
        """None — итог прибора, иначе "L1"/"L2"/"L3"."""
        return self._phase

    def for_profile(self, channel_profile: str) -> "AggregateRepo":
        """Репозиторий итогов для профиля привязки: `total_3p` — итоги
        прибора, `phase_lN` — итоги фазы N. Источник итогов выбирает расчёт
        (`accounting_service.measured_point`) по профилю сегмента привязки."""
        if channel_profile == "total_3p":
            return AggregateRepo(self._db) if self._phase is not None else self
        phase = PROFILE_PHASE.get(channel_profile)
        if phase is None:
            raise ValueError(f"неизвестный профиль канала: {channel_profile!r}")
        if self._phase == phase:
            return self
        return AggregateRepo(self._db, phase=phase)

    # ---------------------------------------------------------------- write

    def _upsert_sql(self) -> str:
        return (
            f"INSERT INTO {self._table} "
            f"(meter_id, {self._key_col}, period_start, period_end, "
            "ap_energy_start, ap_energy_end, ap_energy_delta, "
            "p_avg, p_max, samples_count, quality_flag, computed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            f"ON CONFLICT(meter_id, {self._key_col}, period_start) DO UPDATE SET "
            "period_end       = excluded.period_end, "
            "ap_energy_start  = excluded.ap_energy_start, "
            "ap_energy_end    = excluded.ap_energy_end, "
            "ap_energy_delta  = excluded.ap_energy_delta, "
            "p_avg            = excluded.p_avg, "
            "p_max            = excluded.p_max, "
            "samples_count    = excluded.samples_count, "
            "quality_flag     = excluded.quality_flag, "
            "computed_at      = excluded.computed_at")

    def _row_params(self, a: HourlyAggregate):
        return (a.meter_id, self._key_val, a.period_start, a.period_end,
                a.ap_energy_start, a.ap_energy_end, a.ap_energy_delta,
                a.p_avg, a.p_max, a.samples_count,
                a.quality_flag, a.computed_at)

    def upsert(self, agg: HourlyAggregate) -> None:
        """Записать или перезаписать строку часа."""
        with self._db.transaction() as c:
            c.execute(self._upsert_sql(), self._row_params(agg))

    def upsert_many(self, aggregates: Iterable[HourlyAggregate]) -> int:
        """Записать несколько часов одной транзакцией. Возвращает кол-во."""
        rows = [self._row_params(a) for a in aggregates]
        if not rows:
            return 0
        with self._db.transaction() as c:
            c.executemany(self._upsert_sql(), rows)
        return len(rows)

    # ---------------------------------------------------------------- read

    def get(self, meter_id: int, period_start: int) -> Optional[HourlyAggregate]:
        with self._db.read() as c:
            row = c.execute(
                f"SELECT * FROM {self._table} "
                f"WHERE meter_id=? AND {self._key_col}=? AND period_start=?",
                (meter_id, self._key_val, period_start),
            ).fetchone()
            return HourlyAggregate.from_row(row) if row else None

    def list_range(
        self, meter_id: int, ts_from: int, ts_to: int
    ) -> list[HourlyAggregate]:
        """Все часы для счётчика с `period_start` в [ts_from, ts_to)."""
        with self._db.read() as c:
            rows = c.execute(
                f"SELECT * FROM {self._table} "
                f"WHERE meter_id=? AND {self._key_col}=? "
                "AND period_start >= ? AND period_start < ? "
                "ORDER BY period_start",
                (meter_id, self._key_val, ts_from, ts_to),
            ).fetchall()
            return [HourlyAggregate.from_row(r) for r in rows]

    def sum_kwh(self, meter_id: int, ts_from: int, ts_to: int) -> Optional[float]:
        """
        Суммарный расход (`ap_energy_delta`) по часам, попадающим в
        [ts_from, ts_to). NULL значения пропускаются.
        """
        with self._db.read() as c:
            row = c.execute(
                f"SELECT SUM(ap_energy_delta) AS s FROM {self._table} "
                f"WHERE meter_id=? AND {self._key_col}=? "
                "AND period_start >= ? AND period_start < ? "
                "AND ap_energy_delta IS NOT NULL",
                (meter_id, self._key_val, ts_from, ts_to),
            ).fetchone()
            return row["s"] if row else None

    def earliest_hour(self, meter_id: int) -> Optional[int]:
        """Самая старая посчитанная строка для счётчика."""
        with self._db.read() as c:
            row = c.execute(
                f"SELECT MIN(period_start) AS m FROM {self._table} "
                f"WHERE meter_id=? AND {self._key_col}=?",
                (meter_id, self._key_val),
            ).fetchone()
            return row["m"] if row and row["m"] is not None else None

    def latest_hour(self, meter_id: int) -> Optional[int]:
        """Самая свежая посчитанная строка для счётчика."""
        with self._db.read() as c:
            row = c.execute(
                f"SELECT MAX(period_start) AS m FROM {self._table} "
                f"WHERE meter_id=? AND {self._key_col}=?",
                (meter_id, self._key_val),
            ).fetchone()
            return row["m"] if row and row["m"] is not None else None

    def last_known_value_before(
        self, meter_id: int, before_ts: int,
        max_lookback_s: Optional[int] = None,
    ) -> Optional[HistoryPoint]:
        """Последнее ДОСТОВЕРНО известное значение `ap_energy_end` строго
        до `before_ts` — для переноса вперёд в часы, где RPC-окно не
        вернуло ни одной точки.

        Wiren Board публикует control в MQTT только по изменению
        значения (retained, publish-on-delta) — отсутствие новых точек
        за час НЕ означает "прибора нет", это может означать "значение
        не изменилось". aggregator.py::inject_carry_forward_point
        использует результат этого метода как синтетический якорь.

        Намеренно смотрит только на строки с `quality_flag='ok'`
        (оба конца часа были подтверждены РЕАЛЬНЫМИ точками) — иначе
        перенос мог бы опираться на уже перенесённое ранее значение и
        протянуть его сколь угодно далеко, никогда не упираясь в
        `max_lookback_s`. С этим ограничением "источник" переноса
        всегда настоящий, и его возраст всегда измеряется от настоящего
        наблюдения, а не от предыдущего переноса.

        `max_lookback_s`, если задан, ограничивает, насколько старым
        может быть этот якорь — не тащить значение через обрыв в
        полгода, как будто ничего не произошло."""
        with self._db.read() as c:
            params = [meter_id, self._key_val, before_ts]
            floor_clause = ""
            if max_lookback_s is not None:
                floor_clause = "AND period_start >= ? "
                params.append(before_ts - max_lookback_s)
            row = c.execute(
                f"SELECT period_start, ap_energy_end FROM {self._table} "
                f"WHERE meter_id=? AND {self._key_col}=? AND period_start < ? "
                f"{floor_clause}"
                "AND quality_flag='ok' AND ap_energy_end IS NOT NULL "
                "ORDER BY period_start DESC LIMIT 1",
                params,
            ).fetchone()
            if row is None:
                return None
            # period_start+3600 — момент конца часа, для которого
            # ap_energy_end был последний раз подтверждён реальной точкой.
            return HistoryPoint(
                timestamp=row["period_start"] + 3600,
                value=row["ap_energy_end"],
            )

    def count_for_meter(self, meter_id: int) -> int:
        with self._db.read() as c:
            row = c.execute(
                f"SELECT COUNT(*) AS n FROM {self._table} "
                f"WHERE meter_id=? AND {self._key_col}=?",
                (meter_id, self._key_val),
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
                f"SELECT period_start FROM {self._table} "
                f"WHERE meter_id=? AND {self._key_col}=? "
                "AND period_start >= ? AND period_start < ? "
                "ORDER BY period_start",
                (meter_id, self._key_val, ts_from, ts_to),
            ).fetchall()
            existing = {r["period_start"] for r in rows}
        expected = set(range(ts_from, ts_to, 3600))
        return sorted(expected - existing)

    def stats(self) -> dict:
        """Общая статистика по таблице, для CLI/UI."""
        with self._db.read() as c:
            total = c.execute(
                f"SELECT COUNT(*) AS n FROM {self._table}"
            ).fetchone()["n"]
            min_ts = c.execute(
                f"SELECT MIN(period_start) AS m FROM {self._table}"
            ).fetchone()["m"]
            max_ts = c.execute(
                f"SELECT MAX(period_start) AS m FROM {self._table}"
            ).fetchone()["m"]
            by_meter = c.execute(
                "SELECT meter_id, COUNT(*) AS n, "
                "MIN(period_start) AS min_ts, MAX(period_start) AS max_ts, "
                "SUM(ap_energy_delta) AS sum_kwh "
                f"FROM {self._table} "
                f"WHERE {self._key_col}=? "
                "GROUP BY meter_id",
                (self._key_val,),
            ).fetchall()
            by_quality = c.execute(
                f"SELECT quality_flag, COUNT(*) AS n FROM {self._table} "
                f"WHERE {self._key_col}=? GROUP BY quality_flag",
                (self._key_val,),
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

    def phase_stats(self) -> dict:
        """Статистика по `phase_aggregates` (для CLI/UI): всего строк и
        строки по парам (прибор, фаза). Для итогов прибора считается отдельно
        от `stats()`, чтобы формат `stats()` не менялся."""
        with self._db.read() as c:
            total = c.execute(
                "SELECT COUNT(*) AS n FROM phase_aggregates").fetchone()["n"]
            rows = c.execute(
                "SELECT meter_id, phase, COUNT(*) AS n, "
                "MIN(period_start) AS min_ts, MAX(period_start) AS max_ts, "
                "SUM(ap_energy_delta) AS sum_kwh "
                "FROM phase_aggregates GROUP BY meter_id, phase "
                "ORDER BY meter_id, phase").fetchall()
            by_quality = c.execute(
                "SELECT quality_flag, COUNT(*) AS n FROM phase_aggregates "
                "GROUP BY quality_flag").fetchall()
        return {
            "rows_total": int(total),
            "by_meter_phase": [
                {"meter_id": r["meter_id"], "phase": r["phase"], "rows": r["n"],
                 "earliest_ts": r["min_ts"], "latest_ts": r["max_ts"],
                 "sum_kwh": r["sum_kwh"]} for r in rows],
            "by_quality": {r["quality_flag"]: r["n"] for r in by_quality},
        }

    def delete_for_meter(self, meter_id: int) -> int:
        """Удалить все агрегаты счётчика. Используется при удалении счётчика.
        У репозитория итогов прибора — и итоги, и все его фазы; у репозитория
        фазы — только её строки."""
        with self._db.transaction() as c:
            cur = c.execute(
                f"DELETE FROM {self._table} WHERE meter_id=?"
                + (f" AND {self._key_col}=?" if self._phase is not None else ""),
                (meter_id,) if self._phase is None else (meter_id, self._key_val),
            )
            n = cur.rowcount
            if self._phase is None:
                c.execute("DELETE FROM phase_aggregates WHERE meter_id=?",
                          (meter_id,))
            return n


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

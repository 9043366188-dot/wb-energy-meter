"""Калькулятор расхода электроэнергии."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional, Sequence

from .periods import Period
from .wb_db_client import HistoryPoint, WbDbClient

log = logging.getLogger(__name__)

EDGE_SEARCH_WINDOW_S = 24 * 3600
EDGE_APPROX_THRESHOLD_S = 5 * 60
INTERNAL_GAP_THRESHOLD_S = 30 * 60


@dataclass
class ConsumptionResult:
    device_id: str
    period: Period
    consumption_kwh: Optional[float]
    ap_energy_start: Optional[float]
    ap_energy_end: Optional[float]
    ts_start_actual: Optional[float]
    ts_end_actual: Optional[float]
    samples_in_period: int
    quality: str
    warnings: list = field(default_factory=list)

    def to_dict(self):
        return {
            "device_id": self.device_id,
            "period": self.period.to_dict(),
            "consumption_kwh": (
                round(self.consumption_kwh, 6)
                if self.consumption_kwh is not None else None),
            "ap_energy_start": self.ap_energy_start,
            "ap_energy_end": self.ap_energy_end,
            "ts_start_actual": self.ts_start_actual,
            "ts_end_actual": self.ts_end_actual,
            "samples_in_period": self.samples_in_period,
            "quality": self.quality,
            "warnings": self.warnings,
        }


def calculate_from_points(points, period, device_id=""):
    warnings = []
    if not points:
        return ConsumptionResult(
            device_id=device_id, period=period, consumption_kwh=None,
            ap_energy_start=None, ap_energy_end=None,
            ts_start_actual=None, ts_end_actual=None,
            samples_in_period=0, quality="no_data",
            warnings=["Нет данных в истории по этому каналу"])

    start_pt = _find_point_at_or_before(points, period.ts_from)
    if start_pt is None:
        start_pt = points[0]
        warnings.append("Нет данных до начала периода: значение в начале "
                        "аппроксимировано первой доступной точкой ПОСЛЕ начала")

    end_pt = _find_point_at_or_before(points, period.ts_to)
    if end_pt is None:
        return ConsumptionResult(
            device_id=device_id, period=period, consumption_kwh=None,
            ap_energy_start=start_pt.value, ap_energy_end=None,
            ts_start_actual=start_pt.timestamp, ts_end_actual=None,
            samples_in_period=0, quality="no_data",
            warnings=["В период не попало ни одной точки данных"])

    quality = "ok"
    start_dist = abs(start_pt.timestamp - period.ts_from)
    end_dist = abs(end_pt.timestamp - period.ts_to)
    if start_dist > EDGE_APPROX_THRESHOLD_S:
        quality = "edge_approx"
        warnings.append(f"Граница начала периода аппроксимирована: ближайшая "
                        f"точка за {_human_age(start_dist)} от начала")
    if end_dist > EDGE_APPROX_THRESHOLD_S:
        quality = "edge_approx"
        warnings.append(f"Граница конца периода аппроксимирована: ближайшая "
                        f"точка за {_human_age(end_dist)} от конца")

    in_period = [p for p in points
                 if period.ts_from <= p.timestamp <= period.ts_to]
    samples_in_period = len(in_period)

    if samples_in_period >= 2:
        max_gap = 0.0
        for i in range(1, len(in_period)):
            gap = in_period[i].timestamp - in_period[i - 1].timestamp
            if gap > max_gap: max_gap = gap
        if max_gap > INTERNAL_GAP_THRESHOLD_S:
            if quality == "ok": quality = "gap"
            warnings.append(f"В период есть разрыв в данных длиной "
                            f"{_human_age(max_gap)}; расход может быть неточным")

    delta = end_pt.value - start_pt.value

    if delta < 0:
        return ConsumptionResult(
            device_id=device_id, period=period, consumption_kwh=None,
            ap_energy_start=start_pt.value, ap_energy_end=end_pt.value,
            ts_start_actual=start_pt.timestamp, ts_end_actual=end_pt.timestamp,
            samples_in_period=samples_in_period, quality="reset",
            warnings=warnings + [
                f"Накопительная энергия уменьшилась: {start_pt.value} -> "
                f"{end_pt.value}. Возможен сброс или замена."])

    if (samples_in_period == 0 and start_pt is end_pt
            and start_pt.timestamp < period.ts_from):
        quality = "stale"
        warnings.append("В период не попало ни одной новой точки. Скорее "
                        "всего, счётчик не публикует новых значений "
                        "(нет нагрузки или прибор не работает).")

    return ConsumptionResult(
        device_id=device_id, period=period,
        consumption_kwh=float(delta),
        ap_energy_start=start_pt.value, ap_energy_end=end_pt.value,
        ts_start_actual=start_pt.timestamp, ts_end_actual=end_pt.timestamp,
        samples_in_period=samples_in_period, quality=quality,
        warnings=warnings)


def _find_point_at_or_before(points, target_ts):
    best = None
    for p in points:
        if p.timestamp <= target_ts: best = p
        else: break
    return best


def _human_age(seconds):
    if seconds < 60: return f"{seconds:.0f} с"
    if seconds < 3600: return f"{seconds/60:.1f} мин"
    if seconds < 86400: return f"{seconds/3600:.1f} ч"
    return f"{seconds/86400:.1f} дн"


class ConsumptionService:
    AP_CHANNEL = "Total AP energy"

    def __init__(self, db_client):
        self._db = db_client

    def calculate(self, device_id, period, channel=None):
        ch = channel or self.AP_CHANNEL
        ts_from_query = period.ts_from - EDGE_SEARCH_WINDOW_S
        ts_to_query = period.ts_to + 60.0
        try:
            points = self._db.get_values(
                device=device_id, control=ch,
                ts_from=ts_from_query, ts_to=ts_to_query, limit=10000)
        except Exception as e:
            log.warning("Не смог получить историю %s/%s: %s",
                        device_id, ch, e)
            return ConsumptionResult(
                device_id=device_id, period=period, consumption_kwh=None,
                ap_energy_start=None, ap_energy_end=None,
                ts_start_actual=None, ts_end_actual=None,
                samples_in_period=0, quality="no_data",
                warnings=[f"Ошибка чтения истории: {e}"])

        if not points:
            try:
                points = self._db.get_values(
                    device=device_id, control=ch,
                    ts_from=None, ts_to=ts_to_query, limit=10000)
            except Exception:
                points = []

        return calculate_from_points(points, period, device_id=device_id)

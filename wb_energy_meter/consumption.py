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

    def __init__(self, db_client, aggregates_repo=None, meters_repo=None):
        """
        :param db_client: WbDbClient — для RPC к wb-mqtt-db (всегда нужен).
        :param aggregates_repo: AggregateRepo — если передан, используется
                                для ускорения за счёт предрассчитанных часов.
        :param meters_repo: MeterRepo — нужен только если используется
                                aggregates_repo (чтобы по device_id найти id).
        """
        self._db = db_client
        self._aggregates = aggregates_repo
        self._meters = meters_repo

    def calculate(self, device_id, period, channel=None):
        ch = channel or self.AP_CHANNEL

        # Гибридная стратегия — только для канала Total AP energy и при
        # наличии репозитория агрегатов.
        if (self._aggregates is not None and self._meters is not None
                and ch == self.AP_CHANNEL):
            try:
                hybrid = self._calculate_hybrid(device_id, period)
                if hybrid is not None:
                    return hybrid
            except Exception as e:
                log.debug("hybrid calc fallback to RPC: %s", e)

        # Старый путь: всё через RPC.
        return self._calculate_via_rpc(device_id, period, ch)

    def _calculate_hybrid(self, device_id, period):
        """
        Гибридный расчёт:
          [полные внутренние часы] = SUM из period_aggregates
          [хвосты]                  = RPC за начало и конец периода

        Возвращает ConsumptionResult, либо None если по каким-то причинам
        гибрид не получился (например, счётчика нет в БД, или нет полных
        часов целиком внутри периода — тогда дешевле через RPC).
        """
        from .aggregates_repo import align_hour_down

        meter = self._meters.get_by_device_id(device_id)
        if meter is None:
            return None

        ts_from = period.ts_from
        ts_to = period.ts_to

        # Границы «полных часов» внутри периода.
        # Полный час [H, H+3600) лежит ВНУТРИ периода если ts_from <= H и H+3600 <= ts_to.
        # Иначе он попадает в один из «хвостов».
        # Найдём первый полный час: первая граница часа >= ts_from.
        # И последний полный час: последняя граница часа + 3600 <= ts_to.
        first_full = align_hour_down(ts_from)
        if first_full < ts_from:
            first_full += 3600
        last_full = align_hour_down(ts_to)  # это правая граница: full_end = last_full
        # Если last_full > ts_to — невозможно, align вниз. last_full <= ts_to.
        # full_hours покрывают [first_full, last_full)

        if last_full <= first_full:
            # Период полностью укладывается в один час либо две хвостовые
            # доли. Hybrid тут не выгоден — RPC всё посчитает за один запрос.
            return None

        # 1. Получить SUM по агрегатам для интервала [first_full, last_full)
        full_aggs = self._aggregates.list_range(meter.id, first_full, last_full)
        full_hours_count = (last_full - first_full) // 3600

        # Если в БД покрыто меньше 80% часов — fallback на RPC.
        # (Иначе ответ будет некачественный, лучше пойти честным путём.)
        if len(full_aggs) < full_hours_count * 0.8:
            return None

        # Считаем сумму и собираем warnings
        sum_kwh = 0.0
        any_no_data = 0
        any_reset = False
        for a in full_aggs:
            if a.ap_energy_delta is None:
                any_no_data += 1
                if a.quality_flag == "reset":
                    any_reset = True
            else:
                sum_kwh += a.ap_energy_delta

        # 2. Хвосты: запросить RPC для двух коротких диапазонов.
        # Левый хвост: [ts_from, first_full)
        # Правый хвост: [last_full, ts_to)
        left_kwh = 0.0
        right_kwh = 0.0
        warnings_tails: list[str] = []

        if first_full > ts_from:
            left = self._compute_tail(device_id, ts_from, first_full)
            if left.consumption_kwh is not None:
                left_kwh = left.consumption_kwh
            else:
                warnings_tails.append("Левый «хвост» периода: " + (
                    left.warnings[0] if left.warnings else left.quality
                ))

        if last_full < ts_to:
            right = self._compute_tail(device_id, last_full, ts_to)
            if right.consumption_kwh is not None:
                right_kwh = right.consumption_kwh
            else:
                warnings_tails.append("Правый «хвост» периода: " + (
                    right.warnings[0] if right.warnings else right.quality
                ))

        total = sum_kwh + left_kwh + right_kwh

        # Определяем общий quality
        quality = "ok"
        warnings = []
        if any_no_data > 0:
            warnings.append(
                f"В периоде {any_no_data} час(ов) без данных в агрегатах — "
                f"они посчитаны как 0. Если данные подъедут, выполните "
                f"`aggregates catchup`.")
            quality = "gap"
        if any_reset:
            warnings.append("В периоде зафиксирован сброс счётчика — "
                            "расход может быть неточным")
            quality = "reset"
        warnings.extend(warnings_tails)

        return ConsumptionResult(
            device_id=device_id, period=period,
            consumption_kwh=float(total),
            ap_energy_start=None,
            ap_energy_end=None,
            ts_start_actual=None, ts_end_actual=None,
            samples_in_period=sum(a.samples_count for a in full_aggs),
            quality=quality,
            warnings=warnings,
        )

    def _compute_tail(self, device_id, ts_from, ts_to):
        """RPC-расчёт для короткого хвоста периода."""
        from .periods import Period
        sub_period = Period(
            ts_from=float(ts_from), ts_to=float(ts_to),
            label="tail",
            description=f"tail [{ts_from}..{ts_to}]",
        )
        return self._calculate_via_rpc(device_id, sub_period, self.AP_CHANNEL)

    def _calculate_via_rpc(self, device_id, period, ch):
        """Старая логика — всё через RPC."""
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

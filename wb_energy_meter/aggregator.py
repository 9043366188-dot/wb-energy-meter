"""Воркер почасовых агрегатов.

Делает три вещи:

1. **Регулярная задача**: на границе каждого часа считает дельту
   `Total AP energy` за прошедший час и кладёт строку в
   `period_aggregates`. Гарантирует идемпотентность через `INSERT OR REPLACE`.

2. **Catch-up при старте**: в фоновом потоке догоняет пропущенные часы за
   последние N дней (по конфигу `catchup_days`). Идёт батчем по 24 часа за
   один RPC к wb-mqtt-db. Имеет жёсткий тайм-аут (по умолчанию 5 минут),
   после которого корректно завершается. Оставшееся можно добить
   командой CLI или дождаться регулярного латальщика.

3. **Латальщик**: раз в 6 часов перепроверяет последние 7 суток на дыры
   (`quality=no_data` или вообще отсутствующие часы) и пересчитывает их.

Все задачи в отдельных потоках, демон не блокируется.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from .aggregates_repo import (
    AggregateRepo, HourlyAggregate, PERIOD_TYPE_HOUR, align_hour_down,
)
from .repo import MeterRepo
from .wb_db_client import HistoryPoint, RpcError, WbDbClient


log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Конфигурация по умолчанию (можно переопределить через AggregatorConfig)
# ---------------------------------------------------------------------------

DEFAULTS = {
    # Глобальный таймаут на весь catch-up. После него поток завершается.
    "max_catchup_duration_s": 300,
    # На сколько дней назад дотягивать catch-up при старте.
    "catchup_days": 90,
    # Сколько последних суток перепроверять на дыры.
    "recompute_recent_h": 168,
    # Через сколько секунд после :00 запускать почасовой расчёт.
    # 90 с — даёт wb-mqtt-db время записать значения завершившегося часа.
    "hour_offset_s": 90,
    # Задержка перед стартом catch-up после запуска демона.
    # 10 с — пусть MQTT соберёт retained и статусы устаканятся.
    "catchup_start_delay_s": 10,
    # Интервал работы латальщика (часы между прогонами).
    "patcher_interval_h": 6,
    # Канал, по которому считаем расход.
    "energy_channel": "Total AP energy",
    "power_channel": "Total P",
    # При batch-запросе сколько точек за раз получать.
    "rpc_limit": 50000,
    # Пауза между обработкой счётчиков в catch-up.
    "inter_meter_pause_s": 0.1,
    # Backoff при RPC-ошибках.
    "rpc_backoff_s": 5.0,
    "rpc_max_retries": 3,
    # RPC-таймаут на одиночный вызов.
    "rpc_timeout_s": 15.0,
}


@dataclass
class AggregatorConfig:
    """Конфигурация агрегатора (поля из секции `aggregator:` в YAML)."""
    enabled: bool = True
    max_catchup_duration_s: int = DEFAULTS["max_catchup_duration_s"]
    catchup_days: int = DEFAULTS["catchup_days"]
    recompute_recent_h: int = DEFAULTS["recompute_recent_h"]
    hour_offset_s: int = DEFAULTS["hour_offset_s"]
    catchup_start_delay_s: int = DEFAULTS["catchup_start_delay_s"]
    patcher_interval_h: int = DEFAULTS["patcher_interval_h"]
    energy_channel: str = DEFAULTS["energy_channel"]
    power_channel: str = DEFAULTS["power_channel"]
    rpc_limit: int = DEFAULTS["rpc_limit"]
    inter_meter_pause_s: float = DEFAULTS["inter_meter_pause_s"]
    rpc_backoff_s: float = DEFAULTS["rpc_backoff_s"]
    rpc_max_retries: int = DEFAULTS["rpc_max_retries"]
    rpc_timeout_s: float = DEFAULTS["rpc_timeout_s"]


# ---------------------------------------------------------------------------
# Чистый алгоритм: посчитать один час из набора точек
# ---------------------------------------------------------------------------

def compute_hourly_aggregate(
    meter_id: int,
    hour_start: int,
    points_with_context: list[HistoryPoint],
    points_power: Optional[list[HistoryPoint]] = None,
) -> HourlyAggregate:
    """
    Посчитать дельту energy за час из готового списка точек.

    `points_with_context` должны включать одну точку <= hour_start
    (для AP_start) и одну точку <= hour_end (для AP_end). Если их нет —
    quality будет `no_data`.

    `points_power` — опциональные точки `Total P` за тот же час для p_avg/p_max.
    """
    hour_end = hour_start + 3600

    # Найти граничные точки
    pt_start = None
    pt_end = None
    for p in points_with_context:
        if p.timestamp <= hour_start:
            pt_start = p  # обновляется до последней <= hour_start
        if p.timestamp <= hour_end:
            pt_end = p
    in_hour = [p for p in points_with_context
               if hour_start <= p.timestamp < hour_end]

    # Power statistics (опционально)
    p_avg = None
    p_max = None
    if points_power:
        in_hour_power = [p.value for p in points_power
                         if hour_start <= p.timestamp < hour_end]
        if in_hour_power:
            p_avg = sum(in_hour_power) / len(in_hour_power)
            p_max = max(in_hour_power)

    now_ts = int(time.time())

    if pt_start is None or pt_end is None:
        return HourlyAggregate(
            meter_id=meter_id,
            period_start=hour_start, period_end=hour_end,
            ap_energy_start=pt_start.value if pt_start else None,
            ap_energy_end=pt_end.value if pt_end else None,
            ap_energy_delta=None,
            p_avg=p_avg, p_max=p_max,
            samples_count=len(in_hour),
            quality_flag="no_data",
            computed_at=now_ts,
        )

    delta = pt_end.value - pt_start.value

    quality = "ok"
    if delta < 0:
        # Счётчик обнулился внутри часа
        return HourlyAggregate(
            meter_id=meter_id,
            period_start=hour_start, period_end=hour_end,
            ap_energy_start=pt_start.value, ap_energy_end=pt_end.value,
            ap_energy_delta=None,
            p_avg=p_avg, p_max=p_max,
            samples_count=len(in_hour),
            quality_flag="reset",
            computed_at=now_ts,
        )

    # Качество граничных точек
    start_dist = abs(pt_start.timestamp - hour_start)
    end_dist = abs(pt_end.timestamp - hour_end)
    if start_dist > 600 or end_dist > 600:  # > 10 минут
        quality = "edge_approx"

    return HourlyAggregate(
        meter_id=meter_id,
        period_start=hour_start, period_end=hour_end,
        ap_energy_start=pt_start.value, ap_energy_end=pt_end.value,
        ap_energy_delta=float(delta),
        p_avg=p_avg, p_max=p_max,
        samples_count=len(in_hour),
        quality_flag=quality,
        computed_at=now_ts,
    )


# ---------------------------------------------------------------------------
# Aggregator — связь с MQTT-RPC и БД
# ---------------------------------------------------------------------------

class Aggregator:
    """
    Запускает три фоновых потока: hourly worker, catch-up worker, patcher.
    Каждый поток корректно завершается при stop().
    """

    def __init__(
        self,
        config: AggregatorConfig,
        db_client: WbDbClient,
        meters_repo: MeterRepo,
        aggregates_repo: AggregateRepo,
    ):
        self._cfg = config
        self._db_client = db_client
        self._meters = meters_repo
        self._aggregates = aggregates_repo
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        # Статус для UI/CLI
        self._status_lock = threading.RLock()
        self._catchup_running = False
        self._catchup_started_at: Optional[float] = None
        self._catchup_finished_at: Optional[float] = None
        self._catchup_processed_hours = 0
        self._catchup_total_planned = 0
        self._last_hourly_at: Optional[float] = None
        self._last_patcher_at: Optional[float] = None

    # ---- lifecycle ----

    def start(self) -> None:
        if not self._cfg.enabled:
            log.info("Aggregator выключен в конфиге (aggregator.enabled=false)")
            return
        for target, name in [
            (self._run_hourly, "aggr-hourly"),
            (self._run_catchup, "aggr-catchup"),
            (self._run_patcher, "aggr-patcher"),
        ]:
            t = threading.Thread(target=target, name=name, daemon=True)
            t.start()
            self._threads.append(t)
        log.info("Aggregator запущен (catchup_days=%d, max_catchup=%ds)",
                 self._cfg.catchup_days, self._cfg.max_catchup_duration_s)

    def stop(self) -> None:
        self._stop.set()
        for t in self._threads:
            t.join(timeout=5.0)
        self._threads = []

    def status(self) -> dict:
        with self._status_lock:
            return {
                "enabled": self._cfg.enabled,
                "catchup_running": self._catchup_running,
                "catchup_started_at": self._catchup_started_at,
                "catchup_finished_at": self._catchup_finished_at,
                "catchup_processed_hours": self._catchup_processed_hours,
                "catchup_total_planned": self._catchup_total_planned,
                "last_hourly_at": self._last_hourly_at,
                "last_patcher_at": self._last_patcher_at,
                "config": {
                    "catchup_days": self._cfg.catchup_days,
                    "max_catchup_duration_s": self._cfg.max_catchup_duration_s,
                    "recompute_recent_h": self._cfg.recompute_recent_h,
                    "patcher_interval_h": self._cfg.patcher_interval_h,
                },
            }

    # ---- 1. hourly worker ----

    def _run_hourly(self) -> None:
        """Раз в час, в HH:00+offset, считает прошедший час."""
        while not self._stop.is_set():
            sleep_s = self._seconds_until_next_hour_with_offset()
            if self._stop.wait(sleep_s):
                return
            try:
                self._compute_previous_hour_for_all()
            except Exception as e:
                log.exception("hourly worker error: %s", e)
            with self._status_lock:
                self._last_hourly_at = time.time()

    def _seconds_until_next_hour_with_offset(self) -> float:
        """Сколько секунд до ближайшего HH:00 + hour_offset_s."""
        now = datetime.now()
        next_hour = (now.replace(minute=0, second=0, microsecond=0)
                     + timedelta(hours=1))
        target = next_hour + timedelta(seconds=self._cfg.hour_offset_s)
        delta = (target - now).total_seconds()
        if delta < 0:
            delta = 60.0  # на всякий случай
        return delta

    def _compute_previous_hour_for_all(self) -> None:
        """Посчитать прошедший час для всех активных счётчиков."""
        now_aligned = align_hour_down(time.time())
        prev_hour_start = now_aligned - 3600

        meters = self._meters.list_all(only_enabled=True)
        log.info("Hourly: считаю час %s для %d счётчиков",
                 datetime.fromtimestamp(prev_hour_start).isoformat(timespec="seconds"),
                 len(meters))

        for m in meters:
            if self._stop.is_set():
                return
            try:
                self._compute_one_hour_for_meter(m.id, m.device_id, prev_hour_start)
            except Exception as e:
                log.warning("hourly: %s упал: %s", m.device_id, e)

    def _compute_one_hour_for_meter(
        self, meter_id: int, device_id: str, hour_start: int
    ) -> Optional[HourlyAggregate]:
        """Запросить RPC, посчитать, записать. Возвращает результат."""
        hour_end = hour_start + 3600
        # Берём с запасом: 1 час назад для AP_start, 5 минут после для AP_end
        ts_from = hour_start - 3600
        ts_to = hour_end + 300

        points_energy = self._rpc_get_values_with_retry(
            device_id, self._cfg.energy_channel, ts_from, ts_to,
        )
        if points_energy is None:
            # RPC окончательно отказал — пишем no_data
            agg = HourlyAggregate(
                meter_id=meter_id, period_start=hour_start, period_end=hour_end,
                ap_energy_start=None, ap_energy_end=None, ap_energy_delta=None,
                p_avg=None, p_max=None, samples_count=0,
                quality_flag="no_data", computed_at=int(time.time()),
            )
            self._aggregates.upsert(agg)
            return agg

        # Total P — best-effort, не критично если упадёт
        points_power = self._rpc_get_values_with_retry(
            device_id, self._cfg.power_channel, hour_start, hour_end,
            allow_empty=True,
        ) or []

        agg = compute_hourly_aggregate(
            meter_id=meter_id, hour_start=hour_start,
            points_with_context=points_energy,
            points_power=points_power,
        )
        self._aggregates.upsert(agg)
        return agg

    def _rpc_get_values_with_retry(
        self,
        device_id: str,
        channel: str,
        ts_from: float,
        ts_to: float,
        allow_empty: bool = False,
    ) -> Optional[list[HistoryPoint]]:
        """Запрос RPC с backoff и retry. Возвращает None при полной неудаче."""
        for attempt in range(self._cfg.rpc_max_retries):
            if self._stop.is_set():
                return None
            try:
                return self._db_client.get_values(
                    device=device_id, control=channel,
                    ts_from=ts_from, ts_to=ts_to,
                    limit=self._cfg.rpc_limit,
                    timeout_s=self._cfg.rpc_timeout_s,
                )
            except RpcError as e:
                if attempt == self._cfg.rpc_max_retries - 1:
                    log.warning("RPC %s/%s окончательно: %s",
                                device_id, channel, e)
                    return None if not allow_empty else []
                log.debug("RPC %s/%s попытка %d упала: %s. Backoff %.1fs",
                          device_id, channel, attempt + 1, e,
                          self._cfg.rpc_backoff_s)
                if self._stop.wait(self._cfg.rpc_backoff_s):
                    return None
        return None

    # ---- 2. catch-up worker ----

    def _run_catchup(self) -> None:
        """Однократный прогон при старте, с тайм-аутом."""
        # Задержка старта
        if self._stop.wait(self._cfg.catchup_start_delay_s):
            return
        try:
            self._do_catchup()
        except Exception as e:
            log.exception("catchup error: %s", e)

    def _do_catchup(self) -> None:
        meters = self._meters.list_all(only_enabled=True)
        if not meters:
            log.info("Catch-up: счётчиков нет, пропускаю")
            return

        with self._status_lock:
            self._catchup_running = True
            self._catchup_started_at = time.time()
            self._catchup_processed_hours = 0
            self._catchup_total_planned = 0

        deadline = time.time() + self._cfg.max_catchup_duration_s
        ts_now = align_hour_down(time.time())
        ts_oldest = ts_now - self._cfg.catchup_days * 86400

        log.info("Catch-up: начинаю, бюджет %ds, диапазон %d дней назад, "
                 "%d счётчиков",
                 self._cfg.max_catchup_duration_s,
                 self._cfg.catchup_days, len(meters))

        try:
            for m in meters:
                if self._stop.is_set() or time.time() >= deadline:
                    log.info("Catch-up прерван (timeout=%s, stop=%s) на %s",
                             time.time() >= deadline, self._stop.is_set(),
                             m.device_id)
                    break
                try:
                    self._catchup_one_meter(m.id, m.device_id,
                                             ts_oldest, ts_now, deadline)
                except Exception as e:
                    log.warning("catch-up %s: %s", m.device_id, e)

                if self._stop.wait(self._cfg.inter_meter_pause_s):
                    break
        finally:
            with self._status_lock:
                self._catchup_running = False
                self._catchup_finished_at = time.time()
            log.info(
                "Catch-up завершён: %d часов за %.1fs",
                self._catchup_processed_hours,
                self._catchup_finished_at - self._catchup_started_at,
            )

    def _catchup_one_meter(
        self,
        meter_id: int,
        device_id: str,
        ts_oldest: int,
        ts_newest: int,
        deadline: float,
    ) -> None:
        """Догнать пропущенные часы для одного счётчика."""
        missing = self._aggregates.missing_hours(meter_id, ts_oldest, ts_newest)
        if not missing:
            return

        with self._status_lock:
            self._catchup_total_planned += len(missing)

        log.info("Catch-up %s: %d часов для расчёта", device_id, len(missing))

        # Группируем подряд идущие пропуски в «диапазоны» — за один RPC
        # получаем сразу все точки для дня (или большего куска), а затем
        # внутри Python нарезаем на часы.
        for batch in _group_into_batches(missing, max_span_hours=24):
            if self._stop.is_set() or time.time() >= deadline:
                return
            batch_start = batch[0]
            batch_end = batch[-1] + 3600
            ts_from = batch_start - 3600  # с запасом для AP_start
            ts_to = batch_end + 300

            points_energy = self._rpc_get_values_with_retry(
                device_id, self._cfg.energy_channel, ts_from, ts_to,
            )
            if points_energy is None:
                # RPC окончательно отказал на этом батче — пишем no_data
                # для всех часов в батче, чтобы catch-up не пытался
                # бесконечно одно и то же дёргать. Латальщик потом
                # перепроверит.
                no_data_aggs = [
                    HourlyAggregate(
                        meter_id=meter_id, period_start=h, period_end=h + 3600,
                        ap_energy_start=None, ap_energy_end=None,
                        ap_energy_delta=None, p_avg=None, p_max=None,
                        samples_count=0, quality_flag="no_data",
                        computed_at=int(time.time()),
                    )
                    for h in batch
                ]
                self._aggregates.upsert_many(no_data_aggs)
                with self._status_lock:
                    self._catchup_processed_hours += len(batch)
                continue

            # Считаем все часы батча из одного набора точек
            aggs = []
            for hour_start in batch:
                agg = compute_hourly_aggregate(
                    meter_id=meter_id, hour_start=hour_start,
                    points_with_context=points_energy,
                    points_power=None,  # в catch-up не считаем p_avg/p_max
                                         # (экономия RPC; можно посчитать позже)
                )
                aggs.append(agg)
            self._aggregates.upsert_many(aggs)
            with self._status_lock:
                self._catchup_processed_hours += len(batch)

    # ---- 3. patcher ----

    def _run_patcher(self) -> None:
        """Раз в `patcher_interval_h` часов перепроверяет дыры в последних N днях."""
        interval_s = self._cfg.patcher_interval_h * 3600.0
        # Первый запуск — через интервал, не сразу
        if self._stop.wait(interval_s):
            return
        while not self._stop.is_set():
            try:
                self._do_patch()
            except Exception as e:
                log.exception("patcher error: %s", e)
            with self._status_lock:
                self._last_patcher_at = time.time()
            if self._stop.wait(interval_s):
                return

    def _do_patch(self) -> None:
        """
        Перепроверить последние `recompute_recent_h` часов и пересчитать
        строки с quality=no_data — возможно, в wb-mqtt-db уже подъехали точки.
        """
        meters = self._meters.list_all(only_enabled=True)
        if not meters:
            return
        ts_now = align_hour_down(time.time())
        ts_oldest = ts_now - self._cfg.recompute_recent_h * 3600
        patched = 0
        for m in meters:
            if self._stop.is_set():
                return
            # Найти часы с no_data (и пропущенные) в диапазоне
            missing = self._aggregates.missing_hours(m.id, ts_oldest, ts_now)
            no_data_existing = [
                a.period_start
                for a in self._aggregates.list_range(m.id, ts_oldest, ts_now)
                if a.quality_flag == "no_data"
            ]
            todo = sorted(set(missing) | set(no_data_existing))
            if not todo:
                continue
            log.info("Patcher %s: %d часов на пересчёт", m.device_id, len(todo))
            for batch in _group_into_batches(todo, max_span_hours=24):
                if self._stop.is_set():
                    return
                batch_start = batch[0]
                batch_end = batch[-1] + 3600
                points = self._rpc_get_values_with_retry(
                    m.device_id, self._cfg.energy_channel,
                    batch_start - 3600, batch_end + 300,
                )
                if points is None:
                    continue
                aggs = [
                    compute_hourly_aggregate(
                        meter_id=m.id, hour_start=h,
                        points_with_context=points,
                    )
                    for h in batch
                ]
                self._aggregates.upsert_many(aggs)
                patched += len(batch)
        if patched:
            log.info("Patcher: пересчитано часов: %d", patched)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _group_into_batches(
    hours_sorted: list[int], max_span_hours: int = 24
) -> list[list[int]]:
    """
    Группировать список выровненных-на-час timestamp'ов в батчи,
    в которых разрыв между крайними точками не превышает max_span_hours.
    Не обязательно непрерывных — это нормально для одного RPC.
    """
    if not hours_sorted:
        return []
    batches = []
    current = [hours_sorted[0]]
    max_span_s = max_span_hours * 3600
    for ts in hours_sorted[1:]:
        if ts - current[0] <= max_span_s:
            current.append(ts)
        else:
            batches.append(current)
            current = [ts]
    batches.append(current)
    return batches

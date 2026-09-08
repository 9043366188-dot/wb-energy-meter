"""Фоновые задачи: синхронизация серийников."""

from __future__ import annotations

import logging
import threading

log = logging.getLogger(__name__)


class BackgroundTasks:
    def __init__(self, registry, meters_repo, interval_s=60.0):
        self._registry = registry
        self._meters = meters_repo
        self._interval = interval_s
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        if self._thread: return
        self._thread = threading.Thread(target=self._run,
                                         name="bg-tasks", daemon=True)
        self._thread.start()
        log.info("BackgroundTasks запущены (интервал %.0f с)", self._interval)

    def stop(self):
        self._stop.set()
        if self._thread: self._thread.join(timeout=5.0)
        self._thread = None

    def _run(self):
        if self._stop.wait(10.0): return
        while not self._stop.is_set():
            try:
                self._sync_serials()
                self._sync_groups()
            except Exception as e:
                log.exception("BackgroundTasks error: %s", e)
            self._stop.wait(self._interval)

    def _sync_serials(self):
        for meter in self._registry.all():
            serial = meter.get_serial()
            if not serial: continue
            try:
                self._meters.update_serial_observed(meter.device_id, serial)
            except Exception as e:
                log.debug("sync_serials %s: %s", meter.device_id, e)

    def _sync_groups(self):
        """Периодическая пересинхронизация group/display_name БД ->
        in-memory реестр — страховка от рассинхрона по любой причине,
        не только точка push-обновления в api.py (A1 в ТЗ v0.8.0).

        apply_registry_config() создаёт отсутствующие MeterState для
        счётчиков, которые есть в БД, но ещё не появились в MQTT — это
        нормально, их статус остаётся UNKNOWN, пока не придут данные."""
        if self._meters is None: return
        try:
            rows = self._meters.list_all()
        except Exception as e:
            log.debug("sync_groups list_all: %s", e); return
        self._registry.apply_registry_config([
            {"device_id": m.device_id, "display_name": m.display_name,
             "group": m.group_name}
            for m in rows
        ])

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
            try: self._sync_serials()
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

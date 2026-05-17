"""Движок статусов."""

from __future__ import annotations

import logging
import threading
import time
from typing import Optional

from .config import StatusConfig
from .model import MeterRegistry, MeterState, MeterStatus, PHASES


log = logging.getLogger(__name__)


class StatusEngine:
    def __init__(self, registry, status_cfg, interval_s=2.0):
        self._registry = registry
        self._cfg = status_cfg
        self._interval = interval_s
        self._stop_event = threading.Event()
        self._thread = None

    def start(self):
        if self._thread: return
        self._thread = threading.Thread(target=self._run,
                                         name="status-engine", daemon=True)
        self._thread.start()
        log.info("StatusEngine запущен (интервал %.1f с)", self._interval)

    def stop(self):
        self._stop_event.set()
        if self._thread: self._thread.join(timeout=5.0)
        self._thread = None

    def _run(self):
        while not self._stop_event.is_set():
            try: self.recompute_all()
            except Exception as e: log.exception("recompute error: %s", e)
            self._stop_event.wait(self._interval)

    def recompute_all(self):
        for meter in self._registry.all():
            self._recompute_one(meter)

    def _recompute_one(self, meter):
        status, reason = self._classify(meter)
        with self._registry.lock():
            meter.status = status
            meter.status_reason = reason

    def _classify(self, meter):
        now = time.time()
        if meter.last_any_ts <= 0 or not meter.controls:
            return MeterStatus.UNKNOWN, "Нет данных от устройства"
        err_channels = [c.name for c in meter.controls.values() if c.error]
        if err_channels:
            preview = ", ".join(err_channels[:3])
            suffix = f" и ещё {len(err_channels) - 3}" if len(err_channels) > 3 else ""
            return MeterStatus.DEVICE_ERROR, f"Ошибки каналов: {preview}{suffix}"
        age = now - meter.last_any_ts
        if age > self._cfg.no_connection_timeout_s:
            return MeterStatus.NO_CONNECTION, f"Нет обновлений {_fmt_age(age)}"

        u = [meter.get_float(f"Urms {p}") for p in PHASES]
        i = [meter.get_float(f"Irms {p}") for p in PHASES]
        total_p = meter.get_float("Total P")
        total_ap = meter.get_float("Total AP energy")
        freq = meter.get_float("Frequency")

        has_any_u = any(v is not None and v > 1.0 for v in u)
        total_p_nonzero = total_p is not None and abs(total_p) > 0.001
        all_u_zero_or_none = all(v is None or v <= 1.0 for v in u)
        all_i_zero_or_none = all(v is None or v <= 0.001 for v in i)
        if all_u_zero_or_none and all_i_zero_or_none and not total_p_nonzero:
            device_alive = (
                (total_ap is not None and total_ap > 0)
                or meter.get_serial() is not None
            )
            if device_alive:
                return (MeterStatus.NO_MEASUREMENT,
                        "Связь с прибором есть, но измерения нулевые "
                        "(вероятно, нет силового подключения или нет нагрузки)")
            return (MeterStatus.NO_MEASUREMENT,
                    "Данные от прибора обнулены, идентификация не прочитана")

        lost_phases = []
        for name, val in zip(PHASES, u):
            if val is not None and val < self._cfg.phase_lost_v and has_any_u:
                if any(v is not None and v > self._cfg.phase_lost_v for v in u):
                    lost_phases.append(name)
        if lost_phases:
            return (MeterStatus.INCOMPLETE_MEASUREMENT,
                    f"Нет фазы: {', '.join(lost_phases)}")

        warnings = []
        for name, val in zip(PHASES, u):
            if val is None: continue
            if val < self._cfg.undervoltage_v and val >= self._cfg.phase_lost_v:
                warnings.append(f"{name}: пониженное {val:.0f} В")
            elif val > self._cfg.overvoltage_v:
                warnings.append(f"{name}: повышенное {val:.0f} В")
        if freq is not None and freq > 0.0:
            if freq < self._cfg.freq_min_hz or freq > self._cfg.freq_max_hz:
                warnings.append(f"Частота вне нормы: {freq:.2f} Гц")
        if age > self._cfg.stale_warning_timeout_s and not warnings:
            warnings.append(f"Данные обновлялись {_fmt_age(age)} назад")
        if warnings:
            return MeterStatus.WARNING, "; ".join(warnings)
        return MeterStatus.OK, ""


def _fmt_age(sec):
    if sec < 60: return f"{sec:.0f} с"
    if sec < 3600: return f"{sec/60:.1f} мин"
    return f"{sec/3600:.1f} ч"

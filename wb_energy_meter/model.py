"""Модели данных в памяти."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


PHASES = ("L1", "L2", "L3")

MAIN_CHANNELS: tuple[str, ...] = (
    "Total AP energy", "Total P", "Frequency", "Serial",
    "Urms L1", "Urms L2", "Urms L3",
    "Irms L1", "Irms L2", "Irms L3",
)

NUMERIC_CONTROL_TYPES = {
    "voltage", "current", "power", "power_consumption", "value",
}


class MeterStatus(str, Enum):
    OK = "ok"
    NO_CONNECTION = "no_connection"
    NO_MEASUREMENT = "no_measurement"
    INCOMPLETE_MEASUREMENT = "incomplete"
    WARNING = "warning"
    DEVICE_ERROR = "device_error"
    UNKNOWN = "unknown"

    @property
    def priority(self) -> int:
        return _STATUS_PRIORITY[self]


_STATUS_PRIORITY = {
    MeterStatus.UNKNOWN: 0,
    MeterStatus.OK: 1,
    MeterStatus.WARNING: 2,
    MeterStatus.INCOMPLETE_MEASUREMENT: 3,
    MeterStatus.NO_MEASUREMENT: 4,
    MeterStatus.NO_CONNECTION: 5,
    MeterStatus.DEVICE_ERROR: 6,
}


@dataclass
class ControlState:
    name: str
    value: Any = None
    raw_value: Optional[str] = None
    meta: dict = field(default_factory=dict)
    error: Optional[str] = None
    first_seen_ts: float = 0.0
    last_update_ts: float = 0.0
    update_count: int = 0

    @property
    def age_seconds(self) -> float:
        if self.last_update_ts <= 0:
            return -1.0
        return time.time() - self.last_update_ts

    @property
    def is_numeric(self) -> bool:
        return self.meta.get("type") in NUMERIC_CONTROL_TYPES

    def as_float(self) -> Optional[float]:
        if self.value is None:
            return None
        if isinstance(self.value, (int, float)):
            return float(self.value)
        if isinstance(self.value, str):
            try:
                return float(self.value)
            except ValueError:
                return None
        return None


@dataclass
class MeterState:
    device_id: str
    mqtt_name: Optional[str] = None
    driver: Optional[str] = None
    display_name: Optional[str] = None
    group: Optional[str] = None
    controls: dict[str, ControlState] = field(default_factory=dict)
    first_seen_ts: float = 0.0
    last_any_ts: float = 0.0
    status: MeterStatus = MeterStatus.UNKNOWN
    status_reason: str = ""

    def get(self, control: str) -> Optional[ControlState]:
        return self.controls.get(control)

    def get_float(self, control: str) -> Optional[float]:
        c = self.controls.get(control)
        return c.as_float() if c else None

    def get_serial(self) -> Optional[str]:
        c = self.controls.get("Serial")
        if c is None or c.value is None:
            return None
        return str(c.value).strip() or None

    @property
    def effective_name(self) -> str:
        return self.display_name or self.mqtt_name or self.device_id

    def to_api_dict(self) -> dict:
        main: dict[str, Any] = {}
        for ch in MAIN_CHANNELS:
            c = self.controls.get(ch)
            if c is None:
                main[ch] = None
            elif ch == "Serial":
                main[ch] = str(c.value) if c.value is not None else None
            else:
                main[ch] = c.as_float()
        return {
            "device_id": self.device_id,
            "display_name": self.effective_name,
            "mqtt_name": self.mqtt_name,
            "group": self.group,
            "serial": self.get_serial(),
            "status": self.status.value,
            "status_reason": self.status_reason,
            "last_update_ts": self.last_any_ts,
            "last_update_age_s": (
                time.time() - self.last_any_ts if self.last_any_ts > 0 else None
            ),
            "main_values": main,
            "controls_count": len(self.controls),
        }


class MeterRegistry:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._meters: dict[str, MeterState] = {}

    def get_or_create(self, device_id: str) -> MeterState:
        with self._lock:
            m = self._meters.get(device_id)
            if m is None:
                m = MeterState(device_id=device_id, first_seen_ts=time.time())
                self._meters[device_id] = m
            return m

    def get(self, device_id: str) -> Optional[MeterState]:
        with self._lock:
            return self._meters.get(device_id)

    def all(self) -> list[MeterState]:
        with self._lock:
            return sorted(self._meters.values(), key=lambda m: m.effective_name)

    def all_ids(self) -> list[str]:
        with self._lock:
            return list(self._meters.keys())

    def count(self) -> int:
        with self._lock:
            return len(self._meters)

    def apply_registry_config(self, entries: list[dict]) -> None:
        with self._lock:
            for entry in entries:
                did = entry["device_id"]
                m = self._meters.get(did)
                if m is None:
                    m = MeterState(device_id=did)
                    self._meters[did] = m
                m.display_name = entry.get("display_name")
                m.group = entry.get("group")

    def lock(self) -> threading.RLock:
        return self._lock

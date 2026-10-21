"""Общий стенд для тестов эмулятора счётчиков (партия находок 0.23.1, П12).

Не тест (имя без `test_` — раннер его не запускает), а набор деталей, из которых
tests/test_step67_emulator_api.py и tests/browser/test_b22_emulator.py собирают мир:

  Clock      — часы, которыми управляет тест (эмулятор и сервис живут по ним);
  Loopback   — «публикатор-петля»: всё, что публикует эмулятор, попадает в
               `MqttService` (реестр, как из брокера) и в журнал «истории»
               (как будто её пишет wb-mqtt-db);
  run        — прокрутить часы вперёд, тикая эмулятором;
  fill_aggregates — часовые итоги из «истории» БОЕВОЙ `compute_hourly_aggregate`,
               а не формулой теста.
"""

from __future__ import annotations

import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from wb_energy_meter.aggregates_repo import AggregateRepo  # noqa: E402
from wb_energy_meter.aggregator import compute_hourly_aggregate  # noqa: E402
from wb_energy_meter.wb_db_client import HistoryPoint  # noqa: E402

HOUR = 3600


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


class Loopback:
    """Публикатор-«петля»: сообщения эмулятора идут в реестр (как из брокера)
    и в журнал истории (как будто их пишет wb-mqtt-db)."""

    connected = True

    def __init__(self, mqtt, clock):
        self.mqtt, self.clock, self.history = mqtt, clock, {}

    def connect(self, timeout_s=5.0):
        return True

    def publish(self, topic, payload, retain=True, qos=1):
        self.mqtt._handle_message(topic, payload.encode("utf-8"), retain=False)
        parts = topic.split("/")
        if len(parts) == 5 and parts[3] == "controls" and payload != "":
            ctrl = parts[4]
            if ctrl == "Total AP energy" or ctrl.startswith("AP energy "):
                self.history.setdefault((parts[2], ctrl), []).append(
                    HistoryPoint(timestamp=self.clock.t, value=float(payload)))

    def flush(self, timeout_s=5.0):
        return True

    def close(self):
        pass


def run(emu, clock, seconds, step=60):
    end = clock.t + seconds
    while clock.t < end:
        clock.t += step
        emu.tick(clock.t)


def hour_points(loop, device, control, hour_start):
    """Точки истории с контекстом: одна ≤ начала часа и все до конца часа."""
    pts = loop.history.get((device, control), [])
    before = [p for p in pts if p.timestamp <= hour_start]
    inside = [p for p in pts if hour_start < p.timestamp <= hour_start + HOUR]
    return before[-1:] + inside


def fill_aggregates(loop, meters, device, profile, hours, t0):
    """Часовые итоги из «истории» эмулятора — боевой compute_hourly_aggregate.
    `hours` — номера часов от `t0`."""
    meter_id = meters.get_by_device_id(device).id
    control = "Total AP energy" if profile == "total_3p" else \
        "AP energy " + profile[-2:].upper()
    repo = AggregateRepo(meters._db).for_profile(profile)
    for h in hours:
        hs = t0 + h * HOUR
        agg = compute_hourly_aggregate(meter_id, hs, hour_points(loop, device, control, hs))
        repo.upsert(agg)

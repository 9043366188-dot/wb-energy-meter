#!/usr/bin/env python3
"""Шаг 66 (партия находок 0.23.1, П12): эмулятор счётчиков с НАСТОЯЩИМ
mosquitto на 127.0.0.1:1883 (раннер tests/run_all.py пропускает `*_daemon.py`,
если брокера нет).

Эмулятор публикует в брокер, обычный `MqttService` принимает это как приборы
(префикс `emu-` пропускается фильтром), `StatusEngine` считает статусы:
  1. включение: приборы появляются в реестре, у них название с «эмулятор»,
     драйвер эмулятора, серийный `EMU-…`, статус «ок» (живые значения);
  2. потеря связи: на каналах `meta/error = r`, статус «ошибка прибора»;
     по окончании ошибка снимается, статус возвращается;
  3. выключение: новый подписчик НЕ получает ни одного retained-сообщения
     `emu-…`, приборы забыты в реестре (кроме «привязанных»);
  4. перезапуск службы при включённом эмуляторе — приборы возвращаются, счётчик
     не меньше прежнего.

Запуск: python tests/test_step66_emulator_daemon.py
"""

import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import paho.mqtt.client as mqtt  # noqa: E402

from wb_energy_meter import emulator as em  # noqa: E402
from wb_energy_meter.config import StatusConfig  # noqa: E402
from wb_energy_meter.model import MeterRegistry, MeterStatus  # noqa: E402
from wb_energy_meter.mqtt_client import MqttService  # noqa: E402
from wb_energy_meter.status import StatusEngine  # noqa: E402

BROKER = ("127.0.0.1", 1883)


class Kv:
    def __init__(self):
        self.d = {}

    def get(self, key, default=None):
        return json.loads(self.d[key]) if key in self.d else default

    def set(self, key, value):
        self.d[key] = json.dumps(value, ensure_ascii=False)


class Meters:
    def __init__(self, bound=()):
        self.bound = set(bound)

    def get_by_device_id(self, did):
        return object() if did in self.bound else None


def wait_for(cond, timeout=8.0, step=0.05):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(step)
    return False


def retained_emulator_topics(window_s=1.5):
    """Что брокер хранит под `/devices/emu-…` — глазами нового подписчика."""
    got = []
    c = mqtt.Client(client_id="emu-probe-%d" % time.time_ns(), clean_session=True)
    # подстановка «emu-+» в MQTT недопустима: фильтруем сами
    c.on_connect = lambda cl, u, f, rc: cl.subscribe("/devices/+/#", qos=1)
    c.on_message = lambda cl, u, m: got.append((m.topic, bool(m.retain), m.payload)) \
        if m.topic.startswith("/devices/emu-") else None
    c.connect(*BROKER, 5)
    c.loop_start()
    time.sleep(window_s)
    c.loop_stop()
    c.disconnect()
    return got


def new_service(kv, registry, bound=()):
    return em.EmulatorService(
        kv, publisher_factory=lambda: em.PahoPublisher(*BROKER),
        registry=registry, meters_repo=Meters(bound), tick_s=0.3,
        utc_offset_s=0)


def main():
    assert not retained_emulator_topics(0.8), \
        "в брокере остались retained-топики emu-… от прежнего запуска"
    registry = MeterRegistry()
    engine = StatusEngine(registry, StatusConfig())
    mqtt_svc = MqttService(BROKER[0], BROKER[1], registry, "wb-map3e_")
    assert mqtt_svc.start(connect_timeout=5.0), "нет подключения к mosquitto"
    kv = Kv()
    svc = new_service(kv, registry, bound=["emu-map3e-2"])
    try:
        made = svc.add_preset()
        ids = [d.device_id for d in made]
        svc.start()
        svc.set_enabled(True)

        # 1. приборы в реестре как обычные счётчики с меткой эмулятора
        assert wait_for(lambda: all(
            registry.get(i) is not None and len(registry.get(i).controls) >= len(em.CONTROL_NAMES)
            for i in ids)), "эмулятор не появился в реестре"
        assert wait_for(lambda: all(
            registry.get(i).get_float("Total P") is not None and
            registry.get(i).last_any_ts > 0 for i in ids))
        time.sleep(0.6)
        engine.recompute_all()
        for d in made:
            m = registry.get(d.device_id)
            assert "эмулятор" in (m.mqtt_name or "").lower(), m.mqtt_name
            assert m.driver == em.EMU_DRIVER
            assert m.get_serial() == "EMU-%d-0" % d.n, m.get_serial()
            assert m.status == MeterStatus.OK, (d.device_id, m.status, m.status_reason)
        inp = registry.get(ids[3])
        loads_p = sum(registry.get(i).get_float("Total P") for i in ids[:3])
        assert abs(inp.get_float("Total P") / loads_p - 1.03) < 0.01, \
            (inp.get_float("Total P"), loads_p)
        print("[OK] 1. приборы в реестре: «эмулятор» в названии, драйвер эмулятора, "
              "серийный EMU-…, статус ok; ввод = сумма нагрузок + 3 %")

        # 2. потеря связи
        first = registry.get(ids[0])
        e0 = first.get_float("Total AP energy")
        svc.apply_fault(1, "loss", 1)
        svc._devices[1].loss_until = time.time() + 1.5       # не ждать минуту
        assert wait_for(lambda: all(c.error == "r" for c in first.controls.values())), \
            "meta/error = r не дошёл"
        engine.recompute_all()
        assert first.status == MeterStatus.DEVICE_ERROR, (first.status, first.status_reason)
        assert wait_for(lambda: not any(c.error for c in first.controls.values()),
                        timeout=8), "ошибка не снята"
        assert wait_for(lambda: first.get_float("Total AP energy") > e0, timeout=8)
        engine.recompute_all()
        assert first.status == MeterStatus.OK, (first.status, first.status_reason)
        print("[OK] 2. потеря связи: meta/error = r → «ошибка прибора», потом снята, "
              "статус ok, энергия не меньше прежней")

        # 4. перезапуск службы при включённом эмуляторе (до выключения)
        before = first.get_float("Total AP energy")
        svc.stop()
        svc2 = new_service(kv, registry, bound=["emu-map3e-2"])
        assert svc2.is_enabled() and len(svc2.snapshot()["devices"]) == 4
        svc2.start()
        assert wait_for(lambda: svc2.snapshot()["devices"][0]["energy_total_kwh"] >= before)
        time.sleep(0.8)
        assert first.get_float("Total AP energy") >= before
        svc = svc2
        print("[OK] 4. перезапуск службы при включённом эмуляторе: счётчик не меньше прежнего")

        # 3. выключение
        assert retained_emulator_topics(0.8), "до выключения retained должен быть"
        svc.set_enabled(False)
        left = retained_emulator_topics(1.5)
        assert left == [], "retained не стёрт: %r" % [t for t, _, _ in left][:5]
        assert wait_for(lambda: all(registry.get(i) is None for i in
                                    (ids[0], ids[2], ids[3])), timeout=3), \
            "приборы без привязки остались в реестре"
        assert registry.get(ids[1]) is not None, "привязанный прибор должен остаться"
        assert svc.snapshot()["enabled"] is False
        print("[OK] 3. выключение: retained стёрт (новый подписчик не получил ни одного "
              "сообщения), приборы без привязки забыты, привязанный остался")
    finally:
        try:
            svc.set_enabled(False)
        except Exception:
            pass
        svc.stop()
        mqtt_svc.stop()
    print("[OK] шаг 66 (daemon): эмулятор в настоящем брокере")


if __name__ == "__main__":
    main()

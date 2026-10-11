#!/usr/bin/env python3
"""Партия 12, этап 12.4: A16 — retained-сообщения после перезапуска демона
(решение Кира от 10.10.2026, docs/TZ-finish-plan.md §10 / 12.4).

С НАСТОЯЩИМ mosquitto на 127.0.0.1:1883 (как test_step3_daemon.py; раннер
tests/run_all.py пропускает файлы `*_daemon.py`, если брокера нет).

Сценарий:
  1. До старта клиента брокер уже хранит retained-значения прибора (как после
     перезапуска демона: прибор давно молчит, а брокер помнит последнее).
  2. Клиент стартует и подписывается. Значения принимаются и ПОКАЗЫВАЮТСЯ
     (энергия 110, напряжения 230), но прибор не «свежий»: `last_any_ts`,
     возраст каналов и `last_measurement_ts` не двигаются, статус —
     `unconfirmed` («не подтверждено»), а не OK.
  3. Живое (не retained) сообщение прибора (Uptime) — статус становится
     обычным (OK); `awaiting_live` снят.
  4. Если живого сообщения нет дольше обычного таймаута связи — `no_connection`,
     как и раньше (имитируем сдвигом момента прихода retained).
  5. Прибор, который уже был живым, не «оживает» от повторной доставки
     retained после переподключения (возраст канала и last_any_ts остаются).

A15 (Uptime жив при неизменной энергии) и A38 (never_seen) проверяют
test_step49 — они не должны ломаться (запускаются в общем наборе).

Запуск: python tests/test_step57_retained_a16_daemon.py
"""

import os
import sys
import time
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import paho.mqtt.client as mqtt  # noqa: E402

from wb_energy_meter.config import StatusConfig  # noqa: E402
from wb_energy_meter.model import MeterRegistry, MeterStatus  # noqa: E402
from wb_energy_meter.mqtt_client import MqttService  # noqa: E402
from wb_energy_meter.status import StatusEngine  # noqa: E402

BROKER = ("127.0.0.1", 1883)


def wait_for(cond, timeout=6.0, step=0.05):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(step)
    return False


def main():
    suffix = uuid.uuid4().hex[:6]
    dev = f"wb-map3e_a16{suffix}"
    prefix = f"wb-map3e_a16{suffix}"
    base = f"/devices/{dev}"
    retained = {
        f"{base}/meta/name": "Прибор A16",
        f"{base}/controls/Serial": "16818659",
        f"{base}/controls/Total AP energy": "110.0",
        f"{base}/controls/Total P": "1.5",
        f"{base}/controls/Frequency": "50.0",
        f"{base}/controls/Urms L1": "230", f"{base}/controls/Urms L2": "230",
        f"{base}/controls/Urms L3": "230",
        f"{base}/controls/Irms L1": "2.0", f"{base}/controls/Irms L2": "2.0",
        f"{base}/controls/Irms L3": "2.0",
        f"{base}/controls/Uptime": "3600",
    }

    pub = mqtt.Client(client_id=f"a16-pub-{suffix}")
    pub.connect(*BROKER, 5)
    pub.loop_start()
    svc = None
    try:
        # 1. брокер помнит значения ДО старта клиента
        for topic, payload in retained.items():
            pub.publish(topic, payload, qos=1, retain=True).wait_for_publish(5)

        registry = MeterRegistry()
        cfg = StatusConfig()
        engine = StatusEngine(registry, cfg)
        svc = MqttService(BROKER[0], BROKER[1], registry, prefix)
        assert svc.start(connect_timeout=5.0), "нет подключения к mosquitto"

        # 2. значения пришли, но прибор не подтверждён
        assert wait_for(lambda: registry.get(dev) is not None
                        and registry.get(dev).get_float("Urms L3") == 230.0), \
            "retained-значения не дошли"
        assert wait_for(lambda: len(registry.get(dev).controls) >= 9)
        time.sleep(0.4)
        meter = registry.get(dev)
        engine.recompute_all()
        assert meter.get_float("Total AP energy") == 110.0, "значение не показано"
        assert meter.last_any_ts == 0.0, "retained сделал прибор «свежим»"
        assert meter.last_measurement_ts == 0.0
        assert meter.awaiting_live is True and meter.last_retained_ts > 0
        assert all(c.last_update_ts == 0.0 for c in meter.controls.values()
                   if c.name in ("Total AP energy", "Urms L1")), "возраст канала обновлён retained"
        assert meter.status == MeterStatus.UNCONFIRMED, (meter.status, meter.status_reason)
        assert meter.status.value == "unconfirmed" and "retained" in meter.status_reason
        print("[OK] 2. retained принят и показан (энергия 110, U 230), last_any_ts не тронут, "
              "статус unconfirmed «не подтверждено», а не OK")

        # 4. живого сообщения нет дольше таймаута связи -> no_connection (как раньше)
        saved = meter.last_retained_ts
        meter.last_retained_ts = saved - (cfg.no_connection_timeout_s + 5)
        engine.recompute_all()
        assert meter.status == MeterStatus.NO_CONNECTION, (meter.status, meter.status_reason)
        meter.last_retained_ts = saved
        engine.recompute_all()
        assert meter.status == MeterStatus.UNCONFIRMED
        print("[OK] 4. нет живого сообщения дольше no_connection_timeout_s -> no_connection")

        # 3. живое (не retained) сообщение Uptime -> обычная классификация
        def live_seen():
            pub.publish(f"{base}/controls/Uptime", "3660", qos=0, retain=False)
            time.sleep(0.15)
            return meter.last_any_ts > 0
        assert wait_for(live_seen, timeout=6.0, step=0.01), "живое сообщение не дошло"
        engine.recompute_all()
        assert meter.awaiting_live is False
        assert meter.status == MeterStatus.OK, (meter.status, meter.status_reason)
        live_ts = meter.last_any_ts
        print("[OK] 3. живое сообщение (Uptime) снимает «не подтверждено» -> статус ok")

        # 5. повторная доставка retained при переподключении живость не продлевает
        svc.stop()
        time.sleep(0.3)
        svc = MqttService(BROKER[0], BROKER[1], registry, prefix)
        assert svc.start(connect_timeout=5.0)
        time.sleep(1.0)       # брокер заново отдал все retained
        assert meter.last_any_ts == live_ts, "повторная retained-доставка продлила «жизнь» прибора"
        assert meter.awaiting_live is False
        print("[OK] 5. повторная доставка retained после переподключения last_any_ts не двигает")

        print("\nA16 (Шаг 57) — все проверки пройдены.")
        return 0
    finally:
        if svc is not None:
            svc.stop()
        for topic in retained:                       # убрать retained из брокера
            pub.publish(topic, "", qos=0, retain=True)
        time.sleep(0.3)
        pub.loop_stop()
        pub.disconnect()


if __name__ == "__main__":
    sys.exit(main())

"""Тест партии 15 (находка гейта): пустое MQTT-сообщение не создаёт прибор в реестре.

Самостоятельный скрипт (не pytest), брокер не нужен:
    python tests/test_step78_mqtt_empty_payload.py

Почему это важно. Выключая эмулятор, служба стирает retained-топики пустыми сообщениями
и сразу после этого забывает приборы в реестре (`registry.forget`). Работающий подписчик
(`MqttService`) получает эти же пустые сообщения; если они доходят уже ПОСЛЕ `forget`
(под нагрузкой процессора), раньше `get_or_create` создавал прибор заново, и «призрак»
выключенного эмулятора оставался в реестре до перезапуска службы. Так падал
`test_step66_emulator_daemon.py` (воспроизведено под нагрузкой: 5 падений из 25 прогонов).

Проверяется:
  * пустое сообщение о неизвестном приборе (эмулятор и обычный прибор; значение, `meta`,
    `meta/error`) прибор не создаёт;
  * первое непустое сообщение прибор создаёт, как раньше;
  * для известного прибора пустое значение обрабатывается как раньше (снятие `meta/error`);
  * точная последовательность гонки: прибор создан → `forget` → запоздавшие стирающие
    сообщения по всем топикам прибора → в реестре его нет.
"""

from __future__ import annotations

import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from wb_energy_meter import emulator as em
from wb_energy_meter.model import MeterRegistry
from wb_energy_meter.mqtt_client import MqttService


def make():
    registry = MeterRegistry()
    return registry, MqttService("127.0.0.1", 1883, registry, "wb-")


def test_empty_message_does_not_create_device():
    registry, svc = make()
    for did in ("emu-map3e-4", "wb-map3e_77"):
        for topic in ("/devices/%s/controls/Total P" % did,
                      "/devices/%s/controls/Total P/meta" % did,
                      "/devices/%s/controls/Total P/meta/error" % did,
                      "/devices/%s/meta" % did,
                      "/devices/%s/meta/name" % did):
            for retain in (False, True):
                svc._handle_message(topic, b"", retain=retain)
        assert registry.get(did) is None, "пустые сообщения создали прибор %s" % did
    assert registry.all() == [], registry.all()
    print("[OK] пустое сообщение о неизвестном приборе (эмулятор и обычный) прибор не создаёт")


def test_first_nonempty_message_creates_device():
    registry, svc = make()
    svc._handle_message("/devices/emu-map3e-1/controls/Total P", b"1.5", retain=False)
    svc._handle_message("/devices/wb-map3e_77/controls/Total P", b"2.5", retain=True)
    emu, real = registry.get("emu-map3e-1"), registry.get("wb-map3e_77")
    assert emu is not None and emu.get_float("Total P") == 1.5
    assert real is not None and real.get_float("Total P") == 2.5
    assert real.awaiting_live is True and emu.awaiting_live is False
    # чужие префиксы по-прежнему игнорируются
    svc._handle_message("/devices/other-device/controls/X", b"1", retain=False)
    assert registry.get("other-device") is None
    print("[OK] первое непустое сообщение создаёт прибор, чужой префикс игнорируется")


def test_known_device_empty_message_works_as_before():
    registry, svc = make()
    svc._handle_message("/devices/wb-map3e_77/controls/Total P", b"2.5", retain=False)
    svc._handle_message("/devices/wb-map3e_77/controls/Total P/meta/error", b"r", retain=False)
    ctrl = registry.get("wb-map3e_77").controls["Total P"]
    assert ctrl.error == "r", ctrl.error
    svc._handle_message("/devices/wb-map3e_77/controls/Total P/meta/error", b"", retain=False)
    assert ctrl.error is None, "пустой meta/error должен снимать ошибку у известного прибора"
    assert registry.get("wb-map3e_77") is not None
    print("[OK] известный прибор: пустой meta/error снимает ошибку, как раньше")


def test_late_erase_messages_after_forget_leave_no_ghost():
    registry, svc = make()
    did = "emu-map3e-4"
    svc._handle_message("/devices/%s/controls/Total P" % did, b"1.0", retain=False)
    assert registry.get(did) is not None
    # выключение эмулятора: служба забывает прибор, а стирающие сообщения доходят позже
    assert registry.forget(did) is True
    for topic in em.device_topics(did):
        svc._handle_message(topic, b"", retain=False)
    assert registry.get(did) is None, "запоздавшие стирающие сообщения вернули прибор в реестр"
    print("[OK] запоздавшие стирающие сообщения после forget не возвращают прибор")


if __name__ == "__main__":
    test_empty_message_does_not_create_device()
    test_first_nonempty_message_creates_device()
    test_known_device_empty_message_works_as_before()
    test_late_erase_messages_after_forget_leave_no_ghost()
    print("[ALL OK] test_step78_mqtt_empty_payload")

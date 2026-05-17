"""MQTT-клиент, парсер WB Conventions."""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import uuid
from typing import Callable, Optional

import paho.mqtt.client as mqtt

from .model import ControlState, MeterRegistry, MeterState


log = logging.getLogger(__name__)

TOPIC_RE = re.compile(r"^/devices/([^/]+)/(meta|controls)(?:/(.+))?$")


class MqttService:
    def __init__(self, broker, port, registry, device_prefix,
                 keepalive=30, username=None, password=None,
                 client_id_prefix="wb-energy-meter",
                 on_connect_change=None):
        self._broker = broker
        self._port = port
        self._registry = registry
        self._device_prefix = device_prefix
        self._keepalive = keepalive
        self._on_connect_change = on_connect_change
        client_id = f"{client_id_prefix}-{uuid.uuid4().hex[:8]}"
        self._client = mqtt.Client(client_id=client_id, clean_session=True)
        if username:
            self._client.username_pw_set(username, password or "")
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message
        self._connected = threading.Event()
        self._stopped = False
        self._msg_count = 0
        self._error_count = 0

    def start(self, connect_timeout=5.0):
        try:
            self._client.connect_async(self._broker, self._port, self._keepalive)
        except Exception as e:
            log.error("MQTT connect_async failed: %s", e)
            return False
        self._client.loop_start()
        ok = self._connected.wait(timeout=connect_timeout)
        if not ok:
            log.warning("MQTT: нет подключения к %s:%d за %.1f сек",
                        self._broker, self._port, connect_timeout)
        return ok

    def stop(self):
        self._stopped = True
        try:
            self._client.loop_stop()
            self._client.disconnect()
        except Exception:
            pass

    @property
    def is_connected(self):
        return self._connected.is_set()

    @property
    def message_count(self):
        return self._msg_count

    @property
    def error_count(self):
        return self._error_count

    def _on_connect(self, client, userdata, flags, rc):
        if rc != 0:
            log.error("MQTT connect failed rc=%d", rc)
            return
        log.info("MQTT: подключён к %s:%d", self._broker, self._port)
        self._connected.set()
        if self._on_connect_change:
            try: self._on_connect_change(True)
            except Exception: pass
        for t in ("/devices/+/meta", "/devices/+/meta/+",
                  "/devices/+/controls/+",
                  "/devices/+/controls/+/meta",
                  "/devices/+/controls/+/meta/+"):
            client.subscribe(t, qos=0)

    def _on_disconnect(self, client, userdata, rc):
        self._connected.clear()
        if rc == 0:
            log.info("MQTT: штатно отключён")
        else:
            log.warning("MQTT: отключение rc=%d", rc)
        if self._on_connect_change:
            try: self._on_connect_change(False)
            except Exception: pass

    def _on_message(self, client, userdata, msg):
        self._msg_count += 1
        try:
            self._handle_message(msg.topic, msg.payload)
        except Exception as e:
            self._error_count += 1
            log.debug("MQTT handle error (topic=%s): %s", msg.topic, e)

    def _handle_message(self, topic, payload):
        m = TOPIC_RE.match(topic)
        if not m: return
        device_id, section, rest = m.group(1), m.group(2), m.group(3)
        if not device_id.startswith(self._device_prefix):
            return
        try:
            text = payload.decode("utf-8", errors="replace")
        except Exception:
            text = ""
        meter = self._registry.get_or_create(device_id)
        now = time.time()
        meter.last_any_ts = now
        if meter.first_seen_ts == 0.0:
            meter.first_seen_ts = now
        if section == "meta":
            self._handle_device_meta(meter, rest, text)
            return
        if rest is None:
            return
        parts = rest.split("/")
        control_name = parts[0]
        ctrl = meter.controls.get(control_name)
        if ctrl is None:
            ctrl = ControlState(name=control_name, first_seen_ts=now)
            meter.controls[control_name] = ctrl
        if len(parts) == 1:
            self._handle_control_value(ctrl, text)
            return
        if parts[1] == "meta":
            if len(parts) == 2:
                self._handle_control_meta_json(ctrl, text)
            elif len(parts) == 3:
                self._handle_control_meta_field(ctrl, parts[2], text)

    def _handle_device_meta(self, meter, rest, text):
        if rest is None:
            try:
                obj = json.loads(text)
            except json.JSONDecodeError:
                return
            if not isinstance(obj, dict): return
            name = _extract_localized(obj.get("title") or obj.get("name"))
            if name: meter.mqtt_name = name
            driver = obj.get("driver")
            if isinstance(driver, str): meter.driver = driver
            return
        if rest == "name":
            meter.mqtt_name = text.strip() or meter.mqtt_name
        elif rest == "driver":
            meter.driver = text.strip() or meter.driver

    def _handle_control_value(self, ctrl, text):
        now = time.time()
        ctrl.raw_value = text
        if ctrl.name == "Serial" or ctrl.meta.get("type") == "text":
            ctrl.value = text.strip()
        else:
            ctrl.value = _parse_float_safe(text)
        ctrl.last_update_ts = now
        ctrl.update_count += 1

    def _handle_control_meta_json(self, ctrl, text):
        try:
            obj = json.loads(text)
        except json.JSONDecodeError:
            return
        if not isinstance(obj, dict): return
        if "readonly" in obj:
            obj["readonly"] = bool(obj["readonly"])
        if "title" in obj and isinstance(obj["title"], dict):
            obj["title"] = _extract_localized(obj["title"]) or ""
        ctrl.meta.update(obj)

    def _handle_control_meta_field(self, ctrl, field_name, text):
        if field_name == "error":
            ctrl.error = text.strip() or None
            return
        if field_name == "precision":
            ctrl.meta["precision"] = _parse_float_safe(text)
            return
        if field_name in ("readonly", "order"):
            v = _parse_float_safe(text)
            if v is not None:
                ctrl.meta[field_name] = bool(v) if field_name == "readonly" else int(v)
            return
        ctrl.meta.setdefault(field_name, text)


def _parse_float_safe(text):
    s = text.strip()
    if not s: return None
    s = s.replace(",", ".")
    try: return float(s)
    except ValueError: return None


def _extract_localized(v):
    if v is None: return None
    if isinstance(v, str): return v.strip() or None
    if isinstance(v, dict):
        for key in ("ru", "en"):
            if key in v and isinstance(v[key], str) and v[key].strip():
                return v[key].strip()
        for val in v.values():
            if isinstance(val, str) and val.strip():
                return val.strip()
    return None

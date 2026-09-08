"""Клиент к wb-mqtt-db через MQTT-RPC."""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Optional

import paho.mqtt.client as mqtt


log = logging.getLogger(__name__)


class RpcError(Exception): pass
class RpcTimeout(RpcError): pass
class RpcConnectError(RpcError): pass
class RpcRemoteError(RpcError):
    def __init__(self, payload):
        self.payload = payload
        super().__init__(f"RPC error: {payload}")


@dataclass
class HistoryChannel:
    device: str
    control: str
    items: int
    last_ts: int

    @property
    def key(self): return f"{self.device}/{self.control}"


@dataclass
class HistoryPoint:
    timestamp: float
    value: float

    def __repr__(self):
        return f"<HistoryPoint t={self.timestamp:.1f} v={self.value}>"


class WbDbClient:
    SERVICE = "db_logger"
    OBJECT = "history"

    def __init__(self, broker="127.0.0.1", port=1883, username=None,
                 password=None, default_timeout_s=10.0,
                 client_id_prefix="wb-energy-meter-rpc"):
        self._broker = broker
        self._port = port
        self._username = username
        self._password = password
        self._default_timeout = default_timeout_s
        self._client_id_prefix = client_id_prefix

    def get_channels(self, timeout_s=None):
        result = self._call("get_channels", {}, timeout_s)
        channels_dict = (result or {}).get("channels") or {}
        out = []
        for full_key, info in channels_dict.items():
            if "/" not in full_key: continue
            device, control = full_key.split("/", 1)
            out.append(HistoryChannel(
                device=device, control=control,
                items=int(info.get("items", 0) or 0),
                last_ts=int(info.get("last_ts", 0) or 0),
            ))
        return out

    def get_values(self, device, control, ts_from=None, ts_to=None,
                   limit=10000, min_interval_ms=None, timeout_s=None):
        params = {
            "channels": [[device, control]],
            # ver=1 — короткие имена полей ответа ("t"/"v"), их и парсим
            # ниже. По умолчанию (ver=0) сервис отвечает полями
            # "timestamp"/"value", и без этого флага все точки молча
            # отбраковывались как будто истории нет вовсе.
            "ver": 1,
            "limit": int(limit),
        }
        ts_filter = {}
        if ts_from is not None:
            ts_filter["gt"] = float(ts_from) - 1.0
        if ts_to is not None:
            ts_filter["lt"] = float(ts_to) + 1.0
        if ts_filter:
            params["timestamp"] = ts_filter
        if min_interval_ms is not None:
            params["min_interval"] = int(min_interval_ms)

        result = self._call("get_values", params, timeout_s)
        raw_points = (result or {}).get("values") or []
        out = []
        for p in raw_points:
            try:
                t = p.get("t")
                v = p.get("v")
                if t is None or v is None: continue
                t_float = float(t)
                if t_float > 1e12:
                    t_float = t_float / 1000.0
                try: v_float = float(v)
                except (TypeError, ValueError): continue
                out.append(HistoryPoint(timestamp=t_float, value=v_float))
            except Exception as e:
                log.debug("get_values skip %r: %s", p, e)
        out.sort(key=lambda x: x.timestamp)
        return out

    def _call(self, method, params, timeout_s=None):
        timeout = timeout_s if timeout_s is not None else self._default_timeout
        request_id = uuid.uuid4().hex[:12]
        client_id = f"{self._client_id_prefix}-{uuid.uuid4().hex[:8]}"
        request_topic = f"/rpc/v1/{self.SERVICE}/{self.OBJECT}/{method}/{client_id}"
        reply_topic = request_topic + "/reply"

        reply_received = threading.Event()
        reply_payload = {}
        connect_event = threading.Event()
        sub_event = threading.Event()
        connect_rc = [-1]

        def on_connect(c, userdata, flags, rc):
            connect_rc[0] = rc
            connect_event.set()
            if rc == 0:
                c.subscribe(reply_topic, qos=1)

        def on_subscribe(c, userdata, mid, granted_qos):
            sub_event.set()

        def on_message(c, userdata, msg):
            if msg.topic != reply_topic: return
            try:
                payload = json.loads(msg.payload.decode("utf-8",
                                                        errors="replace"))
            except json.JSONDecodeError as e:
                reply_payload["__parse_error__"] = str(e)
                reply_received.set(); return
            if payload.get("id") != request_id: return
            reply_payload.update(payload)
            reply_received.set()

        client = mqtt.Client(client_id=client_id, clean_session=True)
        if self._username:
            client.username_pw_set(self._username, self._password or "")
        client.on_connect = on_connect
        client.on_subscribe = on_subscribe
        client.on_message = on_message

        try:
            client.connect(self._broker, self._port, keepalive=15)
        except Exception as e:
            raise RpcConnectError(
                f"Не удалось подключиться к {self._broker}:{self._port}: {e}"
            ) from e

        client.loop_start()
        try:
            if not connect_event.wait(timeout=min(5.0, timeout)):
                raise RpcConnectError("Таймаут подключения к брокеру")
            if connect_rc[0] != 0:
                raise RpcConnectError(
                    f"MQTT connect rc={connect_rc[0]}")
            if not sub_event.wait(timeout=2.0):
                raise RpcError("Подписка не подтверждена")

            payload_bytes = json.dumps({
                "id": request_id, "params": params,
            }).encode("utf-8")
            info = client.publish(request_topic, payload_bytes, qos=1)
            # Не используем wait_for_publish(timeout=...): параметр timeout
            # появился в paho-mqtt 1.5.1, а на контроллере пакет ставится из
            # apt (python3-paho-mqtt) и может быть старее — тогда падает
            # "wait_for_publish() got an unexpected keyword argument 'timeout'".
            # is_published() есть во всех версиях, опрашиваем вручную с дедлайном.
            publish_deadline = time.time() + 5.0
            while not info.is_published():
                if time.time() >= publish_deadline:
                    raise RpcError("Таймаут публикации запроса в MQTT")
                time.sleep(0.02)

            deadline = time.time() + timeout
            remaining = deadline - time.time()
            if not reply_received.wait(timeout=max(0.1, remaining)):
                raise RpcTimeout(
                    f"Нет ответа от wb-mqtt-db на {method} за {timeout:.1f} с")
        finally:
            try:
                client.loop_stop(); client.disconnect()
            except Exception: pass

        if "__parse_error__" in reply_payload:
            raise RpcError(f"Не разобрал JSON: "
                           f"{reply_payload['__parse_error__']}")
        err = reply_payload.get("error")
        if err:
            raise RpcRemoteError(err)
        return reply_payload.get("result") or {}

"""E2E с реальным брокером и фейковым wb-mqtt-db."""

import json, os, sys, threading, time, uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import paho.mqtt.client as mqtt

from wb_energy_meter.wb_db_client import (
    RpcRemoteError, RpcTimeout, WbDbClient,
)


BROKER = "127.0.0.1"
PORT = 1883


class FakeWbMqttDb:
    def __init__(self):
        self.channels_db = {}
        self.values_db = {}
        self.fail_with_error = None
        self.silent = False
        self._client_id = f"fake-{uuid.uuid4().hex[:6]}"
        self._client = mqtt.Client(client_id=self._client_id, clean_session=True)
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message

    def start(self):
        self._client.connect(BROKER, PORT, 15)
        self._client.loop_start()
        time.sleep(0.3)

    def stop(self):
        try:
            self._client.loop_stop(); self._client.disconnect()
        except Exception: pass

    def _on_connect(self, c, u, f, rc):
        if rc == 0:
            c.subscribe("/rpc/v1/db_logger/history/+/+", qos=1)

    def _on_message(self, c, u, msg):
        if self.silent: return
        topic = msg.topic
        parts = topic.split("/")
        if len(parts) < 6: return
        method = parts[5]
        try: payload = json.loads(msg.payload.decode("utf-8"))
        except Exception: return
        request_id = payload.get("id", "")
        params = payload.get("params") or {}
        reply_topic = topic + "/reply"

        if self.fail_with_error is not None:
            reply = {"id": request_id, "result": None,
                     "error": self.fail_with_error}
        elif method == "get_channels":
            reply = {"id": request_id, "error": None,
                     "result": {"channels": self.channels_db}}
        elif method == "get_values":
            channels = params.get("channels") or []
            ts = params.get("timestamp") or {}
            gt = ts.get("gt", float("-inf"))
            lt = ts.get("lt", float("inf"))
            limit = params.get("limit", 10000)
            values = []
            for idx, (dev, ctrl) in enumerate(channels):
                pts = self.values_db.get((dev, ctrl), [])
                for t, v in pts:
                    if gt < t < lt:
                        values.append({"c": idx, "t": int(t * 1000), "v": v})
                        if len(values) >= limit: break
            reply = {"id": request_id, "error": None,
                     "result": {"values": values}}
        else:
            reply = {"id": request_id, "result": None,
                     "error": {"message": f"unknown {method}"}}
        c.publish(reply_topic, json.dumps(reply), qos=1)


def test_get_channels():
    fake = FakeWbMqttDb()
    fake.channels_db = {
        "wb-map3e_16/Total AP energy": {"items": 0, "last_ts": 1700000000},
        "wb-map3e_16/Urms L1":          {"items": 100, "last_ts": 1700001000},
        "metrics/cpu":                  {"items": 1000, "last_ts": 1700003000},
    }
    fake.start()
    try:
        c = WbDbClient(broker=BROKER, port=PORT, default_timeout_s=5.0)
        chans = c.get_channels(timeout_s=5.0)
        assert len(chans) == 3
        ap = next(x for x in chans if x.key == "wb-map3e_16/Total AP energy")
        assert ap.items == 0
        print("[OK] get_channels")
    finally:
        fake.stop()


def test_get_values():
    fake = FakeWbMqttDb()
    fake.values_db[("wb-map3e_16", "Total AP energy")] = [
        (1700000000.0, 100.0),
        (1700000600.0, 100.5),
        (1700001200.0, 101.2),
    ]
    fake.start()
    try:
        c = WbDbClient(broker=BROKER, port=PORT, default_timeout_s=5.0)
        pts = c.get_values("wb-map3e_16", "Total AP energy",
                           ts_from=1699999000.0, ts_to=1700002000.0,
                           timeout_s=5.0)
        assert len(pts) == 3
        assert pts[0].value == 100.0
        assert pts[0].timestamp == 1700000000.0
        print("[OK] get_values")
    finally:
        fake.stop()


def test_remote_error():
    fake = FakeWbMqttDb()
    fake.fail_with_error = {"code": -32000, "message": "synthetic"}
    fake.start()
    try:
        c = WbDbClient(broker=BROKER, port=PORT, default_timeout_s=3.0)
        try:
            c.get_channels(timeout_s=3.0); assert False
        except RpcRemoteError as e:
            assert "synthetic" in str(e)
        print("[OK] remote error")
    finally:
        fake.stop()


def test_timeout():
    fake = FakeWbMqttDb()
    fake.silent = True
    fake.start()
    try:
        c = WbDbClient(broker=BROKER, port=PORT, default_timeout_s=2.0)
        try:
            c.get_channels(timeout_s=2.0); assert False
        except RpcTimeout: pass
        print("[OK] timeout")
    finally:
        fake.stop()


def test_consumption_with_fake():
    from wb_energy_meter.consumption import ConsumptionService
    from wb_energy_meter.periods import Period

    fake = FakeWbMqttDb()
    fake.values_db[("wb-map3e_16", "Total AP energy")] = [
        (1700000000.0, 100.0),
        (1700001800.0, 105.0),
        (1700003600.0, 110.0),
    ]
    fake.start()
    try:
        c = WbDbClient(broker=BROKER, port=PORT, default_timeout_s=5.0)
        svc = ConsumptionService(c)
        period = Period(ts_from=1700000000.0, ts_to=1700003600.0,
                        label="custom", description="1h")
        r = svc.calculate("wb-map3e_16", period)
        assert r.consumption_kwh == 10.0
        assert r.quality == "ok"
        print("[OK] consumption with fake")
    finally:
        fake.stop()


def test_consumption_no_history():
    """Кейс пользователя: канал в registry есть, но в истории 0 точек."""
    from wb_energy_meter.consumption import ConsumptionService
    from wb_energy_meter.periods import Period

    fake = FakeWbMqttDb()
    fake.start()
    try:
        c = WbDbClient(broker=BROKER, port=PORT, default_timeout_s=5.0)
        svc = ConsumptionService(c)
        period = Period(ts_from=1700000000.0, ts_to=1700003600.0,
                        label="custom", description="1h")
        r = svc.calculate("wb-map3e_16", period)
        assert r.consumption_kwh is None
        assert r.quality == "no_data"
        print("[OK] consumption empty history")
    finally:
        fake.stop()


if __name__ == "__main__":
    test_get_channels()
    test_get_values()
    test_remote_error()
    test_timeout()
    test_consumption_with_fake()
    test_consumption_no_history()
    print("\nВсе e2e тесты Шага 3 пройдены.")

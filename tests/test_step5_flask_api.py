"""Тесты Шага 5: Flask API через test_client (без сети)."""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wb_energy_meter.api import create_app, _AppState


class FakeControl:
    def __init__(self, value):
        self.value = value
        self.raw_value = str(value)
        self.meta = {"type": "power", "order": 1}
        self.error = None
        self.update_count = 5
        self.last_update_ts = 1700000000.0
        self.age_seconds = 3.0
    def as_float(self):
        try: return float(self.value)
        except (TypeError, ValueError): return None


class FakeMeter:
    def __init__(self, device_id="wb-map3e_16"):
        self.device_id = device_id
        self.effective_name = "Тестовый счётчик"
        self.mqtt_name = "Тест"
        self.group = "Зона"
        self.driver = "wb-modbus"
        self.status = type("S", (), {"value": "ok"})()
        self.status_reason = ""
        self.first_seen_ts = 1700000000.0
        self.last_any_ts = 1700000000.0
        self.controls = {"Total P": FakeControl(1500.0)}
    def get_serial(self): return "16818659"
    def to_api_dict(self):
        return {"device_id": self.device_id,
                "display_name": self.effective_name,
                "status": self.status.value}


class FakeRegistry:
    def __init__(self, meters): self._m = {m.device_id: m for m in meters}
    def all(self): return list(self._m.values())
    def get(self, d): return self._m.get(d)


def make_client(with_meters=True):
    meters = [FakeMeter()] if with_meters else []
    state = _AppState(
        registry=FakeRegistry(meters),
        meters_repo=None,
        is_mqtt_connected=lambda: True,
        mqtt_message_count=lambda: 100,
        mqtt_error_count=lambda: 0,
        wb_db_client=None,
        consumption_service=None,
        started_at=1700000000.0,
        aggregates_repo=None,
        aggregator=None,
    )
    app = create_app(state)
    return app.test_client()


def test_health():
    c = make_client()
    r = c.get("/health")
    assert r.status_code == 200
    assert json.loads(r.get_data(as_text=True)) == {"ok": True}
    print("[OK] /health")


def test_status():
    c = make_client()
    r = c.get("/api/status")
    assert r.status_code == 200
    d = json.loads(r.get_data(as_text=True))
    assert d["service"] == "wb-energy-meter"
    assert d["meters_total"] == 1
    assert d["mqtt"]["connected"] is True
    assert d["mqtt"]["messages"] == 100
    print("[OK] /api/status")


def test_meters_list():
    c = make_client()
    r = c.get("/api/meters")
    assert r.status_code == 200
    d = json.loads(r.get_data(as_text=True))
    assert d["count"] == 1
    assert d["items"][0]["device_id"] == "wb-map3e_16"
    print("[OK] /api/meters")


def test_meter_detail():
    c = make_client()
    r = c.get("/api/meters/wb-map3e_16")
    assert r.status_code == 200
    d = json.loads(r.get_data(as_text=True))
    assert d["device_id"] == "wb-map3e_16"
    assert d["serial"] == "16818659"
    assert "Total P" in d["controls"]
    print("[OK] /api/meters/<id>")


def test_meter_not_found():
    c = make_client()
    r = c.get("/api/meters/wb-map3e_99")
    assert r.status_code == 404
    d = json.loads(r.get_data(as_text=True))
    assert d["error"] == "meter not found"
    print("[OK] /api/meters/<unknown> -> 404")


def test_consumption_no_service():
    c = make_client()
    r = c.get("/api/meters/wb-map3e_16/consumption?period=today")
    # consumption_service is None -> 503
    assert r.status_code == 503
    print("[OK] consumption without service -> 503")


def test_aggregates_no_repo():
    c = make_client()
    r = c.get("/api/aggregates/status")
    assert r.status_code == 503
    print("[OK] aggregates without repo -> 503")


def test_unknown_path():
    c = make_client()
    r = c.get("/api/this/does/not/exist")
    assert r.status_code == 404
    d = json.loads(r.get_data(as_text=True))
    assert d["error"] == "not found"
    print("[OK] unknown path -> 404 JSON")


def test_docs_page():
    c = make_client()
    r = c.get("/api/docs")
    assert r.status_code == 200
    assert b"wb-energy-meter" in r.get_data()
    assert "text/html" in r.content_type
    print("[OK] /api/docs")


def test_root_page():
    c = make_client()
    r = c.get("/")
    assert r.status_code == 200
    assert "text/html" in r.content_type
    print("[OK] /")


def test_cors_and_cache_headers():
    c = make_client()
    r = c.get("/api/status")
    assert r.headers.get("Access-Control-Allow-Origin") == "*"
    assert r.headers.get("Cache-Control") == "no-store"
    print("[OK] CORS + Cache-Control headers")


def test_cyrillic_not_escaped():
    c = make_client()
    r = c.get("/api/meters")
    raw = r.get_data(as_text=True)
    # display_name содержит кириллицу — не должно быть \u
    assert "\\u" not in raw
    print("[OK] кириллица не эскейпится")


if __name__ == "__main__":
    test_health()
    test_status()
    test_meters_list()
    test_meter_detail()
    test_meter_not_found()
    test_consumption_no_service()
    test_aggregates_no_repo()
    test_unknown_path()
    test_docs_page()
    test_root_page()
    test_cors_and_cache_headers()
    test_cyrillic_not_escaped()
    print("\nВсе тесты Шага 5 (Flask API) пройдены.")

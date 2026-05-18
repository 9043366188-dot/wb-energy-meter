"""HTTP API."""

from __future__ import annotations

import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Optional
from urllib.parse import parse_qs, urlparse

from . import __version__
from .consumption import ConsumptionService
from .model import ControlState, MeterRegistry, MeterState
from .periods import PERIOD_PRESETS, build_period, parse_user_datetime
from .repo import MeterRepo
from .wb_db_client import RpcError, WbDbClient


log = logging.getLogger(__name__)


class _AppState:
    def __init__(self, registry, meters_repo, is_mqtt_connected,
                 mqtt_message_count, mqtt_error_count,
                 wb_db_client, consumption_service, started_at,
                 aggregates_repo=None, aggregator=None):
        self.registry = registry
        self.meters_repo = meters_repo
        self.is_mqtt_connected = is_mqtt_connected
        self.mqtt_message_count = mqtt_message_count
        self.mqtt_error_count = mqtt_error_count
        self.wb_db_client = wb_db_client
        self.consumption_service = consumption_service
        self.aggregates_repo = aggregates_repo
        self.aggregator = aggregator
        self.started_at = started_at


class _Handler(BaseHTTPRequestHandler):
    app_state: _AppState

    def log_message(self, fmt, *args):
        log.debug("HTTP %s - %s", self.address_string(), fmt % args)

    def do_GET(self):
        try:
            parsed = urlparse(self.path)
            path = parsed.path
            qs = parse_qs(parsed.query)

            if path == "/health":
                self._json(200, {"ok": True}); return
            if path == "/api/status":
                self._json(200, self._build_status()); return
            if path == "/api/meters":
                self._json(200, self._build_meters_list()); return
            if path == "/api/summary/consumption":
                self._handle_summary_consumption(qs); return
            if path == "/api/aggregates/status":
                self._handle_aggregates_status(); return
            if path.startswith("/api/meters/"):
                tail = path[len("/api/meters/"):].strip("/")
                if not tail:
                    self._json(400, {"error": "device_id required"}); return
                if "/" in tail:
                    device_id, sub = tail.split("/", 1)
                else:
                    device_id, sub = tail, ""
                if not device_id:
                    self._json(400, {"error": "device_id required"}); return
                if sub == "":
                    self._handle_meter_detail(device_id); return
                if sub == "consumption":
                    self._handle_consumption(device_id, qs); return
                if sub == "history-info":
                    self._handle_history_info(device_id); return
                if sub == "hourly":
                    self._handle_hourly(device_id, qs); return
                self._json(404, {"error": "unknown subresource", "sub": sub})
                return
            if path == "/":
                self._html(200, _ROOT_HTML); return
            self._json(404, {"error": "not found", "path": path})
        except Exception as e:
            log.exception("HTTP handler error: %s", e)
            self._json(500, {"error": "internal", "detail": str(e)})

    def _build_status(self):
        state = self.app_state
        meters = state.registry.all()
        by_status = {}
        for m in meters:
            by_status[m.status.value] = by_status.get(m.status.value, 0) + 1
        return {
            "service": "wb-energy-meter", "version": __version__,
            "uptime_s": time.time() - state.started_at,
            "mqtt": {
                "connected": state.is_mqtt_connected(),
                "messages": state.mqtt_message_count(),
                "errors": state.mqtt_error_count(),
            },
            "meters_total": len(meters),
            "meters_by_status": by_status,
            "meters": [m.to_api_dict() for m in meters],
        }

    def _build_meters_list(self):
        meters = self.app_state.registry.all()
        return {"count": len(meters),
                "items": [m.to_api_dict() for m in meters]}

    def _handle_meter_detail(self, device_id):
        meter = self.app_state.registry.get(device_id)
        if meter is None:
            self._json(404, {"error": "meter not found",
                             "device_id": device_id}); return
        self._json(200, _meter_detail(meter))

    def _parse_period_from_qs(self, qs):
        preset = (qs.get("period") or [None])[0]
        if preset:
            return {"preset": preset}
        ts_from_s = (qs.get("from") or [None])[0]
        ts_to_s = (qs.get("to") or [None])[0]
        if ts_from_s and ts_to_s:
            return {
                "ts_from": parse_user_datetime(ts_from_s),
                "ts_to": parse_user_datetime(ts_to_s),
            }
        return {"preset": "today"}

    def _handle_consumption(self, device_id, qs):
        meter = self.app_state.registry.get(device_id)
        if meter is None:
            self._json(404, {"error": "meter not found",
                             "device_id": device_id}); return
        if self.app_state.consumption_service is None:
            self._json(503, {"error": "consumption service not available"}); return
        try:
            period = build_period(**self._parse_period_from_qs(qs))
        except ValueError as e:
            self._json(400, {"error": "bad period", "detail": str(e),
                             "available_presets": list(PERIOD_PRESETS)}); return
        try:
            result = self.app_state.consumption_service.calculate(
                device_id, period)
        except RpcError as e:
            self._json(502, {"error": "RPC error", "detail": str(e)}); return
        out = result.to_dict()
        out["display_name"] = meter.effective_name
        out["group"] = meter.group
        self._json(200, out)

    def _handle_summary_consumption(self, qs):
        if self.app_state.consumption_service is None:
            self._json(503, {"error": "consumption service not available"}); return
        try:
            period = build_period(**self._parse_period_from_qs(qs))
        except ValueError as e:
            self._json(400, {"error": "bad period", "detail": str(e)}); return

        meters = self.app_state.registry.all()
        items = []
        total_kwh = 0.0
        any_unknown = False
        for m in meters:
            try:
                r = self.app_state.consumption_service.calculate(
                    m.device_id, period)
            except RpcError as e:
                items.append({
                    "device_id": m.device_id,
                    "display_name": m.effective_name,
                    "group": m.group, "consumption_kwh": None,
                    "quality": "no_data", "error": str(e),
                })
                any_unknown = True; continue
            d = r.to_dict()
            d["display_name"] = m.effective_name
            d["group"] = m.group
            items.append(d)
            if r.consumption_kwh is not None:
                total_kwh += r.consumption_kwh
            else:
                any_unknown = True
        items.sort(key=lambda x: x.get("consumption_kwh") or -1.0,
                   reverse=True)
        self._json(200, {
            "period": period.to_dict(),
            "meters_total": len(meters),
            "consumption_kwh_total": round(total_kwh, 6),
            "any_unknown": any_unknown,
            "items": items,
        })

    def _handle_history_info(self, device_id):
        if self.app_state.wb_db_client is None:
            self._json(503, {"error": "wb-mqtt-db client not available"}); return
        try:
            channels = self.app_state.wb_db_client.get_channels(timeout_s=5.0)
        except RpcError as e:
            self._json(502, {"error": "RPC error", "detail": str(e)}); return
        device_chans = [c for c in channels if c.device == device_id]
        device_chans.sort(key=lambda c: c.control)
        self._json(200, {
            "device_id": device_id,
            "channels_in_history": len(device_chans),
            "items": [
                {"control": c.control, "items": c.items, "last_ts": c.last_ts}
                for c in device_chans
            ],
        })

    def _handle_hourly(self, device_id, qs):
        """GET /api/meters/<id>/hourly?period=last_7d — почасовые агрегаты."""
        if self.app_state.aggregates_repo is None:
            self._json(503, {"error": "aggregates not available"}); return
        meter_row = self.app_state.meters_repo.get_by_device_id(device_id)
        if meter_row is None:
            self._json(404, {"error": "meter not found",
                             "device_id": device_id}); return
        try:
            period = build_period(**self._parse_period_from_qs(qs))
        except ValueError as e:
            self._json(400, {"error": "bad period", "detail": str(e)}); return

        from .aggregates_repo import align_hour_down
        ts_from = align_hour_down(period.ts_from)
        ts_to = align_hour_down(period.ts_to) + 3600
        rows = self.app_state.aggregates_repo.list_range(
            meter_row.id, ts_from, ts_to,
        )
        self._json(200, {
            "device_id": device_id,
            "display_name": meter_row.display_name,
            "period": period.to_dict(),
            "hours_count": len(rows),
            "items": [r.to_dict() for r in rows],
        })

    def _handle_aggregates_status(self):
        """GET /api/aggregates/status — сводка по таблице period_aggregates."""
        if self.app_state.aggregates_repo is None:
            self._json(503, {"error": "aggregates not available"}); return
        stats = self.app_state.aggregates_repo.stats()
        worker_status = (self.app_state.aggregator.status()
                         if self.app_state.aggregator else None)
        self._json(200, {
            "rows_total": stats["rows_total"],
            "earliest_ts": stats["earliest_ts"],
            "latest_ts": stats["latest_ts"],
            "by_meter": stats["by_meter"],
            "by_quality": stats["by_quality"],
            "worker": worker_status,
        })

    def _json(self, code, body):
        payload = json.dumps(body, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(payload)

    def _html(self, code, body):
        payload = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def _meter_detail(m):
    controls = {}
    for name, c in sorted(
        m.controls.items(),
        key=lambda x: x[1].meta.get("order", 999) if isinstance(
            x[1].meta.get("order"), int) else 999):
        controls[name] = _control_detail(c)
    return {
        "device_id": m.device_id,
        "display_name": m.effective_name,
        "mqtt_name": m.mqtt_name, "group": m.group,
        "driver": m.driver, "serial": m.get_serial(),
        "status": m.status.value, "status_reason": m.status_reason,
        "first_seen_ts": m.first_seen_ts,
        "last_update_ts": m.last_any_ts,
        "last_update_age_s": (
            time.time() - m.last_any_ts if m.last_any_ts > 0 else None),
        "controls_count": len(m.controls), "controls": controls,
    }


def _control_detail(c):
    return {
        "value": c.value, "raw_value": c.raw_value,
        "numeric": c.as_float(),
        "type": c.meta.get("type"),
        "precision": c.meta.get("precision"),
        "order": c.meta.get("order"),
        "readonly": c.meta.get("readonly"),
        "error": c.error, "update_count": c.update_count,
        "last_update_ts": c.last_update_ts,
        "last_update_age_s": c.age_seconds,
    }


class ApiServer:
    def __init__(self, host, port, registry, meters_repo,
                 is_mqtt_connected, mqtt_message_count, mqtt_error_count,
                 wb_db_client=None, consumption_service=None,
                 aggregates_repo=None, aggregator=None):
        self._host = host; self._port = port
        self._app_state = _AppState(
            registry=registry, meters_repo=meters_repo,
            is_mqtt_connected=is_mqtt_connected,
            mqtt_message_count=mqtt_message_count,
            mqtt_error_count=mqtt_error_count,
            wb_db_client=wb_db_client,
            consumption_service=consumption_service,
            aggregates_repo=aggregates_repo,
            aggregator=aggregator,
            started_at=time.time())
        self._server = None
        self._thread = None

    def start(self):
        handler_cls = type("BoundHandler", (_Handler,),
                           {"app_state": self._app_state})
        self._server = ThreadingHTTPServer((self._host, self._port), handler_cls)
        self._thread = threading.Thread(target=self._server.serve_forever,
                                         name="http-api", daemon=True)
        self._thread.start()
        log.info("HTTP API запущен: http://%s:%d", self._host, self._port)

    def stop(self):
        if self._server:
            try:
                self._server.shutdown(); self._server.server_close()
            except Exception: pass
        if self._thread:
            self._thread.join(timeout=5.0)


_ROOT_HTML = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><title>wb-energy-meter</title>
<style>body{font-family:system-ui,sans-serif;max-width:780px;margin:2em auto;padding:0 1em}
code{background:#f4f4f4;padding:2px 6px;border-radius:3px}</style></head>
<body><h1>wb-energy-meter</h1><p>Сервис работает. UI появится на Шаге 6.</p>
<p>Endpoint'ы:</p><ul>
<li><a href="/api/status"><code>/api/status</code></a></li>
<li><a href="/api/meters"><code>/api/meters</code></a></li>
<li><code>/api/meters/&lt;id&gt;</code></li>
<li><code>/api/meters/&lt;id&gt;/consumption?period=today</code></li>
<li><code>/api/meters/&lt;id&gt;/hourly?period=last_7d</code></li>
<li><code>/api/meters/&lt;id&gt;/history-info</code></li>
<li><code>/api/summary/consumption?period=this_month</code></li>
<li><a href="/api/aggregates/status"><code>/api/aggregates/status</code></a></li>
<li><a href="/health"><code>/health</code></a></li>
</ul></body></html>
"""

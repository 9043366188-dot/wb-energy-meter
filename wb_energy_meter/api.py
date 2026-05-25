"""HTTP API на Flask.

Все эндпоинты сохраняют URL и формат ответов из предыдущей версии
(на http.server) — внешний контракт не меняется.

Flask 1.1.2+ (из Debian apt). Запускается в отдельном потоке через
werkzeug make_server (для управляемого shutdown). Для нагрузки
«несколько клиентов, редкие запросы» этого достаточно.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Optional

from flask import Flask, Response, request

from . import __version__
from .aggregates_repo import align_hour_down
from .consumption import ConsumptionService
from .periods import PERIOD_PRESETS, build_period, parse_user_datetime
from .wb_db_client import RpcError


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


# ---------------------------------------------------------------------------
# Чистые функции построения ответов
# ---------------------------------------------------------------------------

def _build_status(state):
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


def _build_meters_list(state):
    meters = state.registry.all()
    return {"count": len(meters),
            "items": [m.to_api_dict() for m in meters]}


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


def _parse_period_from_request():
    preset = request.args.get("period")
    if preset:
        return {"preset": preset}
    ts_from_s = request.args.get("from")
    ts_to_s = request.args.get("to")
    if ts_from_s and ts_to_s:
        return {
            "ts_from": parse_user_datetime(ts_from_s),
            "ts_to": parse_user_datetime(ts_to_s),
        }
    return {"preset": "today"}


def _dumps(body):
    import json
    return json.dumps(body, ensure_ascii=False, default=str)


def _load_static(filename):
    """Прочитать файл из папки static рядом с модулем.

    Если файл недоступен (например, не скопирован) — отдаём
    минимальную заглушку, чтобы сервис не падал.
    """
    import os
    static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
    path = os.path.join(static_dir, filename)
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except OSError as e:
        log.warning("Не смог прочитать static/%s: %s", filename, e)
        return _ROOT_HTML_FALLBACK


# ---------------------------------------------------------------------------
# Flask app factory
# ---------------------------------------------------------------------------

def create_app(state):
    app = Flask("wb_energy_meter")
    app.config["JSON_SORT_KEYS"] = False
    app.config["JSON_AS_ASCII"] = False

    def json_response(body, code=200):
        resp = app.response_class(
            response=_dumps(body), status=code,
            mimetype="application/json")
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["Access-Control-Allow-Origin"] = "*"
        return resp

    @app.route("/health")
    def health():
        return json_response({"ok": True})

    @app.route("/api/status")
    def api_status():
        return json_response(_build_status(state))

    @app.route("/api/meters")
    def api_meters():
        return json_response(_build_meters_list(state))

    @app.route("/api/meters/<device_id>")
    def api_meter_detail(device_id):
        meter = state.registry.get(device_id)
        if meter is None:
            return json_response(
                {"error": "meter not found", "device_id": device_id}, 404)
        return json_response(_meter_detail(meter))

    @app.route("/api/meters/<device_id>/consumption")
    def api_meter_consumption(device_id):
        meter = state.registry.get(device_id)
        if meter is None:
            return json_response(
                {"error": "meter not found", "device_id": device_id}, 404)
        if state.consumption_service is None:
            return json_response(
                {"error": "consumption service not available"}, 503)
        try:
            period = build_period(**_parse_period_from_request())
        except ValueError as e:
            return json_response(
                {"error": "bad period", "detail": str(e),
                 "available_presets": list(PERIOD_PRESETS)}, 400)
        try:
            result = state.consumption_service.calculate(device_id, period)
        except RpcError as e:
            return json_response({"error": "RPC error", "detail": str(e)}, 502)
        out = result.to_dict()
        out["display_name"] = meter.effective_name
        out["group"] = meter.group
        return json_response(out)

    @app.route("/api/meters/<device_id>/history-info")
    def api_meter_history_info(device_id):
        if state.wb_db_client is None:
            return json_response(
                {"error": "wb-mqtt-db client not available"}, 503)
        try:
            channels = state.wb_db_client.get_channels(timeout_s=5.0)
        except RpcError as e:
            return json_response({"error": "RPC error", "detail": str(e)}, 502)
        device_chans = [c for c in channels if c.device == device_id]
        device_chans.sort(key=lambda c: c.control)
        return json_response({
            "device_id": device_id,
            "channels_in_history": len(device_chans),
            "items": [
                {"control": c.control, "items": c.items, "last_ts": c.last_ts}
                for c in device_chans
            ],
        })

    @app.route("/api/meters/<device_id>/hourly")
    def api_meter_hourly(device_id):
        if state.aggregates_repo is None:
            return json_response({"error": "aggregates not available"}, 503)
        meter_row = state.meters_repo.get_by_device_id(device_id)
        if meter_row is None:
            return json_response(
                {"error": "meter not found", "device_id": device_id}, 404)
        try:
            period = build_period(**_parse_period_from_request())
        except ValueError as e:
            return json_response({"error": "bad period", "detail": str(e)}, 400)
        ts_from = align_hour_down(period.ts_from)
        ts_to = align_hour_down(period.ts_to) + 3600
        rows = state.aggregates_repo.list_range(meter_row.id, ts_from, ts_to)
        return json_response({
            "device_id": device_id,
            "display_name": meter_row.display_name,
            "period": period.to_dict(),
            "hours_count": len(rows),
            "items": [r.to_dict() for r in rows],
        })

    @app.route("/api/summary/consumption")
    def api_summary_consumption():
        if state.consumption_service is None:
            return json_response(
                {"error": "consumption service not available"}, 503)
        try:
            period = build_period(**_parse_period_from_request())
        except ValueError as e:
            return json_response({"error": "bad period", "detail": str(e)}, 400)
        meters = state.registry.all()
        items = []
        total_kwh = 0.0
        any_unknown = False
        for m in meters:
            try:
                r = state.consumption_service.calculate(m.device_id, period)
            except RpcError as e:
                items.append({
                    "device_id": m.device_id,
                    "display_name": m.effective_name,
                    "group": m.group, "consumption_kwh": None,
                    "quality": "no_data", "error": str(e),
                })
                any_unknown = True
                continue
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
        return json_response({
            "period": period.to_dict(),
            "meters_total": len(meters),
            "consumption_kwh_total": round(total_kwh, 6),
            "any_unknown": any_unknown,
            "items": items,
        })

    @app.route("/api/aggregates/status")
    def api_aggregates_status():
        if state.aggregates_repo is None:
            return json_response({"error": "aggregates not available"}, 503)
        stats = state.aggregates_repo.stats()
        worker_status = (state.aggregator.status()
                         if state.aggregator else None)
        return json_response({
            "rows_total": stats["rows_total"],
            "earliest_ts": stats["earliest_ts"],
            "latest_ts": stats["latest_ts"],
            "by_meter": stats["by_meter"],
            "by_quality": stats["by_quality"],
            "worker": worker_status,
        })

    @app.route("/")
    def root():
        return Response(_load_static("index.html"), mimetype="text/html")

    @app.route("/api/docs")
    def api_docs():
        return Response(_DOCS_HTML, mimetype="text/html")

    @app.errorhandler(404)
    def not_found(e):
        return json_response({"error": "not found", "path": request.path}, 404)

    @app.errorhandler(Exception)
    def unhandled(e):
        # 404 уже обработан выше; сюда падают реальные ошибки.
        from werkzeug.exceptions import HTTPException
        if isinstance(e, HTTPException):
            return json_response({"error": e.name, "code": e.code}, e.code)
        log.exception("Unhandled HTTP error: %s", e)
        return json_response({"error": "internal", "detail": str(e)}, 500)

    return app


# ---------------------------------------------------------------------------
# ApiServer — прежний интерфейс start/stop
# ---------------------------------------------------------------------------

class ApiServer:
    def __init__(self, host, port, registry, meters_repo,
                 is_mqtt_connected, mqtt_message_count, mqtt_error_count,
                 wb_db_client=None, consumption_service=None,
                 aggregates_repo=None, aggregator=None):
        self._host = host
        self._port = port
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
        self._app = create_app(self._app_state)
        self._server = None
        self._thread = None

    def start(self):
        from werkzeug.serving import make_server
        self._server = make_server(
            self._host, self._port, self._app, threaded=True)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="http-api", daemon=True)
        self._thread.start()
        log.info("HTTP API (Flask) запущен: http://%s:%d",
                 self._host, self._port)

    def stop(self):
        if self._server is not None:
            try:
                self._server.shutdown()
            except Exception:
                pass
        if self._thread is not None:
            self._thread.join(timeout=5.0)


_ROOT_HTML_FALLBACK = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><title>wb-energy-meter</title>
<style>body{font-family:system-ui,sans-serif;max-width:780px;margin:2em auto;padding:0 1em}
code{background:#f4f4f4;padding:2px 6px;border-radius:3px}
a{color:#0366d6}</style></head>
<body><h1>wb-energy-meter</h1>
<p>Веб-интерфейс (static/index.html) не найден. Сервис работает, API доступен.</p>
<p>См. <a href="/api/docs">описание API</a> или:</p><ul>
<li><a href="/api/status"><code>/api/status</code></a></li>
<li><a href="/api/meters"><code>/api/meters</code></a></li>
<li><a href="/api/aggregates/status"><code>/api/aggregates/status</code></a></li>
<li><a href="/health"><code>/health</code></a></li>
</ul></body></html>
"""

_DOCS_HTML = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><title>wb-energy-meter API</title>
<style>
body{font-family:system-ui,sans-serif;max-width:900px;margin:2em auto;padding:0 1em;line-height:1.5}
h1{border-bottom:2px solid #eee;padding-bottom:.3em}
.ep{border:1px solid #e1e4e8;border-radius:6px;padding:1em;margin:1em 0}
.method{display:inline-block;background:#2188ff;color:#fff;padding:2px 8px;border-radius:3px;font-size:.85em;font-weight:bold}
code{background:#f6f8fa;padding:2px 6px;border-radius:3px;font-family:monospace}
.path{font-family:monospace;font-size:1.05em;margin-left:.5em}
.desc{color:#444;margin:.5em 0}
.params{font-size:.9em;color:#666}
a{color:#0366d6}
</style></head>
<body>
<h1>wb-energy-meter — API</h1>
<p>Все ответы в JSON (UTF-8). CORS открыт. Версия сервиса — в
<code>/api/status</code>.</p>

<div class="ep"><span class="method">GET</span><span class="path">/health</span>
<div class="desc">Проверка живости. Возвращает <code>{"ok": true}</code>.</div></div>

<div class="ep"><span class="method">GET</span><span class="path">/api/status</span>
<div class="desc">Сводка: версия, аптайм, состояние MQTT, список счётчиков
со статусами и текущими значениями.</div></div>

<div class="ep"><span class="method">GET</span><span class="path">/api/meters</span>
<div class="desc">Список всех счётчиков из реестра с текущими значениями.</div></div>

<div class="ep"><span class="method">GET</span><span class="path">/api/meters/&lt;device_id&gt;</span>
<div class="desc">Детали одного счётчика: все каналы, метаданные, статус.</div></div>

<div class="ep"><span class="method">GET</span><span class="path">/api/meters/&lt;device_id&gt;/consumption</span>
<div class="desc">Расход за период (гибридный расчёт: агрегаты + RPC-хвосты).</div>
<div class="params">Параметры: <code>?period=today|yesterday|this_month|last_month|last_24h|last_7d|last_30d</code>
или <code>?from=YYYY-MM-DD&amp;to=YYYY-MM-DD</code></div></div>

<div class="ep"><span class="method">GET</span><span class="path">/api/meters/&lt;device_id&gt;/hourly</span>
<div class="desc">Почасовые агрегаты за период (для графиков).</div>
<div class="params">Параметры: те же, что у consumption.</div></div>

<div class="ep"><span class="method">GET</span><span class="path">/api/meters/&lt;device_id&gt;/history-info</span>
<div class="desc">Какие каналы этого счётчика есть в wb-mqtt-db и сколько в них точек.</div></div>

<div class="ep"><span class="method">GET</span><span class="path">/api/summary/consumption</span>
<div class="desc">Расход по всем счётчикам за период, с итоговой суммой.</div>
<div class="params">Параметры: те же, что у consumption.</div></div>

<div class="ep"><span class="method">GET</span><span class="path">/api/aggregates/status</span>
<div class="desc">Статистика по таблице почасовых агрегатов и состояние
фонового воркера (catch-up, последний расчёт).</div></div>

<p style="margin-top:2em;color:#888;font-size:.9em">
wb-energy-meter — учёт электроэнергии для Wiren Board.
<a href="https://github.com/9043366188-dot/wb-energy-meter">GitHub</a></p>
</body></html>
"""

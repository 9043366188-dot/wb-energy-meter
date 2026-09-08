"""HTTP API на Flask."""

from __future__ import annotations

import logging
import threading
import time

from flask import Flask, Response, request

from . import __version__
from .aggregates_repo import align_hour_down
from .channels import CATEGORIES, CHANNEL_INFO, get_channel_info
from .periods import PERIOD_PRESETS, build_period, parse_user_datetime
from .repo import GroupNameConflict
from .wb_db_client import RpcError

log = logging.getLogger(__name__)


class _AppState:
    def __init__(self, registry, meters_repo, is_mqtt_connected,
                 mqtt_message_count, mqtt_error_count,
                 wb_db_client, consumption_service, started_at,
                 aggregates_repo=None, aggregator=None,
                 groups_repo=None, alert_repo=None):
        self.registry = registry
        self.meters_repo = meters_repo
        self.groups_repo = groups_repo
        self.alert_repo = alert_repo
        self.is_mqtt_connected = is_mqtt_connected
        self.mqtt_message_count = mqtt_message_count
        self.mqtt_error_count = mqtt_error_count
        self.wb_db_client = wb_db_client
        self.consumption_service = consumption_service
        self.aggregates_repo = aggregates_repo
        self.aggregator = aggregator
        self.started_at = started_at


def _build_status(state):
    meters = state.registry.all()
    by_status = {}
    for m in meters:
        by_status[m.status.value] = by_status.get(m.status.value, 0) + 1

    # Подтягиваем notes и role из БД одним запросом
    notes_map = {}
    role_map = {}
    if state.meters_repo is not None:
        try:
            for row in state.meters_repo.list_all():
                notes_map[row.device_id] = row.notes
                role_map[row.device_id] = row.role
        except Exception:
            pass

    def meter_dict(m):
        d = m.to_api_dict()
        d["notes"] = notes_map.get(m.device_id)
        d["role"] = role_map.get(m.device_id, "consumer")
        return d

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
        "meters": [meter_dict(m) for m in meters],
    }


def _sync_registry_groups(state) -> None:
    """Push-синхронизация group/display_name из БД в in-memory реестр.

    Корневая причина A1 (ТЗ v0.8.0): group у счётчика живёт в двух
    местах — в SQLite и в MeterState.group в памяти, а второе
    заполнялось только один раз при старте демона (main.py). Из-за
    этого назначенная в «Настройках» зона не появлялась на дашборде
    (/api/status берёт данные из памяти) до перезапуска сервиса.

    Вызывается в конце каждого обработчика, меняющего привязку
    счётчика к зоне: add/update счётчика, rename/delete зоны. Плюс
    периодическая пересинхронизация в background.py — страховка от
    рассинхрона по любой другой причине."""
    if state.meters_repo is None:
        return
    try:
        rows = state.meters_repo.list_all()
    except Exception:
        log.exception("Не удалось синхронизировать группы в реестр")
        return
    state.registry.apply_registry_config([
        {"device_id": m.device_id, "display_name": m.display_name,
         "group": m.group_name}
        for m in rows
    ])


def _build_meters_list(state):
    meters = state.registry.all()
    return {"count": len(meters), "items": [m.to_api_dict() for m in meters]}


def _meter_detail(m):
    controls = {}
    for name, c in sorted(
        m.controls.items(),
        key=lambda x: x[1].meta.get("order", 999)
        if isinstance(x[1].meta.get("order"), int) else 999):
        info = get_channel_info(name)
        meta_units = c.meta.get("units")
        controls[name] = {
            "value": c.value, "raw_value": c.raw_value,
            "numeric": c.as_float(),
            "type": c.meta.get("type"),
            "precision": c.meta.get("precision"),
            "order": c.meta.get("order"),
            "readonly": c.meta.get("readonly"),
            # Единицы измерения (C3): приоритет у meta устройства,
            # иначе — из словаря каналов.
            "units": meta_units if meta_units else info["units"],
            "error": c.error, "update_count": c.update_count,
            "last_update_ts": c.last_update_ts,
            "last_update_age_s": c.age_seconds,
            # Русификация и категоризация (§5 ТЗ v0.8.0).
            "label": info["label"],
            "hint": info["hint"],
            "category": info["category"],
            "main": info["main"],
        }
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


def _parse_period_from_request():
    preset = request.args.get("period")
    if preset:
        return {"preset": preset}
    ts_from_s = request.args.get("from")
    ts_to_s = request.args.get("to")
    if ts_from_s and ts_to_s:
        return {"ts_from": parse_user_datetime(ts_from_s),
                "ts_to": parse_user_datetime(ts_to_s)}
    return {"preset": "today"}


def _dumps(body):
    import json
    return json.dumps(body, ensure_ascii=False, default=str)


def _fmt_ts(ts: int) -> str:
    """Unix timestamp → читаемая строка для UI."""
    import time as _time
    return _time.strftime("%d.%m.%Y %H:%M", _time.localtime(ts))


def _load_static(filename):
    import os
    static_dir = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "static")
    path = os.path.join(static_dir, filename)
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except OSError as e:
        log.warning("Не смог прочитать static/%s: %s", filename, e)
        return _ROOT_HTML_FALLBACK


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

    # ---- read-only ----

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
        device_chans = sorted(
            [c for c in channels if c.device == device_id],
            key=lambda c: c.control)
        return json_response({
            "device_id": device_id,
            "channels_in_history": len(device_chans),
            "items": [{"control": c.control, "items": c.items,
                       "last_ts": c.last_ts} for c in device_chans],
        })

    @app.route("/api/meters/<device_id>/channel-history")
    def api_meter_channel_history(device_id):
        """GET /api/meters/<id>/channel-history?control=...&period=...
        GET /api/meters/<id>/channel-history?control=...&from=...&to=...

        История значений одного параметра (задача 2, §4.1 ТЗ v0.8.0) —
        клик по плитке параметра в модалке деталей открывает график."""
        control = (request.args.get("control") or "").strip()
        if not control:
            return json_response({"error": "control required"}, 400)
        if state.wb_db_client is None:
            return json_response(
                {"error": "wb-mqtt-db client not available"}, 503)
        try:
            period = build_period(**_parse_period_from_request())
        except ValueError as e:
            return json_response(
                {"error": "bad period", "detail": str(e),
                 "available_presets": list(PERIOD_PRESETS)}, 400)

        ts_from, ts_to = period.ts_from, period.ts_to
        duration_s = max(1.0, ts_to - ts_from)
        # Прореживание: не больше ~1500 точек на график + жёсткий limit
        # страховкой на случай очень длинного периода с частыми точками.
        min_interval_ms = max(1000, int(duration_s * 1000 / 1500))
        try:
            points = state.wb_db_client.get_values(
                device_id, control, ts_from=ts_from, ts_to=ts_to,
                limit=5000, min_interval_ms=min_interval_ms)
        except RpcError as e:
            return json_response({"error": "RPC error", "detail": str(e)}, 502)

        info = get_channel_info(control)
        values = [p.value for p in points]
        avg = (sum(values) / len(values)) if values else None
        return json_response({
            "device_id": device_id,
            "control": control,
            "label": info["label"],
            "units": info["units"],
            "period": period.to_dict(),
            "points_count": len(points),
            "min": min(values) if values else None,
            "max": max(values) if values else None,
            "avg": round(avg, 6) if avg is not None else None,
            "last": values[-1] if values else None,
            "items": [{"t": int(p.timestamp), "v": p.value} for p in points],
        })

    @app.route("/api/channels/dictionary")
    def api_channels_dictionary():
        """Словарь каналов для фронта — русские названия, единицы,
        подсказки, категории (§5.1 ТЗ v0.8.0). Статичен на время работы
        процесса, поэтому кэшируется на час."""
        resp = json_response({
            "categories": CATEGORIES,
            "channels": CHANNEL_INFO,
        })
        resp.headers["Cache-Control"] = "public, max-age=3600"
        return resp

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
                    "quality": "no_data", "error": str(e)})
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
        items.sort(key=lambda x: x.get("consumption_kwh") or -1.0, reverse=True)
        return json_response({
            "period": period.to_dict(),
            "meters_total": len(meters),
            "consumption_kwh_total": round(total_kwh, 6),
            "any_unknown": any_unknown, "items": items,
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

    # ---- settings API (CRUD для реестра) ----

    @app.route("/api/meters/unregistered")
    def api_meters_unregistered():
        """Счётчики, которые видны в MQTT, но не добавлены в реестр."""
        if state.meters_repo is None:
            return json_response({"error": "meters_repo not available"}, 503)
        # Все device_id из in-memory registry (видели в MQTT)
        all_in_mqtt = {m.device_id for m in state.registry.all()}
        # Все device_id из БД (зарегистрированы)
        all_in_db = {m.device_id for m in state.meters_repo.list_all()}
        unregistered = sorted(all_in_mqtt - all_in_db)
        result = []
        for did in unregistered:
            m = state.registry.get(did)
            result.append({
                "device_id": did,
                "mqtt_name": m.mqtt_name if m else None,
                "serial": m.get_serial() if m else None,
                "status": m.status.value if m else "unknown",
                "last_update_age_s": (
                    time.time() - m.last_any_ts
                    if m and m.last_any_ts > 0 else None),
            })
        return json_response({"count": len(result), "items": result})

    @app.route("/api/registry/meters", methods=["POST"])
    def api_registry_meter_add():
        """Добавить счётчик в реестр. Body: {device_id, display_name, group?}"""
        if state.meters_repo is None:
            return json_response({"error": "meters_repo not available"}, 503)
        import json as _json
        try:
            body = _json.loads(request.data.decode("utf-8"))
        except Exception:
            return json_response({"error": "invalid JSON body"}, 400)
        device_id = (body.get("device_id") or "").strip()
        display_name = (body.get("display_name") or "").strip()
        group = (body.get("group") or "").strip() or None
        notes = (body.get("notes") or "").strip() or None
        if not device_id:
            return json_response({"error": "device_id required"}, 400)
        if not display_name:
            display_name = device_id
        try:
            m = state.meters_repo.add(
                device_id=device_id, display_name=display_name,
                group=group, notes=notes)
        except ValueError as e:
            return json_response({"error": str(e)}, 409)
        _sync_registry_groups(state)
        log.info("Добавлен счётчик через API: %s -> %r", device_id, display_name)
        return json_response({
            "ok": True, "id": m.id,
            "device_id": m.device_id, "display_name": m.display_name,
            "group": m.group_name,
        }, 201)

    @app.route("/api/registry/meters")
    def api_registry_meters_list():
        """Список счётчиков из БД с group_name и serial_number."""
        if state.meters_repo is None:
            return json_response({"error": "meters_repo not available"}, 503)
        items = state.meters_repo.list_all()
        return json_response({
            "count": len(items),
            "items": [m.to_dict() for m in items],
        })

    @app.route("/api/registry/meters/<device_id>", methods=["GET"])
    def api_registry_meter_get(device_id):
        """Детали счётчика из БД."""
        if state.meters_repo is None:
            return json_response({"error": "meters_repo not available"}, 503)
        m = state.meters_repo.get_by_device_id(device_id)
        if m is None:
            return json_response(
                {"error": "meter not found", "device_id": device_id}, 404)
        return json_response(m.to_dict())

    @app.route("/api/registry/meters/<device_id>", methods=["PATCH"])
    def api_registry_meter_update(device_id):
        """Обновить имя и/или группу. Body: {display_name?, group?}"""
        if state.meters_repo is None:
            return json_response({"error": "meters_repo not available"}, 503)
        import json as _json
        try:
            body = _json.loads(request.data.decode("utf-8"))
        except Exception:
            return json_response({"error": "invalid JSON body"}, 400)
        kwargs = {}
        if "display_name" in body:
            v = (body["display_name"] or "").strip()
            if v: kwargs["display_name"] = v
        if "group" in body:
            # A2: различаем «ключ не передан» (не трогать группу) и
            # «передана пустая строка / null» (снять группу). Раньше оба
            # случая схлопывались в None, а MeterRepo.update() пропускает
            # group=None — PATCH {"group":""} молча ничего не менял.
            raw = body["group"]
            kwargs["group"] = "" if raw in (None, "") else str(raw).strip()
        if "notes" in body:
            kwargs["notes"] = (body["notes"] or "").strip() or None
        if not kwargs:
            return json_response({"error": "nothing to update"}, 400)
        try:
            m = state.meters_repo.update(device_id, **kwargs)
        except ValueError as e:
            return json_response({"error": str(e)}, 404)
        _sync_registry_groups(state)
        log.info("Обновлён счётчик через API: %s %s", device_id, kwargs)
        return json_response({
            "ok": True, "device_id": m.device_id,
            "display_name": m.display_name, "group": m.group_name,
        })

    @app.route("/api/registry/meters/<device_id>", methods=["DELETE"])
    def api_registry_meter_delete(device_id):
        """Удалить счётчик из реестра."""
        if state.meters_repo is None:
            return json_response({"error": "meters_repo not available"}, 503)
        m = state.meters_repo.get_by_device_id(device_id)
        if m is None:
            return json_response(
                {"error": "meter not found", "device_id": device_id}, 404)
        state.meters_repo.remove(device_id)
        log.info("Удалён счётчик через API: %s", device_id)
        return json_response({"ok": True, "device_id": device_id})

    @app.route("/api/meters/<device_id>/availability")
    def api_meter_availability(device_id):
        """GET /api/meters/<id>/availability?period=last_30d"""
        if state.alert_repo is None or state.meters_repo is None:
            return json_response({"error": "alert_repo not available"}, 503)
        meter_row = state.meters_repo.get_by_device_id(device_id)
        if meter_row is None:
            return json_response(
                {"error": "meter not found", "device_id": device_id}, 404)
        try:
            period = build_period(**_parse_period_from_request())
        except ValueError as e:
            return json_response({"error": "bad period", "detail": str(e)}, 400)
        stats = state.alert_repo.availability_stats(
            meter_row.id, int(period.ts_from), int(period.ts_to))
        # Добавим имена в интервалы для удобства UI
        for iv in stats["intervals"]:
            iv["started_label"] = _fmt_ts(iv["started_at"])
            iv["ended_label"] = (_fmt_ts(iv["ended_at"])
                                 if iv["ended_at"] else "сейчас")
        return json_response({
            "device_id": device_id,
            "display_name": meter_row.display_name,
            "period": period.to_dict(),
            **stats,
        })

    @app.route("/api/availability/summary")
    def api_availability_summary():
        """GET /api/availability/summary?period=last_30d — по всем счётчикам."""
        if state.alert_repo is None or state.meters_repo is None:
            return json_response({"error": "alert_repo not available"}, 503)
        try:
            period = build_period(**_parse_period_from_request())
        except ValueError as e:
            return json_response({"error": "bad period", "detail": str(e)}, 400)
        all_meters = state.meters_repo.list_all()
        items = []
        for m in all_meters:
            stats = state.alert_repo.availability_stats(
                m.id, int(period.ts_from), int(period.ts_to))
            items.append({
                "device_id": m.device_id,
                "display_name": m.display_name,
                "group": m.group_name,
                "role": m.role,
                "availability_pct": stats["availability_pct"],
                "unavailable_s": stats["unavailable_s"],
                "incidents": stats["incidents"],
            })
        items.sort(key=lambda x: x["availability_pct"])
        return json_response({
            "period": period.to_dict(),
            "items": items,
        })

    @app.route("/api/registry/meters/<device_id>/role", methods=["PATCH"])
    def api_registry_meter_role(device_id):
        """Изменить роль счётчика. Body: {role: "input"|"consumer"|"other"}"""
        if state.meters_repo is None:
            return json_response({"error": "meters_repo not available"}, 503)
        import json as _json
        try:
            body = _json.loads(request.data.decode("utf-8"))
        except Exception:
            return json_response({"error": "invalid JSON body"}, 400)
        role = (body.get("role") or "").strip()
        if role not in ("input", "consumer", "other"):
            return json_response(
                {"error": "role must be input, consumer or other"}, 400)
        try:
            m = state.meters_repo.update(device_id, role=role)
        except ValueError as e:
            return json_response({"error": str(e)}, 404)
        log.info("Роль счётчика %s изменена на %s", device_id, role)
        return json_response({"ok": True, "device_id": device_id, "role": role})

    @app.route("/api/reports/balance")
    def api_reports_balance():
        """GET /api/reports/balance?period=this_month"""
        if state.consumption_service is None or state.meters_repo is None:
            return json_response({"error": "service not available"}, 503)
        try:
            period = build_period(**_parse_period_from_request())
        except ValueError as e:
            return json_response({"error": "bad period", "detail": str(e)}, 400)

        all_meters = state.meters_repo.list_all()
        inputs    = [m for m in all_meters if m.role == "input"]
        consumers = [m for m in all_meters if m.role == "consumer"]
        others    = [m for m in all_meters if m.role == "other"]

        def calc_group(meters):
            items, total, any_unknown = [], 0.0, False
            for m in meters:
                try:
                    r = state.consumption_service.calculate(m.device_id, period)
                except RpcError as e:
                    items.append({"device_id": m.device_id,
                                  "display_name": m.display_name,
                                  "group": m.group_name,
                                  "consumption_kwh": None,
                                  "quality": "no_data"})
                    any_unknown = True
                    continue
                kwh = r.consumption_kwh
                items.append({"device_id": m.device_id,
                               "display_name": m.display_name,
                               "group": m.group_name,
                               "consumption_kwh": kwh,
                               "quality": r.quality})
                if kwh is not None: total += kwh
                else: any_unknown = True
            return items, round(total, 6), any_unknown

        inp_items, inp_total, inp_unk = calc_group(inputs)
        con_items, con_total, con_unk = calc_group(consumers)
        oth_items, oth_total, _       = calc_group(others)

        imbalance = round(inp_total - con_total, 6)
        imbalance_pct = (round(imbalance / inp_total * 100, 2)
                         if inp_total > 0 else None)

        return json_response({
            "period": period.to_dict(),
            "input":    {"total_kwh": inp_total, "any_unknown": inp_unk,  "meters": inp_items},
            "consumer": {"total_kwh": con_total, "any_unknown": con_unk,  "meters": con_items},
            "other":    {"total_kwh": oth_total, "meters": oth_items},
            "imbalance_kwh": imbalance,
            "imbalance_pct": imbalance_pct,
            "has_inputs":    len(inputs) > 0,
            "has_consumers": len(consumers) > 0,
        })

    @app.route("/api/registry/groups")
    def api_registry_groups():
        """Список зон с id, именем, цветом и количеством счётчиков."""
        if state.meters_repo is None:
            return json_response({"error": "meters_repo not available"}, 503)
        # Считаем счётчики по зонам
        counts = {}
        for m in state.meters_repo.list_all():
            if m.group_name:
                counts[m.group_name] = counts.get(m.group_name, 0) + 1
        # Берём все группы из БД
        groups = []
        if state.groups_repo is not None:
            for g in state.groups_repo.list_all():
                groups.append({
                    "id": g.id,
                    "name": g.name,
                    "color": g.color,
                    "meter_count": counts.get(g.name, 0),
                })
        else:
            # Fallback: из имён групп счётчиков
            for name, cnt in sorted(counts.items()):
                groups.append({"id": None, "name": name, "color": None,
                               "meter_count": cnt})
        return json_response({"count": len(groups), "groups": groups})

    @app.route("/api/registry/groups", methods=["POST"])
    def api_registry_group_create():
        """Создать зону. Body: {name, color?}"""
        if state.groups_repo is None:
            return json_response({"error": "groups_repo not available"}, 503)
        import json as _json
        try:
            body = _json.loads(request.data.decode("utf-8"))
        except Exception:
            return json_response({"error": "invalid JSON body"}, 400)
        name = (body.get("name") or "").strip()
        color = (body.get("color") or "").strip() or None
        if not name:
            return json_response({"error": "name required"}, 400)
        try:
            g = state.groups_repo.create(name, color=color)
        except GroupNameConflict as e:
            return json_response({
                "error": "Зона с таким именем уже есть",
                "existing_id": e.existing_id,
            }, 409)
        except ValueError as e:
            return json_response({"error": str(e)}, 409)
        log.info("Создана зона через API: %r (id=%d)", name, g.id)
        return json_response({"ok": True, "id": g.id, "name": g.name,
                              "color": g.color}, 201)

    @app.route("/api/registry/groups/<int:group_id>", methods=["PATCH"])
    def api_registry_group_rename(group_id):
        """Переименовать и/или сменить цвет зоны.
        Body: {name?, color?, merge?}

        Переименование в занятое (по casefold-сравнению) имя без флага
        merge=true возвращает 409 (A5 — раньше это молча сливало зоны).
        С merge:true — явное слияние: счётчики перепривязываются на уже
        существующую зону, дубль удаляется."""
        if state.groups_repo is None:
            return json_response({"error": "groups_repo not available"}, 503)
        import json as _json
        try:
            body = _json.loads(request.data.decode("utf-8"))
        except Exception:
            return json_response({"error": "invalid JSON body"}, 400)
        g = state.groups_repo.get_by_id(group_id)
        if g is None:
            return json_response({"error": "group not found", "id": group_id}, 404)

        # Смена только цвета — не требует имени.
        if "color" in body and not (body.get("name") or "").strip():
            try:
                new_g = state.groups_repo.set_color(group_id, body.get("color"))
            except ValueError as e:
                return json_response({"error": str(e)}, 404)
            return json_response({"ok": True, "id": new_g.id, "name": new_g.name,
                                  "color": new_g.color})

        new_name = (body.get("name") or "").strip()
        merge = bool(body.get("merge"))
        if not new_name:
            return json_response({"error": "name required"}, 400)
        old_name = g.name

        try:
            new_g = state.groups_repo.rename(group_id, new_name)
        except GroupNameConflict as e:
            if not merge:
                return json_response({
                    "error": "Зона с таким именем уже есть",
                    "existing_id": e.existing_id,
                }, 409)
            # Явное слияние: перепривязываем счётчики старой зоны на
            # уже существующую и удаляем зону-дубль (A5).
            existing = state.groups_repo.get_by_id(e.existing_id)
            if state.meters_repo is not None and existing is not None:
                for m in state.meters_repo.list_all():
                    if m.group_id == group_id:
                        state.meters_repo.update(m.device_id, group=existing.name)
            state.groups_repo.delete(group_id)
            _sync_registry_groups(state)
            log.info("Слияние зон через API: %r -> %r (id=%d)",
                     old_name, existing.name if existing else new_name,
                     e.existing_id)
            return json_response({
                "ok": True, "id": e.existing_id,
                "name": existing.name if existing else new_name,
                "old_name": old_name, "merged": True,
            })
        except ValueError as e:
            return json_response({"error": str(e)}, 404)

        if "color" in body:
            try: state.groups_repo.set_color(new_g.id, body.get("color"))
            except ValueError: pass
            new_g = state.groups_repo.get_by_id(new_g.id)

        _sync_registry_groups(state)
        log.info("Переименована зона через API: %r -> %r", old_name, new_name)
        return json_response({"ok": True, "id": new_g.id, "name": new_g.name,
                              "old_name": old_name, "color": new_g.color})

    @app.route("/api/registry/groups/<int:group_id>", methods=["DELETE"])
    def api_registry_group_delete(group_id):
        """Удалить зону. Счётчики переходят в 'без зоны'."""
        if state.groups_repo is None:
            return json_response({"error": "groups_repo not available"}, 503)
        g = state.groups_repo.get_by_id(group_id)
        if g is None:
            return json_response({"error": "group not found", "id": group_id}, 404)
        name = g.name
        # Сначала убираем группу у всех счётчиков
        if state.meters_repo is not None:
            for m in state.meters_repo.list_all():
                if m.group_name == name:
                    state.meters_repo.update(m.device_id, group="")
        state.groups_repo.delete(group_id)
        _sync_registry_groups(state)
        log.info("Удалена зона через API: %r (id=%d)", name, group_id)
        return json_response({"ok": True, "id": group_id, "name": name})

    # ---- pages ----

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
        from werkzeug.exceptions import HTTPException
        if isinstance(e, HTTPException):
            return json_response({"error": e.name, "code": e.code}, e.code)
        log.exception("Unhandled HTTP error: %s", e)
        return json_response({"error": "internal", "detail": str(e)}, 500)

    return app


class ApiServer:
    def __init__(self, host, port, registry, meters_repo,
                 is_mqtt_connected, mqtt_message_count, mqtt_error_count,
                 wb_db_client=None, consumption_service=None,
                 aggregates_repo=None, aggregator=None,
                 groups_repo=None, alert_repo=None):
        self._host = host
        self._port = port
        self._app_state = _AppState(
            registry=registry, meters_repo=meters_repo,
            groups_repo=groups_repo, alert_repo=alert_repo,
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
            try: self._server.shutdown()
            except Exception: pass
        if self._thread is not None:
            self._thread.join(timeout=5.0)


_ROOT_HTML_FALLBACK = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><title>wb-energy-meter</title>
</head><body><h1>wb-energy-meter</h1>
<p>UI (static/index.html) не найден. API работает.</p>
<p><a href="/api/status">/api/status</a> · <a href="/api/docs">/api/docs</a></p>
</body></html>"""

_DOCS_HTML = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><title>wb-energy-meter API</title>
<style>body{font-family:system-ui,sans-serif;max-width:900px;margin:2em auto;
padding:0 1em;line-height:1.5}
.ep{border:1px solid #e1e4e8;border-radius:6px;padding:1em;margin:1em 0}
.method{display:inline-block;padding:2px 8px;border-radius:3px;
font-size:.85em;font-weight:bold;color:#fff}
.get{background:#2188ff}.post{background:#22863a}
.patch{background:#e36209}.delete{background:#cb2431}
code{background:#f6f8fa;padding:2px 6px;border-radius:3px}
.path{font-family:monospace;font-size:1.05em;margin-left:.5em}</style>
</head><body>
<h1>wb-energy-meter — API</h1>
<div class="ep"><span class="method get">GET</span>
<span class="path">/api/status</span>
<p>Сводка: версия, MQTT, список счётчиков со статусами.</p></div>
<div class="ep"><span class="method get">GET</span>
<span class="path">/api/meters</span>
<p>Список зарегистрированных счётчиков.</p></div>
<div class="ep"><span class="method get">GET</span>
<span class="path">/api/meters/unregistered</span>
<p>Счётчики, видимые в MQTT, но не добавленные в реестр.</p></div>
<div class="ep"><span class="method get">GET</span>
<span class="path">/api/meters/&lt;id&gt;/consumption?period=today</span>
<p>Расход за период. Периоды: today yesterday this_month last_month last_24h last_7d last_30d или ?from=YYYY-MM-DD&amp;to=YYYY-MM-DD</p></div>
<div class="ep"><span class="method get">GET</span>
<span class="path">/api/meters/&lt;id&gt;/hourly?period=last_7d</span>
<p>Почасовые агрегаты для графиков.</p></div>
<div class="ep"><span class="method get">GET</span>
<span class="path">/api/summary/consumption?period=this_month</span>
<p>Расход по всем счётчикам с итогом.</p></div>
<div class="ep"><span class="method get">GET</span>
<span class="path">/api/aggregates/status</span>
<p>Статистика агрегатов и воркера.</p></div>
<div class="ep"><span class="method post">POST</span>
<span class="path">/api/registry/meters</span>
<p>Добавить счётчик. Body: <code>{"device_id":"wb-map3e_17","display_name":"Ввод 1","group":"Щит 1"}</code></p></div>
<div class="ep"><span class="method patch">PATCH</span>
<span class="path">/api/registry/meters/&lt;id&gt;</span>
<p>Переименовать / сменить группу. Body: <code>{"display_name":"Новое имя","group":"Щит 2"}</code></p></div>
<div class="ep"><span class="method delete">DELETE</span>
<span class="path">/api/registry/meters/&lt;id&gt;</span>
<p>Удалить счётчик из реестра.</p></div>
<div class="ep"><span class="method get">GET</span>
<span class="path">/api/registry/groups</span>
<p>Список всех групп.</p></div>
<div class="ep"><span class="method get">GET</span>
<span class="path">/api/meters/&lt;id&gt;/channel-history?control=...&amp;period=...</span>
<p>История значений одного параметра (для графика в карточке счётчика).</p></div>
<div class="ep"><span class="method get">GET</span>
<span class="path">/api/channels/dictionary</span>
<p>Словарь каналов: русские названия, единицы, подсказки, категории.</p></div>
</body></html>"""

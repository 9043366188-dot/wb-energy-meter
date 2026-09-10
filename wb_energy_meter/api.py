"""HTTP API на Flask."""

from __future__ import annotations

import logging
import os
import threading
import time

from flask import Flask, Response, request

from . import __version__
from . import updater as _updater_module
from .aggregates_repo import align_hour_down
from .channels import (CATEGORIES, CHANNEL_INFO, get_channel_info,
                       localize_units)
from .model import MeterStatus
from .periods import PERIOD_PRESETS, build_period, parse_user_datetime
from .repo import GroupNameConflict
from .wb_db_client import RpcError

from . import image_meta as _image_meta_module
from . import plan_geo as _plan_geo_module
from . import plan_repo as _plan_repo_module
from . import wb_serial_config as _wb_serial_module

log = logging.getLogger(__name__)

# Ключ в таблице kv: список device_id, для которых конфиг драйвера уже
# поправлен, но wb-mqtt-serial ещё не перезапущен. Именно в kv, а не в
# памяти — список обязан пережить перезапуск НАШЕГО сервиса (§4.2 ТЗ).
PENDING_RESTART_KEY = "wb_serial_pending_restart"


class _WbSerialDefaults:
    """Значения по умолчанию, если состояние собрано без секции
    wb_serial (старые вызовы create_app в тестах)."""
    config_path = _wb_serial_module.DEFAULT_CONFIG_PATH
    templates_dirs = list(_wb_serial_module.DEFAULT_TEMPLATES_DIRS)
    allow_edit = False
    backup_dir = _wb_serial_module.DEFAULT_BACKUP_DIR
    service_name = _wb_serial_module.DEFAULT_SERVICE_NAME


class _AppState:
    def __init__(self, registry, meters_repo, is_mqtt_connected,
                 mqtt_message_count, mqtt_error_count,
                 wb_db_client, consumption_service, started_at,
                 aggregates_repo=None, aggregator=None,
                 groups_repo=None, alert_repo=None,
                 update_config=None, updater=None,
                 status_path=None, install_dir=None, http_port=None,
                 wb_serial_config=None, kv_repo=None,
                 wb_serial=None, service_restarter=None,
                 plan_repo=None, plan_zone_repo=None, plan_link_repo=None,
                 plans_dir=None, db=None):
        # `db` (wb_energy_meter.db.Database) — только для /api/v2 (этап C):
        # существующие поля выше остаются отдельными репозиториями legacy
        # API, db добавлен по необходимости и не меняет их поведение.
        self.db = db
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
        # Самообновление (ТЗ v0.9.0). `updater` — модуль (или подмена в
        # тестах) с функциями check_remote/read_status/start_update/...;
        # по умолчанию — настоящий wb_energy_meter.updater.
        self.update_config = update_config
        self.updater = updater if updater is not None else _updater_module
        self.status_path = status_path
        self.install_dir = install_dir
        self.http_port = http_port
        # Канал Uptime и конфиг wb-mqtt-serial (ТЗ v0.10.0).
        # `wb_serial` — модуль (подменяется в тестах), `kv_repo` хранит
        # список счётчиков с неприменёнными изменениями,
        # `service_restarter` — точка подмены systemctl в тестах.
        self.wb_serial_config = (wb_serial_config if wb_serial_config
                                 is not None else _WbSerialDefaults())
        self.kv_repo = kv_repo
        self.wb_serial = (wb_serial if wb_serial is not None
                          else _wb_serial_module)
        self.service_restarter = service_restarter
        # План объекта: зоны на схеме и кабельные связи (ТЗ v0.11.0).
        # Зона на плане ссылается на существующую meter_groups — вторая
        # сущность "зона" не заводится.
        self.plan_repo = plan_repo
        self.plan_zone_repo = plan_zone_repo
        self.plan_link_repo = plan_link_repo
        self.plans_dir = plans_dir


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
            # иначе — из словаря каналов. Единицы из meta приходят
            # латиницей ("V", "kWh") — переводим на русский.
            "units": (localize_units(meta_units) if meta_units
                      else info["units"]),
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


def build_selfcheck_result(static_dir, vendor_dir, version):
    """Самопроверка интерфейса (ТЗ v0.11.1, §2.1): может ли index.html
    вообще загрузиться. Достаёт из index.html все ссылки вида
    src="/static/..." и href="/static/..." (та же регулярка, что и в
    tests/test_step11_plan.py::test_static_vendor_serves_every_referenced_file)
    и для каждой проверяет напрямую на диске — без единого HTTP-запроса
    к самому себе, — что файл существует, читается и непустой.

    Разрешение пути повторяет логику маршрута /static/vendor/<file>
    (см. static_vendor в create_app): сегменты без "."/".."/абсолютных
    путей + realpath обязан остаться внутри разрешённого каталога — чтобы
    проверка совпадала с тем, что реально отдаст сервер, а не жила
    отдельной жизнью. Вынесена на уровень модуля (а не внутрь create_app),
    чтобы тесты могли прогнать её на временном каталоге с намеренно
    битыми/отсутствующими файлами, не трогая настоящий
    wb_energy_meter/static (см. tests/test_step12_selfcheck.py).

    Никогда не бросает исключения — при любом сбое возвращает ok=False
    с человекочитаемой причиной; вызывающая сторона (маршрут /api/selfcheck)
    всегда отвечает 200, читать нужно поле "ok" (§2.1 ТЗ)."""
    import re

    def resolve(ref):
        if not ref.startswith("/static/"):
            return None, "не ссылка на /static/"
        rel = ref[len("/static/"):]
        parts = rel.replace("\\", "/").split("/")
        for part in parts:
            if part in ("", ".", "..") or os.path.isabs(part):
                return None, "недопустимый путь"
        if parts[0] == "vendor" and len(parts) > 1:
            full = os.path.join(vendor_dir, *parts[1:])
            root = os.path.realpath(vendor_dir)
        else:
            full = os.path.join(static_dir, *parts)
            root = os.path.realpath(static_dir)
        real_full = os.path.realpath(full)
        if real_full != root and not real_full.startswith(root + os.sep):
            return None, "путь вне каталога static"
        return real_full, None

    index_path = os.path.join(static_dir, "index.html")
    try:
        with open(index_path, "r", encoding="utf-8") as f:
            html = f.read()
    except (OSError, UnicodeDecodeError) as e:
        return {
            "ok": False,
            "checked": 0,
            "failed": [{"ref": "index.html",
                        "reason": "не удалось прочитать index.html: %s" % e}],
            "version": version,
        }

    refs = sorted(set(re.findall(r'(?:src|href)="(/static/[^"]+)"', html)))
    failed = []
    for ref in refs:
        path, reason = resolve(ref)
        if path is None:
            failed.append({"ref": ref, "reason": reason})
            continue
        if not os.path.isfile(path):
            failed.append({"ref": ref, "reason": "файл не найден"})
            continue
        try:
            if os.path.getsize(path) == 0:
                failed.append({"ref": ref, "reason": "файл пустой"})
                continue
            with open(path, "rb") as f:
                f.read(1)
        except OSError as e:
            failed.append({"ref": ref, "reason": "не читается: %s" % e})

    return {
        "ok": not failed,
        "checked": len(refs),
        "failed": failed,
        "version": version,
    }


def create_app(state):
    app = Flask("wb_energy_meter")
    app.config["JSON_SORT_KEYS"] = False
    app.config["JSON_AS_ASCII"] = False
    # Жёсткий предел размера тела запроса (загрузка плана, §6 ТЗ v0.11.0):
    # Werkzeug обрывает приём, не буферизуя лишнее в память. Небольшой
    # запас — на служебные поля и границы multipart-формы.
    app.config["MAX_CONTENT_LENGTH"] = (
        _plan_repo_module.MAX_UPLOAD_BYTES + 1024 * 1024)

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

    # ---- update (самообновление из GitHub, ТЗ v0.9.0) ----

    @app.route("/api/update/check")
    def api_update_check():
        uc = state.update_config
        if uc is None or not uc.enabled:
            return json_response(
                {"error": "Самообновление отключено в конфиге "
                          "(update.enabled: false)"}, 503)
        installed = state.updater.get_installed_info(state.install_dir)
        try:
            remote = state.updater.check_remote(
                uc.repo_owner, uc.repo_name, uc.ref,
                timeout=uc.check_timeout_s)
        except state.updater.UpdateCheckError as e:
            return json_response({"error": str(e)}, 502)
        available = state.updater.is_update_available(installed, remote)
        return json_response({
            "current": installed,
            "remote": remote,
            "update_available": available,
            "allow_from_ui": bool(uc.allow_from_ui),
        })

    @app.route("/api/update/status")
    def api_update_status():
        # Никогда не 500: нет файла -> {"state": "idle"} — это гарантирует
        # сам updater.read_status().
        if state.status_path is None:
            return json_response({"state": "idle"})
        return json_response(state.updater.read_status(state.status_path))

    @app.route("/api/update/start", methods=["POST"])
    def api_update_start():
        uc = state.update_config
        if uc is None or not uc.enabled:
            return json_response(
                {"error": "Самообновление отключено в конфиге "
                          "(update.enabled: false)"}, 503)
        if not uc.allow_from_ui:
            return json_response(
                {"error": "Обновление через веб-интерфейс запрещено "
                          "администратором (update.allow_from_ui: "
                          "false в конфиге). Обновите вручную по SSH: "
                          "scripts/install-from-github.sh"}, 403)

        current_status = state.updater.read_status(state.status_path)
        if current_status.get("state") in state.updater.ACTIVE_STATES:
            return json_response(
                {"error": "Обновление уже идёт", "status": current_status},
                409)

        body = request.get_json(silent=True) or {}
        commit = str(body.get("commit") or "").strip()
        if not commit:
            return json_response(
                {"error": "Не указан commit (тело запроса "
                          '{"commit": "<sha из /api/update/check>"})'}, 400)

        try:
            remote = state.updater.check_remote(
                uc.repo_owner, uc.repo_name, uc.ref,
                timeout=uc.check_timeout_s)
        except state.updater.UpdateCheckError as e:
            return json_response({"error": str(e)}, 502)

        if remote.get("commit") != commit:
            return json_response({
                "error": "Версия на GitHub изменилась, проверьте ещё раз",
                "remote": remote,
            }, 409)

        try:
            status = state.updater.start_update(
                install_dir=state.install_dir,
                status_path=state.status_path,
                repo_owner=uc.repo_owner, repo_name=uc.repo_name,
                ref=uc.ref, expected_sha=remote["commit"],
                http_port=state.http_port)
        except state.updater.UpdateInProgressError:
            return json_response({"error": "Обновление уже идёт"}, 409)

        log.info("Запущено самообновление: %s -> %s",
                 uc.repo_owner + "/" + uc.repo_name, remote.get("commit"))
        return json_response(status, 202)

    # ---- канал Uptime и конфиг wb-mqtt-serial (ТЗ v0.10.0) ----

    def _ws_cfg():
        return state.wb_serial_config

    def _pending_get():
        """Список счётчиков с неприменёнными изменениями конфига.
        Хранится в kv (переживает перезапуск сервиса); без kv_repo —
        деградируем до памяти процесса, чтобы API оставался рабочим."""
        if state.kv_repo is not None:
            try:
                val = state.kv_repo.get(PENDING_RESTART_KEY, [])
            except Exception:
                log.exception("Не удалось прочитать %s из kv",
                              PENDING_RESTART_KEY)
                val = []
        else:
            val = getattr(state, "_pending_memory", [])
        if not isinstance(val, list):
            return []
        return [str(v) for v in val]

    def _pending_set(items):
        items = sorted(set(str(i) for i in items))
        if state.kv_repo is not None:
            try:
                state.kv_repo.set(PENDING_RESTART_KEY, items)
                return items
            except Exception:
                log.exception("Не удалось записать %s в kv",
                              PENDING_RESTART_KEY)
        state._pending_memory = items
        return items

    def _read_wb_config():
        """(data, sha, error_text). Ошибка чтения — НЕ 500: файла может
        не быть на машине разработчика, в тестах, в контейнере (§3.2)."""
        ws = state.wb_serial
        try:
            data, sha = ws.load_config(_ws_cfg().config_path)
            return data, sha, None
        except ws.WbSerialConfigError as e:
            return None, None, str(e)

    @app.route("/api/meters/<device_id>/uptime-channel")
    def api_meter_uptime_channel(device_id):
        """Вердикт по каналу Uptime одного счётчика (§3.5 ТЗ v0.10.0).

        device_id используется ТОЛЬКО для поиска по уже разобранному
        JSON — ни в пути, ни в командах он не участвует."""
        ws = state.wb_serial
        cfg = _ws_cfg()
        data, sha, err = _read_wb_config()
        id_map = (ws.build_template_id_map(cfg.templates_dirs)
                  if data is not None else {})
        meter = state.registry.get(device_id)
        out = ws.describe_meter(
            meter, device_id, data, id_map, config_error=err,
            allow_edit=bool(cfg.allow_edit))
        out["sha256"] = sha
        out["config_path"] = cfg.config_path
        out["pending"] = device_id in _pending_get()
        return json_response(out)

    @app.route("/api/uptime-channel/summary")
    def api_uptime_channel_summary():
        """То же по всем счётчикам реестра. Конфиг читается ОДИН раз на
        весь ответ, а не по разу на счётчик."""
        ws = state.wb_serial
        cfg = _ws_cfg()
        data, sha, err = _read_wb_config()
        id_map = (ws.build_template_id_map(cfg.templates_dirs)
                  if data is not None else {})
        pending = _pending_get()
        items = []
        for m in state.registry.all():
            row = ws.describe_meter(
                m, m.device_id, data, id_map, config_error=err,
                allow_edit=bool(cfg.allow_edit))
            row["display_name"] = m.effective_name
            row["pending"] = m.device_id in pending
            items.append(row)
        counts = {}
        for row in items:
            counts[row["state"]] = counts.get(row["state"], 0) + 1
        return json_response({
            "allow_edit": bool(cfg.allow_edit),
            "config_path": cfg.config_path,
            "config_error": err,
            "sha256": sha,
            "count": len(items),
            "by_state": counts,
            "pending": pending,
            "needs_restart": bool(pending),
            "items": items,
        })

    @app.route("/api/wb-config/pending")
    def api_wb_config_pending():
        pending = _pending_get()
        return json_response({
            "pending": pending,
            "needs_restart": bool(pending),
            "allow_edit": bool(_ws_cfg().allow_edit),
            "service_name": _ws_cfg().service_name,
        })

    @app.route("/api/wb-config/enable-uptime", methods=["POST"])
    def api_wb_config_enable_uptime():
        """Включить канал Uptime у одного устройства.
        Body: {"device_id": "...", "sha256": "..."}

        Всё, что может пойти не так, заканчивается отказом БЕЗ записи:
        правка запрещена (403), файл не JSON / устройство не найдено /
        хеш не совпал (409). См. §5 ТЗ — это главное в задаче."""
        ws = state.wb_serial
        cfg = _ws_cfg()
        if not cfg.allow_edit:
            return json_response({
                "error": "Автоматическая правка конфига драйвера выключена "
                         "(wb_serial.allow_edit: false в "
                         "/etc/wb-energy-meter.conf). Включите канал вручную: "
                         "Device Manager → устройство → HW Info → Uptime → "
                         "in queue order, либо разрешите правку в конфиге "
                         "сервиса и перезапустите wb-energy-meter.",
            }, 403)

        body = request.get_json(silent=True) or {}
        device_id = str(body.get("device_id") or "").strip()
        expected_sha = str(body.get("sha256") or "").strip()
        if not device_id:
            return json_response({"error": "device_id обязателен"}, 400)
        if not expected_sha:
            return json_response(
                {"error": "sha256 обязателен — возьмите его из "
                          "/api/uptime-channel/summary"}, 400)

        try:
            data, sha = ws.load_config(cfg.config_path)
        except ws.WbSerialConfigError as e:
            return json_response({"error": str(e)}, 409)

        if sha != expected_sha:
            return json_response({
                "error": "Конфиг изменился, обновите страницу",
                "sha256": sha,
            }, 409)

        id_map = ws.build_template_id_map(cfg.templates_dirs)
        found = ws.find_device(data, device_id, id_map)
        if found is None:
            return json_response({
                "error": "Устройство %s не найдено в %s (или найдено больше "
                         "одного подходящего) — правка отменена" %
                         (device_id, cfg.config_path),
            }, 409)
        port_idx, dev_idx, device = found

        if ws.uptime_channel_state(device) == "enabled":
            return json_response({
                "ok": True, "already_enabled": True, "device_id": device_id,
                "sha256": sha, "pending": _pending_get(),
                "needs_restart": bool(_pending_get()),
            })

        new_data = ws.set_uptime_enabled(data, port_idx, dev_idx)

        def _verify(written):
            f = ws.find_device(written, device_id, id_map)
            return f is not None and ws.uptime_channel_state(f[2]) == "enabled"

        try:
            result = ws.save_config(
                cfg.config_path, new_data, expected_sha=expected_sha,
                backup_dir=cfg.backup_dir, verify=_verify)
        except ws.WbSerialConflict as e:
            return json_response({"error": str(e)}, 409)
        except ws.WbSerialConfigError as e:
            return json_response({"error": str(e)}, 500)

        pending = _pending_set(_pending_get() + [device_id])
        log.info("Канал Uptime включён в конфиге драйвера для %s "
                 "(бэкап %s)", device_id, result.get("backup_path"))
        return json_response({
            "ok": True,
            "device_id": device_id,
            "sha256": result["sha256"],
            "backup_path": result["backup_path"],
            "pending": pending,
            "needs_restart": True,
        })

    @app.route("/api/wb-config/restart-driver", methods=["POST"])
    def api_wb_config_restart_driver():
        """Перезапустить wb-mqtt-serial. Только по явной кнопке: на
        несколько секунд прерывается опрос ВСЕХ устройств контроллера.

        Если драйвер не поднялся — откатываем конфиг из последнего
        бэкапа и пробуем ещё раз (§4.2, §5.7)."""
        ws = state.wb_serial
        cfg = _ws_cfg()
        if not cfg.allow_edit:
            return json_response({
                "error": "Перезапуск драйвера из веб-интерфейса выключен "
                         "(wb_serial.allow_edit: false в конфиге сервиса). "
                         "Перезапустите вручную: "
                         "systemctl restart wb-mqtt-serial",
            }, 403)

        restarter = state.service_restarter or ws.restart_service
        ok, detail = restarter(cfg.service_name)
        if ok:
            _pending_set([])
            log.info("Драйвер %s перезапущен, pending очищен",
                     cfg.service_name)
            return json_response({
                "ok": True, "pending": [], "needs_restart": False,
                "detail": "Драйвер %s перезапущен" % cfg.service_name,
            })

        backup = ws.latest_backup(cfg.config_path, cfg.backup_dir)
        rollback_detail = "бэкапов не найдено, конфиг не откатывался"
        second_ok = False
        if backup:
            try:
                ws.restore_backup(backup, cfg.config_path)
                rollback_detail = "конфиг восстановлен из %s" % backup
                second_ok, second_detail = restarter(cfg.service_name)
                rollback_detail += (
                    "; повторный запуск: %s" %
                    ("успешно" if second_ok else second_detail))
            except OSError as e:
                rollback_detail = ("ОТКАТ НЕ УДАЛСЯ (%s), бэкап лежит "
                                   "здесь: %s" % (e, backup))
        log.error("Драйвер %s не поднялся: %s (%s)",
                  cfg.service_name, detail, rollback_detail)
        return json_response({
            "error": "Драйвер %s не поднялся после перезапуска: %s" %
                     (cfg.service_name, detail),
            "rollback": rollback_detail,
            "backup_path": backup,
            "restored": bool(backup),
            "driver_active_after_rollback": second_ok,
            "pending": _pending_get(),
            "needs_restart": bool(_pending_get()),
        }, 500)

    # ---- статика вендоренных библиотек (ТЗ v0.11.0, §4) ----

    _VENDOR_DIR = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "static", "vendor")
    _VENDOR_MIME = {
        ".js": "application/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8",
        ".png": "image/png",
    }

    @app.route("/static/vendor/<path:filename>")
    def static_vendor(filename):
        """Отдаёт вендоренные leaflet/geoman/alpine (§4 ТЗ).

        Обход каталога закрыт тремя независимыми проверками, все на
        стандартной библиотеке:
          1. сегменты пути разбираем сами и отвергаем пустые, `.`, `..`,
             обратные слэши и абсолютные пути;
          2. после склейки сверяем `realpath` — результат обязан лежать
             внутри каталога vendor (ловит в том числе симлинки);
          3. белый список расширений — даже если что-то проскочит,
             произвольный файл наружу не уйдёт.

        ВАЖНО: намеренно НЕ используем `werkzeug.safe_join`. Она живёт в
        разных модулях в разных версиях: в Werkzeug 1.0.1 (Debian
        bullseye, python3-werkzeug на контроллере) — в
        `werkzeug.security`, а в `werkzeug.utils` появилась только во
        2.0. Импорт `from werkzeug.utils import safe_join` на боевом
        контроллере падал с ImportError, обработчик отдавал 500,
        `alpine.min.js` не загружался — и весь интерфейс превращался в
        белый экран (см. AGENTS.md). Своя проверка от версий не зависит.
        """
        parts = filename.replace("\\", "/").split("/")
        for part in parts:
            if part in ("", ".", "..") or os.path.isabs(part):
                return json_response({"error": "not found"}, 404)
        full = os.path.join(_VENDOR_DIR, *parts)
        if not os.path.isfile(full):
            return json_response({"error": "not found"}, 404)
        real_vendor = os.path.realpath(_VENDOR_DIR)
        real_full = os.path.realpath(full)
        if real_full != real_vendor and not real_full.startswith(
                real_vendor + os.sep):
            return json_response({"error": "not found"}, 404)
        ext = os.path.splitext(real_full)[1].lower()
        mime = _VENDOR_MIME.get(ext)
        if mime is None:
            return json_response({"error": "not found"}, 404)
        with open(real_full, "rb") as f:
            data = f.read()
        resp = Response(data, mimetype=mime)
        resp.headers["Cache-Control"] = "public, max-age=86400"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        return resp

    # ---- самопроверка интерфейса (ТЗ v0.11.1) ----
    #
    # 09.09.2026 обновление до v0.11.0 положило контроллер: маршрут
    # /static/vendor/<file> падал по несовместимости с Werkzeug 1.0.1,
    # alpine.min.js не грузился, Alpine не стартовал — белый экран без
    # единой ошибки в логе сервиса. /health при этом отвечал "всё
    # хорошо": демон был жив, просто отдавал сломанный интерфейс.
    # Автооткат self-update.sh проверял только is-active + /health и
    # поломку не увидел. Логика проверки — в build_selfcheck_result()
    # на уровне модуля (см. ниже), чтобы тесты могли прогнать её на
    # временном каталоге с намеренно битыми файлами, не трогая реальный
    # wb_energy_meter/static.
    _STATIC_DIR = os.path.dirname(_VENDOR_DIR)

    @app.route("/api/selfcheck")
    def api_selfcheck():
        """Может ли интерфейс вообще загрузиться (см. докстринг выше).

        HTTP-код ВСЕГДА 200 — это диагностика, а не сама ошибка; читать
        нужно поле "ok". Исключений наружу не бросает."""
        return json_response(
            build_selfcheck_result(_STATIC_DIR, _VENDOR_DIR, __version__))

    # ---- план объекта: зоны на схеме и кабельные связи (ТЗ v0.11.0) ----

    def _plan_or_404(plan_id):
        if state.plan_repo is None:
            return None, json_response({"error": "plan_repo not available"}, 503)
        plan = state.plan_repo.get_by_id(plan_id)
        if plan is None:
            return None, json_response(
                {"error": "plan not found", "id": plan_id}, 404)
        return plan, None

    def _plan_zone_dict(zone):
        d = zone.to_dict()
        group = (state.groups_repo.get_by_id(zone.group_id)
                if state.groups_repo else None)
        d["group_name"] = group.name if group else None
        d["color"] = group.color if group else None
        return d

    @app.route("/api/plans")
    def api_plans_list():
        if state.plan_repo is None:
            return json_response({"error": "plan_repo not available"}, 503)
        plans = state.plan_repo.list_all()
        return json_response({"count": len(plans),
                              "items": [p.to_dict() for p in plans]})

    @app.route("/api/plans", methods=["POST"])
    def api_plans_create():
        """multipart/form-data: name, file. См. §6 ТЗ — файл принимается
        только по сигнатуре PNG/JPEG, имя на диске генерируем мы, имя из
        запроса не используется НИГДЕ (даже в логе)."""
        if state.plan_repo is None:
            return json_response({"error": "plan_repo not available"}, 503)
        if state.plans_dir is None:
            return json_response({"error": "plans_dir not configured"}, 503)

        max_bytes = _plan_repo_module.MAX_UPLOAD_BYTES
        # Основная защита — app.config["MAX_CONTENT_LENGTH"] (Werkzeug
        # обрывает приём тела запроса, не буферизуя его целиком). Эта
        # проверка — дополнительная, на случай отсутствующего/лживого
        # Content-Length у клиента.
        if (request.content_length is not None
                and request.content_length > max_bytes):
            return json_response(
                {"error": "Файл больше 10 МБ — загрузка отклонена"}, 413)

        upload = request.files.get("file")
        if upload is None:
            return json_response(
                {"error": "Файл не передан (поле формы file)"}, 400)
        name = (request.form.get("name") or "").strip()
        if not name:
            return json_response(
                {"error": "Укажите имя плана (поле формы name)"}, 400)

        data = upload.stream.read(max_bytes + 1)
        if len(data) > max_bytes:
            return json_response(
                {"error": "Файл больше 10 МБ — загрузка отклонена"}, 413)
        if not data:
            return json_response({"error": "Пустой файл"}, 400)

        try:
            img_format, width, height = (
                _image_meta_module.parse_image_size(data))
        except _image_meta_module.ImageFormatError as e:
            return json_response({"error": str(e)}, 400)

        try:
            plan = state.plan_repo.create(
                name=name, image_format=img_format, width=width,
                height=height, image_bytes=data,
                plans_directory=state.plans_dir)
        except _plan_repo_module.PlanError as e:
            return json_response({"error": str(e)}, 400)

        log.info("Загружен план объекта: id=%d, %dx%d, формат=%s",
                 plan.id, width, height, img_format)
        return json_response(plan.to_dict(), 201)

    @app.route("/api/plans/<int:plan_id>")
    def api_plan_detail(plan_id):
        plan, err = _plan_or_404(plan_id)
        if err:
            return err
        zones = (state.plan_zone_repo.list_by_plan(plan_id)
                 if state.plan_zone_repo else [])
        links = (state.plan_link_repo.list_by_plan(plan_id)
                if state.plan_link_repo else [])
        out = plan.to_dict()
        out["zones"] = [_plan_zone_dict(z) for z in zones]
        out["links"] = [l.to_dict() for l in links]
        return json_response(out)

    @app.route("/api/plans/<int:plan_id>/image")
    def api_plan_image(plan_id):
        """Отдаёт файл картинки плана. Путь строится ИСКЛЮЧИТЕЛЬНО из
        image_file, прочитанного из БД по числовому id — из запроса в
        путь ничего не попадает."""
        plan, err = _plan_or_404(plan_id)
        if err:
            return err
        if state.plans_dir is None:
            return json_response({"error": "plans_dir not configured"}, 503)
        data = _plan_repo_module.read_plan_image(
            state.plans_dir, plan.image_file)
        if data is None:
            return json_response({"error": "image file not found"}, 404)
        ext = os.path.splitext(plan.image_file)[1].lower()
        mime = "image/png" if ext == ".png" else "image/jpeg"
        resp = Response(data, mimetype=mime)
        resp.headers["Cache-Control"] = "private, max-age=3600"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        return resp

    @app.route("/api/plans/<int:plan_id>", methods=["PATCH"])
    def api_plan_update(plan_id):
        """Body: {name?, is_default?}"""
        plan, err = _plan_or_404(plan_id)
        if err:
            return err
        import json as _json
        try:
            body = _json.loads(request.data.decode("utf-8"))
        except Exception:
            return json_response({"error": "invalid JSON body"}, 400)
        kwargs = {}
        if "name" in body:
            kwargs["name"] = body["name"]
        if "is_default" in body:
            kwargs["is_default"] = bool(body["is_default"])
        if not kwargs:
            return json_response({"error": "nothing to update"}, 400)
        try:
            new_plan = state.plan_repo.update(plan_id, **kwargs)
        except _plan_repo_module.PlanError as e:
            return json_response({"error": str(e)}, 400)
        return json_response(new_plan.to_dict())

    @app.route("/api/plans/<int:plan_id>", methods=["DELETE"])
    def api_plan_delete(plan_id):
        plan, err = _plan_or_404(plan_id)
        if err:
            return err
        state.plan_repo.delete(plan_id, state.plans_dir)
        log.info("Удалён план объекта id=%d", plan_id)
        return json_response({"ok": True, "id": plan_id})

    @app.route("/api/plans/<int:plan_id>/zones/<int:group_id>",
              methods=["PUT"])
    def api_plan_zone_put(plan_id, group_id):
        """Body: {shape_type?, geometry, anchor?}"""
        plan, err = _plan_or_404(plan_id)
        if err:
            return err
        if state.groups_repo is None or state.plan_zone_repo is None:
            return json_response({"error": "not available"}, 503)
        group = state.groups_repo.get_by_id(group_id)
        if group is None:
            return json_response(
                {"error": "group not found", "id": group_id}, 404)
        import json as _json
        try:
            body = _json.loads(request.data.decode("utf-8"))
        except Exception:
            return json_response({"error": "invalid JSON body"}, 400)
        shape_type = body.get("shape_type") or "polygon"
        geometry = body.get("geometry")
        anchor = body.get("anchor")
        if geometry is None:
            return json_response({"error": "geometry обязательна"}, 400)
        try:
            zone = state.plan_zone_repo.upsert(
                plan, group_id, shape_type, geometry, anchor)
        except (ValueError, _plan_repo_module.PlanError) as e:
            return json_response({"error": str(e)}, 400)
        log.info("Зона на плане сохранена: plan_id=%d group_id=%d",
                 plan_id, group_id)
        return json_response(_plan_zone_dict(zone))

    @app.route("/api/plans/<int:plan_id>/zones/<int:group_id>",
              methods=["DELETE"])
    def api_plan_zone_delete(plan_id, group_id):
        plan, err = _plan_or_404(plan_id)
        if err:
            return err
        if state.plan_zone_repo is None:
            return json_response({"error": "not available"}, 503)
        removed = state.plan_zone_repo.delete(plan_id, group_id)
        if not removed:
            return json_response({"error": "zone not found on this plan"}, 404)
        log.info("Зона убрана с плана: plan_id=%d group_id=%d",
                 plan_id, group_id)
        return json_response({"ok": True})

    @app.route("/api/plans/<int:plan_id>/links", methods=["POST"])
    def api_plan_link_create(plan_id):
        """Body: {from_zone_id, to_zone_id, source_meter_id?,
        rated_current_a?, waypoints?, label?}"""
        plan, err = _plan_or_404(plan_id)
        if err:
            return err
        if state.plan_link_repo is None or state.plan_zone_repo is None:
            return json_response({"error": "not available"}, 503)
        import json as _json
        try:
            body = _json.loads(request.data.decode("utf-8"))
        except Exception:
            return json_response({"error": "invalid JSON body"}, 400)
        try:
            from_zone_id = int(body.get("from_zone_id"))
            to_zone_id = int(body.get("to_zone_id"))
        except (TypeError, ValueError):
            return json_response(
                {"error": "from_zone_id и to_zone_id обязательны"}, 400)
        try:
            link = state.plan_link_repo.create(
                plan_id, from_zone_id, to_zone_id, state.plan_zone_repo,
                source_meter_id=body.get("source_meter_id"),
                rated_current_a=body.get("rated_current_a"),
                waypoints=body.get("waypoints"),
                label=body.get("label"), plan=plan)
        except (ValueError, _plan_repo_module.PlanError) as e:
            return json_response({"error": str(e)}, 400)
        log.info("Создана связь на плане: id=%d plan_id=%d %d->%d",
                 link.id, plan_id, from_zone_id, to_zone_id)
        return json_response(link.to_dict(), 201)

    @app.route("/api/plans/<int:plan_id>/links/<int:link_id>",
              methods=["PATCH"])
    def api_plan_link_update(plan_id, link_id):
        plan, err = _plan_or_404(plan_id)
        if err:
            return err
        if state.plan_link_repo is None or state.plan_zone_repo is None:
            return json_response({"error": "not available"}, 503)
        existing = state.plan_link_repo.get_by_id(link_id)
        if existing is None or existing.plan_id != plan_id:
            return json_response({"error": "link not found"}, 404)
        import json as _json
        try:
            body = _json.loads(request.data.decode("utf-8"))
        except Exception:
            return json_response({"error": "invalid JSON body"}, 400)
        fields = set(body.keys())
        kwargs = {}
        if "from_zone_id" in fields:
            try: kwargs["from_zone_id"] = int(body["from_zone_id"])
            except (TypeError, ValueError):
                return json_response({"error": "from_zone_id должен быть числом"}, 400)
        if "to_zone_id" in fields:
            try: kwargs["to_zone_id"] = int(body["to_zone_id"])
            except (TypeError, ValueError):
                return json_response({"error": "to_zone_id должен быть числом"}, 400)
        if "source_meter_id" in fields:
            kwargs["source_meter_id"] = body["source_meter_id"]
        if "rated_current_a" in fields:
            kwargs["rated_current_a"] = body["rated_current_a"]
        if "waypoints" in fields:
            kwargs["waypoints"] = body["waypoints"]
        if "label" in fields:
            kwargs["label"] = body["label"]
        try:
            link = state.plan_link_repo.update(
                link_id, state.plan_zone_repo, plan=plan, _fields=fields,
                **kwargs)
        except (ValueError, _plan_repo_module.PlanError) as e:
            return json_response({"error": str(e)}, 400)
        return json_response(link.to_dict())

    @app.route("/api/plans/<int:plan_id>/links/<int:link_id>",
              methods=["DELETE"])
    def api_plan_link_delete(plan_id, link_id):
        plan, err = _plan_or_404(plan_id)
        if err:
            return err
        if state.plan_link_repo is None:
            return json_response({"error": "not available"}, 503)
        existing = state.plan_link_repo.get_by_id(link_id)
        if existing is None or existing.plan_id != plan_id:
            return json_response({"error": "link not found"}, 404)
        state.plan_link_repo.delete(link_id)
        log.info("Удалена связь на плане: id=%d plan_id=%d", link_id, plan_id)
        return json_response({"ok": True})

    @app.route("/api/plans/<int:plan_id>/live")
    def api_plan_live(plan_id):
        """Живые данные для отрисовки: мощность/статус зон, ток связей.
        Никогда не 500 — план без зон/счётчиков отдаёт 200 с null-ами
        (§9 п.9 ТЗ)."""
        plan, err = _plan_or_404(plan_id)
        if err:
            return err
        if state.plan_zone_repo is None or state.plan_link_repo is None:
            return json_response({"error": "not available"}, 503)
        try:
            period = build_period(**_parse_period_from_request())
        except ValueError as e:
            return json_response({"error": "bad period", "detail": str(e)}, 400)

        zones = state.plan_zone_repo.list_by_plan(plan_id)
        groups_by_id = {}
        if state.groups_repo is not None:
            for g in state.groups_repo.list_all():
                groups_by_id[g.id] = g
        meters_by_group = {}
        if state.meters_repo is not None:
            for m in state.meters_repo.list_all():
                meters_by_group.setdefault(m.group_id, []).append(m)

        zones_out = []
        for z in zones:
            group = groups_by_id.get(z.group_id)
            meter_rows = meters_by_group.get(z.group_id, [])
            meters_ok = 0
            worst = None
            power_kw = 0.0
            any_power = False
            for mr in meter_rows:
                ms = state.registry.get(mr.device_id) if state.registry else None
                st = ms.status if ms is not None else MeterStatus.UNKNOWN
                if st == MeterStatus.OK:
                    meters_ok += 1
                if worst is None or st.priority > worst.priority:
                    worst = st
                p = ms.get_float("Total P") if ms is not None else None
                if p is not None:
                    power_kw += p
                    any_power = True
            consumption_kwh = None
            if state.consumption_service is not None and meter_rows:
                total = 0.0
                got_any = False
                for mr in meter_rows:
                    try:
                        r = state.consumption_service.calculate(
                            mr.device_id, period)
                    except RpcError:
                        continue
                    except Exception:
                        log.exception(
                            "plan live: ошибка расчёта расхода %s",
                            mr.device_id)
                        continue
                    if r.consumption_kwh is not None:
                        total += r.consumption_kwh
                        got_any = True
                if got_any:
                    consumption_kwh = round(total, 6)
            zones_out.append({
                "group_id": z.group_id,
                "name": group.name if group else None,
                "color": group.color if group else None,
                "meters_total": len(meter_rows),
                "meters_ok": meters_ok,
                "worst_status": worst.value if worst is not None else None,
                "power_kw": round(power_kw, 6) if any_power else None,
                "consumption_kwh": consumption_kwh,
            })

        links = state.plan_link_repo.list_by_plan(plan_id)
        max_metric = 0.0
        link_metrics = []
        for l in links:
            current_a = None
            power_kw = None
            if l.source_meter_id and state.meters_repo is not None \
                    and state.registry is not None:
                meter_row = state.meters_repo.get_by_id(l.source_meter_id)
                if meter_row is not None:
                    ms = state.registry.get(meter_row.device_id)
                    if ms is not None:
                        currents = [ms.get_float(f"Irms {ph}")
                                   for ph in ("L1", "L2", "L3")]
                        currents = [c for c in currents if c is not None]
                        if currents:
                            current_a = max(currents)
                        power_kw = ms.get_float("Total P")
            metric = current_a if current_a is not None else (power_kw or 0.0)
            if metric and metric > max_metric:
                max_metric = metric
            link_metrics.append((l, current_a, power_kw, metric))

        links_out = []
        for l, current_a, power_kw, metric in link_metrics:
            load_pct = None
            state_lbl = "neutral"
            if current_a is not None and l.rated_current_a:
                load_pct = round(current_a / l.rated_current_a * 100, 1)
                if load_pct < 70:
                    state_lbl = "ok"
                elif load_pct < 90:
                    state_lbl = "warn"
                else:
                    state_lbl = "danger"
            links_out.append({
                "id": l.id, "from_zone_id": l.from_zone_id,
                "to_zone_id": l.to_zone_id, "label": l.label,
                "rated_current_a": l.rated_current_a,
                "current_a": current_a, "power_kw": power_kw,
                "load_pct": load_pct, "state": state_lbl,
                "weight": (round(metric / max_metric, 4)
                          if max_metric > 0 else 0.0),
            })

        return json_response({"zones": zones_out, "links": links_out})

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

    from . import api_v2 as _api_v2_module
    _api_v2_module.register_v2_routes(app, state, json_response)

    return app


class ApiServer:
    def __init__(self, host, port, registry, meters_repo,
                 is_mqtt_connected, mqtt_message_count, mqtt_error_count,
                 wb_db_client=None, consumption_service=None,
                 aggregates_repo=None, aggregator=None,
                 groups_repo=None, alert_repo=None,
                 update_config=None, updater=None,
                 status_path=None, install_dir=None,
                 wb_serial_config=None, kv_repo=None,
                 plan_repo=None, plan_zone_repo=None, plan_link_repo=None,
                 plans_dir=None, db=None):
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
            update_config=update_config, updater=updater,
            status_path=status_path, install_dir=install_dir,
            http_port=port,
            wb_serial_config=wb_serial_config, kv_repo=kv_repo,
            plan_repo=plan_repo, plan_zone_repo=plan_zone_repo,
            plan_link_repo=plan_link_repo, plans_dir=plans_dir,
            db=db,
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
<div class="ep"><span class="method get">GET</span>
<span class="path">/api/update/check</span>
<p>Сверить установленную версию с веткой main на GitHub. 503 если update.enabled: false.</p></div>
<div class="ep"><span class="method get">GET</span>
<span class="path">/api/update/status</span>
<p>Статус текущего/последнего обновления. Без файла статуса — {"state":"idle"}.</p></div>
<div class="ep"><span class="method post">POST</span>
<span class="path">/api/update/start</span>
<p>Запустить обновление. Body: <code>{"commit":"&lt;sha из /api/update/check&gt;"}</code>. 403 если update.allow_from_ui: false, 409 если уже идёт или sha устарел.</p></div>
<div class="ep"><span class="method get">GET</span>
<span class="path">/api/meters/&lt;id&gt;/uptime-channel</span>
<p>Состояние канала Uptime у счётчика: ok / disabled / not_in_config / stale / device_not_found / unknown.</p></div>
<div class="ep"><span class="method get">GET</span>
<span class="path">/api/uptime-channel/summary</span>
<p>То же по всем счётчикам одним запросом (конфиг драйвера читается один раз).</p></div>
<div class="ep"><span class="method post">POST</span>
<span class="path">/api/wb-config/enable-uptime</span>
<p>Включить канал Uptime в /etc/wb-mqtt-serial.conf. Body: <code>{"device_id":"wb-map3e_21","sha256":"&lt;из summary&gt;"}</code>. 403 если wb_serial.allow_edit: false, 409 если конфиг не JSON, устройство не найдено или sha256 разошёлся.</p></div>
<div class="ep"><span class="method get">GET</span>
<span class="path">/api/wb-config/pending</span>
<p>Счётчики с неприменёнными изменениями конфига драйвера.</p></div>
<div class="ep"><span class="method post">POST</span>
<span class="path">/api/wb-config/restart-driver</span>
<p>Перезапустить wb-mqtt-serial (прерывает опрос ВСЕХ устройств контроллера). При неудаче — откат конфига из бэкапа.</p></div>
</body></html>"""

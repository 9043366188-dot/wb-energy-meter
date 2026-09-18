"""HTTP-слой `/api/v2` (ТЗ §9.2) — регистрирует маршруты в приложении,
созданном `api.create_app()`.

Написано лично (не делегировано) — переводит исключения сервисов этапов
B/C (BindingConflict/TopologyConflict/AccountingConflict/ValueError) в
контракт ошибок §9.2 (`code`, русское `message`, `fields`, затронутые ID),
что и есть самая ответственная часть этого слоя.

Область покрытия в этой реализации (осознанно ограничена объёмом задачи,
см. TZ §9.2 таблицу маршрутов):

  РЕАЛИЗОВАНО: points (CRUD + bindings + replace-meter), locations (CRUD),
  topology/nodes, topology/edges (draft CRUD), topology/validate,
  topology/publish, balance-scopes (CRUD с версионируемым составом),
  metrics/query (measured/sum/balance/comparison), snapshot (этап E,
  ТЗ §8.2/§9.2 — текущие значения без исторических RPC, см. v2_snapshot),
  groups (этап E/F, ТЗ §4.4 — CRUD + версионируемая иерархия с защитой
  циклов и областью видимости категории, версионируемый состав точек
  (одна точка может состоять в нескольких группах), рекурсивный
  эффективный состав с дедупликацией/происхождением через
  group_repo_v2.GroupRepoV2 — см. v2_group_effective_members).

  Партия 2: глобальный протокол ревизий конфигурации (§6.1/§9.2 —
  `expected_revision`/409, см. revision_service.py) на ВСЕХ доменных
  изменяющих маршрутах выше; `GET /api/v2/revision`;
  `POST /api/v2/overview/summary` (§8.2 «Обзор» — итог объекта только
  через назначенный ввод, ветви, небаланс отдельной строкой, A03/A04/
  A08/A10/A11 — см. v2_overview_summary); `POST /api/v2/reports/query`
  (§8.4 «Отчёты» — срезы point/branch/group/balance_scope поверх того
  же accounting_service, что и Обзор/metrics/query, режимы состава
  группы as_was/current и сравнение периодов A21/A22 — см.
  v2_reports_query). metrics/query, overview/summary и reports/query
  держат ОДИН db.read() на весь расчёт (A43 — согласованный снимок
  конфигурации внутри запроса), см. докстринги этих функций.

  Партия 3: провижининг нового физического прибора (`meters`+
  `meter_sources`) прямо внутри `replace-meter` — тело `{"new_meter":
  {"controller_key":.., "device_id":.., "display_name":..}}` вместо
  готового `meter_source_id` (§5.4, задача 3, см. v2_point_replace_meter).
  `GET /api/v2/validation` (§8.2/§13, задача 4) — сводка незавершённой
  настройки: точки без прибора/места/группы/плана, узлы без связей,
  связи без измерения; точка ввода не считается проблемой в "без группы"
  (см. v2_validation).
  `GET /api/v2/migration/legacy-links` + `POST .../confirm` (задача 2) —
  мастер переноса связей старой модели планов в electrical_edges;
  только с явным подтверждением каждой связи, идемпотентно через
  migration_map (см. migration_wizard_service.py, v2_migration_legacy_links).
  `GET /api/v2/structure/points` (§8.3, задача 1) — поиск точек учёта по
  имени/коду/MQTT ID/серийнику прибора/пути размещения (A02) для левой
  панели дерева экрана «Структура»; отдаёт также непривязанные/
  неразмещённые точки (bound/placed_on_plan флагами, не прячет их) —
  инспектор и остальные числа берутся из уже существующих ручек
  (snapshot, bindings, groups, topology/*), см. v2_structure_points.

  НЕ РЕАЛИЗОВАНО (сознательно отложено — вне этой задачи):
  metrics/jobs (долгие расчёты/подписки); plans/items/layout (это стадия D
  плана — редактор плана v2); migration/status; инспектор объекта/
  однолинейная схема как второй канвас; адаптивная вёрстка (§8.5).
  Протокол ревизий не хранит и не восстанавливает историческую
  электрическую топологию "как было на ревизии N" (см. ограничение в
  revision_service.py) и не пишет change_log.

Ответ на ошибку — единый envelope §9.2: {"code", "message", "fields",
"ids", "path"}. 400 — неверный запрос/тип; 404 — неизвестный ID; 409 —
конфликт (двойной счёт, пересечение интервалов, топология, устаревшая
ревизия конфигурации).
"""

from __future__ import annotations

import logging
import time

import os

from datetime import datetime, timezone

from flask import request, Response

from .accounting_contract import ContractViolation, resolve_percentage
from .accounting_service import (
    AccountingConflict, measured_point, sum_points, balance, comparison,
)
from .binding_service import BindingConflict, PointBindingRepo
from .location_repo import LocationRepo
from .model import PHASES
from .point_repo import MeteringPointRepo, MeterSourceRepo
from .topology_service import (
    ElectricalNodeRepo, ElectricalEdgeRepo, TopologyConflict,
)
from .aggregates_repo import AggregateRepo
from .periods import parse_user_datetime
from .plan_repo import read_plan_image
from .plan_service_v2 import (
    SitePlanRepoV2, PlanItemRepo, PlanEdgeViewRepo,
    RevisionConflict, PlanError, save_plan_layout,
)
from .legacy_migration import (
    migrate_meters_and_groups, SCHEMA_MIGRATION_VERSION, DEFAULT_CONTROLLER_KEY,
)
from .plan_repo import SitePlanRepo, PlanZoneRepo, PlanLinkRepo
from . import migration_wizard_service
from .repo import GroupRepo, MeterRepo, validate_device_id
from .group_repo_v2 import GroupRepoV2
from .revision_service import (
    RevisionConflict as GlobalRevisionConflict, with_revision_check, bump_revision,
    current_revision_id, check_expected_revision, create_revision, revision_exists,
)
from dataclasses import asdict as _dataclass_asdict

log = logging.getLogger(__name__)


def _err(code, message, status, fields=None, ids=None, path=None):
    body = {"code": code, "message": message}
    if fields is not None:
        body["fields"] = fields
    if ids is not None:
        body["ids"] = ids
    if path is not None:
        body["path"] = path
    return body, status


def _unwrap_data(body):
    """§9.2: "Единый envelope записи: expected_revision, effective_from,
    reason, data". Полная проверка expected_revision — отдельная задача
    (см. модульный docstring); здесь достаточно снять data, если тело
    пришло в полном конверте, и принять плоское тело для совместимости
    с более простыми клиентами/тестами."""
    if isinstance(body, dict) and isinstance(body.get("data"), dict):
        return body["data"], body
    return (body if isinstance(body, dict) else {}), (body if isinstance(body, dict) else {})


def register_v2_routes(app, state, json_response):
    if state.db is None:
        log.warning("api_v2: state.db не задан — маршруты /api/v2 не зарегистрированы")
        return

    db = state.db

    def _point_repo(): return MeteringPointRepo(db)
    def _location_repo(): return LocationRepo(db)
    def _source_repo(): return MeterSourceRepo(db)
    def _binding_repo(): return PointBindingRepo(db)
    def _node_repo(): return ElectricalNodeRepo(db)
    def _edge_repo(): return ElectricalEdgeRepo(db)
    def _aggregates_repo(): return state.aggregates_repo or AggregateRepo(db)
    def _plan_repo_v2(): return SitePlanRepoV2(db, state.plans_dir)
    def _plan_item_repo(): return PlanItemRepo(db)
    def _plan_edge_view_repo(): return PlanEdgeViewRepo(db)
    def _group_repo_v2(): return GroupRepoV2(db)

    def _point_to_dict(p):
        return {
            "id": p.id, "code": p.code, "name": p.name,
            "description": p.description,
            "installation_location_id": p.installation_location_id,
            "installation_note": p.installation_note,
            "enabled": bool(p.enabled),
            "archived_at": p.archived_at,
            "created_at": p.created_at, "updated_at": p.updated_at,
        }

    def _location_to_dict(l):
        return {
            "id": l.id, "parent_id": l.parent_id, "kind": l.kind,
            "name": l.name, "code": l.code, "sort_order": l.sort_order,
            "archived_at": l.archived_at,
            "created_at": l.created_at, "updated_at": l.updated_at,
        }

    def _node_to_dict(n):
        return {
            "id": n.id, "code": n.code, "name": n.name, "kind": n.kind,
            "location_id": n.location_id, "archived_at": n.archived_at,
            "created_at": n.created_at, "updated_at": n.updated_at,
        }

    def _edge_to_dict(e):
        return {
            "id": e.id, "code": e.code, "name": e.name,
            "from_node_id": e.from_node_id, "to_node_id": e.to_node_id,
            "primary_point_id": e.primary_point_id,
            "phase_count": e.phase_count, "rated_current_a": e.rated_current_a,
            "cable_note": e.cable_note, "state": e.state,
            "valid_from": e.valid_from, "valid_to": e.valid_to,
            "archived_at": e.archived_at,
            "created_at": e.created_at, "updated_at": e.updated_at,
        }

    def _binding_to_dict(b):
        return {
            "id": b.id, "point_id": b.point_id,
            "meter_source_id": b.meter_source_id,
            "channel_profile": b.channel_profile, "role": b.role,
            "valid_from": b.valid_from, "valid_to": b.valid_to,
            "replacement_note": b.replacement_note,
            "created_at": b.created_at,
        }

    def _resolve_or_provision_meter_source(meter_source_id, new_meter, at=None):
        """Партия 3/5: вернуть meter_source_id — либо уже готовый (передан
        явно), либо провижинить физически новый прибор по адресу MQTT
        (controller_key/device_id), заводя meters+meter_sources или
        переиспользуя существующие с тем же адресом (устойчиво к повтору
        запроса). Общий код для replace-meter (партия 3) и открытия
        первой привязки точки (партия 5, задача 1) — раньше был продублирован
        внутри replace-meter, теперь один источник истины. controller_key
        по умолчанию — DEFAULT_CONTROLLER_KEY (контроллер в этой системе
        один и тот же, что и у мастера переноса legacy)."""
        if meter_source_id is not None:
            return meter_source_id
        if not isinstance(new_meter, dict):
            raise ValueError(
                "требуется meter_source_id существующего источника либо "
                "new_meter с адресом нового прибора (device_id)")
        controller_key = str(new_meter.get("controller_key") or DEFAULT_CONTROLLER_KEY).strip()
        if not controller_key:
            raise ValueError("new_meter.controller_key не может быть пустым")
        device_id = validate_device_id(new_meter.get("device_id"))
        meters = MeterRepo(db, GroupRepo(db))
        sources = MeterSourceRepo(db)
        meter = meters.get_by_device_id(device_id)
        if meter is None:
            # Физически новый прибор — заводим `meters` заново. Тем же
            # device_id, что и адрес MQTT — как и в legacy_migration.py,
            # это устойчивый естественный ключ прибора.
            display_name = new_meter.get("display_name") or device_id
            meter = meters.add(device_id, display_name)
        current = sources.get_current(meter.id)
        if (current is not None and current.controller_key == controller_key
                and current.device_id == device_id):
            # Повтор того же запроса (например, после сетевого сбоя) —
            # источник уже открыт именно на этот адрес, переприсваивать
            # незачем (и не плодим лишний ряд).
            source = current
        else:
            # reassign_source сама закрывает старый открытый источник
            # ЭТОГО прибора, если он был, и атомарно открывает новый —
            # устойчиво и для совсем нового прибора (current is None —
            # эквивалентно open_source).
            source = sources.reassign_source(meter.id, controller_key, device_id, at=at)
        return source.id

    def _group_to_dict(g):
        return {
            "id": g.id, "name": g.name, "category": g.category,
            "parent_id": g.parent_id, "color": g.color,
            "created_at": g.created_at,
        }

    def _membership_to_dict(m):
        return {
            "id": m.id, "group_id": m.group_id, "point_id": m.point_id,
            "valid_from": m.valid_from, "valid_to": m.valid_to,
            "created_at": m.created_at,
        }

    def _plan_or_404_v2(plan_id):
        plan = _plan_repo_v2().get_by_id(plan_id)
        if plan is None:
            body, status = _err("not_found", f"План {plan_id} не найден", 404, ids=[plan_id])
            return None, json_response(body, status)
        return plan, None

    def _revision_conflict(e, ids=None):
        """§9.2/§6.1 (партия 2, задача 1): единый 409 для устаревшей/
        отсутствующей expected_revision — см. revision_service.py."""
        body, status = _err("revision_conflict", str(e), 409, ids=ids,
                             fields=["expected_revision"])
        return json_response(body, status)

    # ------------------------------------------------------- revision (partия 2)

    @app.route("/api/v2/revision", methods=["GET"])
    def v2_revision():
        """Текущая глобальная ревизия конфигурации — точка отсчёта для
        expected_revision в последующей записи (ТЗ §6.1/§9.2). 0, если в
        этой БД ещё не было ни одной защищённой предметной транзакции."""
        with db.read() as c:
            rev = current_revision_id(c)
        return json_response({"configuration_revision": rev})

    # ------------------------------------------------------------ points

    @app.route("/api/v2/points", methods=["GET", "POST"])
    def v2_points():
        repo = _point_repo()
        if request.method == "GET":
            include_archived = request.args.get("include_archived") == "1"
            return json_response(
                [_point_to_dict(p) for p in repo.list_all(include_archived)])

        data, _envelope = _unwrap_data(request.get_json(silent=True) or {})
        try:
            p, new_rev = bump_revision(
                db,
                lambda: repo.add(
                    code=data.get("code"), name=data.get("name"),
                    description=data.get("description"),
                    installation_location_id=data.get("installation_location_id"),
                    installation_note=data.get("installation_note"),
                ),
                touched=[("metering_point", "new")])
        except ValueError as e:
            body, status = _err("bad_request", str(e), 400, fields=["code", "name"])
            return json_response(body, status)
        out = _point_to_dict(p)
        out["configuration_revision"] = new_rev
        return json_response(out, 201)

    @app.route("/api/v2/points/<int:point_id>", methods=["GET", "PATCH"])
    def v2_point_detail(point_id):
        repo = _point_repo()
        p = repo.get_by_id(point_id)
        if p is None:
            body, status = _err("not_found", f"Точка {point_id} не найдена", 404, ids=[point_id])
            return json_response(body, status)

        if request.method == "GET":
            return json_response(_point_to_dict(p))

        data, _envelope = _unwrap_data(request.get_json(silent=True) or {})

        def _mutate():
            result = p
            if "enabled" in data:
                result = repo.set_enabled(point_id, bool(data["enabled"]))
            if "archived" in data and data["archived"]:
                result = repo.archive(point_id)
            if any(k in data for k in ("name", "description", "installation_note")):
                result = repo.update_fields(
                    point_id, name=data.get("name"),
                    description=data.get("description"),
                    installation_note=data.get("installation_note"))
            if "installation_location_id" in data:
                # Партия 5, задача 2 (§8.3 "редактировать принадлежность"):
                # смена места установки уже существующей точки — раньше
                # PATCH это поле не принимал вообще (см. update_fields).
                result = repo.update_fields(
                    point_id, installation_location_id=data.get("installation_location_id"))
            return result

        try:
            _, new_rev = with_revision_check(
                db, data.get("expected_revision"), _mutate,
                touched=[("metering_point", point_id)])
        except GlobalRevisionConflict as e:
            return _revision_conflict(e, ids=[point_id])
        except ValueError as e:
            body, status = _err("bad_request", str(e), 400)
            return json_response(body, status)
        out = _point_to_dict(repo.get_by_id(point_id))
        out["configuration_revision"] = new_rev
        return json_response(out)

    @app.route("/api/v2/points/<int:point_id>/bindings", methods=["GET", "POST"])
    def v2_point_bindings(point_id):
        repo = _point_repo()
        if repo.get_by_id(point_id) is None:
            body, status = _err("not_found", f"Точка {point_id} не найдена", 404, ids=[point_id])
            return json_response(body, status)

        if request.method == "GET":
            bindings = _binding_repo().list_for_point(point_id)
            return json_response([_binding_to_dict(b) for b in bindings])

        """POST — партия 5, задача 1: открыть привязку точки к прибору
        (в первую очередь — ПЕРВУЮ, у только что заведённой точки прибора
        ещё не было). `replace-meter` для этого не годится: она требует
        уже открытую основную привязку и закрывает её (см. докстринг
        v2_point_replace_meter и binding_service.PointBindingRepo.
        replace_meter — кидает ValueError, если открытой основной
        привязки нет). Нужный метод, PointBindingRepo.open_binding, уже
        существует и используется мастером переноса legacy
        (legacy_migration.py), но до этой партии не был доступен через
        HTTP ни одним маршрутом — проверено по полному списку @app.route
        в этом файле. Тело — как у replace-meter: meter_source_id
        готового источника ЛИБО new_meter с адресом нового прибора (см.
        _resolve_or_provision_meter_source)."""
        body = request.get_json(silent=True) or {}
        data, _envelope = _unwrap_data(body)
        meter_source_id = data.get("meter_source_id")
        new_meter = data.get("new_meter")

        if meter_source_id is None and not isinstance(new_meter, dict):
            body, status = _err(
                "bad_request",
                "требуется meter_source_id существующего источника либо "
                "new_meter с адресом нового прибора (controller_key, device_id)",
                400, fields=["meter_source_id", "new_meter"])
            return json_response(body, status)

        channel_profile = data.get("channel_profile") or "total_3p"
        role = data.get("role") or "primary"
        valid_from = data.get("valid_from", data.get("at"))
        bindings = _binding_repo()

        def _provision_and_open():
            source_id = _resolve_or_provision_meter_source(
                meter_source_id, new_meter, at=valid_from)
            return bindings.open_binding(
                point_id, source_id, channel_profile, role=role,
                valid_from=valid_from,
                replacement_note=data.get("replacement_note"),
            ), source_id

        touched = [("metering_point", point_id)]
        if meter_source_id is not None:
            touched.append(("meter_source", meter_source_id))

        try:
            (new_binding, used_source_id), new_rev = with_revision_check(
                db, data.get("expected_revision"), _provision_and_open,
                touched=touched)
        except GlobalRevisionConflict as e:
            return _revision_conflict(
                e, ids=[i for i in (point_id, meter_source_id) if i is not None])
        except BindingConflict as e:
            body, status = _err(
                "double_counting", str(e), 409,
                ids=[i for i in (point_id, meter_source_id) if i is not None])
            return json_response(body, status)
        except ValueError as e:
            body, status = _err("bad_request", str(e), 400)
            return json_response(body, status)
        out = _binding_to_dict(new_binding)
        out["configuration_revision"] = new_rev
        out["meter_source_id"] = used_source_id
        return json_response(out, 201)

    @app.route("/api/v2/points/<int:point_id>/replace-meter", methods=["POST"])
    def v2_point_replace_meter(point_id):
        """§5.4/A18/партия 3 задача 3: атомарная замена прибора. Тело —
        один из двух вариантов адреса нового прибора:

        1) {"meter_source_id": <id существующего meter_source>, ...} —
           источник уже существует (был использован раньше или создан
           заранее отдельным вызовом topology/точек).
        2) {"new_meter": {"controller_key": .., "device_id": ..,
           "display_name": .. (опционально)}, ...} — физически новый
           прибор: адрес контроллера, как он приходит из MQTT. Заводит
           `meter` (либо переиспользует существующий с тем же device_id
           — устойчиво к повтору запроса) и `meter_source`, и только
           затем открывает новый интервал привязки — одной транзакцией
           с ревизией, как и вариант 1.

        Общие поля тела: "at" (опционально, unix ts), "channel_profile"
        (опционально), "replacement_note" (опционально),
        "expected_revision" (обязателен протоколом ревизий).

        Завершение СТАРОГО интервала привязки делает
        `PointBindingRepo.replace_meter` сама — здесь не дублируется."""
        body = request.get_json(silent=True) or {}
        data, _envelope = _unwrap_data(body)
        meter_source_id = data.get("meter_source_id")
        new_meter = data.get("new_meter")

        if meter_source_id is None and not isinstance(new_meter, dict):
            body, status = _err(
                "bad_request",
                "требуется meter_source_id существующего источника либо "
                "new_meter с адресом нового прибора (controller_key, device_id)",
                400, fields=["meter_source_id", "new_meter"])
            return json_response(body, status)

        bindings = _binding_repo()

        def _provision_and_replace():
            # Партия 5: провижининг вынесен в общий
            # _resolve_or_provision_meter_source — используется также
            # новой ручкой открытия первой привязки (POST
            # points/<id>/bindings), логика больше не продублирована.
            source_id = _resolve_or_provision_meter_source(
                meter_source_id, new_meter, at=data.get("at"))
            return bindings.replace_meter(
                point_id, source_id,
                at=data.get("at"),
                channel_profile=data.get("channel_profile"),
                replacement_note=data.get("replacement_note"),
            ), source_id

        touched = [("metering_point", point_id)]
        if meter_source_id is not None:
            touched.append(("meter_source", meter_source_id))

        try:
            (new_binding, used_source_id), new_rev = with_revision_check(
                db, data.get("expected_revision"), _provision_and_replace,
                touched=touched)
        except GlobalRevisionConflict as e:
            return _revision_conflict(
                e, ids=[i for i in (point_id, meter_source_id) if i is not None])
        except BindingConflict as e:
            body, status = _err(
                "double_counting", str(e), 409,
                ids=[i for i in (point_id, meter_source_id) if i is not None])
            return json_response(body, status)
        except ValueError as e:
            body, status = _err("bad_request", str(e), 400)
            return json_response(body, status)
        out = _binding_to_dict(new_binding)
        out["configuration_revision"] = new_rev
        out["meter_source_id"] = used_source_id
        return json_response(out, 200)

    # ---------------------------------------------------------- locations

    @app.route("/api/v2/locations", methods=["GET", "POST"])
    def v2_locations():
        repo = _location_repo()
        if request.method == "GET":
            include_archived = request.args.get("include_archived") == "1"
            return json_response(
                [_location_to_dict(l) for l in repo.list_all(include_archived)])

        data, _envelope = _unwrap_data(request.get_json(silent=True) or {})
        try:
            l, new_rev = bump_revision(
                db,
                lambda: repo.add(
                    name=data.get("name"), kind=data.get("kind"),
                    parent_id=data.get("parent_id"), code=data.get("code"),
                    sort_order=data.get("sort_order", 0),
                ),
                touched=[("location", "new")])
        except ValueError as e:
            body, status = _err("bad_request", str(e), 400, fields=["name", "kind"])
            return json_response(body, status)
        out = _location_to_dict(l)
        out["configuration_revision"] = new_rev
        return json_response(out, 201)

    @app.route("/api/v2/locations/<int:location_id>", methods=["GET", "PATCH"])
    def v2_location_detail(location_id):
        repo = _location_repo()
        l = repo.get_by_id(location_id)
        if l is None:
            body, status = _err("not_found", f"Место {location_id} не найдено", 404, ids=[location_id])
            return json_response(body, status)

        if request.method == "GET":
            return json_response(_location_to_dict(l))

        data, _envelope = _unwrap_data(request.get_json(silent=True) or {})

        def _mutate():
            result = l
            if "parent_id" in data:
                result = repo.set_parent(location_id, data["parent_id"])
            if data.get("archived"):
                result = repo.archive(location_id)
            return result

        try:
            _, new_rev = with_revision_check(
                db, data.get("expected_revision"), _mutate,
                touched=[("location", location_id)])
        except GlobalRevisionConflict as e:
            return _revision_conflict(e, ids=[location_id])
        except ValueError as e:
            code = "cycle_conflict" if "цикл" in str(e).lower() else "bad_request"
            status = 409 if code == "cycle_conflict" else 400
            body, status2 = _err(code, str(e), status, ids=[location_id])
            return json_response(body, status2)
        out = _location_to_dict(repo.get_by_id(location_id))
        out["configuration_revision"] = new_rev
        return json_response(out)

    # -------------------------------------------------------------- groups
    # ТЗ §4.4 — см. group_repo_v2.GroupRepoV2 для правил (категория,
    # циклы, многогруппность точки, дедупликация эффективного состава).

    @app.route("/api/v2/groups", methods=["GET", "POST"])
    def v2_groups():
        repo = _group_repo_v2()
        if request.method == "GET":
            parent_raw = request.args.get("parent_id")
            if parent_raw is not None:
                if parent_raw == "null":
                    parent_id = None
                else:
                    try:
                        parent_id = int(parent_raw)
                    except ValueError:
                        body, status = _err(
                            "bad_request",
                            "parent_id должен быть целым числом или 'null'",
                            400, fields=["parent_id"])
                        return json_response(body, status)
                return json_response(
                    [_group_to_dict(g) for g in repo.list_children(parent_id)])
            return json_response([_group_to_dict(g) for g in repo.list_all()])

        data, _envelope = _unwrap_data(request.get_json(silent=True) or {})
        try:
            g, new_rev = bump_revision(
                db,
                lambda: repo.add(
                    name=data.get("name"), category=data.get("category"),
                    parent_id=data.get("parent_id"), color=data.get("color"),
                ),
                touched=[("group", "new")])
        except ValueError as e:
            msg = str(e).lower()
            if "цикл" in msg:
                code, status = "cycle_conflict", 409
            elif "категор" in msg:
                code, status = "category_conflict", 409
            else:
                code, status = "bad_request", 400
            body, status2 = _err(code, str(e), status, fields=["name", "parent_id"])
            return json_response(body, status2)
        out = _group_to_dict(g)
        out["configuration_revision"] = new_rev
        return json_response(out, 201)

    @app.route("/api/v2/groups/<int:group_id>", methods=["GET", "PATCH"])
    def v2_group_detail(group_id):
        repo = _group_repo_v2()
        g = repo.get_by_id(group_id)
        if g is None:
            body, status = _err("not_found", f"Группа {group_id} не найдена", 404, ids=[group_id])
            return json_response(body, status)

        if request.method == "GET":
            return json_response(_group_to_dict(g))

        data, _envelope = _unwrap_data(request.get_json(silent=True) or {})

        def _mutate():
            result = g
            if "parent_id" in data:
                result = repo.set_parent(group_id, data["parent_id"])
            return result

        try:
            _, new_rev = with_revision_check(
                db, data.get("expected_revision"), _mutate,
                touched=[("group", group_id)])
        except GlobalRevisionConflict as e:
            return _revision_conflict(e, ids=[group_id])
        except ValueError as e:
            msg = str(e).lower()
            if "цикл" in msg:
                code, status = "cycle_conflict", 409
            elif "категор" in msg:
                code, status = "category_conflict", 409
            else:
                code, status = "bad_request", 400
            body, status2 = _err(code, str(e), status, ids=[group_id])
            return json_response(body, status2)
        out = _group_to_dict(repo.get_by_id(group_id))
        out["configuration_revision"] = new_rev
        return json_response(out)

    @app.route("/api/v2/groups/<int:group_id>/members", methods=["GET", "POST"])
    def v2_group_members(group_id):
        repo = _group_repo_v2()
        if repo.get_by_id(group_id) is None:
            body, status = _err("not_found", f"Группа {group_id} не найдена", 404, ids=[group_id])
            return json_response(body, status)

        if request.method == "GET":
            include_closed = request.args.get("include_closed") == "1"
            members = repo.list_members(group_id, include_closed=include_closed)
            return json_response([_membership_to_dict(m) for m in members])

        data, _envelope = _unwrap_data(request.get_json(silent=True) or {})
        point_id = data.get("point_id")
        if point_id is None:
            body, status = _err("bad_request", "требуется point_id", 400, fields=["point_id"])
            return json_response(body, status)
        try:
            m, new_rev = with_revision_check(
                db, data.get("expected_revision"),
                lambda: repo.add_member(group_id, point_id, valid_from=data.get("valid_from")),
                touched=[("group", group_id), ("metering_point", point_id)])
        except GlobalRevisionConflict as e:
            return _revision_conflict(e, ids=[group_id, point_id])
        except ValueError as e:
            msg = str(e).lower()
            if "не найд" in msg:
                code, status = "not_found", 404
            elif "уже состоит" in msg:
                code, status = "conflict", 409
            else:
                code, status = "bad_request", 400
            body, status2 = _err(code, str(e), status, ids=[group_id, point_id])
            return json_response(body, status2)
        out = _membership_to_dict(m)
        out["configuration_revision"] = new_rev
        return json_response(out, 201)

    @app.route("/api/v2/groups/<int:group_id>/members/<int:point_id>", methods=["DELETE"])
    def v2_group_member_detail(group_id, point_id):
        repo = _group_repo_v2()
        at_raw = request.args.get("at")
        at = None
        if at_raw:
            try:
                at = int(at_raw)
            except ValueError:
                body, status = _err(
                    "bad_request", "at должен быть unix-временем (целое число секунд)",
                    400, fields=["at"])
                return json_response(body, status)
        # DELETE обычно без тела — expected_revision принимается и из
        # query-параметра (как "at" выше), и из JSON-тела, если он есть.
        body_data, _envelope = _unwrap_data(request.get_json(silent=True) or {})
        expected_revision = request.args.get("expected_revision", body_data.get("expected_revision"))
        try:
            _, new_rev = with_revision_check(
                db, expected_revision,
                lambda: repo.remove_member(group_id, point_id, at=at),
                touched=[("group", group_id), ("metering_point", point_id)])
        except GlobalRevisionConflict as e:
            return _revision_conflict(e, ids=[group_id, point_id])
        except ValueError as e:
            msg = str(e).lower()
            code = "not_found" if "не найд" in msg or "не состоит" in msg else "bad_request"
            status = 404 if code == "not_found" else 400
            body, status2 = _err(code, str(e), status, ids=[group_id, point_id])
            return json_response(body, status2)
        return ("", 204, {"X-Configuration-Revision": str(new_rev)})

    @app.route("/api/v2/groups/<int:group_id>/effective-members", methods=["GET"])
    def v2_group_effective_members(group_id):
        """ТЗ §4.4: "состав родителя... одинаковую точку, встретившуюся
        несколькими путями, учитывать один раз и раскрывать происхождение
        включения" — рекурсивный состав группы (сама группа + все
        дочерние), дедуплицированный по точке, с provenance (via)."""
        repo = _group_repo_v2()
        if repo.get_by_id(group_id) is None:
            body, status = _err("not_found", f"Группа {group_id} не найдена", 404, ids=[group_id])
            return json_response(body, status)

        at_raw = request.args.get("at")
        at = None
        if at_raw:
            try:
                at = int(at_raw)
            except ValueError:
                body, status = _err(
                    "bad_request", "at должен быть unix-временем (целое число секунд)",
                    400, fields=["at"])
                return json_response(body, status)

        result = repo.resolve_effective_members(group_id, at=at)
        point_repo = _point_repo()
        items = []
        for r in result:
            p = point_repo.get_by_id(r["point_id"])
            items.append({
                "point_id": r["point_id"],
                "code": p.code if p else None,
                "name": p.name if p else None,
                "via": r["via"],
            })
        return json_response({
            "group_id": group_id,
            "as_of": at if at is not None else int(time.time()),
            "points": items,
        })

    @app.route("/api/v2/points/<int:point_id>/groups", methods=["GET"])
    def v2_point_groups(point_id):
        if _point_repo().get_by_id(point_id) is None:
            body, status = _err("not_found", f"Точка {point_id} не найдена", 404, ids=[point_id])
            return json_response(body, status)
        memberships = _group_repo_v2().list_groups_for_point(point_id)
        return json_response([_membership_to_dict(m) for m in memberships])

    # --------------------------------------------------------- topology

    @app.route("/api/v2/topology/nodes", methods=["GET", "POST"])
    def v2_topology_nodes():
        repo = _node_repo()
        if request.method == "GET":
            include_archived = request.args.get("include_archived") == "1"
            return json_response(
                [_node_to_dict(n) for n in repo.list_all(include_archived)])

        data, _envelope = _unwrap_data(request.get_json(silent=True) or {})
        try:
            n, new_rev = bump_revision(
                db,
                lambda: repo.add(
                    code=data.get("code"), name=data.get("name"),
                    kind=data.get("kind"), location_id=data.get("location_id"),
                ),
                touched=[("electrical_node", "new")])
        except ValueError as e:
            body, status = _err("bad_request", str(e), 400, fields=["code", "name", "kind"])
            return json_response(body, status)
        out = _node_to_dict(n)
        out["configuration_revision"] = new_rev
        return json_response(out, 201)

    @app.route("/api/v2/topology/nodes/<int:node_id>", methods=["GET", "PATCH"])
    def v2_topology_node_detail(node_id):
        repo = _node_repo()
        n = repo.get_by_id(node_id)
        if n is None:
            body, status = _err("not_found", f"Узел {node_id} не найден", 404, ids=[node_id])
            return json_response(body, status)
        if request.method == "GET":
            return json_response(_node_to_dict(n))

        data, _envelope = _unwrap_data(request.get_json(silent=True) or {})
        if data.get("archived"):
            try:
                _, new_rev = with_revision_check(
                    db, data.get("expected_revision"),
                    lambda: repo.archive(node_id),
                    touched=[("electrical_node", node_id)])
            except GlobalRevisionConflict as e:
                return _revision_conflict(e, ids=[node_id])
            except ValueError as e:
                body, status = _err("conflict", str(e), 409, ids=[node_id])
                return json_response(body, status)
            out = _node_to_dict(repo.get_by_id(node_id))
            out["configuration_revision"] = new_rev
            return json_response(out)
        return json_response(_node_to_dict(repo.get_by_id(node_id)))

    @app.route("/api/v2/topology/edges", methods=["GET", "POST"])
    def v2_topology_edges():
        repo = _edge_repo()
        if request.method == "GET":
            state_filter = request.args.get("state", "draft")
            if state_filter == "published":
                items = repo.list_active_published()
            else:
                items = repo.list_drafts()
            return json_response([_edge_to_dict(e) for e in items])

        data, _envelope = _unwrap_data(request.get_json(silent=True) or {})
        try:
            e, new_rev = bump_revision(
                db,
                lambda: repo.add_draft(
                    from_node_id=data.get("from_node_id"),
                    to_node_id=data.get("to_node_id"),
                    code=data.get("code"), name=data.get("name"),
                    primary_point_id=data.get("primary_point_id"),
                    phase_count=data.get("phase_count"),
                    rated_current_a=data.get("rated_current_a"),
                    cable_note=data.get("cable_note"),
                ),
                touched=[("electrical_edge", "new")])
        except ValueError as e2:
            body, status = _err(
                "bad_request", str(e2), 400, fields=["from_node_id", "to_node_id"])
            return json_response(body, status)
        out = _edge_to_dict(e)
        out["configuration_revision"] = new_rev
        return json_response(out, 201)

    @app.route("/api/v2/topology/edges/<int:edge_id>", methods=["GET", "PATCH"])
    def v2_topology_edge_detail(edge_id):
        repo = _edge_repo()
        e = repo.get_by_id(edge_id)
        if e is None:
            body, status = _err("not_found", f"Связь {edge_id} не найдена", 404, ids=[edge_id])
            return json_response(body, status)
        if request.method == "GET":
            return json_response(_edge_to_dict(e))

        data, _envelope = _unwrap_data(request.get_json(silent=True) or {})
        if data.get("retire"):
            try:
                _, new_rev = with_revision_check(
                    db, data.get("expected_revision"),
                    lambda: repo.retire_edge(edge_id, at=data.get("at")),
                    touched=[("electrical_edge", edge_id)])
            except GlobalRevisionConflict as e2:
                return _revision_conflict(e2, ids=[edge_id])
            except ValueError as e2:
                body, status = _err("bad_request", str(e2), 400, ids=[edge_id])
                return json_response(body, status)
            out = _edge_to_dict(repo.get_by_id(edge_id))
            out["configuration_revision"] = new_rev
            return json_response(out)
        return json_response(_edge_to_dict(repo.get_by_id(edge_id)))

    def _violations_to_json(violations):
        return [
            {"kind": v.kind, "message": v.message, "node_ids": v.node_ids,
             "edge_ids": v.edge_ids}
            for v in violations
        ]

    @app.route("/api/v2/topology/validate", methods=["POST"])
    def v2_topology_validate():
        data = request.get_json(silent=True) or {}
        edge_ids = data.get("edge_ids") or []
        repo = _edge_repo()
        try:
            violations = repo.validate_edges(edge_ids)
        except ValueError as e:
            body, status = _err("bad_request", str(e), 400)
            return json_response(body, status)
        ok = not violations
        return json_response({"ok": ok, "violations": _violations_to_json(violations)})

    @app.route("/api/v2/topology/publish", methods=["POST"])
    def v2_topology_publish():
        """§9.2: "Публикация сети принимает draft_id, expected_configuration_
        revision, effective_from" — партия 2, задача 1: реализовано.
        draft_id здесь — edge_ids (список ID черновиков).
        expected_configuration_revision обязателен — устаревшая/
        отсутствующая ревизия отклоняется 409 ДО публикации (топология не
        меняется, см. RevisionConflict)."""
        data = request.get_json(silent=True) or {}
        edge_ids = data.get("edge_ids") or data.get("draft_id") or []
        if isinstance(edge_ids, int):
            edge_ids = [edge_ids]
        effective_from = data.get("effective_from")
        expected_revision = data.get("expected_configuration_revision")

        repo = _edge_repo()
        try:
            published, new_rev = with_revision_check(
                db, expected_revision,
                lambda: repo.publish_edges(edge_ids, at=effective_from),
                touched=[("electrical_edge", eid) for eid in edge_ids])
        except GlobalRevisionConflict as e:
            body, status = _err(
                "revision_conflict", str(e), 409,
                fields=["expected_configuration_revision"])
            return json_response(body, status)
        except TopologyConflict as e:
            violations = repo.validate_edges(edge_ids) if edge_ids else []
            body, status = _err(
                "topology_conflict", str(e), 409,
                path=_violations_to_json(violations))
            return json_response(body, status)
        except ValueError as e:
            body, status = _err("bad_request", str(e), 400)
            return json_response(body, status)
        return json_response(
            {"edges": [_edge_to_dict(e) for e in published],
             "configuration_revision": new_rev}, 200)

    # ----------------------------------------------------- balance-scopes

    @app.route("/api/v2/balance-scopes", methods=["GET", "POST"])
    def v2_balance_scopes():
        if request.method == "GET":
            with db.read() as c:
                rows = c.execute(
                    "SELECT * FROM balance_scopes WHERE archived_at IS NULL "
                    "ORDER BY name COLLATE NOCASE"
                ).fetchall()
            return json_response([dict(r) for r in rows])

        data, _envelope = _unwrap_data(request.get_json(silent=True) or {})
        name = (data.get("name") or "").strip()
        if not name:
            body, status = _err("bad_request", "имя обязательно", 400, fields=["name"])
            return json_response(body, status)
        now = int(time.time())
        with db.transaction() as c:
            cur = c.execute(
                "INSERT INTO balance_scopes (name, description, created_at, updated_at) "
                "VALUES (?, ?, ?, ?)",
                (name, data.get("description"), now, now))
            scope_id = cur.lastrowid
            for pid in data.get("input_point_ids", []) or []:
                c.execute(
                    "INSERT INTO balance_members (scope_id, point_id, side, valid_from, created_at) "
                    "VALUES (?, ?, 'input', ?, ?)", (scope_id, pid, now, now))
            for pid in data.get("output_point_ids", []) or []:
                c.execute(
                    "INSERT INTO balance_members (scope_id, point_id, side, valid_from, created_at) "
                    "VALUES (?, ?, 'output', ?, ?)", (scope_id, pid, now, now))
            new_rev = create_revision(c, db.current_schema_version(),
                                       touched=[("balance_scope", "new")])
        with db.read() as c:
            row = c.execute("SELECT * FROM balance_scopes WHERE id = ?", (scope_id,)).fetchone()
        out = dict(row)
        out["configuration_revision"] = new_rev
        return json_response(out, 201)

    @app.route("/api/v2/balance-scopes/<int:scope_id>", methods=["GET", "PATCH"])
    def v2_balance_scope_detail(scope_id):
        with db.read() as c:
            row = c.execute("SELECT * FROM balance_scopes WHERE id = ?", (scope_id,)).fetchone()
        if row is None:
            body, status = _err("not_found", f"Граница баланса {scope_id} не найдена",
                                 404, ids=[scope_id])
            return json_response(body, status)

        if request.method == "GET":
            with db.read() as c:
                members = c.execute(
                    "SELECT * FROM balance_members WHERE scope_id = ? AND valid_to IS NULL",
                    (scope_id,)).fetchall()
            d = dict(row)
            d["input_point_ids"] = [m["point_id"] for m in members if m["side"] == "input"]
            d["output_point_ids"] = [m["point_id"] for m in members if m["side"] == "output"]
            return json_response(d)

        # PATCH: полный новый состав input_point_ids/output_point_ids —
        # закрываем то, что больше не входит, открываем новое, версионируя
        # (тот же принцип, что и в других *_bindings таблицах этапа B).
        data, _envelope = _unwrap_data(request.get_json(silent=True) or {})
        now = int(time.time())
        try:
            with db.transaction() as c:
                check_expected_revision(c, data.get("expected_revision"))

                current = c.execute(
                    "SELECT * FROM balance_members WHERE scope_id = ? AND valid_to IS NULL",
                    (scope_id,)).fetchall()
                wanted = []
                if "input_point_ids" in data:
                    wanted += [(pid, "input") for pid in data["input_point_ids"]]
                if "output_point_ids" in data:
                    wanted += [(pid, "output") for pid in data["output_point_ids"]]
                wanted_set = set(wanted)
                current_set = {(m["point_id"], m["side"]) for m in current}

                if "input_point_ids" in data or "output_point_ids" in data:
                    for m in current:
                        if (m["point_id"], m["side"]) not in wanted_set:
                            c.execute(
                                "UPDATE balance_members SET valid_to = ? WHERE id = ?",
                                (now, m["id"]))
                    for pid, side in wanted_set - current_set:
                        c.execute(
                            "INSERT INTO balance_members "
                            "(scope_id, point_id, side, valid_from, created_at) "
                            "VALUES (?, ?, ?, ?, ?)", (scope_id, pid, side, now, now))
                if "name" in data or "description" in data:
                    c.execute(
                        "UPDATE balance_scopes SET name = COALESCE(?, name), "
                        "description = COALESCE(?, description), updated_at = ? WHERE id = ?",
                        (data.get("name"), data.get("description"), now, scope_id))

                new_rev = create_revision(c, db.current_schema_version(),
                                           touched=[("balance_scope", scope_id)])
        except GlobalRevisionConflict as e:
            return _revision_conflict(e, ids=[scope_id])

        with db.read() as c:
            row2 = c.execute("SELECT * FROM balance_scopes WHERE id = ?", (scope_id,)).fetchone()
            members2 = c.execute(
                "SELECT * FROM balance_members WHERE scope_id = ? AND valid_to IS NULL",
                (scope_id,)).fetchall()
        d = dict(row2)
        d["input_point_ids"] = [m["point_id"] for m in members2 if m["side"] == "input"]
        d["output_point_ids"] = [m["point_id"] for m in members2 if m["side"] == "output"]
        d["configuration_revision"] = new_rev
        return json_response(d)

    # -------------------------------------------------------- metrics/query

    def _parse_ts(value, field_name):
        if isinstance(value, (int, float)):
            return int(value)
        if isinstance(value, str):
            try:
                return parse_user_datetime(value)
            except ValueError:
                pass
        raise ValueError(f"поле {field_name} должно быть unix-временем или датой")

    def _tag_result(result, revision_id):
        """Партия 2, задача 1 (A43): проставляет ревизию/as_of, зафиксированные
        ОДИН раз в начале запроса (см. v2_metrics_query), на готовый
        MetricResult — контракт (accounting_contract.py) уже несёт эти поля,
        считать их должен вызывающий HTTP-слой, а не сам расчётный сервис."""
        result.configuration_revision_id = revision_id
        result.configuration_revision_ids = [revision_id]
        result.as_of = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        return result

    @app.route("/api/v2/metrics/query", methods=["POST"])
    def v2_metrics_query():
        """A43 (ТЗ §13/§6.1, партия 2 задача 1): "внутри запроса один снимок,
        даже если внутри несколько обращений к репозиториям". Реализовано
        через ОДИН внешний `with db.read()` на весь расчёт (не по одному на
        репозиторий, как раньше) — `Database` в этом проекте держит
        единственное соединение под общим `threading.RLock()`
        (см. db.py), поэтому пока этот блок не завершится, ни одна
        конкурентная запись (`db.transaction()`, та же блокировка) не
        может вклиниться между двумя внутренними чтениями расчёта; ревизия
        фиксируется первой инструкцией внутри блока и используется на всё
        время вычисления."""
        data = request.get_json(silent=True) or {}
        mode = data.get("mode")
        timezone_name = data.get("timezone", "UTC")

        try:
            ts_from = _parse_ts(data.get("from"), "from")
            ts_to = _parse_ts(data.get("to"), "to")
        except ValueError as e:
            body, status = _err("bad_request", str(e), 400, fields=["from", "to"])
            return json_response(body, status)

        if ts_to <= ts_from:
            body, status = _err("bad_request", "to должно быть позже from", 400, fields=["from", "to"])
            return json_response(body, status)

        requested_revision = data.get("configuration_revision_id")

        binding_repo = _binding_repo()
        aggregates_repo = _aggregates_repo()
        source_repo = _source_repo()
        edge_repo = _edge_repo()

        # A43: держим ОДИН read-контекст (== одну блокировку) на весь
        # расчёт, а не по одному на repo-вызов внутри measured/sum/balance —
        # см. докстринг метода.
        with db.read() as c:
            if requested_revision is not None:
                try:
                    requested_revision = int(requested_revision)
                except (TypeError, ValueError):
                    body, status = _err(
                        "bad_request", "configuration_revision_id должен быть целым числом",
                        400, fields=["configuration_revision_id"])
                    return json_response(body, status)
                if not revision_exists(c, requested_revision):
                    body, status = _err(
                        "not_found", f"Ревизия конфигурации {requested_revision} не найдена",
                        404, ids=[requested_revision])
                    return json_response(body, status)
                pinned_revision = requested_revision
            else:
                pinned_revision = current_revision_id(c)

            try:
                if mode == "measured":
                    point_ids = data.get("point_ids") or []
                    if len(point_ids) != 1:
                        body, status = _err(
                            "bad_request", "measured требует ровно один point_ids", 400,
                            fields=["point_ids"])
                        return json_response(body, status)
                    result = measured_point(binding_repo, aggregates_repo, source_repo,
                                             point_ids[0], ts_from, ts_to, timezone_name)
                    return json_response(_tag_result(result, pinned_revision).to_dict())

                elif mode == "sum":
                    point_ids = data.get("point_ids") or []
                    if not point_ids:
                        body, status = _err("bad_request", "sum требует point_ids", 400,
                                             fields=["point_ids"])
                        return json_response(body, status)
                    result = sum_points(binding_repo, aggregates_repo, source_repo, edge_repo,
                                         point_ids, ts_from, ts_to, timezone_name)
                    return json_response(_tag_result(result, pinned_revision).to_dict())

                elif mode == "balance":
                    scope_id = data.get("scope")
                    if scope_id is None:
                        body, status = _err("bad_request", "balance требует scope (id границы)",
                                             400, fields=["scope"])
                        return json_response(body, status)
                    result = balance(db, binding_repo, aggregates_repo, source_repo, edge_repo,
                                      scope_id, ts_from, ts_to, timezone_name)
                    return json_response(_tag_result(result, pinned_revision).to_dict())

                elif mode == "comparison":
                    point_ids = data.get("point_ids") or []
                    if not point_ids:
                        body, status = _err("bad_request", "comparison требует point_ids", 400,
                                             fields=["point_ids"])
                        return json_response(body, status)
                    results = comparison(binding_repo, aggregates_repo, source_repo,
                                          point_ids, ts_from, ts_to, timezone_name)
                    return json_response({
                        str(pid): _tag_result(r, pinned_revision).to_dict()
                        for pid, r in results.items()
                    })

                else:
                    body, status = _err(
                        "bad_request",
                        f"неизвестный mode={mode!r}, допустимые: measured|sum|balance|comparison",
                        400, fields=["mode"])
                    return json_response(body, status)

            except AccountingConflict as e:
                body, status = _err("double_counting", str(e), 409)
                return json_response(body, status)
            except ContractViolation as e:
                # не должно происходить при корректном коде сервиса — если
                # случилось, это внутренняя ошибка формирования результата,
                # не ошибка запроса клиента.
                log.exception("ContractViolation при metrics/query: %s", e)
                body, status = _err("internal", "внутренняя ошибка формирования результата", 500)
                return json_response(body, status)
            except ValueError as e:
                body, status = _err("bad_request", str(e), 400)
                return json_response(body, status)

    # ----------------------------------------------------------- snapshot
    # Этап E (ТЗ §8.2 «Обзор», §9.2/§10): пакет ТЕКУЩИХ значений для
    # выбранного набора точек, БЕЗ обращения к исторической агрегации —
    # источник для верхних KPI/таблицы ветвей "Обзора" на каждом
    # live-тике (§10: "раз в 5 с одним пакетным запросом... никаких 100
    # исторических RPC внутри live"). В отличие от /api/v2/metrics/query
    # (считает через accounting_service поверх period_aggregates), здесь
    # данные берутся из уже посчитанного в фоне `state.registry`
    # (см. status.py::StatusEngine — отдельный поток, независимо и
    # непрерывно классифицирует статус каждого устройства) — не тянет
    # MQTT-историю по запросу.
    def _point_snapshot(point, binding_repo, source_repo, registry):
        result = {
            "point_id": point.id, "code": point.code, "name": point.name,
            "enabled": bool(point.enabled),
            "binding_status": "unbound",
            "device_status": None,
            "device_status_reason": None,
            "power_w": None, "energy_total_kwh": None,
            "voltage_v": None, "current_a": None, "frequency_hz": None,
            "last_update_age_s": None, "last_measurement_age_s": None,
        }
        binding = binding_repo.get_open_primary(point.id)
        if binding is None:
            # ТЗ §8.5: "Нет источника измерения — выбрать прибор" — нет
            # открытой primary-привязки, дальше и смотреть не на что.
            return result
        result["binding_status"] = "bound"

        source = source_repo.get_by_id(binding.meter_source_id)
        if source is None:
            # FK (point_bindings.meter_source_id -> meter_sources.id) не
            # даёт этому случиться через обычные операции — но раз этот
            # путь в принципе достижим (сырой SQL, ручное вмешательство),
            # одна такая строка не должна валить весь снимок с 500.
            result["device_status"] = "unknown"
            result["device_status_reason"] = "Источник привязки не найден"
            return result

        meter = registry.get(source.device_id)
        if meter is None:
            # ТЗ A38: устройство зарегистрировано в БД как источник, но
            # MQTT его ЕЩЁ НИ РАЗУ не видел с момента старта демона —
            # отдельное состояние от "no_connection" (тот значит "видели
            # раньше, сейчас недоступен").
            result["device_status"] = "never_seen"
            result["device_status_reason"] = "Устройство ещё не появлялось в MQTT"
            return result

        # meter.status уже посчитан фоновым StatusEngine — не пересчитываем
        # здесь заново (единый источник классификации, см. status.py).
        result["device_status"] = meter.status.value
        result["device_status_reason"] = meter.status_reason
        result["power_w"] = meter.get_float("Total P")
        result["energy_total_kwh"] = meter.get_float("Total AP energy")
        result["frequency_hz"] = meter.get_float("Frequency")

        voltage = {ph: meter.get_float(f"Urms {ph}") for ph in PHASES}
        if any(v is not None for v in voltage.values()):
            result["voltage_v"] = voltage
        current = {ph: meter.get_float(f"Irms {ph}") for ph in PHASES}
        if any(v is not None for v in current.values()):
            result["current_a"] = current

        result["last_update_age_s"] = (
            time.time() - meter.last_any_ts if meter.last_any_ts > 0 else None
        )
        result["last_measurement_age_s"] = (
            time.time() - meter.last_measurement_ts
            if meter.last_measurement_ts > 0 else None
        )
        return result

    @app.route("/api/v2/snapshot", methods=["GET"])
    def v2_snapshot():
        registry = state.registry
        point_repo = _point_repo()
        binding_repo = _binding_repo()
        source_repo = _source_repo()

        raw_ids = request.args.get("point_ids")
        if raw_ids:
            try:
                point_ids = [int(x) for x in raw_ids.split(",") if x.strip()]
            except ValueError:
                body, status = _err(
                    "bad_request",
                    "point_ids должен быть списком целых чисел через запятую",
                    400, fields=["point_ids"])
                return json_response(body, status)
            points = []
            missing_ids = []
            for pid in point_ids:
                p = point_repo.get_by_id(pid)
                if p is None:
                    missing_ids.append(pid)
                else:
                    points.append(p)
            if missing_ids:
                body, status = _err(
                    "not_found", f"Точки не найдены: {missing_ids}", 404,
                    ids=missing_ids)
                return json_response(body, status)
        else:
            # По умолчанию — все НЕархивные точки; §5/§9: enabled=0
            # исключает точку из активного состава, но она остаётся
            # видимой (не пропадает из ответа) — архивные же в общий
            # снимок молча не попадают, их нужно запросить явно по id.
            points = point_repo.list_all(include_archived=False)

        items = [
            _point_snapshot(p, binding_repo, source_repo, registry)
            for p in points
        ]
        return json_response({
            "as_of": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "points": items,
        })

    # ------------------------------------ структура/инспектор (партия 3, задача 1)
    # ТЗ §8.3: "слева дерево и поиск... поиск по имени, коду точки, MQTT
    # ID, серийному номеру и пути" (A02). Этот маршрут отдаёт ТОЛЬКО то,
    # чего не хватает обычному GET /api/v2/points для такого поиска —
    # путь по месту установки и адрес/серийник ДЕЙСТВУЮЩЕГО прибора.
    # Расход, состояние, история замен и т.п. инспектор запрашивает
    # отдельно через уже существующие ручки (snapshot, metrics/query,
    # points/<id>/bindings, points/<id>/groups, topology/*) — здесь
    # заново не считаются и не дублируются (§8.3: "инспектор... через
    # уже имеющиеся ручки v2").
    @app.route("/api/v2/structure/points", methods=["GET"])
    def v2_structure_points():
        point_repo = _point_repo()
        location_repo = _location_repo()
        binding_repo = _binding_repo()
        source_repo = _source_repo()
        meters_repo_legacy = MeterRepo(db, GroupRepo(db))

        locations_by_id = {
            l.id: l for l in location_repo.list_all(include_archived=True)}

        def _location_path(loc_id):
            if loc_id is None:
                return None
            parts = []
            seen = set()
            cur_id = loc_id
            while cur_id is not None and cur_id not in seen:
                seen.add(cur_id)
                loc = locations_by_id.get(cur_id)
                if loc is None:
                    break
                parts.append(loc.name)
                cur_id = loc.parent_id
            return " / ".join(reversed(parts)) if parts else None

        with db.read() as c:
            plan_point_ids = {
                row["point_id"] for row in c.execute(
                    "SELECT DISTINCT point_id FROM plan_items "
                    "WHERE kind = 'point' AND point_id IS NOT NULL "
                    "AND archived_at IS NULL").fetchall()
            }

        q = (request.args.get("q") or "").strip().casefold()
        items = []
        for p in point_repo.list_all(include_archived=False):
            binding = binding_repo.get_open_primary(p.id)
            meter_device_id = meter_controller_key = meter_serial = None
            if binding is not None:
                source = source_repo.get_by_id(binding.meter_source_id)
                if source is not None:
                    meter_device_id = source.device_id
                    meter_controller_key = source.controller_key
                    meter = meters_repo_legacy.get_by_id(source.meter_id)
                    if meter is not None:
                        meter_serial = meter.serial_number

            location_path = _location_path(p.installation_location_id)

            if q:
                searchable = " ".join(str(x) for x in (
                    p.code, p.name, meter_device_id, meter_serial,
                    location_path) if x).casefold()
                if q not in searchable:
                    continue

            items.append({
                "point_id": p.id, "code": p.code, "name": p.name,
                "enabled": bool(p.enabled),
                "location_id": p.installation_location_id,
                "location_path": location_path,
                "bound": binding is not None,
                "meter_device_id": meter_device_id,
                "meter_controller_key": meter_controller_key,
                "meter_serial": meter_serial,
                "placed_on_plan": p.id in plan_point_ids,
            })

        return json_response({"points": items})

    # ---------------------------------------- validation (партия 3, задача 4)
    # ТЗ §8.2/§13, docs/TZ-batch3 задача 4: "на ста точках без него
    # невозможно найти забытое". Только чтение — ничего не изменяет и не
    # требует expected_revision. Один db.read() на весь расчёт (тот же
    # принцип согласованного снимка A43, что и у overview/reports, хотя
    # формально сюда не входит протокол ревизий — это не предметная
    # запись).
    #
    # "Влияние", а не алфавит (§8.2): не оценка серьёзности каждой
    # конкретной проблемы (это отдельная, более крупная задача), а
    # фиксированный порядок категорий по тому, что они означают для
    # расчёта — от "данных не существует вообще" до "не заведено на
    # плане" (чисто навигационное неудобство). Внутри категории —
    # по id (стабильно, не по имени).
    _VALIDATION_IMPACT_RANK = {
        "no_meter": 1,               # точка есть, измерять нечем
        "edge_without_measurement": 2,  # связь есть, чем измерена — нет
        "no_location": 3,
        "no_group": 4,
        "no_plan": 5,
        "node_without_edges": 6,     # узел висит в воздухе, ни к чему не относится
    }

    @app.route("/api/v2/validation", methods=["GET"])
    def v2_validation():
        point_repo = _point_repo()
        binding_repo = _binding_repo()
        node_repo = _node_repo()
        edge_repo = _edge_repo()

        with db.read() as c:
            points = point_repo.list_all(include_archived=False)
            nodes = node_repo.list_all(include_archived=False)
            published_edges = edge_repo.list_active_published()

            group_point_ids = {
                row["point_id"] for row in c.execute(
                    "SELECT DISTINCT point_id FROM group_memberships "
                    "WHERE valid_to IS NULL").fetchall()
            }
            plan_point_ids = {
                row["point_id"] for row in c.execute(
                    "SELECT DISTINCT point_id FROM plan_items "
                    "WHERE kind = 'point' AND point_id IS NOT NULL "
                    "AND archived_at IS NULL").fetchall()
            }

        # §8.2 (задача 4, известное поведение): точка ввода не обязана
        # состоять в ветвях/группах — это не забытая настройка, а
        # нормальное свойство ввода. Тот же список, что уже считает
        # overview/summary для "не входит ни в одну ветвь" (см. выше).
        input_point_ids = set(_resolve_object_input_point_ids(node_repo, edge_repo))

        nodes_with_edge = set()
        for e in published_edges:
            nodes_with_edge.add(e.from_node_id)
            nodes_with_edge.add(e.to_node_id)

        points_without_meter = []
        points_without_location = []
        points_without_group = []
        points_without_plan = []
        for p in points:
            if binding_repo.get_open_primary(p.id) is None:
                points_without_meter.append(p)
            if p.installation_location_id is None:
                points_without_location.append(p)
            if p.id not in group_point_ids and p.id not in input_point_ids:
                points_without_group.append(p)
            if p.id not in plan_point_ids:
                points_without_plan.append(p)

        nodes_without_edges = [n for n in nodes if n.id not in nodes_with_edge]
        edges_without_measurement = [
            e for e in published_edges if e.primary_point_id is None]

        def _point_ref(p):
            return {"point_id": p.id, "code": p.code, "name": p.name}

        def _node_ref(n):
            return {"node_id": n.id, "code": n.code, "name": n.name}

        def _edge_ref(e):
            return {"edge_id": e.id, "code": e.code, "name": e.name,
                    "from_node_id": e.from_node_id, "to_node_id": e.to_node_id}

        issues = []
        for p in points_without_meter:
            issues.append({"kind": "no_meter", "entity_type": "point",
                            "entity_id": p.id, "label": p.name, "code": p.code})
        for e in edges_without_measurement:
            issues.append({"kind": "edge_without_measurement", "entity_type": "edge",
                            "entity_id": e.id, "label": e.name or e.code or f"#{e.id}",
                            "code": e.code})
        for p in points_without_location:
            issues.append({"kind": "no_location", "entity_type": "point",
                            "entity_id": p.id, "label": p.name, "code": p.code})
        for p in points_without_group:
            issues.append({"kind": "no_group", "entity_type": "point",
                            "entity_id": p.id, "label": p.name, "code": p.code})
        for p in points_without_plan:
            issues.append({"kind": "no_plan", "entity_type": "point",
                            "entity_id": p.id, "label": p.name, "code": p.code})
        for n in nodes_without_edges:
            issues.append({"kind": "node_without_edges", "entity_type": "node",
                            "entity_id": n.id, "label": n.name, "code": n.code})
        issues.sort(key=lambda it: (
            _VALIDATION_IMPACT_RANK.get(it["kind"], 99), it["entity_id"]))

        return json_response({
            "as_of": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "points_without_meter": [_point_ref(p) for p in points_without_meter],
            "points_without_location": [_point_ref(p) for p in points_without_location],
            "points_without_group": [_point_ref(p) for p in points_without_group],
            "points_without_plan": [_point_ref(p) for p in points_without_plan],
            "nodes_without_edges": [_node_ref(n) for n in nodes_without_edges],
            "edges_without_measurement": [_edge_ref(e) for e in edges_without_measurement],
            "issues": issues,
            "total_issues": len(issues),
        })

    # ------------------------------------------------- overview (задача 2)
    # ТЗ §8.2, партия 2: "Обзор отвечает на четыре вопроса: сколько
    # потребляет объект, какие ветви дают расход, где проблема, каких
    # данных нет". Единственная задача этого маршрута — ОДИН раз собрать
    # то, что экран и CSV обязаны показывать одинаково (A43): итог объекта
    # через назначенный ввод (а не сумму счётчиков), ветви верхнего
    # уровня, небаланс подписанным. Экран (index.html) не считает
    # ничего сам — только рендерит уже посчитанное здесь через
    # accounting_service (сформулировано в задании прямым текстом:
    # "если обнаружишь, что проще посчитать на фронте — это сигнал, что
    # ты идёшь не туда").

    def _resolve_object_input_point_ids(node_repo, edge_repo):
        """§8.2: "Основа итога — назначенный ввод". Ввод объекта — это
        primary_point_id каждой действующей ОПУБЛИКОВАННОЙ связи,
        исходящей из узла kind='source' (внешний ввод сети — по
        валидатору топологии source никогда не бывает приёмником, см.
        topology_service.validate_forest). Несколько вводов
        суммируются через тот же sum_points, что и любая ветвь — двойной
        счёт (A03/A04) проверяется той же логикой, что и везде."""
        edges = edge_repo.list_active_published()
        point_ids = []
        for e in edges:
            if e.primary_point_id is None:
                continue
            node = node_repo.get_by_id(e.from_node_id)
            if node is not None and node.kind == "source":
                point_ids.append(e.primary_point_id)
        return list(dict.fromkeys(point_ids))

    def _overview_branch_result(member_point_ids, binding_repo, aggregates_repo,
                                 source_repo, edge_repo, ts_from, ts_to, timezone_name):
        """§8.2: "Если у выбранной группы неподтверждённый non-overlap —
        Обзор переключается в режим сравнения точек (без общего итога)".
        sum_points уже возвращает structure_quality=unverified, когда
        топология части точек ПРОСТО неизвестна (это нормальный
        промежуточный итог) — единственный случай, требующий переключения
        в comparison, это ПОДТВЕРЖДЁННОЕ пересечение (AccountingConflict,
        A04)."""
        if not member_point_ids:
            return {"mode": "sum", "result": None, "conflict_reason": None}
        try:
            result = sum_points(binding_repo, aggregates_repo, source_repo, edge_repo,
                                 member_point_ids, ts_from, ts_to, timezone_name)
            return {"mode": "sum", "result": result, "conflict_reason": None}
        except AccountingConflict as e:
            results = comparison(binding_repo, aggregates_repo, source_repo,
                                  member_point_ids, ts_from, ts_to, timezone_name)
            return {"mode": "comparison", "result": results, "conflict_reason": str(e)}

    @app.route("/api/v2/overview/summary", methods=["POST"])
    def v2_overview_summary():
        """Тело: {"from", "to", "timezone", "configuration_revision_id"?}
        (те же поля, что и metrics/query, — A43 тем же приёмом: один
        db.read() на весь расчёт, ревизия фиксируется один раз в начале)."""
        data = request.get_json(silent=True) or {}
        timezone_name = data.get("timezone", "UTC")
        try:
            ts_from = _parse_ts(data.get("from"), "from")
            ts_to = _parse_ts(data.get("to"), "to")
        except ValueError as e:
            body, status = _err("bad_request", str(e), 400, fields=["from", "to"])
            return json_response(body, status)
        if ts_to <= ts_from:
            body, status = _err("bad_request", "to должно быть позже from", 400, fields=["from", "to"])
            return json_response(body, status)

        requested_revision = data.get("configuration_revision_id")

        binding_repo = _binding_repo()
        aggregates_repo = _aggregates_repo()
        source_repo = _source_repo()
        edge_repo = _edge_repo()
        node_repo = _node_repo()
        group_repo = _group_repo_v2()

        with db.read() as c:
            if requested_revision is not None:
                try:
                    requested_revision = int(requested_revision)
                except (TypeError, ValueError):
                    body, status = _err(
                        "bad_request", "configuration_revision_id должен быть целым числом",
                        400, fields=["configuration_revision_id"])
                    return json_response(body, status)
                if not revision_exists(c, requested_revision):
                    body, status = _err(
                        "not_found", f"Ревизия конфигурации {requested_revision} не найдена",
                        404, ids=[requested_revision])
                    return json_response(body, status)
                pinned_revision = requested_revision
            else:
                pinned_revision = current_revision_id(c)

            try:
                input_point_ids = _resolve_object_input_point_ids(node_repo, edge_repo)
                object_total = None
                object_total_unavailable_reason = None
                if not input_point_ids:
                    # §8.2: "Без назначенного ввода показывать действие
                    # 'Настроить границу объекта', а не ложное число" —
                    # никакой суммы всех счётчиков вместо этого.
                    object_total_unavailable_reason = "no_input_assigned"
                else:
                    try:
                        object_total = _tag_result(
                            sum_points(binding_repo, aggregates_repo, source_repo, edge_repo,
                                       input_point_ids, ts_from, ts_to, timezone_name),
                            pinned_revision)
                    except AccountingConflict as e:
                        object_total_unavailable_reason = f"input_overlap_conflict: {e}"

                branches = []
                for g in group_repo.list_children(None):
                    member_ids = [
                        m["point_id"] for m in
                        group_repo.resolve_effective_members(g.id)
                    ]
                    branch = _overview_branch_result(
                        member_ids, binding_repo, aggregates_repo, source_repo, edge_repo,
                        ts_from, ts_to, timezone_name)
                    entry = {
                        "group_id": g.id, "name": g.name, "category": g.category,
                        "member_point_ids": member_ids,
                        "mode": branch["mode"],
                        "conflict_reason": branch["conflict_reason"],
                    }
                    if branch["mode"] == "sum":
                        result = branch["result"]
                        if result is not None:
                            _tag_result(result, pinned_revision)
                            entry["result"] = result.to_dict()
                            pct, pct_reason = resolve_percentage(
                                result.value,
                                object_total.value if object_total is not None else None)
                            entry["percentage_of_object"] = pct
                            entry["percentage_of_object_reason"] = pct_reason
                        else:
                            entry["result"] = None
                            entry["percentage_of_object"] = None
                            entry["percentage_of_object_reason"] = "no_data"
                    else:
                        entry["points"] = {
                            str(pid): _tag_result(r, pinned_revision).to_dict()
                            for pid, r in branch["result"].items()
                        }
                    branches.append(entry)

                # §8.2: "Небаланс — отдельной строкой, знак сохраняется".
                # Считается только если известны и итог объекта, и ВСЕ
                # ветви режима sum с числовым result.value; иначе
                # неизвестен целиком (не подменяется нулём/частичной
                # суммой — тот же инвариант A08/A09, что и в balance()).
                imbalance_value = None
                if (object_total is not None and object_total.value is not None
                        and all(b["mode"] == "sum" and b.get("result") is not None
                                and b["result"]["value"] is not None for b in branches)):
                    branches_sum = sum(b["result"]["value"] for b in branches)
                    imbalance_value = round(object_total.value - branches_sum, 6)

                # A08 буквально требует «Небаланс −10, −10%»: одного
                # значения в кВт·ч недостаточно, процент от итога объекта
                # входит в критерий. Считаем ТЕМ ЖЕ resolve_percentage,
                # что и проценты ветвей, — он один отвечает за правило
                # A11 (нет базы или база <= 0 -> None с причиной, без
                # деления на ноль и фиктивных 100%). Не считать процент
                # на фронте: экран, отчёт и CSV обязаны брать одно и то
                # же число из одного расчёта (A43).
                imbalance_percent, imbalance_percent_reason = resolve_percentage(
                    imbalance_value,
                    object_total.value if object_total is not None else None)

                ungrouped_ids = None
                if object_total is not None or branches:
                    grouped = set()
                    for b in branches:
                        grouped.update(b["member_point_ids"])
                    all_ids = {p.id for p in _point_repo().list_all(include_archived=False)}
                    ungrouped_ids = sorted(all_ids - grouped)

            except ContractViolation as e:
                log.exception("ContractViolation при overview/summary: %s", e)
                body, status = _err("internal", "внутренняя ошибка формирования результата", 500)
                return json_response(body, status)
            except ValueError as e:
                body, status = _err("bad_request", str(e), 400)
                return json_response(body, status)

        return json_response({
            "configuration_revision_id": pinned_revision,
            "as_of": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "period": {"from": str(ts_from), "to": str(ts_to), "timezone": timezone_name},
            "object_input_point_ids": input_point_ids,
            "object_total": object_total.to_dict() if object_total is not None else None,
            "object_total_unavailable_reason": object_total_unavailable_reason,
            "imbalance_value": imbalance_value,
            "imbalance_percent": imbalance_percent,
            "imbalance_percent_reason": imbalance_percent_reason,
            "branches": branches,
            "ungrouped_point_ids": ungrouped_ids,
        })

    # ------------------------------------------------------------ reports
    # Партия 2, задача 3 (ТЗ §8.4, docs/TZ-batch2-overview-reports-revisions.md):
    # новые срезы отчёта (точка/ветвь/группа/граница баланса) поверх ТОГО
    # ЖЕ расчётного слоя v2 (accounting_service), что и Обзор и
    # metrics/query — числа совпадают по построению (не пересчитываются
    # заново отдельной формулой в CSV/JS, ТЗ §5: "не реализовывать разные
    # формулы в дашборде, отчётах и JavaScript"). Существующие отчёты (v1,
    # api.py + клиентский JS в index.html) не трогаются — это ДОБАВЛЕНИЕ
    # нового маршрута, см. §9.2 запрет менять существующие форматы ответа.
    #
    # A21/A22 — раздельные режимы состава группы для отчёта за прошлый
    # период:
    #   composition_mode="as_was" (по умолчанию) — эффективный состав
    #   группы резолвится НА МОМЕНТ начала запрошенного периода
    #   (group_repo.resolve_effective_members(gid, at=ts_from)) — перевод
    #   точки в другую группу ПОСЛЕ периода не меняет уже посчитанный
    #   прошлый отчёт (A21: "перенос точки из арендатора А в Б с 15
    #   числа — отчёт за прошлый месяц не меняется").
    #   composition_mode="current" — состав группы на сейчас, но приборы
    #   всё равно резолвятся исторически верно: measured_point() всегда
    #   сегментирует период по истории привязок точки независимо от
    #   composition_mode (A22: "текущие группы, но исторически правильные
    #   физические приборы").
    # Оба режима явно возвращаются в ответе (`composition_mode`) —
    # фронтенд обязан подписать выбранный режим и на экране, и в CSV,
    # чтобы никогда не выдавать один набор чисел за другой молча.
    def _reports_resolve_group_points(group_repo, group_id, composition_mode, ts_from):
        at = ts_from if composition_mode == "as_was" else None
        return [m["point_id"] for m in group_repo.resolve_effective_members(group_id, at=at)]

    def _reports_row_result(dimension, point_ids, binding_repo, aggregates_repo,
                             source_repo, edge_repo, ts_from, ts_to, timezone_name):
        """Точка — measured_point по единственному id; ветвь/группа —
        sum_points по составу. A04: подтверждённое электрическое
        пересечение НЕ схлопывает всю выгрузку — только эта строка
        получает conflict_reason и result=None, остальные строки
        считаются как обычно (в отличие от Обзора, здесь без отката в
        comparison — отчёт технический, конфликт должен быть виден и
        устранён в топологии, а не молча подменён поточной раскладкой)."""
        if dimension == "point":
            if not point_ids:
                return None, "точка не найдена"
            result = measured_point(binding_repo, aggregates_repo, source_repo,
                                     point_ids[0], ts_from, ts_to, timezone_name)
            return result, None
        if not point_ids:
            return None, None
        try:
            result = sum_points(binding_repo, aggregates_repo, source_repo, edge_repo,
                                 point_ids, ts_from, ts_to, timezone_name)
            return result, None
        except AccountingConflict as e:
            return None, str(e)

    @app.route("/api/v2/reports/query", methods=["POST"])
    def v2_reports_query():
        """Тело: {"dimension": "point"|"branch"|"group"|"balance_scope",
        "scope_ids"?: [...] (обязателен для point/group/balance_scope;
        для branch игнорируется — берутся все ветви верхнего уровня группы,
        как в Обзоре), "from", "to", "timezone"?,
        "configuration_revision_id"?, "composition_mode"?: "as_was"|
        "current" (по умолчанию as_was, A21/A22 — см. комментарий выше),
        "compare"?: {"from","to"} — второй период, чтобы явно раскрыть
        отличие состава/замены между периодами (A21/A22), а не молча
        показать несравнимые числа рядом.

        A43: один db.read() на весь расчёт — тот же приём, что и в
        metrics/query и overview/summary (см. их докстринги)."""
        data = request.get_json(silent=True) or {}
        dimension = data.get("dimension")
        if dimension not in ("point", "branch", "group", "balance_scope"):
            body, status = _err(
                "bad_request",
                f"dimension={dimension!r} — допустимые: point|branch|group|balance_scope",
                400, fields=["dimension"])
            return json_response(body, status)

        composition_mode = data.get("composition_mode", "as_was")
        if composition_mode not in ("as_was", "current"):
            body, status = _err(
                "bad_request", "composition_mode — допустимые: as_was|current", 400,
                fields=["composition_mode"])
            return json_response(body, status)

        timezone_name = data.get("timezone", "UTC")
        try:
            ts_from = _parse_ts(data.get("from"), "from")
            ts_to = _parse_ts(data.get("to"), "to")
        except ValueError as e:
            body, status = _err("bad_request", str(e), 400, fields=["from", "to"])
            return json_response(body, status)
        if ts_to <= ts_from:
            body, status = _err("bad_request", "to должно быть позже from", 400, fields=["from", "to"])
            return json_response(body, status)

        compare = data.get("compare")
        cmp_ts_from = cmp_ts_to = None
        if compare is not None:
            if not isinstance(compare, dict):
                body, status = _err("bad_request", "compare должен быть объектом {from,to}", 400,
                                     fields=["compare"])
                return json_response(body, status)
            try:
                cmp_ts_from = _parse_ts(compare.get("from"), "compare.from")
                cmp_ts_to = _parse_ts(compare.get("to"), "compare.to")
            except ValueError as e:
                body, status = _err("bad_request", str(e), 400, fields=["compare"])
                return json_response(body, status)
            if cmp_ts_to <= cmp_ts_from:
                body, status = _err("bad_request", "compare.to должно быть позже compare.from",
                                     400, fields=["compare"])
                return json_response(body, status)

        scope_ids = data.get("scope_ids")
        if dimension != "branch":
            if not scope_ids or not isinstance(scope_ids, list):
                body, status = _err(
                    "bad_request", f"dimension={dimension} требует непустой scope_ids", 400,
                    fields=["scope_ids"])
                return json_response(body, status)

        requested_revision = data.get("configuration_revision_id")

        binding_repo = _binding_repo()
        aggregates_repo = _aggregates_repo()
        source_repo = _source_repo()
        edge_repo = _edge_repo()
        group_repo = _group_repo_v2()
        point_repo = _point_repo()

        with db.read() as c:
            if requested_revision is not None:
                try:
                    requested_revision = int(requested_revision)
                except (TypeError, ValueError):
                    body, status = _err(
                        "bad_request", "configuration_revision_id должен быть целым числом",
                        400, fields=["configuration_revision_id"])
                    return json_response(body, status)
                if not revision_exists(c, requested_revision):
                    body, status = _err(
                        "not_found", f"Ревизия конфигурации {requested_revision} не найдена",
                        404, ids=[requested_revision])
                    return json_response(body, status)
                pinned_revision = requested_revision
            else:
                pinned_revision = current_revision_id(c)

            try:
                targets = []  # (id, name, point_ids | None для balance_scope)
                if dimension == "point":
                    for pid in scope_ids:
                        p = point_repo.get_by_id(pid)
                        if p is None:
                            body, status = _err("not_found", f"Точка {pid} не найдена", 404, ids=[pid])
                            return json_response(body, status)
                        targets.append((p.id, p.name, [p.id]))
                elif dimension == "branch":
                    for g in group_repo.list_children(None):
                        pts = _reports_resolve_group_points(group_repo, g.id, composition_mode, ts_from)
                        targets.append((g.id, g.name, pts))
                elif dimension == "group":
                    for gid in scope_ids:
                        g = group_repo.get_by_id(gid)
                        if g is None:
                            body, status = _err("not_found", f"Группа {gid} не найдена", 404, ids=[gid])
                            return json_response(body, status)
                        pts = _reports_resolve_group_points(group_repo, gid, composition_mode, ts_from)
                        targets.append((g.id, g.name, pts))
                else:  # balance_scope
                    for sid in scope_ids:
                        row = c.execute(
                            "SELECT * FROM balance_scopes WHERE id = ?", (sid,)).fetchone()
                        if row is None:
                            body, status = _err("not_found", f"Граница баланса {sid} не найдена",
                                                 404, ids=[sid])
                            return json_response(body, status)
                        targets.append((row["id"], row["name"], None))

                rows = []
                for scope_id, name, point_ids in targets:
                    if dimension == "balance_scope":
                        result = balance(db, binding_repo, aggregates_repo, source_repo, edge_repo,
                                          scope_id, ts_from, ts_to, timezone_name)
                        conflict_reason = None
                        member_ids = (result.explanation.get("input_point_ids", [])
                                      + result.explanation.get("output_point_ids", []))
                    else:
                        result, conflict_reason = _reports_row_result(
                            dimension, point_ids, binding_repo, aggregates_repo, source_repo,
                            edge_repo, ts_from, ts_to, timezone_name)
                        member_ids = point_ids

                    if result is not None:
                        _tag_result(result, pinned_revision)

                    row = {
                        "dimension": dimension,
                        "id": scope_id,
                        "name": name,
                        "member_point_ids": member_ids,
                        "result": result.to_dict() if result is not None else None,
                        "conflict_reason": conflict_reason,
                    }

                    if compare is not None:
                        if dimension in ("branch", "group"):
                            cmp_point_ids = _reports_resolve_group_points(
                                group_repo, scope_id, composition_mode, cmp_ts_from)
                        else:
                            cmp_point_ids = point_ids

                        if dimension == "balance_scope":
                            cmp_result = balance(db, binding_repo, aggregates_repo, source_repo,
                                                  edge_repo, scope_id, cmp_ts_from, cmp_ts_to,
                                                  timezone_name)
                            cmp_conflict = None
                            cmp_member_ids = (cmp_result.explanation.get("input_point_ids", [])
                                              + cmp_result.explanation.get("output_point_ids", []))
                        else:
                            cmp_result, cmp_conflict = _reports_row_result(
                                dimension, cmp_point_ids, binding_repo, aggregates_repo,
                                source_repo, edge_repo, cmp_ts_from, cmp_ts_to, timezone_name)
                            cmp_member_ids = cmp_point_ids

                        if cmp_result is not None:
                            _tag_result(cmp_result, pinned_revision)

                        # A21/A22: явно раскрываем отличие состава между
                        # периодами, а не молча публикуем разницу чисел,
                        # посчитанных по разному составу точек. В режиме
                        # composition_mode="current" состав в обоих
                        # периодах резолвится "на сейчас" (at=None) — по
                        # определению одинаков, composition_changed всегда
                        # False (это и есть смысл режима "current").
                        composition_changed = (
                            sorted(member_ids or []) != sorted(cmp_member_ids or [])
                            if dimension in ("branch", "group") else False
                        )

                        delta_value = None
                        if (result is not None and cmp_result is not None
                                and result.value is not None and cmp_result.value is not None):
                            delta_value = round(result.value - cmp_result.value, 6)
                        delta_pct, delta_pct_reason = resolve_percentage(
                            delta_value, cmp_result.value if cmp_result is not None else None)

                        row["compare_member_point_ids"] = cmp_member_ids
                        row["compare_result"] = cmp_result.to_dict() if cmp_result is not None else None
                        row["compare_conflict_reason"] = cmp_conflict
                        row["composition_changed"] = composition_changed
                        row["delta_value"] = delta_value
                        row["delta_percentage"] = delta_pct
                        row["delta_percentage_reason"] = delta_pct_reason

                    rows.append(row)

            except ContractViolation as e:
                log.exception("ContractViolation при reports/query: %s", e)
                body, status = _err("internal", "внутренняя ошибка формирования результата", 500)
                return json_response(body, status)
            except ValueError as e:
                body, status = _err("bad_request", str(e), 400)
                return json_response(body, status)

        response = {
            "configuration_revision_id": pinned_revision,
            "as_of": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "dimension": dimension,
            "composition_mode": composition_mode,
            "period": {"from": str(ts_from), "to": str(ts_to), "timezone": timezone_name},
            "rows": rows,
        }
        if compare is not None:
            response["compare_period"] = {
                "from": str(cmp_ts_from), "to": str(cmp_ts_to), "timezone": timezone_name}
        return json_response(response)

    # -------------------------------------------------------------- plans
    # Стадия D (ТЗ §7): план помещений/однолинейная схема поверх
    # site_plans(plan_kind/canvas_*) — параллельно v1 /api/plans
    # (plan_zones/plan_links), который не трогается (см. plan_service_v2
    # module docstring). Фронтенд-редактор (Leaflet-Geoman) в эту
    # задачу не входит — здесь только HTTP-слой над репозиториями.

    def _plan_to_dict_v2(p):
        return p.to_dict()

    def _plan_item_to_dict(i):
        return i.to_dict()

    def _plan_edge_view_to_dict(v):
        return v.to_dict()

    @app.route("/api/v2/plans", methods=["GET", "POST"])
    def v2_plans():
        if state.plans_dir is None:
            body, status = _err("service_unavailable", "plans_dir не настроен", 503)
            return json_response(body, status)
        repo = _plan_repo_v2()
        if request.method == "GET":
            plan_kind = request.args.get("plan_kind")
            return json_response([_plan_to_dict_v2(p) for p in repo.list_all(plan_kind)])

        # multipart/form-data (как v1 /api/plans, §6/§7.2): name,
        # plan_kind, file (необязателен для single_line),
        # canvas_width/canvas_height (для пустой single_line).
        name = (request.form.get("name") or "").strip()
        plan_kind = request.form.get("plan_kind", "floor")
        image_bytes = None
        upload = request.files.get("file")
        if upload is not None:
            image_bytes = upload.stream.read()
        canvas_width = request.form.get("canvas_width")
        canvas_height = request.form.get("canvas_height")
        try:
            canvas_width = int(canvas_width) if canvas_width else None
            canvas_height = int(canvas_height) if canvas_height else None
        except ValueError:
            body, status = _err(
                "bad_request", "canvas_width/canvas_height должны быть целыми числами",
                400, fields=["canvas_width", "canvas_height"])
            return json_response(body, status)

        try:
            plan = repo.create(
                name=name, plan_kind=plan_kind, image_bytes=image_bytes,
                canvas_width=canvas_width, canvas_height=canvas_height)
        except PlanError as e:
            body, status = _err("bad_request", str(e), 400)
            return json_response(body, status)
        return json_response(_plan_to_dict_v2(plan), 201)

    @app.route("/api/v2/plans/<int:plan_id>", methods=["GET", "DELETE"])
    def v2_plan_detail(plan_id):
        plan, err = _plan_or_404_v2(plan_id)
        if err:
            return err

        if request.method == "DELETE":
            _plan_repo_v2().delete(plan_id)
            return json_response({"ok": True, "id": plan_id})

        out = _plan_to_dict_v2(plan)
        out["items"] = [_plan_item_to_dict(i) for i in _plan_item_repo().list_for_plan(plan_id)]
        out["edges"] = [_plan_edge_view_to_dict(v)
                        for v in _plan_edge_view_repo().list_for_plan(plan_id)]
        return json_response(out)

    @app.route("/api/v2/plans/<int:plan_id>/image", methods=["GET", "POST"])
    def v2_plan_image(plan_id):
        plan, err = _plan_or_404_v2(plan_id)
        if err:
            return err
        if state.plans_dir is None:
            body, status = _err("service_unavailable", "plans_dir не настроен", 503)
            return json_response(body, status)

        if request.method == "GET":
            if not plan.image_file:
                body, status = _err(
                    "not_found", f"У плана {plan_id} нет фонового изображения", 404,
                    ids=[plan_id])
                return json_response(body, status)
            data = read_plan_image(state.plans_dir, plan.image_file)
            if data is None:
                body, status = _err("not_found", "файл изображения не найден", 404, ids=[plan_id])
                return json_response(body, status)
            ext = os.path.splitext(plan.image_file)[1].lower()
            mime = "image/png" if ext == ".png" else "image/jpeg"
            resp = Response(data, mimetype=mime)
            resp.headers["Cache-Control"] = "private, max-age=3600"
            resp.headers["X-Content-Type-Options"] = "nosniff"
            return resp

        upload = request.files.get("file")
        if upload is None:
            body, status = _err("bad_request", "Файл не передан (поле формы file)", 400,
                                 fields=["file"])
            return json_response(body, status)
        image_bytes = upload.stream.read()
        try:
            plan = _plan_repo_v2().replace_image(plan_id, image_bytes)
        except PlanError as e:
            body, status = _err("bad_request", str(e), 400)
            return json_response(body, status)
        return json_response(_plan_to_dict_v2(plan))

    @app.route("/api/v2/plans/<int:plan_id>/items", methods=["GET", "POST"])
    def v2_plan_items(plan_id):
        plan, err = _plan_or_404_v2(plan_id)
        if err:
            return err
        repo = _plan_item_repo()
        if request.method == "GET":
            return json_response([_plan_item_to_dict(i) for i in repo.list_for_plan(plan_id)])

        data, _envelope = _unwrap_data(request.get_json(silent=True) or {})
        try:
            item = repo.add(
                plan_id, kind=data.get("kind"), geometry=data.get("geometry"),
                coord_space=data.get("coord_space"),
                point_id=data.get("point_id"), location_id=data.get("location_id"),
                group_id=data.get("group_id"), node_id=data.get("node_id"),
                target_plan_id=data.get("target_plan_id"),
                label=data.get("label"), sort_order=data.get("sort_order", 0),
            )
        except PlanError as e:
            body, status = _err("bad_request", str(e), 400)
            return json_response(body, status)
        except ValueError as e:
            body, status = _err("bad_request", str(e), 400)
            return json_response(body, status)
        return json_response(_plan_item_to_dict(item), 201)

    @app.route("/api/v2/plans/<int:plan_id>/items/<int:item_id>", methods=["PATCH", "DELETE"])
    def v2_plan_item_detail(plan_id, item_id):
        plan, err = _plan_or_404_v2(plan_id)
        if err:
            return err
        repo = _plan_item_repo()
        item = repo.get_by_id(item_id)
        if item is None or item.plan_id != plan_id:
            body, status = _err(
                "not_found", f"Элемент плана {item_id} не найден на плане {plan_id}", 404,
                ids=[plan_id, item_id])
            return json_response(body, status)

        if request.method == "DELETE":
            repo.remove_from_plan(item_id)
            return json_response({"ok": True, "id": item_id})

        data, _envelope = _unwrap_data(request.get_json(silent=True) or {})
        if "geometry" not in data:
            body, status = _err("bad_request", "требуется geometry", 400, fields=["geometry"])
            return json_response(body, status)
        try:
            item = repo.update_geometry(item_id, data["geometry"], data.get("coord_space"))
        except PlanError as e:
            body, status = _err("bad_request", str(e), 400)
            return json_response(body, status)
        return json_response(_plan_item_to_dict(item))

    @app.route("/api/v2/plans/<int:plan_id>/edges", methods=["GET", "POST"])
    def v2_plan_edge_views(plan_id):
        plan, err = _plan_or_404_v2(plan_id)
        if err:
            return err
        repo = _plan_edge_view_repo()
        if request.method == "GET":
            return json_response([_plan_edge_view_to_dict(v) for v in repo.list_for_plan(plan_id)])

        data, _envelope = _unwrap_data(request.get_json(silent=True) or {})
        edge_id = data.get("edge_id")
        if edge_id is None:
            body, status = _err("bad_request", "требуется edge_id", 400, fields=["edge_id"])
            return json_response(body, status)
        try:
            view = repo.add(
                plan_id, edge_id,
                from_item_id=data.get("from_item_id"), to_item_id=data.get("to_item_id"),
                waypoints=data.get("waypoints"), view_kind=data.get("view_kind", "structural"),
            )
        except PlanError as e:
            body, status = _err("bad_request", str(e), 400)
            return json_response(body, status)
        except ValueError as e:
            body, status = _err("bad_request", str(e), 400)
            return json_response(body, status)
        return json_response(_plan_edge_view_to_dict(view), 201)

    @app.route("/api/v2/plans/<int:plan_id>/edges/<int:view_id>", methods=["DELETE"])
    def v2_plan_edge_view_delete(plan_id, view_id):
        plan, err = _plan_or_404_v2(plan_id)
        if err:
            return err
        view = _plan_edge_view_repo().get_by_id(view_id)
        if view is None or view.plan_id != plan_id:
            body, status = _err(
                "not_found", f"Связь {view_id} не найдена на плане {plan_id}", 404,
                ids=[plan_id, view_id])
            return json_response(body, status)
        _plan_edge_view_repo().remove(view_id)
        return json_response({"ok": True, "id": view_id})

    @app.route("/api/v2/plans/<int:plan_id>/layout", methods=["POST"])
    def v2_plan_layout(plan_id):
        """A35 (ТЗ §13): атомарное сохранение пакета изменений layout с
        оптимистичной блокировкой по canvas_revision. Тело: {"data":
        {"expected_revision": N, "item_ops": [...], "edge_view_ops": [...]}}
        (или плоское тело — см. _unwrap_data). Конфликт ревизии -> 409,
        БЕЗ каких-либо изменений в БД (save_plan_layout проверяет ревизию
        первой операцией транзакции)."""
        plan, err = _plan_or_404_v2(plan_id)
        if err:
            return err

        data, _envelope = _unwrap_data(request.get_json(silent=True) or {})
        expected_revision = data.get("expected_revision")
        if expected_revision is None:
            body, status = _err(
                "bad_request", "требуется expected_revision (текущая canvas_revision клиента)",
                400, fields=["expected_revision"])
            return json_response(body, status)

        try:
            updated = save_plan_layout(
                db, plan_id, expected_revision,
                item_ops=data.get("item_ops"), edge_view_ops=data.get("edge_view_ops"))
        except RevisionConflict as e:
            body, status = _err(
                "revision_conflict", str(e), 409, ids=[plan_id],
                fields=["expected_revision"])
            return json_response(body, status)
        except PlanError as e:
            body, status = _err("bad_request", str(e), 400, ids=[plan_id])
            return json_response(body, status)
        except ValueError as e:
            body, status = _err("bad_request", str(e), 400, ids=[plan_id])
            return json_response(body, status)
        return json_response(_plan_to_dict_v2(updated))

    # ------------------- мастер переноса legacy-связей (партия 3, задача 2)
    # ТЗ §2 (строка про §31.5): перенос СТАРЫХ связей (plan_links) в
    # electrical_edges — ТОЛЬКО с явным подтверждением каждой связи
    # пользователем (см. migration_wizard_service.py — там же почему
    # автоматика запрещена большим ТЗ). Старая модель не удаляется и не
    # меняется этими маршрутами.

    @app.route("/api/v2/migration/legacy-links", methods=["GET"])
    def v2_migration_legacy_links():
        """Список связей старой модели планов (все планы) с их концами
        (зона -> учётная группа) и статусом переноса (уже перенесена —
        по migration_map, или ожидает подтверждения). Только чтение."""
        plans_repo = SitePlanRepo(db)
        zone_repo = PlanZoneRepo(db)
        link_repo = PlanLinkRepo(db)
        groups_repo_legacy = GroupRepo(db)

        items = []
        with db.read() as c:
            for plan in plans_repo.list_all():
                for link in link_repo.list_by_plan(plan.id):
                    from_zone = zone_repo.get_by_id(link.from_zone_id)
                    to_zone = zone_repo.get_by_id(link.to_zone_id)

                    def _zone_ref(zone):
                        if zone is None:
                            return None
                        group = groups_repo_legacy.get_by_id(zone.group_id)
                        return {
                            "zone_id": zone.id, "group_id": zone.group_id,
                            "group_name": group.name if group else None,
                        }

                    migrated_edge_id = migration_wizard_service._map_get(
                        c, "plan_links", link.id, "electrical_edges")
                    items.append({
                        "plan_link_id": link.id, "plan_id": plan.id,
                        "plan_name": plan.name, "label": link.label,
                        "source_meter_id": link.source_meter_id,
                        "rated_current_a": link.rated_current_a,
                        "from_zone": _zone_ref(from_zone),
                        "to_zone": _zone_ref(to_zone),
                        "migration_status": (
                            "migrated" if migrated_edge_id is not None else "pending"),
                        "migrated_edge_id": migrated_edge_id,
                    })
        return json_response({"links": items})

    @app.route("/api/v2/migration/legacy-links/confirm", methods=["POST"])
    def v2_migration_legacy_links_confirm():
        """Подтвердить перенос одной или нескольких связей старой модели
        (тело: {"confirmations": [...], "expected_revision": N} — формат
        каждого элемента см. migration_wizard_service.confirm_links).
        Вся пачка — одна атомарная предметная запись: ошибка в любом
        элементе откатывает всю пачку целиком (ничего не создаётся),
        как и любая другая запись протокола ревизий."""
        body = request.get_json(silent=True) or {}
        data, _envelope = _unwrap_data(body)
        confirmations = data.get("confirmations")

        plan_link_repo = PlanLinkRepo(db)
        node_repo = _node_repo()
        edge_repo = _edge_repo()

        def _do_confirm():
            with db.transaction() as c:
                return migration_wizard_service.confirm_links(
                    c, plan_link_repo, node_repo, edge_repo, confirmations)

        try:
            results, new_rev = with_revision_check(
                db, data.get("expected_revision"), _do_confirm,
                touched=[("migration_wizard", len(confirmations)
                          if isinstance(confirmations, list) else 0)])
        except GlobalRevisionConflict as e:
            return _revision_conflict(e)
        except migration_wizard_service.WizardError as e:
            body, status = _err("bad_request", str(e), 400)
            return json_response(body, status)
        except ValueError as e:
            body, status = _err("bad_request", str(e), 400)
            return json_response(body, status)

        return json_response({
            "configuration_revision": new_rev,
            "results": [r.to_dict() for r in results],
        })

    # --- Разовый перенос легаси meters/meter_groups в v2 (Стадия F,
    # docs/migration-plan-v2.md §5 п.1-2) --------------------------------
    @app.route("/api/v2/admin/migrate-legacy", methods=["POST"])
    def v2_admin_migrate_legacy():
        """Переносит все строки `meters`/`meter_groups` в
        metering_point/meter_source/point_binding/group_memberships (см.
        legacy_migration.migrate_meters_and_groups). Без этого шага
        справочники point/node в редакторе плана v2 пусты — точки учёта
        физически ещё не существуют в новой модели.

        Требует явного {"confirm": true} в теле — это прямое DML по
        реальным производственным данным на контроллере, хотя сама
        функция переноса идемпотентна и безопасна для повторного вызова
        (см. её докстринг: уже перенесённые meters пропускаются, упавший
        на середине прогон восстанавливается, а не плодит дубли).

        Перед запуском ВСЕГДА снимается консистентный бэкап БД через
        Database.backup_to() (SQLite Online Backup API — корректно
        работает поверх WAL, в отличие от обычного копирования файла).
        Если сам бэкап не удался — миграция не запускается вообще."""
        data, _envelope = _unwrap_data(request.get_json(silent=True) or {})
        if data.get("confirm") is not True:
            body, status = _err(
                "bad_request",
                "требуется подтверждение: {\"confirm\": true} — перенос "
                "меняет данные на контроллере (бэкап делается автоматически, "
                "но подтверждение обязательно)",
                400, fields=["confirm"])
            return json_response(body, status)

        backup_dir = os.path.join(os.path.dirname(db.path) or ".", "migration-backups")
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup_path = os.path.join(backup_dir, f"pre-legacy-migration-{stamp}.db")
        try:
            db.backup_to(backup_path)
        except Exception as e:
            log.exception("legacy_migration: не удалось сделать бэкап БД перед переносом")
            body, status = _err(
                "server_error",
                f"не удалось сделать бэкап БД, перенос НЕ запущен: {e}",
                500)
            return json_response(body, status)

        try:
            report = migrate_meters_and_groups(db)
        except Exception as e:
            log.exception("legacy_migration: перенос meters/meter_groups упал")
            body, status = _err(
                "server_error",
                f"перенос упал: {e}. Функция идемпотентна — можно "
                f"безопасно повторить запрос после починки причины. "
                f"Бэкап БД на момент до попытки: {backup_path}",
                500)
            return json_response(body, status)

        return json_response({"backup_path": backup_path, **_dataclass_asdict(report)})

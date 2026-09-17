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

  НЕ РЕАЛИЗОВАНО (сознательно отложено — вне этой задачи):
  metrics/jobs (долгие расчёты/подписки); plans/items/layout (это стадия D
  плана — редактор плана v2); validation (сводка непривязанных/неразмещённых
  объектов); migration/status. Полный `expected_revision`/`configuration_revision`
  протокол конфликта версий (409 при устаревшей ревизии) тоже не реализован —
  данные читаются/пишутся без проверки ревизии; это тоже отдельная,
  бóльшая задача (интеграция с `configuration_revisions`).

Ответ на ошибку — единый envelope §9.2: {"code", "message", "fields",
"ids", "path"}. 400 — неверный запрос/тип; 404 — неизвестный ID; 409 —
конфликт (двойной счёт, пересечение интервалов, топология).
"""

from __future__ import annotations

import logging
import time

import os

from datetime import datetime, timezone

from flask import request, Response

from .accounting_contract import ContractViolation
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
from .legacy_migration import migrate_meters_and_groups
from .group_repo_v2 import GroupRepoV2
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
            p = repo.add(
                code=data.get("code"), name=data.get("name"),
                description=data.get("description"),
                installation_location_id=data.get("installation_location_id"),
                installation_note=data.get("installation_note"),
            )
        except ValueError as e:
            body, status = _err("bad_request", str(e), 400, fields=["code", "name"])
            return json_response(body, status)
        return json_response(_point_to_dict(p), 201)

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
        try:
            if "enabled" in data:
                p = repo.set_enabled(point_id, bool(data["enabled"]))
            if "archived" in data and data["archived"]:
                p = repo.archive(point_id)
            if any(k in data for k in ("name", "description", "installation_note")):
                p = repo.update_fields(
                    point_id, name=data.get("name"),
                    description=data.get("description"),
                    installation_note=data.get("installation_note"))
        except ValueError as e:
            body, status = _err("bad_request", str(e), 400)
            return json_response(body, status)
        return json_response(_point_to_dict(repo.get_by_id(point_id)))

    @app.route("/api/v2/points/<int:point_id>/bindings", methods=["GET"])
    def v2_point_bindings(point_id):
        repo = _point_repo()
        if repo.get_by_id(point_id) is None:
            body, status = _err("not_found", f"Точка {point_id} не найдена", 404, ids=[point_id])
            return json_response(body, status)
        bindings = _binding_repo().list_for_point(point_id)
        return json_response([_binding_to_dict(b) for b in bindings])

    @app.route("/api/v2/points/<int:point_id>/replace-meter", methods=["POST"])
    def v2_point_replace_meter(point_id):
        """§5.4/A18: атомарная замена прибора. Тело:
        {"meter_source_id": <id существующего meter_source>,
         "at": <unix ts, опционально>, "channel_profile": <опционально>,
         "replacement_note": <опционально>}. Провижининг НОВОГО
         meter/meter_source из адреса контроллера здесь не делается —
         вызывающий код создаёт источник заранее (см. модульный
         docstring, "не реализовано")."""
        body = request.get_json(silent=True) or {}
        data, _envelope = _unwrap_data(body)
        meter_source_id = data.get("meter_source_id")
        if meter_source_id is None:
            body, status = _err(
                "bad_request", "требуется meter_source_id существующего источника",
                400, fields=["meter_source_id"])
            return json_response(body, status)

        bindings = _binding_repo()
        try:
            new_binding = bindings.replace_meter(
                point_id, meter_source_id,
                at=data.get("at"),
                channel_profile=data.get("channel_profile"),
                replacement_note=data.get("replacement_note"),
            )
        except BindingConflict as e:
            body, status = _err(
                "double_counting", str(e), 409, ids=[point_id, meter_source_id])
            return json_response(body, status)
        except ValueError as e:
            body, status = _err("bad_request", str(e), 400)
            return json_response(body, status)
        return json_response(_binding_to_dict(new_binding), 200)

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
            l = repo.add(
                name=data.get("name"), kind=data.get("kind"),
                parent_id=data.get("parent_id"), code=data.get("code"),
                sort_order=data.get("sort_order", 0),
            )
        except ValueError as e:
            body, status = _err("bad_request", str(e), 400, fields=["name", "kind"])
            return json_response(body, status)
        return json_response(_location_to_dict(l), 201)

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
        try:
            if "parent_id" in data:
                l = repo.set_parent(location_id, data["parent_id"])
            if data.get("archived"):
                l = repo.archive(location_id)
        except ValueError as e:
            code = "cycle_conflict" if "цикл" in str(e).lower() else "bad_request"
            status = 409 if code == "cycle_conflict" else 400
            body, status2 = _err(code, str(e), status, ids=[location_id])
            return json_response(body, status2)
        return json_response(_location_to_dict(repo.get_by_id(location_id)))

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
            g = repo.add(
                name=data.get("name"), category=data.get("category"),
                parent_id=data.get("parent_id"), color=data.get("color"),
            )
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
        return json_response(_group_to_dict(g), 201)

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
        try:
            if "parent_id" in data:
                g = repo.set_parent(group_id, data["parent_id"])
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
        return json_response(_group_to_dict(repo.get_by_id(group_id)))

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
            m = repo.add_member(group_id, point_id, valid_from=data.get("valid_from"))
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
        return json_response(_membership_to_dict(m), 201)

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
        try:
            repo.remove_member(group_id, point_id, at=at)
        except ValueError as e:
            msg = str(e).lower()
            code = "not_found" if "не найд" in msg or "не состоит" in msg else "bad_request"
            status = 404 if code == "not_found" else 400
            body, status2 = _err(code, str(e), status, ids=[group_id, point_id])
            return json_response(body, status2)
        return ("", 204)

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
            n = repo.add(
                code=data.get("code"), name=data.get("name"),
                kind=data.get("kind"), location_id=data.get("location_id"),
            )
        except ValueError as e:
            body, status = _err("bad_request", str(e), 400, fields=["code", "name", "kind"])
            return json_response(body, status)
        return json_response(_node_to_dict(n), 201)

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
                n = repo.archive(node_id)
            except ValueError as e:
                body, status = _err("conflict", str(e), 409, ids=[node_id])
                return json_response(body, status)
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
            e = repo.add_draft(
                from_node_id=data.get("from_node_id"),
                to_node_id=data.get("to_node_id"),
                code=data.get("code"), name=data.get("name"),
                primary_point_id=data.get("primary_point_id"),
                phase_count=data.get("phase_count"),
                rated_current_a=data.get("rated_current_a"),
                cable_note=data.get("cable_note"),
            )
        except ValueError as e2:
            body, status = _err(
                "bad_request", str(e2), 400, fields=["from_node_id", "to_node_id"])
            return json_response(body, status)
        return json_response(_edge_to_dict(e), 201)

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
                e = repo.retire_edge(edge_id, at=data.get("at"))
            except ValueError as e2:
                body, status = _err("bad_request", str(e2), 400, ids=[edge_id])
                return json_response(body, status)
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
        revision, effective_from". draft_id здесь — edge_ids (список ID
        черновиков), проверка expected_configuration_revision не
        реализована (см. модульный docstring)."""
        data = request.get_json(silent=True) or {}
        edge_ids = data.get("edge_ids") or data.get("draft_id") or []
        if isinstance(edge_ids, int):
            edge_ids = [edge_ids]
        effective_from = data.get("effective_from")

        repo = _edge_repo()
        try:
            published = repo.publish_edges(edge_ids, at=effective_from)
        except TopologyConflict as e:
            violations = repo.validate_edges(edge_ids) if edge_ids else []
            body, status = _err(
                "topology_conflict", str(e), 409,
                path=_violations_to_json(violations))
            return json_response(body, status)
        except ValueError as e:
            body, status = _err("bad_request", str(e), 400)
            return json_response(body, status)
        return json_response({"edges": [_edge_to_dict(e) for e in published]}, 200)

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
        with db.read() as c:
            row = c.execute("SELECT * FROM balance_scopes WHERE id = ?", (scope_id,)).fetchone()
        return json_response(dict(row), 201)

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
        with db.transaction() as c:
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

        with db.read() as c:
            row2 = c.execute("SELECT * FROM balance_scopes WHERE id = ?", (scope_id,)).fetchone()
            members2 = c.execute(
                "SELECT * FROM balance_members WHERE scope_id = ? AND valid_to IS NULL",
                (scope_id,)).fetchall()
        d = dict(row2)
        d["input_point_ids"] = [m["point_id"] for m in members2 if m["side"] == "input"]
        d["output_point_ids"] = [m["point_id"] for m in members2 if m["side"] == "output"]
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

    @app.route("/api/v2/metrics/query", methods=["POST"])
    def v2_metrics_query():
        data = request.get_json(silent=True) or {}
        mode = data.get("mode")
        timezone = data.get("timezone", "UTC")

        try:
            ts_from = _parse_ts(data.get("from"), "from")
            ts_to = _parse_ts(data.get("to"), "to")
        except ValueError as e:
            body, status = _err("bad_request", str(e), 400, fields=["from", "to"])
            return json_response(body, status)

        if ts_to <= ts_from:
            body, status = _err("bad_request", "to должно быть позже from", 400, fields=["from", "to"])
            return json_response(body, status)

        binding_repo = _binding_repo()
        aggregates_repo = _aggregates_repo()
        source_repo = _source_repo()
        edge_repo = _edge_repo()

        try:
            if mode == "measured":
                point_ids = data.get("point_ids") or []
                if len(point_ids) != 1:
                    body, status = _err(
                        "bad_request", "measured требует ровно один point_ids", 400,
                        fields=["point_ids"])
                    return json_response(body, status)
                result = measured_point(binding_repo, aggregates_repo, source_repo,
                                         point_ids[0], ts_from, ts_to, timezone)
                return json_response(result.to_dict())

            elif mode == "sum":
                point_ids = data.get("point_ids") or []
                if not point_ids:
                    body, status = _err("bad_request", "sum требует point_ids", 400,
                                         fields=["point_ids"])
                    return json_response(body, status)
                result = sum_points(binding_repo, aggregates_repo, source_repo, edge_repo,
                                     point_ids, ts_from, ts_to, timezone)
                return json_response(result.to_dict())

            elif mode == "balance":
                scope_id = data.get("scope")
                if scope_id is None:
                    body, status = _err("bad_request", "balance требует scope (id границы)",
                                         400, fields=["scope"])
                    return json_response(body, status)
                result = balance(db, binding_repo, aggregates_repo, source_repo, edge_repo,
                                  scope_id, ts_from, ts_to, timezone)
                return json_response(result.to_dict())

            elif mode == "comparison":
                point_ids = data.get("point_ids") or []
                if not point_ids:
                    body, status = _err("bad_request", "comparison требует point_ids", 400,
                                         fields=["point_ids"])
                    return json_response(body, status)
                results = comparison(binding_repo, aggregates_repo, source_repo,
                                      point_ids, ts_from, ts_to, timezone)
                return json_response({str(pid): r.to_dict() for pid, r in results.items()})

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

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
  metrics/query (measured/sum/balance/comparison).

  НЕ РЕАЛИЗОВАНО (сознательно отложено — вне этой задачи): groups (нет
  версионируемого group_repo v2 — group_parent_bindings/group_memberships
  существуют в схеме, но CRUD-сервис для них ещё не написан); snapshot;
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

from flask import request

from .accounting_contract import ContractViolation
from .accounting_service import (
    AccountingConflict, measured_point, sum_points, balance, comparison,
)
from .binding_service import BindingConflict, PointBindingRepo
from .location_repo import LocationRepo
from .point_repo import MeteringPointRepo, MeterSourceRepo
from .topology_service import (
    ElectricalNodeRepo, ElectricalEdgeRepo, TopologyConflict,
)
from .aggregates_repo import AggregateRepo
from .periods import parse_user_datetime

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

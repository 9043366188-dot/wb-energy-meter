#!/usr/bin/env python3
"""Партия 12, этап 12.1: журнал изменений (`change_log`) и
`GET /api/v2/change-log` (docs/design-history.md, §1).

Что проверяется (каждая операция — это ТОЧНЫЕ строки журнала, а не «что-то
записалось»):

  1. таблица «операция -> строки журнала» по HTTP-операциям структуры:
     какие `entity_type` / `entity_id` / `action`, с какой ревизией, какое
     старое и новое значение, какая операция и происхождение;
  2. атомарность: исключение внутри транзакции, конфликт ревизии и сбой
     после части записей не оставляют в журнале ничего;
  3. шум не пишется: правка одного `updated_at`, создание+удаление в одной
     транзакции, чтение (GET);
  4. фильтры `/change-log`: entity_type, entity_id, action, from/to,
     limit (потолок 500), курсор before_id (полный обход без повторов),
     related=1 (подчинённые сущности), ошибки 400;
  5. пара «пусто / есть данные»: на свежей БД журнал пуст и отдаёт метку
     начала; после правки — непуст;
  6. метка начала журнала ставится один раз и переживает переоткрытие БД;
     записи, сделанные в обход (до включения журнала), в журнал не попадают;
  7. раскладка плана — одна запись `plan_layout` на сохранение, со счётчиками.

Запуск: python tests/test_step54_change_log.py
"""

import io
import json
import os
import sqlite3
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from wb_energy_meter import change_journal  # noqa: E402
from wb_energy_meter.db import Database  # noqa: E402

from test_step21_plan_v2 import make_client, make_png  # noqa: E402


# ---------------------------------------------------------------- помощники

def rev(client):
    return client.get("/api/v2/revision").get_json()["configuration_revision"]


def post(client, url, **body):
    body.setdefault("expected_revision", rev(client))
    r = client.post(url, json=body)
    assert r.status_code in (200, 201), (url, r.status_code, r.get_json())
    return r.get_json()


def patch(client, url, **body):
    body.setdefault("expected_revision", rev(client))
    r = client.patch(url, json=body)
    assert r.status_code in (200, 201), (url, r.status_code, r.get_json())
    return r.get_json()


def log_all(client, **params):
    """Все записи журнала по возрастанию id (обход курсором)."""
    out, before = [], None
    while True:
        q = dict(params, limit=500)
        if before is not None:
            q["before_id"] = before
        qs = "&".join(f"{k}={v}" for k, v in q.items())
        data = client.get(f"/api/v2/change-log?{qs}").get_json()
        out.extend(data["items"])
        before = data["next_before_id"]
        if before is None:
            break
    return list(reversed(out))


class Journal:
    """Курсор «что появилось с прошлого раза»."""

    def __init__(self, client):
        self.client = client
        self.last = 0

    def new(self):
        rows = [r for r in log_all(self.client) if r["id"] > self.last]
        if rows:
            self.last = rows[-1]["id"]
        return rows

    @staticmethod
    def shape(rows):
        return [(r["entity_type"], r["entity_id"], r["action"]) for r in rows]


# ------------------------------------------------------------ 1. операции

def test_rows_per_operation():
    client, db, path, plans_dir = make_client()
    try:
        j = Journal(client)
        assert j.new() == [], "на свежей БД журнал пуст"

        # -- место
        loc = post(client, "/api/v2/locations", name="Цех", kind="room")
        rows = j.new()
        assert j.shape(rows) == [("location", loc["id"], "create"),
                                 ("location_parent", 1, "create")], rows
        assert rows[0]["old_value"] is None
        assert rows[0]["new_value"]["name"] == "Цех"
        assert {r["revision_id"] for r in rows} == {rev(client)}, \
            "обе строки операции несут ревизию этой операции"
        assert rows[0]["operation"] == "POST /api/v2/locations"
        assert rows[0]["origin"] == "web"
        assert rows[0]["client"] == "127.0.0.1"
        assert rows[0]["effective_from"] == rows[0]["new_value"]["created_at"]

        # -- точка
        p1 = post(client, "/api/v2/points", code="p1", name="П1",
                  installation_location_id=loc["id"])
        rows = j.new()
        assert j.shape(rows) == [("metering_point", p1["id"], "create"),
                                 ("point_state", 1, "create")], rows
        assert rows[0]["new_value"]["code"] == "p1"
        assert rows[1]["new_value"]["enabled"] == 1
        rev_point = rev(client)
        assert {r["revision_id"] for r in rows} == {rev_point}

        # -- правка поля точки: старое и новое только изменённого поля
        patch(client, f"/api/v2/points/{p1['id']}", name="П1 новая")
        rows = j.new()
        assert j.shape(rows) == [("metering_point", p1["id"], "update")], rows
        assert rows[0]["old_value"] == {"name": "П1"}, rows[0]
        assert rows[0]["new_value"] == {"name": "П1 новая"}, rows[0]
        assert rows[0]["revision_id"] == rev(client) and rows[0]["revision_id"] > rev_point
        assert rows[0]["operation"] == "PATCH /api/v2/points/<int:point_id>"

        # -- привязка к прибору
        post(client, f"/api/v2/points/{p1['id']}/bindings",
             new_meter={"controller_key": "c", "device_id": "d1",
                        "display_name": "M1"},
             channel_profile="total_3p", valid_from=100)
        rows = j.new()
        assert sorted(j.shape(rows)) == sorted([
            ("point_binding", 1, "create"), ("meter_source", 1, "create")]), rows
        by_type = {r["entity_type"]: r for r in rows}
        assert by_type["point_binding"]["new_value"]["point_id"] == p1["id"]
        assert by_type["meter_source"]["new_value"]["device_id"] == "d1"
        assert by_type["meter_source"]["effective_from"] == 100

        # -- замена прибора: закрытие старой привязки (close) + новая
        post(client, f"/api/v2/points/{p1['id']}/replace-meter",
             new_meter={"controller_key": "c", "device_id": "d2",
                        "display_name": "M2"}, at=500, note="замена")
        rows = j.new()
        assert sorted(j.shape(rows)) == sorted([
            ("point_binding", 1, "close"), ("point_binding", 2, "create"),
            ("meter_source", 2, "create")]), rows
        close = [r for r in rows if r["action"] == "close"][0]
        assert close["old_value"] == {"valid_to": None}
        assert close["new_value"] == {"valid_to": 500}
        assert close["effective_from"] == 500, "close вступает в силу в valid_to"

        # -- группа и состав
        g = post(client, "/api/v2/groups", name="Группа", category="tenant")
        rows = j.new()
        assert j.shape(rows) == [("group", g["id"], "create"),
                                 ("group_parent", 1, "create")], rows
        post(client, f"/api/v2/groups/{g['id']}/members",
             point_id=p1["id"], valid_from=0)
        rows = j.new()
        assert j.shape(rows) == [("group_member", 1, "create")], rows
        r = client.delete(f"/api/v2/groups/{g['id']}/members/{p1['id']}"
                          f"?expected_revision={rev(client)}")
        assert r.status_code == 204, r.status_code
        rows = j.new()
        assert j.shape(rows) == [("group_member", 1, "close")], rows
        assert rows[0]["old_value"] == {"valid_to": None}
        assert rows[0]["new_value"]["valid_to"] is not None

        # -- узлы и связи
        n_src = post(client, "/api/v2/topology/nodes", code="SRC",
                     name="Ввод", kind="source")
        n_panel = post(client, "/api/v2/topology/nodes", code="PNL",
                       name="Щит", kind="panel")
        rows = j.new()
        assert j.shape(rows) == [("electrical_node", n_src["id"], "create"),
                                 ("electrical_node", n_panel["id"], "create")], rows
        assert rows[0]["revision_id"] != rows[1]["revision_id"], \
            "два узла — две операции — две ревизии"

        edge = post(client, "/api/v2/topology/edges",
                    from_node_id=n_src["id"], to_node_id=n_panel["id"],
                    name="Линия", primary_point_id=p1["id"])
        rows = j.new()
        assert j.shape(rows) == [("electrical_edge", edge["id"], "create")], rows
        assert rows[0]["new_value"]["state"] == "draft"
        assert rows[0]["new_value"]["primary_point_id"] == p1["id"]

        # публикация: draft -> published, действует с valid_from
        r = client.post("/api/v2/topology/publish", json={
            "edge_ids": [edge["id"]],
            "expected_configuration_revision": rev(client)})
        assert r.status_code == 200, r.get_json()
        rows = j.new()
        assert j.shape(rows) == [("electrical_edge", edge["id"], "publish")], rows
        pub = rows[0]
        assert pub["old_value"]["state"] == "draft"
        assert pub["new_value"]["state"] == "published"
        assert pub["old_value"]["valid_from"] is None
        assert pub["new_value"]["valid_from"] == pub["effective_from"] is not None
        assert pub["old_value"]["primary_point_id"] == p1["id"], \
            "ключевые поля связи всегда в обоих значениях"
        assert pub["revision_id"] == rev(client)

        # смена вида узла «на месте» — ровно то, что восстановит «как было»
        patch(client, f"/api/v2/topology/nodes/{n_panel['id']}", kind="load")
        rows = j.new()
        assert j.shape(rows) == [("electrical_node", n_panel["id"], "update")], rows
        assert rows[0]["old_value"] == {"kind": "panel"}
        assert rows[0]["new_value"] == {"kind": "load"}

        # смена измерителя линии «на месте»
        p2 = post(client, "/api/v2/points", code="p2", name="П2")
        j.new()
        patch(client, f"/api/v2/topology/edges/{edge['id']}",
              primary_point_id=p2["id"])
        rows = j.new()
        assert j.shape(rows) == [("electrical_edge", edge["id"], "update")], rows
        assert rows[0]["old_value"]["primary_point_id"] == p1["id"]
        assert rows[0]["new_value"]["primary_point_id"] == p2["id"]
        assert rows[0]["old_value"]["state"] == "published"

        # архивирование точки: archive + закрытие состояния + новое состояние
        patch(client, f"/api/v2/points/{p1['id']}", archived=True)
        rows = j.new()
        assert sorted(j.shape(rows)) == sorted([
            ("metering_point", p1["id"], "archive"),
            ("point_state", 1, "close"), ("point_state", 3, "create")]), rows
        arch = [r for r in rows if r["entity_type"] == "metering_point"][0]
        assert arch["old_value"] == {"archived_at": None, "enabled": 1}
        assert arch["new_value"]["enabled"] == 0
        assert arch["new_value"]["archived_at"] is not None

        # снятие линии: close, valid_to
        patch(client, f"/api/v2/topology/edges/{edge['id']}", retire=True)
        rows = j.new()
        assert j.shape(rows) == [("electrical_edge", edge["id"], "close")], rows
        assert rows[0]["old_value"]["valid_to"] is None
        assert rows[0]["new_value"]["valid_to"] == rows[0]["effective_from"]

        # подключение связи и потребителя
        e2 = post(client, "/api/v2/topology/edges/connect",
                  from_node_id=n_src["id"], to_node_id=n_panel["id"], name="Л2")
        rows = j.new()
        assert j.shape(rows) == [("electrical_edge", e2["id"], "create")], rows
        assert rows[0]["operation"] == "POST /api/v2/topology/edges/connect"
        res = post(client, f"/api/v2/topology/nodes/{n_src['id']}/add-consumer",
                   name="Станок")
        rows = j.new()
        assert j.shape(rows) == [
            ("electrical_node", res["node"]["id"], "create"),
            ("electrical_edge", res["edge"]["id"], "create")], rows
        assert len({r["revision_id"] for r in rows}) == 1

        # граница баланса: прямой create_revision (не with_revision_check)
        sc = post(client, "/api/v2/balance-scopes", name="Б",
                  input_point_ids=[p2["id"]], output_point_ids=[])
        rows = j.new()
        assert j.shape(rows) == [("balance_scope", sc["id"], "create"),
                                 ("balance_member", 1, "create")], rows
        assert {r["revision_id"] for r in rows} == {rev(client)}
        patch(client, f"/api/v2/balance-scopes/{sc['id']}",
              input_point_ids=[], output_point_ids=[p2["id"]])
        rows = j.new()
        assert sorted(j.shape(rows)) == sorted([
            ("balance_member", 1, "close"), ("balance_member", 2, "create")]), rows

        # старый интерфейс групп (мимо протокола ревизий): запись есть,
        # ревизии нет, происхождение — legacy-api
        r = client.post("/api/registry/groups", json={"name": "Старая"})
        assert r.status_code in (200, 201), (r.status_code, r.get_json())
        rows = j.new()
        assert [(x["entity_type"], x["action"]) for x in rows] == \
            [("group", "create")], rows
        assert rows[0]["revision_id"] is None
        assert rows[0]["origin"] == "legacy-api"
        assert rows[0]["operation"] == "POST /api/registry/groups"

        # чтение не пишет в журнал
        for url in ("/api/v2/points", "/api/v2/topology/nodes",
                    "/api/v2/topology/edges", "/api/v2/groups",
                    "/api/v2/revision", "/api/v2/change-log"):
            assert client.get(url).status_code == 200, url
        assert j.new() == [], "GET ничего не пишет в журнал"
        print("[OK] 1. операция -> точные строки журнала, ревизия, "
              "старое/новое значение, операция и происхождение")
    finally:
        db.close(); os.unlink(path)


# ----------------------------------------------------------- 2. атомарность

def test_atomicity_and_noise():
    client, db, path, plans_dir = make_client()
    try:
        j = Journal(client)
        loc = post(client, "/api/v2/locations", name="Цех", kind="room")
        j.new()
        p = post(client, "/api/v2/points", code="p1", name="П1")
        j.new()
        rev_before = rev(client)

        # исключение внутри транзакции после записей: журнал не меняется
        class Boom(Exception):
            pass
        try:
            with db.transaction() as c:
                c.execute("UPDATE metering_points SET name = 'X' WHERE id = ?",
                          (p["id"],))
                c.execute("INSERT INTO locations (name, name_norm, kind, "
                          "created_at, updated_at) VALUES ('Новое','новое',"
                          "'room',1,1)")
                raise Boom()
        except Boom:
            pass
        assert j.new() == [], "откат транзакции откатывает и журнал"
        with db.read() as c:
            assert c.execute("SELECT name FROM metering_points WHERE id = ?",
                             (p["id"],)).fetchone()["name"] == "П1"
        # накопленное в теневых таблицах после отката не «протекает»
        # в следующую удачную транзакцию
        patch(client, f"/api/v2/points/{p['id']}", description="d")
        rows = j.new()
        assert [(r["entity_type"], r["action"]) for r in rows] == \
            [("metering_point", "update")], rows
        assert rows[0]["new_value"] == {"description": "d"}, \
            "в журнале только эта правка, без остатков откатанной"

        # конфликт ревизии: ничего не меняется и не пишется
        r = client.patch(f"/api/v2/points/{p['id']}",
                         json={"name": "Z", "expected_revision": 0})
        assert r.status_code == 409, r.status_code
        assert j.new() == []
        assert rev(client) == rev_before + 1

        # изменение одного updated_at — не событие
        with db.transaction() as c:
            c.execute("UPDATE metering_points SET updated_at = updated_at + 5 "
                      "WHERE id = ?", (p["id"],))
        assert j.new() == [], "правка одного updated_at не пишется"

        # правка на то же значение — не событие
        with db.transaction() as c:
            c.execute("UPDATE metering_points SET name = name WHERE id = ?",
                      (p["id"],))
        assert j.new() == []

        # создание и удаление в одной транзакции — след не оставляют
        with db.transaction() as c:
            cur = c.execute("INSERT INTO locations (name, name_norm, kind, "
                            "created_at, updated_at) VALUES "
                            "('Врем','врем','room',1,1)")
            c.execute("DELETE FROM locations WHERE id = ?", (cur.lastrowid,))
        assert j.new() == [], "create+delete в одной транзакции пропущены"

        # несколько правок одной строки в одной транзакции -> одна запись
        with db.transaction() as c:
            c.execute("UPDATE metering_points SET name = 'A' WHERE id = ?",
                      (p["id"],))
            c.execute("UPDATE metering_points SET name = 'B' WHERE id = ?",
                      (p["id"],))
        rows = j.new()
        assert len(rows) == 1, rows
        assert rows[0]["old_value"] == {"name": "П1"} and \
            rows[0]["new_value"] == {"name": "B"}, rows[0]

        # удаление строки: полная старая строка
        with db.transaction() as c:
            cur = c.execute("INSERT INTO locations (name, name_norm, kind, "
                            "created_at, updated_at) VALUES "
                            "('Удал','удал','room',1,1)")
        j.new()
        with db.transaction() as c:
            c.execute("DELETE FROM locations WHERE name = 'Удал'")
        rows = j.new()
        assert [(r["entity_type"], r["action"]) for r in rows] == \
            [("location", "delete")], rows
        assert rows[0]["old_value"]["name"] == "Удал"
        assert rows[0]["new_value"] is None
        assert rows[0]["revision_id"] is None, "правка без ревизии — NULL"

        # сбой самого переноса в журнал откатывает правку целиком
        orig = change_journal.ChangeCapture.drain

        def broken(self, conn):
            raise sqlite3.OperationalError("диск полон")
        change_journal.ChangeCapture.drain = broken
        try:
            try:
                with db.transaction() as c:
                    c.execute("UPDATE metering_points SET name = 'Q' "
                              "WHERE id = ?", (p["id"],))
            except sqlite3.OperationalError:
                pass
            else:
                raise AssertionError("ожидали исключение переноса журнала")
        finally:
            change_journal.ChangeCapture.drain = orig
        with db.read() as c:
            assert c.execute("SELECT name FROM metering_points WHERE id = ?",
                             (p["id"],)).fetchone()["name"] == "B", \
                "если журнал не записался — правка не фиксируется"
        assert j.new() == []
        print("[OK] 2. атомарность: откат, конфликт ревизии, сбой журнала; "
              "шум (updated_at, no-op, create+delete) не пишется")
    finally:
        db.close(); os.unlink(path)


# ---------------------------------------------------------------- 3. фильтры

def test_filters_pagination_related():
    client, db, path, plans_dir = make_client()
    # управляемые часы: recorded_at растёт на 10 секунд за операцию
    clock = {"t": 1_000_000}
    orig_clock = change_journal._clock
    change_journal._clock = lambda: clock["t"]
    try:
        # пара «пусто / есть данные»
        empty = client.get("/api/v2/change-log").get_json()
        assert empty["items"] == [] and empty["next_before_id"] is None
        assert isinstance(empty["journal_started_at"], int)
        assert empty["limit"] == 100

        loc = post(client, "/api/v2/locations", name="Цех", kind="room")
        clock["t"] += 10
        p1 = post(client, "/api/v2/points", code="p1", name="П1",
                  installation_location_id=loc["id"])
        clock["t"] += 10
        post(client, f"/api/v2/points/{p1['id']}/bindings",
             new_meter={"controller_key": "c", "device_id": "d1",
                        "display_name": "M1"},
             channel_profile="total_3p", valid_from=100)
        clock["t"] += 10
        g = post(client, "/api/v2/groups", name="Группа", category="tenant")
        clock["t"] += 10
        post(client, f"/api/v2/groups/{g['id']}/members", point_id=p1["id"],
             valid_from=0)
        clock["t"] += 10
        n1 = post(client, "/api/v2/topology/nodes", code="A", name="A",
                  kind="source")
        n2 = post(client, "/api/v2/topology/nodes", code="B", name="B",
                  kind="panel")
        clock["t"] += 10
        e = post(client, "/api/v2/topology/edges", from_node_id=n1["id"],
                 to_node_id=n2["id"], name="Л", primary_point_id=p1["id"])
        clock["t"] += 10
        for i in range(3):                # набор правок для постраничного обхода
            patch(client, f"/api/v2/points/{p1['id']}", name=f"П1-{i}")
            clock["t"] += 10

        full = log_all(client)
        assert len(full) > 10
        ids = [r["id"] for r in full]
        assert ids == sorted(ids) and len(set(ids)) == len(ids)

        # entity_type / entity_id / action
        only = log_all(client, entity_type="metering_point",
                       entity_id=p1["id"])
        assert [r["action"] for r in only] == \
            ["create", "update", "update", "update"], only
        assert log_all(client, action="create", entity_type="electrical_node")
        assert {r["action"] for r in log_all(client, action="update")} == {"update"}
        assert log_all(client, entity_type="electrical_node",
                       entity_id=9999) == []

        # from / to — по времени записи, [from, to)
        t_edge = [r for r in full if r["entity_type"] == "electrical_edge"][0]
        win = log_all(client, **{"from": t_edge["recorded_at"],
                                 "to": t_edge["recorded_at"] + 1})
        assert win and {r["recorded_at"] for r in win} == {t_edge["recorded_at"]}
        assert all(r["recorded_at"] < t_edge["recorded_at"]
                   for r in log_all(client, **{"to": t_edge["recorded_at"]}))
        assert all(r["recorded_at"] >= t_edge["recorded_at"]
                   for r in log_all(client, **{"from": t_edge["recorded_at"]}))

        # limit и потолок 500
        two = client.get("/api/v2/change-log?limit=2").get_json()
        assert len(two["items"]) == 2 and two["next_before_id"] == two["items"][-1]["id"]
        assert two["items"][0]["id"] > two["items"][1]["id"], "новые сверху"
        huge = client.get("/api/v2/change-log?limit=100000").get_json()
        assert huge["limit"] == 500, "потолок limit"

        # курсор: полный обход страницами по 3 без повторов и пропусков
        seen, before, pages = [], None, 0
        while True:
            url = "/api/v2/change-log?limit=3" + \
                (f"&before_id={before}" if before is not None else "")
            d = client.get(url).get_json()
            seen.extend(r["id"] for r in d["items"])
            pages += 1
            before = d["next_before_id"]
            if before is None:
                break
        assert sorted(seen) == ids and len(seen) == len(set(seen)), (seen, ids)
        assert pages == -(-len(ids) // 3)

        # related=1: точка + её привязки, членство в группе, линии
        rel = log_all(client, entity_type="metering_point",
                      entity_id=p1["id"], related=1)
        kinds = {r["entity_type"] for r in rel}
        assert {"metering_point", "point_binding", "point_state",
                "group_member", "electrical_edge"} <= kinds, kinds
        assert all(r["entity_type"] != "electrical_node" for r in rel)
        plain = log_all(client, entity_type="metering_point", entity_id=p1["id"])
        assert len(rel) > len(plain)
        # чужие записи не подмешиваются
        p2 = post(client, "/api/v2/points", code="p2", name="П2")
        rel2 = log_all(client, entity_type="metering_point",
                       entity_id=p2["id"], related=1)
        assert {r["entity_type"] for r in rel2} == {"metering_point", "point_state"}
        # related для узла: его линии
        reln = log_all(client, entity_type="electrical_node",
                       entity_id=n1["id"], related=1)
        assert {"electrical_node", "electrical_edge"} <= \
            {r["entity_type"] for r in reln}

        # ошибки параметров
        for bad in ("entity_type=нет_такого", "limit=0", "limit=abc",
                    "entity_id=x", "before_id=x", "from=вчера"):
            r = client.get(f"/api/v2/change-log?{bad}")
            assert r.status_code == 400, (bad, r.status_code, r.get_json())
        # маршрут только на чтение
        for method in ("post", "patch", "delete", "put"):
            r = getattr(client, method)("/api/v2/change-log", json={})
            assert r.status_code == 405, (method, r.status_code)
        print("[OK] 3. фильтры, from/to, limit<=500, курсор без повторов, "
              "related=1, ошибки 400, только чтение")
    finally:
        change_journal._clock = orig_clock
        db.close(); os.unlink(path)


# ----------------------------------------------- 4. метка начала и обход

def test_started_marker_and_bypass_writes():
    fd, path = tempfile.mkstemp(suffix=".sqlite3")
    os.close(fd); os.unlink(path)
    try:
        orig_clock = change_journal._clock
        change_journal._clock = lambda: 1_700_000_000
        try:
            db = Database(path=path)
            db.open()
        finally:
            change_journal._clock = orig_clock
        with db.read() as c:
            t0 = change_journal.started_at(c)
        assert t0 == 1_700_000_000, t0
        db.close()

        # запись «в обход» — сырым соединением без триггеров журнала
        raw = sqlite3.connect(path)
        raw.execute("INSERT INTO locations (name, name_norm, kind, created_at, "
                    "updated_at) VALUES ('Обход','обход','room',1,1)")
        raw.commit(); raw.close()

        db = Database(path=path)
        db.open()                                  # переоткрытие
        with db.read() as c:
            assert change_journal.started_at(c) == t0, \
                "метка начала не перезаписывается при переоткрытии"
            assert c.execute("SELECT COUNT(*) AS n FROM change_log"
                             ).fetchone()["n"] == 0, \
                "до включения журнала/в обход он ничего не знает"
        with db.transaction() as c:
            c.execute("UPDATE locations SET name = 'Обход2' WHERE name = 'Обход'")
        with db.read() as c:
            rows = c.execute("SELECT entity_type, action FROM change_log"
                             ).fetchall()
        assert [(r["entity_type"], r["action"]) for r in rows] == \
            [("location", "update")]
        db.close()
        print("[OK] 4. метка начала журнала ставится один раз; записи в обход "
              "в журнал не попадают")
    finally:
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(path + suffix):
                os.unlink(path + suffix)


# -------------------------------------------------------- 5. раскладка плана

def test_plan_layout_is_one_record():
    client, db, path, plans_dir = make_client()
    try:
        j = Journal(client)
        p1 = post(client, "/api/v2/points", code="p1", name="П1")
        j.new()
        r = client.post("/api/v2/plans", data={
            "name": "Этаж", "plan_kind": "floor",
            "file": (io.BytesIO(make_png(800, 600)), "bg.png")},
            content_type="multipart/form-data")
        assert r.status_code == 201, r.get_json()
        plan = r.get_json()
        rows = j.new()
        assert rows[0]["entity_type"] == "plan" and rows[0]["action"] == "create"
        assert rows[0]["revision_id"] is None

        r = client.post(f"/api/v2/plans/{plan['id']}/layout", json={"data": {
            "expected_revision": plan["canvas_revision"],
            "item_ops": [
                {"op": "upsert", "kind": "point", "point_id": p1["id"],
                 "geometry": {"x": 10, "y": 10}},
                {"op": "upsert", "kind": "point", "point_id": p1["id"],
                 "geometry": {"x": 30, "y": 30}}],
            "edge_view_ops": []}})
        assert r.status_code == 200, r.get_json()
        rows = j.new()
        assert [(x["entity_type"], x["entity_id"], x["action"]) for x in rows] \
            == [("plan_layout", plan["id"], "update")], rows
        assert rows[0]["old_value"] == {"canvas_revision": plan["canvas_revision"]}
        assert rows[0]["new_value"] == {
            "canvas_revision": plan["canvas_revision"] + 1,
            "items": {"added": 2, "updated": 0, "removed": 0},
            "edge_views": {"added": 0, "updated": 0, "removed": 0}}, rows[0]
        assert rows[0]["operation"] == "POST /api/v2/plans/<int:plan_id>/layout"

        # конфликт канвы: журнал не пишется
        r = client.post(f"/api/v2/plans/{plan['id']}/layout", json={"data": {
            "expected_revision": plan["canvas_revision"],
            "item_ops": [], "edge_view_ops": []}})
        assert r.status_code == 409, r.status_code
        assert j.new() == []
        # одиночные правки элемента плана — тоже запись plan_layout
        r = client.post(f"/api/v2/plans/{plan['id']}/items", json={
            "kind": "point", "point_id": p1["id"],
            "geometry": {"x": 50, "y": 50},
            "coord_space": "image_px_xy_v2"})
        assert r.status_code == 201, r.get_json()
        item_id = r.get_json()["id"]
        rows = j.new()
        assert [(x["entity_type"], x["action"]) for x in rows] == \
            [("plan_layout", "update")], rows
        assert rows[0]["new_value"]["items"] == \
            {"added": 1, "updated": 0, "removed": 0}, rows[0]
        r = client.patch(f"/api/v2/plans/{plan['id']}/items/{item_id}",
                         json={"geometry": {"x": 60, "y": 60}})
        assert r.status_code == 200, r.get_json()
        assert j.shape(j.new()) == [("plan_layout", plan["id"], "update")]
        r = client.delete(f"/api/v2/plans/{plan['id']}/items/{item_id}")
        assert r.status_code in (200, 204), r.status_code
        rows = j.new()
        assert rows and rows[0]["new_value"]["items"]["removed"] == 1, rows
        # related для плана подтягивает его сохранения
        rel = log_all(client, entity_type="plan", entity_id=plan["id"],
                      related=1)
        assert "plan_layout" in {x["entity_type"] for x in rel}
        print("[OK] 5. раскладка плана — одна запись plan_layout со счётчиками")
    finally:
        db.close(); os.unlink(path)


if __name__ == "__main__":
    test_rows_per_operation()
    test_atomicity_and_noise()
    test_filters_pagination_related()
    test_started_marker_and_bypass_writes()
    test_plan_layout_is_one_record()
    print("\nЖурнал изменений (Шаг 54) — все проверки пройдены.")

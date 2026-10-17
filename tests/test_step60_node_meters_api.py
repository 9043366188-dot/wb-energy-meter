"""Шаг 60 (партия 13, находки пилота П1/П4/П5): серверная часть
«+ Счётчик» в карточке узла и архивации/вывода из работы.

П5 — `POST /api/v2/topology/nodes/<id>/meters` (решение R10): прибор →
точка → привязка → «измеряет эту линию», при необходимости — линия и новая
нагрузка, ОДНОЙ ревизией. Проверяются все варианты из `docs/pilot-findings.md`
(ввод щита с входящей линией и без неё, отходящая к существующей линии, к
существующему узлу и к новой нагрузке, ввод, нагрузка), отказ на занятом
приборе (409 и ни одной новой записи: точек, привязок, приборов, узлов,
линий, ревизий), перечитывание сущностей.

П4 — отказ привязки занятого прибора по ВСЕМ маршрутам — человеческим
текстом (без `scope`, `channel_profile`, `valid_from`, номеров записей);
`structure/points` отдаёт `occupied_devices` для списков выбора прибора.

П1 — серверные действия «Убрать счётчик» (архивация точки освобождает
прибор и снимает точку с линии), «Архивировать узел» (отказ называет
линии; `retire_edges` — вместе с линиями одной ревизией; значки узла
уходят с планов), «Вывести линию из работы» (изображение линии уходит с
планов), «Убрать с плана» (`?edge_views=remove`), «Удалить план» (узлы,
линии, точки остаются, A27).

Каждый «отказ» — в паре с «легитимный запрос к тому же ресурсу работает» и
перечитыванием сущности (CODING_STANDARDS.md, «API и проверки поведения»).

Самостоятельный скрипт (не pytest):
    python tests/test_step60_node_meters_api.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from wb_energy_meter.db import Database
from wb_energy_meter.repo import GroupRepo, MeterRepo
from wb_energy_meter.api import create_app, _AppState
from wb_energy_meter.model import MeterRegistry

TECH_WORDS = ("scope", "channel_profile", "valid_from", "meter_source_id",
              "point_id=", "binding", "role=")


def make_client():
    fd, path = tempfile.mkstemp(suffix=".sqlite3")
    os.close(fd)
    os.unlink(path)
    db = Database(path=path)
    db.open()
    groups_repo = GroupRepo(db)
    meters_repo = MeterRepo(db, groups_repo)
    state = _AppState(
        registry=MeterRegistry(), meters_repo=meters_repo, groups_repo=groups_repo,
        is_mqtt_connected=lambda: False, mqtt_message_count=lambda: 0,
        mqtt_error_count=lambda: 0, wb_db_client=None,
        consumption_service=None, started_at=time.time(), db=db,
        plans_dir=tempfile.mkdtemp(),
    )
    app = create_app(state)
    return app.test_client(), db, path


def rev(client):
    return client.get("/api/v2/revision").get_json()["configuration_revision"]


def counts(db):
    """Всё, что операция «+ Счётчик» может создать, и число ревизий."""
    out = {}
    with db.read() as c:
        for t in ("metering_points", "point_bindings", "meters", "meter_sources",
                  "electrical_nodes", "electrical_edges", "configuration_revisions"):
            out[t] = c.execute("SELECT COUNT(*) AS n FROM %s" % t).fetchone()["n"]
    return out


def make_node(client, name, kind):
    r = client.post("/api/v2/topology/nodes", json={
        "code": "n-%s-%d" % (kind, time.time_ns()), "name": name, "kind": kind,
        "expected_revision": rev(client)})
    assert r.status_code == 201, r.get_json()
    return r.get_json()


def connect(client, from_id, to_id):
    r = client.post("/api/v2/topology/edges/connect", json={
        "from_node_id": from_id, "to_node_id": to_id, "expected_revision": rev(client)})
    assert r.status_code == 201, r.get_json()
    return r.get_json()


def add_meter(client, node_id, **body):
    payload = {"expected_revision": rev(client)}
    payload.update(body)
    return client.post("/api/v2/topology/nodes/%d/meters" % node_id, json=payload)


def get_edge(client, edge_id):
    r = client.get("/api/v2/topology/edges/%d" % edge_id)
    assert r.status_code == 200, r.get_json()
    return r.get_json()


def get_point(client, point_id):
    r = client.get("/api/v2/points/%d" % point_id)
    assert r.status_code == 200, r.get_json()
    return r.get_json()


def bindings_of(client, point_id):
    r = client.get("/api/v2/points/%d/bindings" % point_id)
    assert r.status_code == 200, r.get_json()
    return r.get_json()


def published_edges(client):
    r = client.get("/api/v2/topology/edges?state=published")
    assert r.status_code == 200
    return r.get_json()


def node_by_id(client, node_id):
    r = client.get("/api/v2/topology/nodes/%d" % node_id)
    assert r.status_code == 200, r.get_json()
    return r.get_json()


def no_tech(message):
    low = message.lower()
    bad = [w for w in TECH_WORDS if w in low]
    assert not bad, "в тексте отказа служебные слова %s: %s" % (bad, message)


def open_primary_device(client, point_id):
    """device_id прибора открытой основной привязки точки (через
    structure/points — тот же источник, что и у интерфейса)."""
    rows = client.get("/api/v2/structure/points").get_json()["points"]
    for p in rows:
        if p["point_id"] == point_id:
            return p["meter_device_id"]
    return None


def scheme(client):
    """Источник → панель → нагрузка; возвращает (source, panel, load, e_in, e_out)."""
    src = make_node(client, "Ввод", "source")
    panel = make_node(client, "ЩР-1", "panel")
    load = make_node(client, "Станок", "load")
    e_in = connect(client, src["id"], panel["id"])
    e_out = connect(client, panel["id"], load["id"])
    return src, panel, load, e_in, e_out


# ---------------------------------------------------------------------
# П5: варианты «+ Счётчик»
# ---------------------------------------------------------------------

def test_panel_input_existing_incoming_line():
    client, db, path = make_client()
    try:
        src, panel, load, e_in, e_out = scheme(client)
        before = counts(db)
        rev_before = rev(client)
        r = add_meter(client, panel["id"], device_id="wb-map3e_16", measures="input")
        assert r.status_code == 201, r.get_json()
        d = r.get_json()
        assert d["created_edge"] is False and d["created_node"] is False, d
        assert d["edge"]["id"] == e_in["id"], d
        assert d["configuration_revision"] == rev_before + 1, d
        # Имя по умолчанию — имя (адрес) прибора.
        assert d["point"]["name"] == "wb-map3e_16", d["point"]
        after = counts(db)
        assert after["metering_points"] == before["metering_points"] + 1
        assert after["point_bindings"] == before["point_bindings"] + 1
        assert after["electrical_edges"] == before["electrical_edges"], "лишняя линия"
        assert after["electrical_nodes"] == before["electrical_nodes"], "лишний узел"
        assert after["configuration_revisions"] == before["configuration_revisions"] + 1, \
            "ровно одна ревизия"
        # Перечитываем: линия измеряется новой точкой, у точки открыта привязка.
        assert get_edge(client, e_in["id"])["primary_point_id"] == d["point"]["id"]
        assert get_edge(client, e_out["id"])["primary_point_id"] is None
        b = bindings_of(client, d["point"]["id"])
        assert len(b) == 1 and b[0]["valid_to"] is None and b[0]["role"] == "primary", b
        assert open_primary_device(client, d["point"]["id"]) == "wb-map3e_16"
        print("[OK] П5: щит, ввод щита с существующей входящей линией — точка и "
              "привязка созданы одной ревизией, линия измеряется новой точкой")
    finally:
        db.close(); os.unlink(path)


def test_panel_input_without_incoming_line_creates_line_from_feeder():
    client, db, path = make_client()
    try:
        src = make_node(client, "Ввод", "source")
        panel = make_node(client, "ЩР-1", "panel")
        before = counts(db)

        # Отказ: у щита нет входящей линии, откуда питается — не сказано.
        r = add_meter(client, panel["id"], device_id="wb-map3e_1", measures="input")
        assert r.status_code == 400, r.get_json()
        assert "откуда" in r.get_json()["message"].lower(), r.get_json()
        assert counts(db) == before, "отказ ничего не должен создавать"
        assert published_edges(client) == []

        # Легитимный запрос к тому же узлу — с указанием источника питания.
        r = add_meter(client, panel["id"], device_id="wb-map3e_1", measures="input",
                      fed_from_node_id=src["id"], name="Ввод щита")
        assert r.status_code == 201, r.get_json()
        d = r.get_json()
        assert d["created_edge"] is True, d
        assert d["edge"]["from_node_id"] == src["id"] and d["edge"]["to_node_id"] == panel["id"]
        assert d["point"]["name"] == "Ввод щита"
        edges = published_edges(client)
        assert len(edges) == 1 and edges[0]["primary_point_id"] == d["point"]["id"], edges
        after = counts(db)
        assert after["electrical_edges"] == before["electrical_edges"] + 1
        assert after["configuration_revisions"] == before["configuration_revisions"] + 1
        print("[OK] П5: ввод щита без входящей линии — отказ без fed_from_node_id "
              "ничего не создал; с ним линия и счётчик созданы одной ревизией")
    finally:
        db.close(); os.unlink(path)


def test_panel_outgoing_existing_line_and_wrong_line_refused():
    client, db, path = make_client()
    try:
        src, panel, load, e_in, e_out = scheme(client)
        before = counts(db)
        # Отказ: линия e_in входит в щит, а не отходит от него.
        r = add_meter(client, panel["id"], device_id="wb-m12_3", measures="outgoing",
                      line_id=e_in["id"])
        assert r.status_code == 400, r.get_json()
        assert counts(db) == before
        assert get_edge(client, e_in["id"])["primary_point_id"] is None

        r = add_meter(client, panel["id"], device_id="wb-m12_3", measures="outgoing",
                      line_id=e_out["id"])
        assert r.status_code == 201, r.get_json()
        d = r.get_json()
        assert d["created_edge"] is False
        assert get_edge(client, e_out["id"])["primary_point_id"] == d["point"]["id"]
        assert get_edge(client, e_in["id"])["primary_point_id"] is None
        print("[OK] П5: отходящая линия щита — чужая линия отклонена без "
              "записей, своя измеряется новой точкой")
    finally:
        db.close(); os.unlink(path)


def test_panel_outgoing_to_existing_node_reuses_or_creates_line():
    client, db, path = make_client()
    try:
        src, panel, load, e_in, e_out = scheme(client)
        load2 = make_node(client, "Насос", "load")
        before = counts(db)

        # Линия к «Насос» ещё не проведена — создаётся вместе со счётчиком.
        r = add_meter(client, panel["id"], device_id="wb-a", measures="outgoing",
                      to_node_id=load2["id"])
        assert r.status_code == 201, r.get_json()
        d = r.get_json()
        assert d["created_edge"] is True and d["created_node"] is False, d
        assert d["edge"]["from_node_id"] == panel["id"] and d["edge"]["to_node_id"] == load2["id"]
        assert counts(db)["electrical_edges"] == before["electrical_edges"] + 1
        assert counts(db)["electrical_nodes"] == before["electrical_nodes"]

        # К «Станок» линия уже есть — переиспользуется, второй не создаётся.
        edges_before = len(published_edges(client))
        r = add_meter(client, panel["id"], device_id="wb-b", measures="outgoing",
                      to_node_id=load["id"])
        assert r.status_code == 201, r.get_json()
        assert r.get_json()["created_edge"] is False
        assert r.get_json()["edge"]["id"] == e_out["id"]
        assert len(published_edges(client)) == edges_before
        assert get_edge(client, e_out["id"])["primary_point_id"] == r.get_json()["point"]["id"]
        print("[OK] П5: отходящая к существующему узлу — линия создаётся только "
              "если её не было")
    finally:
        db.close(); os.unlink(path)


def test_panel_outgoing_new_load_creates_load_and_line_atomically():
    client, db, path = make_client()
    try:
        src, panel, load, e_in, e_out = scheme(client)
        before = counts(db)
        r = add_meter(client, panel["id"], device_id="wb-new", measures="outgoing",
                      new_load_name="Компрессор")
        assert r.status_code == 201, r.get_json()
        d = r.get_json()
        assert d["created_node"] is True and d["created_edge"] is True, d
        assert d["node"]["name"] == "Компрессор" and d["node"]["kind"] == "load", d
        # Перечитываем узел и линию.
        n = node_by_id(client, d["node"]["id"])
        assert n["name"] == "Компрессор" and n["kind"] == "load" and n["archived_at"] is None
        e = get_edge(client, d["edge"]["id"])
        assert e["from_node_id"] == panel["id"] and e["to_node_id"] == n["id"]
        assert e["primary_point_id"] == d["point"]["id"] and e["state"] == "published"
        after = counts(db)
        assert after["electrical_nodes"] == before["electrical_nodes"] + 1
        assert after["electrical_edges"] == before["electrical_edges"] + 1
        assert after["metering_points"] == before["metering_points"] + 1
        assert after["configuration_revisions"] == before["configuration_revisions"] + 1

        # Пустое имя новой нагрузки — отказ, ничего не создано (пара к успеху).
        before2 = counts(db)
        r = add_meter(client, panel["id"], device_id="wb-new2", measures="outgoing",
                      new_load_name="   ")
        assert r.status_code == 400, r.get_json()
        assert counts(db) == before2
        print("[OK] П5: отходящая к НОВОЙ нагрузке — нагрузка, линия, точка и "
              "привязка созданы одной ревизией; пустое имя ничего не создаёт")
    finally:
        db.close(); os.unlink(path)


def test_source_outgoing_and_load_input_and_kind_mismatch_refused():
    client, db, path = make_client()
    try:
        src, panel, load, e_in, e_out = scheme(client)
        before = counts(db)
        # Ввод: «входящей» линии у него нет.
        r = add_meter(client, src["id"], device_id="wb-s1", measures="input")
        assert r.status_code == 400, r.get_json()
        assert "ввод" in r.get_json()["message"].lower()
        # Нагрузка: «отходящих» линий у неё нет.
        r = add_meter(client, load["id"], device_id="wb-l1", measures="outgoing",
                      new_load_name="Что-то")
        assert r.status_code == 400, r.get_json()
        assert "нагрузк" in r.get_json()["message"].lower()
        # Без measures вообще.
        r = add_meter(client, panel["id"], device_id="wb-p1")
        assert r.status_code == 400, r.get_json()
        assert counts(db) == before, "ни один отказ не должен ничего создавать"

        # Легитимные запросы к тем же узлам.
        r = add_meter(client, src["id"], device_id="wb-s1", measures="outgoing",
                      to_node_id=panel["id"])
        assert r.status_code == 201, r.get_json()
        assert r.get_json()["edge"]["id"] == e_in["id"]
        r = add_meter(client, load["id"], device_id="wb-l1", measures="input")
        assert r.status_code == 201, r.get_json()
        assert r.get_json()["edge"]["id"] == e_out["id"]
        assert get_edge(client, e_in["id"])["primary_point_id"] is not None
        assert get_edge(client, e_out["id"])["primary_point_id"] is not None
        print("[OK] П5: ввод — отходящая, нагрузка — входящая; неподходящий тип "
              "измерения отклонён без записей")
    finally:
        db.close(); os.unlink(path)


def test_occupied_device_409_human_text_nothing_created():
    client, db, path = make_client()
    try:
        src, panel, load, e_in, e_out = scheme(client)
        r = add_meter(client, panel["id"], device_id="wb-busy", measures="input",
                      name="Ввод щита")
        assert r.status_code == 201, r.get_json()
        holder_id = r.get_json()["point"]["id"]

        before = counts(db)
        # Тот же прибор на ДРУГУЮ линию, с созданием нагрузки: при отказе не
        # должно остаться ни нагрузки, ни линии, ни точки, ни привязки.
        r = add_meter(client, panel["id"], device_id="wb-busy", measures="outgoing",
                      new_load_name="Новая нагрузка")
        assert r.status_code == 409, r.get_json()
        body = r.get_json()
        assert body["code"] == "double_counting", body
        assert "Ввод щита" in body["message"], body
        no_tech(body["message"])
        assert counts(db) == before, "отказ не должен оставить НИ ОДНОЙ записи"
        assert get_edge(client, e_out["id"])["primary_point_id"] is None
        assert get_point(client, holder_id)["archived_at"] is None

        # Список занятых приборов (для выбора прибора в интерфейсе).
        occ = client.get("/api/v2/structure/points").get_json()["occupied_devices"]
        assert [(o["device_id"], o["point_id"], o["point_name"], o["archived"])
                for o in occ] == [("wb-busy", holder_id, "Ввод щита", False)], occ

        # Легитимный запрос к тому же щиту — другой, свободный прибор.
        r = add_meter(client, panel["id"], device_id="wb-free", measures="outgoing",
                      new_load_name="Новая нагрузка")
        assert r.status_code == 201, r.get_json()
        print("[OK] П4/П5: занятый прибор — 409 double_counting, понятный текст с "
              "именем точки, ни одной новой записи; свободный проходит")
    finally:
        db.close(); os.unlink(path)


def test_already_metered_line_409_and_same_device_is_idempotent():
    client, db, path = make_client()
    try:
        src, panel, load, e_in, e_out = scheme(client)
        r = add_meter(client, panel["id"], device_id="wb-first", measures="input")
        assert r.status_code == 201, r.get_json()
        first_point = r.get_json()["point"]["id"]

        before = counts(db)
        r = add_meter(client, panel["id"], device_id="wb-second", measures="input")
        assert r.status_code == 409, r.get_json()
        assert r.get_json()["code"] == "already_metered", r.get_json()
        assert counts(db) == before
        assert get_edge(client, e_in["id"])["primary_point_id"] == first_point

        # Тот же прибор на ту же линию — не плодим вторую точку, ревизия не растёт.
        rev_before = rev(client)
        r = add_meter(client, panel["id"], device_id="wb-first", measures="input")
        assert r.status_code == 200, r.get_json()
        assert r.get_json()["attached"] == "already"
        assert r.get_json()["point"]["id"] == first_point
        assert counts(db) == before and rev(client) == rev_before
        print("[OK] П5: линия уже измеряется другой точкой — 409 already_metered; "
              "повтор того же прибора — 200 без новых записей")
    finally:
        db.close(); os.unlink(path)


def test_revision_conflict_creates_nothing_fresh_revision_works():
    client, db, path = make_client()
    try:
        src, panel, load, e_in, e_out = scheme(client)
        before = counts(db)
        stale = rev(client) - 1
        r = client.post("/api/v2/topology/nodes/%d/meters" % panel["id"], json={
            "device_id": "wb-x", "measures": "outgoing", "new_load_name": "Нагрузка X",
            "expected_revision": stale})
        assert r.status_code == 409 and r.get_json()["code"] == "revision_conflict", r.get_json()
        r = client.post("/api/v2/topology/nodes/%d/meters" % panel["id"], json={
            "device_id": "wb-x", "measures": "outgoing", "new_load_name": "Нагрузка X"})
        assert r.status_code == 409 and r.get_json()["code"] == "revision_conflict", r.get_json()
        assert counts(db) == before

        r = add_meter(client, panel["id"], device_id="wb-x", measures="outgoing",
                      new_load_name="Нагрузка X")
        assert r.status_code == 201, r.get_json()
        print("[OK] П5: устаревшая и отсутствующая ревизия — 409 без записей; "
              "со свежей ревизией операция проходит")
    finally:
        db.close(); os.unlink(path)


def test_topology_conflict_cycle_rolls_everything_back():
    client, db, path = make_client()
    try:
        src = make_node(client, "Ввод", "source")
        a = make_node(client, "Щит А", "panel")
        b = make_node(client, "Щит Б", "panel")
        connect(client, a["id"], b["id"])
        before = counts(db)
        # «Щит А питается от щита Б», а Б уже питается от А — цикл.
        r = add_meter(client, a["id"], device_id="wb-cyc", measures="input",
                      fed_from_node_id=b["id"])
        assert r.status_code == 409, r.get_json()
        assert r.get_json()["code"] == "topology_conflict", r.get_json()
        assert counts(db) == before, "цикл: не должно остаться ни линии, ни точки"

        # Легитимный запрос к тому же щиту — питание от ввода.
        r = add_meter(client, a["id"], device_id="wb-cyc", measures="input",
                      fed_from_node_id=src["id"])
        assert r.status_code == 201, r.get_json()
        assert r.get_json()["created_edge"] is True
        print("[OK] П5: цикл питания — 409 topology_conflict, откат всего; "
              "питание от ввода проходит")
    finally:
        db.close(); os.unlink(path)


def test_unknown_node_and_bad_input():
    client, db, path = make_client()
    try:
        src, panel, load, e_in, e_out = scheme(client)
        before = counts(db)
        r = add_meter(client, 9999, device_id="wb-q", measures="input")
        assert r.status_code == 404, r.get_json()
        r = add_meter(client, panel["id"], measures="input")
        assert r.status_code == 400 and "device_id" in (r.get_json().get("fields") or []), r.get_json()
        r = add_meter(client, panel["id"], device_id="wb q!", measures="input")
        assert r.status_code == 400, r.get_json()
        r = add_meter(client, panel["id"], device_id="wb-q", measures="input",
                      channel_profile="bogus")
        assert r.status_code == 400, r.get_json()
        r = add_meter(client, panel["id"], device_id="wb-q", measures="outgoing",
                      line_id="abc")
        assert r.status_code == 400, r.get_json()
        assert counts(db) == before
        r = add_meter(client, panel["id"], device_id="wb-q", measures="input")
        assert r.status_code == 201, r.get_json()
        print("[OK] П5: неизвестный узел 404; без device_id, плохой device_id, "
              "профиль и line_id — 400 без записей; корректный запрос проходит")
    finally:
        db.close(); os.unlink(path)


def test_archived_node_refused():
    client, db, path = make_client()
    try:
        src = make_node(client, "Ввод", "source")
        lone = make_node(client, "Щит без линий", "panel")
        r = client.patch("/api/v2/topology/nodes/%d" % lone["id"], json={
            "archived": True, "expected_revision": rev(client)})
        assert r.status_code == 200, r.get_json()
        before = counts(db)
        r = add_meter(client, lone["id"], device_id="wb-z", measures="input",
                      fed_from_node_id=src["id"])
        assert r.status_code == 400 and "архив" in r.get_json()["message"], r.get_json()
        assert counts(db) == before
        print("[OK] П5: в архивный узел счётчик не добавить, записей нет")
    finally:
        db.close(); os.unlink(path)


# ---------------------------------------------------------------------
# П4: человеческий текст на остальных маршрутах привязки
# ---------------------------------------------------------------------

def test_old_routes_refuse_occupied_device_in_human_words():
    client, db, path = make_client()
    try:
        src, panel, load, e_in, e_out = scheme(client)
        r = add_meter(client, panel["id"], device_id="wb-held", measures="input",
                      name="Точка-хозяин")
        assert r.status_code == 201, r.get_json()

        p2 = client.post("/api/v2/points", json={
            "code": "p-second", "name": "Вторая точка"}).get_json()
        before = counts(db)
        r = client.post("/api/v2/points/%d/bindings" % p2["id"], json={
            "new_meter": {"device_id": "wb-held"}, "channel_profile": "total_3p",
            "expected_revision": rev(client)})
        assert r.status_code == 409, r.get_json()
        assert r.get_json()["code"] == "double_counting"
        assert "Точка-хозяин" in r.get_json()["message"], r.get_json()
        no_tech(r.get_json()["message"])
        assert counts(db) == before, "привязка занятого прибора ничего не создаёт"
        assert bindings_of(client, p2["id"]) == []

        # attach-new-point тем же прибором на ДРУГУЮ линию.
        r = client.post("/api/v2/topology/edges/%d/attach-new-point" % e_out["id"], json={
            "device_id": "wb-held", "expected_revision": rev(client)})
        assert r.status_code == 409 and r.get_json()["code"] == "double_counting", r.get_json()
        no_tech(r.get_json()["message"])
        assert counts(db) == before

        # Легитимный запрос: свободный прибор той же второй точке.
        r = client.post("/api/v2/points/%d/bindings" % p2["id"], json={
            "new_meter": {"device_id": "wb-free-2"}, "channel_profile": "total_3p",
            "expected_revision": rev(client)})
        assert r.status_code == 201, r.get_json()
        assert len(bindings_of(client, p2["id"])) == 1
        print("[OK] П4: привязка и «точка из прибора» на занятом приборе — 409 "
              "человеческим текстом с именем точки, записей нет; свободный проходит")
    finally:
        db.close(); os.unlink(path)


# ---------------------------------------------------------------------
# П1: «Убрать счётчик» (архивация точки)
# ---------------------------------------------------------------------

def test_archive_point_releases_device_and_line():
    client, db, path = make_client()
    try:
        src, panel, load, e_in, e_out = scheme(client)
        r = add_meter(client, panel["id"], device_id="wb-rm", measures="input",
                      name="Счётчик щита")
        assert r.status_code == 201, r.get_json()
        pid = r.get_json()["point"]["id"]

        # Отказ: устаревшая ревизия — точка не архивируется.
        r = client.patch("/api/v2/points/%d" % pid, json={
            "archived": True, "expected_revision": rev(client) - 1})
        assert r.status_code == 409, r.get_json()
        assert get_point(client, pid)["archived_at"] is None
        assert get_edge(client, e_in["id"])["primary_point_id"] == pid

        # Легитимный запрос к той же точке.
        r = client.patch("/api/v2/points/%d" % pid, json={
            "archived": True, "expected_revision": rev(client)})
        assert r.status_code == 200, r.get_json()
        d = r.get_json()
        assert d["archived_at"] is not None, d
        assert d["released"] == {"closed_bindings": 1, "edge_ids": [e_in["id"]]}, d

        # Перечитываем всё: точка в архиве, линия без счётчика, привязка закрыта
        # (но осталась как история), в списках без «архивных» точки нет.
        assert get_point(client, pid)["archived_at"] is not None
        assert get_edge(client, e_in["id"])["primary_point_id"] is None
        b = bindings_of(client, pid)
        assert len(b) == 1 and b[0]["valid_to"] is not None, b
        ids_default = [p["id"] for p in client.get("/api/v2/points").get_json()]
        ids_all = [p["id"] for p in client.get("/api/v2/points?include_archived=1").get_json()]
        assert pid not in ids_default and pid in ids_all
        sp = client.get("/api/v2/structure/points").get_json()
        assert pid not in [p["point_id"] for p in sp["points"]]
        assert sp["occupied_devices"] == [], "прибор освободился"

        # Прибор можно поставить заново — на ту же линию, новой точкой.
        r = add_meter(client, panel["id"], device_id="wb-rm", measures="input")
        assert r.status_code == 201, r.get_json()
        new_pid = r.get_json()["point"]["id"]
        assert new_pid != pid
        assert get_edge(client, e_in["id"])["primary_point_id"] == new_pid
        print("[OK] П1: «Убрать счётчик» — точка в архиве, прибор свободен, линия "
              "без счётчика, история привязки сохранена; прибор ставится заново")
    finally:
        db.close(); os.unlink(path)


def test_archive_point_without_line_still_frees_device():
    client, db, path = make_client()
    try:
        p = client.post("/api/v2/points", json={"code": "lonely", "name": "Одинокая"}).get_json()
        r = client.post("/api/v2/points/%d/bindings" % p["id"], json={
            "new_meter": {"device_id": "wb-lonely"}, "channel_profile": "total_3p",
            "expected_revision": rev(client)})
        assert r.status_code == 201, r.get_json()
        assert [o["device_id"] for o in
                client.get("/api/v2/structure/points").get_json()["occupied_devices"]] == ["wb-lonely"]
        r = client.patch("/api/v2/points/%d" % p["id"], json={
            "archived": True, "expected_revision": rev(client)})
        assert r.status_code == 200, r.get_json()
        assert r.get_json()["released"] == {"closed_bindings": 1, "edge_ids": []}, r.get_json()
        assert client.get("/api/v2/structure/points").get_json()["occupied_devices"] == []
        print("[OK] П1: точка без линии при архивации тоже освобождает прибор")
    finally:
        db.close(); os.unlink(path)


# ---------------------------------------------------------------------
# П1: узел, линия, место на плане, план
# ---------------------------------------------------------------------

def make_plan(client, name="План"):
    r = client.post("/api/v2/plans", data={
        "name": name, "plan_kind": "single_line",
        "canvas_width": "2000", "canvas_height": "1200"})
    assert r.status_code in (200, 201), r.get_json()
    return r.get_json()


def put_item(client, plan_id, node):
    r = client.post("/api/v2/plans/%d/items" % plan_id, json={
        "kind": "node", "node_id": node["id"], "geometry": {"x": 100, "y": 100},
        "coord_space": "canvas_xy_v2", "label": node["name"]})
    assert r.status_code == 201, r.get_json()
    return r.get_json()


def put_view(client, plan_id, edge, from_item, to_item):
    r = client.post("/api/v2/plans/%d/edges" % plan_id, json={
        "edge_id": edge["id"], "from_item_id": from_item["id"],
        "to_item_id": to_item["id"], "view_kind": "structural"})
    assert r.status_code == 201, r.get_json()
    return r.get_json()


def plan_state(client, plan_id):
    r = client.get("/api/v2/plans/%d" % plan_id)
    assert r.status_code == 200, r.get_json()
    return r.get_json()


def test_archive_node_names_lines_then_retire_edges_in_one_revision():
    client, db, path = make_client()
    try:
        src, panel, load, e_in, e_out = scheme(client)
        plan = make_plan(client)
        it_src, it_panel, it_load = (put_item(client, plan["id"], n) for n in (src, panel, load))
        put_view(client, plan["id"], e_in, it_src, it_panel)
        put_view(client, plan["id"], e_out, it_panel, it_load)

        before = counts(db)
        r = client.patch("/api/v2/topology/nodes/%d" % panel["id"], json={
            "archived": True, "expected_revision": rev(client)})
        assert r.status_code == 409, r.get_json()
        body = r.get_json()
        assert "Ввод → ЩР-1" in body["message"] and "ЩР-1 → Станок" in body["message"], body
        assert body["fields"] == ["retire_edges"], body
        assert sorted(l["edge_id"] for l in body["path"]) == sorted([e_in["id"], e_out["id"]])
        assert counts(db) == before
        assert node_by_id(client, panel["id"])["archived_at"] is None
        assert len(published_edges(client)) == 2
        assert len(plan_state(client, plan["id"])["items"]) == 3

        # Легитимный запрос к тому же узлу — вместе с линиями, одной ревизией.
        rev_before = rev(client)
        r = client.patch("/api/v2/topology/nodes/%d" % panel["id"], json={
            "archived": True, "retire_edges": True, "expected_revision": rev_before})
        assert r.status_code == 200, r.get_json()
        assert r.get_json()["configuration_revision"] == rev_before + 1
        assert sorted(r.get_json()["retired_edge_ids"]) == sorted([e_in["id"], e_out["id"]])
        assert node_by_id(client, panel["id"])["archived_at"] is not None
        assert published_edges(client) == []
        assert get_edge(client, e_in["id"])["valid_to"] is not None  # история связи осталась
        st = plan_state(client, plan["id"])
        assert [i["node_id"] for i in st["items"]] == [src["id"], load["id"]], st["items"]
        assert st["edges"] == [], "изображения линий выведенных связей ушли с плана"
        # Сам узел остаётся в списке с архивными.
        names = [n["name"] for n in client.get("/api/v2/topology/nodes?include_archived=1").get_json()]
        assert "ЩР-1" in names
        assert "ЩР-1" not in [n["name"] for n in client.get("/api/v2/topology/nodes").get_json()]
        print("[OK] П1: архивация узла с линиями — 409 называет линии; с "
              "retire_edges одной ревизией: линии выведены, значок и линии с плана "
              "убраны, узел в архиве, история связей сохранена")
    finally:
        db.close(); os.unlink(path)


def test_archive_node_without_lines_works_plain():
    client, db, path = make_client()
    try:
        lone = make_node(client, "Пустой щит", "panel")
        plan = make_plan(client)
        put_item(client, plan["id"], lone)
        r = client.patch("/api/v2/topology/nodes/%d" % lone["id"], json={
            "archived": True, "expected_revision": rev(client)})
        assert r.status_code == 200, r.get_json()
        assert node_by_id(client, lone["id"])["archived_at"] is not None
        assert plan_state(client, plan["id"])["items"] == []
        print("[OK] П1: узел без линий архивируется без флага, значок с плана ушёл")
    finally:
        db.close(); os.unlink(path)


def test_retire_edge_removes_its_drawing_and_double_retire_refused():
    client, db, path = make_client()
    try:
        src, panel, load, e_in, e_out = scheme(client)
        plan = make_plan(client)
        it_src, it_panel, it_load = (put_item(client, plan["id"], n) for n in (src, panel, load))
        put_view(client, plan["id"], e_in, it_src, it_panel)
        put_view(client, plan["id"], e_out, it_panel, it_load)

        r = client.patch("/api/v2/topology/edges/%d" % e_out["id"], json={
            "retire": True, "expected_revision": rev(client)})
        assert r.status_code == 200, r.get_json()
        assert get_edge(client, e_out["id"])["valid_to"] is not None
        assert [e["id"] for e in published_edges(client)] == [e_in["id"]]
        views = plan_state(client, plan["id"])["edges"]
        assert [v["edge_id"] for v in views] == [e_in["id"]], views

        # Повторно вывести ту же линию нельзя; соседняя линия при этом работает.
        before = counts(db)
        r = client.patch("/api/v2/topology/edges/%d" % e_out["id"], json={
            "retire": True, "expected_revision": rev(client)})
        assert r.status_code == 400, r.get_json()
        assert counts(db) == before
        r = client.patch("/api/v2/topology/edges/%d" % e_in["id"], json={
            "retire": True, "expected_revision": rev(client)})
        assert r.status_code == 200, r.get_json()
        assert published_edges(client) == []
        print("[OK] П1: линия выведена из работы — изображение ушло с плана, "
              "повторный вывод отклонён, соседняя линия выводится")
    finally:
        db.close(); os.unlink(path)


def test_remove_node_from_plan_keeps_node_and_cleans_lines():
    client, db, path = make_client()
    try:
        src, panel, load, e_in, e_out = scheme(client)
        plan = make_plan(client)
        it_src, it_panel, it_load = (put_item(client, plan["id"], n) for n in (src, panel, load))
        put_view(client, plan["id"], e_in, it_src, it_panel)
        put_view(client, plan["id"], e_out, it_panel, it_load)

        # Без параметра — прежнее поведение: значок уходит, линия остаётся с
        # оборванным концом (это нужно проверить, чтобы флаг был осмыслен).
        r = client.delete("/api/v2/plans/%d/items/%d" % (plan["id"], it_load["id"]))
        assert r.status_code == 200, r.get_json()
        st = plan_state(client, plan["id"])
        dangling = [v for v in st["edges"] if v["edge_id"] == e_out["id"]]
        assert len(dangling) == 1 and dangling[0]["to_item_id"] is None, st["edges"]

        # С ?edge_views=remove — значок и нарисованные к нему линии.
        r = client.delete("/api/v2/plans/%d/items/%d?edge_views=remove" % (
            plan["id"], it_panel["id"]))
        assert r.status_code == 200, r.get_json()
        st = plan_state(client, plan["id"])
        assert [i["node_id"] for i in st["items"]] == [src["id"]], st["items"]
        assert st["edges"] == [], st["edges"]

        # Узлы и линии схемы на месте (убрано только представление).
        for n in (src, panel, load):
            assert node_by_id(client, n["id"])["archived_at"] is None
        assert len(published_edges(client)) == 2
        # Отказ + легитимный: чужой/несуществующий значок.
        r = client.delete("/api/v2/plans/%d/items/%d" % (plan["id"], 99999))
        assert r.status_code == 404, r.get_json()
        print("[OK] П1: «Убрать с плана» — значок (и по флагу его линии на этом "
              "плане) уходят, узлы и линии схемы остаются; несуществующий значок 404")
    finally:
        db.close(); os.unlink(path)


def test_delete_plan_keeps_scheme_and_points():
    client, db, path = make_client()
    try:
        src, panel, load, e_in, e_out = scheme(client)
        r = add_meter(client, panel["id"], device_id="wb-keep", measures="input")
        pid = r.get_json()["point"]["id"]
        plan = make_plan(client)
        put_item(client, plan["id"], panel)
        before = counts(db)

        r = client.delete("/api/v2/plans/99999")
        assert r.status_code == 404
        r = client.delete("/api/v2/plans/%d" % plan["id"])
        assert r.status_code == 200, r.get_json()
        assert client.get("/api/v2/plans/%d" % plan["id"]).status_code == 404
        assert [p["id"] for p in client.get("/api/v2/plans").get_json()] == []
        after = counts(db)
        for t in ("electrical_nodes", "electrical_edges", "metering_points", "point_bindings"):
            assert after[t] == before[t], t
        assert get_edge(client, e_in["id"])["primary_point_id"] == pid
        assert node_by_id(client, panel["id"])["archived_at"] is None
        print("[OK] П1: «Удалить план» — план исчез, узлы, линии, точки и привязки "
              "остались (A27); несуществующий план 404")
    finally:
        db.close(); os.unlink(path)


if __name__ == "__main__":
    test_panel_input_existing_incoming_line()
    test_panel_input_without_incoming_line_creates_line_from_feeder()
    test_panel_outgoing_existing_line_and_wrong_line_refused()
    test_panel_outgoing_to_existing_node_reuses_or_creates_line()
    test_panel_outgoing_new_load_creates_load_and_line_atomically()
    test_source_outgoing_and_load_input_and_kind_mismatch_refused()
    test_occupied_device_409_human_text_nothing_created()
    test_already_metered_line_409_and_same_device_is_idempotent()
    test_revision_conflict_creates_nothing_fresh_revision_works()
    test_topology_conflict_cycle_rolls_everything_back()
    test_unknown_node_and_bad_input()
    test_archived_node_refused()
    test_old_routes_refuse_occupied_device_in_human_words()
    test_archive_point_releases_device_and_line()
    test_archive_point_without_line_still_frees_device()
    test_archive_node_names_lines_then_retire_edges_in_one_revision()
    test_archive_node_without_lines_works_plain()
    test_retire_edge_removes_its_drawing_and_double_retire_refused()
    test_remove_node_from_plan_keeps_node_and_cleans_lines()
    test_delete_plan_keeps_scheme_and_points()
    print("[ALL OK] test_step60_node_meters_api")

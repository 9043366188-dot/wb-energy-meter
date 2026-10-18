#!/usr/bin/env python3
"""Шаг 65 (партия находок 0.22.3, П9): «+ Счётчик» → «по фазам», занятость по
фазам и конфликты (docs/design-phase-aggregates.md §7–§8).

Серверная операция `POST /api/v2/topology/nodes/<id>/meters` с
`mode="phases"`:
  - одна ревизия на все фазы; на каждую используемую фазу — своя линия
    (`phase_count = 1`), своя точка и привязка с профилем `phase_lN`;
    «не используется» (`skip`) ничего не создаёт;
  - любой отказ не оставляет ничего (ни линий, ни нагрузок, ни точек, ни
    привязок, ни ревизии): занятая фаза, `total_3p` поверх фазы и наоборот,
    нет пофазных счётчиков в MQTT, неизвестный прибор, одна линия на две фазы,
    пустой список, `measures="input"`, устаревшая ревизия;
  - к каждому отказу — парный запрос «легитимный работает», сущности
    перечитываются (CODING_STANDARDS.md, «API и проверки поведения»).
`structure/points` → `occupied_devices` расширен: `profiles`, `busy_phases`,
`free_phases`, `fully_busy` (прежние поля на месте). `topology/nodes/<id>/lines`
называет прибор и фазу точки линии. Первая пофазная привязка поднимает
поколение БД до 3.

Самостоятельный скрипт (не pytest):
    python tests/test_step65_phase_api.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from wb_energy_meter import domain_generation  # noqa: E402
from wb_energy_meter.api import _AppState, create_app  # noqa: E402
from wb_energy_meter.db import Database  # noqa: E402
from wb_energy_meter.model import ControlState, MeterRegistry  # noqa: E402
from wb_energy_meter.repo import GroupRepo, KvRepo, MeterRepo  # noqa: E402

TECH_WORDS = ("scope", "channel_profile", "valid_from", "meter_source_id",
              "point_id=", "binding", "role=")
DEV = "wb-map3e_16"


def make_world():
    fd, path = tempfile.mkstemp(suffix=".sqlite3")
    os.close(fd)
    os.unlink(path)
    db = Database(path=path)
    db.open()
    groups = GroupRepo(db)
    registry = MeterRegistry()
    for dev in (DEV, "wb-map3e_17", "wb-map3e_18"):
        m = registry.get_or_create(dev)
        for ph in ("L1", "L2", "L3"):
            name = "AP energy " + ph
            m.controls[name] = ControlState(name=name, value=1.0, raw_value="1.0",
                                            meta={"type": "value"})
    plain = registry.get_or_create("wb-plain_1")             # без пофазных счётчиков
    plain.controls["Total AP energy"] = ControlState(
        name="Total AP energy", value=1.0, raw_value="1.0", meta={"type": "value"})
    state = _AppState(
        registry=registry, meters_repo=MeterRepo(db, groups), groups_repo=groups,
        is_mqtt_connected=lambda: False, mqtt_message_count=lambda: 0,
        mqtt_error_count=lambda: 0, wb_db_client=None, consumption_service=None,
        started_at=time.time(), db=db, plans_dir=tempfile.mkdtemp())
    return create_app(state).test_client(), db


def rev(client):
    return client.get("/api/v2/revision").get_json()["configuration_revision"]


def counts(db):
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


def add_meter(client, node_id, expected=None, **body):
    payload = {"expected_revision": rev(client) if expected is None else expected}
    payload.update(body)
    return client.post("/api/v2/topology/nodes/%d/meters" % node_id, json=payload)


def by_phases(client, node_id, device_id, phases, **extra):
    return add_meter(client, node_id, device_id=device_id, measures="outgoing",
                     mode="phases", phases=phases, **extra)


def get_edge(client, edge_id):
    r = client.get("/api/v2/topology/edges/%d" % edge_id)
    assert r.status_code == 200, r.get_json()
    return r.get_json()


def get_bindings(client, point_id):
    r = client.get("/api/v2/points/%d/bindings" % point_id)
    assert r.status_code == 200, r.get_json()
    return r.get_json()


def occupancy(client, device_id):
    r = client.get("/api/v2/structure/points")
    assert r.status_code == 200, r.get_json()
    for o in r.get_json()["occupied_devices"]:
        if o["device_id"] == device_id:
            return o
    return None


def no_tech(message):
    low = message.lower()
    bad = [w for w in TECH_WORDS if w in low]
    assert not bad, "в тексте отказа служебные слова %s: %s" % (bad, message)


def test_phases_success_one_revision_and_reread():
    client, db = make_world()
    try:
        panel = make_node(client, "Щит 1", "panel")
        before, rev0 = counts(db), rev(client)
        r = by_phases(client, panel["id"], DEV, [
            {"phase": "L1", "new_load_name": "Освещение"},
            {"phase": "L2", "new_load_name": "Розетки"},
            {"phase": "L3", "skip": True}])
        assert r.status_code == 201, r.get_json()
        body = r.get_json()
        after = counts(db)
        assert rev(client) == rev0 + 1 == body["configuration_revision"], "одна ревизия на все фазы"
        assert after["configuration_revisions"] == before["configuration_revisions"] + 1
        assert after["metering_points"] == before["metering_points"] + 2
        assert after["point_bindings"] == before["point_bindings"] + 2
        assert after["electrical_edges"] == before["electrical_edges"] + 2
        assert after["electrical_nodes"] == before["electrical_nodes"] + 2
        assert after["meters"] == before["meters"] + 1 and after["meter_sources"] == before["meter_sources"] + 1
        assert body["mode"] == "phases" and body["point"] is None and body["edge"] is None \
            and body["node"] is None, body
        assert body["created_edge"] is True and body["created_node"] is True
        assert [p["phase"] for p in body["phases"]] == ["L1", "L2"], body["phases"]

        names = {"L1": "Освещение", "L2": "Розетки"}
        for item in body["phases"]:
            ph = item["phase"]
            edge = get_edge(client, item["edge"]["id"])                 # перечитали
            assert edge["primary_point_id"] == item["point"]["id"], edge
            assert edge["phase_count"] == 1, edge
            assert edge["from_node_id"] == panel["id"], edge
            assert item["node"]["name"] == names[ph] and item["node"]["kind"] == "load"
            assert edge["to_node_id"] == item["node"]["id"]
            binds = get_bindings(client, item["point"]["id"])
            items = binds if isinstance(binds, list) else binds.get("bindings", binds)
            assert len(items) == 1 and items[0]["channel_profile"] == "phase_" + ph.lower() \
                and items[0]["role"] == "primary" and items[0]["valid_to"] is None, items
            assert item["point"]["name"] == "%s · %s" % (DEV, ph), item["point"]

        # карточка узла: прибор и фаза у линий
        lines = client.get("/api/v2/topology/nodes/%d/lines" % panel["id"]).get_json()["lines"]
        got = {l["phase"]: l for l in lines}
        assert set(got) == {"L1", "L2"}, lines
        assert got["L1"]["meter_device_id"] == DEV and got["L1"]["channel_profile"] == "phase_l1"
        assert got["L1"]["other_node_name"] == "Освещение" and got["L1"]["phase_count"] == 1
        assert got["L2"]["other_node_name"] == "Розетки"

        # занятость по фазам; прежние поля записи на месте
        occ = occupancy(client, DEV)
        assert occ["busy_phases"].keys() == {"L1", "L2"}, occ
        assert occ["free_phases"] == ["L3"] and occ["fully_busy"] is False, occ
        assert sorted(occ["profiles"]) == ["phase_l1", "phase_l2"]
        for key in ("device_id", "controller_key", "point_id", "point_name", "archived"):
            assert key in occ, (key, occ)
        assert occ["busy_phases"]["L1"]["point_name"] == "%s · L1" % DEV

        # поколение БД поднялось (откат на 0.22.2 теперь запрещён)
        kv = KvRepo(db)
        assert domain_generation.get_min_reader_generation(kv) == 3
    finally:
        db.close()
    print("[OK] «по фазам»: одна ревизия, 2 линии phase_count=1, 2 точки, 2 привязки phase_l1/l2, "
          "L3 не создана; карточка узла называет прибор и фазу; занятость L1,L2 / свободна L3; поколение 3")


def test_third_phase_later_makes_device_fully_busy_and_existing_line_gets_phase_count():
    client, db = make_world()
    try:
        panel = make_node(client, "Щит 1", "panel")
        r = by_phases(client, panel["id"], DEV, [{"phase": "L1", "new_load_name": "Свет"}])
        assert r.status_code == 201, r.get_json()
        # линия к существующему узлу, у которой phase_count пока не задан
        load = make_node(client, "Вентиляция", "load")
        c = client.post("/api/v2/topology/edges/connect", json={
            "from_node_id": panel["id"], "to_node_id": load["id"], "expected_revision": rev(client)})
        assert c.status_code == 201, c.get_json()
        line = c.get_json()
        assert line["phase_count"] is None
        r = by_phases(client, panel["id"], DEV, [{"phase": "L3", "line_id": line["id"]}])
        assert r.status_code == 201, r.get_json()
        assert r.get_json()["created_edge"] is False
        assert get_edge(client, line["id"])["phase_count"] == 1, "существующая линия фазы получает phase_count=1"
        occ = occupancy(client, DEV)
        assert occ["free_phases"] == ["L2"] and occ["fully_busy"] is False, occ
        r = by_phases(client, panel["id"], DEV, [{"phase": "L2", "new_load_name": "Насос"}])
        assert r.status_code == 201, r.get_json()
        occ = occupancy(client, DEV)
        assert occ["free_phases"] == [] and occ["fully_busy"] is True, occ
    finally:
        db.close()
    print("[OK] фазы по очереди: L1, затем L3 на существующей линии (phase_count=1), затем L2 → "
          "прибор занят полностью")


def test_refusals_leave_nothing_and_legit_requests_work():
    client, db = make_world()
    try:
        panel = make_node(client, "Щит 1", "panel")
        assert by_phases(client, panel["id"], DEV, [
            {"phase": "L1", "new_load_name": "Свет"}]).status_code == 201

        # --- L1 повторно (занята) ---
        before, rev0 = counts(db), rev(client)
        r = by_phases(client, panel["id"], DEV, [{"phase": "L1", "new_load_name": "Ещё свет"}])
        assert r.status_code == 409 and r.get_json()["code"] == "double_counting", r.get_json()
        msg = r.get_json()["message"]
        no_tech(msg)
        assert "фаза L1" in msg and "%s · L1" % DEV in msg, msg
        assert counts(db) == before and rev(client) == rev0, "отказ не должен ничего записать"
        # парный легитимный запрос: свободная L2 того же прибора
        assert by_phases(client, panel["id"], DEV, [
            {"phase": "L2", "new_load_name": "Ещё свет"}]).status_code == 201

        # --- атомарность: L1 свободна, L2 занята → ни одна фаза не создана ---
        dev2 = "wb-map3e_17"
        assert add_meter(client, panel["id"], device_id=dev2, measures="outgoing",
                         channel_profile="phase_l2", new_load_name="Занятая L2").status_code == 201
        before, rev0 = counts(db), rev(client)
        r = by_phases(client, panel["id"], dev2, [
            {"phase": "L1", "new_load_name": "Свободная L1"},
            {"phase": "L2", "new_load_name": "Занятая L2 ещё раз"}])
        assert r.status_code == 409 and r.get_json()["code"] == "double_counting", r.get_json()
        assert counts(db) == before and rev(client) == rev0, "L1 не должна остаться создана"
        occ = occupancy(client, dev2)
        assert occ["busy_phases"].keys() == {"L2"}
        r = by_phases(client, panel["id"], dev2, [{"phase": "L1", "new_load_name": "Свободная L1"}])
        assert r.status_code == 201, r.get_json()           # парный запрос

        # --- total_3p поверх занятых фаз и фаза поверх total_3p ---
        before, rev0 = counts(db), rev(client)
        r = add_meter(client, panel["id"], device_id=DEV, measures="outgoing",
                      new_load_name="Весь прибор")
        assert r.status_code == 409 and r.get_json()["code"] == "double_counting", r.get_json()
        no_tech(r.get_json()["message"])
        assert counts(db) == before and rev(client) == rev0
        dev3 = "wb-map3e_18"
        assert add_meter(client, panel["id"], device_id=dev3, measures="outgoing",
                         new_load_name="Всё на трёх фазах").status_code == 201
        before, rev0 = counts(db), rev(client)
        r = by_phases(client, panel["id"], dev3, [{"phase": "L1", "new_load_name": "Фаза на занятом"}])
        assert r.status_code == 409 and r.get_json()["code"] == "double_counting", r.get_json()
        assert counts(db) == before and rev(client) == rev0
        occ = occupancy(client, dev3)
        assert occ["fully_busy"] is True and occ["profiles"] == ["total_3p"], occ

        # --- устаревшая ревизия ---
        before = counts(db)
        r = add_meter(client, panel["id"], expected=rev(client) - 1, device_id=DEV,
                      measures="outgoing", mode="phases",
                      phases=[{"phase": "L3", "new_load_name": "Поздно"}])
        assert r.status_code == 409 and r.get_json()["code"] == "revision_conflict", r.get_json()
        assert counts(db) == before
        assert by_phases(client, panel["id"], DEV, [
            {"phase": "L3", "new_load_name": "Вовремя"}]).status_code == 201
    finally:
        db.close()
    print("[OK] отказы: фаза занята, атомарность (L1 свободна + L2 занята → ничего), total_3p "
          "поверх фаз и фаза поверх total_3p, устаревшая ревизия — ничего не записано; парные "
          "легитимные запросы работают")


def test_validation_refusals_create_nothing():
    client, db = make_world()
    try:
        panel = make_node(client, "Щит 1", "panel")
        load = make_node(client, "Нагрузка", "load")
        line = client.post("/api/v2/topology/edges/connect", json={
            "from_node_id": panel["id"], "to_node_id": load["id"],
            "expected_revision": rev(client)}).get_json()

        def refused(body_phases, device=DEV, **extra):
            before, rev0 = counts(db), rev(client)
            r = by_phases(client, panel["id"], device, body_phases, **extra)
            assert r.status_code == 400 and r.get_json()["code"] == "bad_request", r.get_json()
            assert counts(db) == before and rev(client) == rev0, "отказ записал что-то"
            return r.get_json()["message"]

        msg = refused([{"phase": "L1", "new_load_name": "Свет"}], device="wb-plain_1")
        assert "нет пофазных счётчиков" in msg and "wb-plain_1" in msg, msg
        no_tech(msg)
        msg = refused([{"phase": "L1", "new_load_name": "Свет"}], device="wb-never-seen_9")
        assert "ещё не появлялся" in msg, msg
        refused([])
        refused([{"phase": "L1", "skip": True}, {"phase": "L2", "skip": True}])
        refused([{"phase": "L1", "new_load_name": "А"}, {"phase": "L1", "new_load_name": "Б"}])
        refused([{"phase": "L4", "new_load_name": "А"}])
        refused([{"phase": "L1"}])
        refused([{"phase": "L1", "line_id": line["id"]}, {"phase": "L2", "line_id": line["id"]}])
        refused([{"phase": "L1", "to_node_id": load["id"]}, {"phase": "L2", "to_node_id": load["id"]}])
        # measures="input" — по фазам только отходящие
        before = counts(db)
        r = add_meter(client, panel["id"], device_id=DEV, measures="input", mode="phases",
                      phases=[{"phase": "L1", "new_load_name": "X"}])
        assert r.status_code == 400, r.get_json()
        assert counts(db) == before

        # одиночный phase_l1 без пофазных счётчиков — тот же отказ
        r = add_meter(client, panel["id"], device_id="wb-plain_1", measures="outgoing",
                      channel_profile="phase_l1", new_load_name="Свет")
        assert r.status_code == 400 and "нет пофазных счётчиков" in r.get_json()["message"]
        assert counts(db) == before

        # парный запрос: правильный прибор и корректные фазы работают
        r = by_phases(client, panel["id"], DEV, [
            {"phase": "L1", "line_id": line["id"]}, {"phase": "L2", "new_load_name": "Свет"}])
        assert r.status_code == 201, r.get_json()
    finally:
        db.close()
    print("[OK] валидация: нет пофазных счётчиков / прибор не виден / пустой список / дубли / "
          "неверная фаза / input — 400 и ничего не создано; корректный запрос работает")


def test_single_phase_profile_and_total_mode_unchanged():
    client, db = make_world()
    try:
        panel = make_node(client, "Щит 1", "panel")
        r = add_meter(client, panel["id"], device_id=DEV, measures="outgoing",
                      channel_profile="phase_l2", new_load_name="Одна фаза")
        assert r.status_code == 201, r.get_json()
        body = r.get_json()
        assert get_edge(client, body["edge"]["id"])["phase_count"] == 1
        binds = get_bindings(client, body["point"]["id"])
        items = binds if isinstance(binds, list) else binds.get("bindings", binds)
        assert items[0]["channel_profile"] == "phase_l2"
        # обычный режим total_3p: линия без phase_count, формат ответа прежний
        r = add_meter(client, panel["id"], device_id="wb-map3e_17", measures="outgoing",
                      new_load_name="Весь прибор")
        assert r.status_code == 201, r.get_json()
        body = r.get_json()
        assert get_edge(client, body["edge"]["id"])["phase_count"] is None
        assert body["point"] and body["edge"] and body["node"], "формат ответа total_3p не менялся"
        assert "phases" not in body
    finally:
        db.close()
    print("[OK] одиночный phase_l2 → phase_count=1; режим total_3p без изменений (phase_count пуст, формат ответа прежний)")


def test_total_binding_does_not_raise_generation_and_phase_does():
    client, db = make_world()
    try:
        panel = make_node(client, "Щит 1", "panel")
        kv = KvRepo(db)
        assert add_meter(client, panel["id"], device_id=DEV, measures="outgoing",
                         new_load_name="Всё").status_code == 201
        assert domain_generation.get_min_reader_generation(kv) == 2, "total_3p поколение не поднимает"
        assert by_phases(client, panel["id"], "wb-map3e_17", [
            {"phase": "L1", "new_load_name": "Фаза"}]).status_code == 201
        assert domain_generation.get_min_reader_generation(kv) == 3
    finally:
        db.close()
    print("[OK] total_3p оставляет поколение 2, первая пофазная привязка поднимает до 3")


def test_devices_report_phase_channels_and_points_report_phase():
    """Форма «+ Счётчик» решает по этим полям, можно ли «по фазам» и какую фазу меряет точка."""
    client, db = make_world()
    try:
        # приборы реестра БД (registry/meters) и видимые только в MQTT (unregistered)
        assert client.post("/api/registry/meters", json={"device_id": DEV, "display_name": "Трёхфазный"}).status_code == 201
        assert client.post("/api/registry/meters", json={"device_id": "wb-plain_1", "display_name": "Простой"}).status_code == 201
        reg = {m["device_id"]: m for m in client.get("/api/registry/meters").get_json()["items"]}
        assert reg[DEV]["phase_channels"] is True and reg["wb-plain_1"]["phase_channels"] is False, reg
        unreg = {m["device_id"]: m for m in client.get("/api/meters/unregistered").get_json()["items"]}
        assert unreg["wb-map3e_17"]["phase_channels"] is True, unreg

        panel = make_node(client, "Щит 1", "panel")
        assert by_phases(client, panel["id"], DEV, [
            {"phase": "L2", "new_load_name": "Розетки"}]).status_code == 201
        assert add_meter(client, panel["id"], device_id="wb-plain_1", measures="outgoing",
                         new_load_name="Станок").status_code == 201
        pts = {p["meter_device_id"]: p for p in client.get("/api/v2/structure/points").get_json()["points"]}
        assert pts[DEV]["meter_phase"] == "L2" and pts[DEV]["channel_profile"] == "phase_l2", pts[DEV]
        assert pts["wb-plain_1"]["meter_phase"] is None and pts["wb-plain_1"]["channel_profile"] == "total_3p"
    finally:
        db.close()
    print("[OK] registry/meters и unregistered называют phase_channels; structure/points — "
          "channel_profile и meter_phase точки")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print("\nВСЁ ЗЕЛЁНОЕ: %d проверок" % len(tests))

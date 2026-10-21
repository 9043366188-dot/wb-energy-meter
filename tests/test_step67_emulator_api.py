#!/usr/bin/env python3
"""Шаг 67 (партия находок 0.23.1, П12): HTTP API эмулятора счётчиков и метки
«эмулятор» (docs/design-emulator.md §8, §10).

Сквозной сценарий на НАСТОЯЩИХ значениях эмулятора: публикатор подменён
«петлёй» — всё, что эмулятор публикует, попадает в `MqttService` (реестр, как
из брокера) и в «историю» (как будто её пишет wb-mqtt-db). Часовые итоги
считает боевая `compute_hourly_aggregate` из этой истории, а не формула теста.
Проверяется:
  - без эмулятора в сервисе: чтение состояния и сводки работает (available=false),
    изменения — 503;
  - включение, типовой набор, правка прибора, `PATCH` без полей — 400,
    неисправности и их отказы; к каждому отказу — парный легитимный запрос;
  - приборы эмулятора видны в «неподключённых»/реестре с `is_emulator`;
  - схема Ввод → Щит → нагрузки (одна нагрузка «по фазам»): небаланс щита
    равен заданному проценту; метки `is_emulator` в снимке точки, «Структуре»
    (точки и занятость приборов), сводке «Обзора» и строках отчёта —
    у настоящего прибора метки нет;
  - «сброс» прибора → в отчёте флаг `reset` и «неполные данные»;
  - выключение: приборы без привязки исчезли из списков, точки остаются;
    «Архивировать точки эмулятора» требует подтверждения и архивирует
    ровно точки эмулятора (настоящая точка остаётся) одной ревизией.

Самостоятельный скрипт (не pytest):
    python tests/test_step67_emulator_api.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from wb_energy_meter import emulator as em  # noqa: E402
from wb_energy_meter.aggregates_repo import AggregateRepo  # noqa: E402
from wb_energy_meter.api import _AppState, create_app  # noqa: E402
from wb_energy_meter.db import Database  # noqa: E402
from wb_energy_meter.model import ControlState, MeterRegistry  # noqa: E402
from wb_energy_meter.mqtt_client import MqttService  # noqa: E402
from wb_energy_meter.repo import GroupRepo, KvRepo, MeterRepo  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import emu_rig as rig  # noqa: E402
from emu_rig import Clock, Loopback, run, HOUR  # noqa: E402,F401

T0 = 20000 * 86400
REAL = "wb-map3e_77"


def make_world(with_emulator=True):
    fd, path = tempfile.mkstemp(suffix=".sqlite3")
    os.close(fd)
    os.unlink(path)
    db = Database(path=path)
    db.open()
    groups = GroupRepo(db)
    meters = MeterRepo(db, groups)
    registry = MeterRegistry()
    real = registry.get_or_create(REAL)
    for name in ("Total AP energy", "Total P"):
        real.controls[name] = ControlState(name=name, value=1.0, raw_value="1.0",
                                           meta={"type": "value"})
    clock = Clock(T0)
    mqtt = MqttService("127.0.0.1", 1883, registry, "wb-map3e_")
    loop = Loopback(mqtt, clock)
    emu = None
    if with_emulator:
        emu = em.EmulatorService(
            KvRepo(db), publisher_factory=lambda: loop, registry=registry,
            meters_repo=meters, clock=clock, utc_offset_s=0)
    state = _AppState(
        registry=registry, meters_repo=meters, groups_repo=groups,
        is_mqtt_connected=lambda: False, mqtt_message_count=lambda: 0,
        mqtt_error_count=lambda: 0, wb_db_client=None, consumption_service=None,
        started_at=time.time(), db=db, plans_dir=tempfile.mkdtemp(),
        kv_repo=KvRepo(db), emulator=emu)
    return create_app(state).test_client(), db, emu, clock, loop, registry, meters


def rev(client):
    return client.get("/api/v2/revision").get_json()["configuration_revision"]


def make_node(client, name, kind):
    r = client.post("/api/v2/topology/nodes", json={
        "code": "n-%s-%d" % (kind, time.time_ns()), "name": name, "kind": kind,
        "expected_revision": rev(client)})
    assert r.status_code == 201, r.get_json()
    return r.get_json()


def add_meter(client, node_id, **body):
    # at=0: привязка действует с начала времён, иначе период 2024 года,
    # на котором считает тест, лежал бы до привязки (она создаётся «сейчас»)
    payload = {"expected_revision": rev(client), "at": 0}
    payload.update(body)
    r = client.post("/api/v2/topology/nodes/%d/meters" % node_id, json=payload)
    assert r.status_code == 201, r.get_json()
    return r.get_json()


def fill_aggregates(loop, meters, device, profile, hours):
    rig.fill_aggregates(loop, meters, device, profile, hours, T0)


# --------------------------------------------------------------------------

def test_without_emulator_service():
    client, db, *_ = make_world(with_emulator=False)
    try:
        # чтение не падает (экран «Настроек» открывается и без эмулятора),
        # изменения — 503
        r = client.get("/api/v2/emulator")
        assert r.status_code == 200, r.status_code
        body = r.get_json()
        assert body["available"] is False and body["enabled"] is False
        assert body["devices"] == [] and body["bound_points"] == []
        for method, url in (("post", "/api/v2/emulator/enabled"),
                            ("post", "/api/v2/emulator/devices"),
                            ("post", "/api/v2/emulator/devices/preset"),
                            ("post", "/api/v2/emulator/probe-history"),
                            ("patch", "/api/v2/emulator/devices/1"),
                            ("delete", "/api/v2/emulator/devices/1"),
                            ("post", "/api/v2/emulator/devices/1/fault")):
            r = getattr(client, method)(url, json={"enabled": True})
            assert r.status_code == 503, (url, r.status_code)
        s = client.get("/api/v2/emulator/summary").get_json()
        assert s["enabled"] is False and s["banner"] == "" and s["device_count"] == 0
        print("[OK] без эмулятора в сервисе: сводка отвечает «выключен», изменения — 503")
    finally:
        db.close()


def test_api_flow_and_marks():
    client, db, emu, clock, loop, registry, meters = make_world()
    try:
        # --- начальное состояние: выключен, приборов нет
        st = client.get("/api/v2/emulator").get_json()
        assert st["enabled"] is False and st["devices"] == [] and st["banner"] == ""
        assert st["bound_points"] == [] and st["history"]["state"] == "pending"
        assert client.get("/api/v2/emulator/summary").get_json()["enabled"] is False

        # --- отказы и парные легитимные запросы
        r = client.post("/api/v2/emulator/enabled", json={"enabled": "yes"})
        assert r.status_code == 400 and r.get_json()["fields"] == ["enabled"]
        assert client.post("/api/v2/emulator/enabled", json={}).status_code == 400
        r = client.post("/api/v2/emulator/devices", json={"kw": [1, 2]})
        assert r.status_code == 400 and "kw" in r.get_json()["message"]
        assert client.get("/api/v2/emulator").get_json()["devices"] == [], \
            "отказ ничего не создал"
        r = client.post("/api/v2/emulator/devices", json={
            "name": "Склад", "kw": [0.5, 1.0, 1.5], "profile": "flat"})
        assert r.status_code == 201, r.get_json()
        dev = r.get_json()
        assert dev["device_id"] == "emu-map3e-1" and dev["is_emulator"] is True
        assert dev["name"] == "Эмулятор: Склад" and dev["kw"] == [0.5, 1.0, 1.5]
        r = client.patch("/api/v2/emulator/devices/1", json={})
        assert r.status_code == 400 and "нечего менять" in r.get_json()["message"]
        r = client.patch("/api/v2/emulator/devices/1", json={"kw": [2, 2, 2]})
        assert r.status_code == 200 and r.get_json()["kw"] == [2.0, 2.0, 2.0]
        assert client.get("/api/v2/emulator").get_json()["devices"][0]["kw"] == [2.0, 2.0, 2.0]
        assert client.patch("/api/v2/emulator/devices/99", json={"pct": 1}).status_code == 404
        r = client.delete("/api/v2/emulator/devices/1")
        assert r.status_code == 200 and r.get_json() == {"removed": 1}
        assert client.delete("/api/v2/emulator/devices/1").status_code == 404
        r = client.post("/api/v2/emulator/devices/preset", json={})
        assert r.status_code == 201, r.get_json()
        made = r.get_json()
        assert [d["device_id"] for d in made] == [
            "emu-map3e-2", "emu-map3e-3", "emu-map3e-4", "emu-map3e-5"], \
            "номера не переиспользуются"
        assert made[3]["role"] == "input" and made[3]["sources"] == [2, 3, 4]
        assert all("эмулятор" in d["name"].lower() for d in made)

        # неисправности: выключенный эмулятор не принимает
        r = client.post("/api/v2/emulator/devices/2/fault", json={"kind": "reset"})
        assert r.status_code == 409 and "выключен" in r.get_json()["message"]

        # --- включение: приборы появляются в реестре с меткой
        assert [m for m in registry.all_ids() if m.startswith("emu-")] == []
        r = client.post("/api/v2/emulator/enabled", json={"enabled": True})
        assert r.status_code == 200 and r.get_json()["enabled"] is True
        assert r.get_json()["banner"] == \
            "Включён эмулятор: 4 виртуальных прибора — цифры с ними не настоящие"
        assert client.get("/api/v2/emulator/summary").get_json()["banner"] == r.get_json()["banner"]
        un = client.get("/api/meters/unregistered").get_json()["items"]
        emu_items = [i for i in un if i["device_id"].startswith("emu-")]
        assert len(emu_items) == 4 and all(i["is_emulator"] for i in emu_items)
        assert all(i["phase_channels"] for i in emu_items), "«по фазам» доступно"
        assert all("эмулятор" in (i["mqtt_name"] or "").lower() for i in emu_items)
        assert [i for i in un if i["device_id"] == REAL][0]["is_emulator"] is False
        st = client.get("/api/status").get_json()
        flags = {m["device_id"]: m["is_emulator"] for m in st["meters"]} \
            if "meters" in st else {}
        assert flags.get("emu-map3e-2") is True and flags.get(REAL) is False, flags

        # --- схема Ввод → Щит → нагрузки; нагрузка C «по фазам»
        src = make_node(client, "Ввод", "source")
        panel = make_node(client, "Щит 1", "panel")
        add_meter(client, panel["id"], device_id="emu-map3e-5", measures="input",
                  fed_from_node_id=src["id"])
        add_meter(client, panel["id"], device_id="emu-map3e-2", measures="outgoing",
                  new_load_name="Нагрузка A")
        add_meter(client, panel["id"], device_id="emu-map3e-3", measures="outgoing",
                  new_load_name="Нагрузка B")
        ph = add_meter(client, panel["id"], device_id="emu-map3e-4", measures="outgoing",
                       mode="phases", phases=[
                           {"phase": "L1", "new_load_name": "C фаза 1"},
                           {"phase": "L2", "new_load_name": "C фаза 2"},
                           {"phase": "L3", "new_load_name": "C фаза 3"}])
        real_load = make_node(client, "Настоящая нагрузка", "load")
        add_meter(client, panel["id"], device_id=REAL, measures="outgoing",
                  to_node_id=real_load["id"])

        # --- метки в «Структуре»: точки и занятость приборов
        sp = client.get("/api/v2/structure/points").get_json()
        by_dev = {p["meter_device_id"]: p for p in sp["points"]}
        assert sp["points"] and all(
            p["is_emulator"] is (p["meter_device_id"].startswith("emu-"))
            for p in sp["points"]), [(p["meter_device_id"], p["is_emulator"]) for p in sp["points"]]
        assert by_dev[REAL]["is_emulator"] is False
        occ = {o["device_id"]: o for o in sp["occupied_devices"]}
        assert occ["emu-map3e-5"]["is_emulator"] is True and occ[REAL]["is_emulator"] is False
        assert len([p for p in sp["points"] if p["is_emulator"]]) == 6   # 1+1+1+3

        # --- метки в снимке точки
        snap = client.get("/api/v2/snapshot").get_json()
        pts = snap["points"] if isinstance(snap, dict) else snap
        mine = {p["point_id"]: p for p in pts}
        for p in sp["points"]:
            assert mine[p["point_id"]]["is_emulator"] is p["is_emulator"], p
        emu_snap = [mine[p["point_id"]] for p in sp["points"] if p["is_emulator"]]
        assert all(s["binding_status"] == "bound" for s in emu_snap)

        # --- история 6 часов: первая половина — до «сброса» прибора 2
        run(emu, clock, 90 * 60)
        r = client.post("/api/v2/emulator/devices/2/fault", json={"kind": "reset"})
        assert r.status_code == 200, r.get_json()
        assert r.get_json()["device"]["energy_total_kwh"] < 0.01
        assert r.get_json()["journal"][0]["kind"] == "reset"
        r = client.post("/api/v2/emulator/devices/2/fault", json={"kind": "loss", "minutes": 0})
        assert r.status_code == 400 and "minutes" in r.get_json()["message"]
        run(emu, clock, 6 * HOUR - 90 * 60)

        for dev_id, profile in (("emu-map3e-5", "total_3p"), ("emu-map3e-2", "total_3p"),
                                ("emu-map3e-3", "total_3p"), ("emu-map3e-4", "phase_l1"),
                                ("emu-map3e-4", "phase_l2"), ("emu-map3e-4", "phase_l3")):
            fill_aggregates(loop, meters, dev_id, profile, range(0, 6))
        real_id = meters.get_by_device_id(REAL).id
        from wb_energy_meter.aggregates_repo import HourlyAggregate
        for h in range(6):
            AggregateRepo(db).upsert(HourlyAggregate(
                meter_id=real_id, period_start=T0 + h * HOUR, period_end=T0 + (h + 1) * HOUR,
                ap_energy_start=0.0, ap_energy_end=1.0, ap_energy_delta=1.0, p_avg=None,
                p_max=None, samples_count=1, quality_flag="ok", computed_at=1))

        # --- небаланс щита = заданный процент (после «сброса», часы 3–6)
        r = client.post("/api/v2/topology/nodes/%d/balance" % panel["id"], json={
            "from": T0 + 3 * HOUR, "to": T0 + 6 * HOUR, "timezone": "UTC"})
        assert r.status_code == 200, r.get_json()
        bal = r.get_json()
        assert bal["unavailable_reason"] is None, bal
        inp = bal["input"]["result"]["value"]
        outs = sum(o["result"]["value"] for o in bal["outputs"]
                   if o["result"] is not None and o["result"]["value"] is not None)
        assert inp > 5, inp
        # настоящая нагрузка добавила 3 кВт·ч к выходам: вычтем её и проверим «+3 %»
        outs_emu = outs - 3.0
        assert abs(inp / outs_emu - 1.03) < 1e-3, (inp, outs_emu, inp / outs_emu)
        print("[OK] щит: ввод эмулятора = сумма нагрузок × 1,03 (три нагрузки, одна «по фазам»)")

        # --- сводка «Обзора» и отчёт: метки у строк с данными эмулятора
        ov = client.post("/api/v2/overview/summary", json={
            "from": T0 + 3 * HOUR, "to": T0 + 6 * HOUR, "timezone": "UTC"}).get_json()
        assert ov["has_emulator"] is True and ov["object_has_emulator"] is True, ov["has_emulator"]
        rp = client.post("/api/v2/reports/query", json={
            "dimension": "point",
            "scope_ids": [p["point_id"] for p in sp["points"]],
            "from": T0 + 3 * HOUR, "to": T0 + 6 * HOUR, "timezone": "UTC"}).get_json()
        flags = {r["id"]: r["has_emulator"] for r in rp["rows"]}
        for p in sp["points"]:
            assert flags[p["point_id"]] is p["is_emulator"], (p, flags)
        assert rp["has_emulator"] is True
        only_real = client.post("/api/v2/reports/query", json={
            "dimension": "point", "scope_ids": [by_dev[REAL]["point_id"]],
            "from": T0 + 3 * HOUR, "to": T0 + 6 * HOUR, "timezone": "UTC"}).get_json()
        assert only_real["has_emulator"] is False and \
            only_real["rows"][0]["has_emulator"] is False
        print("[OK] метки is_emulator: снимок, «Структура» (точки и занятость), «Обзор», отчёт; "
              "у настоящего прибора метки нет")

        # --- «сброс» виден в отчёте флагом и неполными данными
        p2 = by_dev["emu-map3e-2"]["point_id"]
        rep = client.post("/api/v2/reports/query", json={
            "dimension": "point", "scope_ids": [p2],
            "from": T0, "to": T0 + 3 * HOUR, "timezone": "UTC"}).get_json()
        res = rep["rows"][0]["result"]
        assert "reset" in res["quality_flags"], res
        assert res["availability"] != "complete", res
        clean = client.post("/api/v2/reports/query", json={
            "dimension": "point", "scope_ids": [p2],
            "from": T0 + 3 * HOUR, "to": T0 + 6 * HOUR, "timezone": "UTC"}).get_json()
        assert "reset" not in clean["rows"][0]["result"]["quality_flags"]
        assert clean["rows"][0]["result"]["availability"] == "complete"
        print("[OK] «сброс» прибора эмулятора: в отчёте флаг reset и неполные данные; "
              "период без сброса — чистый")

        # --- выключение: приборы без привязки забыты, точки остаются
        client.post("/api/v2/emulator/devices", json={"kw": [1, 1, 1]})        # emu-map3e-6
        run(emu, clock, 10, step=5)
        assert registry.get("emu-map3e-6") is not None
        r = client.post("/api/v2/emulator/enabled", json={"enabled": False})
        assert r.status_code == 200 and r.get_json()["enabled"] is False
        assert registry.get("emu-map3e-6") is None, "неподключённый эмулятор забыт"
        assert registry.get("emu-map3e-2") is not None, "привязанный остаётся"
        un = client.get("/api/meters/unregistered").get_json()["items"]
        assert [i for i in un if i["device_id"] == "emu-map3e-6"] == []
        st = client.get("/api/v2/emulator").get_json()
        assert len(st["bound_points"]) == 6 and st["banner"] == ""
        assert len(client.get("/api/v2/structure/points").get_json()["points"]) == 7

        # --- архивирование точек эмулятора
        r = client.post("/api/v2/emulator/archive-points", json={"expected_revision": rev(client)})
        assert r.status_code == 400 and r.get_json()["fields"] == ["confirm"]
        assert len(client.get("/api/v2/emulator").get_json()["bound_points"]) == 6, \
            "без подтверждения ничего не изменилось"
        r = client.post("/api/v2/emulator/archive-points", json={
            "confirm": True, "expected_revision": 1})
        assert r.status_code == 409 and r.get_json()["code"] == "revision_conflict"
        r0 = rev(client)
        r = client.post("/api/v2/emulator/archive-points", json={
            "confirm": True, "expected_revision": r0})
        assert r.status_code == 200, r.get_json()
        body = r.get_json()
        assert body["archived"] == 6 and body["configuration_revision"] == r0 + 1, body
        assert sorted(p["point_id"] for p in body["points"]) == sorted(
            p["point_id"] for p in sp["points"] if p["is_emulator"])
        assert client.get("/api/v2/emulator").get_json()["bound_points"] == []
        live = client.get("/api/v2/structure/points").get_json()["points"]
        assert [p["meter_device_id"] for p in live if p["bound"]] == [REAL], \
            "настоящая точка осталась привязанной"
        assert [p for p in live if p["is_emulator"]] == [] or all(
            p["bound"] is False for p in live if p["is_emulator"])
        # повторно — нечего архивировать, ревизия не растёт
        r = client.post("/api/v2/emulator/archive-points", json={
            "confirm": True, "expected_revision": rev(client)})
        assert r.status_code == 200 and r.get_json()["archived"] == 0
        assert rev(client) == r0 + 1
        print("[OK] выключение и «Архивировать точки эмулятора»: подтверждение обязательно, "
              "одна ревизия, настоящая точка не тронута")
    finally:
        db.close()


def test_probe_history_route():
    client, db, emu, clock, loop, registry, meters = make_world()
    try:
        r = client.post("/api/v2/emulator/probe-history", json={})
        assert r.status_code == 409, r.get_json()           # проба не подключена
        emu._probe = lambda device, control, ts_from: 0
        r = client.post("/api/v2/emulator/probe-history", json={})
        assert r.status_code == 409 and "нет приборов" in r.get_json()["message"]
        client.post("/api/v2/emulator/devices/preset", json={})
        client.post("/api/v2/emulator/enabled", json={"enabled": True})
        r = client.post("/api/v2/emulator/probe-history", json={})
        assert r.status_code == 200 and r.get_json()["state"] == "missing"
        assert client.get("/api/v2/emulator").get_json()["history"]["message"] == \
            em.HISTORY_WARNING
        assert client.get("/api/v2/emulator/summary").get_json()["history_state"] == "missing"
        emu._probe = lambda device, control, ts_from: 5
        assert client.post("/api/v2/emulator/probe-history", json={}).get_json()["state"] == "ok"
        print("[OK] проба истории по API: missing с предупреждением, потом ok")
    finally:
        db.close()


def main():
    tests = [test_without_emulator_service, test_api_flow_and_marks, test_probe_history_route]
    for t in tests:
        t()
    print("[OK] шаг 67: API эмулятора и метки «эмулятор»")


if __name__ == "__main__":
    main()

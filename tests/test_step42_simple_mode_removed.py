"""Шаг 42 (партия 8, этап A, C5): простой режим удалён.

Партия 6 завела простой режим (`docs/TZ-batch6-simple-mode.md`) — тонкий
слой поверх узлов/связей, скрыто заводивший служебные узлы `sm-pt-<id>`/
`sm-src-<id>` и публиковавший через них связи. Партия 7 признала его
отменённым (две несовместимые модели питания — `docs/review-2026-09-23.md`
§4), партия 8 удаляет сам сервис (`wb_energy_meter/simple_mode_service.py`)
целиком.

Данные, которые простой режим успел создать на реальной БД, НЕ мигрируются
и не переименовываются (docs/TZ-batch7-review-fixes.md, этап 4, C3) — это
уже корректная топология в терминах «Плана v3»: обычные узлы и опубликованные
связи. Тест (a) заводит именно такую БД напрямую через репозитории (то же
самое, что раньше делал `simple_mode_service.apply_power_supply`, но
сервиса больше нет — теперь это просто electrical_nodes/electrical_edges)
и проверяет, что после удаления сервиса всё продолжает работать: баланс,
карточка узла, `is_input`/`fed_from_point_id`, видимость среди
неразмещённых узлов.

Тест (b) проверяет, что PATCH `/api/v2/points/<id>` с `is_input` или
`fed_from_point_id` теперь отклоняется 400 `field_removed` и не меняет
НИЧЕГО — с парной проверкой, что легитимный PATCH (`name`) работает.

Каждая проверка "отказ" — с парной "легитимный запрос работает" (см.
CODING_STANDARDS.md#api-и-проверки-поведения).

Самостоятельный скрипт (не pytest):
    python tests/test_step42_simple_mode_removed.py
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
from wb_energy_meter.aggregates_repo import AggregateRepo, HourlyAggregate
from wb_energy_meter.topology_service import ElectricalNodeRepo, ElectricalEdgeRepo

HOUR = 3600


def make_client():
    fd, path = tempfile.mkstemp(suffix=".sqlite3")
    os.close(fd)
    os.unlink(path)
    db = Database(path=path)
    db.open()
    groups_repo = GroupRepo(db)
    meters_repo = MeterRepo(db, groups_repo)
    registry = MeterRegistry()
    state = _AppState(
        registry=registry, meters_repo=meters_repo, groups_repo=groups_repo,
        is_mqtt_connected=lambda: False, mqtt_message_count=lambda: 0,
        mqtt_error_count=lambda: 0, wb_db_client=None,
        consumption_service=None, started_at=time.time(), db=db,
    )
    app = create_app(state)
    return app.test_client(), db, path


def current_rev(client):
    return client.get("/api/v2/revision").get_json()["configuration_revision"]


def _make_point(client, code, name):
    rev = current_rev(client)
    r = client.post("/api/v2/points", json={
        "code": code, "name": name, "expected_revision": rev})
    assert r.status_code == 201, r.get_json()
    return r.get_json()


def _bind_meter(client, point_id, device_id, display_name):
    rev = current_rev(client)
    r = client.post(f"/api/v2/points/{point_id}/bindings", json={
        "new_meter": {"controller_key": "wb8-main", "device_id": device_id,
                      "display_name": display_name},
        "channel_profile": "total_3p", "valid_from": 0, "expected_revision": rev,
    })
    assert r.status_code == 201, r.get_json()


def seed_energy(db, device_id, kwh):
    meters = MeterRepo(db, GroupRepo(db))
    m = meters.get_by_device_id(device_id)
    assert m is not None, f"meter {device_id} должен уже существовать"
    AggregateRepo(db).upsert(HourlyAggregate(
        meter_id=m.id, period_start=0, period_end=HOUR,
        ap_energy_start=0.0, ap_energy_end=kwh, ap_energy_delta=kwh,
        p_avg=None, p_max=None, samples_count=1, quality_flag="ok",
        computed_at=0))


def _seed_legacy_simple_mode_topology(db, p_input, p_c1, p_c2):
    """Заводит РОВНО то, что раньше делал
    simple_mode_service.apply_power_supply() (партия 6, теперь удалён):
    служебный узел-источник sm-src-<id> ввода, служебные узлы точки
    sm-pt-<id> на каждую из трёх точек, опубликованные связи между ними.
    Сервиса больше нет — здесь то же самое напрямую через репозитории,
    как оно и осталось бы на реальной БД, обновлённой с более старой
    версии."""
    node_repo = ElectricalNodeRepo(db)
    edge_repo = ElectricalEdgeRepo(db)

    src = node_repo.add(code=f"sm-src-{p_input['id']}",
                         name=f"Внешняя сеть ({p_input['name']})", kind="source")
    n_input = node_repo.add(code=f"sm-pt-{p_input['id']}", name=p_input["name"], kind="panel")
    n_c1 = node_repo.add(code=f"sm-pt-{p_c1['id']}", name=p_c1["name"], kind="panel")
    n_c2 = node_repo.add(code=f"sm-pt-{p_c2['id']}", name=p_c2["name"], kind="panel")

    def _publish(from_node_id, to_node_id, point_id):
        draft = edge_repo.add_draft(
            from_node_id=from_node_id, to_node_id=to_node_id,
            primary_point_id=point_id,
            code=f"sm-{point_id}-{int(time.time()*1_000_000)}")
        edge_repo.validate_edges([draft.id])
        published = edge_repo.publish_edges([draft.id])
        return published[0]

    e_in = _publish(src.id, n_input.id, p_input["id"])
    e_c1 = _publish(n_input.id, n_c1.id, p_c1["id"])
    e_c2 = _publish(n_input.id, n_c2.id, p_c2["id"])
    return {"src": src, "n_input": n_input, "n_c1": n_c1, "n_c2": n_c2,
            "e_in": e_in, "e_c1": e_c1, "e_c2": e_c2}


def test_legacy_simple_mode_data_keeps_working_after_removal():
    client, db, path = make_client()
    try:
        p_input = _make_point(client, "vvod", "Ввод объекта")
        p_c1 = _make_point(client, "stanki", "Станки")
        p_c2 = _make_point(client, "osv", "Освещение")
        _bind_meter(client, p_input["id"], "dev-vvod", "Счётчик ввода")
        _bind_meter(client, p_c1["id"], "dev-stanki", "Счётчик станков")
        _bind_meter(client, p_c2["id"], "dev-osv", "Счётчик освещения")

        topo = _seed_legacy_simple_mode_topology(db, p_input, p_c1, p_c2)

        seed_energy(db, "dev-vvod", 100.0)
        seed_energy(db, "dev-stanki", 70.0)
        seed_energy(db, "dev-osv", 40.0)

        # --- overview/summary: 100 через ввод, небаланс −10/−10% -------
        r = client.post("/api/v2/overview/summary",
                         json={"from": 0, "to": HOUR, "timezone": "UTC"})
        assert r.status_code == 200, r.get_json()
        body = r.get_json()
        assert body["object_input_point_ids"] == [p_input["id"]], body
        assert body["object_total"]["value"] == 100.0, body
        assert body["imbalance_value"] == -10.0, body
        assert body["imbalance_percent"] == -10.0, body

        # --- карточка узла: /topology/nodes/<id>/lines работает --------
        r = client.get(f"/api/v2/topology/nodes/{topo['n_input'].id}/lines")
        assert r.status_code == 200, r.get_json()
        lines = r.get_json()["lines"]
        assert len(lines) == 3, lines  # 1 входящая (ввод) + 2 отходящих
        by_edge = {ln["edge_id"]: ln for ln in lines}
        assert by_edge[topo["e_in"].id]["direction"] == "in"
        assert by_edge[topo["e_in"].id]["primary_point_id"] == p_input["id"]
        assert by_edge[topo["e_c1"].id]["primary_point_id"] == p_c1["id"]
        assert by_edge[topo["e_c2"].id]["primary_point_id"] == p_c2["id"]

        # --- is_input/fed_from_point_id выведены из топологии верно ----
        r = client.get(f"/api/v2/points/{p_input['id']}")
        assert r.status_code == 200, r.get_json()
        ps = r.get_json()["power_supply"]
        assert ps["is_input"] is True, ps
        assert ps["fed_from_point_id"] is None, ps
        assert ps["measured_edge_id"] == topo["e_in"].id, ps

        r = client.get(f"/api/v2/points/{p_c1['id']}")
        assert r.status_code == 200, r.get_json()
        ps = r.get_json()["power_supply"]
        assert ps["is_input"] is False, ps
        assert ps["fed_from_point_id"] == p_input["id"], ps
        assert ps["measured_edge_id"] == topo["e_c1"].id, ps

        r = client.get("/api/v2/structure/points")
        by_id = {i["point_id"]: i for i in r.get_json()["points"]}
        assert by_id[p_input["id"]]["is_input"] is True, by_id[p_input["id"]]
        assert by_id[p_c1["id"]]["fed_from_point_id"] == p_input["id"], by_id[p_c1["id"]]
        assert by_id[p_c2["id"]]["fed_from_point_id"] == p_input["id"], by_id[p_c2["id"]]

        # --- узлы sm-* видны как неразмещённые (ни один не на плане) ---
        r = client.get("/api/v2/topology/nodes")
        assert r.status_code == 200
        all_node_ids = {n["id"] for n in r.get_json()}
        assert {topo["src"].id, topo["n_input"].id, topo["n_c1"].id,
                topo["n_c2"].id} <= all_node_ids

        r = client.get("/api/v2/validation")
        assert r.status_code == 200, r.get_json()
        unplaced_node_ids = {n["node_id"] for n in r.get_json()["nodes_without_edges"]}
        # ни один из sm-* узлов не входит в nodes_without_edges (у всех
        # есть связи) — они "неразмещены" в другом смысле (нет
        # plan_items), что уже проверяет points_without_plan:
        assert not ({topo["src"].id, topo["n_input"].id} & unplaced_node_ids)
        points_without_plan_ids = {p["point_id"] for p in r.get_json()["points_without_plan"]}
        assert {p_input["id"], p_c1["id"], p_c2["id"]} <= points_without_plan_ids, (
            "точки sm-* топологии не размещены ни на одном плане — "
            "должны попадать в points_without_plan, как и любые другие")

        print("[OK] БД в состоянии «после простого режима» (sm-src-*/sm-pt-* "
              "узлы, опубликованные связи): overview/summary 100/−10/−10%, "
              "/topology/nodes/<id>/lines, is_input/fed_from_point_id из "
              "топологии, узлы видны как неразмещённые — всё работает и "
              "без simple_mode_service.py")
    finally:
        db.close()
        os.unlink(path)


def test_patch_is_input_rejected_field_removed():
    client, db, path = make_client()
    try:
        point = _make_point(client, "p1", "Точка 1")

        rev = current_rev(client)
        r = client.patch(f"/api/v2/points/{point['id']}", json={
            "is_input": True, "expected_revision": rev})
        assert r.status_code == 400, r.get_json()
        assert r.get_json()["code"] == "field_removed", r.get_json()

        rev = current_rev(client)
        r = client.patch(f"/api/v2/points/{point['id']}", json={
            "fed_from_point_id": 999, "expected_revision": rev})
        assert r.status_code == 400, r.get_json()
        assert r.get_json()["code"] == "field_removed", r.get_json()

        # ничего не изменилось: имя то же, ревизия не сдвинулась
        r = client.get(f"/api/v2/points/{point['id']}")
        assert r.get_json()["name"] == "Точка 1"
        assert current_rev(client) == rev

        # тело с is_input ВМЕСТЕ с легитимным полем — тоже отклонено
        # целиком, не применяет частично
        r = client.patch(f"/api/v2/points/{point['id']}", json={
            "name": "Новое имя", "is_input": False, "expected_revision": rev})
        assert r.status_code == 400, r.get_json()
        assert r.get_json()["code"] == "field_removed", r.get_json()
        r = client.get(f"/api/v2/points/{point['id']}")
        assert r.get_json()["name"] == "Точка 1", (
            "имя не должно было измениться — is_input в теле отклоняет "
            "весь PATCH, а не только своё поле", r.get_json())

        # легитимная проверка: PATCH name без запрещённых полей работает
        rev = current_rev(client)
        r = client.patch(f"/api/v2/points/{point['id']}", json={
            "name": "Новое имя", "expected_revision": rev})
        assert r.status_code == 200, r.get_json()
        assert r.get_json()["name"] == "Новое имя"

        print("[OK] PATCH is_input/fed_from_point_id -> 400 field_removed, "
              "ничего не меняет даже вместе с легитимным полем; "
              "легитимный PATCH name по-прежнему работает")
    finally:
        db.close()
        os.unlink(path)


def test_measured_edge_label_is_human_not_internal_code():
    """ПРОБЛЕМА (найдено при проверке 26.09.2026): подпись измеряемой
    линии строилась как `edge.name or edge.code or "A → B"`, а код у
    линии есть ВСЕГДА (connect_nodes/add_consumer генерируют
    `pv3-e-<from>-<to>-<нс>`). Значит, у каждой неименованной линии в
    интерфейсе стоял бы `pv3-e-3-4-1790434002725159`, а ветка «A → B»
    была недостижима. Это тот же баг, что уже чинили в партии 6, §5
    («подпись связи edge_id: 1»).

    ЛЕГИТИМНЫЙ СЛУЧАЙ: у линии есть своё имя — показывается оно."""
    client, db, path = make_client()
    try:
        nodes = ElectricalNodeRepo(db)
        edges = ElectricalEdgeRepo(db)
        src = nodes.add(code="n-src", name="Ввод", kind="source")
        shr = nodes.add(code="n-shr", name="ЩР-1", kind="panel")
        load = nodes.add(code="n-load", name="Станки", kind="load")

        p_unnamed = _make_point(client, "pu", "Точка безымянной линии")
        p_named = _make_point(client, "pn", "Точка именованной линии")
        _bind_meter(client, p_unnamed["id"], "dev-u", "U")
        _bind_meter(client, p_named["id"], "dev-n", "N")

        e_unnamed = edges.add_draft(src.id, shr.id,
                                     code="pv3-e-1-2-1790434002725159",
                                     primary_point_id=p_unnamed["id"])
        e_named = edges.add_draft(shr.id, load.id, code="pv3-e-2-3-179043400272",
                                   name="Фидер 3", primary_point_id=p_named["id"])
        edges.publish_edges([e_unnamed.id, e_named.id])

        rows = client.get("/api/v2/structure/points").get_json()["points"]
        by_id = {r["point_id"]: r for r in rows}

        label = by_id[p_unnamed["id"]]["measured_edge_label"]
        assert label == "Ввод → ЩР-1", (
            "у безымянной линии подпись обязана строиться по концам, а не "
            "быть внутренним кодом", label)
        assert "pv3-e-" not in label, label

        assert by_id[p_named["id"]]["measured_edge_label"] == "Фидер 3", (
            "ЛЕГИТИМНЫЙ СЛУЧАЙ сломан: своё имя линии должно побеждать",
            by_id[p_named["id"]]["measured_edge_label"])

        print("[OK] measured_edge_label: безымянная линия -> «Ввод → ЩР-1», "
              "именованная -> своё имя; внутренний код наружу не течёт")
    finally:
        db.close(); os.unlink(path)


if __name__ == "__main__":
    test_legacy_simple_mode_data_keeps_working_after_removal()
    test_patch_is_input_rejected_field_removed()
    test_measured_edge_label_is_human_not_internal_code()
    print("\nВсе тесты удаления простого режима (Шаг 42) пройдены.")

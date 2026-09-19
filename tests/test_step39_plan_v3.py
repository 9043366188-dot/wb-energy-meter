"""Шаг 39 (партия 6, задача «План v3»): узлы на карте, несколько
счётчиков в одном узле, линия без счётчика, человеческие тексты
конфликтов (docs/TZ-batch6-plan-v3.md).

Модель не меняется: те же electrical_nodes/electrical_edges, что и у
«Структуры»/«Плана v2» (см. wb_energy_meter/topology_service.py) — этот
файл проверяет ТОНКИЙ СЛОЙ поверх них (wb_energy_meter/plan_v3_service.py
+ новые маршруты в api_v2.py): «соединить узлы линией» (создание
черновика и публикация одним вызовом), «добавить отходящую линию»
(узел-потребитель + линия одним вызовом), «поставить счётчик на линию»
(PATCH primary_point_id у уже существующей связи), карточка узла
(список смежных линий), свободный текст «где стоит» и полигон-зона.

ОБЯЗАТЕЛЬНЫЙ сквозной сценарий (§8 задания): узел-ввод → щит → (щит
кормит несколько потребителей); счётчики стоят на входящей линии щита
И на одной из отходящих — то есть ДВЕ точки на линиях одного и того же
узла (щита); есть ЕЩЁ ОДНА отходящая линия щита БЕЗ счётчика — отдельно
проверяем, что она не ломает расчёт. Кроме двух метрируемых потребителей
(как того требует задание буквально: «щит и двух потребителей»), заведён
третий, немерянный, специально ради проверки «линия без счётчика» —
это осознанное расширение сценария сверх буквального текста задания, а
не два его прочтения вперемешку: без третьего узла требование
«поставить счётчики... на одну отходящую» (значит — НЕ на все) и
одновременно «расход 100 на вводе, 70 и 40 на отходящих» (для которых
и нужен небаланс −10/−10%, как в test_step36) вместе не выполнить —
третий, отдельный потребитель без счётчика разводит эти два требования
между собой, ничего не подгоняя.

Каждая проверка "отказ" — с парной "легитимный запрос работает" (см.
AGENTS.md).

Самостоятельный скрипт (не pytest):
    python tests/test_step39_plan_v3.py
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


def make_client_with_plans():
    """Как make_client(), но с настроенным plans_dir — только тесту про
    полигон-зону нужен реальный /api/v2/plans (остальным тестам этого
    файла план не нужен, plans_dir им не нужен)."""
    fd, path = tempfile.mkstemp(suffix=".sqlite3")
    os.close(fd)
    os.unlink(path)
    db = Database(path=path)
    db.open()
    groups_repo = GroupRepo(db)
    meters_repo = MeterRepo(db, groups_repo)
    registry = MeterRegistry()
    plans_dir = tempfile.mkdtemp()
    state = _AppState(
        registry=registry, meters_repo=meters_repo, groups_repo=groups_repo,
        is_mqtt_connected=lambda: False, mqtt_message_count=lambda: 0,
        mqtt_error_count=lambda: 0, wb_db_client=None,
        consumption_service=None, started_at=time.time(), db=db,
        plans_dir=plans_dir,
    )
    app = create_app(state)
    return app.test_client(), db, path


def seed_energy(db, device_id, kwh):
    meters = MeterRepo(db, GroupRepo(db))
    m = meters.get_by_device_id(device_id)
    assert m is not None, f"meter {device_id} должен уже существовать"
    AggregateRepo(db).upsert(HourlyAggregate(
        meter_id=m.id, period_start=0, period_end=HOUR,
        ap_energy_start=0.0, ap_energy_end=kwh, ap_energy_delta=kwh,
        p_avg=None, p_max=None, samples_count=1, quality_flag="ok",
        computed_at=0))


def _make_node(client, code, name, kind):
    rev = current_rev(client)
    r = client.post("/api/v2/topology/nodes", json={
        "code": code, "name": name, "kind": kind, "expected_revision": rev})
    assert r.status_code == 201, r.get_json()
    return r.get_json()


def _connect(client, from_id, to_id, **extra):
    rev = current_rev(client)
    body = {"from_node_id": from_id, "to_node_id": to_id, "expected_revision": rev}
    body.update(extra)
    return client.post("/api/v2/topology/edges/connect", json=body)


def _add_consumer(client, node_id, name, **extra):
    rev = current_rev(client)
    body = {"name": name, "expected_revision": rev}
    body.update(extra)
    return client.post(f"/api/v2/topology/nodes/{node_id}/add-consumer", json=body)


def _set_meter_on_edge(client, edge_id, point_id):
    rev = current_rev(client)
    return client.patch(f"/api/v2/topology/edges/{edge_id}", json={
        "primary_point_id": point_id, "expected_revision": rev})


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


def _published_edges(client):
    r = client.get("/api/v2/topology/edges?state=published")
    assert r.status_code == 200
    return r.get_json()


# ---------------------------------------------------------------------
# Сквозной тест (§8, ОБЯЗАТЕЛЬНЫЙ)
# ---------------------------------------------------------------------

def test_e2e_input_panel_two_metered_consumers_and_one_unmetered_line():
    client, db, path = make_client()
    try:
        n_vvod = _make_node(client, "n-vvod", "Ввод", "source")
        n_shr = _make_node(client, "n-shr1", "ЩР-1", "panel")

        r = _connect(client, n_vvod["id"], n_shr["id"], name="Ввод → ЩР-1")
        assert r.status_code == 201, r.get_json()
        e_in = r.get_json()

        r = _add_consumer(client, n_shr["id"], "Станки")
        assert r.status_code == 201, r.get_json()
        e_out1 = r.get_json()["edge"]
        n_out1 = r.get_json()["node"]
        assert n_out1["kind"] == "load", n_out1

        r = _add_consumer(client, n_shr["id"], "Освещение")
        assert r.status_code == 201, r.get_json()
        e_out2 = r.get_json()["edge"]

        # третья отходящая линия щита — сознательно БЕЗ счётчика
        r = _add_consumer(client, n_shr["id"], "Розетки")
        assert r.status_code == 201, r.get_json()
        e_out3 = r.get_json()["edge"]
        assert e_out3["primary_point_id"] is None

        p_input = _make_point(client, "vvod", "Ввод объекта")
        p_c1 = _make_point(client, "stanki", "Станки")
        p_c2 = _make_point(client, "osv", "Освещение")
        _bind_meter(client, p_input["id"], "dev-vvod", "Счётчик ввода")
        _bind_meter(client, p_c1["id"], "dev-stanki", "Счётчик станков")
        _bind_meter(client, p_c2["id"], "dev-osv", "Счётчик освещения")

        # счётчики на входящую линию щита И на одну отходящую — то есть
        # ДВЕ точки на линиях одного и того же узла (ЩР-1) --------------
        r = _set_meter_on_edge(client, e_in["id"], p_input["id"])
        assert r.status_code == 200, r.get_json()
        r = _set_meter_on_edge(client, e_out1["id"], p_c1["id"])
        assert r.status_code == 200, r.get_json()
        r = _set_meter_on_edge(client, e_out2["id"], p_c2["id"])
        assert r.status_code == 200, r.get_json()

        # карточка узла ЩР-1: обе метрируемые линии видны, третья — без
        # счётчика, не выпадает из списка ---------------------------------
        r = client.get(f"/api/v2/topology/nodes/{n_shr['id']}/lines")
        assert r.status_code == 200, r.get_json()
        lines = r.get_json()["lines"]
        assert len(lines) == 4, lines  # 1 входящая + 3 отходящих
        by_edge = {ln["edge_id"]: ln for ln in lines}
        assert by_edge[e_in["id"]]["direction"] == "in"
        assert by_edge[e_in["id"]]["primary_point_id"] == p_input["id"]
        assert by_edge[e_out1["id"]]["primary_point_id"] == p_c1["id"]
        assert by_edge[e_out2["id"]]["primary_point_id"] == p_c2["id"]
        assert by_edge[e_out3["id"]]["primary_point_id"] is None
        assert by_edge[e_out3["id"]]["direction"] == "out"

        rev = current_rev(client)
        r = client.post("/api/v2/groups", json={
            "name": "Нагрузка цеха", "expected_revision": rev})
        assert r.status_code == 201, r.get_json()
        group = r.get_json()
        for pid in (p_c1["id"], p_c2["id"]):
            rev = current_rev(client)
            r = client.post(f"/api/v2/groups/{group['id']}/members", json={
                "point_id": pid, "expected_revision": rev})
            assert r.status_code == 201, r.get_json()

        seed_energy(db, "dev-vvod", 100.0)
        seed_energy(db, "dev-stanki", 70.0)
        seed_energy(db, "dev-osv", 40.0)
        # dev розеток нарочно НЕ существует — на линии без счётчика
        # физически неоткуда взяться показанию.

        r = client.post("/api/v2/overview/summary",
                         json={"from": 0, "to": HOUR, "timezone": "UTC"})
        assert r.status_code == 200, r.get_json()
        body = r.get_json()
        assert body["object_input_point_ids"] == [p_input["id"]], body
        assert body["object_total"]["value"] == 100.0, (
            "итог должен браться через назначенный ввод (100), а не суммой "
            "всех приборов (100+70+40=210)", body)
        assert body["imbalance_value"] == -10.0, body
        assert body["imbalance_percent"] == -10.0, body

        print("[OK] сквозной сценарий «Плана v3»: ввод → щит с тремя "
              "отходящими (2 со счётчиком в одном узле + 1 без счётчика) "
              "→ «Обзор» 100 через ввод, небаланс −10/−10%, линия без "
              "счётчика расчёт не ломает")
    finally:
        db.close(); os.unlink(path)


# ---------------------------------------------------------------------
# Цикл питания — отказ понятным текстом, ничего не сохраняется
# ---------------------------------------------------------------------

def test_cycle_rejected_in_node_terms_and_legit_connection_still_works():
    client, db, path = make_client()
    try:
        a = _make_node(client, "n-a", "Узел А", "panel")
        b = _make_node(client, "n-b", "Узел Б", "panel")
        c = _make_node(client, "n-c", "Узел В", "panel")

        r = _connect(client, a["id"], b["id"])
        assert r.status_code == 201, r.get_json()

        edges_before = _published_edges(client)
        assert len(edges_before) == 1

        r = _connect(client, b["id"], a["id"])
        assert r.status_code == 409, r.get_json()
        msg = r.get_json()["message"]
        assert "Узел А" in msg and "Узел Б" in msg, msg
        assert "цикл" in msg.lower(), msg

        edges_after = _published_edges(client)
        assert len(edges_after) == 1, (
            "цикл отклонён, но опубликованный граф изменился", edges_after)

        # легитимный запрос по-прежнему работает: Б -> В (не цикл) --------
        r = _connect(client, b["id"], c["id"])
        assert r.status_code == 201, r.get_json()
        assert len(_published_edges(client)) == 2

        print("[OK] цикл питания (А→Б, Б→А) отклонён текстом по именам "
              "узлов, граф не изменился; легитимное Б→В прошло")
    finally:
        db.close(); os.unlink(path)


# ---------------------------------------------------------------------
# Второй источник питания одного узла — отказ, ничего не сохраняется
# ---------------------------------------------------------------------

def test_second_power_source_rejected_and_legit_connection_still_works():
    client, db, path = make_client()
    try:
        src1 = _make_node(client, "n-src1", "Ввод 1", "source")
        src2 = _make_node(client, "n-src2", "Ввод 2", "source")
        target = _make_node(client, "n-target", "ЩР-2", "panel")
        other = _make_node(client, "n-other", "ЩР-3", "panel")

        r = _connect(client, src1["id"], target["id"])
        assert r.status_code == 201, r.get_json()

        r = _connect(client, src2["id"], target["id"])
        assert r.status_code == 409, r.get_json()
        msg = r.get_json()["message"]
        assert "ЩР-2" in msg, msg
        assert "Ввод 1" in msg or "Ввод 2" in msg, msg

        edges = _published_edges(client)
        assert len(edges) == 1, (
            "второй источник отклонён, но граф изменился", edges)

        # легитимно: второй ввод питает ДРУГОЙ, свободный узел -----------
        r = _connect(client, src2["id"], other["id"])
        assert r.status_code == 201, r.get_json()
        assert len(_published_edges(client)) == 2

        print("[OK] второй источник питания узла отклонён текстом с "
              "именами узлов, граф не изменился; легитимное подключение "
              "второго ввода к другому узлу прошло")
    finally:
        db.close(); os.unlink(path)


# ---------------------------------------------------------------------
# «Добавить отходящую линию» от потребителя — отказ, легитимно от щита
# ---------------------------------------------------------------------

def test_add_consumer_from_load_rejected_and_from_panel_still_works():
    client, db, path = make_client()
    try:
        shr = _make_node(client, "n-shr", "ЩР-1", "panel")
        r = _add_consumer(client, shr["id"], "Станки")
        assert r.status_code == 201, r.get_json()
        load_node = r.get_json()["node"]
        assert load_node["kind"] == "load"

        nodes_before = client.get("/api/v2/topology/nodes").get_json()

        r = _add_consumer(client, load_node["id"], "Ещё что-то")
        assert r.status_code == 409, r.get_json()
        assert "потребитель" in r.get_json()["message"].lower()

        nodes_after = client.get("/api/v2/topology/nodes").get_json()
        assert len(nodes_after) == len(nodes_before), (
            "отказ добавить потребителя от load, но узел всё равно создался")

        # легитимно: ещё одна отходящая линия от самого щита -------------
        r = _add_consumer(client, shr["id"], "Освещение")
        assert r.status_code == 201, r.get_json()

        print("[OK] «добавить отходящую линию» от потребителя (load) "
              "отклонено без следа в БД; легитимно от щита — работает")
    finally:
        db.close(); os.unlink(path)


# ---------------------------------------------------------------------
# Счётчик уже измеряет другую действующую линию — отказ + снятие/перенос
# ---------------------------------------------------------------------

def test_point_already_measures_another_edge_rejected_and_reassign_works():
    client, db, path = make_client()
    try:
        shr = _make_node(client, "n-shr", "ЩР-1", "panel")
        r = _add_consumer(client, shr["id"], "Станки")
        e1 = r.get_json()["edge"]
        r = _add_consumer(client, shr["id"], "Освещение")
        e2 = r.get_json()["edge"]

        point = _make_point(client, "cnt-1", "Счётчик 1")

        r = _set_meter_on_edge(client, e1["id"], point["id"])
        assert r.status_code == 200, r.get_json()

        r = _set_meter_on_edge(client, e2["id"], point["id"])
        assert r.status_code == 400, r.get_json()
        assert "уже измеряет" in r.get_json()["message"]

        r = client.get(f"/api/v2/topology/edges/{e2['id']}")
        assert r.get_json()["primary_point_id"] is None, (
            "отказ, но точка всё равно записалась на вторую линию")

        # снять с первой линии, поставить на вторую — легитимно ----------
        r = _set_meter_on_edge(client, e1["id"], None)
        assert r.status_code == 200, r.get_json()
        r = _set_meter_on_edge(client, e2["id"], point["id"])
        assert r.status_code == 200, r.get_json()
        assert r.get_json()["primary_point_id"] == point["id"]

        print("[OK] назначение точки на вторую действующую линию отклонено; "
              "снятие с первой и перенос на вторую — легитимно работает")
    finally:
        db.close(); os.unlink(path)


# ---------------------------------------------------------------------
# Карточка узла: имя + свободный текст "где стоит"
# ---------------------------------------------------------------------

def test_node_rename_and_location_text_and_empty_name_rejected():
    client, db, path = make_client()
    try:
        node = _make_node(client, "n-1", "Узел без имени толком", "panel")

        rev = current_rev(client)
        r = client.patch(f"/api/v2/topology/nodes/{node['id']}", json={
            "name": "ЩР-1", "location_text": "Электрощитовая, 1 этаж",
            "expected_revision": rev})
        assert r.status_code == 200, r.get_json()
        out = r.get_json()
        assert out["name"] == "ЩР-1"
        assert out["location_id"] is not None

        r = client.get(f"/api/v2/locations/{out['location_id']}")
        assert r.get_json()["name"] == "Электрощитовая, 1 этаж"

        # тот же текст второй раз — не плодит дубликат места --------------
        rev = current_rev(client)
        node2 = _make_node(client, "n-2", "Второй узел", "panel")
        r = client.patch(f"/api/v2/topology/nodes/{node2['id']}", json={
            "location_text": "электрощитовая, 1 этаж",  # другой регистр
            "expected_revision": current_rev(client)})
        assert r.status_code == 200, r.get_json()
        assert r.get_json()["location_id"] == out["location_id"], (
            "казефолд-совпадающий текст должен найти то же место, а не "
            "завести новое")

        # пустое имя отклонено, легитимное — работает ---------------------
        rev = current_rev(client)
        r = client.patch(f"/api/v2/topology/nodes/{node['id']}", json={
            "name": "   ", "expected_revision": rev})
        assert r.status_code == 400, r.get_json()
        r = client.get(f"/api/v2/topology/nodes/{node['id']}")
        assert r.get_json()["name"] == "ЩР-1"

        rev = current_rev(client)
        r = client.patch(f"/api/v2/topology/nodes/{node['id']}", json={
            "name": "ЩР-1 (главный)", "expected_revision": rev})
        assert r.status_code == 200, r.get_json()

        print("[OK] переименование узла и текст «где стоит» (казефолд, без "
              "дублей мест) работают; пустое имя отклонено, легитимное "
              "переименование — нет")
    finally:
        db.close(); os.unlink(path)


# ---------------------------------------------------------------------
# Зона учёта областью (kind='group', полигон) — баг plan_geo_v2 исправлен
# ---------------------------------------------------------------------

def test_group_polygon_zone_accepted_point_polygon_still_rejected():
    client, db, path = make_client_with_plans()
    try:
        r = client.post("/api/v2/plans", data={
            "name": "Тестовый план", "plan_kind": "single_line",
            "canvas_width": "1000", "canvas_height": "800"})
        assert r.status_code == 201, r.get_json()
        plan = r.get_json()

        rev = current_rev(client)
        r = client.post("/api/v2/groups", json={
            "name": "Зона 1", "expected_revision": rev})
        group = r.get_json()

        polygon = [[10, 10], [200, 10], [200, 150], [10, 150]]
        r = client.post(f"/api/v2/plans/{plan['id']}/items", json={
            "kind": "group", "group_id": group["id"],
            "geometry": polygon, "coord_space": "canvas_xy_v2"})
        assert r.status_code == 201, (
            "полигон для kind='group' должен приниматься (§2/§5 задания)",
            r.get_json())
        item = r.get_json()
        assert item["geometry"] == polygon

        # легитимная точка (обычный маркер) по-прежнему работает ---------
        point = _make_point(client, "p-1", "Точка 1")
        r = client.post(f"/api/v2/plans/{plan['id']}/items", json={
            "kind": "point", "point_id": point["id"],
            "geometry": {"x": 50, "y": 50}, "coord_space": "canvas_xy_v2"})
        assert r.status_code == 201, r.get_json()

        # полигон для kind='point' по-прежнему отклоняется (не ослабили
        # валидацию сверх задуманного) ------------------------------------
        point2 = _make_point(client, "p-2", "Точка 2")
        r = client.post(f"/api/v2/plans/{plan['id']}/items", json={
            "kind": "point", "point_id": point2["id"],
            "geometry": polygon, "coord_space": "canvas_xy_v2"})
        assert r.status_code == 400, r.get_json()

        print("[OK] полигон для зоны учёта (kind='group') принимается "
              "(баг plan_geo_v2 исправлен); точка-маркер и запрет "
              "полигона для kind='point' по-прежнему работают")
    finally:
        db.close(); os.unlink(path)


# ---------------------------------------------------------------------
# §5 задания ("Состояния размещения/изолированности врут"): точка не
# имеет своего узла в модели «Плана v3» — она измеряет линию, а линия
# инцидентна узлу. placed_on_plan/no_plan должны считать точку
# размещённой, если размещён (kind='node') любой из узлов измеряемой ею
# линии — раньше (до этого фикса) это работало только для СВОЕГО узла
# точки из отменённого простого режима, которого «План v3» не заводит,
# и точка неизменно показывалась "не размещена", хотя на карте видна
# линия, которую она измеряет.
# ---------------------------------------------------------------------

def test_placed_on_plan_true_via_measured_edge_node_and_false_without_it():
    client, db, path = make_client_with_plans()
    try:
        r = client.post("/api/v2/plans", data={
            "name": "Тестовый план", "plan_kind": "single_line",
            "canvas_width": "1000", "canvas_height": "800"})
        assert r.status_code == 201, r.get_json()
        plan_id = r.get_json()["id"]

        n_in = _make_node(client, "n-in", "Ввод", "source")
        n_panel = _make_node(client, "n-panel", "Щит", "panel")
        r = _connect(client, n_in["id"], n_panel["id"])
        assert r.status_code == 201, r.get_json()
        e_in = r.get_json()

        p_in = _make_point(client, "p-in", "Счётчик ввода")
        r = _set_meter_on_edge(client, e_in["id"], p_in["id"])
        assert r.status_code == 200, r.get_json()

        # ДО размещения узла на карте — точка НЕ считается размещённой,
        # "нет плана" видна в сводке (placed_on_plan/location_path и т.п.
        # отдаёт /api/v2/structure/points, а не голый /api/v2/points —
        # см. api_v2.py v2_structure_points) -------------------------------
        r = client.get("/api/v2/structure/points")
        assert r.status_code == 200, r.get_json()
        item = next(x for x in r.get_json()["points"] if x["point_id"] == p_in["id"])
        assert item["placed_on_plan"] is False, item

        r = client.get("/api/v2/validation")
        assert r.status_code == 200, r.get_json()
        no_plan_ids = {x["point_id"] for x in r.get_json()["points_without_plan"]}
        assert p_in["id"] in no_plan_ids, r.get_json()["points_without_plan"]

        # разместить узел ВВОДА на карте (как «Плана v3» — kind='node') —
        # точка, измеряющая линию Ввод→Щит (инцидентную узлу «Ввод»),
        # должна тут же стать "размещена" ---------------------------------
        r = client.post(f"/api/v2/plans/{plan_id}/items", json={
            "kind": "node", "node_id": n_in["id"],
            "geometry": {"x": 50, "y": 50}, "coord_space": "canvas_xy_v2"})
        assert r.status_code == 201, r.get_json()

        r = client.get("/api/v2/structure/points")
        item = next(x for x in r.get_json()["points"] if x["point_id"] == p_in["id"])
        assert item["placed_on_plan"] is True, item

        r = client.get("/api/v2/validation")
        no_plan_ids = {x["point_id"] for x in r.get_json()["points_without_plan"]}
        assert p_in["id"] not in no_plan_ids, r.get_json()["points_without_plan"]

        print("[OK] placed_on_plan/no_plan для «Плана v3»: точка размещена, "
              "если размещён любой из узлов измеряемой ею линии (§5 задания)")
    finally:
        db.close(); os.unlink(path)


if __name__ == "__main__":
    test_e2e_input_panel_two_metered_consumers_and_one_unmetered_line()
    test_cycle_rejected_in_node_terms_and_legit_connection_still_works()
    test_second_power_source_rejected_and_legit_connection_still_works()
    test_add_consumer_from_load_rejected_and_from_panel_still_works()
    test_point_already_measures_another_edge_rejected_and_reassign_works()
    test_node_rename_and_location_text_and_empty_name_rejected()
    test_group_polygon_zone_accepted_point_polygon_still_rejected()
    test_placed_on_plan_true_via_measured_edge_node_and_false_without_it()
    print("\nВсе тесты «Плана v3» (Шаг 39) пройдены.")

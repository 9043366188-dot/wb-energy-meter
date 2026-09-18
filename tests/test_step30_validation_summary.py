"""Тесты Шага 30 (партия 3, задача 4): GET /api/v2/validation — сводка
незавершённой настройки (ТЗ §8.2/§13).

Самостоятельный скрипт (не pytest):
    python tests/test_step30_validation_summary.py

Методология партии 3 (docs/TZ-batch3-structure-inspector-legacy.md §5):
у каждой проверки «отказ»/«не в порядке» есть парная проверка, что
корректно настроенный объект в ту же категорию НЕ попадает — иначе легко
пропустить маршрут, который считает проблемой вообще всё (или ничего)."""

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
from wb_energy_meter.location_repo import LocationRepo
from wb_energy_meter.point_repo import MeteringPointRepo, MeterSourceRepo
from wb_energy_meter.binding_service import PointBindingRepo
from wb_energy_meter.topology_service import ElectricalNodeRepo, ElectricalEdgeRepo
from wb_energy_meter.group_repo_v2 import GroupRepoV2


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


def _insert_plan_item_for_point(db, point_id):
    """Минимальная строка plan_items для точки — напрямую SQL, в обход
    валидации геометрии (не предмет этого теста, покрыта test_step21)."""
    now = int(time.time())
    with db.transaction() as c:
        c.execute(
            "INSERT INTO site_plans (name, image_file, image_width, image_height, "
            "is_default, created_at, updated_at) VALUES (?, ?, ?, ?, 0, ?, ?)",
            ("Тестовый план", "test.png", 1000, 1000, now, now))
        plan_id = c.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        c.execute(
            "INSERT INTO plan_items (plan_id, kind, point_id, geometry, coord_space, "
            "sort_order, created_at, updated_at) "
            "VALUES (?, 'point', ?, '{\"x\":1,\"y\":1}', 'image_px_xy_v2', 0, ?, ?)",
            (plan_id, point_id, now, now))


def test_points_without_meter_partitioned_correctly():
    """ПРОБЛЕМА: точка без открытой primary-привязки попадает в
    points_without_meter. ЛЕГИТИМНЫЙ СЛУЧАЙ: точка с приводом — нет."""
    client, db, path = make_client()
    try:
        points = MeteringPointRepo(db)
        meters = MeterRepo(db, GroupRepo(db))
        sources = MeterSourceRepo(db)
        bindings = PointBindingRepo(db)

        bound = points.add(code="bound.1", name="С прибором")
        m = meters.add("dev-30-1", "Прибор")
        s = sources.open_source(m.id, "wb8-main", "dev-30-1")
        bindings.open_binding(bound.id, s.id, "total_3p", role="primary", valid_from=0)

        unbound = points.add(code="unbound.1", name="Без прибора")

        r = client.get("/api/v2/validation")
        assert r.status_code == 200, r.get_json()
        body = r.get_json()
        ids = {x["point_id"] for x in body["points_without_meter"]}
        assert unbound.id in ids, "точка без привязки должна быть в списке"
        assert bound.id not in ids, (
            "ЛЕГИТИМНЫЙ СЛУЧАЙ сломан: привязанная точка не должна считаться "
            "проблемой points_without_meter")
        print("[OK] points_without_meter: без привязки -> в списке, "
              "с привязкой -> легитимно отсутствует")
    finally:
        db.close(); os.unlink(path)


def test_points_without_location_partitioned_correctly():
    client, db, path = make_client()
    try:
        points = MeteringPointRepo(db)
        locations = LocationRepo(db)
        loc = locations.add(name="Цех 1", kind="room")

        placed = points.add(code="placed.1", name="Размещена",
                             installation_location_id=loc.id)
        unplaced = points.add(code="unplaced.1", name="Не размещена")

        r = client.get("/api/v2/validation")
        body = r.get_json()
        ids = {x["point_id"] for x in body["points_without_location"]}
        assert unplaced.id in ids
        assert placed.id not in ids, (
            "ЛЕГИТИМНЫЙ СЛУЧАЙ сломан: точка с местом не должна считаться "
            "проблемой points_without_location")
        print("[OK] points_without_location: без места -> в списке, "
              "с местом -> легитимно отсутствует")
    finally:
        db.close(); os.unlink(path)


def test_points_without_group_excludes_input_point():
    """ПРОБЛЕМА: точка вне всех групп — в списке. ЛЕГИТИМНЫЙ СЛУЧАЙ:
    точка ВНУТРИ группы — не в списке. Отдельно, §8.2 (задача 4): точка
    ввода объекта тоже вне групп, но проблемой считаться не должна —
    в списке её быть не должно, хотя формально она "без группы"."""
    client, db, path = make_client()
    try:
        points = MeteringPointRepo(db)
        groups = GroupRepoV2(db)
        nodes = ElectricalNodeRepo(db)
        edges = ElectricalEdgeRepo(db)

        grouped = points.add(code="grouped.1", name="В группе")
        g = groups.add(name="Группа 1")
        groups.add_member(g.id, grouped.id, valid_from=0)

        ungrouped = points.add(code="ungrouped.1", name="Вне групп")

        input_point = points.add(code="input.1", name="Ввод объекта")
        src_node = nodes.add(code="src", name="Источник", kind="source")
        panel_node = nodes.add(code="panel", name="Щит", kind="panel")
        e = edges.add_draft(src_node.id, panel_node.id, code="L1",
                             primary_point_id=input_point.id)
        edges.publish_edges([e.id])

        r = client.get("/api/v2/validation")
        body = r.get_json()
        ids = {x["point_id"] for x in body["points_without_group"]}
        assert ungrouped.id in ids
        assert grouped.id not in ids, (
            "ЛЕГИТИМНЫЙ СЛУЧАЙ сломан: точка в группе не должна считаться "
            "проблемой points_without_group")
        assert input_point.id not in ids, (
            "§8.2 задача 4: точка ввода не должна считаться проблемой "
            "'вне групп' — это её нормальное свойство, а не забытая настройка")
        print("[OK] points_without_group: вне группы -> в списке, в группе -> "
              "легитимно отсутствует, точка ввода легитимно исключена")
    finally:
        db.close(); os.unlink(path)


def test_points_without_plan_partitioned_correctly():
    client, db, path = make_client()
    try:
        points = MeteringPointRepo(db)
        on_plan = points.add(code="onplan.1", name="На плане")
        _insert_plan_item_for_point(db, on_plan.id)
        off_plan = points.add(code="offplan.1", name="Не на плане")

        r = client.get("/api/v2/validation")
        body = r.get_json()
        ids = {x["point_id"] for x in body["points_without_plan"]}
        assert off_plan.id in ids
        assert on_plan.id not in ids, (
            "ЛЕГИТИМНЫЙ СЛУЧАЙ сломан: точка на плане не должна считаться "
            "проблемой points_without_plan")
        print("[OK] points_without_plan: не на плане -> в списке, на плане -> "
              "легитимно отсутствует")
    finally:
        db.close(); os.unlink(path)


def test_nodes_and_edges_partitioned_correctly():
    """ПРОБЛЕМА: изолированный узел и связь без измерения — в списках.
    ЛЕГИТИМНЫЙ СЛУЧАЙ: связанный узел и измеренная связь — не в списках."""
    client, db, path = make_client()
    try:
        points = MeteringPointRepo(db)
        nodes = ElectricalNodeRepo(db)
        edges = ElectricalEdgeRepo(db)

        src = nodes.add(code="s1", name="Источник", kind="source")
        panel = nodes.add(code="p1", name="Щит", kind="panel")
        isolated = nodes.add(code="iso1", name="Изолированный узел", kind="panel")

        measure_point = points.add(code="measured.1", name="Измеряет связь")
        e_measured = edges.add_draft(src.id, panel.id, code="L1",
                                      primary_point_id=measure_point.id)
        # вторая связь без измерения: отдельная ветка на новую нагрузку
        load = nodes.add(code="l1", name="Нагрузка", kind="load")
        e_unmeasured = edges.add_draft(panel.id, load.id, code="L2")
        edges.publish_edges([e_measured.id, e_unmeasured.id])

        r = client.get("/api/v2/validation")
        body = r.get_json()

        node_ids = {x["node_id"] for x in body["nodes_without_edges"]}
        assert isolated.id in node_ids
        assert src.id not in node_ids and panel.id not in node_ids, (
            "ЛЕГИТИМНЫЙ СЛУЧАЙ сломан: связанные узлы не должны считаться "
            "изолированными")

        edge_ids = {x["edge_id"] for x in body["edges_without_measurement"]}
        assert e_unmeasured.id in edge_ids
        assert e_measured.id not in edge_ids, (
            "ЛЕГИТИМНЫЙ СЛУЧАЙ сломан: измеренная связь не должна считаться "
            "проблемой edges_without_measurement")
        print("[OK] nodes_without_edges / edges_without_measurement: "
              "изолированное/неизмеренное -> в списках, "
              "подключённое/измеренное -> легитимно отсутствует")
    finally:
        db.close(); os.unlink(path)


def test_issues_sorted_by_impact_not_alphabet():
    """§8.2: сортировка по влиянию, а не по алфавиту. no_meter (ранг 1)
    обязан идти раньше no_plan (ранг 5), даже если имя точки без плана
    алфавитно раньше имени точки без прибора."""
    client, db, path = make_client()
    try:
        points = MeteringPointRepo(db)
        # имя специально подобрано так, чтобы алфавитная сортировка дала
        # ОБРАТНЫЙ порядок относительно порядка по влиянию
        no_plan_point = points.add(code="aaa.no_plan", name="AAA без плана")
        no_meter_point = points.add(code="zzz.no_meter", name="ZZZ без прибора")
        # у него тоже нет ни плана — не мешает, просто попадёт в оба списка

        r = client.get("/api/v2/validation")
        body = r.get_json()
        kinds_in_order = [it["kind"] for it in body["issues"]
                           if it["entity_id"] in (no_plan_point.id, no_meter_point.id)]
        # no_meter (ранг 1) должен идти раньше no_plan (ранг 5) для ОБЕИХ точек,
        # несмотря на то что "AAA" алфавитно раньше "ZZZ"
        first_no_meter_pos = kinds_in_order.index("no_meter")
        first_no_plan_pos = kinds_in_order.index("no_plan")
        assert first_no_meter_pos < first_no_plan_pos, (
            f"сортировка не по влиянию: {kinds_in_order}")
        print("[OK] issues отсортированы по влиянию (no_meter раньше no_plan), "
              "не по алфавиту имени")
    finally:
        db.close(); os.unlink(path)


def test_validation_route_does_not_require_expected_revision():
    """ПРОБЛЕМА-негатив/легитимный: validation — только чтение, не
    предметная запись, поэтому НЕ требует expected_revision (в отличие
    от изменяющих маршрутов партии 2) — легитимный GET без каких-либо
    полей тела обязан работать без 409."""
    client, db, path = make_client()
    try:
        r = client.get("/api/v2/validation")
        assert r.status_code == 200, r.get_json()
        assert "issues" in r.get_json() and "total_issues" in r.get_json()
        print("[OK] GET /api/v2/validation не требует expected_revision "
              "(это не предметная запись)")
    finally:
        db.close(); os.unlink(path)


if __name__ == "__main__":
    test_points_without_meter_partitioned_correctly()
    test_points_without_location_partitioned_correctly()
    test_points_without_group_excludes_input_point()
    test_points_without_plan_partitioned_correctly()
    test_nodes_and_edges_partitioned_correctly()
    test_issues_sorted_by_impact_not_alphabet()
    test_validation_route_does_not_require_expected_revision()
    print("\nВсе тесты сводки незавершённой настройки (Шаг 30, партия 3, "
          "задача 4) пройдены.")

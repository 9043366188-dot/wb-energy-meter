"""Тесты Шага 31 (партия 3, задача 2): мастер переноса связей старой
модели планов (plan_links) в electrical_edges (ТЗ §2, строка про §31.5).

Самостоятельный скрипт (не pytest):
    python tests/test_step31_migration_wizard.py

Методология партии 3 (docs/TZ-batch3-structure-inspector-legacy.md §5):
каждая проверка «отказ» — в паре с проверкой, что легитимный запрос
после неё по-прежнему проходит."""

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
from wb_energy_meter.point_repo import MeteringPointRepo
from wb_energy_meter.topology_service import ElectricalNodeRepo, ElectricalEdgeRepo
from wb_energy_meter.plan_repo import PlanZoneRepo, PlanLinkRepo


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
    r = client.get("/api/v2/revision")
    assert r.status_code == 200, r.get_json()
    return r.get_json()["configuration_revision"]


def _raw_legacy_plan_with_zones_and_link(db, group_a_id, group_b_id,
                                          rated_current_a=None, label="Кабель 1"):
    """Минимальные строки старой модели плана напрямую SQL — валидация
    геометрии/картинки (test_step11/21) здесь не предмет теста, мастер
    переноса читает эти таблицы, а не создаёт их."""
    now = int(time.time())
    with db.transaction() as c:
        cur = c.execute(
            "INSERT INTO site_plans (name, image_file, image_width, image_height, "
            "is_default, created_at, updated_at) VALUES (?, ?, ?, ?, 0, ?, ?)",
            ("Старый план", "old.png", 1000, 1000, now, now))
        plan_id = cur.lastrowid
        cur = c.execute(
            "INSERT INTO plan_zones (plan_id, group_id, shape_type, geometry, "
            "created_at, updated_at) VALUES (?, ?, 'polygon', '{}', ?, ?)",
            (plan_id, group_a_id, now, now))
        zone_a_id = cur.lastrowid
        cur = c.execute(
            "INSERT INTO plan_zones (plan_id, group_id, shape_type, geometry, "
            "created_at, updated_at) VALUES (?, ?, 'polygon', '{}', ?, ?)",
            (plan_id, group_b_id, now, now))
        zone_b_id = cur.lastrowid
        cur = c.execute(
            "INSERT INTO plan_links (plan_id, from_zone_id, to_zone_id, "
            "rated_current_a, label, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (plan_id, zone_a_id, zone_b_id, rated_current_a, label, now, now))
        link_id = cur.lastrowid
    return plan_id, zone_a_id, zone_b_id, link_id


def test_get_legacy_links_lists_pending_with_zone_group_names():
    client, db, path = make_client()
    try:
        groups = GroupRepo(db)
        ga = groups.get_or_create("Цех 1")
        gb = groups.get_or_create("Щитовая")
        _plan_id, za, zb, link_id = _raw_legacy_plan_with_zones_and_link(
            db, ga.id, gb.id, rated_current_a=63.0, label="Кабель ЩР1-Цех1")

        r = client.get("/api/v2/migration/legacy-links")
        assert r.status_code == 200, r.get_json()
        links = r.get_json()["links"]
        assert len(links) == 1
        item = links[0]
        assert item["plan_link_id"] == link_id
        assert item["migration_status"] == "pending"
        assert item["migrated_edge_id"] is None
        assert item["from_zone"]["group_name"] == "Цех 1"
        assert item["to_zone"]["group_name"] == "Щитовая"
        assert item["rated_current_a"] == 63.0
        print("[OK] GET /api/v2/migration/legacy-links: связь видна, статус "
              "pending, подписи групп на концах верны")
    finally:
        db.close(); os.unlink(path)


def test_confirm_creates_draft_edge_with_new_and_existing_node():
    """Позитивный сценарий: один конец — новый узел (new_node), другой —
    уже существующий (node_id); точка учёта назначена измерением."""
    client, db, path = make_client()
    try:
        groups = GroupRepo(db)
        ga = groups.get_or_create("Цех 1")
        gb = groups.get_or_create("Щитовая")
        _plan_id, za, zb, link_id = _raw_legacy_plan_with_zones_and_link(
            db, ga.id, gb.id, rated_current_a=40.0, label="Кабель А")

        nodes = ElectricalNodeRepo(db)
        edges = ElectricalEdgeRepo(db)
        points = MeteringPointRepo(db)
        existing_panel = nodes.add(code="panel-existing", name="Щит существующий",
                                    kind="panel")
        measure_point = points.add(code="meas.1", name="Измеряет кабель А")

        rev = current_rev(client)
        r = client.post("/api/v2/migration/legacy-links/confirm", json={
            "expected_revision": rev,
            "confirmations": [{
                "plan_link_id": link_id,
                "from_node": {"new_node": {"code": "panel-new", "name": "Щит новый",
                                            "kind": "panel"}},
                "to_node": {"node_id": existing_panel.id},
                "primary_point_id": measure_point.id,
            }],
        })
        assert r.status_code == 200, r.get_json()
        body = r.get_json()
        assert isinstance(body["configuration_revision"], int)
        results = body["results"]
        assert len(results) == 1
        res = results[0]
        assert res["plan_link_id"] == link_id
        assert res["status"] == "created"
        edge_id = res["edge_id"]

        edge = edges.get_by_id(edge_id)
        assert edge is not None
        assert edge.state == "draft", "мастер создаёт ЧЕРНОВИК, не публикует сам"
        assert edge.primary_point_id == measure_point.id
        assert edge.to_node_id == existing_panel.id
        assert edge.rated_current_a == 40.0, "унаследовано из plan_link, не переопределено"
        assert edge.name == "Кабель А", "имя связи взято из label plan_link по умолчанию"

        new_node = nodes.get_by_id(edge.from_node_id)
        assert new_node is not None and new_node.code == "panel-new"
        print("[OK] confirm: связь создана черновиком, новый+существующий узлы, "
              "точка учёта и параметры унаследованы верно")
    finally:
        db.close(); os.unlink(path)


def test_confirm_is_idempotent_no_duplicate_edge():
    """Повторное подтверждение ТОЙ ЖЕ связи не создаёт вторую — статус
    already_migrated, edge_id тот же (§2: 'повторный запуск не создаёт
    дублей')."""
    client, db, path = make_client()
    try:
        groups = GroupRepo(db)
        ga = groups.get_or_create("A")
        gb = groups.get_or_create("B")
        _plan_id, za, zb, link_id = _raw_legacy_plan_with_zones_and_link(db, ga.id, gb.id)
        nodes = ElectricalNodeRepo(db)
        edges = ElectricalEdgeRepo(db)

        n1 = nodes.add(code="n1", name="N1", kind="panel")
        n2 = nodes.add(code="n2", name="N2", kind="panel")

        conf = {"plan_link_id": link_id, "from_node": {"node_id": n1.id},
                "to_node": {"node_id": n2.id}}

        rev = current_rev(client)
        r1 = client.post("/api/v2/migration/legacy-links/confirm",
                          json={"expected_revision": rev, "confirmations": [conf]})
        assert r1.status_code == 200, r1.get_json()
        edge_id_1 = r1.get_json()["results"][0]["edge_id"]

        rev2 = current_rev(client)
        r2 = client.post("/api/v2/migration/legacy-links/confirm",
                          json={"expected_revision": rev2, "confirmations": [conf]})
        assert r2.status_code == 200, r2.get_json()
        res2 = r2.get_json()["results"][0]
        assert res2["status"] == "already_migrated"
        assert res2["edge_id"] == edge_id_1

        all_edges = edges.list_drafts()
        matching = [e for e in all_edges if e.id == edge_id_1]
        assert len(matching) == 1, "повтор не должен был создать вторую связь"
        print("[OK] повторное подтверждение той же связи идемпотентно "
              "(already_migrated, без дубля)")
    finally:
        db.close(); os.unlink(path)


def test_batch_atomic_bad_link_rejects_whole_batch_but_retry_succeeds():
    """ОТКАЗ: пачка с несуществующим plan_link_id откатывается целиком —
    ни одна связь пачки не создаётся, даже валидная. ЛЕГИТИМНЫЙ ЗАПРОС
    РАБОТАЕТ: повтор без некорректного элемента — создаёт обе валидные."""
    client, db, path = make_client()
    try:
        groups = GroupRepo(db)
        ga = groups.get_or_create("A")
        gb = groups.get_or_create("B")
        _plan_id, za, zb, link_id_1 = _raw_legacy_plan_with_zones_and_link(
            db, ga.id, gb.id, label="Связь 1")
        _plan_id2, za2, zb2, link_id_2 = _raw_legacy_plan_with_zones_and_link(
            db, ga.id, gb.id, label="Связь 2")
        nodes = ElectricalNodeRepo(db)
        edges = ElectricalEdgeRepo(db)
        n1 = nodes.add(code="n1", name="N1", kind="panel")
        n2 = nodes.add(code="n2", name="N2", kind="panel")

        rev = current_rev(client)
        r = client.post("/api/v2/migration/legacy-links/confirm", json={
            "expected_revision": rev,
            "confirmations": [
                {"plan_link_id": link_id_1, "from_node": {"node_id": n1.id},
                 "to_node": {"node_id": n2.id}},
                {"plan_link_id": 999999, "from_node": {"node_id": n1.id},
                 "to_node": {"node_id": n2.id}},
            ],
        })
        assert r.status_code == 400, r.get_json()
        assert r.get_json()["code"] == "bad_request"
        assert len(edges.list_drafts()) == 0, (
            "ОТКАЗ обязан откатить ВСЮ пачку, включая валидный первый элемент")

        # ЛЕГИТИМНЫЙ ЗАПРОС РАБОТАЕТ: та же пара без битого элемента
        rev2 = current_rev(client)
        r2 = client.post("/api/v2/migration/legacy-links/confirm", json={
            "expected_revision": rev2,
            "confirmations": [
                {"plan_link_id": link_id_1, "from_node": {"node_id": n1.id},
                 "to_node": {"node_id": n2.id}},
                {"plan_link_id": link_id_2, "from_node": {"node_id": n1.id},
                 "to_node": {"node_id": n2.id}},
            ],
        })
        assert r2.status_code == 200, r2.get_json()
        assert len(r2.get_json()["results"]) == 2
        assert len(edges.list_drafts()) == 2
        print("[OK] ОТКАЗ: пачка с несуществующей связью откатывается целиком "
              "(0 черновиков) + легитимный повтор без битого элемента создаёт обе")
    finally:
        db.close(); os.unlink(path)


def test_confirm_without_expected_revision_409_but_with_it_succeeds():
    client, db, path = make_client()
    try:
        groups = GroupRepo(db)
        ga = groups.get_or_create("A")
        gb = groups.get_or_create("B")
        _plan_id, za, zb, link_id = _raw_legacy_plan_with_zones_and_link(db, ga.id, gb.id)
        nodes = ElectricalNodeRepo(db)
        edges = ElectricalEdgeRepo(db)
        n1 = nodes.add(code="n1", name="N1", kind="panel")
        n2 = nodes.add(code="n2", name="N2", kind="panel")
        conf = {"plan_link_id": link_id, "from_node": {"node_id": n1.id},
                "to_node": {"node_id": n2.id}}

        r = client.post("/api/v2/migration/legacy-links/confirm",
                         json={"confirmations": [conf]})
        assert r.status_code == 409, r.get_json()
        assert len(edges.list_drafts()) == 0

        rev = current_rev(client)
        r = client.post("/api/v2/migration/legacy-links/confirm",
                         json={"expected_revision": rev, "confirmations": [conf]})
        assert r.status_code == 200, r.get_json()
        assert len(edges.list_drafts()) == 1
        print("[OK] ОТКАЗ без expected_revision -> 409 (ничего не создано) "
              "+ легитимный запрос с ревизией -> 200")
    finally:
        db.close(); os.unlink(path)


def test_legacy_zone_id_shared_across_batch_resolves_to_same_new_node():
    """Внутри ОДНОЙ пачки: первая связь заводит новый узел для зоны и
    запоминает его (legacy_zone_id); вторая связь той же пачки,
    ссылающаяся на ТУ ЖЕ зону через legacy_zone_id (без own new_node),
    обязана получить ТОТ ЖЕ узел, а не создать второй."""
    client, db, path = make_client()
    try:
        groups = GroupRepo(db)
        ga = groups.get_or_create("Общая зона")
        gb1 = groups.get_or_create("Потребитель 1")
        gb2 = groups.get_or_create("Потребитель 2")
        # обе связи выходят из ОДНОЙ и той же зоны (общий щит), к разным концам
        now = int(time.time())
        with db.transaction() as c:
            cur = c.execute(
                "INSERT INTO site_plans (name, image_file, image_width, image_height, "
                "is_default, created_at, updated_at) VALUES (?, ?, ?, ?, 0, ?, ?)",
                ("План", "p.png", 1000, 1000, now, now))
            plan_id = cur.lastrowid
            cur = c.execute(
                "INSERT INTO plan_zones (plan_id, group_id, shape_type, geometry, "
                "created_at, updated_at) VALUES (?, ?, 'polygon', '{}', ?, ?)",
                (plan_id, ga.id, now, now))
            shared_zone_id = cur.lastrowid
            cur = c.execute(
                "INSERT INTO plan_zones (plan_id, group_id, shape_type, geometry, "
                "created_at, updated_at) VALUES (?, ?, 'polygon', '{}', ?, ?)",
                (plan_id, gb1.id, now, now))
            zone_b1 = cur.lastrowid
            cur = c.execute(
                "INSERT INTO plan_zones (plan_id, group_id, shape_type, geometry, "
                "created_at, updated_at) VALUES (?, ?, 'polygon', '{}', ?, ?)",
                (plan_id, gb2.id, now, now))
            zone_b2 = cur.lastrowid
            cur = c.execute(
                "INSERT INTO plan_links (plan_id, from_zone_id, to_zone_id, "
                "label, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (plan_id, shared_zone_id, zone_b1, "Связь 1", now, now))
            link_1 = cur.lastrowid
            cur = c.execute(
                "INSERT INTO plan_links (plan_id, from_zone_id, to_zone_id, "
                "label, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (plan_id, shared_zone_id, zone_b2, "Связь 2", now, now))
            link_2 = cur.lastrowid

        nodes = ElectricalNodeRepo(db)
        n_b1 = nodes.add(code="nb1", name="Nb1", kind="panel")
        n_b2 = nodes.add(code="nb2", name="Nb2", kind="panel")

        rev = current_rev(client)
        r = client.post("/api/v2/migration/legacy-links/confirm", json={
            "expected_revision": rev,
            "confirmations": [
                {"plan_link_id": link_1,
                 "from_node": {"legacy_zone_id": shared_zone_id,
                               "new_node": {"code": "shared", "name": "Общий щит",
                                            "kind": "panel"}},
                 "to_node": {"node_id": n_b1.id}},
                {"plan_link_id": link_2,
                 "from_node": {"legacy_zone_id": shared_zone_id},
                 "to_node": {"node_id": n_b2.id}},
            ],
        })
        assert r.status_code == 200, r.get_json()
        results = r.get_json()["results"]
        from_node_1 = results[0]["from_node_id"]
        from_node_2 = results[1]["from_node_id"]
        assert from_node_1 == from_node_2, (
            "вторая связь той же зоны должна получить ТОТ ЖЕ узел, "
            "а не создать новый (иначе на повторе плодятся дубли по зоне)")
        all_panel_nodes = [n for n in nodes.list_all() if n.code == "shared"]
        assert len(all_panel_nodes) == 1
        print("[OK] legacy_zone_id общей зоны внутри одной пачки резолвится "
              "в один и тот же узел, не создаёт дубль")
    finally:
        db.close(); os.unlink(path)


def test_old_model_untouched_after_migration():
    """Регрессия: старая модель (plan_links/plan_zones) не удаляется и
    не меняется переносом — решение о выводе из эксплуатации принимает
    пользователь (§2 задания, «в эту партию не удалять»)."""
    client, db, path = make_client()
    try:
        groups = GroupRepo(db)
        ga = groups.get_or_create("A")
        gb = groups.get_or_create("B")
        _plan_id, za, zb, link_id = _raw_legacy_plan_with_zones_and_link(
            db, ga.id, gb.id, rated_current_a=25.0, label="Оригинал")
        nodes = ElectricalNodeRepo(db)
        n1 = nodes.add(code="n1", name="N1", kind="panel")
        n2 = nodes.add(code="n2", name="N2", kind="panel")

        original_link = PlanLinkRepo(db).get_by_id(link_id)

        rev = current_rev(client)
        r = client.post("/api/v2/migration/legacy-links/confirm", json={
            "expected_revision": rev,
            "confirmations": [{"plan_link_id": link_id,
                                "from_node": {"node_id": n1.id},
                                "to_node": {"node_id": n2.id}}],
        })
        assert r.status_code == 200, r.get_json()

        after_link = PlanLinkRepo(db).get_by_id(link_id)
        assert after_link is not None, "старая связь не должна быть удалена"
        assert after_link.label == original_link.label
        assert after_link.rated_current_a == original_link.rated_current_a
        assert after_link.from_zone_id == original_link.from_zone_id
        assert after_link.to_zone_id == original_link.to_zone_id
        zone_a = PlanZoneRepo(db).get_by_id(za)
        assert zone_a is not None, "старая зона не должна быть удалена"
        print("[OK] старая модель (plan_links/plan_zones) не изменилась после переноса")
    finally:
        db.close(); os.unlink(path)


if __name__ == "__main__":
    test_get_legacy_links_lists_pending_with_zone_group_names()
    test_confirm_creates_draft_edge_with_new_and_existing_node()
    test_confirm_is_idempotent_no_duplicate_edge()
    test_batch_atomic_bad_link_rejects_whole_batch_but_retry_succeeds()
    test_confirm_without_expected_revision_409_but_with_it_succeeds()
    test_legacy_zone_id_shared_across_batch_resolves_to_same_new_node()
    test_old_model_untouched_after_migration()
    print("\nВсе тесты мастера переноса legacy-связей (Шаг 31, партия 3, "
          "задача 2) пройдены.")

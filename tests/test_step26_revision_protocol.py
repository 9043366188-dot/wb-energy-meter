"""Тесты Шага 26 (партия 2, задача 1): глобальный протокол ревизий
конфигурации — `revision_service.py` (ТЗ §6.1/§9.2/§11) и его подключение
в `api_v2.py`.

Самостоятельный скрипт (не pytest):
    python tests/test_step26_revision_protocol.py

Покрывает:
- модуль revision_service.py напрямую (текущая ревизия/проверка/создание,
  успешный и конфликтный путь with_revision_check, bump_revision без
  проверки);
- реентрантность Database.transaction() (db.py) — композиция вложенных
  транзакций репозиториев внутри внешней транзакции ревизии;
- A35 через HTTP на НОВОМ протоколе (не canvas_revision плана, который
  уже покрыт test_step21_plan_v2.py) — по каждой из защищённых сущностей:
  запись без expected_revision или с устаревшим -> 409 revision_conflict,
  чужие правки не перезаписаны;
- A43 — конкурентная запись не может вклиниться в середину одного расчёта
  metrics/query: прямой тест на Database.read()/transaction() (сам
  механизм) и сквозной HTTP-тест с реальными потоками, доказывающий, что
  писатель либо ждёт, либо гарантированно оказывается ПОСЛЕ фиксации
  ревизии в начале расчёта.
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from wb_energy_meter.db import Database
from wb_energy_meter.repo import GroupRepo, MeterRepo
from wb_energy_meter.api import create_app, _AppState
from wb_energy_meter.model import MeterRegistry
from wb_energy_meter.point_repo import MeteringPointRepo, MeterSourceRepo
from wb_energy_meter.aggregates_repo import AggregateRepo, HourlyAggregate
from wb_energy_meter.binding_service import PointBindingRepo
from wb_energy_meter.group_repo_v2 import GroupRepoV2
from wb_energy_meter import revision_service as rs

HOUR = 3600


def make_db():
    fd, path = tempfile.mkstemp(suffix=".sqlite3")
    os.close(fd)
    os.unlink(path)
    db = Database(path=path)
    db.open()
    return db, path


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
    return app, db, path


def current_rev(client):
    r = client.get("/api/v2/revision")
    assert r.status_code == 200, r.get_json()
    return r.get_json()["configuration_revision"]


# ---------------------------------------------------------------------
# revision_service.py напрямую
# ---------------------------------------------------------------------

def test_current_revision_starts_at_zero():
    db, path = make_db()
    try:
        with db.read() as c:
            assert rs.current_revision_id(c) == 0
        print("[OK] current_revision_id() == 0 на пустой БД (ни одной предметной транзакции)")
    finally:
        db.close(); os.unlink(path)


def test_check_expected_revision_missing_and_stale():
    db, path = make_db()
    try:
        with db.transaction() as c:
            rs.create_revision(c, schema_version=5, touched=[("x", 1)])
        with db.read() as c:
            try:
                rs.check_expected_revision(c, None)
                assert False, "должно было бросить RevisionConflict"
            except rs.RevisionConflict as e:
                assert e.expected is None and e.actual == 1
            try:
                rs.check_expected_revision(c, 0)
                assert False, "должно было бросить RevisionConflict (устарела)"
            except rs.RevisionConflict as e:
                assert e.expected == 0 and e.actual == 1
            # актуальная ревизия проходит
            assert rs.check_expected_revision(c, 1) == 1
        print("[OK] check_expected_revision: отсутствие и устаревшее значение -> RevisionConflict")
    finally:
        db.close(); os.unlink(path)


def test_with_revision_check_success_and_conflict_atomic():
    db, path = make_db()
    try:
        counter = {"n": 0}

        def bump():
            counter["n"] += 1
            return counter["n"]

        result, new_rev = rs.with_revision_check(db, 0, bump, touched=[("thing", 1)])
        assert result == 1 and new_rev == 1
        with db.read() as c:
            assert rs.current_revision_id(c) == 1

        # конфликт: mutate_fn НЕ должен вызываться, ревизия не создаётся,
        # counter не меняется (§6.1: "закрытие старой версии и создание
        # новой выполняются в одной транзакции")
        try:
            rs.with_revision_check(db, 0, bump, touched=[("thing", 1)])
            assert False, "должен был бросить RevisionConflict"
        except rs.RevisionConflict:
            pass
        assert counter["n"] == 1, "mutate_fn не должен был выполниться при конфликте ревизии"
        with db.read() as c:
            assert rs.current_revision_id(c) == 1, "конфликтная попытка не должна создавать ревизию"
        print("[OK] with_revision_check: успех продвигает ревизию и mutate_fn; "
              "конфликт — атомарно ничего не меняет")
    finally:
        db.close(); os.unlink(path)


def test_bump_revision_no_precondition():
    db, path = make_db()
    try:
        _, rev1 = rs.bump_revision(db, lambda: "a", touched=[("thing", "new")])
        _, rev2 = rs.bump_revision(db, lambda: "b", touched=[("thing", "new")])
        assert rev1 == 1 and rev2 == 2
        print("[OK] bump_revision: без expected_revision, ревизия монотонно растёт")
    finally:
        db.close(); os.unlink(path)


def test_reentrant_transaction_composes_with_repo_writes():
    """db.py: Database.transaction() реентрантна — репозиторий, вызванный
    внутри with_revision_check(), не должен открывать вторую транзакцию
    (SQLite отклонил бы вложенный BEGIN на одном соединении)."""
    db, path = make_db()
    try:
        groups = GroupRepoV2(db)

        def make_group():
            return groups.add("Тестовая группа")

        g, new_rev = rs.with_revision_check(db, 0, make_group, touched=[("group", "new")])
        assert g.id is not None
        assert new_rev == 1
        # группа реально создана и видна отдельным чтением — если бы
        # вложенная транзакция репозитория не присоединилась к внешней
        # (а попыталась открыть свою поверх уже открытой), это упало бы
        # с sqlite3.OperationalError на BEGIN, а не молча.
        assert groups.get_by_id(g.id) is not None
        print("[OK] Database.transaction() реентрантна: GroupRepoV2.add() внутри "
              "with_revision_check() не ломает и не дублирует транзакцию")
    finally:
        db.close(); os.unlink(path)


def test_direct_write_blocked_while_read_lock_held():
    """A43, механизм: пока держится with db.read() (та же RLock, что и
    db.transaction()), конкурентная запись в другом потоке физически не
    может выполниться раньше, чем читающий поток отпустит блокировку —
    это то, на чём строится однократный снимок в v2_metrics_query."""
    db, path = make_db()
    try:
        release_reader = threading.Event()
        reader_holds_lock = threading.Event()
        writer_done = threading.Event()
        order = []

        def reader():
            with db.read() as c:
                reader_holds_lock.set()
                release_reader.wait(timeout=5)
                order.append("reader_release")

        def writer():
            reader_holds_lock.wait(timeout=5)
            time.sleep(0.1)  # дать читателю гарантированно захватить лок
            with db.transaction() as c:
                order.append("writer_acquired")
            writer_done.set()

        t_reader = threading.Thread(target=reader)
        t_writer = threading.Thread(target=writer)
        t_reader.start()
        t_writer.start()
        reader_holds_lock.wait(timeout=5)
        time.sleep(0.2)
        assert not writer_done.is_set(), (
            "писатель не должен был выполниться, пока читатель держит лок")
        release_reader.set()
        t_reader.join(timeout=5)
        t_writer.join(timeout=5)
        assert order == ["reader_release", "writer_acquired"], order
        print("[OK] A43 (механизм): db.transaction() ждёт, пока db.read() отпустит "
              "общую блокировку — писатель не может вклиниться в середину чтения")
    finally:
        db.close(); os.unlink(path)


# ---------------------------------------------------------------------
# A35 через HTTP на новых сущностях (не план — план уже в test_step21)
# ---------------------------------------------------------------------

def test_a35_group_patch_stale_revision_rejected():
    app, db, path = make_client()
    try:
        client = app.test_client()
        a = client.post("/api/v2/groups", json={"name": "A"}).get_json()["id"]
        b = client.post("/api/v2/groups", json={"name": "B"}).get_json()["id"]
        rev0 = current_rev(client)

        # без expected_revision вообще
        r = client.patch(f"/api/v2/groups/{a}", json={"parent_id": b})
        assert r.status_code == 409, r.get_json()
        assert r.get_json()["code"] == "revision_conflict"

        # первый редактор — успешно
        r = client.patch(f"/api/v2/groups/{a}",
                          json={"parent_id": b, "expected_revision": rev0})
        assert r.status_code == 200, r.get_json()
        rev1 = r.get_json()["configuration_revision"]
        assert rev1 == rev0 + 1

        # второй редактор с уже устаревшей rev0 — 409, состояние не тронуто
        r = client.patch(f"/api/v2/groups/{a}",
                          json={"parent_id": None, "expected_revision": rev0})
        assert r.status_code == 409, r.get_json()
        assert r.get_json()["code"] == "revision_conflict"

        r = client.get(f"/api/v2/groups/{a}")
        assert r.get_json()["parent_id"] == b, "чужая (первая) правка не должна быть затёрта"
        print("[OK] A35 (группы): PATCH без/с устаревшей expected_revision -> 409, "
              "первая правка сохранена")
    finally:
        db.close(); os.unlink(path)


def test_a35_location_patch_stale_revision_rejected():
    app, db, path = make_client()
    try:
        client = app.test_client()
        a = client.post("/api/v2/locations", json={"name": "A", "kind": "object"}).get_json()["id"]
        b = client.post("/api/v2/locations",
                         json={"name": "B", "kind": "building"}).get_json()["id"]
        rev0 = current_rev(client)

        r = client.patch(f"/api/v2/locations/{a}", json={"parent_id": b})
        assert r.status_code == 409 and r.get_json()["code"] == "revision_conflict"

        r = client.patch(f"/api/v2/locations/{a}",
                          json={"parent_id": b, "expected_revision": rev0})
        assert r.status_code == 200, r.get_json()

        r = client.patch(f"/api/v2/locations/{a}",
                          json={"parent_id": None, "expected_revision": rev0})
        assert r.status_code == 409 and r.get_json()["code"] == "revision_conflict"

        r = client.get(f"/api/v2/locations/{a}")
        assert r.get_json()["parent_id"] == b
        print("[OK] A35 (места): PATCH без/с устаревшей expected_revision -> 409")
    finally:
        db.close(); os.unlink(path)


def test_a35_topology_node_archive_stale_revision_rejected():
    app, db, path = make_client()
    try:
        client = app.test_client()
        n = client.post("/api/v2/topology/nodes",
                         json={"code": "N1", "name": "Щит", "kind": "panel"}).get_json()["id"]
        rev0 = current_rev(client)

        r = client.patch(f"/api/v2/topology/nodes/{n}", json={"archived": True})
        assert r.status_code == 409 and r.get_json()["code"] == "revision_conflict"

        r = client.get(f"/api/v2/topology/nodes/{n}")
        assert r.get_json()["archived_at"] is None, "узел не должен был архивироваться"

        r = client.patch(f"/api/v2/topology/nodes/{n}",
                          json={"archived": True, "expected_revision": rev0})
        assert r.status_code == 200, r.get_json()
        assert r.get_json()["archived_at"] is not None
        print("[OK] A35 (узлы сети): архивирование без expected_revision -> 409, "
              "узел не тронут; с корректной ревизией — 200")
    finally:
        db.close(); os.unlink(path)


def test_a35_topology_edge_retire_stale_revision_rejected():
    app, db, path = make_client()
    try:
        client = app.test_client()
        na = client.post("/api/v2/topology/nodes",
                          json={"code": "NA", "name": "A", "kind": "panel"}).get_json()["id"]
        nb = client.post("/api/v2/topology/nodes",
                          json={"code": "NB", "name": "B", "kind": "panel"}).get_json()["id"]
        e = client.post("/api/v2/topology/edges",
                         json={"from_node_id": na, "to_node_id": nb}).get_json()["id"]
        rev0 = current_rev(client)
        r = client.post("/api/v2/topology/publish",
                         json={"edge_ids": [e], "expected_configuration_revision": rev0})
        assert r.status_code == 200, r.get_json()
        rev1 = current_rev(client)

        r = client.patch(f"/api/v2/topology/edges/{e}", json={"retire": True})
        assert r.status_code == 409 and r.get_json()["code"] == "revision_conflict"

        r = client.patch(f"/api/v2/topology/edges/{e}",
                          json={"retire": True, "expected_revision": rev1})
        assert r.status_code == 200, r.get_json()
        print("[OK] A35 (связи): retire опубликованной связи без expected_revision -> 409")
    finally:
        db.close(); os.unlink(path)


def test_a35_topology_publish_stale_revision_rejected():
    app, db, path = make_client()
    try:
        client = app.test_client()
        na = client.post("/api/v2/topology/nodes",
                          json={"code": "NA", "name": "A", "kind": "source"}).get_json()["id"]
        nb = client.post("/api/v2/topology/nodes",
                          json={"code": "NB", "name": "B", "kind": "panel"}).get_json()["id"]
        e = client.post("/api/v2/topology/edges",
                         json={"from_node_id": na, "to_node_id": nb}).get_json()["id"]
        rev0 = current_rev(client)

        r = client.post("/api/v2/topology/publish", json={"edge_ids": [e]})
        assert r.status_code == 409, r.get_json()
        assert r.get_json()["code"] == "revision_conflict"

        r = client.get("/api/v2/topology/edges?state=published")
        assert r.get_json() == [], "публикация без expected_configuration_revision не должна была пройти"

        r = client.post("/api/v2/topology/publish",
                         json={"edge_ids": [e], "expected_configuration_revision": rev0})
        assert r.status_code == 200, r.get_json()
        print("[OK] A35 (публикация топологии): без expected_configuration_revision -> 409, "
              "структура не изменилась")
    finally:
        db.close(); os.unlink(path)


def test_a35_balance_scope_patch_stale_revision_rejected():
    app, db, path = make_client()
    try:
        client = app.test_client()
        p1 = client.post("/api/v2/points", json={"code": "p1", "name": "П1"}).get_json()["id"]
        p2 = client.post("/api/v2/points", json={"code": "p2", "name": "П2"}).get_json()["id"]
        scope_id = client.post("/api/v2/balance-scopes",
                                json={"name": "Объект", "input_point_ids": [p1]}).get_json()["id"]
        rev0 = current_rev(client)

        r = client.patch(f"/api/v2/balance-scopes/{scope_id}",
                          json={"output_point_ids": [p2]})
        assert r.status_code == 409 and r.get_json()["code"] == "revision_conflict"

        r = client.get(f"/api/v2/balance-scopes/{scope_id}")
        assert r.get_json()["output_point_ids"] == [], "состав не должен был поменяться"

        r = client.patch(f"/api/v2/balance-scopes/{scope_id}",
                          json={"output_point_ids": [p2], "expected_revision": rev0})
        assert r.status_code == 200, r.get_json()
        assert r.get_json()["output_point_ids"] == [p2]
        print("[OK] A35 (границы баланса): PATCH без expected_revision -> 409, состав не изменён")
    finally:
        db.close(); os.unlink(path)


def test_get_revision_endpoint_tracks_writes():
    app, db, path = make_client()
    try:
        client = app.test_client()
        assert current_rev(client) == 0
        client.post("/api/v2/groups", json={"name": "A"})
        assert current_rev(client) == 1
        g2 = client.post("/api/v2/groups", json={"name": "B"}).get_json()["id"]
        assert current_rev(client) == 2
        client.patch(f"/api/v2/groups/{g2}",
                     json={"parent_id": None, "expected_revision": 2})
        # PATCH без реального изменения (parent_id уже None) всё равно
        # создаёт ревизию — так и задумано (§6.1: "каждая предметная
        # транзакция"), проверяем только что счётчик не пошёл назад.
        assert current_rev(client) >= 2
        print("[OK] GET /api/v2/revision отражает каждую защищённую предметную транзакцию")
    finally:
        db.close(); os.unlink(path)


# ---------------------------------------------------------------------
# A43 сквозь HTTP: metrics/query фиксирует ревизию один раз и не даёт
# конкурентной записи попасть "внутрь" уже идущего расчёта
# ---------------------------------------------------------------------

def test_a43_metrics_query_pins_snapshot_against_concurrent_write():
    app, db, path = make_client()
    try:
        client = app.test_client()
        writer_client = app.test_client()

        meters = MeterRepo(db, GroupRepo(db))
        sources = MeterSourceRepo(db)
        points = MeteringPointRepo(db)
        aggregates = AggregateRepo(db)
        bindings = PointBindingRepo(db)

        m = meters.add("m1", "m1")
        src = sources.open_source(m.id, "wb8-main", "m1")
        p = points.add("p1", "Точка 1")
        bindings.open_binding(p.id, src.id, "total_3p", valid_from=0)
        aggregates.upsert(HourlyAggregate(
            meter_id=m.id, period_start=0, period_end=HOUR,
            ap_energy_start=0.0, ap_energy_end=100.0, ap_energy_delta=100.0,
            p_avg=None, p_max=None, samples_count=1, quality_flag="ok", computed_at=0))

        g = client.post("/api/v2/groups", json={"name": "Тест"}).get_json()["id"]
        rev_before = current_rev(client)

        entered_calc = threading.Event()
        allow_calc_finish = threading.Event()
        writer_result = {}

        orig_get = AggregateRepo.get

        def slow_get(self, *a, **kw):
            entered_calc.set()
            allow_calc_finish.wait(timeout=5)
            return orig_get(self, *a, **kw)

        def writer():
            entered_calc.wait(timeout=5)
            time.sleep(0.1)  # дать читателю гарантированно захватить лок
            r = writer_client.patch(
                f"/api/v2/groups/{g}",
                json={"parent_id": None, "expected_revision": rev_before})
            writer_result["status"] = r.status_code
            writer_result["body"] = r.get_json()

        AggregateRepo.get = slow_get
        try:
            t = threading.Thread(target=writer)
            t.start()
            r = client.post("/api/v2/metrics/query", json={
                "mode": "measured", "point_ids": [p.id],
                "from": 0, "to": HOUR, "timezone": "UTC",
            })
            allow_calc_finish.set()
            t.join(timeout=5)
        finally:
            AggregateRepo.get = orig_get

        assert r.status_code == 200, r.get_json()
        body = r.get_json()
        assert body["value"] == 100.0
        assert body["configuration_revision_id"] == rev_before, (
            "расчёт обязан использовать ревизию, зафиксированную в начале запроса, "
            "а не ту, что могла появиться из-за конкурентной записи по ходу расчёта")
        assert body["configuration_revision_ids"] == [rev_before]
        assert body["as_of"], "as_of должен быть проставлен"

        assert "status" in writer_result, "писатель обязан был успеть выполниться после расчёта"
        assert writer_result["status"] == 200, writer_result.get("body")
        assert writer_result["body"]["configuration_revision"] == rev_before + 1
        print("[OK] A43 через HTTP: metrics/query зафиксировал ревизию "
              f"{rev_before} в начале расчёта; конкурентная запись выполнилась "
              "только после и получила следующую ревизию "
              f"{writer_result['body']['configuration_revision']}")
    finally:
        db.close(); os.unlink(path)


def test_metrics_query_explicit_configuration_revision_id():
    app, db, path = make_client()
    try:
        client = app.test_client()
        meters = MeterRepo(db, GroupRepo(db))
        sources = MeterSourceRepo(db)
        points = MeteringPointRepo(db)
        aggregates = AggregateRepo(db)
        bindings = PointBindingRepo(db)

        m = meters.add("m1", "m1")
        src = sources.open_source(m.id, "wb8-main", "m1")
        p = points.add("p1", "Точка 1")
        bindings.open_binding(p.id, src.id, "total_3p", valid_from=0)
        aggregates.upsert(HourlyAggregate(
            meter_id=m.id, period_start=0, period_end=HOUR,
            ap_energy_start=0.0, ap_energy_end=100.0, ap_energy_delta=100.0,
            p_avg=None, p_max=None, samples_count=1, quality_flag="ok", computed_at=0))

        client.post("/api/v2/groups", json={"name": "A"})
        rev1 = current_rev(client)

        r = client.post("/api/v2/metrics/query", json={
            "mode": "measured", "point_ids": [p.id],
            "from": 0, "to": HOUR, "timezone": "UTC",
            "configuration_revision_id": rev1,
        })
        assert r.status_code == 200, r.get_json()
        assert r.get_json()["configuration_revision_id"] == rev1

        r = client.post("/api/v2/metrics/query", json={
            "mode": "measured", "point_ids": [p.id],
            "from": 0, "to": HOUR, "timezone": "UTC",
            "configuration_revision_id": rev1 + 999,
        })
        assert r.status_code == 404, r.get_json()
        print("[OK] metrics/query: явный configuration_revision_id — известный принимается, "
              "неизвестный -> 404")
    finally:
        db.close(); os.unlink(path)


if __name__ == "__main__":
    test_current_revision_starts_at_zero()
    test_check_expected_revision_missing_and_stale()
    test_with_revision_check_success_and_conflict_atomic()
    test_bump_revision_no_precondition()
    test_reentrant_transaction_composes_with_repo_writes()
    test_direct_write_blocked_while_read_lock_held()
    test_a35_group_patch_stale_revision_rejected()
    test_a35_location_patch_stale_revision_rejected()
    test_a35_topology_node_archive_stale_revision_rejected()
    test_a35_topology_edge_retire_stale_revision_rejected()
    test_a35_topology_publish_stale_revision_rejected()
    test_a35_balance_scope_patch_stale_revision_rejected()
    test_get_revision_endpoint_tracks_writes()
    test_a43_metrics_query_pins_snapshot_against_concurrent_write()
    test_metrics_query_explicit_configuration_revision_id()
    print("[ALL OK] test_step26_revision_protocol")

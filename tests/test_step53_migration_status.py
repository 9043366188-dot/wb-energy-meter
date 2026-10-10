"""Тесты партии 11, этап 11.5: `GET /api/v2/migration/status`.

Самостоятельный скрипт (не pytest):
    python tests/test_step53_migration_status.py

Ответ — то, что Кир присылает после пилота (docs/pilot-checklist.md), поэтому
числа сверяются с независимым источником (прямой SQL / соседние маршруты), а
не только с самим ответом:
  M1  пустая БД: версия схемы, поколение 1 (ключей kv нет), нули;
  M2  после `POST /api/v2/admin/migrate-legacy`: счётчики migration_map,
      legacy_meters, поколение 2 и метка первой записи;
  M3  после подтверждения ОДНОЙ связи мастера: unconfirmed уменьшилось на 1,
      migration_map получил plan_links → electrical_edges; повтор подтверждения
      счётчик не меняет; число совпадает с pending в legacy-links;
  M4  значения ключей берутся из kv как есть (в т.ч. «мусор» → поколение 1);
  M5  запрос — чтение: ревизия не движется; он не ждёт открытую запись и не
      видит её незафиксированных строк (11.3), после фиксации — видит.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import threading
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from wb_energy_meter import domain_generation
from wb_energy_meter.api import create_app, _AppState
from wb_energy_meter.db import Database
from wb_energy_meter.model import MeterRegistry
from wb_energy_meter.repo import GroupRepo, KvRepo, MeterRepo
from wb_energy_meter.topology_service import ElectricalNodeRepo


def make_client():
    tmp = tempfile.mkdtemp(prefix="wb_migstatus_")
    db = Database(path=os.path.join(tmp, "t.sqlite3"))
    db.open()
    groups = GroupRepo(db)
    state = _AppState(
        registry=MeterRegistry(), meters_repo=MeterRepo(db, groups),
        groups_repo=groups, is_mqtt_connected=lambda: False,
        mqtt_message_count=lambda: 0, mqtt_error_count=lambda: 0,
        wb_db_client=None, consumption_service=None, started_at=time.time(),
        db=db, plans_dir=os.path.join(tmp, "plans"))
    app = create_app(state)
    return app.test_client(), db, tmp


def status(client):
    r = client.get("/api/v2/migration/status")
    assert r.status_code == 200, (r.status_code, r.get_json())
    return r.get_json()


def current_rev(client):
    return client.get("/api/v2/revision").get_json()["configuration_revision"]


def _legacy_link(db, group_a_id, group_b_id, label):
    """Строки старой модели плана напрямую SQL (как в test_step31): мастер
    читает эти таблицы, а не создаёт их."""
    now = int(time.time())
    with db.transaction() as c:
        plan_id = c.execute(
            "INSERT INTO site_plans (name, image_file, image_width, image_height, "
            "is_default, created_at, updated_at) VALUES (?, ?, ?, ?, 0, ?, ?)",
            (f"Старый план {label}", "old.png", 1000, 1000, now, now)).lastrowid
        zone_a = c.execute(
            "INSERT INTO plan_zones (plan_id, group_id, shape_type, geometry, "
            "created_at, updated_at) VALUES (?, ?, 'polygon', '{}', ?, ?)",
            (plan_id, group_a_id, now, now)).lastrowid
        zone_b = c.execute(
            "INSERT INTO plan_zones (plan_id, group_id, shape_type, geometry, "
            "created_at, updated_at) VALUES (?, ?, 'polygon', '{}', ?, ?)",
            (plan_id, group_b_id, now, now)).lastrowid
        return c.execute(
            "INSERT INTO plan_links (plan_id, from_zone_id, to_zone_id, "
            "rated_current_a, label, created_at, updated_at) "
            "VALUES (?, ?, ?, NULL, ?, ?, ?)",
            (plan_id, zone_a, zone_b, label, now, now)).lastrowid


def _cleanup(db, tmp):
    db.close()
    shutil.rmtree(tmp, ignore_errors=True)


def test_m1_empty_db_reports_generation_1_and_zero_counts():
    client, db, tmp = make_client()
    try:
        s = status(client)
        with db.read() as c:
            expected_schema = c.execute(
                "SELECT MAX(version) AS v FROM schema_migrations").fetchone()["v"]
        assert s["schema_version"] == expected_schema and expected_schema >= 5, s
        assert s["code_generation"] == domain_generation.CURRENT_GENERATION == 2
        # ключей kv нет → «поколение 1», как требует docs/migration-plan-v2.md §7
        assert s["domain_model_generation"] == 1
        assert s["minimum_reader_generation"] == 1
        assert s["model_v2_first_write_revision"] is None
        assert s["migration_map"] == []
        assert s["legacy_meters"] == {"total": 0, "migrated": 0, "not_migrated": 0}
        assert s["legacy_links"] == {"total": 0, "confirmed": 0, "unconfirmed": 0}
        assert sorted(s) == sorted([
            "schema_version", "code_generation", "domain_model_generation",
            "minimum_reader_generation", "model_v2_first_write_revision",
            "migration_map", "legacy_meters", "legacy_links"]), sorted(s)
        print("[OK] M1: пустая БД — схема %d, поколение 1, все счётчики нулевые, "
              "набор ключей зафиксирован" % expected_schema)
    finally:
        _cleanup(db, tmp)


def test_m2_after_migrate_legacy_counts_generation_and_marker():
    client, db, tmp = make_client()
    try:
        mr = MeterRepo(db, GroupRepo(db))
        mr.add(device_id="wb-map3e_10", display_name="Счётчик 10", group="Цех 1")
        mr.add(device_id="wb-map3e_11", display_name="Счётчик 11")
        mr.add(device_id="wb-map3e_12", display_name="Счётчик 12", group="Цех 1")
        mr.update(device_id="wb-map3e_12", enabled=False)   # отключённый тоже переносится

        before = status(client)
        assert before["legacy_meters"] == {"total": 3, "migrated": 0, "not_migrated": 3}
        assert before["migration_map"] == []
        assert before["domain_model_generation"] == 1
        assert before["model_v2_first_write_revision"] is None, \
            "до переноса записей в v2 не было — метки нет"

        r = client.post("/api/v2/admin/migrate-legacy", json={"confirm": True})
        assert r.status_code == 200, r.get_json()
        assert len(r.get_json()["migrated_points"]) == 3

        after = status(client)
        assert after["migration_map"] == [
            {"legacy_table": "meters", "new_table": "metering_points", "count": 3}], after
        assert after["legacy_meters"] == {"total": 3, "migrated": 3, "not_migrated": 0}
        # независимая сверка с прямым SQL
        with db.read() as c:
            n_map = c.execute(
                "SELECT COUNT(*) AS n FROM migration_map WHERE legacy_table='meters'"
            ).fetchone()["n"]
            n_points = c.execute(
                "SELECT COUNT(*) AS n FROM metering_points").fetchone()["n"]
        assert n_map == 3 == n_points
        assert after["domain_model_generation"] == 2
        assert after["minimum_reader_generation"] == 2
        marker = after["model_v2_first_write_revision"]
        assert isinstance(marker, str) and marker, after

        # повторный перенос ничего не меняет в сводке (идемпотентность)
        assert client.post("/api/v2/admin/migrate-legacy",
                           json={"confirm": True}).status_code == 200
        again = status(client)
        assert again == after, (again, after)
        print("[OK] M2: после migrate-legacy — meters→metering_points=3, "
              "перенесено 3 из 3, поколение 2, метка первой записи есть; "
              "повторный перенос сводку не меняет")
    finally:
        _cleanup(db, tmp)


def test_m3_confirming_one_link_reduces_unconfirmed_by_one():
    client, db, tmp = make_client()
    try:
        groups = GroupRepo(db)
        ga = groups.get_or_create("Цех 1")
        gb = groups.get_or_create("Щитовая")
        link1 = _legacy_link(db, ga.id, gb.id, "Кабель 1")
        link2 = _legacy_link(db, gb.id, ga.id, "Кабель 2")

        s0 = status(client)
        assert s0["legacy_links"] == {"total": 2, "confirmed": 0, "unconfirmed": 2}
        assert s0["migration_map"] == []

        nodes = ElectricalNodeRepo(db)
        n1 = nodes.add(code="n1", name="Щит 1", kind="panel")
        n2 = nodes.add(code="n2", name="Щит 2", kind="panel")
        conf = {"plan_link_id": link1, "from_node": {"node_id": n1.id},
                "to_node": {"node_id": n2.id}}
        r = client.post("/api/v2/migration/legacy-links/confirm", json={
            "expected_revision": current_rev(client), "confirmations": [conf]})
        assert r.status_code == 200, r.get_json()
        assert r.get_json()["results"][0]["status"] == "created"

        s1 = status(client)
        assert s1["legacy_links"] == {"total": 2, "confirmed": 1, "unconfirmed": 1}, s1
        assert s1["migration_map"] == [
            {"legacy_table": "plan_links", "new_table": "electrical_edges", "count": 1}], s1

        # то же число видит соседний маршрут мастера
        links = client.get("/api/v2/migration/legacy-links").get_json()["links"]
        pending = [i["plan_link_id"] for i in links if i["migration_status"] == "pending"]
        assert pending == [link2], pending
        assert len(pending) == s1["legacy_links"]["unconfirmed"]

        # повторное подтверждение той же связи (already_migrated) счёт не меняет
        r = client.post("/api/v2/migration/legacy-links/confirm", json={
            "expected_revision": current_rev(client), "confirmations": [conf]})
        assert r.status_code == 200, r.get_json()
        assert r.get_json()["results"][0]["status"] == "already_migrated"
        assert status(client)["legacy_links"] == s1["legacy_links"]

        # подтверждаем вторую — неподтверждённых нет
        conf2 = {"plan_link_id": link2, "from_node": {"node_id": n2.id},
                 "to_node": {"node_id": n1.id}}
        r = client.post("/api/v2/migration/legacy-links/confirm", json={
            "expected_revision": current_rev(client), "confirmations": [conf2]})
        assert r.status_code == 200, r.get_json()
        s2 = status(client)
        assert s2["legacy_links"] == {"total": 2, "confirmed": 2, "unconfirmed": 0}, s2
        assert s2["migration_map"] == [
            {"legacy_table": "plan_links", "new_table": "electrical_edges", "count": 2}]
        print("[OK] M3: 2 связи → подтверждена одна: unconfirmed 2→1, "
              "plan_links→electrical_edges=1, совпадает с pending в legacy-links; "
              "повтор не меняет, после второй unconfirmed=0")
    finally:
        _cleanup(db, tmp)


def test_m4_keys_come_from_kv_as_stored():
    client, db, tmp = make_client()
    try:
        # метка и поколения записываются тем же кодом, что и боевая первая
        # запись в v2 (значение метки хранится как JSON-строка)
        with db.transaction() as c:
            assert domain_generation.mark_v2_domain_write(c, revision="1790000000")
        # нечитаемое значение руками (как «ручная правка» из docstring
        # domain_generation._coerce_generation)
        KvRepo(db).set(domain_generation.KEY_MIN_READER_GENERATION, "мусор")
        s = status(client)
        assert s["domain_model_generation"] == 2
        assert s["minimum_reader_generation"] == 1, \
            "нечитаемое значение трактуется как поколение 1 (domain_generation._coerce_generation)"
        assert s["model_v2_first_write_revision"] == "1790000000"
        print("[OK] M4: ключи поколений берутся из kv как есть, мусор → поколение 1")
    finally:
        _cleanup(db, tmp)


def test_m5_status_is_read_only_and_does_not_wait_for_open_write():
    client, db, tmp = make_client()
    try:
        rev = current_rev(client)
        status(client)
        status(client)
        assert current_rev(client) == rev, "GET migration/status не должен двигать ревизию"

        result = {}

        def _reader():
            t0 = time.monotonic()
            result["status"] = status(client)
            result["took"] = time.monotonic() - t0

        now = int(time.time())
        with db.transaction() as c:
            c.execute(
                "INSERT INTO migration_map (legacy_table, legacy_id, new_table, "
                "new_id, migration_version, created_at) VALUES "
                "('meters', 999, 'metering_points', 1, 5, ?)", (now,))
            th = threading.Thread(target=_reader, daemon=True)
            th.start()
            th.join(timeout=5.0)
            assert not th.is_alive(), \
                "чтение сводки зависло, пока открыта запись (читатель ждёт писателя)"
            assert result["status"]["migration_map"] == [], \
                "читатель видит только зафиксированное: строка открытой записи не видна"
        # запись зафиксирована — следующий запрос её видит
        assert status(client)["migration_map"] == [
            {"legacy_table": "meters", "new_table": "metering_points", "count": 1}]
        print("[OK] M5: сводка не двигает ревизию, не ждёт открытую запись "
              "(%.0f мс) и видит только зафиксированное" % (result["took"] * 1000))
    finally:
        _cleanup(db, tmp)


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
    print(f"[OK] test_step53_migration_status: {len(tests)} тестов")

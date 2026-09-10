"""Тесты Шага 14 (этап B): репозитории точек учёта и мест (ТЗ §4.1/§4.2).

Самостоятельный скрипт (не pytest):
    python tests/test_step14_point_location_repo.py

Проверяет point_repo.py (MeterSourceRepo/MeteringPointRepo) и
location_repo.py (LocationRepo) на реальной БД со схемой v2 (миграция 005):
CRUD, версионирование через *_state_versions/*_parent_bindings, защиту от
циклов в дереве мест, и что осмысленные сообщения об ошибках не путают
нарушение UNIQUE с нарушением FOREIGN KEY.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from wb_energy_meter.db import Database
from wb_energy_meter.repo import GroupRepo, MeterRepo
from wb_energy_meter.location_repo import LocationRepo
from wb_energy_meter.point_repo import MeterSourceRepo, MeteringPointRepo


def make_db():
    fd, path = tempfile.mkstemp(suffix=".sqlite3")
    os.close(fd)
    os.unlink(path)  # Database создаёт файл сама
    db = Database(path=path)
    db.open()
    return db, path


# --- LocationRepo -----------------------------------------------------

def test_location_crud_and_tree():
    db, path = make_db()
    try:
        repo = LocationRepo(db)
        obj = repo.add("Главный корпус", "object")
        floor = repo.add("Этаж 2", "floor", parent_id=obj.id)
        room = repo.add("Комната 201", "room", parent_id=floor.id, code="R201")

        assert repo.get_by_id(room.id).parent_id == floor.id
        assert repo.get_by_code("R201").id == room.id

        children = repo.list_children(obj.id)
        assert [c.id for c in children] == [floor.id]

        path_ids = [loc.id for loc in repo.path_to_root(room.id)]
        assert path_ids == [obj.id, floor.id, room.id]

        print("[OK] LocationRepo: add/get_by_id/get_by_code/list_children/path_to_root")
    finally:
        db.close()
        os.unlink(path)


def test_location_duplicate_code_rejected():
    db, path = make_db()
    try:
        repo = LocationRepo(db)
        repo.add("Корпус А", "object", code="A")
        try:
            repo.add("Корпус А-2", "object", code="A")
        except ValueError as e:
            assert "код" in str(e).lower() or "уже существует" in str(e)
            print("[OK] LocationRepo.add: дубликат кода отклонён понятной ошибкой")
        else:
            raise AssertionError("ожидался ValueError при дубликате кода")
    finally:
        db.close()
        os.unlink(path)


def test_location_missing_parent_rejected():
    db, path = make_db()
    try:
        repo = LocationRepo(db)
        try:
            repo.add("Сирота", "room", parent_id=999999)
        except ValueError as e:
            assert "999999" in str(e)
            print("[OK] LocationRepo.add: несуществующий parent_id отклонён")
        else:
            raise AssertionError("ожидался ValueError")
    finally:
        db.close()
        os.unlink(path)


def test_location_cycle_rejected():
    db, path = make_db()
    try:
        repo = LocationRepo(db)
        a = repo.add("A", "object")
        b = repo.add("B", "building", parent_id=a.id)
        c = repo.add("C", "floor", parent_id=b.id)

        # Попытка сделать A ребёнком C создаёт цикл A -> B -> C -> A
        try:
            repo.set_parent(a.id, c.id)
        except ValueError as e:
            assert "цикл" in str(e).lower()
            print("[OK] LocationRepo.set_parent: цикл в дереве мест отклонён")
        else:
            raise AssertionError("ожидался ValueError о цикле")

        # Само-родитель тоже цикл
        try:
            repo.set_parent(a.id, a.id)
        except ValueError as e:
            assert "цикл" in str(e).lower()
            print("[OK] LocationRepo.set_parent: self-parent отклонён как цикл")
        else:
            raise AssertionError("ожидался ValueError о цикле (self-parent)")
    finally:
        db.close()
        os.unlink(path)


def test_location_set_parent_updates_history_and_cache():
    db, path = make_db()
    try:
        repo = LocationRepo(db)
        a = repo.add("Здание A", "object")
        b = repo.add("Здание B", "object")
        room = repo.add("Переносимая комната", "room", parent_id=a.id)

        repo.set_parent(room.id, b.id)
        assert repo.get_by_id(room.id).parent_id == b.id

        with db.read() as c:
            rows = c.execute(
                "SELECT parent_id, valid_from, valid_to FROM location_parent_bindings "
                "WHERE location_id = ? ORDER BY id", (room.id,)
            ).fetchall()
        assert len(rows) == 2, "должно быть 2 записи истории: закрытая + открытая"
        assert rows[0]["parent_id"] == a.id and rows[0]["valid_to"] is not None
        assert rows[1]["parent_id"] == b.id and rows[1]["valid_to"] is None
        print("[OK] LocationRepo.set_parent: история в location_parent_bindings корректна")
    finally:
        db.close()
        os.unlink(path)


def test_location_archive_blocked_by_active_children():
    db, path = make_db()
    try:
        repo = LocationRepo(db)
        parent = repo.add("Родитель", "object")
        repo.add("Ребёнок", "building", parent_id=parent.id)

        try:
            repo.archive(parent.id)
        except ValueError as e:
            assert "дочерн" in str(e).lower()
            print("[OK] LocationRepo.archive: блокируется активными детьми")
        else:
            raise AssertionError("ожидался ValueError")
    finally:
        db.close()
        os.unlink(path)


# --- MeteringPointRepo / MeterSourceRepo -------------------------------

def test_point_add_and_state_versions():
    db, path = make_db()
    try:
        points = MeteringPointRepo(db)
        loc_repo = LocationRepo(db)
        loc = loc_repo.add("Цех 1", "room")

        p = points.add("cons.tsex1", "Цех 1 — ввод", installation_location_id=loc.id)
        assert p.enabled == 1
        assert p.installation_location_id == loc.id

        points.set_enabled(p.id, False)
        p2 = points.get_by_id(p.id)
        assert p2.enabled == 0

        with db.read() as c:
            rows = c.execute(
                "SELECT enabled, valid_to FROM point_state_versions "
                "WHERE point_id = ? ORDER BY id", (p.id,)
            ).fetchall()
        assert len(rows) == 2
        assert rows[0]["enabled"] == 1 and rows[0]["valid_to"] is not None
        assert rows[1]["enabled"] == 0 and rows[1]["valid_to"] is None
        print("[OK] MeteringPointRepo.add/set_enabled: кэш и point_state_versions согласованы")

        # Идемпотентность set_enabled
        before = points.get_by_id(p.id).updated_at
        points.set_enabled(p.id, False)
        with db.read() as c:
            n = c.execute(
                "SELECT COUNT(*) AS n FROM point_state_versions WHERE point_id = ?",
                (p.id,)
            ).fetchone()["n"]
        assert n == 2, "повторный set_enabled(False) не должен создавать новую версию"
        print("[OK] MeteringPointRepo.set_enabled: идемпотентен при повторном вызове")
    finally:
        db.close()
        os.unlink(path)


def test_point_duplicate_code_vs_bad_location_error_messages():
    db, path = make_db()
    try:
        points = MeteringPointRepo(db)
        points.add("cons.server", "Сервер")

        try:
            points.add("cons.server", "Дубликат")
        except ValueError as e:
            assert "код" in str(e).lower()
            print("[OK] MeteringPointRepo.add: дубликат кода -> понятная ошибка про код")
        else:
            raise AssertionError("ожидался ValueError")

        try:
            points.add("cons.new", "Новая точка", installation_location_id=424242)
        except ValueError as e:
            assert "installation_location_id" in str(e) or "424242" in str(e)
            assert "код" not in str(e).lower(), (
                "ошибка про несуществующее место не должна маскироваться под "
                "'дубликат кода' — именно это чинили в ревью"
            )
            print("[OK] MeteringPointRepo.add: несуществующий installation_location_id "
                  "-> отдельная понятная ошибка, не спутана с дубликатом кода")
        else:
            raise AssertionError("ожидался ValueError")
    finally:
        db.close()
        os.unlink(path)


def test_point_archive():
    db, path = make_db()
    try:
        points = MeteringPointRepo(db)
        p = points.add("cons.stanok", "Станок 1")
        points.archive(p.id)
        p2 = points.get_by_id(p.id)
        assert p2.enabled == 0
        assert p2.archived_at is not None
        print("[OK] MeteringPointRepo.archive: enabled=0 и archived_at проставлены")
    finally:
        db.close()
        os.unlink(path)


def test_meter_source_lifecycle():
    db, path = make_db()
    try:
        meters = MeterRepo(db, GroupRepo(db))
        sources = MeterSourceRepo(db)

        meter = meters.add("cons.tsex1", "Цех 1")

        src1 = sources.open_source(meter.id, "wb8-main", "cons.tsex1")
        assert sources.get_current(meter.id).id == src1.id

        # Переприсвоить на новый адрес — старый должен закрыться
        src2 = sources.reassign_source(meter.id, "wb8-main", "cons.tsex1-v2")
        current = sources.get_current(meter.id)
        assert current.id == src2.id
        history = sources.list_for_meter(meter.id)
        assert len(history) == 2
        assert history[0].valid_to is not None
        assert history[1].valid_to is None
        print("[OK] MeterSourceRepo: open_source/reassign_source/get_current/list_for_meter")

    finally:
        db.close()
        os.unlink(path)


def test_meter_source_duplicate_address_vs_bad_meter_id_error_messages():
    db, path = make_db()
    try:
        meters = MeterRepo(db, GroupRepo(db))
        sources = MeterSourceRepo(db)

        m1 = meters.add("cons.a", "A")
        m2 = meters.add("cons.b", "B")
        sources.open_source(m1.id, "wb8-main", "addr.shared")

        try:
            sources.open_source(m2.id, "wb8-main", "addr.shared")
        except ValueError as e:
            assert "занят" in str(e).lower()
            print("[OK] MeterSourceRepo.open_source: занятый адрес -> понятная ошибка")
        else:
            raise AssertionError("ожидался ValueError про занятый адрес")

        try:
            sources.open_source(999999, "wb8-main", "addr.new")
        except ValueError as e:
            assert "999999" in str(e) or "meter_id" in str(e).lower()
            assert "занят" not in str(e).lower(), (
                "ошибка про несуществующий meter_id не должна маскироваться "
                "под 'адрес уже занят' — именно этот баг чинили в ревью"
            )
            print("[OK] MeterSourceRepo.open_source: несуществующий meter_id -> "
                  "отдельная понятная ошибка, не спутана с занятым адресом")
        else:
            raise AssertionError("ожидался ValueError про несуществующий прибор")
    finally:
        db.close()
        os.unlink(path)


if __name__ == "__main__":
    test_location_crud_and_tree()
    test_location_duplicate_code_rejected()
    test_location_missing_parent_rejected()
    test_location_cycle_rejected()
    test_location_set_parent_updates_history_and_cache()
    test_location_archive_blocked_by_active_children()
    test_point_add_and_state_versions()
    test_point_duplicate_code_vs_bad_location_error_messages()
    test_point_archive()
    test_meter_source_lifecycle()
    test_meter_source_duplicate_address_vs_bad_meter_id_error_messages()
    print("\nВсе тесты point_repo/location_repo (Шаг 14) пройдены.")

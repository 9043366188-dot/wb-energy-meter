"""Тесты Шага 22 (этап F): протокол поколений домена
(docs/migration-plan-v2.md §7, wb_energy_meter/domain_generation.py).

Самостоятельный скрипт (не pytest):
    python tests/test_step22_domain_generation.py

Проверяет:
- значения по умолчанию (LEGACY_GENERATION) на БД без объявленных kv-ключей;
- устойчивость к «мусору» в значении ключа (ручная правка/старые записи);
- атомарность mark_v2_domain_write (все три ключа — в одной транзакции,
  откат транзакции не должен оставить частично применённые ключи);
- идемпотентность (маркер выставляется только один раз, ревизия не «уезжает»
  при повторных вызовах)."""

from __future__ import annotations

import os
import sys
import tempfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from wb_energy_meter.db import Database
from wb_energy_meter.repo import KvRepo
from wb_energy_meter import domain_generation as dg
from wb_energy_meter.point_repo import MeteringPointRepo


def make_db():
    fd, path = tempfile.mkstemp(suffix=".sqlite3")
    os.close(fd)
    os.unlink(path)
    db = Database(path=path)
    db.open()
    return db, path


def test_defaults_on_db_without_keys():
    """docs/migration-plan-v2.md §7 п.2: «по умолчанию 1 для БД без ключа»."""
    db, path = make_db()
    try:
        kv = KvRepo(db)
        assert dg.get_domain_generation(kv) == dg.LEGACY_GENERATION == 1
        assert dg.get_min_reader_generation(kv) == dg.LEGACY_GENERATION == 1
        assert dg.get_v2_first_write_marker(kv) is None
        print("[OK] test_defaults_on_db_without_keys")
    finally:
        db.close(); os.unlink(path)


def test_coerce_generation_ignores_garbage():
    """Битое/нечисловое значение ключа (ручная правка) не должно валить
    приложение — откатываемся к LEGACY_GENERATION, самому безопасному
    значению (никого не заблокирует лишний раз)."""
    db, path = make_db()
    try:
        kv = KvRepo(db)
        kv.set(dg.KEY_DOMAIN_GENERATION, "не число")
        assert dg.get_domain_generation(kv) == dg.LEGACY_GENERATION

        kv.set(dg.KEY_MIN_READER_GENERATION, None)
        assert dg.get_min_reader_generation(kv) == dg.LEGACY_GENERATION

        # валидное числовое значение (в т.ч. как "голая" строка) — читается
        # нормально
        with db.transaction() as c:
            c.execute(
                "INSERT INTO kv (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (dg.KEY_DOMAIN_GENERATION, "3", 0),
            )
        assert dg.get_domain_generation(kv) == 3
        print("[OK] test_coerce_generation_ignores_garbage")
    finally:
        db.close(); os.unlink(path)


def test_mark_v2_domain_write_sets_all_three_keys_atomically():
    db, path = make_db()
    try:
        kv = KvRepo(db)
        with db.transaction() as c:
            first_time = dg.mark_v2_domain_write(c, revision="rev-1")
        assert first_time is True

        assert dg.get_v2_first_write_marker(kv) == "rev-1"
        assert dg.get_domain_generation(kv) == dg.CURRENT_GENERATION
        assert dg.get_min_reader_generation(kv) == dg.CURRENT_GENERATION
        print("[OK] test_mark_v2_domain_write_sets_all_three_keys_atomically")
    finally:
        db.close(); os.unlink(path)


def test_mark_v2_domain_write_is_idempotent():
    """Повторный вызов (например, при повторной миграции после сбоя) не
    должен «уезжать» датой/ревизией — первая запись должна и остаться
    первой (докстринг mark_v2_domain_write)."""
    db, path = make_db()
    try:
        kv = KvRepo(db)
        with db.transaction() as c:
            assert dg.mark_v2_domain_write(c, revision="rev-1") is True
        with db.transaction() as c:
            assert dg.mark_v2_domain_write(c, revision="rev-2") is False

        assert dg.get_v2_first_write_marker(kv) == "rev-1"
        print("[OK] test_mark_v2_domain_write_is_idempotent")
    finally:
        db.close(); os.unlink(path)


def test_mark_v2_domain_write_rolls_back_with_its_transaction():
    """Атомарность §7 п.2: если та же транзакция, что и предметная запись,
    откатывается (исключение после mark_v2_domain_write, до COMMIT) — ни
    один из трёх ключей не должен остаться в БД. Иначе можно было бы
    получить БД, где generation уже поднят, а сама запись, ради которой
    это делалось, не сохранилась."""
    db, path = make_db()
    try:
        kv = KvRepo(db)

        class _Boom(Exception):
            pass

        try:
            with db.transaction() as c:
                dg.mark_v2_domain_write(c, revision="rev-1")
                raise _Boom("симуляция сбоя той же транзакции, что и запись")
        except _Boom:
            pass

        assert dg.get_v2_first_write_marker(kv) is None
        assert dg.get_domain_generation(kv) == dg.LEGACY_GENERATION
        assert dg.get_min_reader_generation(kv) == dg.LEGACY_GENERATION

        # И последующий настоящий вызов всё ещё считается первым:
        with db.transaction() as c:
            assert dg.mark_v2_domain_write(c, revision="rev-2") is True
        assert dg.get_v2_first_write_marker(kv) == "rev-2"
        print("[OK] test_mark_v2_domain_write_rolls_back_with_its_transaction")
    finally:
        db.close(); os.unlink(path)


def test_mark_v2_domain_write_atomic_with_domain_row():
    """Интеграционная проверка формулировки докстринга: маркер и доменная
    запись, вставленные в ОДНОЙ транзакции, либо обе видны, либо (при сбое)
    обе откатываются — ровно так, как это будут использовать реальные
    вызывающие (point_repo.add и т.п.)."""
    db, path = make_db()
    try:
        kv = KvRepo(db)
        with db.transaction() as c:
            c.execute(
                "INSERT INTO kv (key, value, updated_at) VALUES (?, ?, 0)",
                ("_test_marker_probe", '"да, вставлено"'),
            )
            dg.mark_v2_domain_write(c, revision="rev-1")

        assert kv.get("_test_marker_probe") == "да, вставлено"
        assert dg.get_v2_first_write_marker(kv) == "rev-1"
        print("[OK] test_mark_v2_domain_write_atomic_with_domain_row")
    finally:
        db.close(); os.unlink(path)


def test_metering_point_repo_add_marks_v2_domain_write():
    """Сквозная проверка реального места подключения (не только
    domain_generation.py в изоляции): MeteringPointRepo.add — самый
    вероятный первый настоящий v2-write (в т.ч. вызывается из
    legacy_migration.migrate_meters_and_groups для каждого meters.id,
    см. wb_energy_meter/legacy_migration.py) — должен поднимать
    generation/маркер сразу при первой созданной точке, и не трогать их
    повторно при последующих."""
    db, path = make_db()
    try:
        kv = KvRepo(db)
        points = MeteringPointRepo(db)

        assert dg.get_domain_generation(kv) == dg.LEGACY_GENERATION
        assert dg.get_v2_first_write_marker(kv) is None

        p1 = points.add("p1", "Точка 1")
        assert dg.get_domain_generation(kv) == dg.CURRENT_GENERATION
        assert dg.get_min_reader_generation(kv) == dg.CURRENT_GENERATION
        marker_after_first = dg.get_v2_first_write_marker(kv)
        assert marker_after_first is not None

        points.add("p2", "Точка 2")
        assert dg.get_v2_first_write_marker(kv) == marker_after_first, (
            "маркер первой записи не должен 'уезжать' при создании "
            "последующих точек"
        )
        print("[OK] test_metering_point_repo_add_marks_v2_domain_write")
    finally:
        db.close(); os.unlink(path)


if __name__ == "__main__":
    test_defaults_on_db_without_keys()
    test_coerce_generation_ignores_garbage()
    test_mark_v2_domain_write_sets_all_three_keys_atomically()
    test_mark_v2_domain_write_is_idempotent()
    test_mark_v2_domain_write_rolls_back_with_its_transaction()
    test_mark_v2_domain_write_atomic_with_domain_row()
    test_metering_point_repo_add_marks_v2_domain_write()
    print("[ALL OK] test_step22_domain_generation")

"""Тесты Шага 50 (партия 11, этап 11.3, docs/TZ-finish-plan.md §4):
чтение и запись в слое БД не блокируют друг друга.

Самостоятельный скрипт (не pytest):
    python tests/test_step50_db_read_concurrency.py

Было (docs/load-test-2026-09.md, «Находка 3»): Database.read() и
Database.transaction() держали ОДИН RLock на всё время блока, поэтому
тяжёлый расчёт внутри `with db.read()` не давал писать никому, включая
агрегатор (ожидание записи до ~450 мс во время reports/query).

Стало: запись — одно соединение-писатель под блокировкой, чтение — пул
отдельных соединений `PRAGMA query_only=ON`, каждый `with db.read()` —
одна читающая транзакция (BEGIN … COMMIT) и потому один снимок БД (A43).

Сценарии (первые пять — из задания §4, 11.3):
1. запись из основного потока не ждёт долгого `read()` в другом потоке;
2. внутри одного `read()` после чужого коммита читается прежнее значение
   (A43), после выхода из блока — новое;
3. `read()` внутри `transaction()` видит свою незакоммиченную строку, а
   `read()` другого потока — нет;
4. `close()` закрывает все соединения пула; повторный `open()` работает;
5. `":memory:"` работает как раньше (одно соединение).
Дополнительно: запись через соединение из `read()` падает (query_only),
пул ограничен, соединение возвращается в пул при исключении, вложенный
`read()` переиспользует то же соединение, смешанная нагрузка без ошибок."""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import threading
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from wb_energy_meter.db import Database

HOLD_S = 1.0           # сколько «тяжёлый расчёт» держит read()
WRITE_MEDIAN_S = 0.1   # бюджет записи из задания §4, 11.3
WRITE_MAX_S = 0.5      # выброс допустим, но не «ждали чтение»
JOIN_S = 10.0


def make_db(**kw):
    fd, path = tempfile.mkstemp(suffix=".sqlite3")
    os.close(fd)
    os.unlink(path)
    db = Database(path=path, **kw)
    db.open()
    return db, path


def cleanup(db, path):
    db.close()
    for suffix in ("", "-wal", "-shm"):
        try:
            os.unlink(path + suffix)
        except OSError:
            pass


def kv_set(db, key, value):
    with db.transaction() as c:
        c.execute(
            "INSERT INTO kv (key, value, updated_at) VALUES (?, ?, 0) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)))


def kv_get(c, key):
    row = c.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    return None if row is None else row["value"]


def test_write_does_not_wait_for_long_read():
    """Сценарий 1: запись из основного потока завершается быстро, пока
    другой поток держит read() целую секунду. «До исправления» первая
    запись ждала весь HOLD_S."""
    db, path = make_db()
    try:
        kv_set(db, "seed", "0")
        inside = threading.Event()
        release = threading.Event()
        errors = []

        def heavy_reader():
            try:
                with db.read() as c:
                    c.execute("SELECT COUNT(*) FROM kv").fetchone()
                    inside.set()
                    release.wait(HOLD_S)   # «тяжёлый расчёт»
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        t = threading.Thread(target=heavy_reader)
        t.start()
        try:
            assert inside.wait(5), "читатель не вошёл в read()"
            durations = []
            for i in range(3):
                t0 = time.monotonic()
                kv_set(db, "k%d" % i, i)
                durations.append(time.monotonic() - t0)
            still_reading = t.is_alive()
        finally:
            release.set()
            t.join(JOIN_S)
        assert not errors, errors
        print("    ожидание записи при удержании read(): "
              + ", ".join("%.1f мс" % (d * 1000) for d in durations))
        assert max(durations) < WRITE_MAX_S, (
            "запись ждёт чтения: самая долгая %.3f с (держим read() %.1f с)"
            % (max(durations), HOLD_S))
        median = sorted(durations)[1]
        assert median < WRITE_MEDIAN_S, (
            "запись медленная: медиана %.3f с" % median)
        assert still_reading, (
            "читатель уже вышел до конца записей — замер ничего не доказывает")
        with db.read() as c:
            assert [kv_get(c, "k%d" % i) for i in range(3)] == ["0", "1", "2"]
        print("[OK] test_write_does_not_wait_for_long_read")
    finally:
        cleanup(db, path)


def test_read_block_sees_one_snapshot_a43():
    """Сценарий 2 (A43): внутри одного read() все SELECT видят одно
    состояние БД, даже если другой поток закоммитил изменение посередине."""
    db, path = make_db()
    try:
        kv_set(db, "a", 1)
        kv_set(db, "b", 1)
        first_read_done = threading.Event()
        writer_done = threading.Event()
        errors = []

        def writer():
            try:
                first_read_done.wait(5)
                with db.transaction() as c:   # a и b меняются атомарно
                    c.execute("UPDATE kv SET value = '2' "
                              "WHERE key IN ('a', 'b')")
                writer_done.set()
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        t = threading.Thread(target=writer)
        t.start()
        try:
            with db.read() as c:
                a1 = kv_get(c, "a")
                first_read_done.set()
                committed_meanwhile = writer_done.wait(5)
                b1 = kv_get(c, "b")
                a2 = kv_get(c, "a")
        finally:
            first_read_done.set()
            t.join(JOIN_S)
        assert not errors, errors
        assert committed_meanwhile, (
            "чужая запись не смогла завершиться, пока другой поток "
            "держит read()")
        assert (a1, b1, a2) == ("1", "1", "1"), (
            "внутри read() изменился снимок: %r" % ((a1, b1, a2),))
        with db.read() as c:   # после выхода из блока — новое значение
            assert (kv_get(c, "a"), kv_get(c, "b")) == ("2", "2")
        print("[OK] test_read_block_sees_one_snapshot_a43")
    finally:
        cleanup(db, path)


def test_read_inside_transaction_sees_own_uncommitted_rows():
    """Сценарий 3: read() внутри transaction() того же потока отдаёт
    соединение-писатель и видит свои незакоммиченные строки; read() другого
    потока их не видит и не ждёт."""
    db, path = make_db()
    try:
        seen_by_other = []
        with db.transaction() as c:
            c.execute("INSERT INTO kv (key, value, updated_at) "
                      "VALUES ('own', 'x', 0)")
            with db.read() as rc:
                assert kv_get(rc, "own") == "x"

            def other():
                with db.read() as oc:
                    seen_by_other.append(kv_get(oc, "own"))

            t = threading.Thread(target=other)
            t.start()
            t.join(5)
            assert not t.is_alive(), (
                "read() другого потока ждёт чужую открытую транзакцию")
        assert seen_by_other == [None], (
            "чужой поток увидел незакоммиченную строку: %r" % seen_by_other)
        with db.read() as c:
            assert kv_get(c, "own") == "x"   # после COMMIT видна всем

        try:
            with db.transaction() as c:
                c.execute("INSERT INTO kv (key, value, updated_at) "
                          "VALUES ('gone', 'y', 0)")
                with db.read() as rc:
                    assert kv_get(rc, "gone") == "y"
                raise RuntimeError("boom")
        except RuntimeError:
            pass
        with db.read() as c:
            assert kv_get(c, "gone") is None, "ROLLBACK не откатил строку"
        print("[OK] test_read_inside_transaction_sees_own_uncommitted_rows")
    finally:
        cleanup(db, path)


def test_close_closes_all_connections_and_reopen_works():
    """Сценарий 4: close() закрывает писателя и ВСЕ читающие соединения."""
    db, path = make_db(read_pool_size=3)
    try:
        kv_set(db, "k", "v")
        conns = []
        errors = []
        barrier = threading.Barrier(3)

        def reader():
            try:
                with db.read() as c:
                    conns.append(c)
                    barrier.wait(5)   # все три внутри одновременно
                    c.execute("SELECT 1").fetchone()
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=reader) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(JOIN_S)
        assert not errors, errors
        assert len({id(c) for c in conns}) == 3, (
            "три одновременных read() должны получить три разных соединения")
        db.close()
        for c in conns:
            try:
                c.execute("SELECT 1")
            except sqlite3.ProgrammingError:
                continue
            raise AssertionError("читающее соединение осталось открытым")
        try:
            db.conn()
        except RuntimeError:
            pass
        else:
            raise AssertionError("писатель остался открытым после close()")
        db.close()   # повторный close() безопасен

        db.open()
        with db.read() as c:
            assert kv_get(c, "k") == "v"
        kv_set(db, "k2", "v2")
        with db.read() as c:
            assert kv_get(c, "k2") == "v2"
        print("[OK] test_close_closes_all_connections_and_reopen_works")
    finally:
        cleanup(db, path)


def test_memory_db_behaves_as_before():
    """Сценарий 5: ":memory:" — одно соединение (у отдельного соединения
    была бы своя пустая база), поведение прежнее."""
    db = Database(path=":memory:")
    db.open()
    try:
        kv_set(db, "m", "1")
        with db.read() as c:
            assert kv_get(c, "m") == "1"
        with db.transaction() as c:
            c.execute("INSERT INTO kv (key, value, updated_at) "
                      "VALUES ('own', 'x', 0)")
            with db.read() as rc:
                assert kv_get(rc, "own") == "x"
        seen = []
        t = threading.Thread(
            target=lambda: seen.append(_read_one(db, "m")))
        t.start()
        t.join(5)
        assert seen == ["1"], seen
        print("[OK] test_memory_db_behaves_as_before")
    finally:
        db.close()
    db.open()   # повторный open() после close() и тут работает
    try:
        kv_set(db, "again", "1")
        with db.read() as c:
            assert kv_get(c, "again") == "1"
    finally:
        db.close()


def _read_one(db, key):
    with db.read() as c:
        return kv_get(c, key)


def test_write_through_read_connection_fails():
    """Соединение из read() — только для чтения: запись через него падает
    (PRAGMA query_only), БД не меняется, соединение остаётся рабочим."""
    db, path = make_db()
    try:
        kv_set(db, "k", "1")
        try:
            with db.read() as c:
                c.execute("UPDATE kv SET value = '2' WHERE key = 'k'")
        except sqlite3.OperationalError as e:
            assert "readonly" in str(e).lower(), e
        else:
            raise AssertionError("запись через read() прошла")
        with db.read() as c:
            assert kv_get(c, "k") == "1"
        print("[OK] test_write_through_read_connection_fails")
    finally:
        cleanup(db, path)


def test_transaction_inside_read_still_works():
    """Вложенность read() → transaction() в одном потоке (так написан
    существующий код) продолжает работать: запись идёт через писателя."""
    db, path = make_db()
    try:
        with db.read() as c:
            assert kv_get(c, "n") is None
            kv_set(db, "n", "1")
        with db.read() as c:
            assert kv_get(c, "n") == "1"
        print("[OK] test_transaction_inside_read_still_works")
    finally:
        cleanup(db, path)


def test_nested_read_reuses_connection_and_snapshot():
    """Вложенный read() в том же потоке — то же соединение и тот же снимок;
    выход из внутреннего блока не завершает внешнюю читающую транзакцию."""
    db, path = make_db()
    try:
        kv_set(db, "k", "1")
        with db.read() as outer:
            assert outer.in_transaction
            before = kv_get(outer, "k")
            with db.read() as inner:
                assert inner is outer
            assert outer.in_transaction, "внутренний выход закрыл снимок"
            kv_set(db, "k", "2")   # чужая (для снимка) запись
            assert kv_get(outer, "k") == before == "1"
        assert not outer.in_transaction
        print("[OK] test_nested_read_reuses_connection_and_snapshot")
    finally:
        cleanup(db, path)


def test_connection_returned_to_pool_after_exception():
    db, path = make_db(read_pool_size=1)
    try:
        kv_set(db, "k", "1")
        ref = []
        try:
            with db.read() as c:
                ref.append(c)
                assert c.in_transaction
                raise ValueError("boom")
        except ValueError:
            pass
        assert not ref[0].in_transaction, "транзакция чтения не завершена"
        # пул из одного соединения: если оно не вернулось, это зависнет
        done = []
        t = threading.Thread(target=lambda: done.append(_read_one(db, "k")))
        t.start()
        t.join(5)
        assert done == ["1"], "соединение не вернулось в пул"
        print("[OK] test_connection_returned_to_pool_after_exception")
    finally:
        cleanup(db, path)


def test_pool_is_bounded():
    """Не более read_pool_size одновременных читателей; остальные ждут и
    потом проходят (без взаимной блокировки и без роста числа соединений)."""
    size = 2
    db, path = make_db(read_pool_size=size)
    try:
        kv_set(db, "k", "1")
        lock = threading.Lock()
        state = {"now": 0, "peak": 0}
        conn_ids = set()
        errors = []

        def reader():
            try:
                with db.read() as c:
                    with lock:
                        state["now"] += 1
                        state["peak"] = max(state["peak"], state["now"])
                        conn_ids.add(id(c))
                    time.sleep(0.15)
                    assert kv_get(c, "k") == "1"
                    with lock:
                        state["now"] -= 1
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=reader) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(JOIN_S)
        assert not errors, errors
        assert all(not t.is_alive() for t in threads)
        assert state["peak"] <= size, state
        assert len(conn_ids) <= size, conn_ids
        print("[OK] test_pool_is_bounded (peak=%d, connections=%d)"
              % (state["peak"], len(conn_ids)))
    finally:
        cleanup(db, path)


def test_mixed_load_no_errors():
    """Смешанная нагрузка: писатели увеличивают счётчик, читатели видят
    неубывающие значения; ни одной ошибки «database is locked»."""
    db, path = make_db()
    try:
        kv_set(db, "counter", 0)
        stop = threading.Event()
        errors = []
        writes = [0]
        wlock = threading.Lock()

        def writer():
            try:
                while not stop.is_set():
                    with db.transaction() as c:
                        c.execute(
                            "UPDATE kv SET value = CAST(value AS INTEGER) + 1 "
                            "WHERE key = 'counter'")
                    with wlock:
                        writes[0] += 1
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        def reader():
            last = -1
            try:
                while not stop.is_set():
                    with db.read() as c:
                        v1 = int(kv_get(c, "counter"))
                        v2 = int(kv_get(c, "counter"))
                    assert v1 == v2, "снимок внутри read() «поплыл»"
                    assert v1 >= last, "значение уменьшилось"
                    last = v1
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = ([threading.Thread(target=writer) for _ in range(3)]
                   + [threading.Thread(target=reader) for _ in range(4)])
        for t in threads:
            t.start()
        time.sleep(1.0)
        stop.set()
        for t in threads:
            t.join(JOIN_S)
        assert not errors, errors[:3]
        with db.read() as c:
            final = int(kv_get(c, "counter"))
        assert final == writes[0] > 0, (final, writes[0])
        print("[OK] test_mixed_load_no_errors (записей: %d)" % final)
    finally:
        cleanup(db, path)


if __name__ == "__main__":
    test_write_does_not_wait_for_long_read()
    test_read_block_sees_one_snapshot_a43()
    test_read_inside_transaction_sees_own_uncommitted_rows()
    test_close_closes_all_connections_and_reopen_works()
    test_memory_db_behaves_as_before()
    test_write_through_read_connection_fails()
    test_transaction_inside_read_still_works()
    test_nested_read_reuses_connection_and_snapshot()
    test_connection_returned_to_pool_after_exception()
    test_pool_is_bounded()
    test_mixed_load_no_errors()
    print("[ALL OK] test_step50_db_read_concurrency")

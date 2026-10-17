"""Шаг 59: разовый чистый старт через обновление (решения R9 от
11.10.2026 и R12 от 12.10.2026, docs/TZ-finish-plan.md §10,
scripts/clean-start-once.sh).

Всё, что заведено на контроллере до пилота, — тестовые данные. Кир выбрал
очистку обычной кнопкой «Обновить»: install.sh разово переносит state.db,
state.db-wal, state.db-shm и plans в $DATA_DIR/test-data-<дата>/ и пишет
метку .clean-start-r12-done (первая строка — момент чистого старта). Метка
прошлого чистого старта R9 (.clean-start-r9-done) на новое решение не
влияет.

Что проверяется (скрипт запускается по-настоящему, на temp-каталоге):

1. Первый запуск: всё перенесено без потерь, README рядом, метка записана.
2. Повторный запуск со свежей базой (создана после метки) — ничего не
   трогает.
3. Откат обновления вернул старую базу (создана раньше метки) — следующий
   запуск снова откладывает её; метка не меняется. Откат делает
   self-update.sh СТАРОЙ версии, о метке он не знает, — поэтому решение
   принимается по дате создания базы, а не по метке одной.
4. Запас на перевод часов: база на час старше метки не трогается.
5. Метка «0» (ручное отключение) — старая база не трогается.
6. Пустой (несуществующий) каталог данных — создан, метка есть, переносить
   нечего.
7. Сбой mv посреди переноса — уже перенесённое возвращено, метки нет,
   каталога test-data-* нет, код 1.
8. Нечитаемая метка / нечитаемая база — при наличии метки ничего не
   трогается; без метки битая база откладывается как есть.
9. Неверный вызов — код 2.
10. install.sh: вызов идёт после остановки сервиса и резервной копии, до
    копирования кода; сбой → понятное сообщение и exit 6; bash -n обоих
    скриптов.
11. R12 после R9: есть метка R9, метки R12 нет — база откладывается; метка
    R9 «0» (отключали R9) R12 не отключает; метка R9 рядом с R12 остаётся
    нетронутой, а свежая база после R12 не трогается.

Самостоятельный скрипт (не pytest):
    python tests/test_step59_clean_start_once.py
"""

from __future__ import annotations

import glob
import os
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO_ROOT, "scripts", "clean-start-once.sh")
INSTALL_SH = os.path.join(REPO_ROOT, "scripts", "install.sh")
MARK_NAME = ".clean-start-r12-done"
OLD_MARK_NAME = ".clean-start-r9-done"  # выпуск 0.22.1 (R9)
DAY = 86400


def _run(data_dir, env_extra=None, args=None):
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    argv = ["bash", SCRIPT] + ([data_dir] if args is None else args)
    return subprocess.run(argv, capture_output=True, text=True, env=env,
                          timeout=60)


def _make_db(path, born_at):
    """База с schema_migrations, как у настоящей (applied_at первой
    миграции — момент создания), закрытая чисто: WAL слит в файл."""
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY,"
                 " name TEXT NOT NULL, applied_at INTEGER NOT NULL)")
    conn.execute("INSERT INTO schema_migrations VALUES (1, 'initial_schema', ?)",
                 (int(born_at),))
    conn.execute("INSERT INTO schema_migrations VALUES (5, 'v2', ?)",
                 (int(born_at) + 10,))
    conn.execute("CREATE TABLE marker (v TEXT)")
    conn.execute("INSERT INTO marker VALUES ('тестовые данные')")
    conn.commit()
    conn.close()


def _marker(path):
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT v FROM marker").fetchone()[0]
    finally:
        conn.close()


def _write(path, data):
    with open(path, "wb") as f:
        f.write(data)


def _read(path):
    with open(path, "rb") as f:
        return f.read()


def _test_dirs(data_dir):
    return sorted(glob.glob(os.path.join(data_dir, "test-data-*")))


def _mark_ts(data_dir):
    with open(os.path.join(data_dir, MARK_NAME), encoding="utf-8") as f:
        return f.readline().strip()


def _fill_old_data(data_dir, born_at):
    os.makedirs(os.path.join(data_dir, "plans", "7"), exist_ok=True)
    _make_db(os.path.join(data_dir, "state.db"), born_at)
    _write(os.path.join(data_dir, "state.db-wal"), b"wal-bytes")
    _write(os.path.join(data_dir, "state.db-shm"), b"shm-bytes")
    _write(os.path.join(data_dir, "plans", "7", "plan.png"), b"\x89PNG-test")
    # Это не трогается: бэкапы install.sh и конфига wb-mqtt-serial.
    _write(os.path.join(data_dir, "state.db.backup-20261001-120000"), b"bk")
    os.makedirs(os.path.join(data_dir, "wb-serial-backups"), exist_ok=True)
    _write(os.path.join(data_dir, "wb-serial-backups", "a.conf"), b"serial")


# ---------------------------------------------------------------------
# 1-2. Первый запуск переносит, повторный со свежей базой — нет
# ---------------------------------------------------------------------

def test_first_run_moves_everything_and_writes_mark():
    with tempfile.TemporaryDirectory() as tmp:
        data = os.path.join(tmp, "data")
        os.makedirs(data)
        _fill_old_data(data, time.time() - 40 * DAY)
        before = int(time.time())

        r = _run(data)
        assert r.returncode == 0, r.stdout + r.stderr

        dirs = _test_dirs(data)
        assert len(dirs) == 1, dirs
        dest = dirs[0]
        # Байты WAL/SHM — до открытия базы: закрытие соединения SQLite
        # само убирает служебные файлы рядом.
        assert _read(os.path.join(dest, "state.db-wal")) == b"wal-bytes"
        assert _read(os.path.join(dest, "state.db-shm")) == b"shm-bytes"
        assert _marker(os.path.join(dest, "state.db")) == "тестовые данные"
        assert _read(os.path.join(dest, "plans", "7", "plan.png")) == b"\x89PNG-test"
        assert os.path.isfile(os.path.join(dest, "README.txt"))

        for name in ("state.db", "state.db-wal", "state.db-shm", "plans"):
            assert not os.path.exists(os.path.join(data, name)), name
        # Чужое не тронуто.
        assert _read(os.path.join(data, "state.db.backup-20261001-120000")) == b"bk"
        assert _read(os.path.join(data, "wb-serial-backups", "a.conf")) == b"serial"

        ts = _mark_ts(data)
        assert ts.isdigit() and before <= int(ts) <= int(time.time()) + 1, ts
        assert "Отложено в" in r.stdout, r.stdout
    print("[OK] первый запуск: база, WAL, SHM и планы отложены в test-data-*, метка записана")


def test_second_run_keeps_fresh_db():
    with tempfile.TemporaryDirectory() as tmp:
        data = os.path.join(tmp, "data")
        os.makedirs(data)
        _fill_old_data(data, time.time() - 40 * DAY)
        assert _run(data).returncode == 0

        # Сервис стартовал и создал новую базу ПОСЛЕ метки.
        _make_db(os.path.join(data, "state.db"), time.time() + 5)
        os.makedirs(os.path.join(data, "plans"))
        mark_before = _mark_ts(data)

        r = _run(data)
        assert r.returncode == 0, r.stdout + r.stderr
        assert len(_test_dirs(data)) == 1, _test_dirs(data)
        assert _marker(os.path.join(data, "state.db")) == "тестовые данные"
        assert os.path.isdir(os.path.join(data, "plans"))
        assert _mark_ts(data) == mark_before
        assert "не трогаю" in r.stdout, r.stdout
    print("[OK] повторный запуск: свежая база (после метки) не тронута")


# ---------------------------------------------------------------------
# 3-5. Откат вернул старую базу; запас на часы; ручное отключение
# ---------------------------------------------------------------------

def test_rollback_restored_old_db_is_moved_again():
    with tempfile.TemporaryDirectory() as tmp:
        data = os.path.join(tmp, "data")
        os.makedirs(data)
        _fill_old_data(data, time.time() - 40 * DAY)
        assert _run(data).returncode == 0
        mark_before = _mark_ts(data)

        # Старый self-update.sh откатился: вернул снимок старой базы и
        # планы (restore_data_backup), метку он не знает и не трогает.
        _make_db(os.path.join(data, "state.db"), time.time() - 40 * DAY)
        os.makedirs(os.path.join(data, "plans", "7"))
        _write(os.path.join(data, "plans", "7", "plan.png"), b"old")

        r = _run(data)
        assert r.returncode == 0, r.stdout + r.stderr
        dirs = _test_dirs(data)
        assert len(dirs) == 2, dirs
        assert not os.path.exists(os.path.join(data, "state.db"))
        assert not os.path.exists(os.path.join(data, "plans"))
        assert _mark_ts(data) == mark_before
        assert "раньше чистого старта" in r.stdout, r.stdout
    print("[OK] откат вернул старую базу — следующее обновление снова её откладывает")


def test_clock_margin_keeps_db_one_hour_older_than_mark():
    with tempfile.TemporaryDirectory() as tmp:
        data = os.path.join(tmp, "data")
        os.makedirs(data)
        now = int(time.time())
        _write(os.path.join(data, MARK_NAME), f"{now}\n".encode())
        _make_db(os.path.join(data, "state.db"), now - 3600)

        r = _run(data)
        assert r.returncode == 0, r.stdout + r.stderr
        assert os.path.exists(os.path.join(data, "state.db"))
        assert _test_dirs(data) == []
    print("[OK] перевод часов: база на час старше метки не тронута (запас — сутки)")


def test_mark_zero_disables():
    with tempfile.TemporaryDirectory() as tmp:
        data = os.path.join(tmp, "data")
        os.makedirs(data)
        _write(os.path.join(data, MARK_NAME), b"0\n")
        _make_db(os.path.join(data, "state.db"), time.time() - 400 * DAY)

        r = _run(data)
        assert r.returncode == 0, r.stdout + r.stderr
        assert _marker(os.path.join(data, "state.db")) == "тестовые данные"
        assert _test_dirs(data) == []
    print("[OK] метка «0»: перенос отключён, старая база на месте")


# ---------------------------------------------------------------------
# 6-9. Пустой каталог, сбой mv, нечитаемое, неверный вызов
# ---------------------------------------------------------------------

def test_missing_data_dir_only_writes_mark():
    with tempfile.TemporaryDirectory() as tmp:
        data = os.path.join(tmp, "nested", "data")
        r = _run(data)
        assert r.returncode == 0, r.stdout + r.stderr
        assert os.path.isdir(data)
        assert _mark_ts(data).isdigit()
        assert _test_dirs(data) == []
        assert sorted(os.listdir(data)) == [MARK_NAME]
    print("[OK] пустой каталог данных: создан, метка записана, переносить нечего")


def test_mv_failure_rolls_back_and_leaves_no_mark():
    with tempfile.TemporaryDirectory() as tmp:
        data = os.path.join(tmp, "data")
        os.makedirs(data)
        _fill_old_data(data, time.time() - 40 * DAY)

        fake_bin = os.path.join(tmp, "bin")
        os.makedirs(fake_bin)
        real_mv = subprocess.run(["bash", "-c", "command -v mv"],
                                 capture_output=True, text=True).stdout.strip()
        fake_mv = os.path.join(fake_bin, "mv")
        with open(fake_mv, "w", encoding="utf-8") as f:
            f.write("#!/bin/bash\n"
                    "for a in \"$@\"; do\n"
                    "  case \"$a\" in */data/plans) echo 'mv: отказ (тест)' >&2; exit 1;; esac\n"
                    "done\n"
                    f"exec {real_mv} \"$@\"\n")
        os.chmod(fake_mv, os.stat(fake_mv).st_mode | stat.S_IXUSR)

        r = _run(data, env_extra={"PATH": fake_bin + os.pathsep + os.environ["PATH"]})
        assert r.returncode == 1, (r.returncode, r.stdout, r.stderr)
        assert "возвращаю уже перенесённое" in r.stderr, r.stderr
        assert _read(os.path.join(data, "state.db-wal")) == b"wal-bytes"
        assert _read(os.path.join(data, "state.db-shm")) == b"shm-bytes"
        assert _marker(os.path.join(data, "state.db")) == "тестовые данные"
        assert os.path.isfile(os.path.join(data, "plans", "7", "plan.png"))
        assert not os.path.exists(os.path.join(data, MARK_NAME))
        assert _test_dirs(data) == [], _test_dirs(data)
    print("[OK] сбой mv посреди переноса: всё возвращено, метки нет, код 1")


def test_unreadable_mark_or_db_is_left_alone():
    with tempfile.TemporaryDirectory() as tmp:
        data = os.path.join(tmp, "data")
        os.makedirs(data)
        _write(os.path.join(data, MARK_NAME), b"garbage\n")
        _make_db(os.path.join(data, "state.db"), time.time() - 400 * DAY)
        r = _run(data)
        assert r.returncode == 0, r.stdout + r.stderr
        assert os.path.exists(os.path.join(data, "state.db"))
        assert _test_dirs(data) == []
        assert "нечитаема" in r.stdout, r.stdout

    with tempfile.TemporaryDirectory() as tmp:
        data = os.path.join(tmp, "data")
        os.makedirs(data)
        _write(os.path.join(data, MARK_NAME), f"{int(time.time())}\n".encode())
        _write(os.path.join(data, "state.db"), b"not a database at all")
        r = _run(data)
        assert r.returncode == 0, r.stdout + r.stderr
        assert _read(os.path.join(data, "state.db")) == b"not a database at all"
        assert _test_dirs(data) == []

    with tempfile.TemporaryDirectory() as tmp:
        data = os.path.join(tmp, "data")
        os.makedirs(data)
        _write(os.path.join(data, "state.db"), b"not a database at all")
        r = _run(data)
        assert r.returncode == 0, r.stdout + r.stderr
        dirs = _test_dirs(data)
        assert len(dirs) == 1 and _read(os.path.join(dirs[0], "state.db")) == b"not a database at all"
        assert _mark_ts(data).isdigit()
    print("[OK] нечитаемая метка/база при наличии метки — не тронуты; без метки — отложены")


def test_bad_invocation():
    r = _run(None, args=[])
    assert r.returncode == 2, (r.returncode, r.stderr)
    assert "Использование" in r.stderr
    print("[OK] вызов без каталога — код 2")


# ---------------------------------------------------------------------
# 10. Место вызова в install.sh
# ---------------------------------------------------------------------

def test_install_sh_calls_after_stop_and_backup_before_copy():
    with open(INSTALL_SH, encoding="utf-8") as f:
        text = f.read()
    call = 'bash "$SCRIPT_DIR/clean-start-once.sh" "$DATA_DIR"'
    assert text.count(call) == 1, "вызов clean-start-once.sh должен быть ровно один"
    i_call = text.index(call)
    i_stop = text.index("systemctl stop wb-energy-meter.service")
    i_backup_fail = text.index("exit 3")
    i_copy = text.index('echo ">>> Копирование кода..."')
    assert i_stop < i_call, "перенос обязан идти после остановки сервиса"
    assert i_backup_fail < i_call, "перенос обязан идти после резервной копии БД"
    assert i_call < i_copy, "перенос обязан идти до копирования кода"
    tail = text[i_call:i_call + 600]
    assert "exit 6" in tail and "ОШИБКА" in tail, tail
    print("[OK] install.sh: перенос после stop и бэкапа, до копирования кода; сбой -> exit 6")


def test_bash_n_scripts():
    for path in (SCRIPT, INSTALL_SH):
        r = subprocess.run(["bash", "-n", path], capture_output=True, text=True)
        assert r.returncode == 0, f"bash -n {path}: {r.stderr}"
    print("[OK] bash -n clean-start-once.sh и install.sh — синтаксис чист")


# ---------------------------------------------------------------------
# 11. R12 после R9: метка прошлого решения ничего не решает
# ---------------------------------------------------------------------

def test_r12_runs_even_though_r9_mark_exists():
    """Контроллер, уже прошедший R9 (0.22.1): в базе за это время завели
    данные, метка R9 лежит на диске. Обновление до R12 откладывает базу."""
    with tempfile.TemporaryDirectory() as tmp:
        data = os.path.join(tmp, "data")
        os.makedirs(data)
        r9_ts = int(time.time()) - 3 * DAY
        _write(os.path.join(data, OLD_MARK_NAME), f"{r9_ts}\n".encode())
        _fill_old_data(data, time.time() - 2 * DAY)  # база после R9
        assert not os.path.exists(os.path.join(data, MARK_NAME))

        r = _run(data)
        assert r.returncode == 0, r.stdout + r.stderr

        dirs = _test_dirs(data)
        assert len(dirs) == 1, dirs
        assert _marker(os.path.join(dirs[0], "state.db")) == "тестовые данные"
        assert _read(os.path.join(dirs[0], "plans", "7", "plan.png")) == b"\x89PNG-test"
        for name in ("state.db", "state.db-wal", "state.db-shm", "plans"):
            assert not os.path.exists(os.path.join(data, name)), name
        assert _mark_ts(data).isdigit()
        assert "R12" in r.stdout or "R12" in _read(os.path.join(dirs[0], "README.txt")).decode()
        # Метка R9 не тронута и не переписана.
        assert _read(os.path.join(data, OLD_MARK_NAME)) == f"{r9_ts}\n".encode()
    print("[OK] R12 после R9: метка R9 есть, R12 нет — база отложена, метка R9 цела")


def test_r9_disabled_mark_does_not_disable_r12():
    with tempfile.TemporaryDirectory() as tmp:
        data = os.path.join(tmp, "data")
        os.makedirs(data)
        _write(os.path.join(data, OLD_MARK_NAME), b"0\n")  # R9 когда-то отключили
        _make_db(os.path.join(data, "state.db"), time.time() - 400 * DAY)

        r = _run(data)
        assert r.returncode == 0, r.stdout + r.stderr
        assert len(_test_dirs(data)) == 1, _test_dirs(data)
        assert not os.path.exists(os.path.join(data, "state.db"))
        assert _read(os.path.join(data, OLD_MARK_NAME)) == b"0\n"
        assert _mark_ts(data).isdigit() and _mark_ts(data) != "0"
    print("[OK] «0» в метке R9 не отключает R12: база отложена")


def test_fresh_db_after_r12_is_kept_even_with_r9_mark_present():
    with tempfile.TemporaryDirectory() as tmp:
        data = os.path.join(tmp, "data")
        os.makedirs(data)
        _write(os.path.join(data, OLD_MARK_NAME), f"{int(time.time()) - 5 * DAY}\n".encode())
        _fill_old_data(data, time.time() - 2 * DAY)
        assert _run(data).returncode == 0
        mark_before = _mark_ts(data)

        # Сервис стартовал после R12 и завёл новую базу — теперь уже боевую.
        _make_db(os.path.join(data, "state.db"), time.time() + 5)
        r = _run(data)
        assert r.returncode == 0, r.stdout + r.stderr
        assert len(_test_dirs(data)) == 1, _test_dirs(data)
        assert _marker(os.path.join(data, "state.db")) == "тестовые данные"
        assert _mark_ts(data) == mark_before
        assert "не трогаю" in r.stdout, r.stdout
    print("[OK] после R12 свежая база не тронута (метка R9 рядом ничего не меняет)")


def test_disable_r12_by_zero_mark():
    """Отключение R12: `echo 0 > .clean-start-r12-done` до обновления."""
    with tempfile.TemporaryDirectory() as tmp:
        data = os.path.join(tmp, "data")
        os.makedirs(data)
        _write(os.path.join(data, OLD_MARK_NAME), f"{int(time.time()) - 5 * DAY}\n".encode())
        _write(os.path.join(data, MARK_NAME), b"0\n")
        _fill_old_data(data, time.time() - 2 * DAY)

        r = _run(data)
        assert r.returncode == 0, r.stdout + r.stderr
        assert _marker(os.path.join(data, "state.db")) == "тестовые данные"
        assert _test_dirs(data) == []
    print("[OK] «0» в метке R12 отключает перенос, база на месте")


def test_script_uses_only_r12_mark():
    with open(SCRIPT, encoding="utf-8") as f:
        text = f.read()
    assert 'MARK_NAME=".clean-start-r12-done"' in text
    assert 'MARK="$DATA_DIR/$MARK_NAME"' in text
    # Имя прошлой метки встречается только в пояснениях (комментарии).
    for line in text.splitlines():
        if ".clean-start-r9-done" in line:
            assert line.lstrip().startswith("#"), line
    print("[OK] скрипт использует метку R12; R9 упомянута только в комментариях")


if __name__ == "__main__":
    test_first_run_moves_everything_and_writes_mark()
    test_second_run_keeps_fresh_db()
    test_rollback_restored_old_db_is_moved_again()
    test_clock_margin_keeps_db_one_hour_older_than_mark()
    test_mark_zero_disables()
    test_missing_data_dir_only_writes_mark()
    test_mv_failure_rolls_back_and_leaves_no_mark()
    test_unreadable_mark_or_db_is_left_alone()
    test_bad_invocation()
    test_install_sh_calls_after_stop_and_backup_before_copy()
    test_bash_n_scripts()
    test_r12_runs_even_though_r9_mark_exists()
    test_r9_disabled_mark_does_not_disable_r12()
    test_fresh_db_after_r12_is_kept_even_with_r9_mark_present()
    test_disable_r12_by_zero_mark()
    test_script_uses_only_r12_mark()
    print("\nВсе тесты Шага 59 (разовый чистый старт R9/R12) пройдены.")

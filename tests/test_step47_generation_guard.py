"""Шаг 47 (партия 10, этап C, F4): страж поколений домена —
scripts/check-generation.sh, подключённый как ExecStartPre юнита
systemd (docs/migration-plan-v2.md §7 п.3,
docs/TZ-batch10-reliability-and-load.md §3).

До этой партии проверку "код умеет читать эту БД" делал только
scripts/self-update.sh::check_rollback_generation — и только во время
СВОЕГО СОБСТВЕННОГО автоматического отката. Ручной откат (старый
install.sh из старого коммита, ручной systemctl start) проходил мимо
этой проверки: сервис поднимался на БД более нового поколения домена
молча. check-generation.sh закрывает именно этот путь — он не зависит
от self-update.sh и подключён в юнит так, что срабатывает при КАЖДОМ
старте сервиса (systemd не даёт ExecStart выполниться, если
ExecStartPre вернул ненулевой код — это стандартное поведение systemd,
отдельно настраивать не нужно).

Тестируется сам скрипт через subprocess (он POSIX sh, не питон-модуль),
на temp-файлах — реальный systemd здесь не поднять и не нужен:
- совместимое поколение -> 0
- несовместимое (откат на старый код) -> 1 и внятный текст в выводе
- БД отсутствует -> 0 (первый запуск — это не ошибка)
- БД повреждена -> 1, человекочитаемый текст, а НЕ голый traceback

Самостоятельный скрипт (не pytest):
    python tests/test_step47_generation_guard.py
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

SCRIPT = os.path.join(REPO_ROOT, "scripts", "check-generation.sh")


def _run(db_path, init_py_path):
    return subprocess.run(
        ["sh", SCRIPT, db_path, init_py_path],
        capture_output=True, text=True, timeout=30)


def _write_init(path, code_generation):
    with open(path, "w", encoding="utf-8") as f:
        f.write('__version__ = "0.20.0"\n')
        if code_generation is not None:
            f.write(f"__code_generation__ = {code_generation}\n")


def _make_kv_db(path, min_reader_generation):
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT NOT NULL, "
            "updated_at INTEGER NOT NULL)")
        if min_reader_generation is not None:
            conn.execute(
                "INSERT INTO kv (key, value, updated_at) VALUES (?, ?, ?)",
                ("minimum_reader_generation",
                 json.dumps(min_reader_generation), 0))
        conn.commit()
    finally:
        conn.close()


def test_bash_n_check_generation_sh():
    if not shutil.which("bash"):
        print("[SKIP] нет bash — тест пропущен")
        return
    r = subprocess.run(["bash", "-n", SCRIPT], capture_output=True, text=True)
    assert r.returncode == 0, f"bash -n scripts/check-generation.sh: {r.stderr}"
    r = subprocess.run(["sh", "-n", SCRIPT], capture_output=True, text=True)
    assert r.returncode == 0, f"sh -n scripts/check-generation.sh: {r.stderr}"
    print("[OK] check-generation.sh: bash -n и sh -n чисты (POSIX sh)")


def test_missing_db_exit_zero():
    with tempfile.TemporaryDirectory() as tmp:
        init_py = os.path.join(tmp, "__init__.py")
        _write_init(init_py, 2)
        db_path = os.path.join(tmp, "does-not-exist.db")

        r = _run(db_path, init_py)
        assert r.returncode == 0, (
            f"первый запуск (БД ещё нет) не должен блокироваться: "
            f"код {r.returncode}, stderr={r.stderr!r}")
        print("[OK] БД отсутствует -> 0 (первый запуск — не ошибка)")


def test_legacy_db_without_kv_table_exit_zero():
    """Легаси-БД, у которой таблицы kv вообще ещё нет (домен-поколения
    введены позже) — должна считаться поколением 1 и не блокировать
    текущий (поколения 2) код."""
    with tempfile.TemporaryDirectory() as tmp:
        init_py = os.path.join(tmp, "__init__.py")
        _write_init(init_py, 2)
        db_path = os.path.join(tmp, "legacy.db")
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE some_other_table (x INTEGER)")
        conn.commit()
        conn.close()

        r = _run(db_path, init_py)
        assert r.returncode == 0, (
            f"легаси-БД без kv не должна блокировать запуск: "
            f"код {r.returncode}, stderr={r.stderr!r}")
        print("[OK] легаси-БД без таблицы kv -> 0 (поколение 1 по умолчанию)")


def test_compatible_generation_exit_zero():
    with tempfile.TemporaryDirectory() as tmp:
        init_py = os.path.join(tmp, "__init__.py")
        _write_init(init_py, 2)
        db_path = os.path.join(tmp, "state.db")
        _make_kv_db(db_path, 2)

        r = _run(db_path, init_py)
        assert r.returncode == 0, (
            f"код и БД одного поколения не должны блокироваться: "
            f"код {r.returncode}, stderr={r.stderr!r}")
        print("[OK] поколение кода == требуемого поколением БД -> 0")


def test_incompatible_generation_exit_one_with_message():
    """Основной сценарий партии: откат на старый код (поколение 1)
    поверх БД, уже помеченной как поколение 2 (после первой v2-записи —
    см. domain_generation.py::mark_v2_domain_write)."""
    with tempfile.TemporaryDirectory() as tmp:
        init_py = os.path.join(tmp, "__init__.py")
        _write_init(init_py, None)  # константы ещё нет -> поколение 1
        db_path = os.path.join(tmp, "state.db")
        _make_kv_db(db_path, 2)

        r = _run(db_path, init_py)
        assert r.returncode == 1, (
            f"откат на код поколения 1 поверх БД поколения 2 должен "
            f"блокироваться: код {r.returncode}, stdout={r.stdout!r}")
        text = r.stdout + r.stderr
        assert "2" in text and "1" in text, (
            "сообщение должно называть оба поколения (требуемое и имеющееся)")
        assert "Traceback" not in text, \
            "сообщение должно быть человекочитаемым, а не голым traceback"
        print(f"[OK] несовместимое поколение -> 1 с внятным текстом: {r.stderr.strip()!r}")


def test_corrupt_db_exit_one_not_traceback():
    with tempfile.TemporaryDirectory() as tmp:
        init_py = os.path.join(tmp, "__init__.py")
        _write_init(init_py, 2)
        db_path = os.path.join(tmp, "corrupt.db")
        with open(db_path, "wb") as f:
            f.write(b"garbage, definitely not a sqlite file 0123456789")

        r = _run(db_path, init_py)
        assert r.returncode == 1, (
            f"повреждённую БД нельзя молча пропускать: "
            f"код {r.returncode}, stdout={r.stdout!r}")
        text = r.stdout + r.stderr
        assert "Traceback" not in text, (
            "провал на битой БД должен быть человекочитаемым сообщением, "
            f"а не питоновским traceback: {text!r}")
        print(f"[OK] повреждённая БД -> 1, без traceback: {r.stderr.strip()!r}")


if __name__ == "__main__":
    test_bash_n_check_generation_sh()
    test_missing_db_exit_zero()
    test_legacy_db_without_kv_table_exit_zero()
    test_compatible_generation_exit_zero()
    test_incompatible_generation_exit_one_with_message()
    test_corrupt_db_exit_one_not_traceback()
    print("\nВсе тесты Шага 47 (страж поколений домена) пройдены.")

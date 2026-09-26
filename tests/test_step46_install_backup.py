"""Шаг 46 (партия 10, этап B, F3): резервная копия БД в scripts/install.sh
через SQLite Online Backup API вместо файлового `cp -a`
(docs/TZ-batch10-reliability-and-load.md §2,
docs/migration-plan-v2.md §7 п.4).

Поймано 27.09.2026: старый install.sh снимал бэкап `cp -a "$DB_PATH"
"$BACKUP"` ДО остановки сервиса. Пока демон работает, БД открыта в
WAL-режиме: часть уже закоммиченных данных лежит в файле `<DB>-wal`,
который `cp` не копирует. Копия могла оказаться рваной или попросту
отставать от реального состояния — а хуже того, снималась в момент,
когда рассинхронизация наиболее вероятна.

install.sh запускать целиком нельзя: пути (`/opt/wb-energy-meter`,
`/etc/...`, `/mnt/data/...`) в нём захардкожены, не берутся из
переменных окружения (в отличие от self-update.sh, который тестирует
test_step9_updater.py именно так) — полный прогон писал бы в реальные
системные пути песочницы, а не в изолированный temp. Поэтому здесь
тестируется:

1. Сам механизм (Online Backup API против наивного `cp`) — на temp-БД
   с открытым WAL и незачекпоинтенным хвостом.
2. Ровно тот питон-сниппет, что реально зашит в install.sh — извлечён
   из файла по маркерам и запущен как отдельный процесс, а не
   переизобретён — чтобы тест проверял именно то, что уедет на
   контроллер.
3. Структура install.sh текстом: бэкап идёт ПОСЛЕ `systemctl stop`,
   наивного `cp -a` для БД в файле больше нет, при неудаче — понятное
   сообщение и ненулевой код выхода.

Самостоятельный скрипт (не pytest):
    python tests/test_step46_install_backup.py
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

INSTALL_SH = os.path.join(REPO_ROOT, "scripts", "install.sh")


def _install_sh_text():
    with open(INSTALL_SH, encoding="utf-8") as f:
        return f.read()


def _extract_online_backup_snippet(text=None):
    """Достаёт буквальный текст python3-сниппета Online Backup API из
    install.sh (между `python3 -c '` после маркера-комментария и
    закрывающей кавычкой перед `"$DB_PATH" "$BACKUP"`). Тест обязан
    гонять именно ЭТОТ код, а не свою копию — иначе тест проверяет
    что-то своё, а не реальный install.sh."""
    text = text if text is not None else _install_sh_text()
    marker = "Резервная копия БД (Online Backup API"
    idx = text.index(marker)
    start = text.index("python3 -c '", idx) + len("python3 -c '")
    end = text.index("\n' \"$DB_PATH\" \"$BACKUP\"", start)
    return text[start:end]


def _run_snippet(snippet, src_path, dst_path):
    return subprocess.run(
        [sys.executable, "-c", snippet, src_path, dst_path],
        capture_output=True, text=True, timeout=30)


# ---------------------------------------------------------------------
# 1-2. Механизм: Online Backup API видит WAL-хвост, наивный cp — нет
# ---------------------------------------------------------------------

def test_online_backup_captures_wal_tail_naive_cp_lags():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "state.db")
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE t (v INTEGER)")
        conn.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(500)])
        conn.commit()
        assert os.path.exists(db_path + "-wal"), \
            "WAL-файл должен появиться при активном журнале WAL"

        # То, что раньше делал install.sh: голое копирование файла БД,
        # без сопутствующего -wal, ДО остановки демона (здесь демона нет,
        # но соединение conn ещё открыто и ничего не чекпоинтило —
        # ровно та же незачекпоинтенная ситуация).
        cp_dst = os.path.join(tmp, "cp-copy.sqlite3")
        shutil.copy2(db_path, cp_dst)
        cp_conn = sqlite3.connect(cp_dst)
        try:
            cp_rows = cp_conn.execute("SELECT COUNT(*) FROM t").fetchone()[0]
        except sqlite3.OperationalError:
            cp_rows = None  # таблицы в копии вообще нет — ещё нагляднее отставание
        finally:
            cp_conn.close()

        snippet = _extract_online_backup_snippet()
        backup_dst = os.path.join(tmp, "online-backup.sqlite3")
        r = _run_snippet(snippet, db_path, backup_dst)
        assert r.returncode == 0, f"online-бэкап не удался: {r.stderr}"

        ob_conn = sqlite3.connect(backup_dst)
        try:
            ob_rows = ob_conn.execute("SELECT COUNT(*) FROM t").fetchone()[0]
            integrity = ob_conn.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            ob_conn.close()
        conn.close()

        # Обязательная часть: online-бэкап корректен независимо от того,
        # удалось ли ниже воспроизвести отставание cp в этом окружении.
        assert ob_rows == 500, f"online-бэкап должен содержать все 500 строк, получено {ob_rows}"
        assert integrity == "ok", f"PRAGMA integrity_check копии: {integrity!r}"

        if cp_rows == 500:
            print("[OK] online-бэкап через Online Backup API корректен "
                  "(500/500 строк, integrity_check=ok); в этом окружении "
                  "наивный cp неожиданно не отстал (WAL был зачекпоинтен "
                  "раньше времени) — сама демонстрация отставания не "
                  "воспроизвелась, но это не отменяет корректность нового кода")
        else:
            print(f"[OK] наивный cp отстаёт на WAL (в копии: {cp_rows!r} строк "
                  "вместо 500); Online Backup API снимает все 500 строк, "
                  "PRAGMA integrity_check=ok")


def test_online_backup_snippet_fails_loudly_on_bad_source():
    """При неудаче — исключение и ненулевой код выхода, а не тихая
    пустая/битая копия. install.sh (снаружи этого сниппета) на этой
    точке останавливает установку с понятным сообщением (exit 3) —
    структурная проверка этого в test_install_sh_stops_on_backup_failure
    ниже."""
    with tempfile.TemporaryDirectory() as tmp:
        bad_src = os.path.join(tmp, "not-a-database.db")
        with open(bad_src, "wb") as f:
            f.write(b"garbage, not a sqlite file, just some bytes 0123456789")
        dst = os.path.join(tmp, "out.sqlite3")

        snippet = _extract_online_backup_snippet()
        r = _run_snippet(snippet, bad_src, dst)

        assert r.returncode != 0, "бэкап битого источника должен провалиться"
        assert r.stderr.strip(), "должно быть понятное сообщение об ошибке в stderr"
        assert not os.path.exists(dst), \
            "временный файл не должен остаться на месте назначения после провала"
        print("[OK] Online Backup API на битом источнике -> ненулевой код "
              f"выхода ({r.returncode}) с текстом ошибки, целевой файл не создан")


# ---------------------------------------------------------------------
# 3. Структура install.sh: порядок, отсутствие наивного cp, аварийный выход
# ---------------------------------------------------------------------

def test_install_sh_backs_up_after_stop_not_before():
    text = _install_sh_text()
    stop_idx = text.index("systemctl stop wb-energy-meter.service")
    backup_idx = text.index("Резервная копия БД (Online Backup API")
    assert stop_idx < backup_idx, (
        "бэкап БД должен идти ПОСЛЕ остановки сервиса — раньше было "
        "наоборот, и копия снималась с живой WAL-БД в худший момент")
    print("[OK] install.sh: резервная копия БД идёт после systemctl stop")


def test_install_sh_has_no_naive_cp_for_db_backup():
    text = _install_sh_text()
    # Старая дыра: `cp -a "$DB_PATH" "$BACKUP"` — такой строки в файле
    # быть не должно вообще (заменена питон-сниппетом с Online Backup API).
    assert 'cp -a "$DB_PATH" "$BACKUP"' not in text, \
        "наивный cp -a для бэкапа БД должен был быть заменён на Online Backup API"
    assert "sqlite3.connect" in text and "src.backup(dst)" in text, \
        "install.sh должен содержать питон-сниппет с Online Backup API"
    print("[OK] install.sh: наивного cp -a для БД больше нет, есть Online Backup API")


def test_install_sh_stops_on_backup_failure():
    text = _install_sh_text()
    backup_idx = text.index("Резервная копия БД (Online Backup API")
    # Ищем ближайший блок "if ! python3 -c ... exit N" после этого места.
    exit_marker = "exit 3"
    exit_idx = text.index(exit_marker, backup_idx)
    segment = text[backup_idx:exit_idx]
    assert "ОШИБКА" in segment, \
        "при неудаче бэкапа должно печататься понятное сообщение (ОШИБКА: ...)"
    assert "if ! python3 -c" in segment, \
        "провал бэкапа должен проверяться (if ! python3 -c ...; then ... exit)"
    print("[OK] install.sh: провал бэкапа БД -> понятное сообщение + exit 3, "
          "установка дальше не идёт")


def test_bash_n_install_sh():
    if not shutil.which("bash"):
        print("[SKIP] нет bash — тест пропущен")
        return
    r = subprocess.run(["bash", "-n", INSTALL_SH], capture_output=True, text=True)
    assert r.returncode == 0, f"bash -n scripts/install.sh: {r.stderr}"
    print("[OK] bash -n scripts/install.sh — синтаксис чист")


if __name__ == "__main__":
    test_online_backup_captures_wal_tail_naive_cp_lags()
    test_online_backup_snippet_fails_loudly_on_bad_source()
    test_install_sh_backs_up_after_stop_not_before()
    test_install_sh_has_no_naive_cp_for_db_backup()
    test_install_sh_stops_on_backup_failure()
    test_bash_n_install_sh()
    print("\nВсе тесты Шага 46 (бэкап БД в install.sh) пройдены.")

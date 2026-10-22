"""Тесты Шага 76 (партия 15, этап 15.6, A42): откат `self-update.sh` возвращает согласованную тройку
«код — БД — изображения плана» (docs/migration-plan-v2.md §7, docs/acceptance-matrix.md → A42).

Запускается настоящий scripts/self-update.sh в песочнице: подставные `curl` (архив, /health),
`systemctl` и `sleep`, свой INSTALL_DIR, DATA_DIR и каталог планов. Подставной `install.sh` из
«нового» архива делает то, что делает настоящая установка и первый старт нового кода: кладёт новый
код, меняет БД (запись в schema_migrations, новая таблица) и добавляет изображение плана; /health
нового кода не отвечает — проверка после установки проваливается и включается откат.

Сценарии (ожидания посчитаны вручную до запуска):

  A. ДО первой записи v2: в БД нет minimum_reader_generation (читается как 1), код отката —
     поколения 1. Откат разрешён → статус rolled_back; код, VERSION.json и scripts/ — как были;
     БД побайтно по содержимому (iterdump) равна снимку до обновления, строки новой миграции и
     новая таблица исчезли; рядом лежит state.db.pre-rollback-* с состоянием после неудачи; нет
     хвостов -wal/-shm; изображения плана — как до обновления, лишнего файла нет.
  B. ПОСЛЕ первой записи v2, код отката старый (поколение 1 < minimum_reader_generation=2): откат
     ЗАБЛОКИРОВАН — статус failed «Откат заблокирован…», сервис остаётся на новом коде, БД и
     изображения не тронуты (никакого полувосстановленного состояния).
  C. ПОСЛЕ первой записи v2, код отката поколения 3 (понимает модель v2): откат разрешён, БД
     возвращается к снимку (с поколением домена 2 и minimum_reader_generation=2), код — прежний.

Самостоятельный скрипт: python tests/test_step76_rollback_triple.py
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO_ROOT, "scripts", "self-update.sh")

OLD_INIT = '__version__ = "0.24.0"\n%s# OLD_MARKER\n'
NEW_INIT = '__version__ = "0.25.0"\n__code_generation__ = 3\n# NEW_CODE\n'
OLD_INSTALL_SH = "#!/bin/bash\n# OLD scripts/install.sh\n"
OLD_VERSION_JSON = '{"version": "0.24.0", "commit": "oldsha"}\n'

FAKE_INSTALL_SH = r"""#!/bin/bash
# Подставной install.sh: то, что делают настоящая установка и первый старт нового кода.
set -e
rm -rf "$INSTALL_DIR/wb_energy_meter"
cp -a wb_energy_meter "$INSTALL_DIR/wb_energy_meter"
mkdir -p "$INSTALL_DIR/scripts"
cp -a scripts/. "$INSTALL_DIR/scripts/"
echo '{"version": "0.25.0", "commit": "newsha"}' > "$INSTALL_DIR/VERSION.json"
python3 - <<'PY'
import os, sqlite3
c = sqlite3.connect(os.environ["DB_PATH"])
c.execute("INSERT INTO schema_migrations(version) VALUES (99)")
c.execute("CREATE TABLE IF NOT EXISTS new_table (x)")
c.execute("INSERT INTO new_table VALUES ('после обновления')")
c.commit(); c.close()
open(os.path.join(os.environ["PLANS_DIR"], "plan_1_new.png"), "wb").write(b"NEW")
PY
"""

FAKE_CURL = r"""#!/bin/bash
out=""; fmt=""; url=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    -o) out="$2"; shift 2;;
    -w) fmt="$2"; shift 2;;
    --max-time|-X|-H|-d) shift 2;;
    -*) shift;;
    *) url="$1"; shift;;
  esac
done
case "$url" in
  *github.com*) cp "$ARCHIVE" "$out"; exit 0;;
  */health)
    grep -q OLD_MARKER "$INSTALL_DIR/wb_energy_meter/__init__.py" && exit 0 || exit 22;;
  */api/selfcheck) [[ -n "$out" ]] && : > "$out"; printf '404'; exit 0;;
  */api/v2/points) [[ -n "$out" ]] && echo '[]' > "$out"; [[ -n "$fmt" ]] && printf '200'; exit 0;;
esac
exit 22
"""


def _write_exec(path, text):
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)


def _dump(db_path):
    con = sqlite3.connect(db_path)
    try:
        return list(con.iterdump())
    finally:
        con.close()


def _scenario(work, *, old_code_gen, db_min_reader):
    """Готовит песочницу, запускает self-update.sh и возвращает (env-пути, статус)."""
    install_dir = os.path.join(work, "install")
    data_dir = os.path.join(work, "data")
    plans_dir = os.path.join(data_dir, "plans")
    db_path = os.path.join(data_dir, "state.db")
    bin_dir = os.path.join(work, "bin")
    for d in (os.path.join(install_dir, "wb_energy_meter"), os.path.join(install_dir, "scripts"),
              plans_dir, bin_dir):
        os.makedirs(d, exist_ok=True)

    gen_line = "" if old_code_gen is None else "__code_generation__ = %d\n" % old_code_gen
    with open(os.path.join(install_dir, "wb_energy_meter", "__init__.py"), "w", encoding="utf-8") as f:
        f.write(OLD_INIT % gen_line)
    with open(os.path.join(install_dir, "wb_energy_meter", "main.py"), "w", encoding="utf-8") as f:
        f.write("# old main\n")
    with open(os.path.join(install_dir, "VERSION.json"), "w", encoding="utf-8") as f:
        f.write(OLD_VERSION_JSON)
    with open(os.path.join(install_dir, "scripts", "install.sh"), "w", encoding="utf-8") as f:
        f.write(OLD_INSTALL_SH)
    with open(os.path.join(plans_dir, "plan_1_old.png"), "wb") as f:
        f.write(b"OLD")

    con = sqlite3.connect(db_path)
    con.execute("CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT, updated_at INTEGER)")
    con.execute("CREATE TABLE schema_migrations (version INTEGER)")
    con.execute("CREATE TABLE points_marker (id INTEGER PRIMARY KEY, name TEXT)")
    con.execute("INSERT INTO schema_migrations VALUES (1)")
    con.execute("INSERT INTO schema_migrations VALUES (20)")
    con.execute("INSERT INTO points_marker (name) VALUES ('Ввод щита')")
    if db_min_reader is not None:
        for key, value in (("domain_model_generation", 2), ("minimum_reader_generation", db_min_reader),
                           ("model_v2_first_write_revision", "rev-1")):
            con.execute("INSERT INTO kv VALUES (?, ?, 0)", (key, json.dumps(value)))
    con.commit()
    con.close()

    # «Новый» архив
    src = os.path.join(work, "src", "wb-energy-meter-main")
    for sub in ("wb_energy_meter/migrations", "wb_energy_meter/static", "scripts"):
        os.makedirs(os.path.join(src, sub), exist_ok=True)
    with open(os.path.join(src, "wb_energy_meter", "__init__.py"), "w", encoding="utf-8") as f:
        f.write(NEW_INIT)
    for rel, text in (("wb_energy_meter/main.py", "# new main\n"),
                      ("wb_energy_meter/migrations/001_initial_schema.sql", "-- x\n"),
                      ("wb_energy_meter/static/index.html", "<html></html>\n")):
        with open(os.path.join(src, rel), "w", encoding="utf-8") as f:
            f.write(text)
    _write_exec(os.path.join(src, "scripts", "install.sh"), FAKE_INSTALL_SH)
    archive = os.path.join(work, "new.tar.gz")
    subprocess.run(["tar", "-czf", archive, "-C", os.path.dirname(src), "wb-energy-meter-main"], check=True)

    _write_exec(os.path.join(bin_dir, "curl"), FAKE_CURL)
    _write_exec(os.path.join(bin_dir, "systemctl"),
                '#!/bin/bash\necho "$@" >> "%s"\nexit 0\n' % os.path.join(work, "systemctl.log"))
    _write_exec(os.path.join(bin_dir, "sleep"), "#!/bin/bash\nexit 0\n")

    status_file = os.path.join(work, "update-status.json")
    env = dict(os.environ)
    env.update({
        "PATH": bin_dir + os.pathsep + env.get("PATH", ""),
        "REPO_OWNER": "9043366188-dot", "REPO_NAME": "wb-energy-meter", "REF": "main",
        "EXPECTED_SHA": "newsha", "STATUS_FILE": status_file,
        "INSTALL_DIR": install_dir, "DATA_DIR": data_dir, "DB_PATH": db_path, "PLANS_DIR": plans_dir,
        "LOCK_DIR": os.path.join(work, "lock"), "LOG_DIR": os.path.join(work, "log"),
        "HTTP_PORT": "18080", "ARCHIVE": archive,
    })
    before = {
        "dump": _dump(db_path),
        "init": OLD_INIT % gen_line,
    }
    proc = subprocess.run(["bash", SCRIPT], env=env, timeout=120,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
    with open(status_file, encoding="utf-8") as f:
        status = json.load(f)
    log_path = os.path.join(work, "log", "update.log")
    log = open(log_path, encoding="utf-8").read() if os.path.exists(log_path) else ""
    paths = {"install": install_dir, "db": db_path, "plans": plans_dir, "log": log,
             "stderr": proc.stderr, "work": work}
    return paths, status, before


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _min_reader(db_path):
    con = sqlite3.connect(db_path)
    try:
        row = con.execute("SELECT value FROM kv WHERE key='minimum_reader_generation'").fetchone()
    except sqlite3.Error:
        row = None
    con.close()
    return int(json.loads(row[0])) if row else 1


def _assert_rolled_back_triple(paths, status, before, *, old_code_gen):
    assert status["state"] == "rolled_back", (status, paths["log"][-1500:])
    install = paths["install"]
    assert _read(os.path.join(install, "wb_energy_meter", "__init__.py")) == before["init"], "код не возвращён"
    assert _read(os.path.join(install, "VERSION.json")) == OLD_VERSION_JSON, "VERSION.json не возвращён"
    assert _read(os.path.join(install, "scripts", "install.sh")) == OLD_INSTALL_SH, "scripts/ не возвращены"
    assert _dump(paths["db"]) == before["dump"], "БД не равна снимку до обновления"
    for suffix in ("-wal", "-shm"):
        assert not os.path.exists(paths["db"] + suffix), "остался хвост " + suffix
    kept = [n for n in os.listdir(os.path.dirname(paths["db"])) if n.startswith("state.db.pre-rollback-")]
    assert len(kept) == 1, "нет копии БД после неудачной установки: %s" % os.listdir(os.path.dirname(paths["db"]))
    assert any("new_table" in line for line in _dump(os.path.join(os.path.dirname(paths["db"]), kept[0]))), \
        "копия pre-rollback должна хранить состояние после неудачи"
    assert sorted(os.listdir(paths["plans"])) == ["plan_1_old.png"], os.listdir(paths["plans"])
    assert _read_bytes(os.path.join(paths["plans"], "plan_1_old.png")) == b"OLD"
    # согласованность тройки: код отката умеет читать восстановленную БД
    code_gen = 1 if old_code_gen is None else old_code_gen
    assert code_gen >= _min_reader(paths["db"]), "код отката младше minimum_reader_generation БД"
    calls = open(os.path.join(paths["work"], "systemctl.log"), encoding="utf-8").read()
    assert "stop wb-energy-meter.service" in calls and "start wb-energy-meter.service" in calls, calls


def _read_bytes(path):
    with open(path, "rb") as f:
        return f.read()


def test_rollback_before_first_v2_write_restores_consistent_triple():
    work = tempfile.mkdtemp(prefix="wbem-a42-a-")
    try:
        paths, status, before = _scenario(work, old_code_gen=None, db_min_reader=None)
        assert "check_rollback_generation: код отката поколения 1, БД" in paths["log"], paths["log"][-1200:]
        _assert_rolled_back_triple(paths, status, before, old_code_gen=None)
        print("[OK] A42: откат до первой записи v2 — код, VERSION.json, scripts/, БД (iterdump) и изображения "
              "плана равны состоянию до обновления; копия pre-rollback хранит состояние после неудачи")
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_rollback_after_v2_write_to_pre_v2_code_is_blocked_and_touches_nothing():
    work = tempfile.mkdtemp(prefix="wbem-a42-b-")
    try:
        paths, status, before = _scenario(work, old_code_gen=None, db_min_reader=2)
        assert status["state"] == "failed", (status, paths["log"][-1500:])
        assert "Откат заблокирован" in json.dumps(status, ensure_ascii=False), status
        assert "ОТКАТ ЗАБЛОКИРОВАН" in paths["log"]
        install = paths["install"]
        assert "NEW_CODE" in _read(os.path.join(install, "wb_energy_meter", "__init__.py")), \
            "заблокированный откат не должен трогать код"
        assert any("new_table" in line for line in _dump(paths["db"])), "БД должна остаться как после установки"
        assert "plan_1_new.png" in os.listdir(paths["plans"]), "изображения не должны быть тронуты"
        kept = [n for n in os.listdir(os.path.dirname(paths["db"])) if n.startswith("state.db.pre-rollback-")]
        assert kept == [], "при блокировке копий pre-rollback быть не должно"
        print("[OK] A42: после первой записи v2 откат на код без поддержки v2 заблокирован — статус failed, "
              "код, БД и изображения остались как после установки")
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_rollback_after_v2_write_to_v2_aware_code_restores_triple():
    work = tempfile.mkdtemp(prefix="wbem-a42-c-")
    try:
        paths, status, before = _scenario(work, old_code_gen=3, db_min_reader=2)
        _assert_rolled_back_triple(paths, status, before, old_code_gen=3)
        con = sqlite3.connect(paths["db"])
        gen = json.loads(con.execute("SELECT value FROM kv WHERE key='domain_model_generation'").fetchone()[0])
        con.close()
        assert gen == 2 and _min_reader(paths["db"]) == 2
        print("[OK] A42: после первой записи v2 откат на код поколения 3 разрешён — БД вернулась к снимку "
              "(поколение домена 2), код и изображения прежние")
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    if not (shutil.which("bash") and shutil.which("tar") and shutil.which("flock")):
        print("[SKIP] нет bash/tar/flock — тест пропущен (не Linux)")
        sys.exit(0)
    test_rollback_before_first_v2_write_restores_consistent_triple()
    test_rollback_after_v2_write_to_pre_v2_code_is_blocked_and_touches_nothing()
    test_rollback_after_v2_write_to_v2_aware_code_restores_triple()
    print("\nВсе тесты Шага 76 (A42, откат self-update.sh) пройдены.")

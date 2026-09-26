#!/bin/sh
# Партия 10, этап C (F4, docs/migration-plan-v2.md §7 п.3;
# docs/TZ-batch10-reliability-and-load.md §3).
#
# Страж поколений домена — подключается как ExecStartPre юнита systemd
# (scripts/install.sh). До этой партии проверка "код умеет читать эту
# БД" существовала только внутри scripts/self-update.sh
# (check_rollback_generation) и срабатывала ТОЛЬКО во время
# автоматического отката самим self-update.sh. Если пользователь
# откатывался на старую версию вручную (scripts/install.sh из старого
# коммита, ручной systemctl start) — сервис поднимался на БД нового
# поколения домена молча и работал с данными, которых не понимает,
# рискуя тихо потерять их видимость. Этот скрипт закрывает именно этот
# путь: он не зависит от self-update.sh и срабатывает при КАЖДОМ
# старте сервиса.
#
# POSIX sh (не bash) — годится под dash/ash; python3 -c берёт только
# стандартную библиотеку (sqlite3, json), как и во всех остальных
# бэкап/проверочных сниппетах этой партии.
#
# Использование:
#   check-generation.sh <db_path> <init_py_path>
#
# Коды выхода:
#   0 — запуск можно продолжать (БД ещё нет, ключа нет, легаси-БД без
#       таблицы kv, либо поколение кода >= требуемого)
#   1 — запуск нужно ЗАБЛОКИРОВАТЬ: код старше, чем требует БД, либо БД
#       повреждена и поколение достоверно определить невозможно

set -eu

DB_PATH="${1:?использование: check-generation.sh <db_path> <init_py_path>}"
INIT_PY="${2:?использование: check-generation.sh <db_path> <init_py_path>}"

# Первый запуск на контроллере (БД ещё не создана демоном) — это не
# ошибка, а нормальное состояние "до первого старта".
if [ ! -f "$DB_PATH" ]; then
  exit 0
fi

# Поколение, которое понимает УСТАНОВЛЕННЫЙ код — тем же способом,
# каким install.sh уже читает __version__ из этого же файла (grep +
# sed по объявлению константы), не импортируя пакет целиком (тот же
# принцип, что и у self-update.sh::check_rollback_generation — при
# сломанном пакете чтение одной строки текстом обязано отработать).
CODE_GEN="$(grep -m1 '__code_generation__' "$INIT_PY" 2>/dev/null \
    | sed -E 's/.*__code_generation__[[:space:]]*=[[:space:]]*([0-9]+).*/\1/')"
case "$CODE_GEN" in
  ''|*[!0-9]*)
    # Константы в файле нет вовсе (код старше её появления) —
    # wb_energy_meter/domain_generation.py::LEGACY_GENERATION по
    # определению равна 1.
    CODE_GEN=1
    ;;
esac

# Минимальное поколение, которое требует САМА БД. sqlite3.OperationalError
# ловится ОТДЕЛЬНО от общего sqlite3.Error и трактуется как "легаси-БД
# без таблицы kv вообще" (совместима с любым кодом) — а не как порча:
# sqlite3.OperationalError ("no such table: kv") — ПОДКЛАСС
# sqlite3.DatabaseError, поэтому ловить их надо в этом порядке (сначала
# частный случай, потом общий), иначе настоящая порча файла
# (sqlite3.DatabaseError: file is not a database) будет молча принята
# за отсутствие таблицы.
set +e
DB_MIN_GEN="$(python3 -c '
import json, sqlite3, sys

db_path = sys.argv[1]
try:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT value FROM kv WHERE key = ?",
            ("minimum_reader_generation",)).fetchone()
    except sqlite3.OperationalError:
        # Например "no such table: kv" -- легаси-БД без единого ключа
        # поколений: по определению поколение 1, совместима с любым
        # текущим кодом.
        row = None
    finally:
        conn.close()
except sqlite3.Error as e:
    sys.stderr.write("corrupt: %s\n" % e)
    sys.exit(2)

if not row:
    print(1)
else:
    try:
        print(int(json.loads(row[0])))
    except (TypeError, ValueError, json.JSONDecodeError):
        print(1)
' "$DB_PATH" 2>&1)"
PY_STATUS=$?
set -e

if [ "$PY_STATUS" -eq 2 ]; then
  echo "ЗАБЛОКИРОВАН ЗАПУСК: БД ($DB_PATH) повреждена или недоступна для чтения -- надёжно определить поколение домена нельзя, а запускать код вслепую поверх такой БД опаснее, чем не запускать вовсе. Подробности: $DB_MIN_GEN. Восстановите БД из резервной копии (${DB_PATH}.backup-*, см. scripts/install.sh) и повторите запуск." >&2
  exit 1
fi

case "$DB_MIN_GEN" in
  ''|*[!0-9]*) DB_MIN_GEN=1 ;;
esac

if [ "$CODE_GEN" -lt "$DB_MIN_GEN" ]; then
  echo "ЗАБЛОКИРОВАН ЗАПУСК: БД ($DB_PATH) требует поколение домена не ниже $DB_MIN_GEN, а установленный код ($INIT_PY) понимает только поколение $CODE_GEN. Поставьте версию wb-energy-meter, доводящую код минимум до поколения $DB_MIN_GEN (docs/migration-plan-v2.md §7), прежде чем запускать сервис на этой БД." >&2
  exit 1
fi

exit 0

#!/bin/bash
# scripts/self-update.sh — самообновление wb-energy-meter из GitHub (ТЗ v0.9.0).
#
# Источник — ветка main (не теги): что в main на момент проверки в
# UI, то и приезжает на контроллер. Запускается от root, без
# аргументов — все параметры приходят через переменные окружения,
# которые задаёт wb_energy_meter/updater.py::start_update().
#
# Переменные окружения (обязательные, кроме REF/INSTALL_DIR/HTTP_PORT):
#   REPO_OWNER, REPO_NAME, REF (по умолчанию main), EXPECTED_SHA,
#   STATUS_FILE, INSTALL_DIR (по умолчанию /opt/wb-energy-meter),
#   HTTP_PORT (по умолчанию 8080)
#
# ВАЖНО — запуск. Этот скрипт обязан стартовать отдельным transient-
# юнитом systemd-run (--collect), НЕ обычным дочерним процессом сервиса:
#
#   systemd-run --unit=wb-energy-meter-update --collect \
#       --description="Обновление wb-energy-meter" \
#       /bin/bash /opt/wb-energy-meter/scripts/self-update.sh
#
# Причина в AGENTS.md: у wb-energy-meter.service KillMode=mixed и
# MemoryMax=256M. Если апдейтер — дочерний процесс сервиса, он попадает
# в ту же cgroup, и шаг 7 ниже (`systemctl stop` внутри install.sh)
# убивает systemd ВСЮ cgroup, включая сам апдейтер — обновление
# обрывается на середине, в /opt остаётся полускопированное дерево, а
# сервис не поднимается. systemd-run выносит апдейтер из cgroup
# сервиса: его останов больше не задевает.
#
# ВАЖНО — самомодификация. Шаг "installing" запускает install.sh,
# который (по ТЗ §4.2) теперь копирует ВЕСЬ scripts/, включая этот
# самый файл — то есть self-update.sh переписывает себя на диске, пока
# выполняется. Чтобы это не оборвало скрипт на середине (классическая
# грабля bash: `cp` открывает существующий файл с O_TRUNC, а shell
# читает свой скрипт с диска по мере выполнения), ВСЯ логика ниже
# обёрнута в функцию main(), вызываемую последней строкой файла. Bash
# обязан полностью разобрать (прочитать с диска) тело функции до того,
# как начнёт её исполнять — то есть к моменту запуска main() файл уже
# прочитан целиком, а дальше исполнение идёт из уже разобранного дерева
# команд в памяти, а не повторным чтением с диска. Не выносите код за
# пределы функций, определённых до финального `main "$@"`.

set -euo pipefail

REPO_OWNER="${REPO_OWNER:?REPO_OWNER не задан}"
REPO_NAME="${REPO_NAME:?REPO_NAME не задан}"
REF="${REF:-main}"
EXPECTED_SHA="${EXPECTED_SHA:?EXPECTED_SHA не задан}"
STATUS_FILE="${STATUS_FILE:?STATUS_FILE не задан}"
INSTALL_DIR="${INSTALL_DIR:-/opt/wb-energy-meter}"
HTTP_PORT="${HTTP_PORT:-8080}"

# docs/migration-plan-v2.md §2/§7: та же конвенция путей, что и
# scripts/install.sh (DATA_DIR/DB_PATH) — self-update.sh их не задаёт
# сам, поэтому должен совпадать буква в букву, иначе резервная копия
# БД будет молча снимать пустое место. Каталог изображений плана —
# сиблинг state.db (см. wb_energy_meter/plan_repo.py::plans_dir()).
DATA_DIR="${DATA_DIR:-/mnt/data/var/lib/wb-energy-meter}"
DB_PATH="${DB_PATH:-$DATA_DIR/state.db}"
PLANS_DIR="${PLANS_DIR:-$DATA_DIR/plans}"

LOCK_DIR="${LOCK_DIR:-/run/lock}"
LOCK_FILE="$LOCK_DIR/wb-energy-meter-update.lock"
LOG_DIR="${LOG_DIR:-/var/log/wb-energy-meter}"
LOG_FILE="$LOG_DIR/update.log"
ROLLBACK_DIR="${INSTALL_DIR}.rollback"
# БД/картинки плана бэкапятся отдельно от кода, но внутри того же
# каталога — он и так целиком создаётся заново в backup_current() и
# целиком удаляется при успехе (см. main()), значит и уборка бесплатная.
DATA_BACKUP_DIR="$ROLLBACK_DIR/_data_backup"

STAGE="init"
HAVE_LOCK=0
FINISHED=0
TMP_DIR=""
SRC_DIR=""
NEW_VERSION=""
# Заполняются run_selfcheck() (ТЗ v0.11.1, §2.2) и читаются из
# attempt_rollback(), чтобы статус/лог содержали не просто "не прошло",
# а конкретный список файлов, которые не отдались.
SELFCHECK_STATE=""
SELFCHECK_DETAILS=""
# Версия схемы БД ДО этого запуска (backup_data(), до install_new_code())
# — verify_schema_version() сверяет с версией ПОСЛЕ, миграции необратимы,
# поэтому "после" меньше "до" может значить только подмену/повреждение
# БД (docs/migration-plan-v2.md §7 п.5).
SCHEMA_VERSION_BEFORE=""

# ---------------------------------------------------------------------
# Утилиты
# ---------------------------------------------------------------------

log_line() {
  printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$1" >>"$LOG_FILE" 2>/dev/null || true
}

rotate_log() {
  # Ротация: хранить последние 5 файлов или обрезать по 1 МБ (§4.1 п.7 ТЗ).
  if [[ -f "$LOG_FILE" ]]; then
    local size
    size="$(stat -c%s "$LOG_FILE" 2>/dev/null || echo 0)"
    if [[ "$size" -gt 1048576 ]]; then
      local ts
      ts="$(date +%Y%m%d-%H%M%S)"
      mv "$LOG_FILE" "${LOG_FILE}.${ts}" 2>/dev/null || true
      ls -1t "${LOG_FILE}".* 2>/dev/null | tail -n +6 | xargs -r rm -f 2>/dev/null || true
    fi
  fi
}

# Пишет/сливает поля статуса. Логика та же, что в
# wb_energy_meter.updater.write_status (слияние полей + атомарная запись
# tmp + os.replace, чтобы конкурентный GET /api/update/status никогда не
# увидел обрезанный файл), но реализована здесь ВСТРОЕННО, на голой
# стандартной библиотеке.
#
# ВАЖНО — почему не импортируем wb_energy_meter.updater:
# install.sh делает `rm -rf $INSTALL_DIR/wb_energy_meter` и только потом
# копирует новый код. Если установка упадёт в этом промежутке, модуля
# updater просто не существует, импорт падает — и все последующие
# step_status молча не срабатывают. Статус навсегда остаётся
# "installing", а веб-интерфейс вечно показывает «идёт обновление» без
# возможности что-либо предпринять. Писать статус обязаны уметь именно
# тогда, когда всё сломалось, поэтому зависимости от обновляемого кода
# здесь быть не должно.
_write_status_py() {
  python3 -c '
import json, os, sys, tempfile

status_file = sys.argv[1]
fields = {}
rest = sys.argv[2:]
numeric = {"started_at", "finished_at"}
for k, v in zip(rest[0::2], rest[1::2]):
    if k == "log_tail":
        fields.setdefault("log_tail", []).append(v)
        continue
    if k in numeric:
        try:
            v = int(v)
        except ValueError:
            pass
    fields[k] = v

data = {}
try:
    with open(status_file, "r", encoding="utf-8") as f:
        loaded = json.load(f)
    if isinstance(loaded, dict):
        data = loaded
except (OSError, ValueError):
    data = {}
data.update(fields)

d = os.path.dirname(os.path.abspath(status_file)) or "."
os.makedirs(d, exist_ok=True)
fd, tmp = tempfile.mkstemp(dir=d, prefix=".update-status-", suffix=".tmp")
try:
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, status_file)
except BaseException:
    try:
        os.unlink(tmp)
    except OSError:
        pass
    raise
' "$@"
}

step_status() {
  local state="$1"; shift
  local label="$1"; shift
  _write_status_py "$STATUS_FILE" state "$state" step_label "$label" "$@" \
    2>>"$LOG_FILE" \
    || log_line "[!] не удалось записать статус '$state' в $STATUS_FILE"
}

attach_log_tail() {
  local -a lines=()
  local -a args=()
  local l
  if [[ -f "$LOG_FILE" ]]; then
    mapfile -t lines < <(tail -n 20 "$LOG_FILE" 2>/dev/null || true)
  fi
  for l in ${lines[@]+"${lines[@]}"}; do
    args+=("log_tail" "$l")
  done
  _write_status_py "$STATUS_FILE" ${args[@]+"${args[@]}"} \
    2>>"$LOG_FILE" || true
}

finish_status() {
  step_status "$@"
  attach_log_tail
}

# ---------------------------------------------------------------------
# Блокировка
# ---------------------------------------------------------------------

acquire_lock() {
  mkdir -p "$LOCK_DIR"
  exec 9>"$LOCK_FILE"
  if ! flock -n 9; then
    echo "ОШИБКА: обновление уже идёт (заблокировано $LOCK_FILE)" >&2
    log_line "ОШИБКА: не удалось захватить блокировку $LOCK_FILE — обновление уже идёт"
    exit 1
  fi
  HAVE_LOCK=1
  log_line "Блокировка захвачена: $LOCK_FILE"
}

release_lock() {
  if [[ "$HAVE_LOCK" -eq 1 ]]; then
    flock -u 9 2>/dev/null || true
    exec 9>&- 2>/dev/null || true
  fi
}

# ---------------------------------------------------------------------
# Шаги обновления
# ---------------------------------------------------------------------

download_archive() {
  local url="https://github.com/${REPO_OWNER}/${REPO_NAME}/archive/refs/heads/${REF}.tar.gz"
  ARCHIVE_PATH="$TMP_DIR/source.tar.gz"
  log_line "Скачиваю: $url"
  curl -fsSL --max-time 120 -o "$ARCHIVE_PATH" "$url" 2>>"$LOG_FILE"
  log_line "Архив скачан: $(du -h "$ARCHIVE_PATH" 2>/dev/null | cut -f1)"
}

extract_archive() {
  tar -xzf "$ARCHIVE_PATH" -C "$TMP_DIR"
  SRC_DIR="$(find "$TMP_DIR" -mindepth 1 -maxdepth 1 -type d ! -name 'lost+found' | head -1)"
  if [[ -z "$SRC_DIR" || ! -d "$SRC_DIR" ]]; then
    log_line "ОШИБКА: не нашёл папку с исходниками в распакованном архиве"
    return 1
  fi
  log_line "Распаковано в: $SRC_DIR"
}

verify_archive_contents() {
  # Должны существовать до подмены рабочего кода (§4.1 п.4 ТЗ) —
  # "полускопированное дерево" здесь не про install.sh, а про то, чтобы
  # даже не пытаться ставить архив без нужных файлов.
  local required=(
    "wb_energy_meter/main.py"
    "wb_energy_meter/__init__.py"
    "wb_energy_meter/migrations/001_initial_schema.sql"
    "wb_energy_meter/static/index.html"
    "scripts/install.sh"
  )
  local f
  for f in "${required[@]}"; do
    if [[ ! -f "$SRC_DIR/$f" ]]; then
      log_line "ОШИБКА: в архиве отсутствует обязательный файл: $f"
      return 1
    fi
  done
  log_line "Целостность архива подтверждена (${#required[@]} обязательных файлов на месте)"
}

syntax_check_new_code() {
  if ! python3 -m compileall -q "$SRC_DIR/wb_energy_meter" >>"$LOG_FILE" 2>&1; then
    log_line "ОШИБКА: новый код не проходит compileall — рабочая версия не тронута"
    return 1
  fi
  log_line "compileall нового кода — чисто"
  NEW_VERSION="$(grep -m1 '__version__' "$SRC_DIR/wb_energy_meter/__init__.py" \
      | sed -E "s/.*__version__[[:space:]]*=[[:space:]]*[\"']([^\"']+)[\"'].*/\1/")"
  if [[ -z "$NEW_VERSION" ]]; then
    NEW_VERSION="unknown"
  fi
  log_line "Версия в новом коде: $NEW_VERSION"
}

backup_current() {
  rm -rf "$ROLLBACK_DIR"
  cp -a "$INSTALL_DIR" "$ROLLBACK_DIR"
  log_line "Резервная копия рабочего каталога создана: $ROLLBACK_DIR"
}

# docs/migration-plan-v2.md §2/§7 п.4: state.db бэкапится ЧЕРЕЗ Online
# Backup API (sqlite3.Connection.backup), НЕ простым cp — простой cp
# поверх файла, открытого демоном в WAL-режиме, может снять рваный,
# несогласованный снимок (см. докстринг модуля §2). Сервис на этом шаге
# ещё работает (его останавливают только install.sh/systemctl stop
# внутри install_new_code() — до этого момента мы ещё не дошли), поэтому
# именно Online Backup API, а не файловый cp, здесь обязателен.
#
# Зависимостей от пакета wb_energy_meter здесь нет намеренно — та же
# причина, что у _write_status_py() выше: этот шаг обязан отработать,
# даже если пакет уже частично удалён install.sh.
backup_data() {
  mkdir -p "$DATA_BACKUP_DIR"

  if [[ -f "$DB_PATH" ]]; then
    SCHEMA_VERSION_BEFORE="$(python3 -c '
import sqlite3, sys
try:
    conn = sqlite3.connect(sys.argv[1])
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type=\"table\" AND name=\"schema_migrations\""
    ).fetchone()
    if row is None:
        print(0)
    else:
        row = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
        print(row[0] if row and row[0] is not None else 0)
    conn.close()
except sqlite3.Error:
    print(0)
' "$DB_PATH" 2>>"$LOG_FILE")"
    if [[ -z "$SCHEMA_VERSION_BEFORE" || ! "$SCHEMA_VERSION_BEFORE" =~ ^[0-9]+$ ]]; then
      SCHEMA_VERSION_BEFORE=0
    fi
    log_line "Версия схемы БД до обновления: $SCHEMA_VERSION_BEFORE"

    if ! python3 -c '
import os, sqlite3, sys, tempfile

src_path, dst_path = sys.argv[1], sys.argv[2]
d = os.path.dirname(os.path.abspath(dst_path)) or "."
os.makedirs(d, exist_ok=True)
fd, tmp = tempfile.mkstemp(dir=d, prefix=".state-backup-", suffix=".sqlite3")
os.close(fd)
os.unlink(tmp)
try:
    src = sqlite3.connect(src_path)
    try:
        dst = sqlite3.connect(tmp)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    os.replace(tmp, dst_path)
except BaseException:
    try:
        os.unlink(tmp)
    except OSError:
        pass
    raise
' "$DB_PATH" "$DATA_BACKUP_DIR/state.db" >>"$LOG_FILE" 2>&1; then
      log_line "ОШИБКА: не удалось создать резервную копию БД ($DB_PATH -> $DATA_BACKUP_DIR/state.db)"
      return 1
    fi
    log_line "Резервная копия БД создана через Online Backup API: $DATA_BACKUP_DIR/state.db"
  else
    SCHEMA_VERSION_BEFORE=0
    log_line "backup_data: $DB_PATH ещё не существует (первый запуск на этом контроллере до первого старта сервиса) — резервную копию БД пропускаю"
  fi

  if [[ -d "$PLANS_DIR" ]]; then
    rm -rf "$DATA_BACKUP_DIR/plans"
    if ! cp -a "$PLANS_DIR" "$DATA_BACKUP_DIR/plans" 2>>"$LOG_FILE"; then
      log_line "ОШИБКА: не удалось скопировать каталог изображений плана $PLANS_DIR"
      return 1
    fi
    log_line "Резервная копия каталога изображений плана создана: $DATA_BACKUP_DIR/plans"
  else
    log_line "backup_data: каталог изображений плана $PLANS_DIR отсутствует (планов ещё нет) — пропускаю"
  fi
}

install_new_code() {
  log_line "Запускаю install.sh из $SRC_DIR (SKIP_APT=1, SOURCE_SHA=$EXPECTED_SHA)"
  local rc=0
  ( cd "$SRC_DIR" \
      && SKIP_APT=1 SOURCE_SHA="$EXPECTED_SHA" SOURCE_REF="$REF" \
         bash scripts/install.sh ) >>"$LOG_FILE" 2>&1 || rc=$?
  if [[ "$rc" -ne 0 ]]; then
    log_line "install.sh завершился с ошибкой (код $rc)"
    return "$rc"
  fi
  log_line "install.sh выполнен успешно"
}

wait_for_health() {
  # До 90 секунд, опрос раз в 3 с (§4.1 п.8 ТЗ). Только is-active
  # недостаточно: процесс может стартовать и падать в цикле по
  # Restart=on-failure — поэтому обязательно ещё и /health.
  local attempts=30 i
  for ((i = 0; i < attempts; i++)); do
    if systemctl is-active --quiet wb-energy-meter.service 2>/dev/null \
        && curl -fsS --max-time 3 "http://127.0.0.1:${HTTP_PORT}/health" >/dev/null 2>&1; then
      return 0
    fi
    sleep 3
  done
  return 1
}

run_selfcheck() {
  # ТЗ v0.11.1, §2.2. /health отвечает "всё хорошо" даже когда интерфейс
  # не грузится (белый экран 09.09.2026, см. AGENTS.md) — демон жив, а
  # alpine.min.js не отдаётся, и /health этого не видит. /api/selfcheck
  # проверяет именно это: что все файлы, на которые ссылается index.html,
  # реально отдаются. Разбираем ответ без jq (на контроллере его может не
  # быть) — python3 -c с json.load из stdin.
  #
  # 404 на этом эндпоинте — НЕ провал (§2.3 ТЗ): значит на этой версии
  # /api/selfcheck ещё/уже нет (например, откат на v0.11.0) — считаем
  # проверку недоступной и опираемся на /health, как раньше. Так же
  # трактуем и обрыв соединения (curl не смог достучаться) — /health уже
  # подтвердил, что сервис живой и отвечает на этом порту, единичный сбой
  # именно этого запроса не повод откатывать рабочую версию. А вот любой
  # ДРУГОЙ код ответа (500 и т.п.) — это уже сама самопроверка сломана,
  # и это провал.
  local url="http://127.0.0.1:${HTTP_PORT}/api/selfcheck"
  local body http_code py_out status_line
  body="$(mktemp)"

  if ! http_code="$(curl -sS --max-time 5 -o "$body" -w '%{http_code}' "$url" 2>>"$LOG_FILE")"; then
    log_line "selfcheck: запрос к $url не удался (curl) — считаю недоступной, полагаюсь на /health"
    rm -f "$body"
    SELFCHECK_STATE="unavailable"
    return 0
  fi

  if [[ "$http_code" == "404" ]]; then
    log_line "selfcheck: /api/selfcheck отвечает 404 (версия без этого эндпоинта) — не провал, опираюсь на /health"
    rm -f "$body"
    SELFCHECK_STATE="unavailable"
    return 0
  fi

  if [[ "$http_code" != "200" ]]; then
    log_line "selfcheck: неожиданный код ответа $http_code от $url"
    SELFCHECK_DETAILS="HTTP $http_code от /api/selfcheck"
    rm -f "$body"
    SELFCHECK_STATE="failed"
    return 1
  fi

  py_out="$(python3 -c '
import json, sys
try:
    data = json.load(sys.stdin)
except Exception as e:
    print("PARSE_ERROR")
    print(str(e))
    sys.exit(0)
print("OK" if data.get("ok") else "FAIL")
for item in (data.get("failed") or []):
    ref = str(item.get("ref", "?"))
    reason = str(item.get("reason", "?"))
    print(ref + ": " + reason)
' <"$body" 2>>"$LOG_FILE")"
  rm -f "$body"

  status_line="$(printf '%s\n' "$py_out" | head -n1)"
  SELFCHECK_DETAILS="$(printf '%s\n' "$py_out" | tail -n +2 | tr '\n' ';' | sed 's/;$//; s/;/; /g')"

  if [[ "$status_line" == "OK" ]]; then
    log_line "selfcheck: ok=true"
    SELFCHECK_STATE="ok"
    return 0
  fi

  log_line "selfcheck: не прошла ($status_line): ${SELFCHECK_DETAILS:-без подробностей}"
  SELFCHECK_STATE="failed"
  return 1
}

verify_schema_version() {
  # docs/migration-plan-v2.md §7 п.5. Миграции необратимы (§6:
  # "версии <= current пропускаются") — версия схемы после установки
  # обязана быть >= версии до неё. Меньше — значит не тот файл БД,
  # повреждение, либо кто-то подменил $DB_PATH вручную между шагами;
  # в любом случае доверять только что установленному коду в такой
  # ситуации нельзя.
  local version_after
  version_after="$(python3 -c '
import sqlite3, sys
try:
    conn = sqlite3.connect(sys.argv[1])
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type=\"table\" AND name=\"schema_migrations\""
    ).fetchone()
    if row is None:
        print(0)
    else:
        row = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
        print(row[0] if row and row[0] is not None else 0)
    conn.close()
except sqlite3.Error:
    print(0)
' "$DB_PATH" 2>>"$LOG_FILE")"

  if [[ -z "$version_after" || ! "$version_after" =~ ^[0-9]+$ ]]; then
    log_line "ОШИБКА verify_schema_version: не удалось прочитать версию схемы БД после установки ($DB_PATH)"
    return 1
  fi

  log_line "Версия схемы БД после обновления: $version_after (была: ${SCHEMA_VERSION_BEFORE:-0})"

  if [[ "$version_after" -lt "${SCHEMA_VERSION_BEFORE:-0}" ]]; then
    log_line "ОШИБКА verify_schema_version: версия схемы УМЕНЬШИЛАСЬ (${SCHEMA_VERSION_BEFORE:-0} -> $version_after) — миграции необратимы, это не должно происходить"
    return 1
  fi
  return 0
}

verify_control_calculation() {
  # docs/migration-plan-v2.md §7 п.5: "контрольный расчёт на одной
  # существующей точке" — /health и /api/selfcheck проверяют, что
  # сервис жив и отдаёт статику, но не то, что расчётный путь /api/v2
  # (accounting_service, "сердце ТЗ") реально считает поверх ЭТОЙ БД на
  # ЭТОЙ версии кода. Если точек ещё нет (свежая установка до первой
  # миграции) — это не провал, считать не на чем, пропускаем.
  local points_body point_id http_code calc_body py_out

  points_body="$(mktemp)"
  if ! curl -sS --max-time 5 -o "$points_body" "http://127.0.0.1:${HTTP_PORT}/api/v2/points" 2>>"$LOG_FILE"; then
    log_line "verify_control_calculation: не удалось получить список точек — пропускаю (не провал, /health и /api/selfcheck уже подтвердили, что сервис жив)"
    rm -f "$points_body"
    return 0
  fi

  point_id="$(python3 -c '
import json, sys
try:
    with open(sys.argv[1], "r", encoding="utf-8") as f:
        data = json.load(f)
except Exception:
    print("")
    sys.exit(0)
items = data.get("items") if isinstance(data, dict) else data
if not items:
    print("")
    sys.exit(0)
first = items[0]
print(first.get("id", "") if isinstance(first, dict) else "")
' "$points_body" 2>>"$LOG_FILE")"
  rm -f "$points_body"

  if [[ -z "$point_id" ]]; then
    log_line "verify_control_calculation: точек учёта ещё нет — контрольный расчёт пропущен (не провал)"
    return 0
  fi

  calc_body="$(mktemp)"
  local now_ts from_ts
  now_ts="$(date +%s)"
  from_ts="$((now_ts - 3600))"

  if ! http_code="$(curl -sS --max-time 5 -o "$calc_body" -w '%{http_code}' \
      -X POST -H 'Content-Type: application/json' \
      -d "{\"mode\":\"measured\",\"point_ids\":[${point_id}],\"from\":${from_ts},\"to\":${now_ts}}" \
      "http://127.0.0.1:${HTTP_PORT}/api/v2/metrics/query" 2>>"$LOG_FILE")"; then
    log_line "verify_control_calculation: запрос к /api/v2/metrics/query не удался (curl, point_id=$point_id)"
    rm -f "$calc_body"
    return 1
  fi

  py_out="$(python3 -c '
import json, sys
try:
    with open(sys.argv[1], "r", encoding="utf-8") as f:
        data = json.load(f)
except Exception as e:
    print("PARSE_ERROR: " + str(e))
    sys.exit(0)
print("value=" + str(data.get("value")) + " availability=" + str(data.get("availability")))
' "$calc_body" 2>>"$LOG_FILE")"
  rm -f "$calc_body"

  if [[ "$http_code" != "200" ]]; then
    log_line "ОШИБКА verify_control_calculation: /api/v2/metrics/query (point_id=$point_id) вернул HTTP $http_code вместо 200: $py_out"
    return 1
  fi

  log_line "verify_control_calculation: контрольный расчёт для точки $point_id прошёл ($py_out)"
  return 0
}

verify_after_install() {
  if ! wait_for_health; then
    log_line "Сервис не поднялся за 90 секунд после установки"
    return 1
  fi
  log_line "Проверка после установки: сервис активен, /health отвечает"

  if ! verify_schema_version; then
    log_line "Проверка после установки провалена: версия схемы БД после установки некорректна"
    return 1
  fi

  if ! run_selfcheck; then
    log_line "Проверка после установки провалена самопроверкой интерфейса"
    return 1
  fi
  if [[ "$SELFCHECK_STATE" == "unavailable" ]]; then
    log_line "Самопроверка интерфейса недоступна, полагаюсь на /health"
  else
    log_line "Самопроверка интерфейса прошла (/health и /api/selfcheck в порядке)"
  fi

  if ! verify_control_calculation; then
    log_line "Проверка после установки провалена контрольным расчётом"
    return 1
  fi

  log_line "Проверка после установки прошла полностью (/health, версия схемы, /api/selfcheck, контрольный расчёт)"
  return 0
}

check_rollback_generation() {
  # docs/migration-plan-v2.md §7 п.3: ДО того, как трогать
  # $INSTALL_DIR/wb_energy_meter — проверить, не окажется ли
  # откатываемый код (поколение из $ROLLBACK_DIR/wb_energy_meter/
  # __init__.py::__code_generation__) младше minimum_reader_generation,
  # уже объявленного в БД, которая будет восстановлена вместе с ним
  # (см. restore_data_backup() — та же самая $DATA_BACKUP_DIR/state.db).
  # Именно снимок, а не текущая рабочая $DB_PATH: после успешного
  # отката старый код увидит РОВНО этот снимок, не более свежее
  # состояние, которое могло возникнуть уже во время этого (неудачного)
  # запуска self-update.sh.
  local db_for_check="$DATA_BACKUP_DIR/state.db"
  if [[ ! -f "$db_for_check" ]]; then
    log_line "check_rollback_generation: снимок БД ($db_for_check) отсутствует — проверяю по рабочей БД ($DB_PATH) как более слабую замену"
    db_for_check="$DB_PATH"
  fi

  if [[ ! -f "$ROLLBACK_DIR/wb_energy_meter/__init__.py" ]]; then
    # Кода для отката нет вовсе — остальной attempt_rollback это уже
    # логирует отдельно (backup_current не успел/не смог отработать).
    log_line "check_rollback_generation: $ROLLBACK_DIR/wb_energy_meter/__init__.py отсутствует, пропускаю проверку поколения"
    return 0
  fi

  local rollback_code_gen
  rollback_code_gen="$(grep -m1 '__code_generation__' \
      "$ROLLBACK_DIR/wb_energy_meter/__init__.py" 2>/dev/null \
      | sed -E 's/.*__code_generation__[[:space:]]*=[[:space:]]*([0-9]+).*/\1/')"
  if [[ -z "$rollback_code_gen" || ! "$rollback_code_gen" =~ ^[0-9]+$ ]]; then
    # Бэкап кода старше введения этой константы (её там ещё не было) —
    # по определению domain_generation.py::LEGACY_GENERATION это 1.
    rollback_code_gen=1
  fi

  if [[ ! -f "$db_for_check" ]]; then
    log_line "check_rollback_generation: БД для проверки не найдена нигде — откат кода разрешён (БД ещё не было ни разу)"
    return 0
  fi

  local db_min_reader_gen
  db_min_reader_gen="$(python3 -c '
import json, sqlite3, sys

try:
    conn = sqlite3.connect(sys.argv[1])
    row = conn.execute(
        "SELECT value FROM kv WHERE key = ?", ("minimum_reader_generation",)
    ).fetchone()
    conn.close()
except sqlite3.Error:
    print(1)
    sys.exit(0)
if not row:
    print(1)
    sys.exit(0)
try:
    print(int(json.loads(row[0])))
except (TypeError, ValueError, json.JSONDecodeError):
    print(1)
' "$db_for_check" 2>>"$LOG_FILE")"

  if [[ -z "$db_min_reader_gen" || ! "$db_min_reader_gen" =~ ^[0-9]+$ ]]; then
    db_min_reader_gen=1
  fi

  log_line "check_rollback_generation: код отката поколения $rollback_code_gen, БД ($db_for_check) требует минимум $db_min_reader_gen"

  if [[ "$rollback_code_gen" -lt "$db_min_reader_gen" ]]; then
    log_line "ОТКАТ ЗАБЛОКИРОВАН: код отката (поколение $rollback_code_gen) не умеет читать эту БД (minimum_reader_generation=$db_min_reader_gen, docs/migration-plan-v2.md §7 п.3)"
    return 1
  fi
  return 0
}

restore_data_backup() {
  # docs/migration-plan-v2.md §7 п.4: код и БД — совместимая пара.
  # Вызывается ПОСЛЕ check_rollback_generation() (уже подтвердил, что
  # откатываемый код умеет читать этот снимок) и ПОСЛЕ восстановления
  # кода, ПЕРЕД перезапуском сервиса.
  if [[ ! -f "$DATA_BACKUP_DIR/state.db" ]]; then
    log_line "restore_data_backup: снимок БД ($DATA_BACKUP_DIR/state.db) отсутствует — БД не трогаю (бэкап от версии self-update.sh до появления этого шага, либо БД впервые появилась уже после backup_data())"
    return 0
  fi

  if [[ -f "$DB_PATH" ]]; then
    cp -a "$DB_PATH" "${DB_PATH}.pre-rollback-$(date +%Y%m%d-%H%M%S)" 2>>"$LOG_FILE" || true
  fi

  # Восстанавливаем тоже ЧЕРЕЗ Online Backup API (snapshot -> рабочий
  # путь), во временный файл + атомарный os.replace — та же защита от
  # рваного состояния, что и в backup_data(), плюс не оставляем рабочую
  # БД без файла, если процесс восстановления прервётся на середине.
  if python3 -c '
import os, sqlite3, sys, tempfile

backup_path, dst_path = sys.argv[1], sys.argv[2]
d = os.path.dirname(os.path.abspath(dst_path)) or "."
os.makedirs(d, exist_ok=True)
fd, tmp = tempfile.mkstemp(dir=d, prefix=".state-restore-", suffix=".sqlite3")
os.close(fd)
os.unlink(tmp)
try:
    src = sqlite3.connect(backup_path)
    try:
        dst = sqlite3.connect(tmp)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    os.replace(tmp, dst_path)
    for suffix in ("-wal", "-shm"):
        stale = dst_path + suffix
        if os.path.exists(stale):
            os.unlink(stale)
except BaseException:
    try:
        os.unlink(tmp)
    except OSError:
        pass
    raise
' "$DATA_BACKUP_DIR/state.db" "$DB_PATH" >>"$LOG_FILE" 2>&1; then
    log_line "restore_data_backup: БД восстановлена из $DATA_BACKUP_DIR/state.db"
  else
    log_line "ОШИБКА restore_data_backup: не удалось восстановить БД из $DATA_BACKUP_DIR/state.db — рабочая БД оставлена как есть (см. ${DB_PATH}.pre-rollback-* при наличии)"
  fi

  if [[ -d "$DATA_BACKUP_DIR/plans" ]]; then
    rm -rf "$PLANS_DIR" 2>>"$LOG_FILE" || true
    if cp -a "$DATA_BACKUP_DIR/plans" "$PLANS_DIR" 2>>"$LOG_FILE"; then
      log_line "restore_data_backup: каталог изображений плана восстановлен из $DATA_BACKUP_DIR/plans"
    else
      log_line "ОШИБКА restore_data_backup: не удалось восстановить каталог изображений плана"
    fi
  fi
}

attempt_rollback() {
  local rc="$1"
  # ТЗ v0.11.1, §2.2: если причина отката — непройденная самопроверка
  # интерфейса, статус и лог обязаны называть конкретные файлы, а не
  # просто "не прошло" — иначе пользователь опять останется с "обновление
  # откатилось" без единой подсказки, что именно сломалось.
  local reason="Обновление не прошло проверку (код $rc), выполняется откат на предыдущую версию"
  if [[ -n "$SELFCHECK_DETAILS" ]]; then
    reason="Самопроверка интерфейса не прошла: ${SELFCHECK_DETAILS}. Выполняется откат на предыдущую версию"
  fi

  # docs/migration-plan-v2.md §7 п.3: генерационная проверка — ДО того,
  # как вообще что-либо восстанавливать. Используем существующий,
  # уже терминальный для UI (index.html: updIsTerminal проверяет ровно
  # ['success','rolled_back','failed']) статус "failed" — заводить новый
  # "rollback_blocked" означало бы ещё и править фронтенд, а без этого
  # UI навсегда завис бы на "идёт откат" (тот самый класс бага, из-за
  # которого вообще появился STAGE=="done"-guard в on_error, см. коммент
  # у main "$@" в конце файла).
  if ! check_rollback_generation; then
    finish_status "failed" \
      "Откат заблокирован: БД уже содержит данные новой доменной модели, старый код их не понимает" \
      error "Установка $EXPECTED_SHA не прошла проверку (код $rc), но автоматический откат кода заблокирован: БД уже помечена как принадлежащая более новому поколению домена, чем откатываемый код (docs/migration-plan-v2.md §7). Сервис остаётся на новой (неисправной) версии — автоматический откат в этой ситуации может незаметно скрыть часть данных от старого интерфейса. Нужен ручной вход по SSH: journalctl -u wb-energy-meter -n 100, разбор причины провала, ручное решение по коду/БД."
    log_line "ОТКАТ ЗАБЛОКИРОВАН генерационной проверкой — сервис остаётся на новом (нерабочем) коде, статус завершён как 'failed' с пояснением, требуется ручное вмешательство"
    FINISHED=1
    return 0
  fi

  step_status "rolling_back" "Не удалось поднять сервис после обновления, откатываюсь…" \
    error "$reason"
  log_line "ОТКАТ: восстанавливаю $INSTALL_DIR/wb_energy_meter из $ROLLBACK_DIR"

  systemctl stop wb-energy-meter.service >>"$LOG_FILE" 2>&1 || true

  if [[ -d "$ROLLBACK_DIR/wb_energy_meter" ]]; then
    rm -rf "$INSTALL_DIR/wb_energy_meter" 2>>"$LOG_FILE" || true
    cp -a "$ROLLBACK_DIR/wb_energy_meter" "$INSTALL_DIR/wb_energy_meter" 2>>"$LOG_FILE" || true
    log_line "Код восстановлен из резервной копии"
  else
    log_line "ОШИБКА: резервная копия $ROLLBACK_DIR/wb_energy_meter отсутствует — откатывать нечем"
  fi
  if [[ -f "$ROLLBACK_DIR/VERSION.json" ]]; then
    cp -a "$ROLLBACK_DIR/VERSION.json" "$INSTALL_DIR/VERSION.json" 2>>"$LOG_FILE" || true
  fi
  if [[ -d "$ROLLBACK_DIR/scripts" ]]; then
    cp -a "$ROLLBACK_DIR/scripts/." "$INSTALL_DIR/scripts/" 2>>"$LOG_FILE" || true
  fi

  restore_data_backup

  systemctl daemon-reload >>"$LOG_FILE" 2>&1 || true
  systemctl start wb-energy-meter.service >>"$LOG_FILE" 2>&1 || true

  if wait_for_health; then
    local rolled_back_reason="Установка $EXPECTED_SHA не прошла проверку (код $rc); сервис возвращён к предыдущей версии"
    if [[ -n "$SELFCHECK_DETAILS" ]]; then
      rolled_back_reason="Установка $EXPECTED_SHA не прошла самопроверку интерфейса: ${SELFCHECK_DETAILS}. Сервис возвращён к предыдущей версии"
    fi
    finish_status "rolled_back" "Обновление не встало, откат на предыдущую версию выполнен успешно" \
      error "$rolled_back_reason"
    log_line "ОТКАТ УСПЕШЕН: сервис снова работает на предыдущей версии"
  else
    finish_status "failed" "Откат не помог — сервис не поднимается ни на новой, ни на старой версии" \
      error "После отката сервис всё ещё не отвечает на /health. Нужен вход по SSH: journalctl -u wb-energy-meter -n 100"
    log_line "ОТКАТ НЕ ПОМОГ: сервис не поднялся даже после восстановления предыдущей версии — нужен ручной вход по SSH"
  fi
  FINISHED=1
  return 0
}

# ---------------------------------------------------------------------
# Обработка ошибок и завершения — трап обязан ВСЕГДА оставить файл
# статуса в терминальном состоянии, иначе UI навсегда покажет "идёт
# обновление" (§4.1, последний абзац ТЗ).
# ---------------------------------------------------------------------

on_error() {
  local rc=$?
  # ВАЖНО: set +e здесь отключает errexit ГЛОБАЛЬНО (это опция шелла, не
  # локальная для функции) — если после него просто вернуться из трапа,
  # bash решит, что раз -e больше не активен, продолжать выполнение
  # main() дальше по коду как ни в чём не бывало (проверено на практике:
  # без явного exit ниже скрипт после "ошибки на этапе verify_archive"
  # преспокойно катится дальше в syntax_check/backup/install). Поэтому
  # на выходе из этой функции ВСЕГДА завершаем процесс явным exit —
  # именно это, а не сам факт срабатывания трапа, останавливает скрипт.
  set +e

  # Обновление уже завершилось успехом — что бы ни сломалось после,
  # успешный статус затирать нельзя. Иначе пользователь видит красный
  # отчёт о неудаче после удачного обновления (09.09.2026, код 127 из-за
  # самоперезаписи скрипта — см. комментарий у вызова main в конце файла).
  # Причину всё равно фиксируем в логе, но статус не трогаем.
  if [[ "$FINISHED" -eq 1 || "$STAGE" == "done" ]]; then
    log_line "ОШИБКА уже ПОСЛЕ успешного завершения (этап '$STAGE', код $rc) — статус обновления не меняю, обновление считается удачным"
    exit 0
  fi

  log_line "ОШИБКА на этапе '$STAGE' (код $rc)"
  case "$STAGE" in
    download|extract|verify_archive|syntax_check)
      finish_status "failed" "Обновление не удалось на этапе «$STAGE», рабочий каталог не тронут" \
        error "Ошибка на этапе $STAGE (код $rc), подробности: $LOG_FILE"
      FINISHED=1
      ;;
    backup)
      finish_status "failed" "Не удалось сделать резервную копию, рабочий каталог не тронут" \
        error "Ошибка резервного копирования (код $rc)"
      FINISHED=1
      ;;
    install|verify_install)
      attempt_rollback "$rc"
      ;;
    *)
      finish_status "failed" "Непредвиденная ошибка апдейтера (этап $STAGE)" \
        error "Код $rc на этапе $STAGE, см. $LOG_FILE"
      FINISHED=1
      ;;
  esac
  exit "$rc"
}

on_exit() {
  local rc=$?
  set +e
  if [[ "$FINISHED" -ne 1 && "$STAGE" != "done" && "$HAVE_LOCK" -eq 1 ]]; then
    # Последний рубеж: скрипт вышел (kill -9, неотловленная ошибка вне
    # -e, обрыв systemd-run) без того, чтобы on_error/finish_success
    # успели записать терминальный статус.
    log_line "on_exit: аварийное завершение на этапе '$STAGE' без терминального статуса, дописываю failed"
    step_status "failed" "Апдейтер завершился неожиданно (этап $STAGE)" \
      error "Скрипт прерван на этапе $STAGE (код выхода $rc)"
  fi
  [[ -n "$TMP_DIR" && -d "$TMP_DIR" ]] && rm -rf "$TMP_DIR"
  release_lock
  exit "$rc"
}

# ---------------------------------------------------------------------
# main — см. докстринг про самомодификацию наверху файла
# ---------------------------------------------------------------------

main() {
  mkdir -p "$LOG_DIR"
  rotate_log
  log_line "=== self-update: $(date -Is 2>/dev/null || date) ==="
  log_line "REPO=$REPO_OWNER/$REPO_NAME REF=$REF EXPECTED_SHA=$EXPECTED_SHA INSTALL_DIR=$INSTALL_DIR"

  trap on_error ERR
  trap on_exit EXIT

  acquire_lock
  TMP_DIR="$(mktemp -d /tmp/wb-energy-meter-selfupdate.XXXXXX)"

  STAGE="download"
  step_status "downloading" "Скачивание архива с GitHub…"
  download_archive

  STAGE="extract"
  step_status "downloading" "Распаковка архива…"
  extract_archive

  STAGE="verify_archive"
  step_status "downloading" "Проверка целостности архива…"
  verify_archive_contents

  STAGE="syntax_check"
  step_status "downloading" "Синтаксическая проверка нового кода…"
  syntax_check_new_code

  STAGE="backup"
  step_status "downloading" "Резервное копирование текущей версии…"
  backup_current
  backup_data

  STAGE="install"
  step_status "installing" "Установка новой версии…"
  install_new_code

  STAGE="verify_install"
  step_status "verifying" "Проверка сервиса после установки…"
  verify_after_install

  STAGE="done"
  finish_status "success" "Обновление успешно завершено" \
    to_version "$NEW_VERSION" to_commit "$EXPECTED_SHA" finished_at "$(date +%s)"
  log_line "УСПЕХ: обновлено до $NEW_VERSION ($EXPECTED_SHA)"
  rm -rf "$ROLLBACK_DIR"
  FINISHED=1
}

# Фигурные скобки здесь ОБЯЗАТЕЛЬНЫ, это не украшение.
#
# install.sh копирует scripts/ целиком, то есть перезаписывает ЭТОТ файл,
# пока он выполняется. Обёртка всего кода в main() спасает только тело
# функции: оно уже разобрано и лежит в памяти. Но ПОСЛЕ возврата из
# main() bash идёт читать файл дальше — с байтового смещения,
# запомненного до перезаписи. Файл к этому моменту другой длины
# (09.09.2026: 20087 -> 25277 байт при обновлении 0.11.0 -> 0.11.1),
# смещение попадает в середину чужой строки, bash выполняет обрывок и
# получает «command not found» — код 127.
#
# Выглядело это так: обновление полностью прошло, в лог легло
# «УСПЕХ: обновлено до 0.11.1», и сразу следом — «ОШИБКА на этапе 'done'
# (код 127)». Трап ERR затирал успешный статус аварийным, и в интерфейсе
# после удачного обновления висел красный отчёт о неудаче.
#
# Составная команда { ...; } разбирается целиком до начала выполнения,
# поэтому exit оказывается в памяти вместе с вызовом main, и к файлу
# после этого никто не обращается.
{ main "$@"; exit "$?"; }

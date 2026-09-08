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

LOCK_DIR="${LOCK_DIR:-/run/lock}"
LOCK_FILE="$LOCK_DIR/wb-energy-meter-update.lock"
LOG_DIR="${LOG_DIR:-/var/log/wb-energy-meter}"
LOG_FILE="$LOG_DIR/update.log"
ROLLBACK_DIR="${INSTALL_DIR}.rollback"

STAGE="init"
HAVE_LOCK=0
FINISHED=0
TMP_DIR=""
SRC_DIR=""
NEW_VERSION=""

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

verify_after_install() {
  if wait_for_health; then
    log_line "Проверка после установки прошла успешно (сервис активен, /health отвечает)"
    return 0
  fi
  log_line "Сервис не поднялся за 90 секунд после установки"
  return 1
}

attempt_rollback() {
  local rc="$1"
  step_status "rolling_back" "Не удалось поднять сервис после обновления, откатываюсь…" \
    error "Обновление не прошло проверку (код $rc), выполняется откат на предыдущую версию"
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

  systemctl daemon-reload >>"$LOG_FILE" 2>&1 || true
  systemctl start wb-energy-meter.service >>"$LOG_FILE" 2>&1 || true

  if wait_for_health; then
    finish_status "rolled_back" "Обновление не встало, откат на предыдущую версию выполнен успешно" \
      error "Установка $EXPECTED_SHA не прошла проверку (код $rc); сервис возвращён к предыдущей версии"
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

main "$@"

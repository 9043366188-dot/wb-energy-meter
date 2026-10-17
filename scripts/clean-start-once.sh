#!/bin/bash
# Разовый чистый старт (решения R9 от 11.10.2026 и R12 от 12.10.2026,
# docs/TZ-finish-plan.md §10).
#
# Всё, что заведено на контроллере до пилота, — тестовые данные. Пилот
# начинается с пустой базы. Кир выбрал сделать это через обычную кнопку
# «Обновить», а не командами по SSH, поэтому скрипт вызывается из
# scripts/install.sh — после остановки сервиса и резервной копии БД, до
# копирования кода.
#
# Метка у каждого чистого старта своя (MARK_NAME ниже): R9 — выпуск 0.22.1,
# R12 — выпуск 0.22.2 (тестовые данные успели накопиться заново, пилот
# стоял). Метка прошлого решения (.clean-start-r9-done) остаётся на диске и
# на новое решение НЕ влияет: контроллер, где R9 отключили записью 0, при
# обновлении до R12 всё равно отложит базу — отключить R12 нужно отдельно.
# Чтобы провести следующий чистый старт, меняется только MARK_NAME.
#
# Что делает. Ничего не удаляет: state.db, state.db-wal, state.db-shm и
# каталог plans переносятся в $DATA_DIR/test-data-<дата-время>/. Новая
# пустая база создаётся сервисом при первом старте (страж поколений
# отсутствующую БД пропускает).
#
# Разовость. После переноса пишется метка $DATA_DIR/.clean-start-r12-done.
# Первая строка — момент чистого старта (секунды Unix). Дальше:
#   - базу, созданную ПОСЛЕ этого момента, скрипт не трогает никогда;
#   - базу, созданную РАНЬШЕ (её вернул откат неудачного обновления —
#     откат делает self-update.sh старой версии, о метке он не знает),
#     скрипт снова откладывает. Дата создания базы — applied_at первой
#     миграции в schema_migrations.
# Запас MARGIN_SEC (сутки) защищает свежую базу от перевода часов назад.
#
# Как отключить (например, на другом контроллере, где данные настоящие):
# до обновления записать 0 в метку —
#   echo 0 | sudo tee /mnt/data/var/lib/wb-energy-meter/.clean-start-r12-done
# База не бывает создана раньше 0, поэтому скрипт ничего не тронет.
#
# Коды выхода: 0 — сделано или делать нечего; 1 — перенос не удался,
# уже перенесённое возвращено на место; 2 — неверный вызов.

set -uo pipefail

DATA_DIR="${1:-}"
if [[ -z "$DATA_DIR" ]]; then
  echo "Использование: clean-start-once.sh DATA_DIR" >&2
  exit 2
fi

# Имя метки этого чистого старта (см. шапку). R9 был .clean-start-r9-done.
MARK_NAME=".clean-start-r12-done"
MARK="$DATA_DIR/$MARK_NAME"
MARGIN_SEC="${CLEAN_START_MARGIN_SEC:-86400}"
ITEMS=(state.db state.db-wal state.db-shm plans)

DEST=""
MOVED=()

say() { echo "    $*"; }

# Дата создания базы: MIN(applied_at) из schema_migrations. Печатает
# число или ничего. immutable=1 — только чтение, без блокировок и без
# служебных файлов рядом с базой (сервис остановлен, запись чужая).
db_birth() {
  python3 - "$1" 2>/dev/null <<'PY'
import sqlite3, sys, urllib.parse
path = sys.argv[1]
try:
    conn = sqlite3.connect(
        "file:%s?immutable=1" % urllib.parse.quote(path), uri=True)
    try:
        row = conn.execute(
            "SELECT MIN(applied_at) FROM schema_migrations").fetchone()
    finally:
        conn.close()
    if row and row[0] is not None:
        print(int(row[0]))
except (sqlite3.Error, ValueError, TypeError):
    pass
PY
}

undo_move() {
  local m
  for m in ${MOVED[@]+"${MOVED[@]}"}; do
    if ! mv "$DEST/$m" "$DATA_DIR/$m"; then
      echo "ОШИБКА: не удалось вернуть $m из $DEST — верните вручную" >&2
    fi
  done
  MOVED=()
  rmdir "$DEST" 2>/dev/null || true
}

# Переносит то, что есть из ITEMS, в новый каталог test-data-*. При
# сбое возвращает уже перенесённое и отдаёт 1.
move_aside() {
  local reason="$1" it present=()
  for it in "${ITEMS[@]}"; do
    [[ -e "$DATA_DIR/$it" ]] && present+=("$it")
  done
  if [[ ${#present[@]} -eq 0 ]]; then
    say "Откладывать нечего: базы и планов в $DATA_DIR нет"
    return 0
  fi

  local base n=1
  base="$DATA_DIR/test-data-$(date +%Y%m%d-%H%M%S)"
  DEST="$base"
  while [[ -e "$DEST" ]]; do
    DEST="$base-$n"
    n=$((n + 1))
  done
  if ! mkdir "$DEST"; then
    echo "ОШИБКА: не удалось создать $DEST" >&2
    return 1
  fi

  for it in "${present[@]}"; do
    if mv "$DATA_DIR/$it" "$DEST/$it"; then
      MOVED+=("$it")
    else
      echo "ОШИБКА: не удалось перенести $it в $DEST — возвращаю уже перенесённое" >&2
      undo_move
      return 1
    fi
  done

  cat >"$DEST/README.txt" 2>/dev/null <<EOF || true
Тестовые данные, отложенные при обновлении $(date '+%Y-%m-%d %H:%M:%S').
Причина: $reason.
Решения R9 (11.10.2026) и R12 (12.10.2026): всё, заведённое до пилота, —
тестовое.

Как вернуть эту базу:
1. sudo systemctl stop wb-energy-meter
2. Текущие state.db, state.db-wal, state.db-shm и plans из
   $DATA_DIR убрать в другой каталог.
3. Файлы из этого каталога переложить обратно в $DATA_DIR.
4. echo 0 | sudo tee $MARK
   (иначе следующее обновление снова отложит эту базу).
5. sudo systemctl start wb-energy-meter
EOF
  say "Отложено в $DEST: ${MOVED[*]}"
  say "Причина: $reason"
  return 0
}

if ! mkdir -p "$DATA_DIR"; then
  echo "ОШИБКА: не удалось создать $DATA_DIR" >&2
  exit 1
fi

if [[ ! -e "$MARK" ]]; then
  now="$(date +%s)"
  if ! move_aside "чистый старт R12 (выпуск 0.22.2)"; then
    exit 1
  fi
  if ! printf '%s\n# Чистый старт R12 выполнен %s. Базы, созданные раньше, обновление откладывает в test-data-*.\n# Записать сюда 0 — отключить.\n' \
      "$now" "$(date '+%Y-%m-%d %H:%M:%S')" >"$MARK.tmp" \
      || ! mv -f "$MARK.tmp" "$MARK"; then
    echo "ОШИБКА: не удалось записать метку $MARK — возвращаю данные на место" >&2
    rm -f "$MARK.tmp" 2>/dev/null || true
    undo_move
    exit 1
  fi
  say "Метка чистого старта записана: $MARK"
  exit 0
fi

mark_ts=""
IFS= read -r mark_ts <"$MARK" 2>/dev/null || true
mark_ts="${mark_ts//[[:space:]]/}"
if [[ ! "$mark_ts" =~ ^[0-9]+$ ]]; then
  say "Метка $MARK нечитаема (первая строка не число) — ничего не трогаю"
  exit 0
fi

if [[ ! -e "$DATA_DIR/state.db" ]]; then
  say "Чистый старт уже выполнен, базы ещё нет — ничего не трогаю"
  exit 0
fi

birth="$(db_birth "$DATA_DIR/state.db")"
if [[ ! "$birth" =~ ^[0-9]+$ ]]; then
  say "Чистый старт уже выполнен; дату создания базы прочитать не удалось — базу не трогаю"
  exit 0
fi

if (( birth < mark_ts - MARGIN_SEC )); then
  if ! move_aside "база создана раньше чистого старта — скорее всего, её вернул откат обновления"; then
    exit 1
  fi
  exit 0
fi

say "Чистый старт уже выполнен, база создана после него — не трогаю"
exit 0

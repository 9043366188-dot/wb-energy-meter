#!/bin/bash
# install-from-github.sh
# Установщик wb-energy-meter, который качает код с GitHub.
#
# Использование (на WB):
#   curl -fsSL https://raw.githubusercontent.com/9043366188-dot/wb-energy-meter/main/scripts/install-from-github.sh | sudo bash
#
# Или с конкретного тега/ветки:
#   curl -fsSL https://raw.githubusercontent.com/9043366188-dot/wb-energy-meter/main/scripts/install-from-github.sh | sudo REF=v0.4.0 bash

set -euo pipefail

# -----------------------------------------------------------------------------
# Параметры (можно переопределить через переменные окружения)
# -----------------------------------------------------------------------------

REPO_OWNER="${REPO_OWNER:-9043366188-dot}"
REPO_NAME="${REPO_NAME:-wb-energy-meter}"
REF="${REF:-main}"                     # ветка или тег
TMP_DIR="${TMP_DIR:-/tmp/wb-energy-meter-install-$$}"

# -----------------------------------------------------------------------------
# Проверки
# -----------------------------------------------------------------------------

if [[ $EUID -ne 0 ]]; then
  echo "ОШИБКА: нужен root. Запустите через sudo." >&2
  exit 1
fi

echo "=============================================================="
echo "wb-energy-meter — установка из GitHub"
echo "  Репозиторий: ${REPO_OWNER}/${REPO_NAME}"
echo "  Ветка/тег:   ${REF}"
echo "=============================================================="
echo

for cmd in curl tar python3; do
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "ОШИБКА: не найден $cmd. Установите его и повторите." >&2
    exit 1
  fi
done

# -----------------------------------------------------------------------------
# Скачивание архива
# -----------------------------------------------------------------------------

URL="https://github.com/${REPO_OWNER}/${REPO_NAME}/archive/refs/heads/${REF}.tar.gz"
# Если REF выглядит как тег vX.Y.Z — попробуем сначала tag-URL
if [[ "$REF" == v* ]]; then
  URL_TAG="https://github.com/${REPO_OWNER}/${REPO_NAME}/archive/refs/tags/${REF}.tar.gz"
  URL="$URL_TAG"
fi

ARCHIVE="${TMP_DIR}/source.tar.gz"

echo ">>> Скачивание ${URL}..."
mkdir -p "$TMP_DIR"
if ! curl -fsSL --max-time 60 -o "$ARCHIVE" "$URL"; then
  echo "ОШИБКА: не удалось скачать ${URL}" >&2
  echo "Проверьте имя ветки/тега и доступ в интернет." >&2
  rm -rf "$TMP_DIR"
  exit 2
fi
echo "    OK ($(du -h "$ARCHIVE" | cut -f1))"

# -----------------------------------------------------------------------------
# Распаковка
# -----------------------------------------------------------------------------

echo ">>> Распаковка..."
tar -xzf "$ARCHIVE" -C "$TMP_DIR"

# GitHub распаковывает в папку <repo>-<ref-без-v>
# Найдём её, не привязываясь к точному имени.
SRC_DIR="$(find "$TMP_DIR" -maxdepth 1 -mindepth 1 -type d ! -name "lost+found" | head -1)"
if [[ -z "$SRC_DIR" || ! -d "$SRC_DIR" ]]; then
  echo "ОШИБКА: не нашёл папку с исходниками в архиве" >&2
  rm -rf "$TMP_DIR"
  exit 3
fi
echo "    OK: $SRC_DIR"

# Проверим, что внутри есть install.sh
if [[ ! -f "$SRC_DIR/scripts/install.sh" ]]; then
  echo "ОШИБКА: $SRC_DIR/scripts/install.sh не найден" >&2
  echo "Содержимое архива:" >&2
  ls -la "$SRC_DIR" >&2
  rm -rf "$TMP_DIR"
  exit 4
fi

# -----------------------------------------------------------------------------
# Запуск install.sh
# -----------------------------------------------------------------------------

echo
echo ">>> Запуск scripts/install.sh..."
echo "=============================================================="
cd "$SRC_DIR"
bash scripts/install.sh
INSTALL_RC=$?

# -----------------------------------------------------------------------------
# Чистка
# -----------------------------------------------------------------------------

cd /
rm -rf "$TMP_DIR"

echo
echo "=============================================================="
if [[ $INSTALL_RC -eq 0 ]]; then
  echo "[OK] Установка из GitHub завершена."
else
  echo "[!] install.sh завершился с кодом $INSTALL_RC"
  exit $INSTALL_RC
fi

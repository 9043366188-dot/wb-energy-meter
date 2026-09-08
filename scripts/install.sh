#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

CONFIG_PATH="/etc/wb-energy-meter.conf"
LOG_DIR="/var/log/wb-energy-meter"
DATA_DIR="/mnt/data/var/lib/wb-energy-meter"
DB_PATH="$DATA_DIR/state.db"
INSTALL_DIR="/opt/wb-energy-meter"
SERVICE_FILE="/etc/systemd/system/wb-energy-meter.service"
LAUNCHER="/usr/bin/wb-energy-meter"
CLI_LAUNCHER="/usr/bin/wb-energy-meter-cli"

if [[ $EUID -ne 0 ]]; then
  echo "ОШИБКА: нужен root. Запустите: sudo bash scripts/install.sh" >&2
  exit 1
fi

echo ">>> Проверка Python..."
PY_VER="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
echo "    python3 $PY_VER"

# Зависимости. Главный принцип: apt нужен ТОЛЬКО если пакетов реально
# нет. Раньше `apt-get update` вызывался безусловно, а из-за
# `set -euo pipefail` любая его ошибка убивала установку — при том что
# зависимости уже стояли и apt был не нужен вовсе.
#
# Реальный случай (09.09.2026): у зеркала Wiren Board протух
# Release-файл ("Release file ... is expired"), apt вернул 100, и
# обновление рабочего контроллера сорвалось на пустом месте.
# Протухший индекс на зеркале — проблема на стороне сервера, она не
# должна мешать обновить наш код.
#
# SKIP_APT=1 (self-update.sh, ТЗ v0.9.0) остаётся: он просто запрещает
# трогать apt даже когда пакетов нет.
DEPS_PY="import paho.mqtt, yaml, flask"
APT_PACKAGES=(python3-paho-mqtt python3-yaml python3-flask)

install_deps_via_apt() {
  echo ">>> Установка системных зависимостей через apt-get..."
  # Check-Valid-Until=false — обход именно протухшего Release-файла.
  # `|| true`: неудачный update не фатален, вдруг пакеты и так в кэше.
  if ! apt-get update -o Acquire::Check-Valid-Until=false; then
    echo "    [!] apt-get update завершился с ошибкой (недоступное или"
    echo "        протухшее зеркало). Пробую поставить пакеты из кэша."
  fi
  # Тоже не фатально: решение принимает финальная проверка импортом
  # ниже — она заодно печатает человеку, что делать дальше. Без этого
  # `set -e` убил бы скрипт прямо здесь, с голым кодом 100 и без
  # единого намёка на причину.
  if ! apt-get install -y --no-install-recommends "${APT_PACKAGES[@]}"; then
    echo "    [!] apt-get install не смог поставить пакеты."
  fi
}

echo ">>> Проверка зависимостей..."
if python3 -c "$DEPS_PY" 2>/dev/null; then
  echo "    OK: paho-mqtt, yaml, flask уже установлены — apt не нужен"
elif [[ "${SKIP_APT:-0}" == "1" ]]; then
  echo "    [!] SKIP_APT=1, но зависимостей нет — ставлю всё равно," \
       "иначе сервис не стартует"
  install_deps_via_apt
else
  install_deps_via_apt
fi

# Финальная проверка: без зависимостей сервис не поднимется, поэтому
# здесь останавливаемся ДО того, как что-либо трогать на контроллере.
if ! python3 -c "$DEPS_PY" 2>/dev/null; then
  echo "ОШИБКА: не удалось обеспечить зависимости python3-paho-mqtt," >&2
  echo "        python3-yaml, python3-flask." >&2
  echo "        Ничего не изменено, работающий сервис не тронут." >&2
  echo "        Установите их вручную и запустите установку снова:" >&2
  echo "          apt-get update -o Acquire::Check-Valid-Until=false" >&2
  echo "          apt-get install -y ${APT_PACKAGES[*]}" >&2
  exit 5
fi

if [[ -f "$DB_PATH" ]]; then
  BACKUP="$DB_PATH.backup-$(date +%Y%m%d-%H%M%S)"
  echo ">>> Резервная копия БД: $BACKUP"
  cp -a "$DB_PATH" "$BACKUP"
  ls -1t "$DB_PATH".backup-* 2>/dev/null | tail -n +6 | xargs -r rm -f
fi

if systemctl is-active --quiet wb-energy-meter.service 2>/dev/null; then
  echo ">>> Останавливаю текущий сервис..."
  systemctl stop wb-energy-meter.service
fi

echo ">>> Создание директорий..."
mkdir -p "$INSTALL_DIR" "$LOG_DIR" "$DATA_DIR"

echo ">>> Копирование кода..."
rm -rf "$INSTALL_DIR/wb_energy_meter"
cp -r "$PROJECT_ROOT/wb_energy_meter" "$INSTALL_DIR/"
find "$INSTALL_DIR" -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true

if [[ ! -f "$INSTALL_DIR/wb_energy_meter/migrations/001_initial_schema.sql" ]]; then
  echo "ОШИБКА: миграции не скопировались" >&2; exit 2
fi

if [[ ! -f "$INSTALL_DIR/wb_energy_meter/static/index.html" ]]; then
  echo "[!] static/index.html не найден — веб-интерфейс будет недоступен" >&2
  echo "    (сервис и API продолжат работать)" >&2
fi

echo ">>> Копирование scripts/ (нужно для самообновления, ТЗ v0.9.0)..."
mkdir -p "$INSTALL_DIR/scripts"
cp -f "$PROJECT_ROOT/scripts/"*.sh "$INSTALL_DIR/scripts/" 2>/dev/null || true
if [[ -f "$PROJECT_ROOT/scripts/wb-energy-meter.conf.example" ]]; then
  cp -f "$PROJECT_ROOT/scripts/wb-energy-meter.conf.example" "$INSTALL_DIR/scripts/" 2>/dev/null || true
fi
chmod 0755 "$INSTALL_DIR/scripts/"*.sh 2>/dev/null || true

echo ">>> Запись VERSION.json..."
NEW_APP_VERSION="$(grep -m1 '__version__' "$INSTALL_DIR/wb_energy_meter/__init__.py" \
    | sed -E "s/.*__version__[[:space:]]*=[[:space:]]*[\"']([^\"']+)[\"'].*/\1/")"
[[ -z "$NEW_APP_VERSION" ]] && NEW_APP_VERSION="unknown"
# SOURCE_SHA/SOURCE_REF передаёт scripts/self-update.sh при самообновлении;
# при обычной ручной установке их нет — commit пишем как null (§4.2 ТЗ).
if [[ -n "${SOURCE_SHA:-}" ]]; then
  COMMIT_JSON="\"$SOURCE_SHA\""
else
  COMMIT_JSON="null"
fi
cat > "$INSTALL_DIR/VERSION.json" <<EOF
{"version": "$NEW_APP_VERSION", "commit": $COMMIT_JSON, "ref": "${SOURCE_REF:-main}", "installed_at": $(date +%s)}
EOF

echo ">>> Установка launcher'ов..."
cat > "$LAUNCHER" <<EOF
#!/bin/bash
export PYTHONPATH="$INSTALL_DIR\${PYTHONPATH:+:\$PYTHONPATH}"
exec /usr/bin/python3 -m wb_energy_meter.main "\$@"
EOF
chmod 0755 "$LAUNCHER"
cat > "$CLI_LAUNCHER" <<EOF
#!/bin/bash
export PYTHONPATH="$INSTALL_DIR\${PYTHONPATH:+:\$PYTHONPATH}"
exec /usr/bin/python3 -m wb_energy_meter.cli "\$@"
EOF
chmod 0755 "$CLI_LAUNCHER"

echo ">>> systemd-юнит..."
cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=Wiren Board energy meter service (wb-energy-meter)
After=network-online.target mosquitto.service
Wants=network-online.target

[Service]
Type=simple
User=root
Group=root
Environment=PYTHONPATH=$INSTALL_DIR
ExecStart=/usr/bin/python3 -m wb_energy_meter.main --config $CONFIG_PATH --db-path $DB_PATH
Restart=on-failure
RestartSec=5
StartLimitInterval=60
StartLimitBurst=5
MemoryMax=256M
KillMode=mixed
KillSignal=SIGTERM
TimeoutStopSec=15

[Install]
WantedBy=multi-user.target
EOF

if [[ -f "$CONFIG_PATH" ]]; then
  echo ">>> Конфиг уже есть, не трогаем"
else
  echo ">>> Установка конфига по умолчанию..."
  cp "$PROJECT_ROOT/scripts/wb-energy-meter.conf.example" "$CONFIG_PATH"
  chmod 0644 "$CONFIG_PATH"
fi

systemctl daemon-reload
systemctl enable wb-energy-meter.service
systemctl restart wb-energy-meter.service
sleep 2

echo
echo "=============================================================="
echo " Установка/обновление завершено."
echo "=============================================================="
"$LAUNCHER" --version 2>/dev/null || true
echo
echo "Проверка:    systemctl status wb-energy-meter"
echo "Логи:        journalctl -u wb-energy-meter -f"
echo "API:         curl -s http://127.0.0.1:8080/api/status | python3 -m json.tool"
echo "API docs:    http://<IP>:8080/api/docs"
echo
echo "Полезные команды:"
echo "  wb-energy-meter-cli meter list"
echo "  wb-energy-meter-cli aggregates status"
echo "  wb-energy-meter-cli consumption wb-map3e_16 --period last_24h"
echo

if systemctl is-active --quiet wb-energy-meter.service; then
  echo "[OK] Сервис запущен"
else
  echo "[!]  Сервис не активен:"
  echo "     journalctl -u wb-energy-meter -n 50"
fi

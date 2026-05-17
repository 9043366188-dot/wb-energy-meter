#!/bin/bash
set -euo pipefail
if [[ $EUID -ne 0 ]]; then
  echo "ОШИБКА: нужен root" >&2; exit 1
fi
systemctl stop wb-energy-meter.service 2>/dev/null || true
systemctl disable wb-energy-meter.service 2>/dev/null || true
rm -f /etc/systemd/system/wb-energy-meter.service
rm -f /usr/bin/wb-energy-meter
rm -f /usr/bin/wb-energy-meter-cli
rm -rf /opt/wb-energy-meter
systemctl daemon-reload
echo "Удалено."
echo "Не удалено (удалите вручную, если нужно):"
echo "  /etc/wb-energy-meter.conf"
echo "  /var/log/wb-energy-meter/"
echo "  /mnt/data/var/lib/wb-energy-meter/"

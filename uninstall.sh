#!/bin/bash

set -euo pipefail

DAEMON_TARGET=/usr/local/sbin/zimacube-fan
SERVICE_TARGET=/etc/systemd/system/zimacube-fan.service
SYSFAN_TARGET=/usr/local/sbin/zimacube-sysfan
SYSFAN_SERVICE_TARGET=/etc/systemd/system/zimacube-sysfan.service
MODULES_TARGET=/etc/modules-load.d/i2c-dev.conf

if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
    echo "error: run this uninstaller as root: sudo ./uninstall.sh" >&2
    exit 1
fi

echo "Removing ZimaCube fan daemons..."

# Stopping zimacube-sysfan returns the system fan to EC auto mode on its own.
for service in zimacube-fan.service zimacube-sysfan.service; do
    systemctl disable --now "$service" 2>/dev/null || true
done

rm -f "$SERVICE_TARGET" "$SYSFAN_SERVICE_TARGET" "$DAEMON_TARGET" "$SYSFAN_TARGET"
rm -rf /etc/systemd/system/zimacube-fan.service.d /etc/systemd/system/zimacube-sysfan.service.d
systemctl daemon-reload

# i2c-dev is left loaded on purpose: other software may rely on it. Only the
# autoload file this installer wrote is removed.
rm -f "$MODULES_TARGET"

echo
echo "ZimaCube fan daemons removed."
echo "The disk-cage fan keeps the last speed the daemon set until the next"
echo "power cycle; the system fan is back under the EC's own curve."

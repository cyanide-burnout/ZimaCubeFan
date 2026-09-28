#!/bin/bash

set -euo pipefail

DAEMON_TARGET=/usr/local/sbin/zimacube-fan
SERVICE_TARGET=/etc/systemd/system/zimacube-fan.service
SYSFAN_TARGET=/usr/local/sbin/zimacube-sysfan
SYSFAN_SERVICE_TARGET=/etc/systemd/system/zimacube-sysfan.service

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

echo
echo "ZimaCube fan daemons removed."
echo "The bay driver will return the disk-cage fan to its fallback duty;"
echo "the system fan is back under the EC's own curve."

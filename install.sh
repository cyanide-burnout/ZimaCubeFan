#!/bin/bash

set -euo pipefail

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
DAEMON_SOURCE="$SCRIPT_DIR/zimacube_fan_daemon.py"
SERVICE_SOURCE="$SCRIPT_DIR/zimacube-fan.service"
DAEMON_TARGET=/usr/local/sbin/zimacube-fan
SERVICE_TARGET=/etc/systemd/system/zimacube-fan.service
MODULES_TARGET=/etc/modules-load.d/i2c-dev.conf

if [[ $(uname -s) != Linux ]]; then
    echo "error: this installer must run on Linux" >&2
    exit 1
fi

if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
    echo "error: run this installer as root: sudo ./install.sh" >&2
    exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
    echo "error: python3 is required; install it and run this script again" >&2
    exit 1
fi

if [[ ! -f $DAEMON_SOURCE || ! -f $SERVICE_SOURCE ]]; then
    echo "error: zimacube_fan_daemon.py and zimacube-fan.service must be next to install.sh" >&2
    exit 1
fi

echo "Installing ZimaCube fan daemon..."
install -m 0755 "$DAEMON_SOURCE" "$DAEMON_TARGET"
install -m 0644 "$SERVICE_SOURCE" "$SERVICE_TARGET"
install -d -m 0755 /etc/modules-load.d
printf '%s\n' i2c-dev >"$MODULES_TARGET"

modprobe i2c-dev
systemctl daemon-reload
systemctl enable zimacube-fan.service
systemctl restart zimacube-fan.service

echo
echo "ZimaCube fan daemon installed and started."
systemctl --no-pager --full status zimacube-fan.service

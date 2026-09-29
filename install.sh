#!/bin/bash

set -euo pipefail

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
DAEMON_SOURCE="$SCRIPT_DIR/zimacube_fan_daemon.py"
SERVICE_SOURCE="$SCRIPT_DIR/zimacube-fan.service"
SYSFAN_SOURCE="$SCRIPT_DIR/zimacube_sysfan_daemon.py"
SYSFAN_SERVICE_SOURCE="$SCRIPT_DIR/zimacube-sysfan.service"
DAEMON_TARGET=/usr/local/sbin/zimacube-fan
SERVICE_TARGET=/etc/systemd/system/zimacube-fan.service
SYSFAN_TARGET=/usr/local/sbin/zimacube-sysfan
SYSFAN_SERVICE_TARGET=/etc/systemd/system/zimacube-sysfan.service

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

if [[ -d /sys/module/zimacube_bay_fan ]]; then
    echo "error: unload the legacy zimacube_bay_fan module before installing the renamed driver" >&2
    exit 1
fi

if ! modinfo zimacube_bay >/dev/null 2>&1; then
    echo "error: install the zimacube_bay kernel module before updating the fan daemon" >&2
    echo "see https://github.com/cyanide-burnout/zimacube-bay" >&2
    exit 1
fi

if [[ -r /sys/module/zimacube_bay/parameters/enable_fan_control ]] &&
   [[ $(cat /sys/module/zimacube_bay/parameters/enable_fan_control) != Y ]]; then
    echo "error: the loaded bay fan driver is read-only; reload it with fan control enabled" >&2
    exit 1
fi

for source in "$DAEMON_SOURCE" "$SERVICE_SOURCE" "$SYSFAN_SOURCE" "$SYSFAN_SERVICE_SOURCE"; do
    if [[ ! -f $source ]]; then
        echo "error: ${source##*/} must be next to install.sh" >&2
        exit 1
    fi
done

echo "Installing ZimaCube fan daemons..."
install -m 0755 "$DAEMON_SOURCE" "$DAEMON_TARGET"
install -m 0644 "$SERVICE_SOURCE" "$SERVICE_TARGET"
install -m 0755 "$SYSFAN_SOURCE" "$SYSFAN_TARGET"
install -m 0644 "$SYSFAN_SERVICE_SOURCE" "$SYSFAN_SERVICE_TARGET"
systemctl daemon-reload
systemctl enable zimacube-fan.service
systemctl restart zimacube-fan.service

echo
echo "ZimaCube disk-cage fan daemon installed and started."
systemctl --no-pager --full status zimacube-fan.service

# The system fan daemon needs the separate zimacube_ec kernel driver, so it
# is only started where that driver is actually loaded.
if grep -qx zimacube_ec /sys/class/hwmon/hwmon*/name 2>/dev/null; then
    systemctl enable zimacube-sysfan.service
    systemctl restart zimacube-sysfan.service
    echo
    echo "ZimaCube system fan daemon installed and started."
    systemctl --no-pager --full status zimacube-sysfan.service
else
    echo
    echo "ZimaCube system fan daemon installed but left disabled: the"
    echo "zimacube_ec hwmon device is not present. Install the kernel"
    echo "driver from https://github.com/cyanide-burnout/zimacube-ec and"
    echo "then run: sudo systemctl enable --now zimacube-sysfan.service"
fi

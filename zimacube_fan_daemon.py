#!/usr/bin/env python3
"""Control the ZimaCube 2 disk-cage fan directly through Linux ioctls."""

from __future__ import annotations

import argparse
import ctypes
import fcntl
import glob
import logging
import os
import signal
import time
from collections.abc import Callable, Sequence


LOG = logging.getLogger("zimacube-fan")

# Linux UAPI constants from linux/hdreg.h and linux/i2c-dev.h.
HDIO_DRIVE_CMD = 0x031F
ATA_CHECK_POWER_MODE = 0xE5
ATA_CHECK_POWER_MODE_OLD = 0x98
ATA_ACTIVE_OR_IDLE = 0xFF

I2C_SLAVE = 0x0703
I2C_SLAVE_FORCE = 0x0706
I2C_SMBUS = 0x0720
I2C_SMBUS_WRITE = 0
I2C_SMBUS_QUICK = 0
I2C_SMBUS_I2C_BLOCK_DATA = 8
I2C_SMBUS_BLOCK_MAX = 32

FAN_ADDRESS = 0x69
FAN_COMMAND = 0x04
DEFAULT_BUSES = tuple(range(5))


class I2CSmbusData(ctypes.Union):
    _fields_ = [
        ("byte", ctypes.c_uint8),
        ("word", ctypes.c_uint16),
        ("block", ctypes.c_uint8 * (I2C_SMBUS_BLOCK_MAX + 2)),
    ]


class I2CSmbusIoctlData(ctypes.Structure):
    _fields_ = [
        ("read_write", ctypes.c_uint8),
        ("command", ctypes.c_uint8),
        ("size", ctypes.c_uint32),
        ("data", ctypes.POINTER(I2CSmbusData)),
    ]


LIBC = ctypes.CDLL(None, use_errno=True)


def _libc_ioctl(fd: int, request: int, argument: object) -> None:
    if LIBC.ioctl(fd, request, argument) < 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def _select_i2c_address(fd: int, address: int, force: bool) -> None:
    request = I2C_SLAVE_FORCE if force else I2C_SLAVE
    _libc_ioctl(fd, request, ctypes.c_ulong(address))


def _smbus_transfer(
    fd: int,
    command: int,
    size: int,
    data: I2CSmbusData | None = None,
) -> None:
    data_pointer = ctypes.pointer(data) if data is not None else ctypes.POINTER(I2CSmbusData)()
    arguments = I2CSmbusIoctlData(I2C_SMBUS_WRITE, command, size, data_pointer)
    _libc_ioctl(fd, I2C_SMBUS, ctypes.byref(arguments))


def probe_i2c_address(bus: int, address: int = FAN_ADDRESS) -> bool:
    """Probe one address using the SMBus quick-write used by i2cdetect here."""
    path = f"/dev/i2c-{bus}"
    try:
        fd = os.open(path, os.O_RDWR | os.O_CLOEXEC)
    except OSError:
        return False
    try:
        _select_i2c_address(fd, address, force=False)
        _smbus_transfer(fd, command=0, size=I2C_SMBUS_QUICK)
        return True
    except OSError:
        return False
    finally:
        os.close(fd)


def find_i2c_bus(
    buses: Sequence[int] = DEFAULT_BUSES,
    probe: Callable[[int, int], bool] = probe_i2c_address,
) -> int:
    for bus in buses:
        if probe(bus, FAN_ADDRESS):
            return bus
    raise RuntimeError("fan controller 0x69 not found on I2C buses " + ", ".join(map(str, buses)))


def write_i2c_block(bus: int, address: int, command: int, values: bytes) -> None:
    if not 1 <= len(values) <= I2C_SMBUS_BLOCK_MAX:
        raise ValueError("I2C block must contain between 1 and 32 bytes")

    fd = os.open(f"/dev/i2c-{bus}", os.O_RDWR | os.O_CLOEXEC)
    try:
        _select_i2c_address(fd, address, force=True)
        data = I2CSmbusData()
        data.block[0] = len(values)
        for index, value in enumerate(values, start=1):
            data.block[index] = value
        _smbus_transfer(fd, command, I2C_SMBUS_I2C_BLOCK_DATA, data)
    finally:
        os.close(fd)


def query_ata_power_mode(
    device: str,
    opener: Callable[[str, int], int] = os.open,
    ioctl_fn: Callable[..., object] = fcntl.ioctl,
    closer: Callable[[int], None] = os.close,
) -> int:
    """Return the ATA sector-count value without waking the disk."""
    fd = opener(device, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        last_error: OSError | None = None
        for command in (ATA_CHECK_POWER_MODE, ATA_CHECK_POWER_MODE_OLD):
            arguments = bytearray((command, 0, 0, 0))
            try:
                ioctl_fn(fd, HDIO_DRIVE_CMD, arguments, True)
                return arguments[2]
            except OSError as error:
                last_error = error
        assert last_error is not None
        raise last_error
    finally:
        closer(fd)


def drive_state(
    device: str,
    query: Callable[[str], int] = query_ata_power_mode,
) -> str:
    try:
        power_mode = query(device)
    except OSError as error:
        LOG.warning("cannot read power state of %s: %s", device, error)
        return "unknown"
    return "active/idle" if power_mode == ATA_ACTIVE_OR_IDLE else "standby"


def any_drive_active(
    devices: Sequence[str],
    query: Callable[[str], int] = query_ata_power_mode,
) -> bool:
    for device in devices:
        state = drive_state(device, query)
        LOG.debug("%s: %s", device, state)
        if state == "active/idle":
            return True
    return False


def set_fan_speed(
    bus: int,
    speed: int,
    writer: Callable[[int, int, int, bytes], None] = write_i2c_block,
) -> None:
    if not 0 <= speed <= 100:
        raise ValueError("fan speed must be between 0 and 100")

    # Same I2C block transaction as:
    # i2cset -f -y BUS 0x69 0x04 0x01 SPEED 0 0 0 0 1 0 i
    writer(bus, FAN_ADDRESS, FAN_COMMAND, bytes((0x01, speed, 0, 0, 0, 0, 1, 0)))


class FanDaemon:
    def __init__(
        self,
        bus: int,
        interval: float,
        active_speed: int,
        idle_speed: int,
        cooldown: float,
        device_pattern: str,
        dry_run: bool = False,
        power_query: Callable[[str], int] = query_ata_power_mode,
        fan_writer: Callable[[int, int, int, bytes], None] = write_i2c_block,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.bus = bus
        self.interval = interval
        self.active_speed = active_speed
        self.idle_speed = idle_speed
        self.cooldown = cooldown
        self.device_pattern = device_pattern
        self.dry_run = dry_run
        self.power_query = power_query
        self.fan_writer = fan_writer
        self.clock = clock
        self.running = True
        self.last_speed: int | None = None
        self.last_active_at: float | None = None

    def stop(self, _signum: int, _frame: object) -> None:
        self.running = False

    def update(self) -> int:
        devices = sorted(glob.glob(self.device_pattern))
        active = any_drive_active(devices, self.power_query)
        now = self.clock()

        if active:
            self.last_active_at = now
            speed = self.active_speed
            reason = "at least one disk is active"
        elif self.last_active_at is not None and now - self.last_active_at < self.cooldown:
            remaining = self.cooldown - (now - self.last_active_at)
            speed = self.active_speed
            reason = f"cooling down; {remaining:.0f}s remaining"
        else:
            speed = self.idle_speed
            reason = "no active disks"

        if speed != self.last_speed:
            LOG.info("setting fan to %d%% (%s; checked %d disks)", speed, reason, len(devices))
            if not self.dry_run:
                set_fan_speed(self.bus, speed, self.fan_writer)
            self.last_speed = speed
        return speed

    def run(self, once: bool = False) -> None:
        while self.running:
            try:
                self.update()
            except Exception:
                LOG.exception("fan update failed")
            if once:
                return
            end = time.monotonic() + self.interval
            while self.running and time.monotonic() < end:
                time.sleep(min(0.5, end - time.monotonic()))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interval", type=float, default=30, help="poll interval in seconds (default: 30)")
    parser.add_argument("--active-speed", type=int, default=80, help="speed when a disk is active (default: 80)")
    parser.add_argument("--idle-speed", type=int, default=40, help="speed when no disk is active (default: 40)")
    parser.add_argument("--cooldown", type=float, default=120, help="delay before idle speed in seconds (default: 120)")
    parser.add_argument("--devices", default="/dev/sd?", help="disk glob (default: /dev/sd?)")
    parser.add_argument("--bus", type=int, help="I2C bus; auto-detected by default")
    parser.add_argument("--once", action="store_true", help="perform one update and exit")
    parser.add_argument("--dry-run", action="store_true", help="log the desired speed without changing it")
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if args.interval <= 0:
        raise SystemExit("--interval must be greater than zero")
    if args.cooldown < 0:
        raise SystemExit("--cooldown must not be negative")
    for name in ("active-speed", "idle-speed"):
        value = getattr(args, name.replace("-", "_"))
        if not 0 <= value <= 100:
            raise SystemExit(f"--{name} must be between 0 and 100")

    if args.bus is not None:
        bus = args.bus
    elif args.dry_run:
        bus = 0
        LOG.info("dry-run: skipping I2C bus detection")
    else:
        bus = find_i2c_bus()
        LOG.info("fan controller found on I2C bus %d", bus)

    daemon = FanDaemon(
        bus=bus,
        interval=args.interval,
        active_speed=args.active_speed,
        idle_speed=args.idle_speed,
        cooldown=args.cooldown,
        device_pattern=args.devices,
        dry_run=args.dry_run,
    )
    signal.signal(signal.SIGTERM, daemon.stop)
    signal.signal(signal.SIGINT, daemon.stop)
    daemon.run(once=args.once)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

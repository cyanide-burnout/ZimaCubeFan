#!/usr/bin/env python3
"""Control the ZimaCube disk-cage fan through its hwmon driver."""

from __future__ import annotations

import argparse
import ctypes
import errno
import fcntl
import glob
import logging
import math
import os
import re
import signal
import time
from collections.abc import Callable, Sequence


LOG = logging.getLogger("zimacube-fan")

# Linux UAPI constants from linux/hdreg.h.
HDIO_DRIVE_CMD = 0x031F
ATA_CHECK_POWER_MODE = 0xE5
ATA_CHECK_POWER_MODE_OLD = 0x98
ATA_ACTIVE_OR_IDLE = 0xFF

BAY_HWMON_NAME = "zimacube_bay"
LEGACY_BAY_HWMON_NAME = "zimacube_bay_fan"
BAY_HWMON_NAMES = (BAY_HWMON_NAME, LEGACY_BAY_HWMON_NAME)
HWMON_ROOT = "/sys/class/hwmon"
KEEPALIVE_SECONDS = 5.0

# The driver binds from a workqueue, so modprobe returns before the hwmon node
# exists. Discovery waits this long for it instead of failing the first start.
HWMON_WAIT_SECONDS = 10.0
HWMON_POLL_SECONDS = 0.2

# The driver refuses duties below its minimum_percent parameter; this is its
# default, used when the parameter cannot be read.
DRIVER_MINIMUM_PERCENT = 30
DRIVER_PARAMETERS = "/sys/module/zimacube_bay_fan/parameters"

# Key the backplane sensor uses among the per-disk warnings, so a failing read
# is reported once rather than every poll.
BACKPLANE = "backplane"

# Linux UAPI constants from scsi/sg.h, plus the ATA PASS-THROUGH (16) fields
# from SAT. This is the interface smartctl uses; unlike the kernel's drivetemp
# module it keeps the command entirely in this daemon's hands, so no other
# program can trigger a query behind our back.
SG_IO = 0x2285
SG_INTERFACE_ID = ord("S")
SG_DXFER_FROM_DEVICE = -3
SG_CDB_LENGTH = 16
SG_SENSE_LENGTH = 32
SG_TIMEOUT_MILLISECONDS = 5000

SG_ATA_PASS_THROUGH_16 = 0x85
SG_ATA_PROTOCOL_PIO_DATA_IN = 4
SG_ATA_T_DIR_FROM_DEVICE = 0x08
SG_ATA_BYT_BLOK_BLOCKS = 0x04
SG_ATA_T_LENGTH_SECTOR_COUNT = 0x02

ATA_SMART = 0xB0
ATA_SMART_READ_DATA = 0xD0
ATA_SMART_LBA_MID = 0x4F
ATA_SMART_LBA_HIGH = 0xC2

# Layout of the SMART data structure returned by SMART READ DATA.
SMART_DATA_LENGTH = 512
SMART_ATTRIBUTE_OFFSET = 2
SMART_ATTRIBUTE_COUNT = 30
SMART_ATTRIBUTE_LENGTH = 12

# 194 is Temperature_Celsius and is what nearly every drive reports; 190 is
# Airflow_Temperature_Cel, which some Seagate models use instead.
SMART_TEMPERATURE_ATTRIBUTES = (194, 190)
PLAUSIBLE_TEMPERATURES = (1, 99)

# How long a device set aside as "not a disk" is left alone before being asked
# again. One transient failure must not exclude a real disk for good, and the
# retry costs one ioctl per device per interval of this length.
IGNORED_RETRY_SECONDS = 600

# A reading stays usable for this many polling intervals after it was taken, so
# a drive that goes quiet does not drop out of the curve the moment its last
# sample ages out.
TEMPERATURE_VALIDITY_FACTOR = 2.5

# Block device statistics and topology come from sysfs; a module variable so the
# tests can point the daemon at a synthetic tree.
SYSFS = "/sys"

# libata names every port it creates ataN and every disk it attaches sits under
# one, which is what makes a /dev/sd? entry a disk this daemon is responsible
# for rather than a USB stick the glob happened to match.
ATA_PORT = re.compile(r"/ata\d+/")


def validate_bay_hwmon(directory: str) -> str:
    """Require the bay driver, never an unrelated hwmon with a pwm1 file."""
    with open(os.path.join(directory, "name"), encoding="ascii") as handle:
        name = handle.read().strip()
    if name not in BAY_HWMON_NAMES:
        raise ValueError(f"{directory} is {name!r}, not a ZimaCube bay hwmon device")
    for attribute in ("pwm1", "pwm1_enable"):
        if not os.path.exists(os.path.join(directory, attribute)):
            raise RuntimeError(f"{directory} has no {attribute}; load the driver with fan control enabled")
    return directory


class HwmonNotFound(RuntimeError):
    """No bay driver hwmon node exists yet; unlike a read-only one, worth waiting for."""


def find_bay_hwmon(root: str = HWMON_ROOT) -> str:
    directories = sorted(glob.glob(os.path.join(root, "hwmon*")))
    for expected_name in BAY_HWMON_NAMES:
        for directory in directories:
            try:
                with open(os.path.join(directory, "name"), encoding="ascii") as handle:
                    if handle.read().strip() == expected_name:
                        return validate_bay_hwmon(directory)
            except FileNotFoundError:
                continue  # hwmon devices can disappear while we enumerate them
    raise HwmonNotFound(f"{BAY_HWMON_NAME} hwmon device not found; load zimacube_bay_fan")


def wait_for_bay_hwmon(
    root: str = HWMON_ROOT,
    timeout: float = HWMON_WAIT_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> str:
    deadline = clock() + timeout
    while True:
        try:
            return find_bay_hwmon(root)
        except HwmonNotFound:
            if clock() >= deadline:
                raise
        sleep(HWMON_POLL_SECONDS)


def driver_minimum_percent(path: str = os.path.join(DRIVER_PARAMETERS, "minimum_percent")) -> int:
    try:
        with open(path, encoding="ascii") as handle:
            return int(handle.read().strip())
    except (OSError, ValueError):
        return DRIVER_MINIMUM_PERCENT


def read_board_temperature(hwmon: str) -> float:
    """Backplane controller temperature in degrees, from the bay driver.

    This costs one transaction with the controller and none with any disk.
    """
    with open(os.path.join(hwmon, "name"), encoding="ascii") as handle:
        name = handle.read().strip()
    if name not in BAY_HWMON_NAMES:
        # hwmon numbers can be reused after a module reload.
        raise OSError(errno.ENODEV, f"{hwmon} is {name!r}, not a ZimaCube bay hwmon device")
    with open(os.path.join(hwmon, "temp1_input"), encoding="ascii") as handle:
        return int(handle.read().strip()) / 1000


def write_hwmon_attribute(path: str, value: int) -> None:
    # hwmon numbers can change after a module reload. Never write to a reused
    # directory belonging to a different device.
    try:
        validate_bay_hwmon(os.path.dirname(path))
    except (RuntimeError, ValueError) as error:
        raise OSError(errno.ENODEV, str(error)) from error
    with open(path, "w", encoding="ascii") as handle:
        handle.write(f"{value}\n")


LIBC = ctypes.CDLL(None, use_errno=True)


def _libc_ioctl(fd: int, request: int, argument: object) -> None:
    if LIBC.ioctl(fd, request, argument) < 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


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
    level: int = logging.WARNING,
) -> str:
    try:
        power_mode = query(device)
    except OSError as error:
        LOG.log(level, "cannot read power state of %s: %s", device, error)
        return "unknown"
    return "active/idle" if power_mode == ATA_ACTIVE_OR_IDLE else "standby"


# --------------------------------------------------------------- disk temperature


class SgIoHeader(ctypes.Structure):
    """sg_io_hdr_t from scsi/sg.h, the SCSI generic pass-through request."""

    _fields_ = [
        ("interface_id", ctypes.c_int),
        ("dxfer_direction", ctypes.c_int),
        ("cmd_len", ctypes.c_uint8),
        ("mx_sb_len", ctypes.c_uint8),
        ("iovec_count", ctypes.c_uint16),
        ("dxfer_len", ctypes.c_uint32),
        ("dxferp", ctypes.c_void_p),
        ("cmdp", ctypes.c_void_p),
        ("sbp", ctypes.c_void_p),
        ("timeout", ctypes.c_uint32),
        ("flags", ctypes.c_uint32),
        ("pack_id", ctypes.c_int),
        ("usr_ptr", ctypes.c_void_p),
        ("status", ctypes.c_uint8),
        ("masked_status", ctypes.c_uint8),
        ("msg_status", ctypes.c_uint8),
        ("sb_len_wr", ctypes.c_uint8),
        ("host_status", ctypes.c_uint16),
        ("driver_status", ctypes.c_uint16),
        ("resid", ctypes.c_int),
        ("duration", ctypes.c_uint32),
        ("info", ctypes.c_uint32),
    ]


def smart_read_data_cdb() -> bytes:
    """The ATA PASS-THROUGH (16) command block carrying SMART READ DATA.

    This is the same transaction smartctl issues; only the fixed fields are
    set, so nothing here depends on the drive or on the controller.
    """
    return bytes(
        (
            SG_ATA_PASS_THROUGH_16,
            SG_ATA_PROTOCOL_PIO_DATA_IN << 1,
            # Transfer length lives in the sector-count field, counted in
            # blocks, and the data comes from the device.
            SG_ATA_T_DIR_FROM_DEVICE | SG_ATA_BYT_BLOK_BLOCKS | SG_ATA_T_LENGTH_SECTOR_COUNT,
            0,
            ATA_SMART_READ_DATA,
            0,
            1,
            0,
            0,
            0,
            ATA_SMART_LBA_MID,
            0,
            ATA_SMART_LBA_HIGH,
            0,
            ATA_SMART,
            0,
        )
    )


def _sg_transfer(fd: int, header: SgIoHeader) -> None:
    _libc_ioctl(fd, SG_IO, ctypes.byref(header))


def read_smart_data(
    device: str,
    opener: Callable[[str, int], int] = os.open,
    transfer: Callable[[int, SgIoHeader], None] = _sg_transfer,
    closer: Callable[[int], None] = os.close,
) -> bytes:
    """Return the 512-byte SMART data structure of one ATA device."""
    # The header holds these three buffers as bare addresses, so they are kept
    # in locals for the whole call to stop them being collected under the
    # kernel while the ioctl is in flight.
    command = (ctypes.c_uint8 * SG_CDB_LENGTH).from_buffer_copy(smart_read_data_cdb())
    data = (ctypes.c_uint8 * SMART_DATA_LENGTH)()
    sense = (ctypes.c_uint8 * SG_SENSE_LENGTH)()
    header = SgIoHeader(
        interface_id=SG_INTERFACE_ID,
        dxfer_direction=SG_DXFER_FROM_DEVICE,
        cmd_len=SG_CDB_LENGTH,
        mx_sb_len=SG_SENSE_LENGTH,
        dxfer_len=SMART_DATA_LENGTH,
        dxferp=ctypes.cast(data, ctypes.c_void_p),
        cmdp=ctypes.cast(command, ctypes.c_void_p),
        sbp=ctypes.cast(sense, ctypes.c_void_p),
        timeout=SG_TIMEOUT_MILLISECONDS,
    )

    fd = opener(device, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        transfer(fd, header)
    finally:
        closer(fd)

    # The ioctl succeeds whenever the request reached the drive; whether the
    # drive accepted it is reported in the SCSI status.
    if header.status or header.host_status:
        raise OSError(
            errno.EIO,
            f"SMART READ DATA rejected with status 0x{header.status:02x},"
            f" host status 0x{header.host_status:04x}",
        )
    return bytes(data)


def parse_smart_temperature(data: bytes) -> int | None:
    """Pick the temperature out of a SMART data structure, in degrees."""
    if len(data) < SMART_DATA_LENGTH:
        return None

    raw_values: dict[int, bytes] = {}
    for index in range(SMART_ATTRIBUTE_COUNT):
        start = SMART_ATTRIBUTE_OFFSET + index * SMART_ATTRIBUTE_LENGTH
        entry = data[start : start + SMART_ATTRIBUTE_LENGTH]
        identifier = entry[0]
        if identifier:
            raw_values.setdefault(identifier, entry[5:11])

    # The raw field is six bytes wide and vendors pack lifetime minima and
    # maxima into the upper ones; the current temperature is the lowest byte on
    # every drive that reports these attributes at all.
    for identifier in SMART_TEMPERATURE_ATTRIBUTES:
        raw = raw_values.get(identifier)
        if raw is None:
            continue
        celsius = raw[0]
        if PLAUSIBLE_TEMPERATURES[0] <= celsius <= PLAUSIBLE_TEMPERATURES[1]:
            return celsius
        LOG.debug("SMART attribute %d holds an implausible %d C", identifier, celsius)
    return None


def disk_temperature(
    device: str,
    reader: Callable[[str], bytes] = read_smart_data,
) -> int | None:
    """Temperature of one spinning disk, or None when it does not report one.

    The caller is responsible for only asking about disks that are already
    awake and already busy: this issues a real command to the drive.
    """
    return parse_smart_temperature(reader(device))


def is_ata_disk(device: str) -> bool:
    """True when this block device hangs off a libata port.

    Decided from where the device sits in sysfs rather than from whether it
    answers, so a disk behind a controller that is already broken when the
    daemon starts is still recognised as a disk. A USB stick or a card reader
    caught by the /dev/sd? glob sits under a usb node instead and is not one.
    """
    resolved = os.path.realpath(os.path.join(SYSFS, "block", os.path.basename(device)))
    return ATA_PORT.search(resolved + "/") is not None


def block_device_counters(device: str) -> tuple[int, ...] | None:
    """Completed reads and writes of one disk, straight from the kernel.

    Nothing here touches the drive itself, so a disk in standby can be polled
    as often as wanted; the counters simply stop moving.
    """
    path = os.path.join(SYSFS, "block", os.path.basename(device), "stat")
    try:
        with open(path, "r", encoding="ascii") as handle:
            fields = handle.read().split()
    except OSError as error:
        LOG.debug("cannot read %s: %s", path, error)
        return None
    if len(fields) < 7:
        LOG.debug("%s holds %d fields, expected at least 7", path, len(fields))
        return None
    try:
        # Reads completed, sectors read, writes completed, sectors written.
        return tuple(int(fields[index]) for index in (0, 2, 4, 6))
    except ValueError:
        LOG.debug("%s does not hold counters: %r", path, fields[:7])
        return None


def set_fan_speed(
    hwmon: str,
    speed: int,
    writer: Callable[[str, int], None] = write_hwmon_attribute,
) -> None:
    if not 0 <= speed <= 100:
        raise ValueError("fan speed must be between 0 and 100")
    pwm = (speed * 255 + 50) // 100
    writer(os.path.join(hwmon, "pwm1"), pwm)


def keep_fan_speed(
    hwmon: str,
    writer: Callable[[str, int], None] = write_hwmon_attribute,
) -> None:
    # The driver refreshes its watchdog without another controller transaction.
    writer(os.path.join(hwmon, "pwm1_enable"), 1)


def read_fan_enable(hwmon: str) -> int | None:
    """pwm1_enable of the bay driver: 1 while it holds our duty, 0 once it does not.

    The driver reports 0 after its watchdog fell back to the safe duty and after
    a reload, which applies the safe duty at probe.

    A node that is gone or now belongs to another device raises ENOENT/ENODEV,
    like a write would: with the watchdog off and a steady target no write may
    come for a long time, and this read is then the only thing that notices the
    move and gets the daemon restarted onto the new node. A value that cannot be
    read or parsed is only None.
    """
    try:
        validate_bay_hwmon(hwmon)
    except (RuntimeError, ValueError) as error:
        raise OSError(errno.ENODEV, str(error)) from error
    try:
        with open(os.path.join(hwmon, "pwm1_enable"), encoding="ascii") as handle:
            return int(handle.read().strip())
    except (OSError, ValueError):
        return None


def keepalive_seconds(path: str = "/sys/module/zimacube_bay_fan/parameters/watchdog_secs") -> float | None:
    try:
        with open(path, encoding="ascii") as handle:
            watchdog = int(handle.read().strip())
    except (OSError, ValueError):
        return KEEPALIVE_SECONDS
    if watchdog == 0:
        return None
    return min(KEEPALIVE_SECONDS, max(0.2, watchdog / 3))


def clamp(value: float, lowest: float, highest: float) -> float:
    return max(lowest, min(highest, value))


class Curve:
    """Fan speed over temperature, linear between breakpoints and flat outside.

    Temperatures have to rise strictly from point to point and speeds must not
    fall, so a hotter reading can never ask for less air than a cooler one.
    """

    def __init__(self, points: Sequence[tuple[float, int]]) -> None:
        if len(points) < 2:
            raise ValueError("a curve needs at least two points")
        for celsius, percent in points:
            # float() accepts nan and inf, and nan slips through every ordering
            # check below only to break interpolation on each poll later.
            if not math.isfinite(celsius):
                raise ValueError(f"temperature {celsius} is not a finite number")
            if not 0 <= percent <= 100:
                raise ValueError(f"speed {percent} at {celsius:g} C is outside 0-100")
        for (celsius, percent), (next_celsius, next_percent) in zip(points, points[1:]):
            if next_celsius <= celsius:
                raise ValueError(f"temperatures must rise: {next_celsius:g} C follows {celsius:g} C")
            if next_percent < percent:
                raise ValueError(f"speeds must not fall: {next_percent} at {next_celsius:g} C follows {percent}")
        self.points = tuple((float(celsius), int(percent)) for celsius, percent in points)

    @classmethod
    def parse(cls, text: str) -> Curve:
        """Read "40:40,50:70,55:100", temperature in degrees before each colon."""
        points = []
        for item in text.split(","):
            celsius, separator, percent = item.strip().partition(":")
            if not separator:
                raise ValueError(f"{item.strip()!r} is not TEMPERATURE:SPEED")
            try:
                points.append((float(celsius), int(percent)))
            except ValueError:
                raise ValueError(f"{item.strip()!r} is not TEMPERATURE:SPEED") from None
        return cls(points)

    def __call__(self, celsius: float) -> int:
        first_celsius, first_percent = self.points[0]
        if celsius <= first_celsius:
            return first_percent
        for (low, low_percent), (high, high_percent) in zip(self.points, self.points[1:]):
            if celsius <= high:
                return round(low_percent + (celsius - low) / (high - low) * (high_percent - low_percent))
        return self.points[-1][1]

    def __str__(self) -> str:
        return ",".join(f"{celsius:g}:{percent}" for celsius, percent in self.points)


def curve_argument(text: str) -> Curve:
    try:
        return Curve.parse(text)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from None


class FanDaemon:
    def __init__(
        self,
        hwmon: str,
        interval: float,
        active_speed: int,
        idle_speed: int,
        cooldown: float,
        device_pattern: str,
        dry_run: bool = False,
        max_speed: int = 100,
        temperature_low: float = 40,
        temperature_high: float = 55,
        temperature_interval: float = 120,
        hysteresis: int = 3,
        down_step: int = 5,
        temperature_query: Callable[[str], int | None] | None = None,
        hdd_curve: Curve | None = None,
        board_curve: Curve | None = None,
        board_query: Callable[[str], float] = read_board_temperature,
        power_query: Callable[[str], int] = query_ata_power_mode,
        fan_writer: Callable[[str, int], None] = set_fan_speed,
        keepalive_writer: Callable[[str], None] = keep_fan_speed,
        enable_query: Callable[[str], int | None] = read_fan_enable,
        keepalive_interval: float | None = KEEPALIVE_SECONDS,
        counters: Callable[[str], tuple[int, ...] | None] = block_device_counters,
        classifier: Callable[[str], bool] = is_ata_disk,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.hwmon = hwmon
        self.interval = interval
        self.active_speed = active_speed
        self.idle_speed = idle_speed
        self.cooldown = cooldown
        self.device_pattern = device_pattern
        self.dry_run = dry_run
        self.max_speed = max_speed
        self.temperature_low = temperature_low
        self.temperature_high = temperature_high
        self.temperature_interval = temperature_interval
        self.hysteresis = hysteresis
        self.down_step = down_step
        self.temperature_query = temperature_query
        # Without an explicit curve the disks follow the two-point line from
        # temperature_low to temperature_high, spanning the idle to maximum
        # speed. It is only built when disk temperatures are read at all.
        if hdd_curve is None and temperature_query is not None:
            hdd_curve = Curve(((temperature_low, idle_speed), (temperature_high, max_speed)))
        self.hdd_curve = hdd_curve
        self.board_curve = board_curve
        self.board_query = board_query
        self.board_demand: int | None = None
        self.power_query = power_query
        self.fan_writer = fan_writer
        self.keepalive_writer = keepalive_writer
        self.enable_query = enable_query
        self.keepalive_interval = keepalive_interval
        self.counters = counters
        self.classifier = classifier
        self.clock = clock
        self.running = True
        self.last_speed: int | None = None
        self.last_active_at: float | None = None
        self.answered: set[str] = set()
        self.ignored: dict[str, float] = {}
        self.last_counters: dict[str, tuple[int, ...]] = {}
        self.last_attempt: dict[str, float] = {}
        self.temperatures: dict[str, tuple[float, int]] = {}
        self.unreadable: set[str] = set()

    def stop(self, _signum: int, _frame: object) -> None:
        self.running = False

    # ------------------------------------------------------------- temperature

    def busy(self, device: str) -> bool:
        """True when the kernel counted I/O on this disk since the last poll."""
        current = self.counters(device)
        if current is None:
            return False
        previous = self.last_counters.get(device)
        self.last_counters[device] = current
        return previous is not None and current != previous

    def sample(self, device: str, spinning: bool, now: float) -> None:
        """Read one disk's temperature, but only when that costs nothing.

        Two conditions have to hold. The disk must be awake, so the query
        cannot spin it up; and it must have served I/O since the last poll, so
        its own standby timer has just been reset by that traffic anyway. A
        disk that is merely spinning with nothing to do is left alone, because
        a query might restart the timer and keep it from ever sleeping — the
        reason smartd has to be disabled on this machine.
        """
        assert self.temperature_query is not None
        # Counters are refreshed even for a sleeping disk, so the comparison is
        # always against the previous poll rather than against whenever this
        # disk was last awake.
        busy = self.busy(device)
        if not (spinning and busy):
            return

        attempted = self.last_attempt.get(device)
        if attempted is not None and now - attempted < self.temperature_interval:
            return
        self.last_attempt[device] = now

        try:
            celsius = self.temperature_query(device)
        except OSError as error:
            self.complain(device, f"cannot read the temperature of {device}: {error}")
            return
        if celsius is None:
            self.complain(device, f"{device} reports no usable SMART temperature")
            return

        self.unreadable.discard(device)
        LOG.debug("%s: %d C", device, celsius)
        self.temperatures[device] = (now, celsius)

    def complain(self, device: str, message: str) -> None:
        """Warn about a disk once, then keep quiet about it."""
        if device in self.unreadable:
            LOG.debug("%s", message)
            return
        self.unreadable.add(device)
        LOG.warning("%s", message)

    def is_disk(self, device: str) -> bool:
        """Whether an unresponsive device is one this daemon has to cool.

        The sysfs topology decides it, and a successful answer at any point
        settles it too: a disk on a controller this classifier does not
        recognise must not be dropped just because sysfs looked unfamiliar.
        """
        return device in self.answered or self.classifier(device)

    def hottest(self, now: float) -> tuple[int, str] | None:
        validity = self.temperature_interval * TEMPERATURE_VALIDITY_FACTOR
        fresh = [
            (celsius, device)
            for device, (taken, celsius) in self.temperatures.items()
            if now - taken <= validity
        ]
        return max(fresh) if fresh else None

    def forget(self, devices: Sequence[str]) -> None:
        for state in (self.last_counters, self.last_attempt, self.temperatures, self.ignored):
            for device in set(state) - set(devices):
                del state[device]
        self.unreadable &= set(devices) | {BACKPLANE}
        self.answered &= set(devices)

    # ------------------------------------------------------------------ policy

    def target(self, active: bool, now: float) -> tuple[int, str]:
        """The speed the current disk state calls for, before smoothing."""
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

        heat = self.heat(now)
        if not heat:
            return speed, reason

        # Activity is a feed-forward term and temperature a feedback one: the
        # first answers work that has started, the second heat that has already
        # arrived. Each curve may only raise the speed activity asked for, and
        # the most demanding source decides.
        curve, source = max(heat)
        if curve <= speed:
            return speed, f"{reason}; " + ", ".join(description for _, description in heat)
        return curve, source

    def heat(self, now: float) -> list[tuple[int, str]]:
        """What each temperature source asks for, with what it measured."""
        heat = []
        hottest = self.hottest(now)
        if hottest is not None and self.hdd_curve is not None:
            celsius, device = hottest
            heat.append((self.hdd_curve(celsius), f"hottest disk {device} at {celsius} C"))
        if self.board_curve is not None:
            board = self.board_temperature()
            if board is None:
                # A sensor that was asked for and cannot be read needs an
                # unknown amount of cooling rather than none, as a disk that
                # cannot be asked does. Neither the active speed nor what the
                # sensor last asked for is given up until it answers again.
                held = max(self.active_speed, self.board_demand or 0)
                heat.append((held, "backplane temperature unknown"))
            else:
                self.board_demand = self.board_curve(board)
                heat.append((self.board_demand, f"backplane at {board:.1f} C"))
        return heat

    def board_temperature(self) -> float | None:
        try:
            celsius = self.board_query(self.hwmon)
        except (OSError, ValueError) as error:
            self.complain(BACKPLANE, f"cannot read the backplane temperature: {error}")
            return None
        self.unreadable.discard(BACKPLANE)
        LOG.debug("backplane: %.1f C", celsius)
        return celsius

    @property
    def thermal(self) -> bool:
        return self.temperature_query is not None or self.board_curve is not None

    def smooth(self, target: int) -> int:
        """Rate limit the descent so the fan cannot pump around a threshold.

        Only the temperature curves need this. Without them the speed is a two
        level signal that has nowhere to oscillate, and slowing its transitions
        down would only make the daemon less responsive.
        """
        current = self.last_speed
        if not self.thermal or current is None:
            return target
        if target > current:
            # Heat is answered at once.
            return target if target - current >= self.hysteresis else current
        if current - target >= self.hysteresis:
            return max(target, current - self.down_step)
        return current

    def update(self) -> int:
        devices = sorted(glob.glob(self.device_pattern))
        now = self.clock()
        active = False
        faulted: list[str] = []

        for device in devices:
            retrying = device in self.ignored
            if retrying:
                # Set aside as not a disk, but not for good: one failure that
                # happened to be the first must not exclude a real disk
                # permanently, so it is asked again now and then. The topology
                # is re-read every round as well, cheaply, in case the node has
                # since been taken over by a real disk.
                if now - self.ignored[device] < IGNORED_RETRY_SECONDS and not self.classifier(device):
                    continue

            # Failures on a retry are not news, so they stay out of the journal
            # until the device turns out to be a disk after all.
            state = drive_state(
                device,
                self.power_query,
                logging.DEBUG if retrying else logging.WARNING,
            )
            LOG.debug("%s: %s", device, state)
            if state == "unknown":
                # A disk that cannot be asked needs an unknown amount of
                # cooling rather than none, so the airflow is held up until it
                # answers again. Anything that is not a disk is set aside here
                # rather than earlier: every device gets a chance to answer,
                # because an answer is better evidence than the topology is.
                if self.is_disk(device):
                    self.ignored.pop(device, None)
                    faulted.append(device)
                    continue
                if not retrying:
                    LOG.info("ignoring %s: it is not a disk on an ATA port", device)
                self.ignored[device] = now
                continue

            if retrying:
                LOG.info("%s answered after all; it is a disk", device)
            self.ignored.pop(device, None)
            self.answered.add(device)
            spinning = state == "active/idle"
            active = active or spinning
            if self.temperature_query is not None:
                self.sample(device, spinning, now)
        self.forget(devices)

        target, reason = self.target(active or bool(faulted), now)
        if faulted and not active:
            reason = "state of " + ", ".join(faulted) + " is unknown"
        speed = int(clamp(self.smooth(target), self.idle_speed, self.max_speed))

        # The keepalive catches this too, but only when the watchdog is on.
        lost = (
            self.last_speed is not None
            and not self.dry_run
            and self.enable_query(self.hwmon) == 0
        )
        if lost:
            LOG.warning("bay driver no longer holds %d%% (pwm1_enable=0), writing the duty again", self.last_speed)

        if speed != self.last_speed or lost:
            # Routine: with the temperature curves this fires every few rounds. --verbose shows it.
            LOG.debug("setting fan to %d%% (%s; checked %d disks)", speed, reason, len(devices))
            if not self.dry_run:
                self.fan_writer(self.hwmon, speed)
            self.last_speed = speed
        return speed

    def run(self, once: bool = False) -> None:
        next_keepalive = (
            time.monotonic() + self.keepalive_interval
            if self.keepalive_interval is not None else None
        )
        while self.running:
            try:
                self.update()
            except OSError as error:
                if error.errno in (errno.ENOENT, errno.ENODEV):
                    raise  # systemd restarts us and discovers the new hwmon path
                LOG.exception("fan update failed")
            except Exception:
                LOG.exception("fan update failed")
            if once:
                return
            end = time.monotonic() + self.interval
            while self.running and time.monotonic() < end:
                now = time.monotonic()
                if next_keepalive is not None and now >= next_keepalive:
                    if self.last_speed is not None and not self.dry_run:
                        try:
                            self.keepalive_writer(self.hwmon)
                        except OSError as error:
                            if error.errno == errno.EINVAL:
                                # The driver's watchdog already fell back.
                                try:
                                    self.fan_writer(self.hwmon, self.last_speed)
                                except OSError as restore_error:
                                    if restore_error.errno in (errno.ENOENT, errno.ENODEV):
                                        raise
                                    self.last_speed = None
                                    LOG.exception("cannot restore fan duty after watchdog fallback")
                            elif error.errno in (errno.ENOENT, errno.ENODEV):
                                raise  # a module reload may have changed hwmonN
                            else:
                                self.last_speed = None
                                LOG.exception("fan watchdog keepalive failed")
                    next_keepalive = time.monotonic() + self.keepalive_interval
                remaining = end - time.monotonic()
                if next_keepalive is not None:
                    remaining = min(remaining, next_keepalive - time.monotonic())
                if remaining > 0:
                    time.sleep(min(0.5, remaining))


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interval", type=float, default=30, help="poll interval in seconds (default: 30)")
    parser.add_argument("--active-speed", type=int, default=80, help="speed when a disk is active (default: 80)")
    parser.add_argument("--idle-speed", type=int, default=40, help="speed when no disk is active (default: 40)")
    parser.add_argument("--cooldown", type=float, default=120, help="delay before idle speed in seconds (default: 120)")
    parser.add_argument("--disk-temp", action="store_true", help="also raise the speed with the temperature of busy disks")
    parser.add_argument("--max-speed", type=int, default=100, help="speed at full thermal pressure (default: 100)")
    parser.add_argument("--disk-temp-low", type=float, help="disk temperature at idle speed (default: 40)")
    parser.add_argument("--disk-temp-high", type=float, help="disk temperature at maximum speed (default: 55)")
    parser.add_argument("--hdd-curve", type=curve_argument, metavar="T:P,...", help="disk curve as temperature:speed points, instead of --disk-temp-low/high")
    parser.add_argument("--board-curve", type=curve_argument, metavar="T:P,...", help="also raise the speed with the backplane controller temperature along this curve")
    parser.add_argument("--temp-interval", type=float, default=120, help="seconds between temperature reads of one disk (default: 120)")
    parser.add_argument("--hysteresis", type=int, default=3, help="ignore changes smaller than this many percent (default: 3)")
    parser.add_argument("--down-step", type=int, default=5, help="largest speed reduction per interval in percent (default: 5)")
    parser.add_argument("--devices", default="/dev/sd?", help="disk glob (default: /dev/sd?)")
    parser.add_argument("--hwmon", help="bay driver hwmon directory; found by name by default")
    parser.add_argument("--list-disk-temp", action="store_true", help="report the state and temperature of every disk and exit")
    parser.add_argument("--set-speed", type=int, help="set fan duty once; the driver later falls back to 80%%")
    parser.add_argument("--once", action="store_true", help="perform one update and exit")
    parser.add_argument("--dry-run", action="store_true", help="log the desired speed without changing it")
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args(argv)


def list_disk_temperatures(pattern: str) -> None:
    """Report every disk, querying only the ones that are already awake."""
    devices = sorted(glob.glob(pattern))
    if not devices:
        print(f"no disks match {pattern}")
        return

    for device in devices:
        state = drive_state(device)
        counters = block_device_counters(device)
        traffic = f"{counters[0]} reads, {counters[2]} writes" if counters else "no counters"
        if state != "active/idle":
            print(f"  {device}  {state:12}  {traffic}; not queried")
            continue
        try:
            celsius = disk_temperature(device)
        except OSError as error:
            print(f"  {device}  {state:12}  {traffic}; SMART read failed: {error}")
            continue
        reading = f"{celsius} C" if celsius is not None else "no SMART temperature"
        print(f"  {device}  {state:12}  {traffic}; {reading}")


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if args.list_disk_temp:
        list_disk_temperatures(args.devices)
        return 0

    if args.interval <= 0:
        raise SystemExit("--interval must be greater than zero")
    if args.cooldown < 0:
        raise SystemExit("--cooldown must not be negative")
    for name in ("active-speed", "idle-speed", "max-speed"):
        value = getattr(args, name.replace("-", "_"))
        if not 0 <= value <= 100:
            raise SystemExit(f"--{name} must be between 0 and 100")
    if args.set_speed is not None and not 0 <= args.set_speed <= 100:
        raise SystemExit("--set-speed must be between 0 and 100")
    # The driver refuses anything lower. In the daemon the idle speed is the
    # floor every other speed and every curve result is clamped to; a one-off
    # --set-speed does not use it and is checked on its own.
    minimum = driver_minimum_percent()
    if args.set_speed is None and args.idle_speed < minimum:
        raise SystemExit(f"--idle-speed must be at least {minimum}, the driver's minimum_percent")
    if args.set_speed is not None and args.set_speed < minimum:
        raise SystemExit(f"--set-speed must be at least {minimum}, the driver's minimum_percent")
    # The speeds bind each other: the idle speed is a floor and the maximum a
    # ceiling, so an order other than this one silently overrides one of them.
    if args.idle_speed > args.active_speed:
        raise SystemExit("--idle-speed must not exceed --active-speed")
    if args.active_speed > args.max_speed:
        raise SystemExit("--active-speed must not exceed --max-speed")
    if args.hdd_curve is not None:
        if not args.disk_temp:
            raise SystemExit("--hdd-curve needs --disk-temp, which is what lets the daemon read disk temperatures")
        if args.disk_temp_low is not None or args.disk_temp_high is not None:
            raise SystemExit("--hdd-curve replaces --disk-temp-low and --disk-temp-high; give one or the other")
    if args.disk_temp_low is None:
        args.disk_temp_low = 40
    if args.disk_temp_high is None:
        args.disk_temp_high = 55
    if args.disk_temp:
        if args.disk_temp_low >= args.disk_temp_high:
            raise SystemExit("--disk-temp-low must be below --disk-temp-high")
        if args.temp_interval <= 0:
            raise SystemExit("--temp-interval must be greater than zero")
    if args.disk_temp or args.board_curve is not None:
        if args.hysteresis < 0:
            raise SystemExit("--hysteresis must not be negative")
        if args.down_step < 1:
            raise SystemExit("--down-step must be at least 1")

    if args.dry_run and args.board_curve is None:
        hwmon = args.hwmon or ""
        LOG.info("dry-run: skipping bay hwmon discovery")
    else:
        # A dry run still needs the device when the backplane is read.
        try:
            hwmon = validate_bay_hwmon(args.hwmon) if args.hwmon else wait_for_bay_hwmon()
        except (OSError, RuntimeError, ValueError) as error:
            raise SystemExit(f"bay fan hwmon unavailable: {error}") from None
        LOG.info("bay fan hwmon device: %s", hwmon)

    if args.set_speed is not None:
        LOG.info("setting fan to %d%%", args.set_speed)
        if not args.dry_run:
            set_fan_speed(hwmon, args.set_speed)
        return 0

    daemon = FanDaemon(
        hwmon=hwmon,
        interval=args.interval,
        active_speed=args.active_speed,
        idle_speed=args.idle_speed,
        cooldown=args.cooldown,
        device_pattern=args.devices,
        dry_run=args.dry_run,
        max_speed=args.max_speed,
        temperature_low=args.disk_temp_low,
        temperature_high=args.disk_temp_high,
        temperature_interval=args.temp_interval,
        hysteresis=args.hysteresis,
        down_step=args.down_step,
        temperature_query=disk_temperature if args.disk_temp else None,
        hdd_curve=args.hdd_curve,
        board_curve=args.board_curve,
        keepalive_interval=keepalive_seconds() if not args.dry_run else None,
    )
    if daemon.hdd_curve is not None:
        LOG.info("disk curve: %s", daemon.hdd_curve)
    if daemon.board_curve is not None:
        LOG.info("backplane curve: %s", daemon.board_curve)
    signal.signal(signal.SIGTERM, daemon.stop)
    signal.signal(signal.SIGINT, daemon.stop)
    daemon.run(once=args.once)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

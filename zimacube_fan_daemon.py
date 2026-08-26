#!/usr/bin/env python3
"""Control the ZimaCube 2 disk-cage fan directly through Linux ioctls."""

from __future__ import annotations

import argparse
import ctypes
import errno
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

# A reading stays usable for this many polling intervals after it was taken, so
# a drive that goes quiet does not drop out of the curve the moment its last
# sample ages out.
TEMPERATURE_VALIDITY_FACTOR = 2.5

# Block device statistics come from sysfs; a module variable so the tests can
# point the daemon at a synthetic tree.
SYSFS = "/sys"


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
    bus: int,
    speed: int,
    writer: Callable[[int, int, int, bytes], None] = write_i2c_block,
) -> None:
    if not 0 <= speed <= 100:
        raise ValueError("fan speed must be between 0 and 100")

    # Same I2C block transaction as:
    # i2cset -f -y BUS 0x69 0x04 0x01 SPEED 0 0 0 0 1 0 i
    writer(bus, FAN_ADDRESS, FAN_COMMAND, bytes((0x01, speed, 0, 0, 0, 0, 1, 0)))


def clamp(value: float, lowest: float, highest: float) -> float:
    return max(lowest, min(highest, value))


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
        max_speed: int = 100,
        temperature_low: float = 40,
        temperature_high: float = 55,
        temperature_interval: float = 120,
        hysteresis: int = 3,
        down_step: int = 5,
        temperature_query: Callable[[str], int | None] | None = None,
        power_query: Callable[[str], int] = query_ata_power_mode,
        fan_writer: Callable[[int, int, int, bytes], None] = write_i2c_block,
        counters: Callable[[str], tuple[int, ...] | None] = block_device_counters,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.bus = bus
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
        self.power_query = power_query
        self.fan_writer = fan_writer
        self.counters = counters
        self.clock = clock
        self.running = True
        self.last_speed: int | None = None
        self.last_active_at: float | None = None
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

    def hottest(self, now: float) -> tuple[int, str] | None:
        validity = self.temperature_interval * TEMPERATURE_VALIDITY_FACTOR
        fresh = [
            (celsius, device)
            for device, (taken, celsius) in self.temperatures.items()
            if now - taken <= validity
        ]
        return max(fresh) if fresh else None

    def forget(self, devices: Sequence[str]) -> None:
        for state in (self.last_counters, self.last_attempt, self.temperatures):
            for device in set(state) - set(devices):
                del state[device]
        self.unreadable &= set(devices)

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

        hottest = self.hottest(now)
        if hottest is None:
            return speed, reason

        # Activity is a feed-forward term and temperature a feedback one: the
        # first answers work that has started, the second heat that has already
        # arrived. The curve may only raise the speed activity asked for.
        celsius, device = hottest
        pressure = clamp(
            (celsius - self.temperature_low) / (self.temperature_high - self.temperature_low),
            0.0,
            1.0,
        )
        curve = round(self.idle_speed + pressure * (self.max_speed - self.idle_speed))
        if curve <= speed:
            return speed, f"{reason}; hottest disk {device} at {celsius} C"
        return curve, f"{device} at {celsius} C"

    def smooth(self, target: int) -> int:
        """Rate limit the descent so the fan cannot pump around a threshold.

        Only the temperature curve needs this. Without it the speed is a two
        level signal that has nowhere to oscillate, and slowing its transitions
        down would only make the daemon less responsive.
        """
        current = self.last_speed
        if self.temperature_query is None or current is None:
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

        for device in devices:
            state = drive_state(device, self.power_query)
            LOG.debug("%s: %s", device, state)
            spinning = state == "active/idle"
            active = active or spinning
            if self.temperature_query is not None:
                self.sample(device, spinning, now)
        self.forget(devices)

        target, reason = self.target(active, now)
        speed = int(clamp(self.smooth(target), self.idle_speed, self.max_speed))

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


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interval", type=float, default=30, help="poll interval in seconds (default: 30)")
    parser.add_argument("--active-speed", type=int, default=80, help="speed when a disk is active (default: 80)")
    parser.add_argument("--idle-speed", type=int, default=40, help="speed when no disk is active (default: 40)")
    parser.add_argument("--cooldown", type=float, default=120, help="delay before idle speed in seconds (default: 120)")
    parser.add_argument("--disk-temp", action="store_true", help="also raise the speed with the temperature of busy disks")
    parser.add_argument("--max-speed", type=int, default=100, help="speed at full thermal pressure (default: 100)")
    parser.add_argument("--disk-temp-low", type=float, default=40, help="disk temperature at idle speed (default: 40)")
    parser.add_argument("--disk-temp-high", type=float, default=55, help="disk temperature at maximum speed (default: 55)")
    parser.add_argument("--temp-interval", type=float, default=120, help="seconds between temperature reads of one disk (default: 120)")
    parser.add_argument("--hysteresis", type=int, default=3, help="ignore changes smaller than this many percent (default: 3)")
    parser.add_argument("--down-step", type=int, default=5, help="largest speed reduction per interval in percent (default: 5)")
    parser.add_argument("--devices", default="/dev/sd?", help="disk glob (default: /dev/sd?)")
    parser.add_argument("--bus", type=int, help="I2C bus; auto-detected by default")
    parser.add_argument("--list-disk-temp", action="store_true", help="report the state and temperature of every disk and exit")
    parser.add_argument("--set-speed", type=int, help="set the fan to this speed and exit")
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
    if args.disk_temp:
        if args.disk_temp_low >= args.disk_temp_high:
            raise SystemExit("--disk-temp-low must be below --disk-temp-high")
        if args.max_speed < args.active_speed:
            raise SystemExit("--max-speed must not be below --active-speed")
        if args.temp_interval <= 0:
            raise SystemExit("--temp-interval must be greater than zero")
        if args.hysteresis < 0:
            raise SystemExit("--hysteresis must not be negative")
        if args.down_step < 1:
            raise SystemExit("--down-step must be at least 1")

    if args.bus is not None:
        bus = args.bus
    elif args.dry_run:
        bus = 0
        LOG.info("dry-run: skipping I2C bus detection")
    else:
        bus = find_i2c_bus()
        LOG.info("fan controller found on I2C bus %d", bus)

    if args.set_speed is not None:
        LOG.info("setting fan to %d%%", args.set_speed)
        if not args.dry_run:
            set_fan_speed(bus, args.set_speed)
        return 0

    daemon = FanDaemon(
        bus=bus,
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
    )
    signal.signal(signal.SIGTERM, daemon.stop)
    signal.signal(signal.SIGINT, daemon.stop)
    daemon.run(once=args.once)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

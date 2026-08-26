#!/usr/bin/env python3
"""Control the ZimaCube system fan from 10G NIC and Drive Bay 7 NVMe temperatures."""

from __future__ import annotations

import argparse
import glob
import logging
import os
import re
import signal
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass


LOG = logging.getLogger("zimacube-sysfan")

# Every reading and every write goes through sysfs. The prefix is a module
# variable so the tests can point the daemon at a synthetic tree.
SYSFS = "/sys"

# Provided by the out-of-tree kernel driver zimacube_ec_fan:
# https://github.com/cyanide-burnout/zimacube-ec-fan
#
# Channel 1 is the CPU fan and is never referenced here; channel 2 is the
# system fan this daemon owns. Writing pwm2 puts the channel into manual mode
# by itself, writing 2 to pwm2_enable hands it back to the EC's own curve.
EC_HWMON_NAME = "zimacube_ec"
SYS_FAN_PWM = "pwm2"
SYS_FAN_ENABLE = "pwm2_enable"
PWM_ENABLE_AUTO = 2
PWM_RAW_MAX = 255

# Base class and subclass from the PCI code, the programming interface dropped.
PCI_CLASS_ETHERNET = 0x0200
PCI_CLASS_NVME = 0x0108

BDF = re.compile(r"^[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]$")
SHORT_BDF = re.compile(r"^[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]$")

# PCI identities of 10-gigabit Ethernet controllers, used to pick the card out
# of the PCI tree without depending on an interface name. The list covers the
# Aquantia/Marvell AQtion family fitted to current ZimaCube boards plus the
# common server parts; anything else can be added with --10g-nic-id, and a
# specific card pinned with --10g-nic-pci.
TEN_GIGABIT_PCI_IDS = frozenset(
    {
        (0x1D6A, 0x00B1), (0x1D6A, 0x07B1), (0x1D6A, 0x08B1), (0x1D6A, 0x09B1),
        (0x1D6A, 0x11B1), (0x1D6A, 0x12B1), (0x1D6A, 0x80B1), (0x1D6A, 0x87B1),
        (0x1D6A, 0x88B1), (0x1D6A, 0x89B1), (0x1D6A, 0x91B1), (0x1D6A, 0x92B1),
        (0x1D6A, 0x00C0), (0x1D6A, 0x04C0), (0x1D6A, 0x11C0), (0x1D6A, 0x12C0),
        (0x1D6A, 0x14C0), (0x1D6A, 0x34C0), (0x1D6A, 0x93C0), (0x1D6A, 0x94C0),
        (0x1D6A, 0xD100), (0x1D6A, 0xD107), (0x1D6A, 0xD108), (0x1D6A, 0xD109),
        (0x8086, 0x10FB), (0x8086, 0x1528), (0x8086, 0x1563), (0x8086, 0x15AA),
        (0x14E4, 0x16CA), (0x15B3, 0x1015),
    }
)

# The label the Linux NVMe driver gives the controller's own composite reading.
# Sensor 1..8 are vendor defined and are deliberately not used.
NVME_COMPOSITE_LABEL = "Composite"


def sysfs_path(*parts: str) -> str:
    return os.path.join(SYSFS, *parts)


def clamp(value: float, lowest: float, highest: float) -> float:
    return max(lowest, min(highest, value))


def read_text(path: str) -> str | None:
    try:
        with open(path, "r", encoding="ascii", errors="replace") as handle:
            return handle.read().strip()
    except OSError as error:
        LOG.debug("cannot read %s: %s", path, error)
        return None


def read_int(path: str) -> int | None:
    text = read_text(path)
    if text is None:
        return None
    try:
        return int(text, 16) if text.lower().startswith("0x") else int(text, 10)
    except ValueError:
        LOG.debug("%s does not hold an integer: %r", path, text)
        return None


def write_attribute(path: str, value: int) -> None:
    with open(path, "w", encoding="ascii") as handle:
        handle.write(f"{value}\n")


# ------------------------------------------------------------------ PCI topology


def normalise_bdf(value: str) -> str:
    """Accept 5f:00.0 as well as 0000:5f:00.0 and return the full form."""
    text = value.strip().lower()
    if SHORT_BDF.match(text):
        text = "0000:" + text
    if not BDF.match(text):
        raise ValueError(f"{value!r} is not a PCI address such as 0000:5f:00.0")
    return text


def pci_device_path(device: str) -> str:
    return sysfs_path("bus/pci/devices", device)


def list_pci_devices() -> list[str]:
    return sorted(os.path.basename(path) for path in glob.glob(sysfs_path("bus/pci/devices/*")))


def pci_class(device: str) -> int | None:
    value = read_int(os.path.join(pci_device_path(device), "class"))
    return None if value is None else value >> 8


def pci_identity(device: str) -> tuple[int, int] | None:
    vendor = read_int(os.path.join(pci_device_path(device), "vendor"))
    product = read_int(os.path.join(pci_device_path(device), "device"))
    if vendor is None or product is None:
        return None
    return vendor, product


def pci_ancestors(device: str) -> list[str]:
    """Upstream bridges of a device, root port first, the device itself excluded."""
    chain = [part for part in os.path.realpath(pci_device_path(device)).split(os.sep) if BDF.match(part)]
    return chain[:-1]


def list_pci_devices_of_class(wanted: int) -> list[str]:
    return [device for device in list_pci_devices() if pci_class(device) == wanted]


def find_topology_root(devices: Sequence[str]) -> str | None:
    """Return the bridge or switch that fans out to the largest group of devices.

    Drive Bay 7 hangs off a PCIe switch, so the bay is the deepest bridge that
    still has every one of its NVMe controllers below it: the root port above
    the switch sees the same devices, the downstream ports below it see one
    each. Ties on the number of devices are therefore broken by depth.
    """
    beneath: dict[str, set[str]] = {}
    depth: dict[str, int] = {}
    for device in devices:
        for level, bridge in enumerate(pci_ancestors(device)):
            beneath.setdefault(bridge, set()).add(device)
            depth[bridge] = level

    best: str | None = None
    best_rank = (1, -1)
    for bridge, group in beneath.items():
        # A single controller below a bridge is indistinguishable from an M.2
        # slot on the mainboard, so it never identifies the bay on its own.
        if len(group) < 2:
            continue
        rank = (len(group), depth[bridge])
        if rank > best_rank:
            best, best_rank = bridge, rank
    return best


def devices_below(root: str, devices: Iterable[str]) -> list[str]:
    return sorted(device for device in devices if root in pci_ancestors(device))


# -------------------------------------------------------------------- hwmon nodes


def hwmon_directories(device: str) -> list[str]:
    """hwmon nodes of a PCI device, including those parented to its net device."""
    base = pci_device_path(device)
    found = glob.glob(os.path.join(base, "hwmon", "hwmon*"))
    found += glob.glob(os.path.join(base, "net", "*", "hwmon*"))
    return sorted(found)


def temperature_inputs(directory: str) -> list[str]:
    return sorted(glob.glob(os.path.join(directory, "temp*_input")))


def temperature_label(input_path: str) -> str | None:
    return read_text(input_path[: -len("_input")] + "_label")


def describe_inputs(inputs: Sequence[str]) -> str:
    parts = []
    for path in inputs:
        label = temperature_label(path)
        name = os.path.basename(path)[: -len("_input")]
        parts.append(f"{name}={label}" if label else name)
    return ", ".join(parts)


# ------------------------------------------------------------------------ sensors


@dataclass(frozen=True)
class TemperatureSensor:
    """One thermal input: a PCI device, its temperature files and its range."""

    kind: str
    device: str
    detail: str
    inputs: tuple[str, ...]
    low: float
    high: float

    @property
    def identity(self) -> str:
        return f"{self.kind} {self.device}"

    def read(self) -> float | None:
        """Highest readable input in degrees, or None while the device is gone."""
        values = [value for value in (read_int(path) for path in self.inputs) if value is not None]
        if not values:
            return None
        return max(values) / 1000.0

    def pressure(self, temperature: float) -> float:
        return clamp((temperature - self.low) / (self.high - self.low), 0.0, 1.0)


def nic_sensors(devices: Sequence[str], low: float, high: float) -> list[TemperatureSensor]:
    sensors = []
    for device in devices:
        inputs: list[str] = []
        for directory in hwmon_directories(device):
            inputs.extend(temperature_inputs(directory))
        if not inputs:
            LOG.debug("10G NIC %s exposes no hwmon temperature, skipping it", device)
            continue
        # PHY and MAC readings track each other closely on this card; taking the
        # highest one needs no per-driver naming and errs towards more airflow.
        sensors.append(
            TemperatureSensor("10g-nic", device, describe_inputs(inputs), tuple(sorted(inputs)), low, high)
        )
    return sensors


def nvme_composite_input(device: str) -> str | None:
    for directory in sorted(glob.glob(os.path.join(pci_device_path(device), "nvme", "nvme*", "hwmon*"))):
        for path in temperature_inputs(directory):
            if temperature_label(path) == NVME_COMPOSITE_LABEL:
                return path
    return None


def nvme_sensors(devices: Sequence[str], low: float, high: float) -> list[TemperatureSensor]:
    sensors = []
    for device in devices:
        path = nvme_composite_input(device)
        if path is None:
            LOG.debug("NVMe %s reports no %s temperature, skipping it", device, NVME_COMPOSITE_LABEL)
            continue
        sensors.append(
            TemperatureSensor("bay7-nvme", device, NVME_COMPOSITE_LABEL, (path,), low, high)
        )
    return sensors


def find_ten_gigabit_nics(extra_ids: Iterable[tuple[int, int]] = (), pinned: str | None = None) -> list[str]:
    if pinned is not None:
        if not os.path.exists(pci_device_path(pinned)):
            LOG.warning("no PCI device at %s", pinned)
            return []
        return [pinned]

    known = TEN_GIGABIT_PCI_IDS | set(extra_ids)
    found = []
    for device in list_pci_devices_of_class(PCI_CLASS_ETHERNET):
        identity = pci_identity(device)
        if identity is not None and identity in known:
            found.append(device)
    return found


def find_bay7_nvme(pinned_root: str | None = None) -> tuple[str | None, list[str]]:
    """Return the Bay 7 upstream bridge and the NVMe controllers below it."""
    controllers = list_pci_devices_of_class(PCI_CLASS_NVME)
    if not controllers:
        return None, []
    root = pinned_root if pinned_root is not None else find_topology_root(controllers)
    if root is None:
        return None, []
    return root, devices_below(root, controllers)


# ------------------------------------------------------------------- fan control


class SystemFan:
    """The system-fan channel of the zimacube_ec hwmon device."""

    def __init__(self, hwmon_name: str = EC_HWMON_NAME, dry_run: bool = False) -> None:
        self.hwmon_name = hwmon_name
        self.dry_run = dry_run
        self.directory: str | None = None

    def locate(self) -> str:
        if self.directory is not None and os.path.exists(os.path.join(self.directory, SYS_FAN_PWM)):
            return self.directory

        for candidate in sorted(glob.glob(sysfs_path("class/hwmon/hwmon*"))):
            if read_text(os.path.join(candidate, "name")) != self.hwmon_name:
                continue
            if not os.path.exists(os.path.join(candidate, SYS_FAN_PWM)):
                continue
            LOG.info("system fan control found at %s", candidate)
            self.directory = candidate
            return candidate

        self.directory = None
        raise RuntimeError(
            f"no hwmon device named {self.hwmon_name!r} with {SYS_FAN_PWM}; "
            "load the zimacube_ec_fan kernel module"
        )

    def _write(self, attribute: str, value: int) -> None:
        path = os.path.join(self.locate(), attribute)
        if self.dry_run:
            LOG.info("dry-run: would write %d to %s", value, path)
            return
        write_attribute(path, value)

    def read_enable(self) -> int | None:
        """Current control mode: 0 full speed, 1 manual, 2 the EC's own curve."""
        try:
            directory = self.locate()
        except RuntimeError:
            return None
        return read_int(os.path.join(directory, SYS_FAN_ENABLE))

    def read_percent(self) -> int | None:
        try:
            directory = self.locate()
        except RuntimeError:
            return None
        raw = read_int(os.path.join(directory, SYS_FAN_PWM))
        return None if raw is None else round(raw * 100 / PWM_RAW_MAX)

    def set_percent(self, percent: int) -> None:
        raw = round(clamp(percent, 0, 100) * PWM_RAW_MAX / 100)
        # The driver switches the channel to manual as part of this write.
        self._write(SYS_FAN_PWM, raw)

    def set_auto(self) -> None:
        self._write(SYS_FAN_ENABLE, PWM_ENABLE_AUTO)


class SystemFanDaemon:
    def __init__(
        self,
        fan: SystemFan,
        discover: Callable[[], list[TemperatureSensor]],
        interval: float,
        min_pwm: int,
        max_pwm: int,
        hysteresis: int,
        down_step: int,
    ) -> None:
        self.fan = fan
        self.discover = discover
        self.interval = interval
        self.min_pwm = min_pwm
        self.max_pwm = max_pwm
        self.hysteresis = hysteresis
        self.down_step = down_step
        self.running = True
        self.sensors: list[TemperatureSensor] = []
        self.current: int | None = None
        self.automatic = False

    def stop(self, _signum: int, _frame: object) -> None:
        self.running = False

    def adopt_fan_state(self) -> None:
        """Read what the fan is doing before deciding anything.

        Two things are needed. The duty, so the first step is a smooth
        correction rather than a jump — kept as read rather than clamped into
        the configured range, because this field means what the fan is doing,
        not what it is allowed to do. And the control mode, because a daemon
        that stops hands the channel back to the EC curve on the way out: a
        replacement therefore usually finds it there and has to take it over
        even when the duty it reads already matches its target.
        """
        self.automatic = self.fan.read_enable() == PWM_ENABLE_AUTO
        percent = self.fan.read_percent()
        if percent is not None:
            self.current = percent
            LOG.info(
                "system fan currently at %d%%, %s",
                percent,
                "on the EC curve" if self.automatic else "under manual control",
            )

    def measure(self) -> float | None:
        if not self.sensors:
            self.sensors = self.discover()

        pressure: float | None = None
        failed = False
        for sensor in self.sensors:
            temperature = sensor.read()
            if temperature is None:
                LOG.warning("%s stopped reporting, excluding it from this round", sensor.identity)
                failed = True
                continue
            value = sensor.pressure(temperature)
            LOG.debug("%s: %.1f C, pressure %.2f", sensor.identity, temperature, value)
            pressure = value if pressure is None else max(pressure, value)

        if failed:
            # Rebuild the list next round; the device may have been rebound.
            self.sensors = []
        return pressure

    def apply(self, target: int) -> None:
        target = int(clamp(target, self.min_pwm, self.max_pwm))
        current = self.current

        if current is None:
            speed = target
        elif target > current:
            # Heat is answered at once.
            speed = target if target - current >= self.hysteresis else current
        elif current - target >= self.hysteresis:
            # Cooling down is rate limited so the fan does not pump around a
            # threshold when a temperature hovers on it.
            speed = max(target, current - self.down_step)
        else:
            speed = current

        # The range binds the value written, so a fan found outside it is
        # brought back to the nearest edge instead of being left there.
        speed = int(clamp(speed, self.min_pwm, self.max_pwm))

        if speed == current and not self.automatic:
            return

        self.fan.set_percent(speed)
        LOG.info("system fan set to %d%% (target %d%%)", speed, target)
        self.current = speed
        self.automatic = False

    def release(self, reason: str, level: int = logging.WARNING) -> None:
        if self.automatic:
            return
        LOG.log(level, "handing the system fan back to EC auto mode: %s", reason)
        self.fan.set_auto()
        self.automatic = True
        self.current = None

    def update(self) -> int | None:
        pressure = self.measure()
        if pressure is None:
            self.release("no enabled temperature source is readable")
            return None
        self.apply(round(self.min_pwm + pressure * (self.max_pwm - self.min_pwm)))
        return self.current

    def run(self, once: bool = False) -> None:
        while self.running:
            try:
                self.update()
            except RuntimeError as error:
                # Typically the module is not loaded yet; keep waiting for it.
                LOG.error("%s", error)
            except Exception:
                LOG.exception("system fan update failed")
            if once:
                return
            end = time.monotonic() + self.interval
            while self.running and time.monotonic() < end:
                time.sleep(min(0.5, end - time.monotonic()))


# ------------------------------------------------------------------ command line


def pci_id_pair(text: str) -> tuple[int, int]:
    parts = text.lower().split(":")
    if len(parts) != 2:
        raise argparse.ArgumentTypeError(f"{text!r} is not a PCI id such as 1d6a:04c0")
    try:
        return int(parts[0], 16), int(parts[1], 16)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a PCI id such as 1d6a:04c0") from None


def pci_address(text: str) -> str:
    try:
        return normalise_bdf(text)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from None


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--10g-nic", dest="nic", action="store_true", help="use the 10G NIC temperature")
    parser.add_argument("--bay7-nvme", dest="nvme", action="store_true", help="use the Drive Bay 7 NVMe temperatures")
    parser.add_argument("--10g-nic-low", dest="nic_low", type=float, default=70, help="NIC temperature at minimum speed (default: 70)")
    parser.add_argument("--10g-nic-high", dest="nic_high", type=float, default=90, help="NIC temperature at maximum speed (default: 90)")
    parser.add_argument("--nvme-low", type=float, default=45, help="NVMe temperature at minimum speed (default: 45)")
    parser.add_argument("--nvme-high", type=float, default=70, help="NVMe temperature at maximum speed (default: 70)")
    parser.add_argument("--min-pwm", type=int, default=40, help="speed at zero thermal pressure (default: 40)")
    parser.add_argument("--max-pwm", type=int, default=100, help="speed at full thermal pressure (default: 100)")
    parser.add_argument("--interval", type=float, default=10, help="poll interval in seconds (default: 10)")
    parser.add_argument("--hysteresis", type=int, default=3, help="ignore changes smaller than this many percent (default: 3)")
    parser.add_argument("--down-step", type=int, default=5, help="largest speed reduction per interval in percent (default: 5)")
    parser.add_argument("--10g-nic-pci", dest="nic_pci", type=pci_address, help="pin the NIC to this PCI address instead of matching identities")
    parser.add_argument("--10g-nic-id", dest="nic_ids", type=pci_id_pair, action="append", default=[], metavar="VENDOR:DEVICE", help="treat this PCI identity as a 10G NIC; repeatable")
    parser.add_argument("--bay7-pci-root", type=pci_address, help="PCI address of the bridge or switch that Drive Bay 7 hangs off")
    parser.add_argument("--hwmon-name", default=EC_HWMON_NAME, help=f"hwmon device of the EC driver (default: {EC_HWMON_NAME})")
    parser.add_argument("--list-hardware", action="store_true", help="show what would be used and exit")
    parser.add_argument("--once", action="store_true", help="perform one update and exit")
    parser.add_argument("--dry-run", action="store_true", help="log the desired speed without changing it")
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args(argv)


def build_discovery(args: argparse.Namespace) -> Callable[[], list[TemperatureSensor]]:
    # Discovery runs again whenever a sensor drops out, so what it finds is only
    # reported when it differs from the previous round.
    reported: list[list[str] | None] = [None]

    def discover() -> list[TemperatureSensor]:
        sensors: list[TemperatureSensor] = []
        hints: list[str] = []

        if args.nic:
            devices = find_ten_gigabit_nics(args.nic_ids, args.nic_pci)
            if not devices:
                hints.append("no 10G NIC found: pin one with --10g-nic-pci, or add its identity with --10g-nic-id")
            sensors += nic_sensors(devices, args.nic_low, args.nic_high)

        if args.nvme:
            root, devices = find_bay7_nvme(args.bay7_pci_root)
            if root is None:
                hints.append("cannot tell which PCIe switch carries Drive Bay 7: name it with --bay7-pci-root")
            else:
                LOG.debug("Drive Bay 7 root %s carries %s", root, ", ".join(devices) or "nothing")
            sensors += nvme_sensors(devices, args.nvme_low, args.nvme_high)

        identities = [sensor.identity for sensor in sensors]
        if identities != reported[0]:
            reported[0] = identities
            for sensor in sensors:
                LOG.info("using %s (%s), %.0f-%.0f C", sensor.identity, sensor.detail, sensor.low, sensor.high)
            for hint in hints:
                LOG.warning("%s", hint)
            if not sensors:
                LOG.warning("no usable temperature source; --list-hardware shows what was found")
        return sensors

    return discover


def list_hardware(args: argparse.Namespace) -> None:
    fan = SystemFan(args.hwmon_name)
    try:
        print(f"system fan control: {fan.locate()}/{SYS_FAN_PWM}")
    except RuntimeError as error:
        print(f"system fan control: unavailable ({error})")

    print("10G NIC candidates:")
    for device in find_ten_gigabit_nics(args.nic_ids, args.nic_pci):
        vendor, product = pci_identity(device) or (0, 0)
        inputs = [path for directory in hwmon_directories(device) for path in temperature_inputs(directory)]
        detail = describe_inputs(inputs) if inputs else "no hwmon temperature"
        print(f"  {device} [{vendor:04x}:{product:04x}] {detail}")

    root, devices = find_bay7_nvme(args.bay7_pci_root)
    print(f"Drive Bay 7 root: {root or 'undetermined, pass --bay7-pci-root'}")
    for device in list_pci_devices_of_class(PCI_CLASS_NVME):
        composite = nvme_composite_input(device)
        marker = "bay 7" if device in devices else "elsewhere"
        chain = " < ".join(reversed(pci_ancestors(device))) or "root complex"
        print(f"  {device} {marker}: {'Composite' if composite else 'no Composite temperature'}; upstream {chain}")


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.list_hardware:
        list_hardware(args)
        return 0

    if not (args.nic or args.nvme):
        raise SystemExit("enable at least one source with --10g-nic or --bay7-nvme")
    if args.interval <= 0:
        raise SystemExit("--interval must be greater than zero")
    if not 0 <= args.min_pwm <= 100 or not 0 <= args.max_pwm <= 100:
        raise SystemExit("--min-pwm and --max-pwm must be between 0 and 100")
    if args.min_pwm > args.max_pwm:
        raise SystemExit("--min-pwm must not exceed --max-pwm")
    if args.hysteresis < 0:
        raise SystemExit("--hysteresis must not be negative")
    if args.down_step < 1:
        raise SystemExit("--down-step must be at least 1")
    if args.nic and args.nic_low >= args.nic_high:
        raise SystemExit("--10g-nic-low must be below --10g-nic-high")
    if args.nvme and args.nvme_low >= args.nvme_high:
        raise SystemExit("--nvme-low must be below --nvme-high")

    fan = SystemFan(args.hwmon_name, dry_run=args.dry_run)
    daemon = SystemFanDaemon(
        fan=fan,
        discover=build_discovery(args),
        interval=args.interval,
        min_pwm=args.min_pwm,
        max_pwm=args.max_pwm,
        hysteresis=args.hysteresis,
        down_step=args.down_step,
    )
    signal.signal(signal.SIGTERM, daemon.stop)
    signal.signal(signal.SIGINT, daemon.stop)

    daemon.adopt_fan_state()
    try:
        daemon.run(once=args.once)
    finally:
        try:
            daemon.release("daemon stopping", logging.INFO)
        except Exception:
            LOG.exception("cannot return the system fan to EC auto mode")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

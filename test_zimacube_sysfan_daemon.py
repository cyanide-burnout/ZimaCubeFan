import os
import tempfile
import unittest
from unittest.mock import patch

import zimacube_sysfan_daemon as sysfan


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="ascii") as handle:
        handle.write(f"{text}\n")


class FakeSysfs:
    """A synthetic /sys holding the parts of a ZimaCube the daemon looks at."""

    def __init__(self, root):
        self.root = root
        os.makedirs(os.path.join(root, "bus/pci/devices"))
        os.makedirs(os.path.join(root, "class/hwmon"))

    def device_directory(self, address, parents=()):
        link = os.path.join(self.root, "bus/pci/devices", address)
        if os.path.lexists(link):
            return os.path.realpath(link)
        path = os.path.join(self.root, "devices", "pci0000:00", *parents, address)
        os.makedirs(path, exist_ok=True)
        os.symlink(path, link)
        return path

    def add_pci_device(self, address, klass, vendor=0x0000, device=0x0000, parents=()):
        path = self.device_directory(address, parents)
        write(os.path.join(path, "class"), f"0x{klass:04x}00")
        write(os.path.join(path, "vendor"), f"0x{vendor:04x}")
        write(os.path.join(path, "device"), f"0x{device:04x}")
        return path

    def add_bridge(self, address, parents=()):
        return self.add_pci_device(address, 0x0604, parents=parents)

    def add_nic_hwmon(self, address, temperatures, labels=(), name="enp95s0"):
        path = os.path.join(self.device_directory(address), "hwmon", "hwmon7")
        write(os.path.join(path, "name"), name)
        for index, value in enumerate(temperatures, start=1):
            write(os.path.join(path, f"temp{index}_input"), value)
        for index, label in enumerate(labels, start=1):
            write(os.path.join(path, f"temp{index}_label"), label)
        return path

    def add_nvme_hwmon(self, address, controller, composite, extra=()):
        path = os.path.join(self.device_directory(address), "nvme", controller, "hwmon9")
        write(os.path.join(path, "temp1_input"), composite)
        write(os.path.join(path, "temp1_label"), "Composite")
        for index, value in enumerate(extra, start=2):
            write(os.path.join(path, f"temp{index}_input"), value)
            write(os.path.join(path, f"temp{index}_label"), f"Sensor {index - 1}")
        return path

    def add_ec_hwmon(self, name="zimacube_ec", pwm2=153):
        path = os.path.join(self.root, "class/hwmon", "hwmon3")
        write(os.path.join(path, "name"), name)
        write(os.path.join(path, "pwm1"), 120)
        write(os.path.join(path, "pwm1_enable"), 2)
        write(os.path.join(path, "pwm2"), pwm2)
        write(os.path.join(path, "pwm2_enable"), 2)
        return path


class SysfsTestCase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.sysfs = FakeSysfs(self.directory.name)
        patcher = patch.object(sysfan, "SYSFS", self.directory.name)
        patcher.start()
        self.addCleanup(patcher.stop)


class PressureTests(unittest.TestCase):
    def sensor(self, low=40, high=70):
        return sysfan.TemperatureSensor("bay7-nvme", "0000:04:00.0", "Composite", (), low, high)

    def test_pressure_is_zero_below_the_low_threshold(self):
        self.assertEqual(self.sensor().pressure(30), 0.0)

    def test_pressure_is_one_above_the_high_threshold(self):
        self.assertEqual(self.sensor().pressure(90), 1.0)

    def test_pressure_is_linear_between_the_thresholds(self):
        self.assertAlmostEqual(self.sensor().pressure(55), 0.5)


class TopologyTests(SysfsTestCase):
    def build_bay7(self):
        # Root port -> switch upstream port -> four downstream ports -> NVMe.
        self.sysfs.add_bridge("0000:00:1c.0")
        self.sysfs.add_bridge("0000:01:00.0", parents=("0000:00:1c.0",))
        for index in range(4):
            downstream = f"0000:02:0{index}.0"
            parents = ("0000:00:1c.0", "0000:01:00.0")
            self.sysfs.add_bridge(downstream, parents=parents)
            self.sysfs.add_pci_device(
                f"0000:0{3 + index}:00.0",
                sysfan.PCI_CLASS_NVME,
                parents=parents + (downstream,),
            )

    def test_bay7_root_is_the_switch_not_the_root_port(self):
        self.build_bay7()
        root, devices = sysfan.find_bay7_nvme()
        self.assertEqual(root, "0000:01:00.0")
        self.assertEqual(len(devices), 4)

    def test_nvme_outside_the_switch_is_not_part_of_bay7(self):
        self.build_bay7()
        self.sysfs.add_bridge("0000:00:06.0")
        self.sysfs.add_pci_device("0000:08:00.0", sysfan.PCI_CLASS_NVME, parents=("0000:00:06.0",))
        _root, devices = sysfan.find_bay7_nvme()
        self.assertNotIn("0000:08:00.0", devices)

    def test_pinned_root_overrides_detection(self):
        self.build_bay7()
        root, devices = sysfan.find_bay7_nvme("0000:02:00.0")
        self.assertEqual(root, "0000:02:00.0")
        self.assertEqual(devices, ["0000:03:00.0"])

    def test_the_topology_of_a_zimacube_pro_2(self):
        """The real tree of a ZimaCube Pro 2, from lspci -tn.

        Drive Bay 7 is an ASMedia ASM2824 packet switch behind root port
        00:06.0, with four downstream ports and two of the four slots filled.
        The boot SSD at 5b:00.0 hangs off its own root port and must not be
        counted as part of the bay.
        """
        self.sysfs.add_bridge("0000:00:06.0")
        self.sysfs.add_bridge("0000:01:00.0", parents=("0000:00:06.0",))
        switch = ("0000:00:06.0", "0000:01:00.0")
        for port, drive in (("0000:02:00.0", "0000:03:00.0"), ("0000:02:04.0", "0000:04:00.0")):
            self.sysfs.add_bridge(port, parents=switch)
            self.sysfs.add_pci_device(drive, sysfan.PCI_CLASS_NVME, parents=switch + (port,))
        for empty in ("0000:02:08.0", "0000:02:0c.0"):
            self.sysfs.add_bridge(empty, parents=switch)

        self.sysfs.add_bridge("0000:00:1c.0")
        self.sysfs.add_pci_device("0000:5b:00.0", sysfan.PCI_CLASS_NVME, parents=("0000:00:1c.0",))

        root, devices = sysfan.find_bay7_nvme()
        self.assertEqual(root, "0000:01:00.0")
        self.assertEqual(devices, ["0000:03:00.0", "0000:04:00.0"])

    def test_only_the_ten_gigabit_card_is_picked_from_three_nics(self):
        """Two Intel I226-LM at 2.5G alongside the Aquantia AQC113."""
        self.sysfs.add_bridge("0000:00:1c.6")
        self.sysfs.add_bridge("0000:00:1c.7")
        self.sysfs.add_bridge("0000:00:1d.0")
        self.sysfs.add_pci_device("0000:5d:00.0", sysfan.PCI_CLASS_ETHERNET, 0x8086, 0x125B, parents=("0000:00:1c.6",))
        self.sysfs.add_pci_device("0000:5e:00.0", sysfan.PCI_CLASS_ETHERNET, 0x8086, 0x125B, parents=("0000:00:1c.7",))
        self.sysfs.add_pci_device("0000:5f:00.0", sysfan.PCI_CLASS_ETHERNET, 0x1D6A, 0x04C0, parents=("0000:00:1d.0",))
        self.assertEqual(sysfan.find_ten_gigabit_nics(), ["0000:5f:00.0"])

    def test_a_single_nvme_leaves_the_bay_undetermined(self):
        self.sysfs.add_bridge("0000:00:06.0")
        self.sysfs.add_pci_device("0000:08:00.0", sysfan.PCI_CLASS_NVME, parents=("0000:00:06.0",))
        root, devices = sysfan.find_bay7_nvme()
        self.assertIsNone(root)
        self.assertEqual(devices, [])


class DiscoveryTests(SysfsTestCase):
    def test_nvme_sensor_uses_only_the_composite_temperature(self):
        self.sysfs.add_pci_device("0000:03:00.0", sysfan.PCI_CLASS_NVME)
        self.sysfs.add_nvme_hwmon("0000:03:00.0", "nvme0", 45000, extra=(60000, 61000))
        sensors = sysfan.nvme_sensors(["0000:03:00.0"], 40, 70)
        self.assertEqual(len(sensors[0].inputs), 1)
        self.assertEqual(sensors[0].read(), 45.0)

    def test_nvme_without_composite_is_skipped(self):
        path = self.sysfs.device_directory("0000:03:00.0")
        hwmon = os.path.join(path, "nvme", "nvme0", "hwmon9")
        write(os.path.join(hwmon, "temp1_input"), 45000)
        write(os.path.join(hwmon, "temp1_label"), "Sensor 1")
        self.assertEqual(sysfan.nvme_sensors(["0000:03:00.0"], 40, 70), [])

    def test_nic_is_found_by_pci_identity(self):
        self.sysfs.add_pci_device("0000:5f:00.0", sysfan.PCI_CLASS_ETHERNET, 0x1D6A, 0x04C0)
        self.sysfs.add_pci_device("0000:5e:00.0", sysfan.PCI_CLASS_ETHERNET, 0x10EC, 0x8168)
        self.assertEqual(sysfan.find_ten_gigabit_nics(), ["0000:5f:00.0"])

    def test_unknown_nic_identity_can_be_added(self):
        self.sysfs.add_pci_device("0000:5e:00.0", sysfan.PCI_CLASS_ETHERNET, 0xABCD, 0x1234)
        self.assertEqual(sysfan.find_ten_gigabit_nics([(0xABCD, 0x1234)]), ["0000:5e:00.0"])

    def test_nic_sensor_takes_the_highest_reading(self):
        # The AQC113 exposes both readings under an hwmon named after the
        # interface, which is exactly the name the daemon must not depend on.
        self.sysfs.add_pci_device("0000:5f:00.0", sysfan.PCI_CLASS_ETHERNET, 0x1D6A, 0x04C0)
        self.sysfs.add_nic_hwmon(
            "0000:5f:00.0", (67000, 68000), labels=("PHY Temperature", "MAC Temperature")
        )
        sensors = sysfan.nic_sensors(["0000:5f:00.0"], 55, 75)
        self.assertEqual(sensors[0].read(), 68.0)
        self.assertEqual(sensors[0].detail, "temp1=PHY Temperature, temp2=MAC Temperature")

    def test_nic_hwmon_parented_to_the_net_device_is_found(self):
        self.sysfs.add_pci_device("0000:5f:00.0", sysfan.PCI_CLASS_ETHERNET, 0x1D6A, 0x04C0)
        path = self.sysfs.device_directory("0000:5f:00.0")
        write(os.path.join(path, "net", "enp95s0", "hwmon4", "temp1_input"), 57000)
        sensors = sysfan.nic_sensors(["0000:5f:00.0"], 55, 75)
        self.assertEqual(sensors[0].read(), 57.0)


class SystemFanTests(SysfsTestCase):
    def test_control_node_is_found_by_hwmon_name(self):
        write(os.path.join(self.directory.name, "class/hwmon/hwmon0/name"), "coretemp")
        expected = self.sysfs.add_ec_hwmon()
        self.assertEqual(sysfan.SystemFan().locate(), expected)

    def test_missing_driver_is_an_error(self):
        with self.assertRaises(RuntimeError):
            sysfan.SystemFan().locate()

    def test_percent_is_scaled_to_the_hwmon_range(self):
        path = self.sysfs.add_ec_hwmon()
        fan = sysfan.SystemFan()
        fan.set_percent(40)
        self.assertEqual(sysfan.read_int(os.path.join(path, "pwm2")), 102)
        fan.set_percent(100)
        self.assertEqual(sysfan.read_int(os.path.join(path, "pwm2")), 255)

    def test_auto_mode_writes_two_to_pwm2_enable(self):
        path = self.sysfs.add_ec_hwmon()
        write(os.path.join(path, "pwm2_enable"), 1)
        sysfan.SystemFan().set_auto()
        self.assertEqual(sysfan.read_int(os.path.join(path, "pwm2_enable")), 2)

    def test_the_cpu_fan_channel_is_never_written(self):
        path = self.sysfs.add_ec_hwmon()
        fan = sysfan.SystemFan()
        fan.set_percent(70)
        fan.set_auto()
        self.assertEqual(sysfan.read_int(os.path.join(path, "pwm1")), 120)
        self.assertEqual(sysfan.read_int(os.path.join(path, "pwm1_enable")), 2)

    def test_dry_run_does_not_write(self):
        path = self.sysfs.add_ec_hwmon(pwm2=153)
        sysfan.SystemFan(dry_run=True).set_percent(0)
        self.assertEqual(sysfan.read_int(os.path.join(path, "pwm2")), 153)


class RecordingFan:
    def __init__(self, percent=None):
        self.percent = percent
        self.writes = []

    def read_percent(self):
        return self.percent

    def set_percent(self, percent):
        self.writes.append(percent)
        self.percent = percent

    def set_auto(self):
        self.writes.append("auto")
        self.percent = None


class DaemonTests(unittest.TestCase):
    def sensor(self, temperature, low=40, high=70):
        class Fixed(sysfan.TemperatureSensor):
            def read(self_inner):
                return temperature() if callable(temperature) else temperature

        return Fixed("bay7-nvme", "0000:03:00.0", "Composite", (), low, high)

    def daemon(self, fan, sensors, **overrides):
        options = dict(interval=10, min_pwm=40, max_pwm=100, hysteresis=3, down_step=5)
        options.update(overrides)
        return sysfan.SystemFanDaemon(fan=fan, discover=lambda: list(sensors), **options)

    def test_pwm_is_interpolated_between_min_and_max(self):
        fan = RecordingFan()
        daemon = self.daemon(fan, [self.sensor(55)])
        self.assertEqual(daemon.update(), 70)

    def test_the_hottest_source_wins(self):
        fan = RecordingFan()
        daemon = self.daemon(fan, [self.sensor(45), self.sensor(70)])
        self.assertEqual(daemon.update(), 100)

    def test_rises_are_immediate(self):
        fan = RecordingFan()
        temperature = [40.0]
        daemon = self.daemon(fan, [self.sensor(lambda: temperature[0])])
        daemon.update()
        temperature[0] = 70.0
        self.assertEqual(daemon.update(), 100)
        self.assertEqual(fan.writes, [40, 100])

    def test_falls_are_rate_limited(self):
        fan = RecordingFan()
        temperature = [70.0]
        daemon = self.daemon(fan, [self.sensor(lambda: temperature[0])])
        self.assertEqual(daemon.update(), 100)
        temperature[0] = 40.0
        self.assertEqual(daemon.update(), 95)
        self.assertEqual(daemon.update(), 90)

    def test_small_swings_do_not_move_the_fan(self):
        fan = RecordingFan()
        temperature = [55.0]
        daemon = self.daemon(fan, [self.sensor(lambda: temperature[0])])
        self.assertEqual(daemon.update(), 70)
        temperature[0] = 55.5
        self.assertEqual(daemon.update(), 70)
        temperature[0] = 54.5
        self.assertEqual(daemon.update(), 70)
        self.assertEqual(fan.writes, [70])

    def test_one_dead_sensor_leaves_the_others_in_charge(self):
        fan = RecordingFan()
        daemon = self.daemon(fan, [self.sensor(None), self.sensor(55)])
        self.assertEqual(daemon.update(), 70)

    def test_losing_every_sensor_returns_the_fan_to_auto(self):
        fan = RecordingFan()
        temperature = [55.0]
        daemon = self.daemon(fan, [self.sensor(lambda: temperature[0])])
        daemon.update()
        temperature[0] = None
        self.assertIsNone(daemon.update())
        self.assertEqual(fan.writes, [70, "auto"])

    def test_auto_mode_is_entered_only_once(self):
        fan = RecordingFan()
        daemon = self.daemon(fan, [])
        daemon.update()
        daemon.update()
        self.assertEqual(fan.writes, ["auto"])

    def test_control_resumes_after_auto_mode(self):
        fan = RecordingFan()
        temperature = [None]
        daemon = self.daemon(fan, [self.sensor(lambda: temperature[0])])
        daemon.update()
        temperature[0] = 40.0
        self.assertEqual(daemon.update(), 40)
        self.assertEqual(fan.writes, ["auto", 40])

    def test_a_failed_sensor_triggers_rediscovery(self):
        fan = RecordingFan()
        rounds = []

        def discover():
            rounds.append(1)
            return [self.sensor(None)]

        daemon = sysfan.SystemFanDaemon(fan, discover, 10, 40, 100, 3, 5)
        daemon.update()
        daemon.update()
        self.assertEqual(len(rounds), 2)

    def test_a_pinned_speed_is_written_even_when_already_close(self):
        # --min-pwm N --max-pwm N pins the fan; a nearby starting speed must
        # still be corrected rather than accepted as already on target.
        fan = RecordingFan(percent=43)
        daemon = self.daemon(fan, [self.sensor(50)], min_pwm=40, max_pwm=40)
        daemon.adopt_current_speed()
        self.assertEqual(daemon.update(), 40)
        self.assertEqual(fan.writes, [40])

    def test_a_speed_above_the_maximum_comes_down_without_overshooting_it(self):
        fan = RecordingFan(percent=100)
        daemon = self.daemon(fan, [self.sensor(40)], min_pwm=40, max_pwm=80)
        daemon.adopt_current_speed()
        self.assertEqual(daemon.update(), 80)
        self.assertEqual(daemon.update(), 75)

    def test_a_speed_below_the_minimum_is_raised_at_once(self):
        fan = RecordingFan(percent=10)
        daemon = self.daemon(fan, [self.sensor(40)], min_pwm=40, max_pwm=100)
        daemon.adopt_current_speed()
        self.assertEqual(daemon.update(), 40)

    def test_the_running_speed_is_adopted_at_start(self):
        fan = RecordingFan(percent=100)
        daemon = self.daemon(fan, [self.sensor(40)])
        daemon.adopt_current_speed()
        self.assertEqual(daemon.update(), 95)


class ServiceUnitTests(unittest.TestCase):
    def test_the_unit_passes_exactly_the_built_in_defaults(self):
        """The shipped unit and the parser defaults must not drift apart."""
        unit = os.path.join(os.path.dirname(os.path.abspath(__file__)), "zimacube-sysfan.service")
        with open(unit, encoding="ascii") as handle:
            command = next(line for line in handle if line.startswith("ExecStart="))
        arguments = command.split()[1:]
        self.assertEqual(vars(sysfan.parse_args(arguments)), vars(sysfan.parse_args(["--10g-nic", "--bay7-nvme"])))


class CommandLineTests(unittest.TestCase):
    def test_source_flags_parse(self):
        args = sysfan.parse_args(["--10g-nic", "--bay7-nvme", "--10g-nic-low", "50"])
        self.assertTrue(args.nic)
        self.assertTrue(args.nvme)
        self.assertEqual(args.nic_low, 50)

    def test_a_source_must_be_selected(self):
        with self.assertRaises(SystemExit):
            sysfan.main([])

    def test_thresholds_must_be_ordered(self):
        with self.assertRaises(SystemExit):
            sysfan.main(["--bay7-nvme", "--nvme-low", "70", "--nvme-high", "40"])

    def test_short_pci_addresses_are_accepted(self):
        args = sysfan.parse_args(["--bay7-nvme", "--bay7-pci-root", "01:00.0"])
        self.assertEqual(args.bay7_pci_root, "0000:01:00.0")

    def test_pci_identities_are_hexadecimal(self):
        args = sysfan.parse_args(["--10g-nic", "--10g-nic-id", "1d6a:04c0"])
        self.assertEqual(args.nic_ids, [(0x1D6A, 0x04C0)])


if __name__ == "__main__":
    unittest.main()

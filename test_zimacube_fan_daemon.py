import ctypes
import os
import tempfile
import unittest
from unittest.mock import patch

import zimacube_fan_daemon as fan


def smart_block(**attributes: int) -> bytes:
    """A 512-byte SMART data structure carrying the given raw attributes."""
    data = bytearray(fan.SMART_DATA_LENGTH)
    for index, (identifier, raw) in enumerate(attributes.items()):
        start = fan.SMART_ATTRIBUTE_OFFSET + index * fan.SMART_ATTRIBUTE_LENGTH
        data[start] = int(identifier.lstrip("a"))
        data[start + 5] = raw
    return bytes(data)


def sysfs_with_block_devices(root: str, **devices: str) -> None:
    """Build a sysfs tree where each device links to the given device path."""
    os.makedirs(os.path.join(root, "block"))
    for name, path in devices.items():
        target = os.path.join(root, "devices", path.strip("/"), "block", name)
        os.makedirs(target)
        os.symlink(target, os.path.join(root, "block", name))


def daemon_with_temperature(**overrides):
    settings = dict(
        bus=2,
        interval=30,
        active_speed=80,
        idle_speed=40,
        cooldown=120,
        device_pattern="/dev/sd?",
        dry_run=True,
        temperature_query=lambda _device: 38,
        counters=lambda _device: (0, 0, 0, 0),
        power_query=lambda _device: 0xFF,
    )
    settings.update(overrides)
    return fan.FanDaemon(**settings)


class FanDaemonTests(unittest.TestCase):
    def test_ata_active_idle_is_active(self):
        self.assertEqual(fan.drive_state("/dev/sda", lambda _: 0xFF), "active/idle")

    def test_non_ff_ata_state_is_standby(self):
        self.assertEqual(fan.drive_state("/dev/sda", lambda _: 0x00), "standby")

    def test_failed_ata_query_is_unknown(self):
        def fail(_device):
            raise OSError(25, "Inappropriate ioctl for device")

        self.assertEqual(fan.drive_state("/dev/sda", fail), "unknown")

    @patch("zimacube_fan_daemon.glob.glob", return_value=["/dev/sda", "/dev/sdb"])
    def test_one_active_disk_is_enough(self, _glob):
        states = iter((0x00, 0xFF))
        daemon = fan.FanDaemon(
            2, 30, 80, 40, 120, "/dev/sd?", dry_run=True,
            power_query=lambda _: next(states),
        )
        self.assertEqual(daemon.update(), 80)

    def test_ata_query_uses_fallback_command(self):
        commands = []

        def ioctl(_fd, request, arguments, _mutate):
            self.assertEqual(request, fan.HDIO_DRIVE_CMD)
            commands.append(arguments[0])
            if len(commands) == 1:
                raise OSError(5, "I/O error")
            arguments[2] = 0xFF

        mode = fan.query_ata_power_mode("/dev/sda", lambda _p, _f: 7, ioctl, lambda _fd: None)
        self.assertEqual(mode, 0xFF)
        self.assertEqual(commands, [fan.ATA_CHECK_POWER_MODE, fan.ATA_CHECK_POWER_MODE_OLD])

    def test_bus_detection_probes_only_address_69(self):
        probes = []

        def probe(bus, address):
            probes.append((bus, address))
            return bus == 3

        self.assertEqual(fan.find_i2c_bus((0, 1, 2, 3, 4), probe), 3)
        self.assertEqual(probes[-1], (3, 0x69))

    def test_speed_command_has_exact_i2c_block_payload(self):
        writes = []
        fan.set_fan_speed(3, 80, lambda *args: writes.append(args))
        self.assertEqual(writes, [(3, 0x69, 0x04, bytes((1, 80, 0, 0, 0, 0, 1, 0)))])

    @patch("zimacube_fan_daemon.glob.glob", return_value=["/dev/sda"])
    def test_repeated_state_does_not_repeat_i2c_write(self, _glob):
        writes = []
        daemon = fan.FanDaemon(
            2, 30, 80, 40, 120, "/dev/sd?",
            power_query=lambda _: 0xFF,
            fan_writer=lambda *args: writes.append(args),
        )
        daemon.update()
        daemon.update()
        self.assertEqual(len(writes), 1)

    @patch("zimacube_fan_daemon.glob.glob", return_value=["/dev/sda"])
    def test_idle_speed_waits_for_two_minute_cooldown(self, _glob):
        writes = []
        now = [1000.0]
        active = [True]
        daemon = fan.FanDaemon(
            2, 30, 80, 40, 120, "/dev/sd?",
            power_query=lambda _: 0xFF if active[0] else 0x00,
            fan_writer=lambda *args: writes.append(args),
            clock=lambda: now[0],
        )

        self.assertEqual(daemon.update(), 80)
        active[0] = False
        now[0] += 119
        self.assertEqual(daemon.update(), 80)
        self.assertEqual(len(writes), 1)

        now[0] += 1
        self.assertEqual(daemon.update(), 40)
        self.assertEqual([write[3][1] for write in writes], [80, 40])

    @patch("zimacube_fan_daemon.glob.glob", return_value=["/dev/sda"])
    def test_new_activity_restarts_cooldown(self, _glob):
        now = [1000.0]
        states = iter((0xFF, 0x00, 0xFF, 0x00, 0x00))
        daemon = fan.FanDaemon(
            2, 30, 80, 40, 120, "/dev/sd?", dry_run=True,
            power_query=lambda _: next(states),
            clock=lambda: now[0],
        )

        self.assertEqual(daemon.update(), 80)
        now[0] += 100
        self.assertEqual(daemon.update(), 80)
        now[0] += 10
        self.assertEqual(daemon.update(), 80)
        now[0] += 119
        self.assertEqual(daemon.update(), 80)
        now[0] += 1
        self.assertEqual(daemon.update(), 40)

    def test_open_flags_do_not_request_disk_io(self):
        seen_flags = []

        def opener(_path, flags):
            seen_flags.append(flags)
            return 7

        def ioctl(_fd, _request, arguments, _mutate):
            arguments[2] = 0

        fan.query_ata_power_mode("/dev/sda", opener, ioctl, lambda _fd: None)
        self.assertTrue(seen_flags[0] & os.O_NONBLOCK)
        self.assertFalse(seen_flags[0] & os.O_WRONLY)


class SmartReadTests(unittest.TestCase):
    def test_command_block_matches_the_sat_pass_through_layout(self):
        self.assertEqual(
            fan.smart_read_data_cdb(),
            bytes((0x85, 0x08, 0x0E, 0x00, 0xD0, 0x00, 0x01, 0x00,
                   0x00, 0x00, 0x4F, 0x00, 0xC2, 0x00, 0xB0, 0x00)),
        )

    def test_temperature_comes_from_attribute_194(self):
        self.assertEqual(fan.parse_smart_temperature(smart_block(a194=41)), 41)

    def test_airflow_attribute_is_used_when_194_is_absent(self):
        self.assertEqual(fan.parse_smart_temperature(smart_block(a190=37)), 37)

    def test_attribute_194_is_preferred_over_190(self):
        self.assertEqual(fan.parse_smart_temperature(smart_block(a190=37, a194=41)), 41)

    def test_upper_raw_bytes_holding_minima_are_ignored(self):
        data = bytearray(smart_block(a194=41))
        start = fan.SMART_ATTRIBUTE_OFFSET
        data[start + 6 : start + 11] = bytes((0, 24, 0, 52, 0))
        self.assertEqual(fan.parse_smart_temperature(bytes(data)), 41)

    def test_implausible_reading_is_rejected(self):
        self.assertIsNone(fan.parse_smart_temperature(smart_block(a194=0)))
        self.assertIsNone(fan.parse_smart_temperature(smart_block(a194=200)))

    def test_drive_without_temperature_attributes_reports_nothing(self):
        self.assertIsNone(fan.parse_smart_temperature(smart_block(a5=0, a9=17)))

    def test_truncated_block_reports_nothing(self):
        self.assertIsNone(fan.parse_smart_temperature(b"\x00" * 64))

    def test_request_describes_a_512_byte_read_from_the_device(self):
        seen = {}

        def transfer(fd, header):
            seen["fd"] = fd
            seen["interface_id"] = header.interface_id
            seen["dxfer_direction"] = header.dxfer_direction
            seen["dxfer_len"] = header.dxfer_len
            seen["timeout"] = header.timeout
            seen["cdb"] = bytes(
                ctypes.cast(header.cmdp, ctypes.POINTER(ctypes.c_uint8))[: header.cmd_len]
            )
            block = smart_block(a194=39)
            ctypes.memmove(header.dxferp, block, len(block))

        data = fan.read_smart_data("/dev/sda", lambda _p, _f: 9, transfer, lambda _fd: None)
        self.assertEqual(seen["fd"], 9)
        self.assertEqual(seen["interface_id"], ord("S"))
        self.assertEqual(seen["dxfer_direction"], fan.SG_DXFER_FROM_DEVICE)
        self.assertEqual(seen["dxfer_len"], fan.SMART_DATA_LENGTH)
        self.assertEqual(seen["timeout"], fan.SG_TIMEOUT_MILLISECONDS)
        self.assertEqual(seen["cdb"], fan.smart_read_data_cdb())
        self.assertEqual(fan.parse_smart_temperature(data), 39)

    def test_rejected_command_raises_even_though_the_ioctl_succeeded(self):
        def refuse(_fd, header):
            header.status = 0x02

        with self.assertRaises(OSError):
            fan.read_smart_data("/dev/sda", lambda _p, _f: 9, refuse, lambda _fd: None)

    def test_device_is_opened_without_requesting_disk_io(self):
        flags = []

        def opener(_path, value):
            flags.append(value)
            return 9

        fan.read_smart_data("/dev/sda", opener, lambda _fd, _h: None, lambda _fd: None)
        self.assertTrue(flags[0] & os.O_NONBLOCK)
        self.assertFalse(flags[0] & os.O_WRONLY)

    def test_descriptor_is_closed_when_the_transfer_fails(self):
        closed = []

        def explode(_fd, _header):
            raise OSError(5, "I/O error")

        with self.assertRaises(OSError):
            fan.read_smart_data("/dev/sda", lambda _p, _f: 9, explode, closed.append)
        self.assertEqual(closed, [9])

    def test_disk_temperature_parses_what_the_reader_returned(self):
        self.assertEqual(fan.disk_temperature("/dev/sda", lambda _: smart_block(a194=44)), 44)

    def test_counters_come_from_the_kernel_not_the_drive(self):
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, "block", "sda"))
            with open(os.path.join(root, "block", "sda", "stat"), "w") as handle:
                handle.write(" 130 4 5108 61 22 0 176 12 0 84 73\n")
            with patch.object(fan, "SYSFS", root):
                self.assertEqual(fan.block_device_counters("/dev/sda"), (130, 5108, 22, 176))
                self.assertIsNone(fan.block_device_counters("/dev/sdz"))


@patch("zimacube_fan_daemon.glob.glob", return_value=["/dev/sda"])
class TemperatureControlTests(unittest.TestCase):
    def test_standby_disk_is_never_queried(self, _glob):
        queried = []
        daemon = daemon_with_temperature(
            power_query=lambda _: 0x00,
            temperature_query=lambda device: queried.append(device),
        )
        daemon.update()
        daemon.update()
        self.assertEqual(queried, [])

    def test_spinning_but_quiet_disk_is_never_queried(self, _glob):
        queried = []
        daemon = daemon_with_temperature(
            counters=lambda _: (7, 7, 7, 7),
            temperature_query=lambda device: queried.append(device),
        )
        daemon.update()
        daemon.update()
        daemon.update()
        self.assertEqual(queried, [])

    def test_disk_serving_io_is_queried(self, _glob):
        queried = []
        traffic = iter(((1, 1, 0, 0), (2, 9, 0, 0)))
        daemon = daemon_with_temperature(
            counters=lambda _: next(traffic),
            temperature_query=lambda device: queried.append(device) or 41,
        )
        daemon.update()
        daemon.update()
        self.assertEqual(queried, ["/dev/sda"])

    def test_reads_of_one_disk_are_spaced_out(self, _glob):
        now = [1000.0]
        reads = [0]
        counter = [0]

        def busy(_device):
            counter[0] += 1
            return (counter[0], 0, 0, 0)

        def read(_device):
            reads[0] += 1
            return 41

        daemon = daemon_with_temperature(
            counters=busy, temperature_query=read, clock=lambda: now[0],
        )
        for _ in range(4):
            daemon.update()
            now[0] += 30
        self.assertEqual(reads[0], 1)

        now[0] += 60
        daemon.update()
        self.assertEqual(reads[0], 2)

    def test_cool_disk_leaves_the_activity_floor_alone(self, _glob):
        counter = [0]

        def busy(_device):
            counter[0] += 1
            return (counter[0], 0, 0, 0)

        daemon = daemon_with_temperature(counters=busy, temperature_query=lambda _: 38)
        daemon.update()
        self.assertEqual(daemon.update(), 80)

    def test_hot_disk_raises_the_speed_above_the_activity_floor(self, _glob):
        counter = [0]

        def busy(_device):
            counter[0] += 1
            return (counter[0], 0, 0, 0)

        daemon = daemon_with_temperature(counters=busy, temperature_query=lambda _: 55)
        daemon.update()
        self.assertEqual(daemon.update(), 100)

    def test_speed_follows_the_curve_between_the_ends(self, _glob):
        counter = [0]

        def busy(_device):
            counter[0] += 1
            return (counter[0], 0, 0, 0)

        # 52 C is 80% of the way from 40 to 55, so 40 + 0.8 * 60 = 88.
        daemon = daemon_with_temperature(counters=busy, temperature_query=lambda _: 52)
        daemon.update()
        self.assertEqual(daemon.update(), 88)

    def test_reading_expires_once_the_disk_goes_quiet(self, _glob):
        now = [1000.0]
        counter = [0]
        spinning = [True]

        def busy(_device):
            counter[0] += 1
            return (counter[0], 0, 0, 0)

        daemon = daemon_with_temperature(
            counters=busy,
            temperature_query=lambda _: 55,
            power_query=lambda _: 0xFF if spinning[0] else 0x00,
            clock=lambda: now[0],
        )
        daemon.update()
        self.assertEqual(daemon.update(), 100)

        # Past the cooldown and past 2.5 temperature intervals, the last
        # reading no longer describes the disk and only the floor is left.
        spinning[0] = False
        counter[0] = 0
        now[0] += 400
        self.assertIsNone(daemon.hottest(now[0]))

    def test_descent_is_rate_limited(self, _glob):
        now = [1000.0]
        counter = [0]
        spinning = [True]

        def busy(_device):
            counter[0] += 1
            return (counter[0], 0, 0, 0)

        daemon = daemon_with_temperature(
            counters=busy,
            temperature_query=lambda _: 55,
            power_query=lambda _: 0xFF if spinning[0] else 0x00,
            clock=lambda: now[0],
        )
        daemon.update()
        self.assertEqual(daemon.update(), 100)

        spinning[0] = False
        now[0] += 400
        self.assertEqual(daemon.update(), 95)
        self.assertEqual(daemon.update(), 90)

    def test_unreadable_disk_is_reported_once(self, _glob):
        counter = [0]

        def busy(_device):
            counter[0] += 1
            return (counter[0], 0, 0, 0)

        def fail(_device):
            raise OSError(5, "I/O error")

        now = [1000.0]
        daemon = daemon_with_temperature(
            counters=busy, temperature_query=fail, clock=lambda: now[0],
        )
        with self.assertLogs(fan.LOG, level="DEBUG") as captured:
            for _ in range(3):
                daemon.update()
                now[0] += 130
        warnings = [line for line in captured.output if line.startswith("WARNING")]
        self.assertEqual(len(warnings), 1)

    def test_removed_disk_is_forgotten(self, glob_mock):
        counter = [0]

        def busy(_device):
            counter[0] += 1
            return (counter[0], 0, 0, 0)

        daemon = daemon_with_temperature(counters=busy, temperature_query=lambda _: 55)
        daemon.update()
        daemon.update()
        self.assertIn("/dev/sda", daemon.temperatures)

        glob_mock.return_value = []
        daemon.update()
        self.assertEqual(daemon.temperatures, {})
        self.assertEqual(daemon.last_counters, {})


@patch("zimacube_fan_daemon.glob.glob", return_value=["/dev/sda"])
class FaultTests(unittest.TestCase):
    def test_device_that_never_answers_is_not_an_ata_disk(self, _glob):
        def fail(_device):
            raise OSError(25, "Inappropriate ioctl for device")

        daemon = fan.FanDaemon(
            2, 30, 80, 40, 120, "/dev/sd?", dry_run=True, power_query=fail,
            classifier=lambda _: False,
        )
        self.assertEqual(daemon.update(), 40)
        self.assertEqual(daemon.update(), 40)

    def test_disk_that_stops_answering_holds_the_fan_up(self, _glob):
        now = [1000.0]
        healthy = [True]

        def query(_device):
            if healthy[0]:
                return 0xFF
            raise OSError(5, "I/O error")

        daemon = fan.FanDaemon(
            2, 30, 80, 40, 120, "/dev/sd?", dry_run=True,
            power_query=query, clock=lambda: now[0],
        )
        self.assertEqual(daemon.update(), 80)

        # Well past the cooldown, so a disk merely gone to standby would have
        # dropped the fan to 40% by now.
        healthy[0] = False
        now[0] += 600
        self.assertEqual(daemon.update(), 80)
        now[0] += 600
        self.assertEqual(daemon.update(), 80)

        healthy[0] = True
        self.assertEqual(daemon.update(), 80)

    def test_faulted_disk_is_never_sent_a_smart_command(self, _glob):
        healthy = [True]
        queried = []

        def query(_device):
            if healthy[0]:
                return 0xFF
            raise OSError(5, "I/O error")

        counter = [0]

        def busy(_device):
            counter[0] += 1
            return (counter[0], 0, 0, 0)

        daemon = fan.FanDaemon(
            2, 30, 80, 40, 120, "/dev/sd?", dry_run=True,
            power_query=query, counters=busy,
            temperature_query=lambda device: queried.append(device) or 41,
        )
        daemon.update()
        daemon.update()
        self.assertEqual(queried, ["/dev/sda"])

        healthy[0] = False
        daemon.update()
        daemon.update()
        self.assertEqual(queried, ["/dev/sda"])

    def test_unplugged_disk_is_forgotten(self, glob_mock):
        daemon = fan.FanDaemon(
            2, 30, 80, 40, 120, "/dev/sd?", dry_run=True, power_query=lambda _: 0xFF,
        )
        daemon.update()
        self.assertEqual(daemon.answered, {"/dev/sda"})

        glob_mock.return_value = []
        daemon.update()
        self.assertEqual(daemon.answered, set())


class DiskIdentityTests(unittest.TestCase):
    def test_disk_on_an_ata_port_is_a_disk(self):
        with tempfile.TemporaryDirectory() as root:
            sysfs_with_block_devices(
                root,
                sda="pci0000:00/0000:00:17.0/ata1/host0/target0:0:0/0:0:0:0",
                sdf="pci0000:00/0000:00:14.0/usb1/1-2/1-2:1.0/host6/target6:0:0:0/6:0:0:0",
            )
            with patch.object(fan, "SYSFS", root):
                self.assertTrue(fan.is_ata_disk("/dev/sda"))
                self.assertFalse(fan.is_ata_disk("/dev/sdf"))
                self.assertFalse(fan.is_ata_disk("/dev/sdz"))

    @patch("zimacube_fan_daemon.glob.glob", return_value=["/dev/sda"])
    def test_disk_broken_before_the_daemon_started_holds_the_fan_up(self, _glob):
        def fail(_device):
            raise OSError(5, "I/O error")

        daemon = fan.FanDaemon(
            2, 30, 80, 40, 120, "/dev/sd?", dry_run=True,
            power_query=fail, classifier=lambda _: True,
        )
        # Nothing has ever answered, so only the topology says this is a disk.
        self.assertEqual(daemon.update(), 80)
        self.assertEqual(daemon.answered, set())

    @patch("zimacube_fan_daemon.glob.glob", return_value=["/dev/sdf"])
    def test_usb_device_is_asked_once_and_then_left_alone(self, _glob):
        asked = []

        def fail(device):
            asked.append(device)
            raise OSError(25, "Inappropriate ioctl for device")

        daemon = fan.FanDaemon(
            2, 30, 80, 40, 120, "/dev/sd?", dry_run=True,
            power_query=fail, classifier=lambda _: False,
        )
        with self.assertLogs(fan.LOG, level="INFO") as captured:
            for _ in range(4):
                self.assertEqual(daemon.update(), 40)

        # One ATA command in total, and one line about it rather than one
        # warning per interval.
        self.assertEqual(asked, ["/dev/sdf"])
        ignored = [line for line in captured.output if "ignoring" in line]
        self.assertEqual(len(ignored), 1)

    @patch("zimacube_fan_daemon.glob.glob", return_value=["/dev/sdf"])
    def test_node_taken_over_by_a_disk_stops_being_ignored(self, _glob):
        disk = [False]
        failing = [True]

        def query(_device):
            if failing[0]:
                raise OSError(25, "Inappropriate ioctl for device")
            return 0xFF

        daemon = fan.FanDaemon(
            2, 30, 80, 40, 120, "/dev/sd?", dry_run=True,
            power_query=query, classifier=lambda _: disk[0],
        )
        self.assertEqual(daemon.update(), 40)
        self.assertEqual(list(daemon.ignored), ["/dev/sdf"])

        disk[0] = True
        failing[0] = False
        self.assertEqual(daemon.update(), 80)
        self.assertEqual(daemon.ignored, {})

    @patch("zimacube_fan_daemon.glob.glob")
    def test_reappearing_disk_does_not_need_its_history(self, glob_mock):
        failing = [False]

        def query(_device):
            if failing[0]:
                raise OSError(5, "I/O error")
            return 0xFF

        daemon = fan.FanDaemon(
            2, 30, 80, 40, 120, "/dev/sd?", dry_run=True,
            power_query=query, classifier=lambda _: True,
        )
        glob_mock.return_value = ["/dev/sda"]
        daemon.update()

        # Unplugged, which erases what was learned about it, then plugged back
        # in with the controller now broken.
        glob_mock.return_value = []
        daemon.update()
        self.assertEqual(daemon.answered, set())

        glob_mock.return_value = ["/dev/sda"]
        failing[0] = True
        self.assertEqual(daemon.update(), 80)

    @patch("zimacube_fan_daemon.glob.glob", return_value=["/dev/sda"])
    def test_answer_overrides_an_unfamiliar_topology(self, _glob):
        failing = [False]

        def query(_device):
            if failing[0]:
                raise OSError(5, "I/O error")
            return 0xFF

        daemon = fan.FanDaemon(
            2, 30, 80, 40, 120, "/dev/sd?", dry_run=True,
            power_query=query, classifier=lambda _: False,
        )
        self.assertEqual(daemon.update(), 80)
        failing[0] = True
        self.assertEqual(daemon.update(), 80)


@patch("zimacube_fan_daemon.glob.glob", return_value=["/dev/sda"])
class TransientFailureTests(unittest.TestCase):
    def daemon(self, query, now):
        return fan.FanDaemon(
            2, 30, 80, 40, 120, "/dev/sd?", dry_run=True,
            power_query=query, classifier=lambda _: False, clock=lambda: now[0],
        )

    def test_disk_failing_only_on_the_first_poll_is_not_lost(self, _glob):
        now = [1000.0]
        attempts = [0]

        def query(_device):
            attempts[0] += 1
            if attempts[0] == 1:
                raise OSError(5, "I/O error")
            return 0xFF

        daemon = self.daemon(query, now)
        # An unfamiliar topology plus one bad answer sets it aside...
        self.assertEqual(daemon.update(), 40)
        self.assertEqual(list(daemon.ignored), ["/dev/sda"])

        # ...but not for good: the retry finds a working disk.
        now[0] += fan.IGNORED_RETRY_SECONDS
        self.assertEqual(daemon.update(), 80)
        self.assertEqual(daemon.ignored, {})
        self.assertEqual(daemon.answered, {"/dev/sda"})

    def test_recovered_disk_stays_a_disk(self, _glob):
        now = [1000.0]
        failing = [True]

        def query(_device):
            if failing[0]:
                raise OSError(5, "I/O error")
            return 0xFF

        daemon = self.daemon(query, now)
        daemon.update()
        now[0] += fan.IGNORED_RETRY_SECONDS
        failing[0] = False
        self.assertEqual(daemon.update(), 80)

        # Having answered once, a later failure is a fault and holds the speed
        # rather than sending it back to the ignored list.
        failing[0] = True
        now[0] += 1000
        self.assertEqual(daemon.update(), 80)
        self.assertEqual(daemon.ignored, {})

    def test_retry_is_not_attempted_every_poll(self, _glob):
        now = [1000.0]
        attempts = []

        def query(device):
            attempts.append(device)
            raise OSError(25, "Inappropriate ioctl for device")

        daemon = self.daemon(query, now)
        for _ in range(6):
            daemon.update()
            now[0] += 30
        self.assertEqual(len(attempts), 1)

        now[0] += fan.IGNORED_RETRY_SECONDS
        daemon.update()
        self.assertEqual(len(attempts), 2)

    def test_retry_failures_stay_out_of_the_journal(self, _glob):
        now = [1000.0]

        def query(_device):
            raise OSError(25, "Inappropriate ioctl for device")

        daemon = self.daemon(query, now)
        with self.assertLogs(fan.LOG, level="INFO") as captured:
            for _ in range(4):
                daemon.update()
                now[0] += fan.IGNORED_RETRY_SECONDS

        # One "ignoring" line, and no repeat of the power-state warning.
        self.assertEqual(len([l for l in captured.output if "ignoring" in l]), 1)
        self.assertEqual(len([l for l in captured.output if "cannot read power state" in l]), 1)


class SpeedOrderingTests(unittest.TestCase):
    def run_main(self, *arguments):
        return fan.main(["--dry-run", "--once", *arguments])

    def test_idle_speed_above_active_speed_is_refused(self):
        with self.assertRaises(SystemExit):
            self.run_main("--idle-speed", "90", "--active-speed", "60")

    def test_active_speed_above_maximum_is_refused(self):
        with self.assertRaises(SystemExit):
            self.run_main("--active-speed", "90", "--max-speed", "80")

    def test_idle_speed_above_maximum_is_refused(self):
        with self.assertRaises(SystemExit):
            self.run_main("--idle-speed", "90", "--max-speed", "80")

    @patch("zimacube_fan_daemon.glob.glob", return_value=[])
    def test_a_consistent_order_is_accepted(self, _glob):
        self.assertEqual(self.run_main("--idle-speed", "40", "--active-speed", "60"), 0)


if __name__ == "__main__":
    unittest.main()

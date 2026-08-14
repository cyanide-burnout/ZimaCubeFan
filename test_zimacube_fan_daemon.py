import os
import unittest
from unittest.mock import patch

import zimacube_fan_daemon as fan


class FanDaemonTests(unittest.TestCase):
    def test_ata_active_idle_is_active(self):
        self.assertEqual(fan.drive_state("/dev/sda", lambda _: 0xFF), "active/idle")

    def test_non_ff_ata_state_is_standby(self):
        self.assertEqual(fan.drive_state("/dev/sda", lambda _: 0x00), "standby")

    def test_failed_ata_query_is_unknown(self):
        def fail(_device):
            raise OSError(25, "Inappropriate ioctl for device")

        self.assertEqual(fan.drive_state("/dev/sda", fail), "unknown")

    def test_one_active_disk_is_enough(self):
        states = iter((0x00, 0xFF))
        self.assertTrue(fan.any_drive_active(["/dev/sda", "/dev/sdb"], lambda _: next(states)))

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


if __name__ == "__main__":
    unittest.main()

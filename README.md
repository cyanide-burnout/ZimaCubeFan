# ZimaCube 2 fan daemon

## Why this project exists

ZimaCube 2 uses a custom controller for the disk-cage fans. These fans cannot
be configured through the BIOS and are not supported by standard Linux fan
control tools. The stock ZimaOS installation presumably manages them, but
ZimaOS is not always the preferred choice for users who want a conventional,
fully customizable Linux system. This project was created for a ZimaCube 2
running Debian.

The manufacturer published information in its Discord community explaining
how the fan speed can be controlled with a specific sequence of I2C commands.
The scripts commonly shared there set the fan to a fixed speed—usually 60%—or
to another manually selected value. They do not adapt the cooling to disk
activity.

This project makes the fan control dynamic:

- when at least one disk is active, the fan runs at 80% to provide stronger
  airflow through the disk cage;
- when all disks are inactive, the fan runs at 40% to maintain a minimum
  continuous airflow;
- after the last disk becomes inactive, the fan remains at 80% for another
  two minutes to remove residual heat before dropping to 40%; any new disk
  activity restarts this cooldown.

Instead of using a fixed compromise such as 60%, the daemon therefore provides
quiet baseline cooling while the disks are idle and more effective cooling
under load.

## How it works

Every 30 seconds, the daemon checks `/dev/sd?` using the Linux
`HDIO_DRIVE_CMD` ioctl. If at least one disk returns the ATA `active/idle`
state, the daemon selects 80%. Any other state, or an empty device list, is
treated as inactive.

Fan control is performed directly through the Linux SMBus ioctl on
`/dev/i2c-N`. The daemon locates the controller at address `0x69` and sends a
command only when the desired fan speed changes.

No external utilities such as `hdparm`, `i2cdetect`, `i2cset`, or `smartctl`
are invoked. Python 3 is the only userspace dependency; the kernel `i2c-dev`
module must be loaded to provide I2C access.

## Installation

Run from the project directory on the ZimaCube:

```bash
sudo ./install.sh
```

The installer:

- verifies that Python 3 is available;
- installs the daemon as `/usr/local/sbin/zimacube-fan`;
- installs and enables `zimacube-fan.service`;
- configures the `i2c-dev` module to load automatically;
- restarts the service and displays its status.

## Checking the service

```bash
systemctl status zimacube-fan.service
journalctl -u zimacube-fan.service -f
```

To test the decision logic once without detecting the controller or writing to
I2C:

```bash
sudo /usr/local/sbin/zimacube-fan --once --dry-run --verbose
```

To perform one real hardware update:

```bash
sudo /usr/local/sbin/zimacube-fan --once --verbose
```

## Default settings

```text
Polling interval:       30 seconds
Active fan speed:       80%
Inactive fan speed:     40%
Cooldown before 40%:    120 seconds
Controller address:     0x69
Disk device pattern:    /dev/sd?
```

The values can be changed with `--interval`, `--active-speed`, `--idle-speed`,
`--cooldown`, `--bus`, and `--devices`.

The service runs as root because ATA commands and direct I2C access require
`CAP_SYS_RAWIO`.

## License

This project is released under the [MIT License](LICENSE).

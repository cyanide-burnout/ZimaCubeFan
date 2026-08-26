# ZimaCube 2 fan daemons

Two independent userspace daemons for a ZimaCube 2 running a conventional
Linux distribution:

- `zimacube-fan` drives the disk-cage fan from disk activity, over I2C;
- `zimacube-sysfan` drives the system fan from the 10G NIC and the Drive Bay 7
  NVMe temperatures, through the hwmon interface of the `zimacube_ec_fan`
  kernel driver.

They share nothing but this repository: separate processes, separate systemd
units, separate hardware paths. Either one runs without the other.

## Disk-cage fan daemon — `zimacube-fan`

### Why it exists

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

### How it works

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

### Important: disable automatic SMART monitoring

On the tested ZimaCube 2 configuration, the storage controller reports disks
in standby as `unknown` to the SMART monitoring path. Consequently, `smartd`
cannot reliably detect that a disk is sleeping: its periodic SMART queries can
wake the disks and restart their standby timers.

If `smartmontools` is installed and disk standby is required, disable its
monitoring daemon:

```bash
sudo systemctl disable --now smartd.service
```

Some Debian releases expose the same daemon as `smartmontools.service`. If the
command above reports that `smartd.service` does not exist, use:

```bash
sudo systemctl disable --now smartmontools.service
```

This warning concerns automatic or periodic SMART polling. Manual `smartctl`
checks remain possible, but they should only be run when intentionally waking
a disk, or when the disk is already active.

### Disk standby timeout

The disks' own inactivity timeout is configured separately from this daemon.
On Debian, it can be set in `/etc/hdparm.conf` using `spindown_time`, for
example:

```text
spindown_time = 240
```

When specified in the global section, this setting applies to all configured
drives. The value is the ATA/`hdparm` encoded timeout, not a number of seconds:
values from 1 to 240 represent multiples of five seconds, so `240` means 20
minutes. Consult `man hdparm` before selecting another value because the
encoding changes above 240 and some drives may interpret special values
differently.

This setting controls when a disk enters standby. It is independent of the fan
daemon's `--cooldown` option, which only controls how long the fan remains at
the active speed after disk activity stops.

### Defaults

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

## System fan daemon — `zimacube-sysfan`

### Why it exists

The second fan in a ZimaCube 2, the system fan at the back of the case, is
driven by the ITE IT5570E embedded controller, whose own curve follows the CPU
package temperature and nothing else. That curve knows nothing about the 10G
network controller or about the NVMe drives in Drive Bay 7, so both can sit
well above their comfortable range while the CPU is idle and the fan is barely
turning.

`zimacube-sysfan` closes that gap. It reads those temperatures and drives the
system fan from them through the standard hwmon interface of the
`zimacube_ec_fan` kernel driver:

<https://github.com/cyanide-burnout/zimacube-ec-fan>

That driver is a separate GPL-2.0 project and no part of it is vendored here.
This daemon is an optional userspace policy layer on top of it: it writes
`pwm2` and `pwm2_enable` of the driver's hwmon device and needs nothing else
from it. Install the driver first — until the module is loaded the daemon
simply waits, logging one line per interval.

The CPU fan is never touched. Only those two system-fan attributes are ever
written, so `pwm1`, the EC's CPU curve and the disk-cage controller at address
`0x69` are all left exactly as they are.

### Temperature sources

Both sources are optional and independent; at least one has to be enabled.

`--10g-nic` uses the 10G network controller. The card is found by PCI
identity, never by interface name: `enp95s0` and `hwmon3` are enumeration
artefacts that move when a BIOS is updated or a card is added, while the PCI
identity and the card's place in the tree do not. Where a card exposes more
than one temperature — the AQC113 fitted to current boards reports a PHY and a
MAC reading, normally within a degree of each other — the higher of them is
used. If the built-in list of identities does not cover a card, add it with
`--10g-nic-id 1d6a:04c0`, or name the card outright with
`--10g-nic-pci 0000:5f:00.0`.

That card idles hot: on the tested machine it sits at 66–67 °C with no traffic,
under an hwmon named `enp95s0` after the interface — the very name the daemon
avoids. The default range starts above that on purpose. A range whose bottom
sits under the idle temperature pins the fan permanently: measured at 55–75 °C,
the same machine parked at 70–76% and never came down, against the 47% the EC's
own curve was giving it.

`--bay7-nvme` uses the NVMe drives in Drive Bay 7. The bay hangs off a PCIe
switch, so its drives are identified by topology rather than by `nvme0` or by a
fixed address: the daemon takes the deepest upstream bridge that has more than
one NVMe controller below it, which is the switch itself and not the root port
above it or the downstream ports below it. An M.2 slot wired straight to the
CPU stays out of that group. Only the standard `Composite` temperature is read.
`Sensor 1` and the vendor-specific channels are not, because they are missing
on some drives and mean different things on others — on the tested machine the
two Samsung drives report `Sensor 1` and `Sensor 2` while the Kingston reports
neither.

On that machine the bay resolves to the ASMedia ASM2824 packet switch at
`0000:01:00.0`, behind root port `00:06.0`, with two of its four slots filled.
The boot SSD on its own root port at `0000:5b:00.0` is correctly left out.

With a single drive fitted the bay cannot be told apart from a mainboard slot.
The daemon then says so and names the option that settles it,
`--bay7-pci-root`. To see what it found, and to pick that address:

```bash
sudo /usr/local/sbin/zimacube-sysfan --list-hardware
```

### How the speed is chosen

Every valid sensor is reduced to a normalized thermal pressure

```text
pressure = clamp((temperature - low) / (high - low), 0..1)
```

against the range for its kind: `--10g-nic-low`/`--10g-nic-high` for the NIC,
`--nvme-low`/`--nvme-high` for the drives, each drive counted on its own. The
highest pressure among all enabled sensors wins, and the speed follows it
linearly:

```text
pwm = min-pwm + pressure * (max-pwm - min-pwm)
```

Speeds are percentages, scaled to the driver's 0–255 range on write.

`--min-pwm` can be a safety floor rather than a comfort setting, depending on
what the header drives. On the tested machine the system fan connector carries
the Drive Bay 7 fan and, through a splitter, the only fan cooling the 10G NIC —
the motherboard compartment has no other airflow of its own. Do not set it to
zero there.

A rise is applied at once, because heat should be answered immediately. A fall
is rate limited to `--down-step` percent per interval, and any change smaller
than `--hysteresis` percent is ignored altogether, so a temperature hovering on
a threshold cannot make the fan pump up and down.

### Fail-safe

A sensor that stops reading — a drive pulled, a driver rebound — is dropped
from that round while the remaining ones keep control, and discovery runs again
on the next round to pick it back up. If no enabled source is readable at all,
the daemon hands the fan back to the EC's own curve by writing `2` to
`pwm2_enable`, and takes it back as soon as a source returns. It does the same
on `SIGTERM`, on `SIGINT` and on any other clean exit, so stopping the service
never leaves the fan frozen at the last duty it was given.

### Defaults

```text
Polling interval:       10 seconds
10G NIC range:          70-90 C
NVMe range:             45-70 C
Minimum speed:          40%
Maximum speed:          100%
Hysteresis:             3%
Maximum fall:           5% per interval
hwmon device:           zimacube_ec
```

The full invocation the service unit uses:

```bash
zimacube-sysfan \
    --10g-nic \
    --bay7-nvme \
    --10g-nic-low 70 \
    --10g-nic-high 90 \
    --nvme-low 45 \
    --nvme-high 70 \
    --min-pwm 40 \
    --max-pwm 100 \
    --interval 10
```

### Persistent configuration

Everything is configured on the command line; there is no configuration file.
To change the policy permanently, override the unit:

```bash
sudo systemctl edit zimacube-sysfan.service
```

```ini
[Service]
ExecStart=
ExecStart=/usr/local/sbin/zimacube-sysfan --bay7-nvme --nvme-low 40 --nvme-high 65 --min-pwm 30 --max-pwm 90 --interval 15 --down-step 3
```

The empty `ExecStart=` is required: it clears the command from the shipped unit
before the replacement is added, and without it systemd rejects the override.
Then:

```bash
sudo systemctl restart zimacube-sysfan.service
```

## Installation

Run from the project directory on the ZimaCube:

```bash
sudo ./install.sh
```

The installer:

- verifies that Python 3 is available;
- installs the daemons as `/usr/local/sbin/zimacube-fan` and
  `/usr/local/sbin/zimacube-sysfan`;
- installs and enables `zimacube-fan.service`;
- configures the `i2c-dev` module to load automatically;
- installs `zimacube-sysfan.service`, enabling it only where the
  `zimacube_ec_fan` hwmon device is present, since the system fan daemon is
  useless without that driver;
- restarts the services and displays their status.

Installing the kernel driver afterwards is fine; enable the service then:

```bash
sudo systemctl enable --now zimacube-sysfan.service
```

Both services run as root: the disk-cage daemon because ATA commands and direct
I2C access require `CAP_SYS_RAWIO`, the system fan daemon because it writes the
driver's hwmon attributes.

## Checking the services

Both services report through systemd in the usual way:

```bash
systemctl status zimacube-fan.service
journalctl -u zimacube-fan.service -f
```

```bash
systemctl status zimacube-sysfan.service
journalctl -u zimacube-sysfan.service -f
```

Either daemon can also be run by hand, which is the quickest way to see what it
decides and why.

### Disk-cage fan

To test the decision logic once without detecting the controller or writing to
I2C:

```bash
sudo /usr/local/sbin/zimacube-fan --once --dry-run --verbose
```

To perform one real hardware update:

```bash
sudo /usr/local/sbin/zimacube-fan --once --verbose
```

### System fan

To see which devices were found, and the PCI addresses to use with
`--10g-nic-pci` or `--bay7-pci-root` if the automatic choice is wrong:

```bash
sudo /usr/local/sbin/zimacube-sysfan --list-hardware
```

To read every sensor and log the speed that would follow, without touching the
fan:

```bash
sudo /usr/local/sbin/zimacube-sysfan --10g-nic --bay7-nvme --once --dry-run --verbose
```

To watch it regulate, at a shorter interval than the service uses. `Ctrl+C`
returns the fan to the EC's own curve:

```bash
sudo /usr/local/sbin/zimacube-sysfan --10g-nic --bay7-nvme --interval 5 --verbose
```

To read back what the fan is actually doing, use sysfs or the driver's own dump
rather than `sensors`:

```bash
cat /sys/class/hwmon/hwmon*/pwm2
sudo cat /sys/kernel/debug/zimacube_ec_fan/regs
```

On this chip `sensors` prints the duty against a full scale of 200 rather than
255, so it reports half the raw value as a percentage: a `pwm2` of 178 — the
70% the daemon set — shows there as 89%, and 255 would show as 127%. The debug
dump gives the manual setpoint and the live duty side by side, both out of 255.

## Removal

```bash
sudo ./uninstall.sh
```

This stops and disables both services, removes the daemons, the unit files and
any `systemctl edit` overrides, and drops the `i2c-dev` autoload file. Stopping
the system fan daemon returns the system fan to the EC's own curve; the
disk-cage fan keeps the last speed it was given until the next power cycle. The
`zimacube_ec_fan` kernel module and its DKMS installation are not touched — it
is a separate project with its own uninstall path.

## License

This project is released under the [MIT License](LICENSE).

The `zimacube_ec_fan` kernel driver is a separate project under GPL-2.0-only.
It is used here only through its hwmon sysfs interface; no code is shared
between the two and the licences do not mix.


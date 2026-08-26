# ZimaCube 2 fan daemons

Two independent userspace daemons for a ZimaCube 2 running a conventional
Linux distribution:

- `zimacube-fan` drives the disk-cage fan from disk activity, and optionally
  from the temperature of the disks themselves, over I2C;
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

This project makes the fan control dynamic. As the service unit runs it:

- when at least one disk is active, the fan runs at 60% to provide stronger
  airflow through the disk cage;
- when all disks are inactive, the fan runs at 40% to maintain a minimum
  continuous airflow;
- after the last disk becomes inactive, the fan remains at 60% for another
  two minutes to remove residual heat before dropping to 40%; any new disk
  activity restarts this cooldown;
- above all of that, the measured temperature of the disks may raise the speed
  further, up to 100%.

Instead of using a fixed compromise such as 60% around the clock, the daemon
therefore provides quiet baseline cooling while the disks are idle, more
airflow under load, and a way out of both if the disks actually get hot.

That last part is what [Disk temperature](#disk-temperature) below describes.
It is worth being honest about what it buys: on the machine this was built for,
four disks under a running backup sit at 35–37 °C and the curve never engages
at all. The day-to-day gain comes from dropping the activity speed from 80% to
60%, which the temperature loop then guards. The loop earns its place on the
days that are not ordinary — a hot room, an array rebuild, a clogged intake, a
fan on its way out — where a fixed speed has no answer.

### How it works

Every 30 seconds, the daemon checks `/dev/sd?` using the Linux
`HDIO_DRIVE_CMD` ioctl. If at least one disk returns the ATA `active/idle`
state, the daemon selects the active speed. Any other state, or an empty device
list, is treated as inactive.

Fan control is performed directly through the Linux SMBus ioctl on
`/dev/i2c-N`. The daemon locates the controller at address `0x69` and sends a
command only when the desired fan speed changes.

No external utilities such as `hdparm`, `i2cdetect`, `i2cset`, or `smartctl`
are invoked. Python 3 is the only userspace dependency; the kernel `i2c-dev`
module must be loaded to provide I2C access.

### Disk temperature

`--disk-temp` lets the measured temperature of the disks raise the speed above
what activity alone asked for, all the way to `--max-speed`. Activity and
temperature are not alternatives here, they are combined:

```text
speed = max(activity speed, curve(hottest disk))
```

Activity is a feed-forward term — work has started, heat is on its way — and
the fan reacts in seconds. Temperature is the feedback term, and the disks take
minutes to warm up. The curve can only push the speed up, never below the
`--idle-speed` floor, and it follows the same normalized pressure the system fan
daemon uses:

```text
pressure = clamp((temperature - low) / (high - low), 0..1)
speed    = idle-speed + pressure * (max-speed - idle-speed)
```

With the shipped range that is 40% below 40 °C, 80% at 50 °C and 100% at 55 °C
or above. The hottest disk decides; each one is measured on its own. The
service unit also lowers `--active-speed` from the daemon's own default of 80%
to 60%: a floor of 80% would cover most of the curve and leave the loop able to
act only at the very top of the range.

#### Reading the temperature without keeping the disks awake

The temperature is read with a SMART READ DATA command sent over the Linux
`SG_IO` ioctl as an ATA PASS-THROUGH (16) request — the same transaction
`smartctl` issues, but issued from inside this daemon so that nothing else can
trigger it. Attribute 194 (`Temperature_Celsius`) is used, falling back to 190
(`Airflow_Temperature_Cel`) on the drives that report that one instead.

The kernel's own `drivetemp` module would have made this a one-line sysfs read
and was deliberately not used. Once it is loaded, every disk's temperature
appears in the shared hwmon tree, where `sensors`, `node_exporter`, `netdata`
and anything else that walks hwmon can query it on their own schedule — exactly
the problem that makes `smartd` unusable here, only harder to notice. Binding
the module also probes every SATA disk once to find out which method it
supports.

Sending the command ourselves solves the *who* but not the *when*. A SMART
query to a disk that is spinning with nothing to do may restart its standby
timer, and at one query per polling interval that disk would never sleep again.
So the daemon reads a temperature only when both of these hold:

- the disk answered `active/idle` to the ATA power-mode check, so it is awake
  and the query cannot spin it up;
- the kernel counters in `/sys/block/sdX/stat` moved since the last poll, so
  the disk is genuinely serving I/O and that traffic has just reset its standby
  timer anyway.

Those counters are maintained by the kernel and cost nothing to read; the disk
itself is never touched to obtain them. Together the two conditions mean the
daemon only ever talks to a disk that is already being talked to, and cannot
extend anyone's idle period. A disk that is merely spinning, or one in standby,
is left completely alone.

On top of that, one disk is asked at most once per `--temp-interval`, 120
seconds by default, which is fast enough for a thermal mass measured in
minutes. A reading stays in use for two and a half intervals after it was
taken, so a disk that goes quiet leaves the curve gradually rather than
dropping out at the next poll.

#### Smoothing

Because the curve is a continuous signal, `--hysteresis` and `--down-step`
apply once `--disk-temp` is on: a rise is answered immediately, a fall is
limited to 5% per interval, and changes smaller than 3% are ignored so that a
temperature sitting on a threshold cannot make the fan pump. Without
`--disk-temp` the speed is a two-level signal with nowhere to oscillate, and
both are left out of the way.

#### Tuning the range on your machine

The shipped 40–55 °C is a ceiling guard, not a working range: it is meant to
sit above where the disks normally live and to do nothing until something goes
wrong. Lowering `--disk-temp-high` to make the curve engage more often only
adds noise.

What is worth knowing is whether the fan has any authority over the disk
temperature at all, since that decides whether the guard can do anything when
it does fire. Load all the disks, let them settle at one speed, then at
another, and compare:

```bash
sudo /usr/local/sbin/zimacube-fan --set-speed 40
```

```bash
sudo /usr/local/sbin/zimacube-fan --list-disk-temp
```

A spread of 10 °C or so between 40% and 100% means the loop is real control. A
spread of 2–3 °C means the fan barely moves the disks, and the useful outcome
is the measurement itself: pick better fixed speeds and drop `--disk-temp` from
the unit. Remember to restart the service afterwards, since `--set-speed`
leaves the fan wherever it was put:

```bash
sudo systemctl restart zimacube-fan.service
```

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

The daemon's own `--disk-temp` reads are SMART queries too, and are subject to
exactly the same concern. They are gated so that they cannot cause it: see
[Reading the temperature without keeping the disks
awake](#reading-the-temperature-without-keeping-the-disks-awake).

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

Disk temperature:       off; enabled with --disk-temp
Disk temperature range: 40-55 C
Maximum speed:          100%
Temperature interval:   120 seconds per disk
Hysteresis:             3%
Maximum fall:           5% per interval
```

Those are the daemon's own defaults, which describe the activity-only policy it
falls back to when run by hand with no arguments. The service unit asks for the
temperature-aware one instead:

```bash
zimacube-fan \
    --interval 30 \
    --active-speed 60 \
    --idle-speed 40 \
    --cooldown 120 \
    --disk-temp \
    --disk-temp-low 40 \
    --disk-temp-high 55 \
    --max-speed 100
```

The values can be changed with `--interval`, `--active-speed`, `--idle-speed`,
`--cooldown`, `--bus`, `--devices`, `--max-speed`, `--disk-temp-low`,
`--disk-temp-high`, `--temp-interval`, `--hysteresis`, and `--down-step`. To
change the policy permanently, override the unit in the same way as [the system
fan daemon](#persistent-configuration):

```bash
sudo systemctl edit zimacube-fan.service
```

```ini
[Service]
ExecStart=
ExecStart=/usr/local/sbin/zimacube-fan --interval 30 --active-speed 80 --idle-speed 40 --cooldown 120
```

That particular override is the one to use to go back to the activity-only
policy, temperature loop and all.

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

To see the state of every disk, and the temperature of the ones that are awake:

```bash
sudo /usr/local/sbin/zimacube-fan --list-disk-temp
```

Disks in standby are listed but not queried, so this command is safe to run at
any time. To watch the temperature loop decide, without touching the fan:

```bash
sudo /usr/local/sbin/zimacube-fan --disk-temp --active-speed 60 --dry-run --verbose
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


# ZimaCube 2 fan daemons

Two independent userspace daemons for a ZimaCube 2 running a conventional
Linux distribution:

- `zimacube-fan` drives the disk-cage fan from disk activity, and optionally
  from the temperature of the disks themselves, through the hwmon interface of
  the [zimacube-bay](https://github.com/cyanide-burnout/zimacube-bay)
  kernel driver (`zimacube_bay`);
- `zimacube-sysfan` drives the system fan from the 10G NIC and the Drive Bay 7
  NVMe temperatures, through the hwmon interface of the
  [zimacube-ec](https://github.com/cyanide-burnout/zimacube-ec)
  kernel driver (`zimacube_ec`).

They share nothing but this repository: separate processes, separate systemd
units, separate hardware paths. Either one runs without the other. Each daemon
needs its own kernel driver; the bundled installer enables the disk-cage daemon
and therefore requires `zimacube_bay`.

## Disk-cage fan daemon — `zimacube-fan`

### Why it exists

ZimaCube 2 uses a custom controller for the disk-cage fans. These fans cannot
be configured through the BIOS and are not supported by standard Linux fan
control tools. The stock ZimaOS installation presumably manages them, but
ZimaOS is not always the preferred choice for users who want a conventional,
fully customizable Linux system. This project was created for a ZimaCube 2
running Debian.

The `zimacube_bay` kernel driver provides fan telemetry and a standard
`pwm1` control. This daemon supplies a policy that adapts cooling to disk
activity and temperature.

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
state, the daemon selects the active speed. A `standby` answer, or an empty
device list, is treated as inactive.

A device that fails the query needs an unknown amount of cooling rather than
none, so the daemon holds the airflow at the active speed until it answers
again, and sends it no SMART command in the meantime. Only devices that are not
disks at all are dropped, and whether one is a disk is decided from where it
sits in sysfs: every disk libata attaches lives under an `ataN` port, while a
USB stick or a card reader the `/dev/sd?` glob happened to match lives under a
`usb` node. Counting one of those as active would pin the fan for as long as it
stayed plugged in, so once one has failed the query it is named in the log and
left out of the decision.

Being set aside is not permanent, because one failure that happened to be the
first must not exclude a real disk for good. Such a device is asked again every
ten minutes, quietly; if it ever answers, it is a disk from then on and is said
so in the log. A disk that is recognised as one and keeps failing is different
again: that is a fault, not a misidentification, and it logs `cannot read power
state` on every poll for as long as it lasts.

The topology is what decides this, rather than whether the device has answered
at some point, because a controller that is already broken when the service
starts would otherwise be indistinguishable from a USB stick — and so would a
disk that was unplugged and put back. Every device still gets one chance to
answer before any of that is applied, since an answer is better evidence than a
sysfs path: a disk behind a controller this rule does not recognise is never
dropped merely because its path looked unfamiliar.

Fan control uses the `pwm1` file of the hwmon device named
`zimacube_bay`. The daemon finds that device by name, writes a new duty
when the desired speed changes, and periodically refreshes `pwm1_enable` so
the driver's watchdog knows the daemon is still running. The kernel driver
returns to 80% if the daemon stops updating it. The driver attaches to the
controller shortly after `modprobe` returns, so at start-up the daemon waits up
to ten seconds for the device to appear instead of failing the first start.
This daemon also accepts the legacy `zimacube_bay_fan` hwmon name during
upgrades. The current driver exports `zimacube_bay`; update any local `sensors`
configuration or monitoring rule that still matches the old chip name.

No external utilities such as `hdparm` or `smartctl` are invoked by the
daemon. Python 3 and the `zimacube_bay` kernel module are required.

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

A straight line is only the default. `--hdd-curve` takes breakpoints as
`temperature:speed` pairs and replaces `--disk-temp-low` and `--disk-temp-high`
outright, so the two cannot be mixed:

```bash
zimacube-fan --disk-temp --hdd-curve 40:40,48:60,52:85,55:100
```

The speed is interpolated linearly between neighbouring points and held flat
below the first and above the last. Temperatures have to rise strictly from
point to point and speeds must not fall, so a hotter disk can never get less
air than a cooler one. The two-point default is the same thing written
`40:40,55:100` with the shipped idle and maximum speeds. The daemon logs the
curve it runs at start-up.

#### Backplane temperature

`--board-curve` adds a second source: the temperature of the backplane
controller itself, `temp1_input` of the bay driver. It is read every poll and
costs one transaction with the controller and none with any disk, so it is
available even when every disk is asleep:

```bash
zimacube-fan --disk-temp --board-curve 35:40,45:80,50:100
```

It is off by default because what that sensor tracks is not established: it
sits on the board behind the disks and may lag them, or follow the room more
than the drives. Check it against the disks under load before relying on it.
With both curves on, the one asking for more wins:

```text
speed = max(activity speed, hdd-curve(hottest disk), board-curve(backplane))
```

A backplane read that fails is warned about once, and until it works again the
fan is held at least at `--active-speed` and at whatever the curve last asked
for, whichever is higher. That is the same rule as for a disk that cannot be
asked: a sensor that was asked for and has gone quiet needs an unknown amount
of cooling rather than none, so losing it must not let the fan drift down,
least of all while it was reporting heat.

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
daemon only ever talks to a disk that is already being talked to.

That bounds how far an idle period can be stretched rather than removing the
effect outright. Activity is noticed one poll after the fact, so the last query
of a burst can land up to `--interval` — thirty seconds — after the traffic
that justified it, and if that query restarts the standby timer, it restarts it
from there. Against a spindown timeout of twenty minutes that is under three
percent, and it cannot repeat: the next poll finds the counters unchanged and
asks nothing. What the daemon cannot do is keep a quiet disk awake
indefinitely, which is the failure mode that matters. A disk that is merely
spinning, or one in standby, is left completely alone.

On top of that, one disk is asked at most once per `--temp-interval`, 120
seconds by default, which is fast enough for a thermal mass measured in
minutes. A reading stays in use for two and a half intervals after it was
taken, so a disk that goes quiet leaves the curve gradually rather than
dropping out at the next poll.

#### Smoothing

Because the curves are continuous signals, `--hysteresis` and `--down-step`
apply once `--disk-temp` or `--board-curve` is on: a rise is answered
immediately, a fall is limited to 5% per interval, and changes smaller than 3%
are ignored so that a temperature sitting on a threshold cannot make the fan
pump. Without either the speed is a two-level signal with nowhere to
oscillate, and both are left out of the way.

#### Tuning the range on your machine

The shipped 40–55 °C is a ceiling guard, not a working range: it is meant to
sit above where the disks normally live and to do nothing until something goes
wrong. Lowering `--disk-temp-high` to make the curve engage more often only
adds noise.

What is worth knowing is whether the fan has any authority over the disk
temperature at all, since that decides whether the guard can do anything when
it does fire. Load all the disks, settle them at one speed, then at another,
and compare the two.

The service has to be stopped first, or it will put the fan back where its own
policy wants it at the next poll and the measurement will be of nothing:

```bash
sudo systemctl stop zimacube-fan.service
```

Read every disk in parallel to keep them all working. This writes nothing, and
needs root like everything else here — the block devices are `root:disk`:

```bash
load=(); for d in /dev/sd?; do sudo timeout 1500 dd if="$d" of=/dev/null bs=1M iflag=direct & load+=($!); done
```

The process ids are kept so that only these readers are stopped at the end, and
not somebody else's `dd` copying or imaging a disk from another terminal. That
does mean the rest of the procedure has to run in this same shell. The
25-minute `timeout` is only a backstop for a session walked away from.

With the load running, use a second terminal to run a fixed 40% policy. Leave
this process running while the disks settle; it maintains the driver's
watchdog. A one-shot `--set-speed 40` would return to the driver's 80% fallback
after the watchdog timeout.

```bash
sudo /usr/local/sbin/zimacube-fan --active-speed 40 --idle-speed 40 --max-speed 40
```

Wait about ten minutes for the disks to settle, then take the first reading:

```bash
sudo /usr/local/sbin/zimacube-fan --list-disk-temp
```

Stop that foreground daemon with `Ctrl+C` in the second terminal, then run a
fixed 100% policy there. Wait another ten minutes and take the second reading
from the original terminal:

```bash
sudo /usr/local/sbin/zimacube-fan --active-speed 100 --idle-speed 100 --max-speed 100
```

```bash
sudo /usr/local/sbin/zimacube-fan --list-disk-temp
```

A spread of 10 °C or so between the two means the loop is real control. A
spread of 2–3 °C means the fan barely moves the disks, and the useful outcome
is the measurement itself: pick better fixed speeds and drop `--disk-temp` from
the unit.

Two things end the experiment. Stop the fixed-speed daemon with `Ctrl+C` in
its terminal. Stop the load, which otherwise runs on for
several minutes past the last reading. It runs as root, so stopping it needs
root too:

```bash
sudo kill "${load[@]}"
```

Then hand the fan back to its normal policy:

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
Fan interface:          zimacube_bay hwmon
Disk device pattern:    /dev/sd?

Disk temperature:       off; enabled with --disk-temp
Disk temperature range: 40-55 C, or the points of --hdd-curve
Backplane curve:        off; enabled with --board-curve
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
`--cooldown`, `--hwmon`, `--devices`, `--max-speed`, `--disk-temp-low`,
`--disk-temp-high`, `--hdd-curve`, `--board-curve`, `--temp-interval`,
`--hysteresis`, and `--down-step`. `--idle-speed` is the floor every other
speed and every curve result is clamped to, so it may not go below the bay
driver's `minimum_percent` (30% unless the module was loaded with another
value); the daemon refuses to start rather than have every write rejected. To
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
[zimacube-ec](https://github.com/cyanide-burnout/zimacube-ec) kernel
driver (`zimacube_ec`).

That driver is a separate GPL-2.0 project and no part of it is vendored here.
This daemon is an optional userspace policy layer on top of it: it writes
`pwm2` and `pwm2_enable` of the driver's hwmon device and needs nothing else
from it. Install the driver first — until the module is loaded the daemon
simply waits, logging one line per interval.

The CPU fan is never touched. Only those two system-fan attributes are ever
written, so `pwm1`, the EC's CPU curve and the disk-cage fan are all left
exactly as they are.

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

The two daemons use two separate kernel drivers:

| Daemon | Required driver | When to install it |
|---|---|---|
| `zimacube-fan` | [zimacube-bay](https://github.com/cyanide-burnout/zimacube-bay) (`zimacube_bay`) | Before running this repository's installer. |
| `zimacube-sysfan` | [zimacube-ec](https://github.com/cyanide-burnout/zimacube-ec) (`zimacube_ec`) | Before enabling the system-fan service; otherwise the installer leaves that service disabled. |

### Upgrading renamed drivers

The Python repository and its `zimacube-fan` and `zimacube-sysfan` daemons keep
their names. The kernel modules were renamed from `zimacube_bay_fan` to
`zimacube_bay` and from `zimacube_ec_fan` to `zimacube_ec`. Do not leave both
versions of either driver loaded: they address the same hardware. Stop the fan
services, unload the old modules, install the new DKMS packages, then update
this repository and run `sudo ./install.sh`. The installer requires the new bay
module and its service unit loads it by its new name. Remove the old DKMS
packages and the old EC modules-load rule before rebooting so both versions do
not autoload. The [bay driver](https://github.com/cyanide-burnout/zimacube-bay)
and [EC driver](https://github.com/cyanide-burnout/zimacube-ec) READMEs give the
package-specific removal commands. The bay driver's slot-power control stays
disabled by default during this migration.

Install each driver from its linked repository using its own instructions. On a
ZimaCube with matching kernel headers and DKMS installed, the driver installation
command in each repository is `sudo make dkms`. Then run from this project
directory on the ZimaCube:

```bash
sudo ./install.sh
```

The installer:

- verifies that Python 3 is available;
- verifies that the bay fan kernel module is installed;
- installs the daemons as `/usr/local/sbin/zimacube-fan` and
  `/usr/local/sbin/zimacube-sysfan`;
- installs and enables `zimacube-fan.service`;
- loads `zimacube_bay` when the disk-cage service starts;
- installs `zimacube-sysfan.service`, enabling it only where the
  `zimacube_ec` hwmon device is present, since the system fan daemon is
  useless without that driver;
- restarts the services and displays their status.

The separate system fan driver can be installed later; enable its service then:

```bash
sudo systemctl enable --now zimacube-sysfan.service
```

Both services run as root: the disk-cage daemon reads ATA power state and
writes the bay driver's hwmon attributes; the system fan daemon writes the EC
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

To inspect the decision logic once without finding hwmon or writing fan duty:

```bash
sudo /usr/local/sbin/zimacube-fan --once --dry-run --verbose
```

To perform one real hardware update (the driver's watchdog later returns to
80% unless a daemon keeps it alive):

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
sudo cat /sys/kernel/debug/zimacube_ec/regs
```

On this chip `sensors` prints the duty against a full scale of 200 rather than
255, so it reports half the raw value as a percentage: a `pwm2` of 178 — the
70% the daemon set — shows there as 89%, and 255 would show as 127%. The debug
dump gives the manual setpoint and the live duty side by side, both out of 255.

## Removal

```bash
sudo ./uninstall.sh
```

This stops and disables both services and removes the daemons, unit files, and
any `systemctl edit` overrides. The bay driver returns to its 80% fallback
after its watchdog expires; the system fan returns to the EC's own curve. Both
kernel drivers and their DKMS installations remain in place.

## License

This project is released under the [MIT License](LICENSE).

The `zimacube_ec` and `zimacube_bay` kernel drivers are separate
GPL-2.0-only projects. This repository uses only their hwmon sysfs interfaces;
no driver code is shared with the daemons.

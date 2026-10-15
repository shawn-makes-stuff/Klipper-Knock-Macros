# Knock gestures for Klipper / Kalico

Knock a rhythm on the printer frame and the printer runs a macro. The
accelerometer you already use for input shaping hears the knocks; no extra
hardware. It only listens while the printer is idle and costs nothing while
printing.

## Supported accelerometers

The chip's own event engine detects the knocks, so the host only reads one
latched flag 20 times a second. The plugin finds the chip type from the
section you point it at and uses the matching register settings; one code
path drives them all. Another chip type is one more entry in the `CHIPS`
table in `knock.py`.

| Chip | Klipper section | Detection | Tested on hardware |
|---|---|---|---|
| LIS2DW12 | `[lis2dw]` | tap engine | yes (Centauri Carbon 1) |
| LIS3DH | `[lis3dh]` | click engine, high-pass filtered | no |
| ADXL345 | `[adxl345]` | activity engine, ac-coupled | no |

The ADXL345's tap engine works on unfiltered data, so gravity keeps one axis
above any useful knock threshold. Its ac-coupled activity engine compares
against the acceleration at the moment it is armed instead, which takes
gravity out.

MPU-6050/9250, ICM-20948 and BMI160 are not supported yet.

## Install

### Kalico

Clone into the plugins folder. The folder must be named `knock`:

    cd ~/kalico/klippy/plugins
    git clone https://github.com/shawn-makes-stuff/Klipper-Knock-Macros knock

### Klipper

Clone anywhere and link the folder into `extras` as `knock`:

    git clone https://github.com/shawn-makes-stuff/Klipper-Knock-Macros ~/knock
    ln -s ~/knock ~/klipper/klippy/extras/knock

### Config

Copy `config/knock.cfg` next to your `printer.cfg`, add `[include knock.cfg]`,
set `accel_chip` to your accelerometer's section name, and restart Klipper.

    [knock]
    accel_chip: adxl345

The accelerometer has to stay connected: if its section is in the config,
Klipper needs it at startup anyway.

### Updates through Moonraker

    [update_manager knock]
    type: git_repo
    path: ~/kalico/klippy/plugins/knock
    origin: https://github.com/shawn-makes-stuff/Klipper-Knock-Macros.git
    managed_services: klipper

Use `path: ~/knock` for the Klipper install.

## Quick start

All commands go in the console (Mainsail or Fluidd) with the printer idle.

1. `KNOCK` shows whether it is listening. Knock the frame: the console shows
   what it heard, e.g. `knock: 3 knocks .- -> no match`.
2. `KNOCK CALIBRATE=1` teaches it your tempo. Follow the console: 5 quick
   knocks, 5 quick knocks again, then 3 knocks with a clear pause between
   them. Done once, saved.
3. Record a pattern:

       KNOCK RECORD=home MACRO=G28

   Knock your rhythm (two knocks or more), then knock it again to confirm.
4. Knock that rhythm whenever the printer is idle.

Patterns match by the proportions of the gaps between knocks, so the same
rhythm knocked faster or slower still matches. In the console, `.` is a short
gap and `-` a long one.

## The KNOCK command

One action per command. `KNOCK` alone lists the state and all patterns.

| Command | Effect |
|---|---|
| `KNOCK RECORD=<name> MACRO=<gcode>` | Record a pattern that runs a command or macro. With arguments use quotes: `MACRO="M117 hello"` |
| `KNOCK RECORD=<name> PARROT=1` | ...that replays your rhythm on the toolhead |
| `KNOCK RECORD=<name> PRINT=<text>` | ...that prints a console message. MACRO, PARROT and PRINT combine. |
| `KNOCK DELETE=<name>` | Remove a recorded pattern |
| `KNOCK REMIND=<name>` | Forgot a pattern? The console shows it and the toolhead plays it |
| `KNOCK CALIBRATE=1` | Tempo calibration, guided by the console |
| `KNOCK CANCEL=1` | Stop a RECORD or CALIBRATE in progress (they also stop after 60 s without a knock) |
| `KNOCK ENABLED=0` / `ENABLED=1` | Off (accelerometer powered down, no polling) / on. Persists. |

Messages you may see:

- `that rhythm already belongs to '...'`: knock a different one.
- `that was different`: the confirmation did not match. Start over.
- `ignored a run of more than 12 knocks`: steady vibration, not knocks. If
  nobody knocked, raise `threshold`.

Parrot and REMIND jerk the toolhead 1.5 mm along X where it stands, towards the
middle of the bed. They home X first if it isn't homed (never Z), and lift a
homed nozzle that is lower than 2 mm.

## When it listens

Only while the printer is idle. Knock detection is off, the accelerometer
powered down, and nothing is polled:

- while a print runs or is paused, including a print's heat-up before the
  first move;
- while the toolhead moves;
- while a resonance test or `ACCELEROMETER_MEASURE` uses the accelerometer.
  Knock detection steps aside and re-arms afterwards.

A matched pattern's gcode waits its turn behind any command already running.

## Configuration

| Option | Default | Meaning |
|---|---|---|
| `accel_chip` | required | Accelerometer section to use, e.g. `adxl345`, `lis2dw`, `lis3dh`, `adxl345 bed` |
| `threshold` | `0.06` | Knock threshold in g. Raise it if fans or the neighbours trigger it. Rounded to the chip's step: 62.5 mg (LIS2DW, ADXL345) or 15.6 mg (LIS3DH). |
| `odr` | chip's highest | Sample rate. ADXL345: 400, 800, 1600 (default), 3200. LIS2DW: 400, 800, 1600 (default). LIS3DH: 400, 1344 (default). |
| `poll_hz` | `20` | How often the flag is read. Lower saves a little CPU but times knocks less precisely. |
| `tolerance` | `0.35` | How far a gap's share of the pattern may deviate from the recording |
| `echo` | `True` | Console line for every knock group |
| `state_file` | `knock.json` next to `printer.cfg` | Recorded patterns, tempo and on/off |

Patterns can also live in the config as `[knock_pattern <name>]` sections
with a `gcode` and either `count: N` (any N knocks) or `pattern: .-.` (one
symbol per gap). Recorded patterns take precedence over config ones.

    [knock_pattern hello]
    count: 2
    gcode: M118 knock knock

## Where to knock

Anywhere on the frame; corners are reliable. Knocks need to be at least 0.15 s
apart. A pause longer than about a second ends the pattern (up to 1.6 s after
calibration).

## How it works

While the printer is idle the accelerometer's event engine runs and latches a
flag per knock. A reactor timer reads the flag 20 times a second, counts
knocks, times the gaps, and closes a group after a second of quiet. A group
matches a pattern when it has the same number of knocks and every gap's share
of the whole agrees within the tolerance.

Cost while idle: one register read per 50 ms (about 2% of one core on the
Centauri Carbon 1) and a few kilobytes of memory. While printing: one state
check every 5 seconds, no bus traffic.

## License

GPLv3, like Klipper.

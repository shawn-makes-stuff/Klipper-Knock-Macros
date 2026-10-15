# Klipper Knock Macros

Knock a rhythm on the printer frame to run a macro. Uses the accelerometer you
already have and only listens while the printer is idle.

| Accelerometer | Klipper section | Tested |
|---|---|---|
| LIS2DW12 | `[lis2dw]` | yes |
| LIS3DH | `[lis3dh]` | no |
| ADXL345 | `[adxl345]` | no |

## Install

The folder must be named `knock`.

COSMOS:

    git clone https://github.com/shawn-makes-stuff/Klipper-Knock-Macros /etc/klipper/plugins/knock

Kalico:

    git clone https://github.com/shawn-makes-stuff/Klipper-Knock-Macros ~/kalico/klippy/plugins/knock

Klipper:

    git clone https://github.com/shawn-makes-stuff/Klipper-Knock-Macros ~/knock
    ln -s ~/knock ~/klipper/klippy/extras/knock

Add to `printer.cfg` and restart Klipper:

    [knock]
    accel_chip: lis2dw    # your accelerometer's section name

For updates, add to `moonraker.conf` (`path` is the folder you cloned to):

    [update_manager knock]
    type: git_repo
    path: /etc/klipper/plugins/knock
    primary_branch: main
    origin: https://github.com/shawn-makes-stuff/Klipper-Knock-Macros.git
    managed_services: klipper

`Section 'knock' is not a valid config section` means the folder isn't named
`knock`.

## Use

    KNOCK RECORD=home MACRO=G28

Knock a rhythm, then knock it again to confirm. From then on that rhythm runs
`G28` whenever the printer is idle.

| Command | |
|---|---|
| `KNOCK` | Show state and patterns |
| `KNOCK RECORD=<name> MACRO=<gcode>` | Record a pattern. Also `PRINT=<text>`, `PARROT=1` (toolhead replays it) |
| `KNOCK DELETE=<name>` | Delete a pattern |
| `KNOCK REMIND=<name>` | Toolhead plays the pattern back |
| `KNOCK CALIBRATE=1` | Learn your knock tempo |
| `KNOCK CANCEL=1` | Stop recording or calibrating |
| `KNOCK ENABLED=0` / `1` | Off / on |

## Options

| Option | Default | |
|---|---|---|
| `accel_chip` | required | Accelerometer section name |
| `threshold` | `0.06` | Knock threshold in g |
| `odr` | chip maximum | Accelerometer sample rate |
| `poll_hz` | `20` | Flag reads per second |
| `tolerance` | `0.35` | How far a rhythm may differ from the recording |
| `echo` | `True` | Show every knock group in the console |
| `state_file` | `knock.json` next to `printer.cfg` | Where recordings are saved |

Patterns can also be set in the config:

    [knock_pattern hello]
    count: 2             # or pattern: .-  (. short gap, - long gap)
    gcode: M118 hello

## License

GPLv3

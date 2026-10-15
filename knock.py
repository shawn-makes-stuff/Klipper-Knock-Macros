# Knock gestures: knock a rhythm on the printer frame to run gcode.
#
# Copyright (C) 2026  Shawn Rains <shawn.rains@area9.dk>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#
# While the printer is idle, the accelerometer's own event engine latches a
# flag when the frame is hit. A reactor timer reads that one flag poll_hz
# times a second. Knocks are grouped, the gaps between them form a rhythm, and
# the group is matched against recorded patterns and [knock_pattern] config
# sections. While printing, paused, moving or measuring, the chip is powered
# down and the timer only checks the printer state every few seconds.
import itertools
import json
import logging
import os

######################################################################
# Accelerometers
######################################################################

# How each chip type latches a gravity-free "frame was hit" flag. All chips
# take the same steps: write "arm" in order, read "flag" (which clears the
# latch), read "awake" to check the chip is still armed, write 0 to the
# "awake" register to power it down. A value with THS set is the threshold in
# chip steps ORed with its low byte; RATE is the configured sample rate's
# register value.
THS = 0x100
RATE = 0x200
SHOCK_SAMPLES = 24  # longest knock: the LIS2DW12 window tested on hardware
CHIPS = {
    # Tap engine; its slope filter removes gravity. From the COSMOS knock
    # module, tested on the Centauri Carbon 1.
    "LIS2DW": {
        "label": "LIS2DW12",
        "rates": {400: 0x74, 800: 0x84, 1600: 0x94},  # CTRL1, high-perf.
        "default_rate": 1600,
        "g_per_step": 2.0 / 32,  # TAP_THS at +-2 g
        "max_steps": 31,
        "arm": [
            (0x25, 0x04),  # CTRL6: +-2 g, low noise
            (0x22, 0x10),  # CTRL3: latch tap flags until read
            (0x23, 0x48),  # CTRL4: route taps to INT1 so the latch applies
            (0x30, THS),  # TAP_THS_X
            (0x31, THS),  # TAP_THS_Y
            (0x32, THS | 0xE0),  # TAP_THS_Z, enable X, Y, Z
            (0x33, 0x7F),  # INT_DUR: shock 3 (24 samples), quiet, latency
            (0x34, 0x00),  # WAKE_UP_THS: single-tap mode
            (0x3F, 0x20),  # CTRL7: interrupts enable
            (0x20, RATE),  # CTRL1
        ],
        "awake": (0x20, RATE),
        "flag": (0x39, 0x37),  # TAP_SRC: X/Y/Z tap
    },
    # Click engine on high-pass filtered data, so gravity is out. From ST's
    # lis3dh_reg.h and its lis3dh_tap_single.c example. Not tested.
    "LIS3DH": {
        "label": "LIS3DH",
        "rates": {400: 0x77, 1344: 0x97},  # CTRL1: ODR, X/Y/Z enabled
        "default_rate": 1344,
        "g_per_step": 2.0 / 128,  # CLICK_THS at +-2 g, 1 LSB = FS/128
        "max_steps": 127,
        "arm": [
            (0x21, 0x04),  # CTRL2: high-pass filter on click data
            (0x22, 0x80),  # CTRL3: click on INT1
            (0x23, 0x08),  # CTRL4: +-2 g, high resolution
            (0x38, 0x15),  # CLICK_CFG: single click on X, Y, Z
            (0x3A, THS | 0x80),  # CLICK_THS, LIR_Click: latch until read
            (0x3B, SHOCK_SAMPLES),  # TIME_LIMIT, 1 LSB = 1/ODR
            (0x20, RATE),  # CTRL1
        ],
        "awake": (0x20, RATE),
        "flag": (0x39, 0x40),  # CLICK_SRC: IA
    },
    # The ADXL345 tap engine works on unfiltered data (datasheet, Threshold
    # section), so gravity keeps one axis above a low threshold. The activity
    # engine in ac-coupled mode compares against the acceleration when it is
    # armed instead. From the ADXL345 datasheet and Adafruit_ADXL343. Not
    # tested.
    "ADXL345": {
        "label": "ADXL345",
        "rates": {400: 0x0C, 800: 0x0D, 1600: 0x0E, 3200: 0x0F},  # BW_RATE
        "default_rate": 1600,
        "g_per_step": 0.0625,  # THRESH_ACT
        "max_steps": 255,
        "arm": [
            (0x2D, 0x00),  # POWER_CTL: standby while configuring
            (0x2E, 0x00),  # INT_ENABLE: off while configuring (datasheet)
            (0x38, 0x00),  # FIFO_CTL: bypass
            (0x2C, RATE),  # BW_RATE
            (0x24, THS),  # THRESH_ACT
            (0x27, 0xF0),  # ACT_INACT_CTL: activity ac-coupled, X, Y, Z
            (0x2E, 0x10),  # INT_ENABLE: activity
            (0x2D, 0x08),  # POWER_CTL: measure; the reference is taken now
        ],
        "awake": (0x2D, 0x08),
        "flag": (0x30, 0x10),  # INT_SOURCE: activity
    },
}


class KnockSensor:
    """The accelerometer named by accel_chip, driven through the Klipper chip
    object's read_reg/set_reg with the register values from CHIPS."""

    def __init__(self, config):
        self.name = config.get("accel_chip")
        if not config.has_section(self.name):
            raise config.error(
                "knock: accel_chip '%s' has no [%s] section"
                % (self.name, self.name)
            )
        self.chip = config.get_printer().load_object(config, self.name)
        kind = type(self.chip).__name__
        if kind == "LIS2DW":  # Klipper drives the LIS3DH with the same class
            kind = getattr(self.chip, "lis_type", kind)
        spec = CHIPS.get(kind)
        if spec is None:
            raise config.error(
                "knock: [%s] is not supported; knock works with %s"
                % (self.name, ", ".join(c["label"] for c in CHIPS.values()))
            )
        self.label = spec["label"]
        rates = spec["rates"]
        self.rate = config.getint("odr", spec["default_rate"])
        if self.rate not in rates:
            raise config.error(
                "knock: odr for %s must be one of %s"
                % (self.label, ", ".join(map(str, sorted(rates))))
            )
        g = config.getfloat("threshold", 0.06, above=0.0)
        step = spec["g_per_step"]
        steps = max(1, min(spec["max_steps"], round(g / step)))
        self.threshold = steps * step  # g, rounded to what the chip can do

        def value(v):
            if v & RATE:
                return rates[self.rate]
            if v & THS:
                return (v & 0xFF) | steps
            return v

        self.arm_regs = [(reg, value(v)) for reg, v in spec["arm"]]
        self.awake_reg, self.awake_value = spec["awake"]
        self.awake_value = value(self.awake_value)
        self.flag_reg, self.flag_mask = spec["flag"]

    def measuring(self):
        # A resonance test or ACCELEROMETER_MEASURE owns the chip
        return self.chip.batch_bulk.is_started

    def arm(self):
        for reg, val in self.arm_regs:
            self.chip.set_reg(reg, val)
        self.chip.read_reg(self.flag_reg)  # drop anything latched before

    def disarm(self):
        self.chip.set_reg(self.awake_reg, 0x00)

    def is_armed(self):
        return self.chip.read_reg(self.awake_reg) == self.awake_value

    def hit(self):
        return bool(self.chip.read_reg(self.flag_reg) & self.flag_mask)


######################################################################
# Rhythms
######################################################################

DEBOUNCE = 0.12  # s; a knock rings <= 0.1 s, knocks come >= 0.15 s apart
MAX_KNOCKS = 12  # a longer run is noise, not a pattern
DEFAULT_TEMPO = {"short_max": 0.42, "group_end": 1.0}
LONGEST_GROUP_END = 1.6  # s; calibration listens this long, and sets no more
SYMBOL_WEIGHT = {".": 1.0, "-": 2.5}  # gap lengths of [knock_pattern] symbols
REMIND_SHORT_GAP = 0.25  # s, a short gap when replaying a [knock_pattern]


def proportions(gaps):
    # Each gap's share of the whole group: the same at any tempo
    total = sum(gaps) or 1.0
    return [g / total for g in gaps]


def rhythm_of(gaps, short_max):
    return "".join("." if g <= short_max else "-" for g in gaps)


def median(values):
    return sorted(values)[len(values) // 2]


def knocks_text(count, rhythm):
    text = "%d knock%s" % (count, "" if count == 1 else "s")
    return "%s %s" % (text, rhythm) if rhythm else text


class Pattern:
    """A knock rhythm and what it runs. gaps are the seconds between knocks;
    rhythm marks each gap short (.) or long (-), or is None for a config
    pattern that only counts knocks."""

    def __init__(
        self,
        name,
        gaps,
        rhythm,
        macro=None,
        parrot=False,
        text=None,
        template=None,
        summary=None,
    ):
        self.name = name
        self.gaps = gaps
        self.count = len(gaps) + 1
        self.rhythm = rhythm
        self.profile = proportions(gaps)
        self.macro = macro
        self.parrot = parrot
        self.text = text
        self.template = template  # gcode of a [knock_pattern] section
        self.summary = summary  # its first line, for listings
        self.builtin = template is not None

    @classmethod
    def from_config(cls, section, gcode_macro):
        name = section.get_name().split(None, 1)[1]
        rhythm = section.get("pattern", None)
        count = section.getint("count", None, minval=1)
        if rhythm is not None:
            rhythm = rhythm.strip()
            if not rhythm or set(rhythm) - set(SYMBOL_WEIGHT):
                raise section.error(
                    "[%s] pattern may only contain . and -" % (name,)
                )
            if count is not None and count != len(rhythm) + 1:
                raise section.error(
                    "[%s] count does not match pattern" % (name,)
                )
            gaps = [REMIND_SHORT_GAP * SYMBOL_WEIGHT[c] for c in rhythm]
        elif count is not None:
            gaps = [REMIND_SHORT_GAP] * (count - 1)
        else:
            raise section.error("[%s] needs pattern or count" % (name,))
        template = gcode_macro.load_template(section, "gcode")
        lines = section.get("gcode").strip().splitlines()
        summary = lines[0].strip() if lines else ""
        return cls(name, gaps, rhythm, template=template, summary=summary)

    @classmethod
    def from_json(cls, name, data):
        return cls(
            str(name),
            [float(g) for g in data["gaps"]],
            str(data["rhythm"]),
            macro=data.get("macro"),
            parrot=bool(data.get("parrot")),
            text=data.get("print"),
        )

    def to_json(self):
        return {
            "rhythm": self.rhythm,
            "gaps": self.gaps,
            "macro": self.macro,
            "parrot": self.parrot,
            "print": self.text,
        }

    def distance(self, count, rhythm, profile, tolerance):
        """0 for a perfect match up to tolerance, or None for no match."""
        if count != self.count:
            return None
        if self.rhythm is None:
            return 0.0
        if count < 3:  # a lone gap has no proportions: compare short/long
            return 0.0 if rhythm == self.rhythm else None
        d = max(
            abs(a - b) / max(a, b, 0.05) for a, b in zip(profile, self.profile)
        )
        return d if d <= tolerance else None

    def describe(self):
        does = [
            self.macro,
            "parrot" if self.parrot else None,
            'print "%s"' % (self.text,) if self.text else None,
            self.summary,
        ]
        return "%s: %s -> %s%s" % (
            self.name,
            knocks_text(self.count, self.rhythm),
            ", ".join(d for d in does if d),
            " (config)" if self.builtin else "",
        )


######################################################################
# Knock module
######################################################################

SLEEP_POLL = 5.0  # s between state checks while not listening
CHECK_TICKS = 100  # polls between checks that the chip is still armed
SESSION_TIMEOUT = 60.0  # s without a knock before RECORD or CALIBRATE ends
PARROT_STEP = 1.5  # mm of X per replayed knock
PARROT_FEED = 12000  # mm/min
PARROT_JERK_TIME = 0.03  # s one jerk takes, taken off the replayed gap
PARROT_MIN_Z = 2.0  # mm; a homed nozzle lower than this is lifted first


class Knock:
    """[knock]: listens while the printer is idle, runs matched patterns,
    and owns the KNOCK command."""

    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object("gcode")
        self.sensor = KnockSensor(config)
        self.poll_time = 1.0 / config.getfloat(
            "poll_hz", 20.0, minval=5.0, maxval=50.0
        )
        self.tolerance = config.getfloat(
            "tolerance", 0.35, minval=0.05, maxval=0.9
        )
        self.echo = config.getboolean("echo", True)
        config_dir = os.path.dirname(
            self.printer.get_start_args()["config_file"]
        )
        self.state_file = os.path.expanduser(
            config.get("state_file", os.path.join(config_dir, "knock.json"))
        )
        gcode_macro = self.printer.load_object(config, "gcode_macro")
        self.builtin = {}
        for section in config.get_prefix_sections("knock_pattern "):
            pattern = Pattern.from_config(section, gcode_macro)
            self.builtin[pattern.name] = pattern
        # Settings and recorded patterns, kept in state_file
        self.enabled = True
        self.tempo = dict(DEFAULT_TEMPO)
        self.recorded = {}
        self._load_state()
        # Listening
        self.idle_timeout = self.print_stats = None
        self.armed = False
        self.failing = False
        self.ticks = 0
        self.times = []  # onsets of the knocks in the current group
        self.session = None  # RECORD or CALIBRATE in progress
        self.last = {}
        self._update_status()
        self.timer = self.reactor.register_timer(self._poll)
        self.printer.register_event_handler("klippy:ready", self._handle_ready)
        for event in ("printing", "ready", "idle"):
            self.printer.register_event_handler(
                "idle_timeout:" + event, self._wake
            )
        self.actions = {
            "RECORD": self._cmd_record,
            "DELETE": self._cmd_delete,
            "REMIND": self._cmd_remind,
            "CALIBRATE": self._cmd_calibrate,
            "CANCEL": self._cmd_cancel,
            "ENABLED": self._cmd_enabled,
        }
        self.gcode.register_command(
            "KNOCK", self.cmd_KNOCK, desc=self.cmd_KNOCK_help
        )

    def _handle_ready(self):
        self.idle_timeout = self.printer.lookup_object("idle_timeout")
        self.print_stats = self.printer.lookup_object("print_stats", None)
        logging.info(
            "knock: %s (%s) at %d Hz, threshold %.4f g, %d recorded patterns",
            self.sensor.name,
            self.sensor.label,
            self.sensor.rate,
            self.sensor.threshold,
            len(self.recorded),
        )
        self._wake()

    def _wake(self, *args):
        self.reactor.update_timer(self.timer, self.reactor.NOW)

    def _say(self, msg, log=True):
        self.gcode.respond_info("knock: " + msg, log)

    # Listening
    def _busy(self, eventtime):
        """Why knocks can't be heard now, or None when they can."""
        if self.idle_timeout.get_status(eventtime)["state"] == "Printing":
            return "the printer is busy"
        if self.sensor.measuring():
            return "the accelerometer is measuring"
        # A print waiting on heaters or paused leaves idle_timeout "Ready"
        if self.print_stats is not None:
            state = self.print_stats.get_status(eventtime)["state"]
            if state in ("printing", "paused"):
                return "a print is in progress"
        return None

    def _poll(self, eventtime):
        # Reactor timer. Register reads block; Klipper allows that in timers.
        if self.printer.is_shutdown():
            return self.reactor.NEVER
        if self.session is not None and eventtime > self.session["deadline"]:
            self._end_session("timed out")
        try:
            if not self.enabled or self._busy(eventtime):
                self._stop_listening()
                return eventtime + SLEEP_POLL
            if not self.armed or (
                self.ticks % CHECK_TICKS == 0 and not self.sensor.is_armed()
            ):
                self.sensor.arm()  # also after a measurement reconfigured it
                self.armed = True
            self.ticks += 1
            if self.sensor.hit():
                self._note_knock(eventtime)
            if self.times and eventtime - self.times[-1] > self._group_end():
                self._end_group()
        except Exception:
            if not self.failing:
                logging.exception("knock: accelerometer failed, retrying")
            self.failing = True
            self.armed = False
            return eventtime + SLEEP_POLL
        if self.failing:
            logging.info("knock: accelerometer back")
            self.failing = False
        return eventtime + self.poll_time

    def _stop_listening(self):
        was_armed, self.armed, self.times = self.armed, False, []
        if was_armed and not self.sensor.measuring():
            self.sensor.disarm()  # power the chip down

    def _note_knock(self, eventtime):
        times = self.times
        if times and eventtime - times[-1] <= DEBOUNCE:
            return  # the last knock still ringing
        if len(times) > MAX_KNOCKS:
            times[-1] = eventtime  # noise: only keep the group open
        else:
            times.append(eventtime)

    def _group_end(self):
        if self.session is not None and self.session["kind"] == "calibrate":
            return LONGEST_GROUP_END
        return self.tempo["group_end"]

    def _end_group(self):
        times, self.times = self.times, []
        if len(times) > MAX_KNOCKS:
            self._say(
                "ignored a run of more than %d knocks - if nobody knocked, "
                "raise threshold in [knock]" % (MAX_KNOCKS,)
            )
            return
        gaps = [round(b - a, 3) for a, b in zip(times, times[1:])]
        if self.session is not None:
            self.session["deadline"] = (
                self.reactor.monotonic() + SESSION_TIMEOUT
            )
            if self.session["kind"] == "record":
                self._record_step(gaps)
            else:
                self._calibrate_step(gaps)
            return
        rhythm = rhythm_of(gaps, self.tempo["short_max"])
        pattern = self._match(gaps, rhythm)
        name = pattern.name if pattern else None
        self.last = {
            "count": len(times),
            "rhythm": rhythm,
            "gaps": gaps,
            "matched": name,
        }
        self._update_status()
        if self.echo:
            self._say(
                "%s -> %s"
                % (knocks_text(len(times), rhythm), name or "no match"),
                log=False,
            )
        if pattern is not None:
            self.reactor.register_callback(lambda e: self._run(pattern, gaps))

    def _match(self, gaps, rhythm, patterns=None):
        """The pattern a knock group matches: recorded before config ones,
        rhythms before plain counts, then the closest."""
        if patterns is None:
            patterns = itertools.chain(
                self.recorded.values(), self.builtin.values()
            )
        count, profile = len(gaps) + 1, proportions(gaps)
        best = None
        for p in patterns:
            d = p.distance(count, rhythm, profile, self.tolerance)
            if d is not None:
                key = (p.builtin, p.rhythm is None, d)
                if best is None or key < best[0]:
                    best = (key, p)
        return best[1] if best else None

    # Actions
    def _run(self, pattern, gaps):
        # Its own reactor callback, so the poll never waits on gcode. The
        # gcode mutex queues it behind any running command.
        logging.info("knock: running '%s'", pattern.name)
        try:
            with self.gcode.get_mutex():
                if pattern.text:
                    self.gcode.respond_info(pattern.text)
                if pattern.template is not None:
                    pattern.template.run_gcode_from_command()
                if pattern.macro:
                    self.gcode.run_script_from_command(pattern.macro)
                if pattern.parrot:
                    self._parrot(gaps)
        except self.printer.command_error:
            pass  # gcode has reported it in the console already
        except Exception as e:
            logging.exception("knock: '%s' failed", pattern.name)
            self.gcode.respond_raw(
                "!! knock: '%s' failed: %s" % (pattern.name, e)
            )

    def _parrot(self, gaps):
        """Replay a rhythm as small X jerks of the toolhead, where it stands.
        Needs the gcode mutex (a command handler or _run holds it)."""
        run = self.gcode.run_script_from_command
        toolhead = self.printer.lookup_object("toolhead")
        status = toolhead.get_status(self.reactor.monotonic())
        if "x" not in status["homed_axes"]:
            self._say("homing X first")
            run("G28 X")
            status = toolhead.get_status(self.reactor.monotonic())
        x, _, z = toolhead.get_position()[:3]
        lo, hi = status["axis_minimum"][0], status["axis_maximum"][0]
        step = PARROT_STEP if x < (lo + hi) / 2.0 else -PARROT_STEP
        jerk = "G1 X%.2f F%d\nG1 X%.2f" % (step, PARROT_FEED, -step)
        script = ["SAVE_GCODE_STATE NAME=knock_parrot", "G91"]
        if "z" in status["homed_axes"] and z < PARROT_MIN_Z:
            script.append("G1 Z%.2f F600" % (PARROT_MIN_Z - z,))
        script.append(jerk)
        for gap in gaps:
            script += [
                "G4 P%d" % (max(0.0, gap - PARROT_JERK_TIME) * 1000),
                jerk,
            ]
        script.append("RESTORE_GCODE_STATE NAME=knock_parrot")
        run("\n".join(script))

    # RECORD and CALIBRATE sessions
    def _start_session(self, kind, **data):
        data.update(
            kind=kind, deadline=self.reactor.monotonic() + SESSION_TIMEOUT
        )
        self.session = data
        self.times = []
        self._update_status()

    def _end_session(self, outcome=None):
        session, self.session = self.session, None
        self._update_status()
        if outcome:
            what = (
                "recording '%s'" % (session["name"],)
                if session["kind"] == "record"
                else "calibration"
            )
            self._say("%s %s" % (what, outcome))

    def _record_step(self, gaps):
        session = self.session
        if not gaps:
            self._say("at least two knocks, please")
            return
        short_max = self.tempo["short_max"]
        rhythm = rhythm_of(gaps, short_max)
        owner = self._match(gaps, rhythm)
        if owner is not None:
            self._say(
                "that rhythm already belongs to '%s' - knock a different one"
                % (owner.name,)
            )
            return
        first = session["first"]
        if first is None:
            session["first"] = gaps
            self._say(
                ">>> heard %s. Knock it once more to confirm <<<"
                % (knocks_text(len(gaps) + 1, rhythm),)
            )
            return
        trial = Pattern(session["name"], first, rhythm_of(first, short_max))
        if self._match(gaps, rhythm, [trial]) is None:
            session["first"] = None
            self._say(
                ">>> that was different. Start over: knock the rhythm for "
                "'%s' <<<" % (session["name"],)
            )
            return
        avg = [round((a + b) / 2.0, 3) for a, b in zip(first, gaps)]
        pattern = Pattern(
            session["name"],
            avg,
            rhythm_of(avg, short_max),
            macro=session["macro"],
            parrot=session["parrot"],
            text=session["text"],
        )
        self.recorded[pattern.name] = pattern
        self._end_session()
        self._save_state()
        self._say("saved %s" % (pattern.describe(),))

    def _calibrate_step(self, gaps):
        quick = self.session["quick"]
        if len(quick) < 2:
            if len(gaps) < 3:
                self._say("that was too few - knock 5 times, quickly")
                return
            quick.append(median(gaps))
            if len(quick) < 2:
                self._say(">>> good. Once more: 5 quick knocks <<<")
            else:
                self._say(
                    ">>> now 3 knocks with a clear pause between them <<<"
                )
            return
        if len(gaps) < 2:
            self._say("that was too few - knock 3 times, with pauses")
            return
        quick_gap, slow_gap = sum(quick) / len(quick), median(gaps)
        if slow_gap < quick_gap * 1.5:
            self._say("the pauses were too short - try again, slower")
            return
        self.tempo = {
            "short_max": round((quick_gap + slow_gap) / 2.0, 3),
            "group_end": round(
                min(max(slow_gap * 1.7, 0.8), LONGEST_GROUP_END), 3
            ),
        }
        self._end_session()
        self._save_state()
        self._say(
            "calibrated: gaps up to %.2f s are short, %.2f s of quiet ends "
            "a pattern" % (self.tempo["short_max"], self.tempo["group_end"])
        )

    # State file
    def _load_state(self):
        try:
            with open(self.state_file) as f:
                data = json.load(f)
            enabled = bool(data.get("enabled", True))
            tempo = data.get("tempo", {})
            tempo = {
                k: float(tempo.get(k, v)) for k, v in DEFAULT_TEMPO.items()
            }
            # "bindings": files written by the COSMOS knock module
            stored = data.get("patterns", data.get("bindings", {}))
            recorded = {}
            for name, item in stored.items():
                try:
                    recorded[name] = Pattern.from_json(name, item)
                except (KeyError, TypeError, ValueError):
                    logging.warning("knock: skipped unreadable '%s'", name)
        except FileNotFoundError:
            return
        except (OSError, ValueError, TypeError, AttributeError):
            # Keep the file for the user rather than overwrite it later
            logging.exception("knock: %s is unreadable", self.state_file)
            try:
                os.replace(self.state_file, self.state_file + ".bad")
            except OSError:
                pass
            return
        self.enabled, self.tempo, self.recorded = enabled, tempo, recorded

    def _save_state(self):
        data = {
            "enabled": self.enabled,
            "tempo": self.tempo,
            "patterns": {n: p.to_json() for n, p in self.recorded.items()},
        }
        tmp = self.state_file + ".tmp"
        try:
            os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
            with open(tmp, "w") as f:
                json.dump(data, f, indent=1)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.state_file)  # never a half-written file
        except OSError as e:
            logging.exception("knock: unable to save %s", self.state_file)
            self.gcode.respond_raw("!! knock: could not save: %s" % (e,))

    # Status for Moonraker and macros; replaced, never changed in place
    def _update_status(self):
        self.status = {
            "enabled": self.enabled,
            "mode": self.session["kind"] if self.session else None,
            "last": self.last,
            "patterns": sorted(set(self.recorded) | set(self.builtin)),
        }

    def get_status(self, eventtime):
        return self.status

    # KNOCK command
    cmd_KNOCK_help = (
        "Knock gestures: KNOCK lists patterns; RECORD=<name> MACRO=<gcode> "
        "| PARROT=1 | PRINT=<text>, DELETE=<name>, REMIND=<name>, "
        "CALIBRATE=1, CANCEL=1, ENABLED=0|1"
    )

    def cmd_KNOCK(self, gcmd):
        params = gcmd.get_command_parameters()
        chosen = [a for a in self.actions if a in params]
        if len(chosen) > 1:
            raise gcmd.error(
                "knock: use one of %s at a time" % (", ".join(chosen),)
            )
        if chosen:
            self.actions[chosen[0]](gcmd)
        else:
            self._cmd_list(gcmd)

    def _cmd_list(self, gcmd):
        if not self.enabled:
            state = "off - KNOCK ENABLED=1 turns it on"
        else:
            busy = self._busy(self.reactor.monotonic())
            state = "on, " + ("waiting: " + busy if busy else "listening")
        lines = [
            "knock: %s (%s, threshold %.3f g)"
            % (state, self.sensor.name, self.sensor.threshold)
        ]
        if self.session is not None:
            kind = self.session["kind"]
            lines.append("  %s in progress - KNOCK CANCEL=1 stops it" % (kind,))
        patterns = sorted(self.recorded.values(), key=lambda p: p.name)
        patterns += sorted(self.builtin.values(), key=lambda p: p.name)
        lines += ["  " + p.describe() for p in patterns]
        if not patterns:
            lines.append(
                "  no patterns yet - KNOCK RECORD=<name> MACRO=<gcode>"
            )
        gcmd.respond_info("\n".join(lines))

    def _require_listening(self, gcmd):
        if not self.enabled:
            raise gcmd.error("knock: off - KNOCK ENABLED=1 turns it on")
        busy = self._busy(self.reactor.monotonic())
        if busy:
            raise gcmd.error("knock: can't listen now, %s" % (busy,))

    def _cmd_record(self, gcmd):
        name = gcmd.get("RECORD").strip()
        if not name:
            raise gcmd.error("knock: RECORD needs a name")
        if name in self.recorded:
            raise gcmd.error(
                "knock: '%s' exists - KNOCK DELETE=%s first" % (name, name)
            )
        if name in self.builtin:
            raise gcmd.error("knock: '%s' is a [knock_pattern]" % (name,))
        macro = gcmd.get("MACRO", "").strip() or None
        parrot = bool(gcmd.get_int("PARROT", 0, minval=0, maxval=1))
        text = gcmd.get("PRINT", None)
        if not (macro or parrot or text):
            raise gcmd.error(
                "knock: RECORD needs MACRO=<gcode>, PARROT=1 or PRINT=<text>"
            )
        if macro:
            command = macro.split()[0].upper()
            if command not in self.gcode.ready_gcode_handlers:
                raise gcmd.error("knock: unknown command '%s'" % (command,))
        self._require_listening(gcmd)
        self._start_session(
            "record",
            name=name,
            macro=macro,
            parrot=parrot,
            text=text,
            first=None,
        )
        self._say(">>> knock the rhythm for '%s', then wait <<<" % (name,))

    def _cmd_delete(self, gcmd):
        name = gcmd.get("DELETE")
        if name in self.recorded:
            del self.recorded[name]
            self._save_state()
            self._update_status()
            self._say("'%s' deleted" % (name,))
        elif name in self.builtin:
            raise gcmd.error(
                "knock: '%s' comes from [knock_pattern %s] in the config"
                % (name, name)
            )
        else:
            raise gcmd.error("knock: no pattern named '%s'" % (name,))

    def _cmd_remind(self, gcmd):
        name = gcmd.get("REMIND")
        pattern = self.recorded.get(name) or self.builtin.get(name)
        if pattern is None:
            raise gcmd.error("knock: no pattern named '%s'" % (name,))
        busy = self._busy(self.reactor.monotonic())
        if busy:
            raise gcmd.error("knock: can't play it now, %s" % (busy,))
        self._say("%s - listen" % (pattern.describe(),))
        self._parrot(pattern.gaps)

    def _cmd_calibrate(self, gcmd):
        self._require_listening(gcmd)
        self._start_session("calibrate", quick=[])
        self._say(">>> knock 5 times, quickly, then wait <<<")

    def _cmd_cancel(self, gcmd):
        if self.session is None:
            raise gcmd.error("knock: nothing to cancel")
        self._end_session("cancelled")

    def _cmd_enabled(self, gcmd):
        self.enabled = bool(gcmd.get_int("ENABLED", minval=0, maxval=1))
        if self.session is not None:
            self._end_session("cancelled")
        self._save_state()
        self._update_status()
        self._wake()
        self._say("on" if self.enabled else "off")


def load_config(config):
    return Knock(config)

#!/usr/bin/env python3
"""
r45w - core library for controlling the Lenovo Legion R45w-30 over DDC/CI.

This module knows nothing about HTTP or user interfaces: it exposes the monitor
as a table of controls, plus read() and write(). Both the CLI (bin/r45w) and the
web service (service/ddc-web.py) are built on it.

Everything here was measured against a real monitor. See REGISTERS.md.

HOW VALUES ARE ENCODED
----------------------
The monitor uses three different encodings, and mixing them up produces very
convincing false negatives:

  byte   value goes in the low byte      (brightness, contrast, source...)
  high   value goes in the HIGH byte     (gamma: 0x7800 means 2.2)
  word   value spans both bytes          (0x60 = <right><left>)

ddcutil DISCARDS the high byte when formatting non-continuous features, so every
read here uses -v and parses sh=/sl= from the raw dump.

THE INDEX + VALUE MECHANISM
---------------------------
Several features (KVM, overclock, True Split...) have no register of their own:
you write an index to 0xF8, then read or write its value in 0xF7. 0xF8 cannot be
read back, so its writes need --noverify, and the latch must settle before
reading 0xF7 - otherwise you read the PREVIOUS index's value.

WARNING: never write 0xF6, it locks the monitor. Recover with `setvcp 0xF6 x00`.
"""

import json
import os
import re
import subprocess
import time

DISPLAY = os.environ.get("DDC_DISPLAY", "1")
LOCK = os.environ.get(
    "DDC_LOCK", f"{os.environ.get('XDG_RUNTIME_DIR', '/tmp')}/r45w.lock")

CONFIG = os.environ.get("R45W_CONFIG") or os.path.join(
    os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"),
    "r45w", "aliases.json")

# The monitor's own input names. These are the generic, always-correct labels;
# anything specific to one setup belongs in the alias file, never here.
PORTS = {0x0F: "DisplayPort", 0x31: "USB-C", 0x11: "HDMI-1", 0x12: "HDMI-2"}


def load_aliases():
    """User-defined names for the inputs, e.g. {0x31: "MacBook"}."""
    try:
        with open(CONFIG) as fh:
            return {int(k, 0): str(v) for k, v in json.load(fh).items()}
    except (OSError, ValueError, TypeError, AttributeError):
        return {}


def save_aliases(aliases):
    os.makedirs(os.path.dirname(CONFIG), exist_ok=True)
    with open(CONFIG, "w") as fh:
        json.dump({f"0x{k:02x}": v for k, v in sorted(aliases.items())}, fh,
                  indent=2, ensure_ascii=False)
        fh.write("\n")


def port_label(code, aliases=None):
    """'USB-C' on its own, or 'MacBook (USB-C)' when an alias is set."""
    aliases = load_aliases() if aliases is None else aliases
    port = PORTS.get(code, f"0x{code:02x}")
    alias = aliases.get(code)
    return f"{alias} ({port})" if alias else port


def _input_options(aliases=None):
    aliases = load_aliases() if aliases is None else aliases
    return [{"txt": aliases.get(h) or PORTS[h],
             "sub": PORTS[h] if aliases.get(h) else None, "val": h}
            for h in (0x0F, 0x31, 0x11, 0x12)]


# ---------------------------------------------------------------- controls --
#
# read:   "<VCP>:<field>"  field = sl | sh | val   ·  "kvm:<index>"  ·  None
# write:  "<VCP>:<enc>"    enc   = byte | high | word ·  "kvm:<index>" · None
# slow:   the write recomposes the picture; wait for the state to settle
#
CONTROLS = [
    # --- layout ---
    {"key": "mode", "name": "Layout", "group": "layout",
     "type": "enum", "read": "derived:layout", "write": "0xF5:word", "slow": True,
     "help": "Screen layout. PIP can only be enabled from the monitor joystick.",
     "options": [
         {"txt": "Full screen", "sub": "single source", "val": 0x0000, "state": "full"},
         {"txt": "PBP 50/50", "sub": "equal halves", "val": 0x0101, "state": "pbp-1"},
         {"txt": "PBP 2/3 + 1/3", "sub": "wider left", "val": 0x0401, "state": "pbp-4"},
     ]},
    {"key": "left", "name": "Left window", "group": "layout",
     "type": "enum", "read": "0x60:sl", "write": "window:sl", "slow": True,
     "help": "Source shown in the left (primary) window. In full screen, the "
             "only window.",
     "options": _input_options},
    {"key": "right", "name": "Right window", "group": "layout",
     "type": "enum", "read": "0x60:sh", "write": "window:sh", "slow": True,
     "help": "Source shown in the right (secondary) window. Only meaningful in PBP.",
     "options": _input_options},
    {"key": "swap", "name": "Swap windows", "group": "layout",
     "type": "action", "read": "0xF4:sl", "write": "0xF2:byte", "value": 0x01,
     "slow": True, "active_if": 1,
     "labels": {0: "not swapped", 1: "swapped"},
     "help": "Swaps the two windows. Audio follows them. The monitor ignores "
             "this when both windows show the same source."},
    {"key": "power", "name": "Screen", "group": "layout",
     "type": "enum", "read": "0xD6:sl", "write": "0xD6:byte", "slow": True,
     "help": "The monitor keeps answering DDC/CI while off, so it can be turned "
             "back on by command without touching the physical button.",
     "options": [
         {"txt": "On", "val": 0x01},
         {"txt": "Standby", "sub": "USB-C keeps charging", "val": 0x04},
         {"txt": "Off", "sub": "stops charging", "val": 0x05},
     ]},

    # --- kvm ---
    {"key": "kvm_switch", "name": "Switch peripherals", "group": "kvm",
     "type": "action", "read": "0xED:val", "write": "kvm:0x08", "value": 0x01,
     "slow": True, "active_if": 0x00,
     "labels": {0x00: "on the USB-C upstream", 0x10: "on the USB-B upstream"},
     "help": "Moves keyboard, mouse and webcam between the two upstream USB "
             "ports. Only two video inputs are bound to the KVM."},
    {"key": "kvm", "name": "KVM", "group": "kvm",
     "type": "enum", "read": "kvm:0x07", "write": "kvm:0x07", "slow": True,
     "help": "Enable or disable the KVM. Note: even when disabled, in full "
             "screen the peripherals still follow the video input.",
     "options": [{"txt": "Enabled", "val": 1}, {"txt": "Disabled", "val": 0}]},

    # --- image ---
    {"key": "brightness", "name": "Brightness", "group": "image",
     "type": "range", "read": "0x10:val", "write": "0x10:byte", "max": 100},
    {"key": "contrast", "name": "Contrast", "group": "image",
     "type": "range", "read": "0x12:val", "write": "0x12:byte", "max": 100},
    {"key": "volume", "name": "Speaker volume", "group": "image",
     "type": "range", "read": "0x62:val", "write": "0x62:byte", "max": 100,
     "help": "Works even though the monitor does NOT declare it in its "
             "capabilities string."},
    {"key": "picture_mode", "name": "Picture mode", "group": "image",
     "type": "enum", "read": "0xF9:val", "write": "0xF9:byte",
     "help": "The monitor's own presets. Names read from its OSD.",
     "options": [
         {"txt": "Standard", "val": 0x0A}, {"txt": "FPS 1", "val": 0x01},
         {"txt": "FPS 2", "val": 0x02}, {"txt": "Racing", "val": 0x03},
         {"txt": "RTS", "val": 0x04}, {"txt": "Game 1", "val": 0x05},
         {"txt": "Game 2", "val": 0x06},
     ]},
    {"key": "sharpness", "name": "Sharpness", "group": "image",
     "type": "range", "read": "0x87:val", "write": "0x87:byte", "max": 4},

    # --- color ---
    {"key": "color_preset", "name": "Color preset", "group": "color",
     "type": "enum", "read": "0x14:val", "write": "0x14:byte",
     "options": [
         {"txt": "sRGB", "val": 0x01}, {"txt": "6500 K", "val": 0x05},
         {"txt": "7500 K", "val": 0x06}, {"txt": "9300 K", "val": 0x08},
         {"txt": "User 1", "val": 0x0B},
     ]},
    {"key": "gamma", "name": "Gamma", "group": "color",
     "type": "enum", "read": "0x72:sh", "write": "0x72:high",
     "help": "WARNING: the value lives in the HIGH byte. Writing 0x78 does "
             "nothing; it has to be 0x7800.",
     "options": [
         {"txt": "1.8", "val": 0x50}, {"txt": "2.0", "val": 0x64},
         {"txt": "2.2", "sub": "native", "val": 0x78},
         {"txt": "2.4", "val": 0x8C}, {"txt": "2.6", "val": 0xA0},
     ]},
    {"key": "red", "name": "Red gain", "group": "color",
     "type": "range", "read": "0x16:val", "write": "0x16:byte", "max": 100,
     "help": "Touching any gain switches the color preset to 'User 1'."},
    {"key": "green", "name": "Green gain", "group": "color",
     "type": "range", "read": "0x18:val", "write": "0x18:byte", "max": 100},
    {"key": "blue", "name": "Blue gain", "group": "color",
     "type": "range", "read": "0x1A:val", "write": "0x1A:byte", "max": 100},

    # --- advanced ---
    {"key": "overdrive", "name": "Over Drive", "group": "advanced",
     "type": "enum", "read": "0xE0:val", "write": "0xE0:byte",
     "help": "Panel response time. Its effect is ONLY visible in motion, never "
             "on a still image.",
     "options": [
         {"txt": "Off", "val": 0x00}, {"txt": "Level 3", "val": 0x03},
         {"txt": "Level 4", "val": 0x04}, {"txt": "Level 5", "val": 0x05},
         {"txt": "Level 6", "val": 0x06},
     ]},
    {"key": "hdr", "name": "HDR", "group": "advanced",
     "type": "enum", "read": "0xEF:val", "write": "0xEF:byte",
     "options": [
         {"txt": "Off", "val": 0x00}, {"txt": "Mode 1", "val": 0x01},
         {"txt": "Mode 6", "val": 0x06}, {"txt": "Mode 7", "val": 0x07},
         {"txt": "Mode 8", "val": 0x08}, {"txt": "Mode 9", "val": 0x09},
     ]},
    {"key": "scaling", "name": "Scaling", "group": "advanced",
     "type": "enum", "read": "0x86:val", "write": "0x86:byte",
     "options": [
         {"txt": "Aspect ratio", "sub": "no distortion", "val": 0x02},
         {"txt": "Fill vertically", "sub": "distorts", "val": 0x05},
     ]},
    {"key": "usb_charging", "name": "USB charging", "group": "advanced",
     "type": "enum", "read": "0xEC:val", "write": "0xEC:byte",
     "options": [{"txt": "Enabled", "val": 0x01}, {"txt": "Disabled", "val": 0x00}]},

    # --- read only ---
    {"key": "refresh", "name": "Refresh rate", "group": "info",
     "type": "info", "read": "0xAE:val", "write": None,
     "help": "Timing of the SECONDARY window. A 60 Hz source there drags the "
             "whole panel down to 60 Hz, including the other window."},
    {"key": "peripherals", "name": "Peripherals on", "group": "info",
     "type": "info", "read": "0xED:val", "write": None,
     "help": "KVM state. Writing this register does NOT switch anything and "
             "leaves the monitor out of sync - use kvm_switch instead.",
     "labels": {0x00: "USB-C upstream", 0x10: "USB-B upstream"}},
    {"key": "charge_power", "name": "Charging power", "group": "info",
     "type": "info", "read": "kvm:0x04", "write": None, "unit": "W"},
    {"key": "overclock", "name": "Overclock", "group": "info",
     "type": "info", "read": "kvm:0x1A", "write": None,
     "help": "Read only: the monitor ignores writes to this index.",
     "labels": {0: "Disabled", 1: "Enabled"}},
    {"key": "true_split", "name": "True Split", "group": "info",
     "type": "info", "read": "kvm:0x15", "write": None,
     "help": "Read only: the monitor ignores writes to this index.",
     "labels": {0: "Disabled", 1: "Enabled"}},
    {"key": "usb_mode", "name": "USB mode", "group": "info",
     "type": "info", "read": "kvm:0x0D", "write": None,
     "labels": {1: "USB 2.0", 2: "USB 3.2"}},
    {"key": "firmware", "name": "Firmware", "group": "info",
     "type": "info", "read": "0xC9:val", "write": None, "hex": True},
]
BY_KEY = {c["key"]: c for c in CONTROLS}

GROUPS = [("layout", "Layout"), ("kvm", "Peripherals"), ("image", "Image"),
          ("color", "Color"), ("advanced", "Advanced"), ("info", "Information")]

# Registers read in a single call. kvm: ones are separate: each needs its index
# latched first.
BULK = sorted({c["read"].split(":")[0] for c in CONTROLS
               if c["read"] and not c["read"].startswith(("kvm:", "derived:"))}
              | {"0xF4", "0xF5", "0x60"})

LAYOUTS = {"full": "full screen", "pip": "PIP (joystick only)",
           "pbp-1": "PBP 50/50", "pbp-4": "PBP 2/3 + 1/3"}

_RAW = re.compile(r"opcode=0x(\w+).*?sh=0x(\w+), sl=0x(\w+), max_val=(\d+)")


class MonitorError(Exception):
    pass


def options_of(c, aliases=None):
    """Options may be a callable, so input lists pick up alias changes."""
    opts = c.get("options")
    return opts(aliases) if callable(opts) else (opts or [])


def _ddcutil(args, timeout=60, extra=()):
    try:
        return subprocess.run(
            ["flock", LOCK, "ddcutil", "--syslog", "NEVER", *extra, "-d", DISPLAY]
            + list(args), capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None


def _parse(out):
    res = {}
    for m in _RAW.finditer(out):
        code = m.group(1).upper()
        sh, sl, mx = int(m.group(2), 16), int(m.group(3), 16), int(m.group(4))
        res[code] = {"val": sh << 8 | sl, "sh": sh, "sl": sl, "max": mx}
    return res


def _read_indexed(index, tries=3):
    """Index into 0xF8, value in 0xF7.

    Requires TWO matching reads: read too soon after latching the index and
    0xF7 returns the previous index's value, which looks like real data.
    """
    prev = None
    for _ in range(tries):
        _ddcutil(["setvcp", "0xF8", f"x{index:02x}"], extra=("--noverify",))
        time.sleep(0.4)
        r = _ddcutil(["-v", "getvcp", "0xF7"])
        if r is None:
            return None
        d = _parse(r.stdout).get("F7")
        v = d["val"] if d else None
        if v is not None and v == prev:
            return v
        prev = v
        time.sleep(0.4)
    return prev


def read(indexed=True, aliases=None):
    """Current state: {key: {value, text, max}}.

    indexed=False skips the index-based reads (KVM, overclock...), which cost a
    round trip each. The web service uses that for its frequent refresh.
    """
    aliases = load_aliases() if aliases is None else aliases
    r = _ddcutil(["-v", "getvcp"] + BULK)
    if r is None:
        raise MonitorError("monitor not responding (powered off or unplugged?)")
    raw = _parse(r.stdout)
    if "60" not in raw or "F4" not in raw:
        raise MonitorError("incomplete read from the monitor")

    mode = raw["F4"]["sh"]
    derived = {"layout": "full" if mode == 0 else "pip" if mode == 1
               else f"pbp-{raw['F5']['sh']}"}

    state = {}
    for c in CONTROLS:
        if not c["read"]:
            continue
        reg, _, field = c["read"].partition(":")
        if reg == "derived":
            val = derived.get(field)
        elif reg == "kvm":
            val = _read_indexed(int(field, 16)) if indexed else None
        else:
            d = raw.get(reg[2:].upper())
            val = d[field] if d else None
            # The right window only means something in multi-window mode.
            if c["key"] == "right" and mode == 0:
                val = None
        state[c["key"]] = {"value": val, "text": text_of(c, val, aliases),
                           "max": c.get("max")}
    state["_layout"] = {"value": mode, "text": LAYOUTS.get(derived["layout"], "?")}
    return state


def text_of(c, val, aliases=None):
    if val is None:
        return "-"
    raw = f"0x{val:04x}" if isinstance(val, int) else str(val)
    if "labels" in c:
        return c["labels"].get(val, raw)
    if c["type"] == "enum":
        if c["key"] in ("left", "right"):
            return port_label(val, aliases)
        for o in options_of(c, aliases):
            # `mode` is read as a derived string ("pbp-1"), not as the value
            # written to it, so its options carry both.
            if val in (o["val"], o.get("state")):
                return o["txt"]
        return raw
    if c.get("hex"):
        return raw
    if c["key"] == "refresh":
        return f"{val / 100:.0f} Hz"
    if c.get("unit"):
        return f"{val} {c['unit']}"
    return str(val)


def valid_values(c):
    if c["type"] == "enum":
        return [o["val"] for o in options_of(c)]
    if c["type"] == "action":
        return [c["value"]]
    if c["type"] == "range":
        return list(range(0, c.get("max", 100) + 1))
    return []


def write(key, value, previous=None):
    """Apply a control. `value` is an int. Returns None, or an error message."""
    c = BY_KEY.get(key)
    if c is None:
        return f"unknown control: {key}"
    if not c["write"]:
        return f"'{c['name']}' is read only"
    if value not in valid_values(c):
        return f"value not allowed for '{c['name']}': {value}"

    target, _, enc = c["write"].partition(":")

    if target == "window":
        # Both windows share 0x60: rebuild the 16-bit value, preserving the
        # other one.
        st = previous or read(indexed=False)
        other = st["right" if enc == "sl" else "left"]["value"] or 0
        word = (other << 8 | value) if enc == "sl" else (value << 8 | other)
        _ddcutil(["setvcp", "0x60", f"x{word:04x}"], extra=("--noverify",))
        return None

    if target == "kvm":
        # Index then value, unverified: 0xF8 cannot be read back and an
        # interleaved read unlatches it.
        _ddcutil(["setvcp", "0xF8", enc], extra=("--noverify",))
        time.sleep(0.15)
        _ddcutil(["setvcp", "0xF7", f"x{value:02x}"], extra=("--noverify",))
        return None

    text = {"byte": f"x{value:02x}", "high": f"x{value:02x}00",
            "word": f"x{value:04x}"}[enc]
    r = _ddcutil(["setvcp", target, text],
                 extra=("--noverify",) if c.get("slow") else ())
    if r is not None and not c.get("slow"):
        # On slow controls the monitor is recomposing and cannot answer the
        # read-back, so it reports a failure that means nothing.
        bad = [l for l in r.stderr.splitlines()
               if "Verification failed" in l or "DDCRC" in l]
        if bad:
            return f"monitor rejected '{c['name']}': {bad[0].strip()}"
    return None


def wait_until_stable(limit=25, indexed=False):
    """Two identical consecutive reads. A layout change takes up to ~10 s and
    the monitor stops answering DDC while it recomposes: waiting a fixed time
    yields mid-transition reads, which look a lot like 'no effect'."""
    start, prev = time.time(), None
    while time.time() - start < limit:
        try:
            v = read(indexed=indexed)
        except MonitorError:
            v = None
        if v is not None and v == prev:
            return v
        prev = v
        time.sleep(1)
    return prev

# r45w — DDC/CI control for the Lenovo Legion R45w-30

Drive an ultrawide monitor shared between several machines entirely by command:
which input goes in each window, the layout, the built-in KVM, picture settings
and power. Without touching the monitor's joystick.

Runs on **Linux with `ddcutil`**, including over the DisplayPort of an NVIDIA
card with the proprietary driver — contrary to what is usually claimed.

DDC/CI rides the video link itself, so any modern digital connection carries it:
the controlling machine talks to the monitor over its own cable, whichever port
that is. What is **verified here** is DisplayPort on NVIDIA's proprietary
driver, the combination most often reported as broken. HDMI and USB-C as the
control link should work the same and are **untested in this project**.

One difference worth knowing: over DisplayPort and USB-C the I2C lines are
multiplexed onto the AUX channel, while over HDMI and DVI they sit on dedicated
pins. It makes no difference to `ddcutil`, but it is why hardware bus sniffing
only works over HDMI or DVI.

```
$ r45w

  Lenovo Legion R45w-30   PBP 50/50

  Layout
    Left window          Workstation (DisplayPort)
    Right window         MacBook (USB-C)
    Screen               On
  Peripherals
    Switch peripherals   on the USB-B upstream
    KVM                  Enabled
  ...
```

---

## Getting started

Requirements: `ddcutil` 2.x, Python 3.9+, and membership of the `i2c` group.

```bash
sudo usermod -aG i2c "$USER"    # then reboot: the systemd --user manager does
                                # not pick up group changes without one
```

### From the terminal

```bash
bin/r45w                        # current state
bin/r45w list                   # every control and the values it accepts
bin/r45w set left usb-c         # by port name, alias, number or hex
bin/r45w set brightness 80
bin/r45w swap                   # swap the two windows (audio follows)
bin/r45w kvm                    # move the peripherals to the other machine
bin/r45w help gamma             # what it does, how it is encoded, its caveats
bin/r45w json                   # for scripts
```

### Naming your inputs

Inputs are named after the physical ports by default, so the tool works on any
setup out of the box. Give them your own names and use those everywhere:

```bash
r45w alias displayport "Workstation"
r45w alias usb-c "MacBook"
r45w alias hdmi-1 "Work laptop"

r45w set left workstation       # aliases work as arguments too
```

Aliases live in `~/.config/r45w/aliases.json` (override with `R45W_CONFIG`).
Nothing machine-specific is hardcoded anywhere else.

### As a web service

```bash
systemctl --user enable --now service/ddc-web.service
```

Serves on `http://<host>:8081`, designed mobile-first. **It has no login**: it
is meant to be exposed only on a private network such as a Tailscale tailnet,
where access is already authenticated at the network layer.

The shipped unit binds `127.0.0.1` on purpose, so it is useless remotely until
you say otherwise. Point it at the address you actually want with a drop-in,
which keeps your setup out of the repository:

```bash
mkdir -p ~/.config/systemd/user/ddc-web.service.d
cat > ~/.config/systemd/user/ddc-web.service.d/local.conf <<'EOF'
[Service]
Environment=DDC_WEB_HOST=100.x.y.z
EOF
systemctl --user daemon-reload && systemctl --user restart ddc-web
```

Binding `0.0.0.0` would expose monitor control to the whole LAN.

---

## How it is put together

```
lib/r45w.py          CORE. The control table plus read()/write().
                     Single source of truth; knows nothing about HTTP or UI.
bin/r45w             CLI. Everything is doable without running the web service.
service/ddc-web.py   HTTP layer and UI, generated from the same table.
tools/ddc-snap       VCP register dump and diff, for further exploration.
```

**Adding a control is one entry in `CONTROLS` in `lib/r45w.py`.** It shows up in
the CLI and the web UI on its own.

The backend is deliberately independent of the frontend: the web UI is just one
client, and the monitor can be driven entirely from a shell or a script.

---

## Using this on a different monitor

The project was written for one model, but it splits cleanly into a part that
is standard and a part that is not.

**Works unchanged on any DDC/CI monitor.** These are VESA MCCS standard codes,
and the control table already uses them:

| | |
|---|---|
| `0x60` input source | `0x10` `0x12` brightness, contrast |
| `0x62` speaker volume | `0x14` color preset · `0x16` `0x18` `0x1A` RGB gains |
| `0xD6` power | `0x87` sharpness · `0x72` gamma · `0x86` scaling |

Input codes `0x0F` (DisplayPort), `0x11` and `0x12` (HDMI) are standard too.
`0x31` for USB-C is **not** — it is a vendor extension, and other makers use
different values. Adjust `PORTS` in `lib/r45w.py`.

**Needs remapping.** Everything interesting is manufacturer-reserved
(`0xE0`-`0xFF`) and differs per vendor, sometimes per model: the layout register
(`0xF5` here), window swapping (`0xF2`), the KVM index mechanism (`0xF8`/`0xF7`),
picture modes (`0xF9`), and the read-only status registers. None of that will
mean the same thing on a Dell or an LG.

**What the project gives you for that work:**

- `tools/ddc-snap` and the method it encodes: dump every register, change
  exactly one setting in the monitor's OSD, dump again, diff. Whatever moved is
  the register you want. It already filters the codes that produce phantom
  changes.
- `REGISTERS.md`'s trap list. These are firmware behaviours, not quirks of this
  unit, and versions of them show up on other monitors: registers whose value
  spans two bytes while the tool shows one, features hidden behind an
  index/value pair, unreadable registers that echo instead of erroring, and
  capabilities strings that both over- and under-declare.
- A control table where adding a register is one entry, and it appears in the
  CLI and the web UI at once.

**If your monitor is also a Lenovo**, start from
[`reference/lenovo-vcp-codes.json`](reference/): that table is generic across
Lenovo models and names most of the reserved codes. It will not always be right
for your model — it calls `0xF4` "PIP Size" and on this one that register is
layout mode plus swap flag — but it turns blind probing into checking a
hypothesis.

Expect the vendor half to be real work. Mapping this monitor took several
sessions, and most of the wrong turns are documented in `REGISTERS.md` precisely
so the next person can skip them.

---

## What works and what does not

| Works by command | Does not |
|---|---|
| Layout: full screen · PBP 50/50 · PBP 2/3+1/3 | Entering **PIP** |
| Source of **both windows**, in a single write | Choosing whether audio follows the left or right window |
| Swapping windows — **audio follows them** | Dynamic contrast |
| KVM: switch, enable/disable, and read its state | Overclock and True Split (readable, not writable) |
| Power: on · standby · full off | |
| Brightness, contrast, volume, sharpness, gamma, color, RGB gains, Over Drive, HDR, scaling, USB charging | |

Audio **is** controllable, just indirectly: it follows the window, so swapping
moves the sound.

---

## The four traps in this monitor

If something "does not work", it is almost certainly one of these. All four cost
hours.

### 1. Some registers are 16-bit, and `ddcutil` truncates them

`0x60` is not "the input": it is **both windows**, low byte the left one and
high byte the right one. But `ddcutil getvcp 0x60` only reports `SNC x0f`,
because it formats the feature as a single-byte value.

**The full value is read correctly** — it is right there under `-v`:

```
$ ddcutil -d 1 -v getvcp 0x60
Raw value: opcode=0x60, mh=0xff, ml=0xff, sh=0x31, sl=0x0f, cur_val=0x310f
                                          ^^^^^^^ right   ^^^^^^^ left
```

Three elaborate mechanisms were built here to work around a limitation that did
not exist, all because of that truncation. **When a register looks like a single
byte, check `-v` before concluding anything.**

The same applies to gamma (`0x72`), whose value lives in the **high** byte:
`setvcp 0x72 x78` does nothing; it has to be `x7800`.

### 2. Some features have no register: index + value

The KVM, overclock, True Split and others have no register of their own. You
write an **index** to `0xF8`, then read or write the **value** in `0xF7`.

```bash
ddcutil -d 1 --noverify setvcp 0xF8 x08   # index: "switch KVM"
sleep 0.2
ddcutil -d 1 --noverify setvcp 0xF7 x01   # do it
```

`--noverify` is mandatory: `0xF8` cannot be read back, so verification would
always fail, and **an interleaved read unlatches the index**.

### 3. Unreadable registers do not error — they echo the previous read

A naive sequential dump makes every opaque register inherit its predecessor's
value, and that looks a great deal like real data. `0x20`, `0x30`, `0xF0`,
`0xF1` and `0xF3` all echo; unsupported `0xF8` indices return `0x0200`/`0x0201`.

The defence is a **canary**: read a known register (`0xCC`, OSD language) before
each read and discard any response identical to it. `tools/ddc-snap` does this.

### 4. `0xF6` locks the monitor

Writing `0x07` to it leaves the screen showing *"The monitor is locked, please
contact with the administrator"*. **Nothing is lost**, and it is recoverable
over DDC without the physical button:

```bash
ddcutil -d 1 setvcp 0xF6 x00
```

`0xFD` is best left alone too: it declares no values, spans 16 bits, and there
is no hypothesis about what it does.

---

## Two things worth knowing before you use it

**Going full screen on an input bound to the KVM takes the keyboard and mouse
with it.** In full screen the peripherals follow the video input, and this
cannot be prevented over DDC.

**Pairing with a 60 Hz source drops the whole panel to 60 Hz**, not just that
window. The UI shows the refresh rate in its header for exactly this reason.

---

## Documentation

**[`REGISTERS.md`](REGISTERS.md)** is the full reference: the register map,
what does not work and why, the traps this firmware sets, and an appendix of the
hypotheses that turned out to be wrong.

**[`reference/`](reference/)** holds the primary sources it was built from, in
decreasing order of authority:

| File | What it is |
|---|---|
| `artery-ddc-trace.log` | A capture of Lenovo's own utility talking to the monitor: every VCP read and write it issues. The strongest evidence here |
| `lenovo-vcp-codes.json` | Lenovo's generic VCP definition table, shipped with that utility. Names most of the manufacturer-reserved codes. **Lenovo's file, not ours** — see `LICENSE` |
| `capabilities-string.txt` | What this monitor declares about itself — **wrong in both directions**, see `reference/README.md` |

### If you are an AI agent working in this repository

Read this before touching the monitor:

1. **Evidence hierarchy.** The Artery trace outranks Lenovo's table, and the
   table outranks anything deduced from diffs. The firmware's capabilities
   string **lies in both directions**: it declares values that do not exist
   (`0xEC`, `0xF5`) and omits registers that work fine (`0x62`, and the KVM
   sub-indices `0x0107`/`0x0207`).
2. **A negative result is only valid together with its conditions.** Half a
   dozen registers were wrongly dismissed here because they were tested in
   setups where the effect was invisible by construction: window swaps with the
   same source in both windows, layout changes with the other machines powered
   off, Over Drive judged on a still image when it only shows in motion. Before
   writing "does not work", ask whether your setup **could have shown** the
   effect at all.
3. **A layout change takes up to ~10 s** and the monitor stops answering DDC
   while it recomposes. Never wait a fixed time: wait until two consecutive
   reads agree (`r45w.wait_until_stable()`).
4. **Some state cannot be read.** The OSD's `PIP/PBP → Audio source` setting is
   neither readable nor writable over DDC — not even Lenovo's own tool can touch
   it. Do not assert anything about it.
5. **The I2C bus does not tolerate concurrency.** Everything goes through
   `flock` on the same lockfile, shared by the CLI, the service and `ddc-snap`.

---

## Hardware notes

The monitor exposes four inputs: DisplayPort, USB-C (DP Alt Mode + 75 W PD) and
two HDMI ports. Both HDMI ports are **HDMI 2.1 TMDS**, meaning no FRL, so they
top out where HDMI 2.0 does: 5120x1440 at 165 Hz needs ~31 Gbps, and over HDMI
you only get 60–75 Hz. Only DisplayPort and USB-C reach 165 Hz.

In PBP each window gets 2560x1440.

The built-in KVM only knows **two** PCs, each binding one video input to one USB
upstream. Inputs not bound to a slot never receive the peripherals. The bindings
are readable and writable — see `REGISTERS.md`.

---

## Status and credits

Everything documented here was **measured against a real monitor**, not inferred
from spec sheets. Anything that could not be verified is marked as such.

The only prior work we found on this model is
[lenovo-r45w30-pbp-switcher](https://github.com/piotr-kijowski/lenovo-r45w30-pbp-switcher),
ControlMyMonitor scripts to toggle PBP. It agrees with our findings on `0xF5`
and `0x60`, and it is what prompted us to re-test three registers we had
dismissed using the wrong values.

---

## License

MIT — see [`LICENSE`](LICENSE). Do what you like with it; keep the copyright
notice; there is no warranty.

One exception, spelled out in that file: `reference/lenovo-vcp-codes.json` is
Lenovo's own data file, reproduced unmodified for reference. All rights to it
remain with Lenovo.

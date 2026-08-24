# Register reference — Lenovo Legion R45w-30

Everything here was measured against a real monitor over DDC/CI, on Linux with
`ddcutil`. Where something could not be verified, it says so.

Product code `0x67b1`, MCCS 2.2, firmware (`0xC9`) `0x0101`.

> **Evidence hierarchy.** When sources disagree, believe them in this order: a
> trace of Lenovo's own utility talking to the monitor, then Lenovo's generic
> VCP table, then anything deduced by diffing register dumps.
>
> **The firmware's capabilities string lies in both directions.** It declares
> values that do not exist (`0xEC` accepts only 0 and 1 of the six it lists;
> `0xF5` implements 2 of 15) and omits registers that work perfectly well
> (`0x62` speaker volume, and the KVM sub-indices `0x0107`/`0x0207`).

---

## Writable

| Register | Function | Values |
|---|---|---|
| `0x60` | **Both windows**, 16-bit: low byte = left, high byte = right | `0F` DisplayPort · `31` USB-C · `11` HDMI-1 · `12` HDMI-2 |
| `0xF5` | **Layout** | `0x0000` full screen · `0x0101` PBP 50/50 · `0x0401` PBP 2/3+1/3 |
| `0xF2` | `0x01` swaps the windows (toggles) · `0x00` turns PBP off | |
| `0xD6` | Power | `0x01` on · `0x04` standby · `0x05` full off |
| `0xF9` | Picture mode | `0x0A` Standard · `01` FPS1 · `02` FPS2 · `03` Racing · `04` RTS · `05` Game1 · `06` Game2 |
| `0x10` `0x12` | Brightness, contrast | 0-100 |
| `0x62` | Speaker volume — **not declared in capabilities**, works anyway | 0-100 |
| `0x87` | Sharpness | 0-4 |
| `0x72` | Gamma — **value in the HIGH byte** | `0x50` 1.8 · `0x64` 2.0 · `0x78` 2.2 · `0x8C` 2.4 · `0xA0` 2.6 |
| `0x14` | Color preset | `01` sRGB · `05` 6500K · `06` 7500K · `08` 9300K · `0B` User 1 |
| `0x16` `0x18` `0x1A` | RGB gains | 0-100 · touching one switches `0x14` to User 1 |
| `0xE0` | Over Drive (panel response) | `00` off · `03`-`06` levels |
| `0xEF` | HDR | `00` off · `01` `06` `07` `08` `09` |
| `0x86` | Scaling | `02` aspect ratio · `05` fill vertically |
| `0xEC` | USB charging | `00` / `01` |
| `0xF8`+`0xF7` | **KVM and other indexed features** — see below | |

## Read only

| Register | What it reports |
|---|---|
| `0xF4` | Real layout state: high byte = mode (`0` full, `1` PIP, `2` PBP), low byte = swap flag |
| `0xED` | KVM state: `0x0000` peripherals on USB-C upstream, `0x0010` on USB-B |
| `0xAC` `0xAE` | Horizontal / vertical timing **of the secondary window** |
| `0xC9` | Firmware version |

## Declared but inert

Accept writes without error and do nothing: `0xEA` (dynamic contrast), `0xF0`
(Audio Source), `0xA5` (Window Select), and the `0xF8` indices for overclock
(`0x1A`), True Split (`0x15`) and DP Select (`0x0B`).

`0xF4` and `0xED` also reject writes — they are status registers. Writing `0xED`
additionally leaves the monitor's internal KVM state out of sync with reality,
requiring two presses of the physical button to recover.

## ⚠ Do not write

**`0xF6` = `0x07` locks the monitor** — the screen shows *"The monitor is
locked, please contact with the administrator"*. Recover over DDC, no physical
button needed:

```bash
ddcutil -d 1 setvcp 0xF6 x00
```

**`0xFD`** declares no values, spans 16 bits and has no hypothesis behind it.

---

## `0x60` — both windows in one register

```
0x60 = <high byte: right window><low byte: left window>

ddcutil -d 1 setvcp 0x60 x310f     # left DisplayPort, right USB-C
ddcutil -d 1 -v getvcp 0x60        # -> sh=0x31, sl=0x0f
```

In full screen only the low byte matters. In PBP the left window is always the
primary one.

> **`ddcutil` discards the high byte when formatting.** `getvcp --terse 0x60`
> returns `SNC x0f` because it treats the feature as a single-byte value. The
> full value **is** read — it is in the `-v` output as `sh=`/`sl=`. Every read
> in this project goes through `-v` for that reason.

## `0xF5` — the layout

| Byte | Meaning |
|---|---|
| low | `00` turns multi-window off (**the high byte is irrelevant**); anything else keeps it on |
| high | PBP ratio: **only `1` (50/50) and `4` (2/3 + 1/3) exist** |

It declares five values for the high byte and implements two. Any unrecognised
value is a **no-op**: from PBP it stays in PBP, from full screen it stays full
screen. `0xF5` is readable and holds the current layout; `0xF4` does not
distinguish PBP ratios, so read `0xF5` to tell 50/50 from 2/3+1/3.

**There is no way to enter PIP by command.** All 15 values were tried from both
full screen and PBP.

## `0xF2` — swapping the windows

It is a **command, not a state**: it keeps whatever was last written and does
not reflect whether the windows are swapped. Writing `0x01` toggles the swap
regardless of the register's current value — two writes of the same value swap
twice. The real state is the low byte of `0xF4`.

The monitor **ignores a swap that would change nothing**, i.e. when both windows
show the same source.

Audio follows the windows, so this is also how you move the sound.

## `0xF8` + `0xF7` — index and value

Several features have no register of their own. Write an **index** to `0xF8`,
then read or write the **value** in `0xF7`.

| Index | Feature | Writable |
|---|---|---|
| `0x07` | KVM enable | yes (`0x00`/`0x01`; the declared `0x02` is rejected) |
| `0x08` | Switch KVM | yes (`0x01`, toggles; state visible in `0xED`) |
| `0x0107` / `0x0207` | Video↔USB binding for PC1 / PC2 | yes — **not declared in capabilities** |
| `0x01` `0x02` `0x04` `0x0B` `0x0D` `0x15` `0x1A` | Sync tech, panel size, charge power, DP select, USB mode, True Split, overclock | read only |

**Rules, taken from the Artery trace:**

- `0xF8` cannot be read back, so writes need `--noverify` — verification would
  always fail. **An interleaved read unlatches the index**: read `0xF7` too soon
  and you get the previous index's value, which looks like real data.
- ~150 ms between `0xF8` and `0xF7`; ~3 s before reading back.
- Require **two matching reads** before trusting an indexed value.

### KVM bindings

Format: `<high byte: USB upstream><low byte: video input>`. Video codes are the
same as `0x60`; USB codes follow `0xED` (`0x31`+ USB-C, `0x51`+ USB-B).

Both slots are coupled and **the last pair written always ends up in PC1**, so
to set a specific configuration write PC2 first. Artery always writes both, ~120
ms apart. The semantics of slot PC2 were not fully pinned down: one write there
swapped the two slots instead of setting the requested value.

Inputs not bound to a slot never receive the peripherals.

---

## What has no register at all

**Audio source.** `0xF0` is named *Audio Source* in Lenovo's table and declared
by the firmware with all four ports, but it is inert. Closed with three
independent lines of evidence: an OSD diff that moved no readable register; six
writes including three with two machines actually playing sound; and five
minutes of Artery traffic with **zero** writes to `0xF0` — Lenovo's own tool
does not even offer the control for this model.

The `PIP/PBP → Audio source` OSD setting (Main/Sub) decides whether the left or
the right window owns the sound. It cannot be read or written over DDC. **It is
the only corner of this monitor that stays blind**: if someone changes it, no
software can tell.

---

## Behaviour that will bite you

- **A layout change takes up to ~10 s**, and the monitor stops answering DDC
  while it recomposes. Never wait a fixed time — wait until two consecutive
  reads agree. A failed read looks a lot like "no effect".
- **Unreadable registers echo the previous read instead of erroring.** A
  sequential dump makes each opaque register inherit its predecessor's value.
  Known echoes: `0x20`, `0x30`, `0xF0`, `0xF1`, `0xF3`. Unsupported `0xF8`
  indices return `0x0200`/`0x0201`. Defend with a canary read (`0xCC`).
- **`ddcutil` reports write rejections on stderr.** Silencing it makes an
  ignored write look successful.
- **On registers that echo, `ddcutil`'s verification always fails** and means
  nothing. `DDCRC_REPORTED_UNSUPPORTED` is different: that is the monitor
  actively denying the feature (`0xF8` responds this way to some indices).
- **`ddcutil` refuses `setvcp 0xA4`** with `Invalid hex value`, because MCCS 2.2
  classes it as a Table feature. `--mccs 2.0` works around it. When you see that
  error, suspect the tool before the firmware.
- **Cost is per invocation, not per feature**: one feature ~1.0 s, four ~0.73 s,
  fifteen ~2.3 s. Batch every read into one call.
- **The I2C bus does not tolerate concurrency.** Serialise with a lock.
- **The I2C bus number is not stable across boots.** Select by display number
  (`-d 1`) or by serial, never by device path.
- **`ddcutil` writes to the system journal on its own**, regardless of whether
  the caller captures stderr. `--syslog NEVER` stops it: measured 0 entries
  versus 5 per 5 reads.
- **The `Failed to find connector name` warning is harmless and unavoidable**:
  the EDID changes when the PBP recomposes. `--sn` does not fix it.
- **The EDID change can leave the host in a fallback video mode.** After several
  command-driven layout changes, GNOME dropped from 2560x1440 to 1920x1080
  without warning. The monitor is fine; the host is what gets confused.

## Operational consequences

- **Going full screen on an input bound to the KVM takes the keyboard and mouse
  with it.** In full screen the peripherals follow the video input. This happens
  identically with the KVM binding setting enabled or disabled — that setting
  (`0xF8`=`0x07`) proved inert in all three tested configurations.
- **A 60 Hz source in either window drops the whole panel to 60 Hz**, including
  the other window. Verified from both sides: `0xAE` reads 6000 and the host
  reports 59.951 Hz.
- **DDC survives the controlling machine leaving the screen.** The channel is
  tied to the physical connection, not to the input being displayed, so a
  machine can reconfigure the monitor blind after taking itself off screen.
- **The monitor keeps answering DDC while powered off**, so it can be turned
  back on by command.

---

## Host setup (Linux)

Access to the I2C bus is the part that most often breaks, and it fails in a way
that looks like the monitor's fault.

- **`i2c-dev` may be built into the kernel** (`CONFIG_I2C_CHARDEV=y`) rather
  than being a module. If so, any `modules-load.d` entry for it does nothing.
- **Join the `i2c` group.** The udev rules shipped with `ddcutil` tag the device
  `uaccess`, which grants an ACL **to the user of the active local session
  only** — `logind` revokes it on logout. A service that outlives the graphical
  session then loses the bus. The group is what makes access persistent:

  ```bash
  sudo usermod -aG i2c "$USER"
  ```

- **With `Linger=yes`, the `systemd --user` manager does not pick up group
  changes without a reboot.** It keeps the credentials it started with, so a
  service it launches inherits the old group set. Verify with:

  ```bash
  grep ^Groups: /proc/$(systemctl --user show -p MainPID --value ddc-web.service)/status
  ```

  The `i2c` gid must appear there. Tested: with it, the service survives a full
  logout and keeps serving from another device.
- **The I2C bus number is not stable across boots** (`/dev/i2c-9` one day,
  `/dev/i2c-11` the next). Select by display number or serial, never by path.
- **Which port you control from does not matter to `ddcutil`.** DDC/CI travels
  on the video link, so the machine driving the monitor uses whichever cable it
  is plugged into. Everything documented here was measured over DisplayPort with
  NVIDIA's proprietary driver; HDMI and USB-C are expected to behave the same
  but were not tested. The physical difference — AUX-multiplexed I2C on
  DisplayPort and USB-C versus dedicated pins on HDMI and DVI — only matters if
  you want to sniff the bus with hardware.

### Performance

Cost is dominated by process start-up, not by the number of features:

| Read | Time |
|---|---|
| 1 feature | ~1.0 s |
| 4 features | ~0.73 s |
| 15 features | ~2.3 s |
| 39 features | ~4.4 s |

So batch everything into a single invocation. Index-based reads (`0xF8`/`0xF7`)
cannot be batched: each needs its own latch and settle, ~1.5 s apiece.

---

## Appendix — how this was found, and what was wrong along the way

The register map was built by diffing full VCP dumps: dump, change exactly one
thing on the monitor's OSD, dump again, compare. That found most of it. What it
could not find were the features behind `0xF8`/`0xF7`: without latching an
index, `0xF7` means nothing, so no single-setting change ever moves it. Those
came from Lenovo's own VCP table and from tracing its Artery utility on
Windows.

### Hypotheses that turned out to be false

| Believed | Actually |
|---|---|
| `0xF0` is the secondary window's input | Named *Audio Source* by Lenovo, and inert |
| `0xE0` is the PBP mode register | It is Over Drive. The layout lives in `0xF5` |
| `0x95`-`0x98` are usable window geometry | Inert; never reflect the real layout |
| The secondary window cannot be read or written | Both, via the high byte of `0x60` |
| `0x60` ignores its high byte | That byte *is* the right window |
| `0xED` is the KVM control | It is the KVM's status register |
| `0xF4` encodes the layout, so writing it changes the layout | `0xF4` is read only; `0xF5` is the writable side |
| Peripheral and audio routing is not exposed at all | The KVM is, via `0xF8`/`0xF7`. Only audio is not |
| Sharpness and gamma are declared but ignored | Both work — the tests used an out-of-range value and the wrong byte |

### Methodological traps, each of which produced a false conclusion

1. **Trusting the tool's formatting instead of the raw value.** The most
   expensive mistake here. `ddcutil` truncated `0x60` to one byte, and three
   elaborate mechanisms were built to route around a limit that did not exist.
2. **Testing an effect in a setup where it was invisible by construction.**
   Window swaps with the same source in both windows change nothing observable —
   not visually, and not in any register. A whole batch of negatives had to be
   thrown away. Over Drive was likewise dismissed while looking at a still
   image, when it only affects motion.
3. **Testing with the other machines powered off.** With no signal on the second
   input the monitor collapses to full screen, making "it was rejected" and "it
   had nothing to show" indistinguishable.
4. **Waiting a fixed time after a write.** Mid-transition reads look like "no
   effect". The signature is an operation that appears to work once and never
   reproduces.
5. **Not verifying the current mode before each test.** Several swap tests ran
   in full screen, where there is no second window. The symptom is a perfect,
   entirely false negative.
6. **Reading a register that cannot distinguish what you are testing.** `0xF4`
   reports "PBP" for both ratios, so a sweep watching only `0xF4` changed the
   ratio without noticing.
7. **Asserting state that cannot be read.** Any claim about the secondary window
   was a guess until `0x60`'s high byte was understood.
8. **Trusting the capabilities string.** It both over- and under-declares.
9. **Silencing stderr**, which hides write rejections.

### Timing telemetry that is readable but not trustworthy as identity

`0xAC` and `0xAE` report the **secondary window's** timing, which is a genuine
readable signal about that window. It was measured across all nine ordered pairs
of three sources: for a given right-hand input, `0xAC` returns exactly the same
value regardless of what is on the left.

It is **not** usable to identify which machine is on the right, and the reason
matters: it encodes *timings*, not *identities*. Two machines outputting the
same refresh rate are indistinguishable, and anyone changing a resolution
silently breaks the mapping. An inference that fails silently is worse than no
inference — so the tools read the right window from `0x60` instead, which is
authoritative.

### Prior art and references

- [lenovo-r45w30-pbp-switcher](https://github.com/piotr-kijowski/lenovo-r45w30-pbp-switcher)
  — the only prior work found on this model: ControlMyMonitor scripts to toggle
  PBP. Agrees on `0xF5` and `0x60`. It is what prompted re-testing `0xA4`,
  `0xF4` and `0xA5`, which had been dismissed using single-byte values.
- [ddcutil: reverse engineering proprietary DDC extensions](https://www.ddcutil.com/sniffing/)
  — the canonical method is sniffing the I2C bus with an
  [I2CDriver](https://www.crowdsupply.com/excamera/i2cdriver). Note that it only
  works over HDMI or DVI: DisplayPort and USB-C multiplex I2C over the AUX
  channel.
- [ScriptGod1337/kvm](https://github.com/ScriptGod1337/kvm) — hooks a vendor's
  own utility to log its DDC calls. Built for Dell, but the technique is what
  produced this project's Artery trace.
- Manufacturer-reserved VCP codes live in the `0xE0`-`0xFF` range. Lenovo's
  utility is *Lenovo Display Control Center*, distributed as **Artery**. It
  ships a `VCPCodeDef.json` that is generic across Lenovo models and names most
  of the reserved codes; it installs under
  `C:\Program Files (x86)\Lenovo\LenovoDisplayControlCenterService\`.

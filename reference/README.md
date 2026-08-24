# Reference material

Primary sources behind [`../REGISTERS.md`](../REGISTERS.md). When something in
the register map looks wrong, check these before re-testing on hardware — and
believe them in this order.

## `artery-ddc-trace.log`

**The strongest evidence in this project.** A capture of Lenovo's own utility
(*Lenovo Display Control Center*, distributed as **Artery**) talking to the
monitor: every VCP read and write it issues, with register, value and order.

Produced on Windows by hooking `SetVCPFeature`/`GetVCPFeatureAndVCPFeatureReply`
in `dxva2.dll` — the same public API any DDC tool uses, so a user-space hook was
enough. `MONITORCONTROL.dll` inside Artery imports from there rather than going
through a private driver path.

This is what settled several questions that hardware testing could not:

- The KVM bindings are always written **in pairs**, ~120 ms apart, and the last
  pair written ends up in PC1. Ordering matters and is not documented anywhere.
- `0x60` is written as a 16-bit value (`0x110F`, `0x310F`) — independent
  confirmation that it carries both windows.
- **Zero writes to `0xF0` in five minutes of driving the audio UI**, which is
  what closed the question of whether audio has a register. It does not.

## `lenovo-vcp-codes.json`

Lenovo's own VCP definition table, `VCPCodeDef.json`, shipped with Artery and
found at
`C:\Program Files (x86)\Lenovo\LenovoDisplayControlCenterService\`.

It is **generic across Lenovo models** (`"model": "*"`, version 1.7), which cuts
both ways: it gave names to six registers that were anonymous here — `0xF0`
Audio Source, `0xF7`/`0xF8` Monitor Features and Feature Index, `0xFA` Mutex
Test, `0xA4` Window Mask Control, `0xFB` Gaming Dashboard — but its names do not
always fit a specific model. It calls `0xF4` "PIP Size"; on the R45w-30 that
register is layout mode plus swap flag, and nothing to do with size.

Use it to generate hypotheses, then verify against the hardware.

## `lenovo-app-enable-keys.json`

`AppEnableKeyDef.json`, also from Artery. Decodes the feature bitmap that
monitors report in `0xC6` (Application enable key), i.e. which features a given
unit claims to support.

## `capabilities-string.txt`

What this specific monitor answers to a capabilities request: the VCP codes it
declares and the values it says it accepts.

**Treat it as a hint, not as truth.** It is wrong in both directions on this
model:

- Declares values that do not exist — `0xEC` lists six and accepts two; `0xF5`
  lists fifteen combinations and implements two.
- Omits registers that work perfectly well — `0x62` (speaker volume) and the KVM
  sub-indices `0x0107`/`0x0207` are absent yet fully functional.

## What is not here

The dumps and scripts used while mapping the monitor are working material, not
reference, and are kept out of the repository. What they produced is in
`REGISTERS.md`.

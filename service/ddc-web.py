#!/usr/bin/env python3
"""
ddc-web - web interface on top of lib/r45w.py.

There is not a single VCP code in this file: all monitor logic lives in the
core, and this is an HTTP layer. Adding a control is done in lib/r45w.py and it
shows up here and in the CLI on its own.

What this layer does solve, because it is specific to serving several clients:

  - The I2C bus is slow and does not tolerate concurrency, so requests are
    queued and applied from one worker thread, coalesced per control (out of
    three rapid clicks only the last one is applied).
  - POST answers in microseconds; the change is applied in the background.
  - State is cached and refreshed while someone is watching, so the interface
    also reflects whatever is changed with the monitor joystick.
  - Index-based reads (KVM, overclock...) cost a round trip each, so they are
    refreshed about once a minute instead of on every poll.
"""

import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))
import r45w  # noqa: E402

HOST = os.environ.get("DDC_WEB_HOST", "0.0.0.0")
PORT = int(os.environ.get("DDC_WEB_PORT", "8081"))
POLL = 10
CLIENT_WINDOW = 60
EVERY_INDEXED = 6          # refresh cycles between index-based reads


class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.values = {}
        self.applying = None
        self.error = None
        self.cycles = 0

    def refresh(self, force_indexed=False):
        with self.lock:
            self.cycles += 1
            with_indexed = force_indexed or self.cycles % EVERY_INDEXED == 0
        try:
            fresh = r45w.read(indexed=with_indexed)
        except r45w.MonitorError as e:
            with self.lock:
                self.error = str(e)
            return
        with self.lock:
            self.error = None
            if not with_indexed:
                # Keep whatever was last read by index instead of blanking it:
                # it rarely changes and re-reading it every 10 s is expensive.
                for k, v in fresh.items():
                    if v["value"] is None and self.values.get(k, {}).get("value") is not None:
                        fresh[k] = self.values[k]
            self.values = fresh

    def install(self, fresh):
        """Adopt a state that was already read, instead of reading again."""
        with self.lock:
            self.error = None
            for k, v in fresh.items():
                if v["value"] is None and self.values.get(k, {}).get("value") is not None:
                    fresh[k] = self.values[k]
            self.values = fresh

    def patch(self, key, value):
        """Record a value the monitor has already confirmed.

        ddcutil verifies an ordinary write itself, so re-reading the monitor to
        learn what we just told it is a round trip that buys nothing."""
        c = r45w.BY_KEY[key]
        with self.lock:
            self.values[key] = {"value": value, "text": r45w.text_of(c, value),
                                "max": c.get("max")}

    def refresh_index(self, key):
        c = r45w.BY_KEY[key]
        try:
            val = r45w.read_index(int(c["read"].split(":")[1], 16))
        except r45w.MonitorError:
            return
        with self.lock:
            self.values[key] = {"value": val, "text": r45w.text_of(c, val),
                                "max": c.get("max")}

    def snapshot(self):
        with self.lock:
            vals, err, applying = dict(self.values), self.error, self.applying
        with requests:
            flying = sorted(inflight)
        aliases = r45w.load_aliases()
        controls = []
        for c in r45w.CONTROLS:
            e = vals.get(c["key"], {})
            item = {k: c[k] for k in ("key", "name", "type", "group")}
            item.update(value=e.get("value"), text=e.get("text", "-"),
                        help=c.get("help"), writable=bool(c["write"]),
                        slow=bool(c.get("slow")))
            if c["type"] == "range":
                item["max"] = c.get("max", 100)
            elif c["type"] == "enum":
                item["options"] = [{"txt": o["txt"], "sub": o.get("sub"),
                                    "val": o["val"], "state": o.get("state", o["val"])}
                                   for o in r45w.options_of(c, aliases)]
            elif c["type"] == "action":
                item["write_value"] = c["value"]
            controls.append(item)
        return {"controls": controls, "groups": r45w.GROUPS,
                "layout": vals.get("_layout", {}).get("text", "unknown"),
                "applying": applying, "inflight": flying, "error": err}


state = State()
requests = threading.Condition()
pending = {}
inflight = set()           # queued or being applied; guarded by `requests`
last_client = 0.0


def worker():
    global pending
    # A quick read first so the interface paints straight away, then the full
    # one: the index reads are five round trips and take ~20 s.
    state.refresh()
    state.refresh(force_indexed=True)
    while True:
        with requests:
            if not pending:
                requests.wait(timeout=POLL)
            work, pending = pending, {}
        if not work:
            if time.time() - last_client < CLIENT_WINDOW:
                state.refresh()
            continue
        # Layout first: if a mode change and a source change arrive together,
        # the source must be applied on the new layout.
        for key in sorted(work, key=lambda k: 0 if k == "mode" else 1):
            c = r45w.BY_KEY[key]
            with state.lock:
                state.applying = key
            try:
                err = r45w.write(key, work[key], previous=state.values or None)
                if err:
                    with state.lock:
                        state.error = err
                    state.refresh()
                elif c.get("slow"):
                    # wait_until_stable ends holding a settled read; asking the
                    # monitor again for the same thing is a third round trip.
                    settled = r45w.wait_until_stable()
                    if settled:
                        state.install(settled)
                    else:
                        state.refresh()
                    # Only the KVM enable flag lives behind the index; the
                    # peripheral side is a plain register and came with the read.
                    if c["read"].startswith("kvm:"):
                        state.refresh_index(key)
                else:
                    # ddcutil verified this write itself. There is nothing left
                    # to find out, so do not spend a read finding it out.
                    state.patch(key, work[key])
            finally:
                with state.lock:
                    state.applying = None
                with requests:
                    inflight.discard(key)


# RAW string on purpose: it carries JavaScript, and a \n written here would be
# turned into a real newline by Python, breaking the JS string literal.
PAGE = r"""<!doctype html>
<html lang="en" data-theme="dark">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="color-scheme" content="dark light">
<title>R45W</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Barlow+Condensed:wght@500;600&family=Barlow:wght@400;500;600&family=Roboto+Mono:wght@400;500&display=swap">
<style>
/* Dark is the working default: this panel is read next to the screen it
   drives, often at night. Light is a deliberate switch, not the OS's call. */
:root{
  --bg:#080B0D; --sf:#12181C; --sf2:#0D1215; --ink:#DFE6EA; --dim:#78868E;
  --ln:#28323A; --hot:#F0A93B; --hot-bg:#2A2113; --off:#161D22;
}
:root[data-theme="light"]{
  --bg:#E4E7E9; --sf:#F7F8F9; --sf2:#D5DADD; --ink:#12171A; --dim:#4B565C;
  --ln:#B9C2C7; --hot:#854D06; --hot-bg:#F2E4CC; --off:#E9ECEE;
}
*{box-sizing:border-box}
[hidden]{display:none!important}
html,body{margin:0}
body{
  background:var(--bg); color:var(--ink);
  font:400 15px/1.45 Barlow,system-ui,sans-serif;
  -webkit-text-size-adjust:100%;
  padding:0 14px calc(28px + env(safe-area-inset-bottom));
}
.cap{
  font:600 11px/1 "Barlow Condensed","Roboto Condensed",system-ui,sans-serif;
  letter-spacing:.16em; text-transform:uppercase;
}
.mono{font-family:"Roboto Mono",ui-monospace,SFMono-Regular,Menlo,monospace;
  font-variant-numeric:tabular-nums}
.wrap{max-width:980px;margin:0 auto}

/* header ------------------------------------------------------------- */
header{
  display:flex;align-items:baseline;gap:12px;
  padding:20px 0 14px;border-bottom:1px solid var(--ln);margin-bottom:16px;
}
.brand{font:600 19px/1 "Barlow Condensed","Roboto Condensed",system-ui,sans-serif;
  letter-spacing:.2em;text-transform:uppercase}
.model{color:var(--dim);flex:1;min-width:0;
  font:400 12px/1 Barlow,system-ui,sans-serif;letter-spacing:.04em;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#theme{
  background:none;border:1px solid var(--ln);color:var(--dim);
  padding:9px 11px;cursor:pointer;font:inherit;line-height:1;border-radius:1px;
}
#theme:hover{color:var(--ink);border-color:var(--dim)}
#alert{
  border:1px solid var(--hot);background:var(--hot-bg);color:var(--ink);
  padding:9px 12px;margin-bottom:14px;font-size:13px;
}
#alert[hidden]{display:none}

/* panels -------------------------------------------------------------- */
.panel{background:var(--sf);border:1px solid var(--ln);border-radius:2px}
.grid{display:grid;gap:12px;grid-template-columns:1fr}
@media (min-width:760px){.grid{grid-template-columns:1fr 1fr}}
.layout{padding:14px;margin-bottom:12px}

/* layout: shape picker ------------------------------------------------ */
.shapes{display:flex;gap:8px;margin-bottom:14px}
.shape{
  flex:1;background:var(--sf2);border:1px solid var(--ln);color:var(--dim);
  padding:9px 6px 7px;cursor:pointer;border-radius:1px;
  display:flex;flex-direction:column;align-items:center;gap:7px;
}
.shape:hover{border-color:var(--dim);color:var(--ink)}
.shape[aria-pressed="true"]{border-color:var(--hot);color:var(--hot);background:var(--hot-bg)}
.shape[disabled]{cursor:default;opacity:.9}
.shape .cap{white-space:nowrap}
.glyph{display:flex;gap:2px;width:56px;height:16px}
.glyph i{background:currentColor;opacity:.45;display:block}
.shape[aria-pressed="true"] .glyph i{opacity:1}
.glyph.pip{position:relative}
.glyph.pip i:last-child{position:absolute;right:0;top:0;width:34%;height:55%}

/* layout: stage ------------------------------------------------------- */
.stage{
  aspect-ratio:32/9;display:flex;gap:1px;background:var(--ln);
  border:1px solid var(--ln);border-radius:1px;overflow:hidden;
}
.pane{
  background:var(--sf2);min-width:0;overflow:hidden;border:0;
  display:flex;flex-direction:column;align-items:center;justify-content:center;
  gap:2px;padding:6px;text-align:center;cursor:pointer;
  font:inherit;color:inherit;
  transition:flex-basis .35s ease;
}
.pane.primary{box-shadow:inset 0 2px 0 var(--hot)}
.pane.picking{background:var(--hot-bg);box-shadow:inset 0 0 0 1px var(--hot)}
.pane.picking .side,.pane.picking .port{color:var(--hot)}
.pane .side{color:var(--dim)}
.pane.picking.primary{box-shadow:inset 0 0 0 1px var(--hot),inset 0 2px 0 var(--hot)}
.pane .who{
  font:500 16px/1.15 Barlow,system-ui,sans-serif;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:100%;
}
.pane .port{color:var(--dim);letter-spacing:.06em;
  font:400 12px/1 "Barlow Condensed","Roboto Condensed",system-ui,sans-serif;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:100%}
.pane[hidden]{display:none}
.stage.pip{position:relative;display:block}
.stage.pip .pane{position:absolute;inset:0;width:auto}
.stage.pip .pane.inset{
  inset:auto 8px 8px auto;width:26%;height:46%;border:1px solid var(--ln);
}
.stage.pip .pane.inset .who{font-size:13px}

/* layout: source picker ------------------------------------------------ */
/* One picker for both panes: it opens under the stage across the whole
   block, and closes as soon as a source is chosen. Sources are picked, never
   cycled, so one tap is one DDC write. */
#picker{display:none}
#picker.open{display:grid;gap:4px;grid-template-columns:1fr 1fr;margin-top:10px}
.opts{display:grid;gap:4px;grid-template-columns:1fr 1fr}
.opt{
  background:var(--sf2);border:1px solid var(--ln);color:var(--ink);
  padding:7px 8px;cursor:pointer;border-radius:1px;text-align:left;
  font:400 13px/1.2 Barlow,system-ui,sans-serif;min-width:0;
}
.opt span{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.opt .sub{color:var(--dim);font-size:11px;margin-top:2px}
.opt:hover{border-color:var(--dim)}
.opt[aria-pressed="true"]{border-color:var(--hot);color:var(--hot);background:var(--hot-bg)}
.opt[aria-pressed="true"] .sub{color:var(--hot)}
.swap{
  width:100%;margin-top:10px;background:var(--sf2);border:1px solid var(--ln);
  color:var(--ink);padding:10px;cursor:pointer;border-radius:1px;
}
.swap:hover{border-color:var(--dim)}
.swap[disabled]{color:var(--dim);cursor:default;opacity:.55}

/* sections ------------------------------------------------------------ */
details{background:var(--sf);border:1px solid var(--ln);border-radius:2px;
  align-self:start}
summary{
  list-style:none;cursor:pointer;padding:12px 14px;
  display:flex;align-items:baseline;gap:10px;
}
summary::-webkit-details-marker{display:none}
summary .cap{flex:1}
summary .lead{color:var(--dim);font-size:13px;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:55%}
summary .chev{color:var(--dim);font-size:11px;transition:transform .15s}
details[open] summary .chev{transform:rotate(90deg)}
details[open] summary{border-bottom:1px solid var(--ln)}
.body{padding:4px 14px 12px}
.row{padding:10px 0;border-bottom:1px solid var(--ln)}
.row:last-child{border-bottom:none}
.line{display:flex;align-items:baseline;gap:10px}
.line .nm{flex:1;min-width:0}
.line .vl{color:var(--hot);font-size:13px;text-align:right}
.row.ro .line .vl{color:var(--dim)}
.hint{color:var(--dim);font-size:12px;margin-top:3px}
.row .opts{margin-top:8px;grid-template-columns:1fr 1fr}
.act{
  background:var(--sf2);border:1px solid var(--ln);color:var(--ink);
  padding:6px 12px;cursor:pointer;border-radius:1px;font:inherit;font-size:13px;
}
.act:hover{border-color:var(--dim)}
input[type=range]{
  -webkit-appearance:none;appearance:none;width:100%;margin:6px 0 0;
  background:transparent;height:32px;
}
input[type=range]::-webkit-slider-runnable-track{height:2px;background:var(--ln)}
input[type=range]::-moz-range-track{height:2px;background:var(--ln)}
input[type=range]::-webkit-slider-thumb{
  -webkit-appearance:none;width:14px;height:14px;background:var(--hot);
  border:none;border-radius:1px;margin-top:-6px;
}
input[type=range]::-moz-range-thumb{
  width:14px;height:14px;background:var(--hot);border:none;border-radius:1px;
}
button:focus-visible,summary:focus-visible,input:focus-visible{
  outline:2px solid var(--hot);outline-offset:2px}
@media (prefers-reduced-motion:reduce){*{transition:none!important}}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div class="brand">R45W</div>
    <div class="model">Lenovo Legion R45w-30</div>
    <button id="theme" class="cap" type="button">Light</button>
  </header>

  <div id="alert" hidden></div>

  <section class="panel layout">
    <div class="shapes" id="shapes"></div>
    <div class="stage" id="stage">
      <button class="pane primary" type="button" data-side="left">
        <span class="side cap"></span><span class="who"></span><span class="port"></span>
      </button>
      <button class="pane" type="button" data-side="right">
        <span class="side cap"></span><span class="who"></span><span class="port"></span>
      </button>
    </div>
    <div id="picker"></div>
    <button class="swap" id="swap" type="button">Swap sides</button>
  </section>

  <div class="grid" id="sections"></div>
</div>

<script>
var THEME = "r45w-theme";
var root = document.documentElement, tbtn = document.getElementById("theme");
function paintTheme(t){
  root.setAttribute("data-theme", t);
  tbtn.textContent = t === "dark" ? "Light" : "Dark";
}
paintTheme(localStorage.getItem(THEME) || "dark");
tbtn.onclick = function(){
  var t = root.getAttribute("data-theme") === "dark" ? "light" : "dark";
  localStorage.setItem(THEME, t); paintTheme(t);
};

/* Widths of the three shapes, used for both the little glyphs and the stage,
   so the drawing is never a rough approximation of the real split. */
var SPLIT = {"full":[100,0], "pbp-1":[50,50], "pbp-4":[66.7,33.3], "pip":[100,0]};
var LEAD  = {};                    /* group -> control that summarises it */
var C     = {};                    /* key -> control, from the last snapshot */
var el    = {};                    /* key -> the nodes that show it */
var built = false, snap = null, busy = false;
var picking = null;                /* side whose picker is open, if any */

function $(t, cls, txt){
  var n = document.createElement(t);
  if (cls) n.className = cls;
  if (txt != null) n.textContent = txt;
  return n;
}
function isActive(c, o){ return c.value === o.val || c.value === o.state; }

/* What we have asked for but the service has not confirmed yet. The screen
   shows these straight away: waiting for I2C to answer before moving a label
   makes a working monitor feel broken. */
var wanted = {};

function localise(key, value){
  var c = C[key];
  if (!c || c.type === "action") return;
  if (c.type === "range") { c.value = value; c.text = String(value); return; }
  (c.options || []).forEach(function(o){
    if (o.val !== value) return;
    c.value = o.state;                    /* mode reads back as a name, not a code */
    c.text = o.txt;
  });
  /* Full screen has no second window, so stop claiming one. */
  if (key === "mode" && c.value === "full" && C.right) C.right.value = null;
}

function send(key, value){
  wanted[key] = value;          /* actions too: they still take time to land */
  localise(key, value);
  busy = true;
  syncLayout(snap); syncSections(); paintAlert(snap);
  fetch("/api/control/" + key + "/" + value, {method:"POST"})
    .catch(function(){ delete wanted[key]; })
    .then(function(){ setTimeout(poll, 300); });
}

/* ---- layout block ---------------------------------------------------- */
function buildShapes(){
  var box = document.getElementById("shapes");
  box.textContent = "";
  C.mode.options.forEach(function(o){
    var b = $("button", "shape"); b.type = "button";
    var g = $("div", "glyph"), w = SPLIT[o.state] || [100, 0];
    var a = $("i"); a.style.flex = w[0]; g.appendChild(a);
    if (w[1]) { var s = $("i"); s.style.flex = w[1]; g.appendChild(s); }
    b.appendChild(g);
    b.appendChild($("div", "cap", o.txt));
    b.onclick = function(){ closePick(); send("mode", o.val); };
    box.appendChild(b);
    (el.shapes = el.shapes || []).push({node:b, opt:o});
  });
  /* PIP cannot be entered by command, only left. It still gets a chip, shown
     only while it is the live layout, so the picker never denies what is on
     screen. */
  var p = $("button", "shape"); p.type = "button"; p.disabled = true; p.hidden = true;
  var pg = $("div", "glyph pip");
  pg.appendChild($("i")); pg.firstChild.style.flex = 1; pg.appendChild($("i"));
  p.appendChild(pg); p.appendChild($("div", "cap", "PIP"));
  box.appendChild(p);
  el.pip = p;
}

function buildStage(){
  ["left", "right"].forEach(function(side){
    var pane = document.querySelector('.pane[data-side="' + side + '"]');
    pane.setAttribute("aria-expanded", "false");
    pane.onclick = function(){ openPick(side); };
  });
  document.getElementById("swap").onclick = function(){
    closePick(); send("swap", C.swap.write_value);
  };
}

function closePick(){
  picking = null;
  var box = document.getElementById("picker");
  box.classList.remove("open");
  box.textContent = "";
  [].forEach.call(document.querySelectorAll(".pane"), function(p){
    p.classList.remove("picking");
    p.setAttribute("aria-expanded", "false");
  });
}

function openPick(side){
  if (picking === side) return closePick();   /* tapping again puts it away */
  closePick();
  var c = C[side], box = document.getElementById("picker");
  c.options.forEach(function(o){
    var b = $("button", "opt"); b.type = "button";
    b.appendChild($("span", "txt", o.txt));
    b.appendChild($("span", "sub", o.sub || ""));
    b.setAttribute("aria-pressed", isActive(c, o) ? "true" : "false");
    b.onclick = function(){ closePick(); send(side, o.val); };
    box.appendChild(b);
  });
  box.classList.add("open");
  picking = side;
  var pane = document.querySelector('.pane[data-side="' + side + '"]');
  pane.classList.add("picking");
  pane.setAttribute("aria-expanded", "true");
}

function syncLayout(s){
  var live = C.mode.value, w = SPLIT[live] || [100, 0], pip = live === "pip";
  var two = w[1] > 0 || pip;      /* PIP draws as one pane, but it has two */
  el.shapes.forEach(function(x){
    x.node.setAttribute("aria-pressed", isActive(C.mode, x.opt) ? "true" : "false");
  });
  el.pip.hidden = !pip;
  el.pip.setAttribute("aria-pressed", pip ? "true" : "false");

  var stage = document.getElementById("stage");
  stage.classList.toggle("pip", pip);
  ["left", "right"].forEach(function(side, i){
    var pane = stage.querySelector('.pane[data-side="' + side + '"]');
    pane.querySelector(".side").textContent = two
      ? (side === "left" ? "Left" : "Right") : "Main";
    var c = C[side], on = i === 0 || two;
    pane.hidden = !on;
    pane.classList.toggle("inset", pip && i === 1);
    pane.style.flexBasis = w[i] + "%";
    var cur = null;
    c.options.forEach(function(o){ if (isActive(c, o)) cur = o; });
    pane.querySelector(".who").textContent = cur ? cur.txt : c.text;
    pane.querySelector(".port").textContent = cur && cur.sub ? cur.sub : "";

  });
  /* The right pane can vanish under you when the layout changes from the
     joystick; its picker must not outlive it. */
  if (picking === "right" && !two) closePick();

  /* The monitor ignores a swap when both windows show the same source, so
     the button says so instead of sending a command that does nothing. */
  var same = C.left.value === C.right.value;
  var sw = document.getElementById("swap");
  sw.disabled = !two || same;
  sw.textContent = two
    ? (same ? "Both sides show the same source" : "Swap sides")
    : "Swap sides needs two windows";
}

/* ---- collapsible sections -------------------------------------------- */
function buildRow(c){
  var row = $("div", "row" + (c.writable ? "" : " ro"));
  var line = $("div", "line");
  line.appendChild($("div", "nm", c.name));
  var vl = $("div", "vl mono", c.text);
  var refs = {row:row, value:vl};

  if (c.type === "action" && c.writable) {
    var b = $("button", "act", "Switch"); b.type = "button";
    b.onclick = function(){ send(c.key, c.write_value); };
    line.appendChild(vl); line.appendChild(b);
    row.appendChild(line);
  } else if (c.type === "range" && c.writable) {
    line.appendChild(vl);
    row.appendChild(line);
    var r = document.createElement("input");
    r.type = "range"; r.min = 0; r.max = c.max; r.value = c.value || 0;
    /* on change, not on input: every move would be a write on a slow bus */
    r.oninput  = function(){ vl.textContent = r.value; };
    r.onchange = function(){ send(c.key, +r.value); };
    row.appendChild(r);
    refs.range = r;
  } else if (c.type === "enum" && c.writable) {
    line.appendChild(vl);
    row.appendChild(line);
    var opts = $("div", "opts");
    refs.items = c.options.map(function(o){
      var b = $("button", "opt"); b.type = "button";
      var t = $("span", "txt", o.txt), s = $("span", "sub", o.sub || "");
      b.appendChild(t); b.appendChild(s);
      b.onclick = function(){ send(c.key, o.val); };
      opts.appendChild(b);
      return {node:b, opt:o};
    });
    row.appendChild(opts);
  } else {
    line.appendChild(vl);
    row.appendChild(line);
  }
  if (c.help && c.writable) row.appendChild($("div", "hint", c.help));
  return refs;
}

function buildSections(s){
  var box = document.getElementById("sections");
  box.textContent = "";
  el.groups = {};
  s.groups.forEach(function(g){
    var key = g[0], title = g[1];
    if (key === "layout") return;
    LEAD[key] = g[2];
    var cs = s.controls.filter(function(c){ return c.group === key; });
    if (!cs.length) return;
    var d = $("details"), sm = $("summary");
    sm.appendChild($("span", "cap", title));
    var lead = $("span", "lead mono");
    sm.appendChild(lead);
    sm.appendChild($("span", "chev", "›"));
    d.appendChild(sm);
    var body = $("div", "body");
    cs.forEach(function(c){ el[c.key] = buildRow(c); body.appendChild(el[c.key].row); });
    d.appendChild(body);
    box.appendChild(d);
    el.groups[key] = {node:d, lead:lead, keys:cs.map(function(c){ return c.key; })};
  });
}

function syncSections(){
  Object.keys(el.groups).forEach(function(key){
    var g = el.groups[key];
    var lead = LEAD[key] && C[LEAD[key]];
    g.lead.textContent = lead ? lead.text
      : g.keys.length + (g.keys.length === 1 ? " setting" : " settings");
    g.keys.forEach(function(k){
      var c = C[k], r = el[k];
      if (!r) return;
      r.value.textContent = c.text;
      if (r.range && document.activeElement !== r.range && c.value != null)
        r.range.value = c.value;
      if (r.items) r.items.forEach(function(x){
        x.node.setAttribute("aria-pressed", isActive(c, x.opt) ? "true" : "false");
      });
    });
  });
}

/* ---- polling ---------------------------------------------------------- */
function paintAlert(s){
  var a = document.getElementById("alert"), key = s && s.applying;
  if (!key && busy) key = Object.keys(wanted)[0];
  if (key) {
    var c = C[key];
    a.hidden = false;
    a.textContent = "Applying " + (c ? c.name : key) + "…"
      + (c && c.slow ? " The monitor goes dark while it recomposes the picture." : "");
  } else if (s && s.error) {
    a.hidden = false; a.textContent = s.error;
  } else {
    a.hidden = true;
  }
}

function apply(s){
  snap = s;
  C = {};
  s.controls.forEach(function(c){ C[c.key] = c; });
  /* The service says which keys are still queued or being applied. Until a key
     leaves that list its own value is stale, so keep showing what was asked
     for; once it leaves, the monitor is the authority again, even if that
     means snapping back because the write was refused. */
  var flying = s.inflight || [];
  Object.keys(wanted).forEach(function(k){
    if (flying.indexOf(k) < 0) delete wanted[k];
    else localise(k, wanted[k]);
  });
  if (!built) { buildShapes(); buildStage(); buildSections(s); built = true; }
  busy = flying.length > 0;
  syncLayout(s);
  syncSections();
  paintAlert(s);
}

var timer = null;
function poll(){
  clearTimeout(timer);
  fetch("/api/state").then(function(r){ return r.json(); }).then(apply)
    .catch(function(){
      var a = document.getElementById("alert");
      a.hidden = false; a.textContent = "No answer from the service.";
    })
    /* Polling is free while something is in flight: /api/state answers from
       cache and never touches the bus. */
    .then(function(){ timer = setTimeout(poll, busy ? 400 : 5000); });
}
poll();
document.addEventListener("visibilitychange", function(){
  if (!document.hidden) poll();
});
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, code, body, kind="application/json; charset=utf-8"):
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        global last_client
        if self.path in ("/", "/index.html"):
            self._send(200, PAGE, "text/html; charset=utf-8")
        elif self.path == "/api/state":
            last_client = time.time()
            self._send(200, json.dumps(state.snapshot()))
        else:
            self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        parts = self.path.partition("?")[0].strip("/").split("/")
        if len(parts) != 4 or parts[:2] != ["api", "control"]:
            return self._send(404, json.dumps({"error": "not found"}))
        key, text = parts[2], parts[3]
        c = r45w.BY_KEY.get(key)
        if not c:
            return self._send(400, json.dumps({"error": "unknown control"}))
        if not c["write"]:
            return self._send(400, json.dumps({"error": "read-only control"}))
        try:
            value = int(text, 0)
        except ValueError:
            return self._send(400, json.dumps({"error": "value is not a number"}))
        # Whitelist straight from the core: this is reachable from every device
        # on the tailnet.
        if value not in r45w.valid_values(c):
            return self._send(400, json.dumps({"error": "value not allowed"}))

        with requests:
            pending[key] = value
            inflight.add(key)
            requests.notify()
        self._send(202, json.dumps({"queued": key, "value": value}))

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    threading.Thread(target=worker, daemon=True).start()
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"ddc-web listening on http://{HOST}:{PORT}", flush=True)
    srv.serve_forever()

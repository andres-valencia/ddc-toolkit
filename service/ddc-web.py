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

    def snapshot(self):
        with self.lock:
            vals, err, applying = dict(self.values), self.error, self.applying
        aliases = r45w.load_aliases()
        controls = []
        for c in r45w.CONTROLS:
            e = vals.get(c["key"], {})
            item = {k: c[k] for k in ("key", "name", "type", "group")}
            item.update(value=e.get("value"), text=e.get("text", "-"),
                        help=c.get("help"), writable=bool(c["write"]))
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
                "applying": applying, "error": err}


state = State()
requests = threading.Condition()
pending = {}
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
                elif c.get("slow"):
                    r45w.wait_until_stable()
                else:
                    time.sleep(1)
                state.refresh(force_indexed=c["write"].startswith("kvm:"))
            finally:
                with state.lock:
                    state.applying = None


# RAW string on purpose: it carries JavaScript, and a \n written here would be
# turned into a real newline by Python, breaking the JS string literal.
PAGE = r"""<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Monitor</title>
<style>
  :root { color-scheme: light dark; --bg:#faf9f7; --fg:#1a1a19; --mut:#6b6a67;
          --bd:#dedcd8; --card:#fff; --ok:#2f7d4f; --err:#a33; --warn:#8a5a00; }
  @media (prefers-color-scheme: dark) {
    :root { --bg:#16171a; --fg:#e9e8e6; --mut:#9b9a97; --bd:#2e3034;
            --card:#1e2024; --ok:#6cc08b; --err:#e08585; --warn:#d9a441; }
  }
  * { box-sizing:border-box; -webkit-tap-highlight-color:transparent }
  body { margin:0; padding:1.25rem 1rem 3rem; background:var(--bg); color:var(--fg);
         font:16px/1.5 system-ui,-apple-system,sans-serif;
         max-width:34rem; margin-inline:auto }
  h1 { font-size:1.05rem; font-weight:600; margin:0 0 .25rem }
  .sub { color:var(--mut); font-size:.85rem; margin-bottom:1.25rem }
  h2 { font-size:.75rem; font-weight:600; text-transform:uppercase;
       letter-spacing:.06em; color:var(--mut); margin:1.75rem 0 .6rem }
  .ctl { margin-bottom:1.1rem }
  .lbl { display:flex; justify-content:space-between; align-items:baseline;
         font-size:.85rem; margin-bottom:.4rem; gap:1rem }
  .lbl b { font-weight:600 } .lbl span { color:var(--mut); text-align:right }
  .ops { display:grid; grid-template-columns:repeat(auto-fit,minmax(9rem,1fr)); gap:.5rem }
  button { padding:.85rem .9rem; font:inherit; font-size:.9rem; font-weight:600;
           text-align:left; background:var(--card); color:var(--fg);
           border:1px solid var(--bd); border-radius:.7rem; cursor:pointer }
  button:active { transform:scale(.99) }
  button[disabled] { opacity:.45; cursor:not-allowed }
  button.on { border-color:var(--ok); box-shadow:inset 0 0 0 1px var(--ok); color:var(--ok) }
  button small { display:block; font-weight:400; color:var(--mut); font-size:.75rem }
  input[type=range] { width:100%; accent-color:var(--ok); height:1.75rem }
  .err { border-left:3px solid var(--err); padding:.6rem .8rem; margin-bottom:1rem;
         background:var(--card); border-radius:.35rem; font-size:.85rem }
  .warn { border-left:3px solid var(--warn); padding:.6rem .8rem; margin:.5rem 0 0;
          background:var(--card); border-radius:.35rem; font-size:.8rem; color:var(--mut) }
  .head { background:var(--card); border:1px solid var(--bd); border-radius:.75rem;
          padding:.7rem .9rem; font-size:.85rem; display:flex;
          justify-content:space-between; gap:1rem; margin-bottom:.5rem }
  .head span { color:var(--mut) }
  .info { display:flex; justify-content:space-between; font-size:.85rem;
          padding:.2rem 0; gap:1rem }
  .info span { color:var(--mut) }
  details { margin-top:.5rem } summary { cursor:pointer; font-size:.85rem; color:var(--mut) }
  .note { color:var(--mut); font-size:.8rem; margin-top:2rem;
          border-top:1px solid var(--bd); padding-top:1rem }
  code { font-size:.9em }
</style>
<h1>Lenovo Legion R45w-30</h1>
<div class="sub">DDC/CI control</div>
<div id="app">Reading the monitor...</div>
<div class="note">
  Everything here also works from a terminal with <code>bin/r45w</code>: the web
  UI and the CLI share the same core. Input names come from
  <code>r45w alias</code>.<br><br>
  <b>Audio follows the window</b> and has no register of its own: swapping the
  windows moves the sound. Whether the left or the right window owns it is set
  in <i>PIP/PBP &rarr; Audio source</i> on the monitor's own menu, and that
  setting cannot be read or written over DDC.
</div>
<script>
const app = document.getElementById('app');
let dragging = null, open = {}, painted = null;

function ctlHTML(c, busy) {
  const help = c.help ? `<div class="lbl"><span>${c.help}</span></div>` : '';
  if (c.type === 'info' || !c.writable)
    return `<div class="info"><span>${c.name}</span><b>${c.text}</b></div>`;
  if (c.type === 'action')
    return `<div class="ctl"><div class="lbl"><b>${c.name}</b><span>${c.text}</span></div>
      ${help}<div class="ops"><button data-k="${c.key}" data-v="${c.write_value}"
        ${busy ? 'disabled' : ''}>${c.name}</button></div></div>`;
  if (c.type === 'enum') {
    const ops = c.options.map(o => `
      <button data-k="${c.key}" data-v="${o.val}"
        class="${o.state === c.value ? 'on' : ''}" ${busy ? 'disabled' : ''}>
        ${o.txt}${o.sub ? `<small>${o.sub}</small>` : ''}
      </button>`).join('');
    const none = c.value === null ? `<div class="lbl"><span>not readable right now</span></div>` : '';
    return `<div class="ctl"><div class="lbl"><b>${c.name}</b></div>
            ${none}${help}<div class="ops">${ops}</div></div>`;
  }
  const v = c.value == null ? 0 : c.value;
  return `<div class="ctl">
    <div class="lbl"><b>${c.name}</b><span id="v-${c.key}">${v} / ${c.max}</span></div>
    ${help}<input type="range" data-k="${c.key}" min="0" max="${c.max}" value="${v}"
      ${busy ? 'disabled' : ''}></div>`;
}

function paint(s) {
  if (dragging) { painted = null; return; }
  const busy = !!s.applying;
  let html = (s.error ? `<div class="err">${s.error}</div>` : '') +
    `<div class="head"><span>State</span><b>${
      busy ? 'applying "' + s.applying + '"...' : s.layout}</b></div>`;
  for (const [g, title] of s.groups) {
    const cs = s.controls.filter(c => c.group === g);
    if (!cs.length) continue;
    let body = cs.map(c => ctlHTML(c, busy)).join('');
    if (g === 'layout') body += `<div class="warn">Going full screen on an input
      that is bound to the KVM <b>takes the keyboard and mouse with it</b>: in
      full screen the peripherals follow the video input.</div>`;
    html += (g === 'advanced' || g === 'color' || g === 'info')
      ? `<details data-g="${g}" ${open[g] ? 'open' : ''}>
           <summary>${title}</summary>${body}</details>`
      : `<h2>${title}</h2>${body}`;
  }
  app.innerHTML = html;

  app.querySelectorAll('details').forEach(d =>
    d.ontoggle = () => { open[d.dataset.g] = d.open; });
  app.querySelectorAll('button[data-k]').forEach(b =>
    b.onclick = () => send(b.dataset.k, b.dataset.v));
  app.querySelectorAll('input[type=range]').forEach(r => {
    r.oninput = () => {
      dragging = r.dataset.k;
      document.getElementById('v-' + r.dataset.k).textContent = r.value + ' / ' + r.max;
    };
    // 'change', not 'input': writing on every pixel of the drag would saturate
    // an I2C bus that is slow and does not tolerate concurrency.
    r.onchange = () => { dragging = null; send(r.dataset.k, r.value); };
  });
}

async function send(k, v) {
  await fetch('/api/control/' + k + '/' + v, {method: 'POST'});
  poll(true);
}

async function poll(fast) {
  try {
    const s = await (await fetch('/api/state')).json();
    // Do not repaint when nothing changed: rewriting the HTML destroys the DOM
    // and collapses the open sections.
    const sig = JSON.stringify([s.controls, s.layout, s.applying, s.error]);
    if (sig !== painted) { painted = sig; paint(s); }
    setTimeout(poll, s.applying || fast ? 900 : 5000);
  } catch (e) { setTimeout(poll, 3000); }
}
poll();
</script>
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
            requests.notify()
        self._send(202, json.dumps({"queued": key, "value": value}))

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    threading.Thread(target=worker, daemon=True).start()
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"ddc-web listening on http://{HOST}:{PORT}", flush=True)
    srv.serve_forever()

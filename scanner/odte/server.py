"""The button, in a browser.

    python -m odte serve        -> http://127.0.0.1:8787

One page, one big button. First press is the opening read; every press after it
re-confirms against that first read. Runs happen on a worker thread so the page
can show progress instead of hanging on a 60-second stream.

Binds loopback only by design: this process holds a brokerage token, and the
page will happily run scans for anyone who can reach it.
"""

from __future__ import annotations

import argparse
import json
import threading
import uuid
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlparse

from . import __version__, report, session
from .runs import Run, RunLog, compare, entry_from_score

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>0DTE read</title>
<style>
/* Bull/bear is a diverging pair, and green-vs-red is the one pair colour-blind
   readers cannot separate. Blue/red poles with a gray midpoint, and every card
   states BULL/BEAR in words with an arrow, so colour never carries it alone. */
:root{
  --plane:#f9f9f7; --surface:#fcfcfb; --ink:#0b0b0b; --ink-2:#52514e; --muted:#898781;
  --line:#e1e0d9; --ring:rgba(11,11,11,.10);
  --bull:#2563b8; --bear:#d03b3b; --flat:#898781;
  --good:#0ca30c; --warn:#fab219; --serious:#ec835a; --crit:#d03b3b;
  --seg-off:#e1e0d9;
}
@media (prefers-color-scheme:dark){:root{
  --plane:#0d0d0d; --surface:#1a1a19; --ink:#fff; --ink-2:#c3c2b7; --muted:#898781;
  --line:#2c2c2a; --ring:rgba(255,255,255,.10);
  --bull:#6ea8f0; --bear:#e86a6a; --seg-off:#2c2c2a;
}}
*{box-sizing:border-box}
body{margin:0;background:var(--plane);color:var(--ink);
  font:15px/1.5 ui-sans-serif,-apple-system,Segoe UI,Roboto,sans-serif;
  font-variant-numeric:tabular-nums;padding:20px 16px 48px}
header{max-width:1040px;margin:0 auto 18px;display:flex;flex-wrap:wrap;
  gap:10px 18px;align-items:baseline}
h1{font-size:17px;margin:0;letter-spacing:.01em}
.meta{color:var(--muted);font-size:13px}
main{max-width:1040px;margin:0 auto}
.bar{display:flex;flex-wrap:wrap;gap:12px;align-items:center;margin-bottom:22px}
button.press{appearance:none;border:0;border-radius:12px;cursor:pointer;
  background:var(--ink);color:var(--plane);font:600 17px/1 inherit;
  padding:18px 28px;letter-spacing:.02em}
button.press:disabled{opacity:.45;cursor:progress}
button.press:focus-visible{outline:3px solid var(--bull);outline-offset:2px}
.status{color:var(--ink-2);font-size:14px}
.cards{display:grid;gap:14px;grid-template-columns:repeat(auto-fill,minmax(330px,1fr))}
.card{background:var(--surface);border:1px solid var(--ring);border-radius:14px;padding:16px}
.card h2{margin:0;font-size:20px;letter-spacing:.02em}
.top{display:flex;justify-content:space-between;align-items:flex-start;gap:12px}
.rating{text-align:right;line-height:1.1}
.score{font-size:30px;font-weight:700}
.score small{font-size:15px;font-weight:500;color:var(--muted)}
.side{font-size:12px;font-weight:700;letter-spacing:.08em}
.bull .score,.bull .side{color:var(--bull)} .bear .score,.bear .side{color:var(--bear)}
.flat .score,.flat .side{color:var(--flat)}
.meter{display:flex;gap:2px;margin:10px 0 12px}
.meter i{height:6px;flex:1;border-radius:2px;background:var(--seg-off)}
.bull .meter i.on{background:var(--bull)} .bear .meter i.on{background:var(--bear)}
.flat .meter i.on{background:var(--flat)}
.px{color:var(--ink-2);font-size:14px;margin-bottom:8px}
.chip{display:inline-block;font-size:11px;font-weight:700;letter-spacing:.06em;
  border-radius:999px;padding:3px 9px;border:1px solid var(--ring)}
.chip.CONFIRMED{color:var(--good)} .chip.HOLDING{color:var(--ink-2)}
.chip.FADED{color:var(--serious)} .chip.FLIPPED{color:var(--crit)}
.chip.NEW{color:var(--warn)}
ul.setups{list-style:none;margin:10px 0 0;padding:0;font-size:13px}
ul.setups li{margin:0 0 7px;display:flex;gap:8px}
ul.setups b{font-weight:700;width:12px;flex:none;text-align:center}
li.plus b{color:var(--good)} li.minus b{color:var(--serious)} li.block b{color:var(--crit)}
ul.setups span{color:var(--ink-2)}
ul.setups em{display:block;color:var(--muted);font-style:normal;font-size:12px}
.rows{margin-top:12px;border-top:1px solid var(--line);padding-top:10px;
  font-size:12px;color:var(--ink-2)}
.rows div{margin-top:4px}
.rows k{color:var(--muted);display:inline-block;width:52px}
footer{max-width:1040px;margin:26px auto 0;color:var(--muted);font-size:12px;
  border-top:1px solid var(--line);padding-top:12px}
.empty{color:var(--muted);padding:26px 0}
</style></head><body>
<header>
  <h1>0DTE read</h1>
  <span class="meta" id="meta">not run yet</span>
</header>
<main>
  <div class="bar">
    <button class="press" id="go">RUN THE READ</button>
    <span class="status" id="status">Press once after 10:00 ET. Press again later to re-confirm.</span>
  </div>
  <div class="cards" id="cards"><p class="empty">No read yet.</p></div>
</main>
<footer>
  Ratings combine the composite bias, the confidence score and named setups
  (gamma regime, walls, max pain, VWAP/opening range, classified flow).
  Setups are documented market-structure priors, not backtested parameters.
  Not investment advice — 0DTE premium can go to zero the same session.
</footer>
<script>
const $ = (id) => document.getElementById(id);
const fmt = (v, d = 2) => (v === null || v === undefined) ? "-" : Number(v).toFixed(d);

function meter(n){
  let out = "";
  for (let i = 1; i <= 10; i++) out += `<i class="${i <= n ? "on" : ""}"></i>`;
  return out;
}

function card(c){
  const side = c.stars < 3 ? "flat" : c.side.toLowerCase();
  const arrow = c.side === "BULL" ? "\\u25b2" : "\\u25bc";
  const ch = c.change;
  const setups = (c.setups || []).map(s => {
    const cls = s.mark === "+" ? "plus" : (s.mark === "-" ? "minus" : "block");
    return `<li class="${cls}"><b>${s.mark}</b><span>${s.label}<em>${s.detail}</em></span></li>`;
  }).join("") || `<li><b>·</b><span>No named setup fired — raw score only.</span></li>`;
  const L = c.levels, T = c.trade;
  return `<article class="card ${side}">
    <div class="top">
      <div><h2>${c.symbol}</h2>
        <div class="px">${fmt(c.last)} &nbsp; ${c.move_pct >= 0 ? "+" : ""}${fmt(c.move_pct)}%</div></div>
      <div class="rating"><div class="score">${c.stars}<small>/10</small></div>
        <div class="side">${arrow} ${c.side}</div></div>
    </div>
    <div class="meter">${meter(c.stars)}</div>
    <div>${ch ? `<span class="chip ${ch.status}">${ch.status}</span>
      <span class="px"> ${ch.d_score >= 0 ? "+" : ""}${fmt(ch.d_score, 1)} pts,
      ${ch.progress_em === null ? "" : `${ch.progress_em >= 0 ? "+" : ""}${fmt(ch.progress_em)} EM,`}
      ${ch.minutes} min on</span>` : `<span class="chip">${c.band}</span>`}</div>
    <ul class="setups">${setups}</ul>
    <div class="rows">
      <div><k>levels</k> VWAP ${fmt(L.vwap)} · OR ${fmt(L.or_low)}-${fmt(L.or_high)} ·
        pain ${fmt(L.max_pain)} · flip ${fmt(L.gamma_flip)}</div>
      <div><k>walls</k> call ${fmt(L.call_wall)} · put ${fmt(L.put_wall)} ·
        EM ±${fmt(L.expected_move)}</div>
      <div><k>flow</k> ${c.chain.flow_source === "side"
        ? `${(c.chain.net_delta_dollars/1e6).toFixed(1)}mm net delta (${Math.round((c.chain.side_coverage||0)*100)}% classified)`
        : "proxy only — no trade side"}</div>
      <div><k>trade</k> ${T.direction} · ATM ${fmt(T.atm)} / 1EM ${fmt(T.otm_1em)} ·
        target ${fmt(T.target)} · invalidate ${fmt(T.invalidation)}</div>
    </div>
  </article>`;
}

function render(res){
  $("meta").textContent =
    `${res.scanned_at.slice(11,19)} ET · press ${res.press} · ${res.provider} · ${res.minutes_to_close} min to close`;
  $("go").textContent = `RE-CONFIRM (press ${res.press + 1})`;
  $("cards").innerHTML = res.cards.length
    ? res.cards.map(card).join("")
    : `<p class="empty">Nothing scored.</p>`;
}

async function poll(job){
  const r = await (await fetch(`/api/status?job=${job}`)).json();
  if (r.state === "running") { setTimeout(() => poll(job), 800); return; }
  $("go").disabled = false;
  if (r.state === "error") { $("status").textContent = r.message; return; }
  $("status").textContent = "Done. Press again to re-confirm.";
  render(r.result);
}

$("go").onclick = async () => {
  $("go").disabled = true;
  $("status").textContent = "Pulling chains and tape…";
  const r = await (await fetch("/api/run", {method: "POST"})).json();
  if (r.error) { $("go").disabled = false; $("status").textContent = r.error; return; }
  poll(r.job);
};

fetch("/api/last").then(r => r.json()).then(r => { if (r.result) render(r.result); });
</script></body></html>
"""


class Runner:
    """Runs scans on a worker thread and keeps the last result."""

    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.jobs: Dict[str, Dict[str, Any]] = {}
        self.last: Optional[Dict[str, Any]] = None
        self._lock = threading.Lock()

    def start(self) -> str:
        with self._lock:
            if any(j["state"] == "running" for j in self.jobs.values()):
                raise RuntimeError("a read is already running")
            job = uuid.uuid4().hex[:12]
            self.jobs = {job: {"state": "running"}}
        threading.Thread(target=self._work, args=(job,), daemon=True).start()
        return job

    def status(self, job: str) -> Dict[str, Any]:
        return self.jobs.get(job, {"state": "error", "message": "unknown job"})

    def _work(self, job: str) -> None:
        from .cli import ScanFailed, perform_scan, resolve_now

        try:
            args = self.args
            now = resolve_now(args)
            out_dir = Path(args.out_dir)
            if not args.sides:
                auto = out_dir / f"sides-{now:%Y-%m-%d}.json"
                if auto.exists():
                    args.sides = str(auto)
            scores, label, mins_left = perform_scan(args, now)

            log = RunLog.for_date(out_dir / "runs", now)
            baseline = log.first
            run = log.record(scores, now, label)
            changes = compare(run, baseline)
            result = {
                "version": __version__,
                "scanned_at": now.isoformat(),
                "provider": label,
                "press": run.press,
                "minutes_to_close": round(mins_left),
                "cards": report.card_payload(scores, changes, args.top),
            }
            report.write_json(out_dir / "latest.json", scores, result)
            self.last = result
            self.jobs[job] = {"state": "done", "result": result}
        except ScanFailed as exc:
            self.jobs[job] = {"state": "error", "message": str(exc)}
        except Exception as exc:  # never leave the page spinning
            self.jobs[job] = {"state": "error", "message": f"{type(exc).__name__}: {exc}"}


def make_handler(runner: Runner):
    class Handler(BaseHTTPRequestHandler):
        server_version = f"odte/{__version__}"

        def log_message(self, fmt, *a):  # quiet: the console is for the scan
            pass

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, payload: Dict[str, Any], code: int = 200) -> None:
            self._send(code, json.dumps(payload).encode(), "application/json")

        def do_GET(self) -> None:
            path = urlparse(self.path)
            if path.path in ("/", "/index.html"):
                self._send(200, PAGE.encode(), "text/html; charset=utf-8")
            elif path.path == "/api/last":
                self._json({"result": runner.last})
            elif path.path == "/api/status":
                job = (parse_qs(path.query).get("job") or [""])[0]
                self._json(runner.status(job))
            else:
                self._json({"error": "not found"}, 404)

        def do_POST(self) -> None:
            if urlparse(self.path).path != "/api/run":
                self._json({"error": "not found"}, 404)
                return
            try:
                self._json({"job": runner.start()})
            except RuntimeError as exc:
                self._json({"error": str(exc)}, 409)

    return Handler


def run_serve(args: argparse.Namespace) -> int:
    runner = Runner(args)
    httpd = ThreadingHTTPServer((args.host, args.port), make_handler(runner))
    url = f"http://{args.host}:{args.port}"
    print(f"0DTE button on {url}  (ctrl-c to stop)")
    if args.open:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")
    finally:
        httpd.server_close()
    return 0

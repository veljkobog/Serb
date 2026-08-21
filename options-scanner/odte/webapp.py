"""Press-the-button scanner: a local web app that runs a live scan on demand.

Serves one page with a Scan button, streams progress while the scan runs, and renders
the top setups with their full strike ladders and backing data. Standard library only.

Bound to 127.0.0.1 by default — this is a local tool, not a service. It holds no
credentials of its own, but whatever provider key is in your environment is usable by
anything that can reach the port, so only bind wider if you mean it.
"""
from __future__ import annotations

import datetime as dt
import html
import json
import os
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlparse

from . import doctor, report, universe as universe_mod
from .calendar_utils import market_is_open, now_et, session_progress
from .config import Config
from .engine import Scanner, ScanResult
from .providers import PROVIDERS

MAX_BODY = 64 * 1024
FAVICON = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
    '<rect width="32" height="32" rx="7" fill="#0d1117"/>'
    '<path d="M5 22 L12 14 L18 19 L27 8" stroke="#3fb950" stroke-width="3" '
    'fill="none" stroke-linecap="round" stroke-linejoin="round"/></svg>'
).encode()
QUIET = bool(os.environ.get("ODTE_QUIET"))     # suppress access log / tracebacks in tests


class ScanRunner:
    """Owns the one-at-a-time scan and its progress, so the UI can poll it."""

    def __init__(self, base: Config):
        self.base = base
        self.lock = threading.Lock()
        self.state = "idle"          # idle | running | done | error
        self.done = 0
        self.total = 0
        self.current = ""
        self.started: Optional[float] = None
        self.finished: Optional[float] = None
        self.result: Optional[ScanResult] = None
        self.error: Optional[str] = None
        self.settings: Dict[str, Any] = {}

    # -- introspection -----------------------------------------------------
    def status(self) -> Dict[str, Any]:
        with self.lock:
            elapsed = ((self.finished or time.time()) - self.started) if self.started else 0.0
            payload = {
                "state": self.state, "done": self.done, "total": self.total,
                "current": self.current, "elapsed": round(elapsed, 1),
                "error": self.error, "settings": self.settings,
                "market_open": market_is_open(),
                "session_progress": round(session_progress(), 3),
                "now": now_et().strftime("%Y-%m-%d %H:%M:%S ET"),
            }
            if self.result is not None:
                payload["candidates"] = len(self.result.candidates)
                payload["screened"] = len(self.result.universe)
            return payload

    def snapshot(self, top: int) -> Dict[str, Any]:
        with self.lock:
            result = self.result
        if result is None:
            return {"ok": False, "reason": "no scan has completed yet"}
        return {
            "ok": True,
            "cards": report.render_cards(result, top),
            "summary": self._summary(result, top),
            "result": result.as_dict(),
        }

    @staticmethod
    def _summary(result: ScanResult, top: int) -> Dict[str, Any]:
        return {
            "generated_at": result.generated_at.strftime("%Y-%m-%d %H:%M:%S ET"),
            "provider": result.provider,
            "screened": len(result.universe),
            "passed": len(result.candidates),
            "showing": min(top, len(result.candidates)),
            "darkpool_days": result.darkpool_days,
            "darkpool_asof": result.darkpool_asof,
            "errors": len(result.errors),
            "first_error": result.errors[0][:200] if result.errors else None,
            "rejected": len(result.rejected),
        }

    # -- running -----------------------------------------------------------
    def start(self, settings: Dict[str, Any]) -> Dict[str, Any]:
        with self.lock:
            if self.state == "running":
                return {"started": False, "reason": "a scan is already running"}
            self.state = "running"
            self.done, self.total, self.current = 0, 0, ""
            self.started, self.finished = time.time(), None
            self.error = None
            self.settings = settings
        threading.Thread(target=self._run, args=(settings,), daemon=True).start()
        return {"started": True}

    def _config_for(self, settings: Dict[str, Any]) -> Config:
        cfg = Config().merge(json.loads(json.dumps(self.base.as_dict())))
        cfg.force_fresh = True          # the whole point of the button
        cfg.journal = True
        if settings.get("provider") in PROVIDERS:
            cfg.provider = settings["provider"]
        if settings.get("max_dte") in (0, 1):
            cfg.gates.max_dte = int(settings["max_dte"])
        for key, attr in (("min_score", "min_score"), ("min_cap", "min_market_cap"),
                          ("min_dollar_volume", "min_avg_dollar_volume"),
                          ("min_option_volume", "min_option_volume")):
            if settings.get(key) is not None:
                setattr(cfg.gates, attr, float(settings[key]))
        if settings.get("account_size"):
            cfg.account_size = float(settings["account_size"])
        if settings.get("risk_per_trade_pct"):
            cfg.risk_per_trade_pct = float(settings["risk_per_trade_pct"])
        if settings.get("top"):
            cfg.top = int(settings["top"])
        return cfg

    def _run(self, settings: Dict[str, Any]) -> None:
        try:
            cfg = self._config_for(settings)
            symbols = universe_mod.load(
                spec=settings.get("universe") or universe_mod.DEFAULT,
                explicit=[s for s in (settings.get("symbols") or "").split(",") if s.strip()] or None,
            )
            with self.lock:
                self.total = len(symbols)

            def progress(symbol: str, done: str) -> None:
                with self.lock:
                    self.current = symbol
                    try:
                        self.done = int(done.split("/")[0])
                    except (ValueError, IndexError):
                        self.done += 1

            if settings.get("demo"):
                from .synthetic import FakeProvider, make_offex
                today = now_et().date()
                symbols = symbols[:12]
                provider = FakeProvider(symbols, today, drift=0.004, vol=0.009, mixed=True)
                bars = {s: provider.daily_bars(s) for s in symbols}
                with self.lock:
                    self.total = len(symbols)
                scanner = Scanner(cfg, provider=provider,
                                  offex=make_offex(symbols, bars, short_ratio=0.40),
                                  progress_cb=progress)
            else:
                scanner = Scanner(cfg, progress_cb=progress)

            result = scanner.run(symbols)
            side = settings.get("side")
            if side in ("calls", "puts"):
                want = "CALLS" if side == "calls" else "PUTS"
                result.candidates = [c for c in result.candidates if c.side == want]
            report.append_journal(result, f"{cfg.out_dir}/journal.jsonl")

            with self.lock:
                self.result = result
                self.state = "done"
                self.finished = time.time()
                self.done = self.total
        except Exception as exc:
            with self.lock:
                self.error = f"{type(exc).__name__}: {exc}"
                self.state = "error"
                self.finished = time.time()
            if not QUIET:
                traceback.print_exc()


# ------------------------------------------------------------------ page ----
def _options(values, selected) -> str:
    return "".join(
        f'<option value="{html.escape(str(v))}"'
        f'{" selected" if str(v) == str(selected) else ""}>{html.escape(str(label))}</option>'
        for v, label in values)


def render_app(cfg: Config, auto_at: Optional[str] = None, demo: bool = False) -> str:
    presets = [(name, f"{name} ({len(syms)})") for name, syms in universe_mod.PRESETS.items()]
    auto_note = (f"Auto-scan armed for {html.escape(auto_at)} ET on trading days."
                 if auto_at else "")
    demo_note = ("<div class='banner demo'>DEMO MODE — synthetic data, not the market.</div>"
                 if demo else "")
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>0/1DTE Scanner</title>
<link rel="icon" href="/favicon.svg" type="image/svg+xml">
<style>
{report.STYLE}
/* --- app chrome ------------------------------------------------------- */
.topbar {{ display:flex; align-items:center; gap:16px; flex-wrap:wrap;
           border-bottom:1px solid var(--line); padding-bottom:16px; }}
.scanbtn {{ appearance:none; border:0; border-radius:10px; padding:16px 40px;
            font-size:18px; font-weight:700; letter-spacing:.5px; cursor:pointer;
            background:#238636; color:#fff; transition:background .15s, transform .05s; }}
.scanbtn:hover:not(:disabled) {{ background:#2ea043; }}
.scanbtn:active:not(:disabled) {{ transform:translateY(1px); }}
.scanbtn:disabled {{ background:#30363d; color:var(--muted); cursor:not-allowed; }}
.ghostbtn {{ appearance:none; border:1px solid var(--line); border-radius:8px;
             padding:10px 16px; font-size:13px; cursor:pointer; background:transparent;
             color:var(--muted); }}
.ghostbtn:hover {{ color:var(--text); border-color:var(--muted); }}
.clock {{ font-variant-numeric:tabular-nums; font-size:13px; color:var(--muted); }}
table.checks {{ width:100%; margin-top:6px; }}
table.checks td {{ padding:4px 10px 4px 0; vertical-align:top; font-size:12px; }}
td.mark {{ font-weight:700; white-space:nowrap; }}
td.mark.ok {{ color:var(--green); }} td.mark.warn {{ color:#d29922; }}
td.mark.fail {{ color:var(--red); }}
.dot {{ display:inline-block; width:8px; height:8px; border-radius:50%; margin-right:6px;
        background:#f85149; vertical-align:middle; }}
.dot.open {{ background:#3fb950; }}
.settings {{ display:grid; gap:10px 16px; grid-template-columns:repeat(auto-fit,minmax(150px,1fr));
             margin:16px 0 0; }}
.settings label {{ display:flex; flex-direction:column; gap:4px; font-size:11px;
                   text-transform:uppercase; letter-spacing:.6px; color:var(--muted); }}
.settings select, .settings input {{ background:#0d1117; color:var(--text);
    border:1px solid var(--line); border-radius:6px; padding:7px 8px; font-size:13px; }}
.progress {{ margin:20px 0; display:none; }}
.progress.on {{ display:block; }}
.track {{ height:8px; background:#21262d; border-radius:4px; overflow:hidden; }}
.track span {{ display:block; height:100%; background:var(--blue); width:0; transition:width .25s; }}
.plabel {{ font-size:12px; color:var(--muted); margin-top:8px; font-variant-numeric:tabular-nums; }}
.banner {{ border-radius:8px; padding:10px 14px; margin:16px 0; font-size:13px;
           border:1px solid var(--line); }}
.banner.demo {{ border-color:#9e6a03; background:rgba(158,106,3,.12); }}
.banner.warn {{ border-color:#f85149; background:rgba(248,81,73,.10); }}
.banner.info {{ border-color:var(--line); background:#161b22; color:var(--muted); }}
.summary {{ color:var(--muted); font-size:13px; margin:16px 0 0; }}
</style>
</head>
<body>
<div class="wrap">
  <div class="topbar">
    <button id="scan" class="scanbtn">SCAN</button>
    <button id="preflight" class="ghostbtn" title="Check the live data path">Preflight</button>
    <div>
      <div class="clock"><span id="dot" class="dot"></span><span id="clock">--:--:--</span></div>
      <div class="clock" id="auto">{auto_note}</div>
    </div>
  </div>

  {demo_note}

  <div class="settings">
    <label>Universe<select id="universe">{_options(presets, universe_mod.DEFAULT)}</select></label>
    <label>Or symbols<input id="symbols" placeholder="NVDA,SPY,AMD"></label>
    <label>Expiry<select id="max_dte">{_options([(1, "0 or 1 DTE"), (0, "same day only")], cfg.gates.max_dte)}</select></label>
    <label>Side<select id="side">{_options([("both", "both"), ("calls", "calls only"), ("puts", "puts only")], "both")}</select></label>
    <label>Show top<input id="top" type="number" min="1" max="25" value="3"></label>
    <label>Min score<input id="min_score" type="number" min="0" max="100" value="{cfg.gates.min_score:.0f}"></label>
    <label>Account $<input id="account_size" type="number" min="0" step="1000" value="{cfg.account_size:.0f}"></label>
    <label>Risk %/trade<input id="risk_per_trade_pct" type="number" min="0.1" max="10" step="0.1" value="{cfg.risk_per_trade_pct}"></label>
  </div>

  <div class="progress" id="progress">
    <div class="track"><span id="bar"></span></div>
    <div class="plabel" id="plabel">starting...</div>
  </div>

  <div id="banner"></div>
  <p class="summary" id="summary"></p>
  <div class="grid" id="results"></div>
</div>

<script>
const $ = id => document.getElementById(id);
const SETTINGS = ["universe","symbols","max_dte","side","top","min_score",
                  "account_size","risk_per_trade_pct"];

function saveSettings() {{
  try {{
    const out = {{}};
    SETTINGS.forEach(k => out[k] = $(k).value);
    localStorage.setItem("odte.settings", JSON.stringify(out));
  }} catch (e) {{ /* private window, storage disabled — not worth failing over */ }}
}}
function loadSettings() {{
  try {{
    const raw = localStorage.getItem("odte.settings");
    if (!raw) return;
    const saved = JSON.parse(raw);
    SETTINGS.forEach(k => {{ if (saved[k] !== undefined && saved[k] !== "") $(k).value = saved[k]; }});
  }} catch (e) {{ /* ignore malformed or unreadable storage */ }}
}}
loadSettings();
SETTINGS.forEach(k => $(k).addEventListener("change", saveSettings));

// Check details are error strings from urllib and vendor APIs; they routinely contain
// angle brackets ("<urlopen error ...>"), which the browser would parse as markup and
// silently swallow. Escape everything that is not markup we wrote ourselves.
function esc(s) {{
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}}

function banner(kind, text) {{
  $("banner").innerHTML = text ? `<div class="banner ${{kind}}">${{text}}</div>` : "";
}}

function tickClock(s) {{
  $("clock").textContent = s.now || "--:--:--";
  $("dot").className = "dot" + (s.market_open ? " open" : "");
}}

async function poll() {{
  const s = await (await fetch("/api/status")).json();
  tickClock(s);
  if (s.state === "running") {{
    $("progress").classList.add("on");
    const pct = s.total ? (100 * s.done / s.total) : 0;
    $("bar").style.width = pct.toFixed(1) + "%";
    $("plabel").textContent =
      `${{s.done}} / ${{s.total}} scanned · ${{s.current || ""}} · ${{s.elapsed}}s`;
    setTimeout(poll, 400);
    return;
  }}
  $("scan").disabled = false;
  $("scan").textContent = "SCAN";
  if (s.state === "error") {{
    $("progress").classList.remove("on");
    banner("warn", "Scan failed: " + (s.error || "unknown error"));
    return;
  }}
  if (s.state === "done") {{
    $("bar").style.width = "100%";
    $("plabel").textContent = `${{s.total}} scanned in ${{s.elapsed}}s`;
    await showResults();
  }}
}}

async function preflight() {{
  const btn = $("preflight");
  btn.disabled = true; btn.textContent = "CHECKING";
  banner("info", "Running preflight checks...");
  try {{
    const d = await (await fetch("/api/doctor")).json();
    const rows = d.checks.map(c =>
      `<tr><td class="mark ${{esc(c.status)}}">${{esc(c.status).toUpperCase()}}</td>` +
      `<td><strong>${{esc(c.name)}}</strong></td><td>${{esc(c.detail)}}` +
      (c.fix ? `<br><span class="muted">${{esc(c.fix)}}</span>` : "") +
      `</td><td class="muted">${{c.ms ? esc(c.ms) + "ms" : ""}}</td></tr>`).join("");
    const kind = d.verdict === "fail" ? "warn" : "info";
    banner(kind, `<strong>Preflight: ${{d.verdict.toUpperCase()}}</strong>` +
                 `<table class="checks">${{rows}}</table>`);
  }} catch (e) {{
    banner("warn", "Preflight could not run: " + esc(e));
  }}
  btn.disabled = false; btn.textContent = "Preflight";
}}
$("preflight").addEventListener("click", preflight);

async function showResults() {{
  const top = parseInt($("top").value || "3", 10);
  const r = await (await fetch("/api/result?top=" + top)).json();
  if (!r.ok) {{ banner("info", r.reason || "no results"); return; }}
  const m = r.summary;
  $("summary").textContent =
    `${{m.generated_at}} · ${{m.provider}} · ${{m.screened}} screened · ` +
    `${{m.passed}} passed · showing ${{m.showing}} · ` +
    (m.darkpool_days ? `off-exchange ${{m.darkpool_days}}d to ${{m.darkpool_asof}}`
                     : "off-exchange unavailable");
  $("results").innerHTML = r.cards;
  if (!m.passed) {{
    banner("info", "Nothing cleared the gates. Lower the min score, widen the universe, " +
                   "or wait for the chains to fill in after the open. If that seems wrong, " +
                   "press Preflight to check whether the data actually arrived.");
  }} else if (m.errors) {{
    banner("info", `${{m.errors}} data error(s) during the scan. First: ${{esc(m.first_error)}}`);
  }} else if (!m.darkpool_days) {{
    banner("info", "FINRA off-exchange data was unavailable, so dark pool signals were skipped.");
  }} else {{
    banner("", "");
  }}
}}

$("scan").addEventListener("click", async () => {{
  saveSettings();
  $("scan").disabled = true;
  $("scan").textContent = "SCANNING";
  banner("", "");
  $("results").innerHTML = "";
  $("summary").textContent = "";
  $("progress").classList.add("on");
  $("bar").style.width = "0%";
  $("plabel").textContent = "starting...";
  const body = {{}};
  SETTINGS.forEach(k => body[k] = $(k).value);
  const res = await (await fetch("/api/scan", {{
    method: "POST", headers: {{"Content-Type": "application/json"}},
    body: JSON.stringify(body)
  }})).json();
  if (!res.started) {{
    banner("info", esc(res.reason || "could not start"));
    $("scan").disabled = false;
    $("scan").textContent = "SCAN";
    return;
  }}
  poll();
}});

// Keep the clock alive and pick up a scan started by the auto-scan timer.
(async function heartbeat() {{
  try {{
    const s = await (await fetch("/api/status")).json();
    tickClock(s);
    if (s.state === "running" && !$("scan").disabled) {{
      $("scan").disabled = true;
      $("scan").textContent = "SCANNING";
      poll();
    }}
  }} catch (e) {{ /* server went away; the next tick will retry */ }}
  setTimeout(heartbeat, 2000);
}})();

// Show the last completed scan on load, if there is one.
showResults().catch(() => {{}});
</script>
</body>
</html>"""


# --------------------------------------------------------------- handler ----
class Handler(BaseHTTPRequestHandler):
    server_version = "odte-scanner"
    runner: ScanRunner
    config: Config
    auto_at: Optional[str] = None
    demo: bool = False

    def log_message(self, fmt, *args):  # quieter than the default access log
        if QUIET or self.path.startswith("/api/status"):
            return
        print(f"  {self.address_string()} {fmt % args}")

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: Dict[str, Any], code: int = 200) -> None:
        self._send(code, json.dumps(payload, default=str).encode(), "application/json")

    def do_GET(self) -> None:  # noqa: N802
        route = urlparse(self.path)
        query = parse_qs(route.query)
        if route.path in ("/", "/index.html"):
            self._send(200, render_app(self.config, self.auto_at, self.demo).encode(),
                       "text/html; charset=utf-8")
        elif route.path == "/api/status":
            self._json(self.runner.status())
        elif route.path == "/api/result":
            try:
                top = max(1, min(25, int(query.get("top", ["3"])[0])))
            except ValueError:
                top = 3
            self._json(self.runner.snapshot(top))
        elif route.path in ("/favicon.svg", "/favicon.ico"):
            self._send(200, FAVICON, "image/svg+xml")
        elif route.path == "/api/doctor":
            checks = doctor.run_checks(self.config)
            self._json({"verdict": doctor.verdict(checks),
                        "checks": [c.as_dict() for c in checks]})
        elif route.path == "/api/health":
            self._json({"ok": True})
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        if urlparse(self.path).path != "/api/scan":
            self._json({"error": "not found"}, 404)
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length > MAX_BODY:
            self._json({"started": False, "reason": "request too large"}, 413)
            return
        try:
            settings = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(settings, dict):
                raise ValueError("expected a JSON object")
        except (ValueError, json.JSONDecodeError) as exc:
            self._json({"started": False, "reason": f"bad request: {exc}"}, 400)
            return
        settings = _clean_settings(settings)
        settings["demo"] = self.demo
        self._json(self.runner.start(settings))


def _clean_settings(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Coerce the form values; the browser sends everything as strings."""
    out: Dict[str, Any] = {}
    for key in ("universe", "symbols", "side", "provider"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            out[key] = value.strip()
    for key in ("max_dte", "top"):
        try:
            out[key] = int(raw[key])
        except (KeyError, TypeError, ValueError):
            pass
    for key in ("min_score", "min_cap", "min_dollar_volume", "min_option_volume",
                "account_size", "risk_per_trade_pct"):
        try:
            out[key] = float(raw[key])
        except (KeyError, TypeError, ValueError):
            pass
    return out


# ------------------------------------------------------------- auto-scan ----
def _auto_scan_loop(runner: ScanRunner, at: str, settings: Dict[str, Any]) -> None:
    """Fire one scan per trading day at ``at`` (HH:MM, ET)."""
    hour, minute = (int(x) for x in at.split(":"))
    fired_on: Optional[dt.date] = None
    while True:
        now = now_et()
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        from .calendar_utils import is_trading_day
        if (is_trading_day(now.date()) and fired_on != now.date()
                and target <= now < target + dt.timedelta(minutes=5)):
            fired_on = now.date()
            print(f"  auto-scan firing at {now:%H:%M} ET")
            runner.start(dict(settings))
        time.sleep(20)


def serve(config: Config, host: str = "127.0.0.1", port: int = 8765,
          auto_at: Optional[str] = None, demo: bool = False,
          auto_settings: Optional[Dict[str, Any]] = None) -> None:
    runner = ScanRunner(config)
    Handler.runner = runner
    Handler.config = config
    Handler.auto_at = auto_at
    Handler.demo = demo

    if auto_at:
        settings = dict(auto_settings or {})
        settings["demo"] = demo
        threading.Thread(target=_auto_scan_loop, args=(runner, auto_at, settings),
                         daemon=True).start()

    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"  0/1DTE scanner ready at http://{host}:{port}")
    if demo:
        print("  DEMO MODE: synthetic data, not the market.")
    if auto_at:
        print(f"  auto-scan armed for {auto_at} ET on trading days")
    print("  press the SCAN button in the browser; Ctrl-C here to stop")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  stopped")
    finally:
        httpd.server_close()

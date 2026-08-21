"""Output renderers: terminal table, JSON, CSV, JSONL journal, and an HTML dashboard."""
from __future__ import annotations

import csv
import html
import json
import os
from typing import List, Optional

from .engine import ScanResult
from .score import Candidate

RESET, BOLD, DIM = "\033[0m", "\033[1m", "\033[2m"
GREEN, RED, YELLOW, CYAN, GREY = "\033[32m", "\033[31m", "\033[33m", "\033[36m", "\033[90m"


def _colour(enabled: bool, code: str, text: str) -> str:
    return f"{code}{text}{RESET}" if enabled else text


def _money(x: Optional[float]) -> str:
    if not x:
        return "-"
    for unit, div in (("T", 1e12), ("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(x) >= div:
            return f"{x/div:.1f}{unit}"
    return f"{x:.0f}"


def _pct(x: Optional[float], nd: int = 1) -> str:
    return "-" if x is None else f"{x*100:.{nd}f}%"


# ---------------------------------------------------------------- terminal --
def render_terminal(result: ScanResult, top: int = 20, colour: bool = True,
                    verbose: bool = False) -> str:
    lines: List[str] = []
    ts = result.generated_at.strftime("%Y-%m-%d %H:%M ET")
    lines.append(_colour(colour, BOLD, f"0/1DTE scan  {ts}  provider={result.provider}  "
                                      f"universe={len(result.universe)}"))
    dp = (f"off-exchange: {result.darkpool_days}d loaded, as of {result.darkpool_asof}"
          if result.darkpool_days else
          _colour(colour, YELLOW, "off-exchange: UNAVAILABLE (dark pool signals disabled)"))
    lines.append(_colour(colour, DIM, dp))
    lines.append("")

    if not result.candidates:
        lines.append(_colour(colour, YELLOW, "No names cleared the gates and score floor."))
        lines.append(_colour(colour, DIM, "Loosen --min-score, widen --universe, or check that "
                                          "the market data provider returned chains."))
    else:
        header = (f"{'#':>2}  {'SYM':<6} {'SIDE':<5} {'SCORE':>5} {'LAST':>9} {'CHG':>7} "
                  f"{'EXP':<10} {'EM%':>6} {'RVOL':>5} {'DPI':>6} {'OPTVOL':>9} {'STRIKE':>9}")
        lines.append(_colour(colour, BOLD, header))
        lines.append(_colour(colour, GREY, "-" * len(header)))
        for i, c in enumerate(result.candidates[:top], 1):
            opt = c.blocks.get("options")
            vol = c.blocks.get("volume")
            dpb = c.blocks.get("darkpool")
            em = (opt.detail.get("expected_move_pct") if opt else None)
            rvol = (vol.detail.get("rvol") if vol else None)
            dpi = (dpb.detail.get("dpi") if dpb and dpb.available else None)
            optvol = (opt.detail.get("total_volume") if opt else None)
            side_col = GREEN if c.side == "CALLS" else RED if c.side == "PUTS" else GREY
            strike = c.plan.get("strike")
            contract = f"{strike:g}{c.plan.get('right', '')}" if strike else "-"
            chg = f"{c.change_pct:+.2f}%" if c.change_pct is not None else "-"
            cells = [
                f"{i:>2}", f"{c.symbol:<6}",
                _colour(colour, side_col, f"{c.side:<5}"),
                f"{c.score:>5.1f}", f"{c.spot:>9,.2f}", f"{chg:>7}",
                f"{str(c.expiry or '-'):<10}",
                f"{(f'{em:.2f}' if em else '-'):>6}",
                f"{(f'{rvol:.2f}' if rvol else '-'):>5}",
                f"{(f'{dpi*100:.1f}' if dpi else '-'):>6}",
                f"{(f'{optvol:,.0f}' if optvol else '-'):>9}",
                f"{contract:>9}",
            ]
            lines.append("  ".join(cells[:2]) + "  " + " ".join(cells[2:]))
            for reason in c.reasons(4):
                lines.append(_colour(colour, GREY, f"       - {reason}"))
            plan = c.plan
            if plan.get("strike"):
                lines.append(_colour(colour, BOLD,
                             f"       {plan['expiry']}  stop {plan.get('underlying_stop')}  "
                             f"T1 {plan.get('target_1')}"
                             + (f"  T2 {plan['target_2']}" if plan.get("target_2") else "")))
            for rung in plan.get("ladder", []):
                outcomes = "  ".join(
                    f"{o['target']}:{o['return_pct']:+.0f}%" for o in rung.get("outcomes", [])
                    if o.get("return_pct") is not None)
                lines.append(_colour(colour, CYAN,
                             f"       {rung['rung']:<7}{rung['strike']:>9g}{rung['right']} "
                             f"{rung['moneyness']:<4} ${rung['mid']:>6.2f}  "
                             f"d={str(rung['delta'] or '-'):<6} OI {rung['open_interest']:>7,.0f}  "
                             f"BE {rung['breakeven']:g} ({rung['pct_move_to_breakeven']:+.2f}%)  "
                             f"x{rung['suggested_contracts']:<3} {outcomes}"))
            for note in plan.get("notes", [])[:3]:
                lines.append(_colour(colour, DIM, f"       ! {note}"))
            lines.append("")

    if verbose and result.rejected:
        lines.append(_colour(colour, BOLD, f"Rejected ({len(result.rejected)}):"))
        for c in result.rejected[:60]:
            why = c.error or "; ".join(c.gate_failures[:3])
            lines.append(_colour(colour, GREY, f"  {c.symbol:<6} {why}"))
    if result.errors:
        lines.append(_colour(colour, DIM, f"{len(result.errors)} data errors "
                                          f"(first: {result.errors[0][:120]})"))
    lines.append("")
    lines.append(_colour(colour, DIM, "Educational tooling, not investment advice. "
                                      "0DTE options can and regularly do go to zero."))
    return "\n".join(lines)


# -------------------------------------------------------------------- files --
def write_json(result: ScanResult, path: str) -> str:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(result.as_dict(), fh, indent=2, default=str)
    return path


def write_csv(result: ScanResult, path: str) -> str:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    cols = ["symbol", "side", "score", "spot", "change_pct", "expiry", "dte", "market_cap",
            "direction", "confluence", "quality", "rvol", "dollar_volume", "dpi",
            "offex_share", "short_pct_float", "days_to_cover", "option_volume", "put_call_ratio",
            "expected_move_pct", "atm_spread", "call_wall", "put_wall", "strike", "right",
            "premium_mid", "underlying_stop", "target_1", "target_2", "flags", "reasons"]
    with open(path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        writer.writeheader()
        for c in result.candidates:
            vol = c.blocks.get("volume")
            dpb = c.blocks.get("darkpool")
            si = c.blocks.get("short_interest")
            opt = c.blocks.get("options")
            writer.writerow({
                "symbol": c.symbol, "side": c.side, "score": round(c.score, 2), "spot": c.spot,
                "change_pct": c.change_pct, "expiry": c.expiry, "dte": c.dte,
                "market_cap": c.market_cap, "direction": round(c.direction, 4),
                "confluence": round(c.confluence, 4), "quality": round(c.quality, 4),
                "rvol": (vol.detail.get("rvol") if vol else None),
                "dollar_volume": (vol.detail.get("dollar_volume") if vol else None),
                "dpi": (dpb.detail.get("dpi") if dpb else None),
                "offex_share": (dpb.detail.get("offex_share") if dpb else None),
                "short_pct_float": (si.detail.get("short_pct_float") if si else None),
                "days_to_cover": (si.detail.get("days_to_cover") if si else None),
                "option_volume": (opt.detail.get("total_volume") if opt else None),
                "put_call_ratio": (opt.detail.get("put_call_ratio") if opt else None),
                "expected_move_pct": (opt.detail.get("expected_move_pct") if opt else None),
                "atm_spread": (opt.detail.get("atm_spread") if opt else None),
                "call_wall": (opt.detail.get("call_wall") if opt else None),
                "put_wall": (opt.detail.get("put_wall") if opt else None),
                "strike": c.plan.get("strike"), "right": c.plan.get("right"),
                "premium_mid": c.plan.get("premium_mid"),
                "underlying_stop": c.plan.get("underlying_stop"),
                "target_1": c.plan.get("target_1"), "target_2": c.plan.get("target_2"),
                "flags": "|".join(c.flags), "reasons": " | ".join(c.reasons(4)),
            })
    return path


def append_journal(result: ScanResult, path: str) -> str:
    """One JSON line per candidate per scan — the raw material for forward-testing."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        for c in result.candidates:
            fh.write(json.dumps({
                "ts": result.generated_at.isoformat(), "symbol": c.symbol, "side": c.side,
                "score": round(c.score, 2), "spot": c.spot, "expiry": str(c.expiry),
                "dte": c.dte, "plan": c.plan,
                "direction": round(c.direction, 4), "quality": round(c.quality, 4),
                "blocks": {k: {"direction": round(v.direction, 4), "quality": round(v.quality, 4)}
                           for k, v in c.blocks.items() if v.available},
            }, default=str) + "\n")
    return path


# --------------------------------------------------------------------- HTML --
def _bar(pct: float, colour: str) -> str:
    width = max(0.0, min(100.0, pct))
    return (f'<div class="bar"><span style="width:{width:.1f}%;background:{colour}"></span></div>')


def _card(c: Candidate) -> str:
    opt = c.blocks.get("options")
    vol = c.blocks.get("volume")
    dpb = c.blocks.get("darkpool")
    si = c.blocks.get("short_interest")
    trend_b = c.blocks.get("trend")
    e = html.escape
    side_class = "calls" if c.side == "CALLS" else "puts" if c.side == "PUTS" else "flat"

    stats = [
        ("Last", f"${c.spot:,.2f}"),
        ("Change", f"{c.change_pct:+.2f}%" if c.change_pct is not None else "-"),
        ("Expiry", f"{c.expiry} ({c.dte}DTE)"),
        ("Mkt cap", _money(c.market_cap)),
        ("RVOL", f"{vol.detail.get('rvol'):.2f}x" if vol and vol.detail.get("rvol") else "-"),
        ("$ volume", f"{_money(vol.detail.get('dollar_volume'))}/day" if vol else "-"),
        ("Dark pool idx", _pct(dpb.detail.get("dpi")) if dpb and dpb.available else "n/a"),
        ("Off-exch share", _pct(dpb.detail.get("offex_share")) if dpb and dpb.available else "n/a"),
        ("Short % float", _pct(si.detail.get("short_pct_float")) if si and si.available else "n/a"),
        ("Days to cover", f"{si.detail.get('days_to_cover'):.1f}" if si and si.available and si.detail.get("days_to_cover") else "n/a"),
        ("Option volume", f"{opt.detail.get('total_volume'):,.0f}" if opt and opt.available else "-"),
        ("Put/call", f"{opt.detail.get('put_call_ratio'):.2f}" if opt and opt.detail.get("put_call_ratio") else "-"),
        ("Expected move", f"{opt.detail.get('expected_move_pct'):.2f}%" if opt and opt.detail.get("expected_move_pct") else "-"),
        ("ATM spread", _pct(opt.detail.get("atm_spread")) if opt and opt.detail.get("atm_spread") is not None else "-"),
        ("Call wall", f"{opt.detail.get('call_wall'):g}" if opt and opt.detail.get("call_wall") else "-"),
        ("Put wall", f"{opt.detail.get('put_wall'):g}" if opt and opt.detail.get("put_wall") else "-"),
        ("ATR(14)", f"{trend_b.detail.get('atr14'):.2f}" if trend_b and trend_b.detail.get("atr14") else "-"),
        ("21 EMA", f"{trend_b.detail.get('ema21'):.2f}" if trend_b and trend_b.detail.get("ema21") else "-"),
    ]
    stat_html = "".join(f"<div><dt>{e(k)}</dt><dd>{e(str(v))}</dd></div>" for k, v in stats)

    blocks_html = ""
    for key, label in (("trend", "Trend"), ("volume", "Volume"), ("darkpool", "Dark pool"),
                       ("options", "Options"), ("short_interest", "Short interest")):
        b = c.blocks.get(key)
        if not b or not b.available:
            blocks_html += f'<tr><th>{e(label)}</th><td colspan="2" class="muted">no data</td></tr>'
            continue
        dcol = "#3fb950" if b.direction >= 0 else "#f85149"
        blocks_html += (
            f'<tr><th>{e(label)}</th>'
            f'<td>{_bar((b.direction + 1) * 50, dcol)}<span class="num">{b.direction:+.2f}</span></td>'
            f'<td>{_bar(b.quality * 100, "#58a6ff")}<span class="num">{b.quality:.2f}</span></td></tr>')

    reasons = "".join(f"<li>{e(r)}</li>" for r in c.reasons(6))
    plan = c.plan
    ladder_html = ""
    if plan.get("ladder"):
        head = ("<tr><th>Rung</th><th>Strike</th><th>Mid</th><th>Δ</th><th>OI</th><th>Vol</th>"
                "<th>Spread</th><th>Cost</th><th>Breakeven</th><th>Move</th><th>Qty</th></tr>")
        rows = ""
        for r in plan["ladder"]:
            outcomes = " &middot; ".join(
                f"{o['target']} {o['underlying']:g} &rarr; ${o['value_at_expiry']:.2f} "
                f"({o['return_pct']:+.0f}%)" for o in r.get("outcomes", [])
                if o.get("return_pct") is not None)
            rows += (
                f'<tr class="rung {e(r["rung"])}">'
                f'<td><strong>{e(r["rung"])}</strong><br><span class="muted">{e(r["moneyness"])}</span></td>'
                f'<td>{r["strike"]:g}{e(r["right"])}</td>'
                f'<td>${r["mid"]:.2f}<br><span class="muted">{r["bid"]:.2f}/{r["ask"]:.2f}</span></td>'
                f'<td>{r["delta"] if r["delta"] is not None else "-"}</td>'
                f'<td>{r["open_interest"]:,.0f}</td>'
                f'<td>{r["volume"]:,.0f}</td>'
                f'<td>{(r["spread_pct"] * 100):.1f}%</td>'
                f'<td>${r["cost_per_contract"]:,.0f}</td>'
                f'<td>{r["breakeven"]:g}</td>'
                f'<td>{r["pct_move_to_breakeven"]:+.2f}%</td>'
                f'<td>{r["suggested_contracts"]}</td>'
                f'</tr>'
                f'<tr class="outcome"><td colspan="11" class="muted">at expiry: {outcomes}</td></tr>')
        ladder_html = (f"<h4>Strike ladder <span class='muted'>(pick your risk)</span></h4>"
                       f"<div class='scroll'><table class='ladder'>{head}{rows}</table></div>")

    plan_html = ""
    if plan.get("strike"):
        rows = [
            ("Contract", f"{plan['expiry']} {plan['strike']:g}{plan['right']}"),
            ("Mid / bid / ask", f"${plan.get('premium_mid')} / {plan.get('bid')} / {plan.get('ask')}"),
            ("Delta", plan.get("delta") if plan.get("delta") is not None else "n/a"),
            ("OI / volume", f"{plan.get('open_interest'):,.0f} / {plan.get('contract_volume'):,.0f}"
             if plan.get("open_interest") is not None else "-"),
            ("Entry above/below", plan.get("entry_trigger")),
            ("Underlying stop", plan.get("underlying_stop")),
("Targets", (f"{plan.get('target_1')} / {plan['target_2']}"
                          if plan.get("target_2") else f"{plan.get('target_1')} (capped by wall)")
             + (" (wall)" if plan.get("target_2_capped_at_wall") and plan.get("target_2") else "")),
            ("Premium stop", f"${plan.get('premium_stop')} "
                             f"(-{int(plan.get('premium_stop_pct', 0)*100)}%)"),
            ("Risk / contract", f"${plan.get('risk_per_contract')}"),
        ]
        plan_html = ("<h4>Trade plan</h4><table class='plan'>"
                     + "".join(f"<tr><th>{e(k)}</th><td>{e(str(v))}</td></tr>" for k, v in rows)
                     + "</table>"
                     + "<ul class='notes'>"
                     + "".join(f"<li>{e(n)}</li>" for n in plan.get("notes", []))
                     + "</ul>")

    flags = "".join(f'<span class="flag">{e(f)}</span>' for f in c.flags)
    return f"""
    <article class="card {side_class}">
      <header>
        <div class="sym"><h3>{e(c.symbol)}</h3><span class="name">{e(c.name or '')}</span></div>
        <div class="verdict"><span class="side">{e(c.side)}</span>
          <span class="score">{c.score:.0f}</span></div>
      </header>
      <div class="flags">{flags}</div>
      <dl class="stats">{stat_html}</dl>
      <h4>Signal blocks <span class="muted">(direction / quality)</span></h4>
      <table class="blocks">{blocks_html}</table>
      <h4>Why it ranked</h4><ul class="reasons">{reasons}</ul>
      {ladder_html}
      {plan_html}
    </article>"""


STYLE = """  :root { color-scheme: dark; --bg:#0d1117; --panel:#161b22; --line:#30363d;
           --text:#e6edf3; --muted:#8b949e; --green:#3fb950; --red:#f85149; --blue:#58a6ff; }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--text); font:14px/1.5 -apple-system,
          BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif; }
  .wrap { max-width:1200px; margin:0 auto; padding:24px 16px 64px; }
  h1 { font-size:22px; margin:0 0 4px; }
  .sub { color:var(--muted); font-size:13px; margin:0 0 4px; }
  .grid { display:grid; gap:16px; grid-template-columns:repeat(auto-fill,minmax(380px,1fr)); margin-top:24px; }
  .card { background:var(--panel); border:1px solid var(--line); border-radius:10px;
           padding:16px; border-top:3px solid var(--line); }
  .card.calls { border-top-color:var(--green); }
  .card.puts { border-top-color:var(--red); }
  .card header { display:flex; justify-content:space-between; align-items:flex-start; gap:12px; }
  .card h3 { margin:0; font-size:20px; letter-spacing:.5px; }
  .name { color:var(--muted); font-size:12px; display:block; max-width:220px;
           overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .verdict { text-align:right; }
  .side { display:block; font-size:11px; letter-spacing:1px; color:var(--muted); }
  .calls .side { color:var(--green); } .puts .side { color:var(--red); }
  .score { font-size:28px; font-weight:700; }
  .flags { margin:8px 0 0; }
  .flag { display:inline-block; background:#2d333b; border:1px solid var(--line);
           border-radius:999px; padding:1px 8px; font-size:11px; margin-right:6px; }
  dl.stats { display:grid; grid-template-columns:repeat(2,1fr); gap:6px 14px; margin:14px 0; }
  dl.stats div { display:flex; justify-content:space-between; border-bottom:1px dotted #21262d; }
  dt { color:var(--muted); font-size:12px; } dd { margin:0; font-size:12px; font-variant-numeric:tabular-nums; }
  h4 { margin:16px 0 6px; font-size:12px; text-transform:uppercase; letter-spacing:.8px; color:var(--muted); }
  table { width:100%; border-collapse:collapse; font-size:12px; }
  table th { text-align:left; font-weight:500; color:var(--muted); padding:3px 6px 3px 0; white-space:nowrap; }
  table td { padding:3px 0; }
  .bar { display:inline-block; width:70px; height:6px; background:#21262d; border-radius:3px;
          overflow:hidden; vertical-align:middle; margin-right:6px; }
  .bar span { display:block; height:100%; }
  .num { font-variant-numeric:tabular-nums; color:var(--muted); }
  ul.reasons, ul.notes { margin:0; padding-left:18px; font-size:12px; color:var(--text); }
  ul.notes { color:var(--muted); margin-top:8px; }
  ul.reasons li, ul.notes li { margin:2px 0; }
  table.plan th { width:42%; }
  .scroll { overflow-x:auto; }
  table.ladder { font-size:11px; min-width:560px; }
  table.ladder th { border-bottom:1px solid var(--line); padding-bottom:4px; }
  table.ladder td { padding:5px 6px 5px 0; border-bottom:1px solid #21262d;
                     font-variant-numeric:tabular-nums; vertical-align:top; }
  tr.rung.core td { background:rgba(88,166,255,.07); }
  tr.outcome td { border-bottom:1px solid var(--line); padding-bottom:8px; font-size:11px; }
  .muted { color:var(--muted); }
  .empty { color:var(--muted); padding:40px; text-align:center; border:1px dashed var(--line);
            border-radius:10px; }
  details { margin-top:32px; } details table { margin-top:8px; }
  footer { margin-top:40px; color:var(--muted); font-size:12px; border-top:1px solid var(--line);
            padding-top:16px; }"""


def render_cards(result: ScanResult, top: int = 20) -> str:
    """Just the candidate cards — shared by the static page and the Scan-button app."""
    return "".join(_card(c) for c in result.candidates[:top])


def render_html(result: ScanResult, top: int = 20) -> str:
    e = html.escape
    ts = result.generated_at.strftime("%Y-%m-%d %H:%M ET")
    dp_note = (f"FINRA off-exchange: {result.darkpool_days} sessions loaded, latest "
               f"{result.darkpool_asof}" if result.darkpool_days
               else "FINRA off-exchange data unavailable — dark pool signals were skipped")
    cards = render_cards(result, top)
    if not cards:
        cards = ('<p class="empty">Nothing cleared the gates. Loosen the filters, widen the '
                 'universe, or re-run once the option chains populate after the open.</p>')
    rejected = "".join(
        f"<tr><td>{e(c.symbol)}</td><td>{e(c.error or '; '.join(c.gate_failures))}</td></tr>"
        for c in result.rejected[:80])
    g = result.config.gates if result.config else None
    gate_line = ("" if not g else
                 f"cap &ge; ${g.min_market_cap/1e9:.1f}B &middot; "
                 f"${g.min_avg_dollar_volume/1e6:.0f}M/day &middot; "
                 f"&ge;{g.min_option_volume:,.0f} contracts &middot; "
                 f"ATM spread &le; {g.max_atm_spread_pct*100:.0f}% &middot; "
                 f"DTE &le; {g.max_dte}")
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>0/1DTE Options Scanner</title>
<style>
{STYLE}
</style>
</head>
<body>
<div class="wrap">
  <h1>0/1DTE Options Scanner</h1>
  <p class="sub">{e(ts)} &middot; provider <strong>{e(result.provider)}</strong> &middot;
     {len(result.universe)} symbols screened &middot; {len(result.candidates)} passed</p>
  <p class="sub">{e(dp_note)}</p>
  <p class="sub">Gates: {gate_line}</p>
  <div class="grid">{cards}</div>
  <details><summary class="muted">Rejected names ({len(result.rejected)})</summary>
    <table>{rejected}</table></details>
  <footer>Educational tooling. Nothing here is investment advice or a recommendation.
    Dark pool figures are FINRA off-exchange volume, an end-of-day proxy for institutional
    activity — not a position report. 0DTE options routinely expire worthless.</footer>
</div>
</body>
</html>"""


def write_html(result: ScanResult, path: str, top: int = 20) -> str:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(render_html(result, top))
    return path

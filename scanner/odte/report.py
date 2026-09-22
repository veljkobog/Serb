"""Console, CSV, JSON and HTML output."""

from __future__ import annotations

import csv
import html
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from .scoring import SymbolScore

COMPONENT_ORDER = [
    "rs", "vwap", "orb", "persistence",
    "skew", "flow", "pcr", "oi_tilt", "gamma_pull", "stretch",
]


def _f(v: Optional[float], nd: int = 2, dash: str = "-") -> str:
    return dash if v is None else f"{v:,.{nd}f}"


def _col(v: Optional[float], width: int, nd: int = 2, sign: bool = False) -> str:
    """Fixed-width numeric cell that degrades to a dash instead of 'nan'."""
    if v is None or v != v:
        return f"{'-':>{width}}"
    fmt = f"{{:>{'+' if sign else ''}{width}.{nd}f}}"
    return fmt.format(v)


def _pct(v: Optional[float], nd: int = 2) -> str:
    return "-" if v is None else f"{v:+.{nd}f}%"


def render_console(
    scores: Sequence[SymbolScore],
    now: datetime,
    provider: str,
    minutes_to_close: float,
    detail: int = 3,
) -> str:
    lines: List[str] = []
    lines.append(
        f"0DTE BIAS SCAN  |  {now:%Y-%m-%d %H:%M:%S %Z}  |  provider={provider}  |  "
        f"{minutes_to_close:.0f} min to close"
    )
    lines.append("=" * 118)
    hdr = (
        f"{'SYM':<6}{'BIAS':>7}{'CONF':>6}  {'VERDICT':<16}{'LAST':>9}"
        f"{'%PC':>8}{'RS':>7}{'VWZ':>6}{'ORB':>6}{'ATMIV':>7}{'EM%':>7}"
        f"{'RR25':>7}{'P/C':>6}{'V/OI':>6}  {'FLAGS'}"
    )
    lines.append(hdr)
    lines.append("-" * 118)
    for s in scores:
        p, o = s.price, s.options
        lines.append(
            f"{s.symbol:<6}{s.bias:>+7.1f}{s.confidence:>6.0f}  {s.verdict:<16}"
            f"{p.last:>9.2f}{(p.ret_prev_close_pct if p.ret_prev_close_pct is not None else p.ret_open_pct):>+8.2f}"
            f"{p.rs_resid_pct:>+7.2f}{p.vwap_z:>+6.1f}{p.or_pos:>+6.1f}"
            + _col(o.atm_iv * 100 if o and o.atm_iv else None, 7, 1)
            + _col(o.em_pct if o else None, 7, 2)
            + _col(o.rr25 if o else None, 7, 1, sign=True)
            + _col(o.pc_volume_ratio if o else None, 6, 2)
            + _col(o.vol_oi_ratio if o else None, 6, 2)
            + f"  {','.join(s.flags)}"
        )
    lines.append("-" * 118)
    lines.append(
        "BIAS -100 (downside) .. +100 (upside) | CONF 0-100 | RS = beta-adj return vs benchmark, %"
    )
    lines.append(
        "VWZ = sigma from VWAP | ORB +1 = at opening-range high | RR25 = 25d call IV - put IV, vol pts"
    )

    for s in scores[:detail]:
        lines.extend(_detail_block(s))
    lines.append("")
    lines.append(
        "Not investment advice. 0DTE decays to zero the same day; size accordingly."
    )
    return "\n".join(lines)


def _detail_block(s: SymbolScore) -> List[str]:
    p, o, pl = s.price, s.options, s.plan
    out = ["", f"--- {s.symbol}  bias {s.bias:+.1f}  conf {s.confidence:.0f}  {s.verdict} ---"]
    contrib = sorted(s.contributions.items(), key=lambda kv: abs(kv[1]), reverse=True)
    out.append(
        "  drivers: "
        + ", ".join(f"{k} {v:+.1f}" for k, v in contrib[:6])
    )
    out.append(
        f"  tape:    last {p.last:,.2f}  vwap {p.vwap:,.2f}  OR {p.or_low:,.2f}-{p.or_high:,.2f}"
        f"  persistence {p.persistence:.0%}  range {p.session_range_pct:.2f}%"
        + (f"  move/ADR {p.move_vs_adr:.2f}x" if p.move_vs_adr is not None else "")
    )
    if o:
        out.append(
            f"  chain:   exp {s.expiry}  ATM {o.atm_strike:,.2f}  straddle {_f(o.atm_straddle)}"
            f"  EM +/-{_f(o.em_dollars)} ({_f(o.em_pct)}%)  vol {o.chain_volume:,.0f}"
            f"  OI {o.total_oi:,.0f}  spread {(_f(o.avg_atm_spread_pct * 100, 1) + '%') if o.avg_atm_spread_pct is not None else '-'}"
        )
        out.append(
            f"  struct:  max pain {_f(o.max_pain)}  gamma wall {_f(o.gamma_wall)}"
            f"  flip {_f(o.gamma_flip)}  net GEX {o.net_gex/1e6:+.1f}mm/1%"
            f"  flow tilt {_f(o.flow_tilt)}  OI tilt {_f(o.oi_tilt)}"
        )
    out.append(
        f"  plan:    {pl.direction}  ATM {_f(pl.atm_strike)} / 1EM OTM {_f(pl.otm_1em_strike)}"
        f"  target {_f(pl.target)}  invalidate {_f(pl.invalidation)}"
    )
    return out


def to_rows(scores: Sequence[SymbolScore]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for s in scores:
        p, o = s.price, s.options
        row: Dict[str, Any] = {
            "symbol": s.symbol,
            "bias": round(s.bias, 1),
            "confidence": round(s.confidence, 0),
            "verdict": s.verdict,
            "expiry": s.expiry,
            "last": p.last,
            "prev_close": p.prev_close,
            "gap_pct": None if p.gap_pct is None else round(p.gap_pct, 3),
            "ret_prev_close_pct": None if p.ret_prev_close_pct is None else round(p.ret_prev_close_pct, 3),
            "ret_open_pct": round(p.ret_open_pct, 3),
            "rs_resid_pct": round(p.rs_resid_pct, 3),
            "beta": round(p.beta, 2),
            "vwap": round(p.vwap, 2),
            "vwap_z": round(p.vwap_z, 2),
            "vwap_slope_bps": round(p.vwap_slope_bps, 1),
            "or_high": round(p.or_high, 2),
            "or_low": round(p.or_low, 2),
            "or_pos": round(p.or_pos, 2),
            "persistence": round(p.persistence, 3),
            "clv": round(p.clv, 3),
            "session_range_pct": round(p.session_range_pct, 3),
            "move_vs_adr": None if p.move_vs_adr is None else round(p.move_vs_adr, 2),
            "share_volume": round(p.share_volume),
        }
        if o:
            row.update(
                {
                    "atm_strike": o.atm_strike,
                    "atm_iv_pct": None if o.atm_iv is None else round(o.atm_iv * 100, 2),
                    "atm_straddle": None if o.atm_straddle is None else round(o.atm_straddle, 2),
                    "expected_move_pct": None if o.em_pct is None else round(o.em_pct, 3),
                    "expected_move_dollars": None if o.em_dollars is None else round(o.em_dollars, 2),
                    "rr25_vol_pts": None if o.rr25 is None else round(o.rr25, 2),
                    "skew_norm": None if o.skew_norm is None else round(o.skew_norm, 3),
                    "pc_volume_ratio": None if o.pc_volume_ratio is None else round(o.pc_volume_ratio, 3),
                    "pc_oi_ratio": None if o.pc_oi_ratio is None else round(o.pc_oi_ratio, 3),
                    "vol_oi_ratio": None if o.vol_oi_ratio is None else round(o.vol_oi_ratio, 3),
                    "flow_tilt": None if o.flow_tilt is None else round(o.flow_tilt, 3),
                    "oi_tilt": None if o.oi_tilt is None else round(o.oi_tilt, 3),
                    "call_delta_dollars": round(o.call_delta_dollars),
                    "put_delta_dollars": round(o.put_delta_dollars),
                    "max_pain": o.max_pain,
                    "gamma_wall": o.gamma_wall,
                    "gamma_flip": o.gamma_flip,
                    "net_gex_per_pct": round(o.net_gex),
                    "chain_volume": round(o.chain_volume),
                    "total_oi": round(o.total_oi),
                    "avg_atm_spread_pct": None if o.avg_atm_spread_pct is None else round(o.avg_atm_spread_pct * 100, 2),
                }
            )
        for k in COMPONENT_ORDER:
            v = s.components.get(k)
            row[f"c_{k}"] = None if v is None else round(v, 3)
            row[f"w_{k}"] = round(s.contributions.get(k, 0.0), 2)
        row.update(
            {
                "trade_direction": s.plan.direction,
                "strike_atm": s.plan.atm_strike,
                "strike_1em_otm": s.plan.otm_1em_strike,
                "target": None if s.plan.target is None else round(s.plan.target, 2),
                "invalidation": None if s.plan.invalidation is None else round(s.plan.invalidation, 2),
                "flags": "|".join(s.flags),
            }
        )
        rows.append(row)
    return rows


def write_csv(path: str | Path, scores: Sequence[SymbolScore]) -> Path:
    rows = to_rows(scores)
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fields: List[str] = []
    for r in rows:
        for k in r:
            if k not in fields:
                fields.append(k)
    with p.open("w", newline="") as fh:
        wr = csv.DictWriter(fh, fieldnames=fields)
        wr.writeheader()
        wr.writerows(rows)
    return p


def write_json(
    path: str | Path, scores: Sequence[SymbolScore], meta: Dict[str, Any]
) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"meta": meta, "scans": to_rows(scores)}, indent=2))
    return p


def write_html(
    path: str | Path, scores: Sequence[SymbolScore], meta: Dict[str, Any]
) -> Path:
    cols = [
        ("symbol", "Sym"), ("bias", "Bias"), ("confidence", "Conf"),
        ("verdict", "Verdict"), ("last", "Last"), ("ret_prev_close_pct", "%vsPC"),
        ("rs_resid_pct", "RS"), ("vwap_z", "VWAP z"), ("or_pos", "ORB"),
        ("atm_iv_pct", "ATM IV"), ("expected_move_pct", "EM%"),
        ("rr25_vol_pts", "RR25"), ("pc_volume_ratio", "P/C vol"),
        ("vol_oi_ratio", "Vol/OI"), ("max_pain", "Max pain"),
        ("gamma_wall", "Gamma wall"), ("flags", "Flags"),
    ]
    rows = to_rows(scores)
    body = []
    for r in rows:
        tds = []
        for key, _ in cols:
            v = r.get(key)
            cls = ""
            if key == "bias" and isinstance(v, (int, float)):
                cls = "up" if v > 0 else ("down" if v < 0 else "")
            txt = "-" if v is None else (f"{v:,.2f}" if isinstance(v, float) else str(v))
            tds.append(f'<td class="{cls}">{html.escape(txt)}</td>')
        body.append("<tr>" + "".join(tds) + "</tr>")
    head = "".join(f"<th>{html.escape(label)}</th>" for _, label in cols)
    doc = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>0DTE Bias Scan</title>
<style>
 body{{font:14px/1.4 -apple-system,Segoe UI,Roboto,sans-serif;margin:24px;color:#111}}
 h1{{font-size:18px;margin:0 0 4px}} .meta{{color:#666;font-size:12px;margin-bottom:16px}}
 table{{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}}
 th,td{{padding:6px 8px;border-bottom:1px solid #eee;text-align:right;white-space:nowrap}}
 th:first-child,td:first-child,th:nth-child(4),td:nth-child(4),th:last-child,td:last-child{{text-align:left}}
 thead th{{background:#fafafa;position:sticky;top:0}}
 td.up{{color:#0a7a34;font-weight:600}} td.down{{color:#b3261e;font-weight:600}}
 footer{{margin-top:16px;color:#888;font-size:11px}}
</style></head><body>
<h1>0DTE bias scan</h1>
<div class="meta">{html.escape(json.dumps(meta))}</div>
<table><thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody></table>
<footer>Educational tool. Not investment advice. 0DTE options can lose 100% of premium intraday.</footer>
</body></html>"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(doc)
    return p

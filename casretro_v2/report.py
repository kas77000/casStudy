"""The trader page: Execution Quality, Flows, Top clients.  Nothing else.

Three sections, in the order the desk reads them, and the page stops there.

The charts are stacked bars in **quantity** -- executed on top of unfilled, one
bar per day -- with the fill ratio direct-labelled above each bar.  That is one
axis and one unit, which a ratio-only chart would have hidden: 100% of 500
shares and 60% of five million are not the same day, and a bare percentage draws
them the same height.

Palette is the repo's own, reused from `casretro.report` so the two reports look
like one system.  The executed/unfilled pair was run through the data-viz
validator in both modes before it was used: OKLab dE 33.6 normal / 24.7 worst
CVD in light, 31.8 / 26.8 in dark, both well clear of the 15 and 8 floors, with
lightness and chroma inside the band and contrast over 3:1 against each surface.
"""

from __future__ import annotations

import datetime as dt
import os

import numpy as np
import pandas as pd

from casretro import config as C
from casretro.report import _CSS, _esc

from . import config as V
from . import metrics as M
from .period import PeriodData

# --------------------------------------------------------------------------- #
# The two series                                                               #
# --------------------------------------------------------------------------- #

#: Executed and unfilled, in fixed order.  Never cycled, never reassigned by
#: rank -- the colour follows the meaning.
SERIES_EXECUTED = "var(--series-1)"
SERIES_UNFILLED = "var(--series-2)"

DASH = "–"


# --------------------------------------------------------------------------- #
# Formatting                                                                   #
# --------------------------------------------------------------------------- #

def _ok(v) -> bool:
    return v is not None and not (isinstance(v, float) and (np.isnan(v) or np.isinf(v)))


def _qty(v) -> str:
    return f"{float(v):,.0f}" if _ok(v) else DASH


def _pct(v, nd: int = 1) -> str:
    return f"{float(v):,.{nd}f}%" if _ok(v) else DASH


def _usd(v) -> str:
    """USD in the units a desk reads: m, k, then dollars."""
    if not _ok(v):
        return DASH
    x = float(v)
    sign = "-" if x < 0 else ""
    a = abs(x)
    if a >= 1e6:
        return f"{sign}${a / 1e6:,.2f}m"
    if a >= 1e3:
        return f"{sign}${a / 1e3:,.1f}k"
    return f"{sign}${a:,.0f}"


def _day(d) -> str:
    if isinstance(d, (dt.date, dt.datetime)):
        return f"{d:%a %d %b}"
    return str(d)


def _ist(t: dt.time) -> str:
    return f"{C.to_ist(t):%H:%M} IST ({t:%H:%M} HKT)"


def _flow_label(flow: str) -> str:
    """`--flow both` is a CLI argument, not something to print on a page."""
    return {
        "both": "SILK and agency flow",
        "silk": "SILK flow",
        "agency": "Agency flow",
    }.get(str(flow).lower(), f"{flow} flow")


# --------------------------------------------------------------------------- #
# Page furniture                                                              #
# --------------------------------------------------------------------------- #

_V2_CSS = """
.lede{font-size:15px;line-height:1.55;max-width:80ch;margin:0 0 20px;}
.note{background:var(--surface-1);border:1px solid var(--border);
 border-left:3px solid var(--warning);border-radius:8px;padding:11px 14px;
 margin:0 0 16px;font-size:13px;color:var(--text-secondary);}
.note b{color:var(--text-primary);font-weight:600;}
.take{font-size:13.5px;color:var(--text-secondary);margin:0 0 14px;max-width:82ch;}
.grid2{display:flex;flex-wrap:wrap;gap:18px;}
.grid2 > *{flex:1 1 460px;min-width:0;}
table.v2{border-collapse:collapse;width:100%;font-size:12.5px;
 font-variant-numeric:tabular-nums;}
table.v2 th,table.v2 td{padding:7px 10px;border-bottom:1px solid var(--grid);
 text-align:left;white-space:nowrap;}
table.v2 th{font-size:10.5px;text-transform:uppercase;letter-spacing:.05em;
 color:var(--text-muted);font-weight:600;position:sticky;top:0;
 background:var(--surface-1);}
table.v2 td.num{text-align:right;}
table.v2 tr.total td{border-top:1px solid var(--axis);font-weight:650;
 border-bottom:none;}
table.v2 tr.total td.num{font-variant-numeric:tabular-nums;}
.swatch{display:inline-block;width:10px;height:10px;border-radius:2px;
 margin-right:6px;vertical-align:-1px;}
"""


def _cells(headers: list[tuple[str, str]], values: list[str]) -> str:
    out = []
    for (_label, cls), v in zip(headers, values):
        attr = f' class="{cls}"' if cls else ""
        out.append(f"<td{attr}>{v}</td>")     # values are pre-escaped by callers
    return "".join(out)


def _table(headers: list[tuple[str, str]], rows: list[list[str]],
           total: list[str] | None = None) -> str:
    if not rows:
        return '<p class="sub">nothing to show</p>'
    head = "".join(f"<th>{_esc(h)}</th>" for h, _ in headers)
    body = [f"<tr>{_cells(headers, r)}</tr>" for r in rows]
    if total:
        body.append(f'<tr class="total">{_cells(headers, total)}</tr>')
    return (f'<div class="scroll"><table class="v2"><thead><tr>{head}</tr></thead>'
            f'<tbody>{"".join(body)}</tbody></table></div>')


def _legend(items: list[tuple[str, str]]) -> str:
    return ('<div class="legend">' + "".join(
        f'<span><i style="background:{c}"></i>{_esc(label)}</span>'
        for label, c in items
    ) + "</div>")


# --------------------------------------------------------------------------- #
# The chart                                                                    #
# --------------------------------------------------------------------------- #

def _stacked_day_chart(
    rows: list[tuple[str, float, float, float]],
    *,
    width: int = 1060,
    height: int = 260,
) -> str:
    """One bar per day: executed stacked under unfilled, ratio labelled on top.

    `rows` is (label, executed, unfilled, ratio_pct).  Both segments are the same
    measure in the same unit, so they share one axis; a 2px surface gap keeps the
    two fills from reading as one block, and the ratio is the only number printed
    on the plot -- the table underneath carries the rest.
    """
    rows = [r for r in rows if _ok(r[1]) or _ok(r[2])]
    if not rows:
        return '<p class="sub">nothing to plot</p>'

    # pad_t leaves room for the ratio label above the tallest bar, plus the 2px
    # the stack gap pushes that bar over the plot height.
    pad_l, pad_r, pad_t, pad_b = 10, 10, 34, 34
    plot_h = height - pad_t - pad_b
    plot_w = width - pad_l - pad_r
    totals = [(e or 0) + (u or 0) for _, e, u, _ in rows]
    vmax = max(totals) or 1.0

    slot = plot_w / len(rows)
    bar_w = min(76.0, slot * 0.5)
    gap = 2.0                       # surface gap between the two fills
    base_y = pad_t + plot_h
    out = [f'<line x1="{pad_l}" y1="{base_y}" x2="{width - pad_r}" y2="{base_y}" '
           f'stroke="var(--axis)" stroke-width="1"/>']

    for i, (label, execd, unfilled, ratio) in enumerate(rows):
        execd = float(execd or 0.0)
        unfilled = float(unfilled or 0.0)
        cx = pad_l + slot * (i + 0.5)
        x = cx - bar_w / 2
        h_exec = execd / vmax * plot_h
        h_unf = unfilled / vmax * plot_h

        # Executed sits on the baseline with the rounded end there; unfilled
        # stacks above it, 2px clear, so the split is legible at any height.
        y_exec = base_y - h_exec
        if h_exec > 0:
            out.append(
                f'<g><title>{_esc(label)} — executed {execd:,.0f}</title>'
                f'<rect class="mark" x="{x:.1f}" y="{y_exec:.1f}" width="{bar_w:.1f}" '
                f'height="{max(h_exec, 1.5):.1f}" rx="4" fill="{SERIES_EXECUTED}"/></g>'
            )
        if h_unf > 0:
            y_unf = y_exec - gap - h_unf
            out.append(
                f'<g><title>{_esc(label)} — sent but not executed {unfilled:,.0f}</title>'
                f'<rect class="mark" x="{x:.1f}" y="{y_unf:.1f}" width="{bar_w:.1f}" '
                f'height="{max(h_unf, 1.5):.1f}" rx="4" fill="{SERIES_UNFILLED}"/></g>'
            )

        top = base_y - h_exec - (gap + h_unf if h_unf > 0 else 0)
        if _ok(ratio):
            out.append(
                f'<text x="{cx:.1f}" y="{top - 8:.1f}" text-anchor="middle" '
                f'font-size="12" font-weight="600" fill="var(--text-primary)">'
                f'{float(ratio):,.0f}%</text>'
            )
        out.append(
            f'<text x="{cx:.1f}" y="{height - 12}" text-anchor="middle" '
            f'font-size="12" fill="var(--text-secondary)">{_esc(label)}</text>'
        )

    return (f'<svg viewBox="0 0 {width} {height}" width="{width}" height="{height}" '
            f'role="img">{"".join(out)}</svg>')


# --------------------------------------------------------------------------- #
# Sections                                                                     #
# --------------------------------------------------------------------------- #

def _execution_quality(data: PeriodData) -> str:
    eq = data.execution_quality
    if eq is None or eq.empty:
        return '<p class="sub">no close child order in the period</p>'

    totals = M.execution_quality_totals(eq)
    parts = [
        '<p class="take">One bar per day, in shares: what we sent to the auction, '
        'split into the part that traded and the part that did not. The number '
        'above each bar is the fill ratio &mdash; executed over sent. The table '
        'carries the same days in USD.</p>',
        _legend([("Executed", SERIES_EXECUTED), ("Sent, not executed", SERIES_UNFILLED)]),
    ]

    for otype in V.OTYPES:
        sub = eq[eq["otype_kind"] == otype]
        parts.append(f"<h3>{_esc(otype.title())}</h3>")
        if sub.empty:
            parts.append(f'<p class="sub">no {otype.lower()} child order reached '
                         f'the auction in this period</p>')
            continue

        rows = [
            (_day(r["date"]), r["exec_qty"], r["unfilled_qty"], r["fill_rate_pct"])
            for _, r in sub.iterrows()
        ]
        parts.append(f'<div class="card">{_stacked_day_chart(rows)}</div>')

        head = [("Day", ""), ("Sent", "num"), ("Executed", "num"),
                ("Fill ratio", "num"), ("Executed notional", "num"),
                ("Total notional sent", "num")]
        body = [
            [_esc(_day(r["date"])), _qty(r["sent_qty"]), _qty(r["exec_qty"]),
             _pct(r["fill_rate_pct"]), _usd(r["exec_notional_usd"]),
             _usd(r["sent_notional_usd"])]
            for _, r in sub.iterrows()
        ]
        trow = totals[totals["otype_kind"] == otype]
        total = None
        if not trow.empty:
            t = trow.iloc[0]
            total = ["Period", _qty(t["sent_qty"]), _qty(t["exec_qty"]),
                     _pct(t["fill_rate_pct"]), _usd(t["exec_notional_usd"]),
                     _usd(t["sent_notional_usd"])]
        parts.append(_table(head, body, total))

        # Market orders have no price of their own, so their unfilled quantity is
        # valued at the auction close.  Say how much of the total that is.
        sub_pct = pd.to_numeric(sub["substituted_pct"], errors="coerce").max()
        if _ok(sub_pct) and sub_pct > 0:
            parts.append(
                f'<p class="sub">Up to {_pct(sub_pct)} of a day&rsquo;s '
                f'&ldquo;total notional sent&rdquo; here is unfilled quantity '
                f'valued at the auction&rsquo;s closing price, because a '
                f'{otype.lower()} order carries no price of its own to be '
                f'unfilled at.</p>'
            )
    return "".join(parts)


def _flows(data: PeriodData) -> str:
    f = data.flows
    if f is None or f.empty:
        return '<p class="sub">nothing to show</p>'

    head = [
        ("Day", ""), ("Flow", ""), ("Type", ""),
        ("Orders", "num"), ("Child orders", "num"),
        ("Notional traded in close", "num"), ("Fill rate", "num"),
        ("Symbols", "num"), ("Market close volume", "num"),
        ("Market notional", "num"), ("% of market notional", "num"),
    ]
    body = [
        [
            _esc(_day(r["date"])), _esc(str(r["flow"])), _esc(str(r["otype_kind"]).title()),
            _qty(r["n_orders"]), _qty(r["n_children"]),
            _usd(r["exec_notional_usd"]), _pct(r["fill_rate_pct"]),
            _qty(r["n_syms"]), _qty(r["mkt_close_qty"]),
            _usd(r["mkt_close_notional_usd"]), _pct(r["our_pct_of_market_notional"], 3),
        ]
        for _, r in f.iterrows()
    ]
    total = None
    t = data.flows_total
    if t is not None:
        total = ["Period", "All", "All", _qty(t["n_orders"]), _qty(t["n_children"]),
                 _usd(t["exec_notional_usd"]), _pct(t["fill_rate_pct"]),
                 _qty(t["n_syms"]), _qty(t["mkt_close_qty"]),
                 _usd(t["mkt_close_notional_usd"]),
                 _pct(t["our_pct_of_market_notional"], 3)]

    return (
        '<p class="take">One row per day, flow and order type. The market columns '
        'are the whole auction <em>in the symbols that row traded</em>: close '
        f'volume is everything printed from {_esc(_ist(V.CLOSE_VOLUME_FROM))} '
        'onwards, valued at the closing price &mdash; the first print between '
        f'{_esc(_ist(V.CLOSE_PRICE_WINDOW[0]))} and '
        f'{_esc(_ist(V.CLOSE_PRICE_WINDOW[1]))}, which is where the auction '
        'freezes.</p>'
        + _table(head, body, total)
        + '<p class="sub">Because each row&rsquo;s market columns cover only its '
        'own symbols, rows that share a name overlap: the market notional column '
        'does not add up down the page. The Period row is recomputed over the '
        'distinct symbols of the whole period rather than summed.</p>'
    )


def _clients(data: PeriodData) -> str:
    flows = data.flows_present
    if not flows:
        return '<p class="sub">nothing to show</p>'

    head = [
        (V.CLIENT_COLUMN.title(), ""), ("Days", "num"), ("Orders", "num"),
        ("Child orders", "num"), ("Symbols", "num"),
        ("Notional traded in close", "num"), ("Fill rate", "num"),
        ("% of market notional", "num"),
    ]
    parts = [
        f'<p class="take">The {V.TOP_CLIENTS} biggest {V.CLIENT_COLUMN}s of each '
        f'flow, by notional traded in the closing auction. &ldquo;% of market '
        f'notional&rdquo; is that {V.CLIENT_COLUMN}&rsquo;s share of the auction '
        f'in the names it traded.</p>'
    ]
    for flow in flows:
        top = data.clients(flow)
        parts.append(f"<h3>{_esc(str(flow).title())}</h3>")
        if top is None or top.empty:
            parts.append('<p class="sub">nothing in this flow</p>')
            continue
        body = [
            [_esc(str(r[V.CLIENT_COLUMN]) or "(none)"), _qty(r.get("n_days")),
             _qty(r["n_orders"]), _qty(r["n_children"]), _qty(r["n_syms"]),
             _usd(r["exec_notional_usd"]), _pct(r["fill_rate_pct"]),
             _pct(r["our_pct_of_market_notional"], 3)]
            for _, r in top.iterrows()
        ]
        parts.append(_table(head, body))
    return "".join(parts)


# --------------------------------------------------------------------------- #
# The page                                                                     #
# --------------------------------------------------------------------------- #

def _provisional(data: PeriodData) -> str:
    if not data.rt_dates:
        return ""
    days = ", ".join(d.isoformat() for d in data.rt_dates)
    return (f'<div class="note"><b>Provisional for {_esc(days)}.</b> That day was '
            f'read from the live trading records, before the end-of-day books '
            f'were finalised. Every earlier day is final.</div>')


def _missing(data: PeriodData) -> str:
    if not data.missing:
        return ""
    days = ", ".join(d.isoformat() for d in data.missing)
    return (f'<div class="note"><b>No data on {_esc(days)}.</b> Excluded from '
            f'every number below rather than counted as a zero.</div>')


def write_html(data: PeriodData, path: str) -> str:
    parts: list[str] = [
        "<h1>Closing auction &ndash; execution review</h1>",
        f'<p class="sub">{_esc(data.label)}'
        + (f' &nbsp;&middot;&nbsp; {len(data.dates)} trading days'
           if data.is_multi_day else "")
        + f' &nbsp;&middot;&nbsp; {_esc(_flow_label(data.flow))}'
        + f' &nbsp;&middot;&nbsp; NSE closing auction'
        + (f' &nbsp;&middot;&nbsp; {_esc(data.fx_note)}' if data.fx_note else "")
        + "</p>",
        _provisional(data),
        _missing(data),
        "<h2>Execution Quality</h2>",
        _execution_quality(data),
        "<h2>Flows</h2>",
        _flows(data),
        f"<h2>Top {V.TOP_CLIENTS} clients</h2>",
        _clients(data),
        f'<p class="foot">Covers child orders sent to a CLOSE venue only. '
        f'Executed quantity is priced at the fill price off the execution tape; '
        f'unfilled quantity at the child order&rsquo;s own price for limit '
        f'orders, and at the auction&rsquo;s closing price for market orders, '
        f'which carry none. Market close volume is every print from '
        f'{_esc(_ist(V.CLOSE_VOLUME_FROM))} to the end of the day; the closing '
        f'price is the first print between '
        f'{_esc(_ist(V.CLOSE_PRICE_WINDOW[0]))} and '
        f'{_esc(_ist(V.CLOSE_PRICE_WINDOW[1]))}. '
        f'Generated {dt.datetime.now():%Y-%m-%d %H:%M}.</p>',
    ]

    doc = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>Closing auction review {_esc(data.label)}</title>"
        f"<style>{_CSS}{_V2_CSS}</style></head>"
        f'<body class="viz-root"><div class="wrap">{"".join(parts)}</div></body></html>'
    )
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(doc)
    return path


def write_csvs(data: PeriodData, outdir: str) -> list[str]:
    """The three sections as CSVs, so every number on the page is checkable."""
    os.makedirs(outdir, exist_ok=True)
    written = []
    frames = {
        "execution_quality": data.execution_quality,
        "flows": data.flows,
        "children": data.children,
        "market": data.market,
    }
    for flow in data.flows_present:
        frames[f"top_clients_{str(flow).lower()}"] = data.clients(flow)
    for name, df in frames.items():
        if df is None or df.empty:
            continue
        p = os.path.join(outdir, f"{name}.csv")
        df.to_csv(p, index=False, float_format="%.4f")
        written.append(p)
    return written

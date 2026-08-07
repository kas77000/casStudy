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
import math
import os

import numpy as np
import pandas as pd

from casretro import config as C
from casretro.report import _CSS, _esc, _tile

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

#: Narrowest a day label may sit from its neighbour before the axis is thinned.
#: "Mon 03 Aug" measures ~66px at 12px; the short forms roughly 36-44px, so this
#: is the tightest of them plus a little air.
MIN_LABEL_PX = 44.0


# --------------------------------------------------------------------------- #
# Formatting                                                                   #
# --------------------------------------------------------------------------- #

def _ok(v) -> bool:
    return v is not None and not (isinstance(v, float) and (np.isnan(v) or np.isinf(v)))


def _qty(v) -> str:
    return f"{float(v):,.0f}" if _ok(v) else DASH


def _pct(v, nd: int = 2) -> str:
    """Two decimals at most, with trailing zeros dropped.

    100.00 reads as 100%, 10.70 as 10.7%, 44.44 stays 44.44%.  A number that is
    not zero but rounds to zero at two decimals reads `<0.01%` rather than `0%`
    -- a real 0.004% share of the auction is small, not absent, and the two
    should not print the same.
    """
    if not _ok(v):
        return DASH
    x = float(v)
    if x and abs(x) < 0.5 * 10 ** -nd:
        sign = "-" if x < 0 else ""
        return f"{sign}<{10 ** -nd:.{nd}f}%"
    s = f"{x:,.{nd}f}"
    if "." in s:                      # only strip a fractional part, never "1,000"
        s = s.rstrip("0").rstrip(".")
    return f"{s}%"


def _usd(v) -> str:
    """USD in the units a desk reads: bn, m, k, then dollars.

    The bn step matters here: a week's notional sent to the auction runs past a
    billion, and "$1,379.13m" is a number nobody reads at a glance.
    """
    if not _ok(v):
        return DASH
    x = float(v)
    sign = "-" if x < 0 else ""
    a = abs(x)
    if a >= 1e9:
        return f"{sign}${a / 1e9:,.2f}bn"
    if a >= 1e6:
        return f"{sign}${a / 1e6:,.2f}m"
    if a >= 1e3:
        return f"{sign}${a / 1e3:,.1f}k"
    return f"{sign}${a:,.0f}"


def _day(d) -> str:
    if isinstance(d, (dt.date, dt.datetime)):
        return f"{d:%a %d %b}"
    return str(d)


def _day_axis(d, n_days: int) -> str:
    """A day label that still fits when the charts sit two to a row.

    "Mon 03 Aug" needs ~66px; a half-width chart gives a 10-day range 50px per
    bar.  The label shortens with the range rather than overprinting its
    neighbour -- the table underneath always carries the full date.
    """
    if not isinstance(d, (dt.date, dt.datetime)):
        return str(d)
    if n_days <= 6:
        return f"{d:%a %d %b}"
    if n_days <= 12:
        return f"{d:%d %b}"
    return f"{d:%d/%m}"


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
/* Wider than the desk's own report (1200px), because this page puts the market
   and limit tables side by side.  Six columns need ~684px each -- "TOTAL
   NOTIONAL SENT" is the widest header and sets it -- so 1500px leaves each
   column 741px and neither table needs a horizontal scrollbar.  It is a cap,
   not a width: a narrower screen still uses what it has. */
.wrap{max-width:1500px;}
.lede{font-size:15px;line-height:1.55;margin:0 0 20px;}
.note{background:var(--surface-1);border:1px solid var(--border);
 border-left:3px solid var(--warning);border-radius:8px;padding:11px 14px;
 margin:0 0 16px;font-size:13px;color:var(--text-secondary);}
.note b{color:var(--text-primary);font-weight:600;}
/* Captions run the full width of the content they describe: these are one- or
   two-line notes above a 1200px table, not body copy that needs a reading
   measure. */
.take{font-size:13.5px;color:var(--text-secondary);margin:0 0 14px;}
.grid2{display:flex;flex-wrap:wrap;gap:18px;align-items:flex-start;}
.grid2 > *{flex:1 1 440px;min-width:0;}
.grid2 h3{margin-top:0;}
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
    width: int = 660,
    height: int = 260,
) -> str:
    """One bar per day: executed stacked under unfilled, ratio labelled on top.

    `rows` is (label, executed, unfilled, ratio_pct).  Both segments are the same
    measure in the same unit, so they share one axis; a 2px surface gap keeps the
    two fills from reading as one block, and the ratio is the only number printed
    on the plot -- the table underneath carries the rest.

    The default width is half the content column, because the two order types sit
    side by side: rendering at roughly the size it is displayed keeps the labels
    at their intended 12px rather than shrinking them with the SVG.

    The ratio is formatted by `_pct`, so it carries up to two decimals and drops
    trailing zeros: a limit book that fills 0.4% and one that fills 4.1% are told
    apart, while a clean 100% is not printed as 100.00%.
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

    # Past a few weeks the bars are narrower than their own labels, so label
    # every nth bar rather than overprinting.  The table carries every day, so
    # thinning the axis costs nothing that is not still on the page.
    stride = max(1, int(math.ceil(MIN_LABEL_PX / slot)))
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
        if _ok(ratio) and i % stride == 0:
            out.append(
                f'<text x="{cx:.1f}" y="{top - 8:.1f}" text-anchor="middle" '
                f'font-size="12" font-weight="600" fill="var(--text-primary)">'
                f'{_pct(ratio)}</text>'
            )
        if i % stride == 0:
            out.append(
                f'<text x="{cx:.1f}" y="{height - 12}" text-anchor="middle" '
                f'font-size="12" fill="var(--text-secondary)">{_esc(label)}</text>'
            )

    return (f'<svg viewBox="0 0 {width} {height}" width="{width}" height="{height}" '
            f'role="img">{"".join(out)}</svg>')


# --------------------------------------------------------------------------- #
# Sections                                                                     #
# --------------------------------------------------------------------------- #

def _kpis(data: PeriodData) -> str:
    """The row a trader or a manager reads before anything else.

    Six tiles, all anchored on notional executed in the auction: what we put
    through, what we sent to get it, how much of it filled, how big that made us
    in the auction, what was left, and who it was for.  Everything is the period
    total -- ratios recomputed from summed quantities, never averaged across
    days.
    """
    k = M.headline(data.children, data.market)
    if not k:
        return ""

    by = k.get("by_otype", {})
    mkt = by.get(V.OTYPE_MARKET, {})
    lim = by.get(V.OTYPE_LIMIT, {})

    split = " · ".join(
        f"{name.lower()} {_usd(d.get('exec_notional_usd'))}"
        for name, d in ((V.OTYPE_MARKET, mkt), (V.OTYPE_LIMIT, lim)) if d
    )

    who = f"{k['n_clients']:,} clients"
    if k.get("top_client_pct") is not None:
        who += f" · top {_pct(k['top_client_pct'], 0)}"

    best = ""
    if k.get("best_day") is not None:
        best = f"biggest day {_day(k['best_day'])} at {_pct(k.get('best_day_pct'), 0)}"

    # Market and limit get a tile each rather than sharing one: they are the two
    # halves of the page below, they behave nothing like each other, and two
    # numbers crammed into a single tile read as a footnote instead of a figure.
    def otype_tile(name: str, d: dict) -> str:
        if not d:
            return _tile(name.title(), "—", "nothing reached the auction")
        return _tile(name.title(), _pct(d.get("fill_rate_qty_pct")),
                     f"filled · {_usd(d.get('exec_notional_usd'))} traded")

    tiles = [
        _tile("Notional executed in the close", _usd(k["exec_notional_usd"]),
              split or f"{_qty(k['exec_qty'])} shares"),
        _tile("Fill rate", _pct(k["fill_rate_notional_pct"]),
              f"of notional · {_pct(k['fill_rate_qty_pct'])} of shares"),
        otype_tile(V.OTYPE_MARKET, mkt),
        otype_tile(V.OTYPE_LIMIT, lim),
        _tile("Share of the auction", _pct(k.get("share_of_auction_pct")),
              f"of {_usd(k.get('mkt_close_notional_usd'))} printed in our names"
              + (f" · {_pct(k['auction_coverage_pct'], 0)} of our notional priced"
                 if _ok(k.get("auction_coverage_pct"))
                 and k["auction_coverage_pct"] < 99.5 else "")),
    ]

    foot = f"{k['n_syms']:,} names · {who}"
    if best:
        foot += f" · {best}"
    return (f'<div class="tiles">{"".join(tiles)}</div>'
            f'<p class="take">{_esc(foot)}</p>')


def _execution_quality(data: PeriodData) -> str:
    eq = data.execution_quality
    if eq is None or eq.empty:
        return '<p class="sub">no close child order in the period</p>'

    totals = M.execution_quality_totals(eq)
    parts = [
        '<p class="take">One bar per day, in shares: the size of the orders that '
        'competed in the auction, split into the part that traded and the part '
        'that did not. The number above each bar is the fill ratio &mdash; '
        'executed over sent. The tables carry the same days in USD. Orders that '
        'traded nothing at all are not counted &mdash; they never competed, and '
        'including them would measure intent rather than execution.</p>',
        _legend([("Executed", SERIES_EXECUTED), ("Sent, not executed", SERIES_UNFILLED)]),
    ]

    # Market and limit sit side by side, charts on one row and their tables on
    # the next, so the two order types are read against each other rather than
    # one scrolled past to reach the other.  Both columns are built in the same
    # pass and emitted as two rows.
    charts: list[str] = []
    tables: list[str] = []

    for otype in V.OTYPES:
        sub = eq[eq["otype_kind"] == otype]
        title = f"<h3>{_esc(otype.title())}</h3>"
        if sub.empty:
            note = (f'<p class="sub">no {otype.lower()} child order reached the '
                    f'auction in this period</p>')
            charts.append(f"<div>{title}{note}</div>")
            tables.append(f"<div>{title}{note}</div>")
            continue

        rows = [
            (_day_axis(r["date"], len(sub)), r["exec_qty"], r["unfilled_qty"],
             r["fill_rate_pct"])
            for _, r in sub.iterrows()
        ]
        charts.append(
            f'<div>{title}<div class="card">{_stacked_day_chart(rows)}</div></div>'
        )

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

        # Market orders have no price of their own, so their unfilled quantity is
        # valued at the auction close.  Say how much of the total that is.
        sub_pct = pd.to_numeric(sub["substituted_pct"], errors="coerce").max()
        caveat = ""
        if _ok(sub_pct) and sub_pct > 0:
            caveat = (
                f'<p class="sub">Up to {_pct(sub_pct)} of a day&rsquo;s '
                f'&ldquo;total notional sent&rdquo; here is unfilled quantity '
                f'valued at the auction&rsquo;s closing price, because a '
                f'{otype.lower()} order carries no price of its own to be '
                f'unfilled at.</p>'
            )
        tables.append(f"<div>{title}{_table(head, body, total)}{caveat}</div>")

    parts.append(f'<div class="grid2">{"".join(charts)}</div>')
    parts.append(f'<div class="grid2">{"".join(tables)}</div>')
    return "".join(parts)


def _flows(data: PeriodData) -> str:
    f = data.flows
    if f is None or f.empty:
        return '<p class="sub">nothing to show</p>'

    head = [
        ("Day", ""), ("Flow", ""), ("Type", ""),
        ("Orders", "num"), ("Child orders", "num"),
        ("Notional traded in close", "num"), ("Fill rate", "num"),
        ("Symbols", "num"),
        ("Market notional", "num"), ("% of market notional", "num"),
    ]
    body = [
        [
            _esc(_day(r["date"])), _esc(str(r["flow"])), _esc(str(r["otype_kind"]).title()),
            _qty(r["n_orders"]), _qty(r["n_children"]),
            _usd(r["exec_notional_usd"]), _pct(r["fill_rate_pct"]),
            _qty(r["n_syms"]),
            _usd(r["mkt_close_notional_usd"]), _pct(r["our_pct_of_market_notional"]),
        ]
        for _, r in f.iterrows()
    ]
    total = None
    t = data.flows_total
    if t is not None:
        total = ["Period", "All", "All", _qty(t["n_orders"]), _qty(t["n_children"]),
                 _usd(t["exec_notional_usd"]), _pct(t["fill_rate_pct"]),
                 _qty(t["n_syms"]),
                 _usd(t["mkt_close_notional_usd"]),
                 _pct(t["our_pct_of_market_notional"])]

    return (
        '<p class="take">One row per day, flow and order type. Market notional is '
        'the auction itself <em>in the symbols that row traded</em>: everything '
        f'printed between {_esc(_ist(V.CLOSE_WINDOW[0]))} and '
        f'{_esc(_ist(V.CLOSE_WINDOW[1]))}, where the auction freezes, valued at '
        'the first price in that window. Trading-at-last, after 18:00, is not '
        'counted. The volume behind it is in <code>csv/flows.csv</code>.</p>'
        + _table(head, body, total)
        + '<p class="sub">Because each row&rsquo;s market notional covers only its '
        'own symbols, rows that share a name overlap: that column '
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
             _pct(r["our_pct_of_market_notional"])]
            for _, r in top.iterrows()
        ]
        parts.append(_table(head, body))
    return "".join(parts)


# --------------------------------------------------------------------------- #
# The page                                                                     #
# --------------------------------------------------------------------------- #

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
        _missing(data),
        _kpis(data),
        "<h2>Execution Quality</h2>",
        _execution_quality(data),
        "<h2>Flows</h2>",
        _flows(data),
        f"<h2>Top {V.TOP_CLIENTS} clients</h2>",
        _clients(data),
        f'<p class="foot">Covers child orders sent to a CLOSE venue that traded '
        f'at least in part and were still on the market after '
        f'{_esc(_ist(V.OFF_MARKET_AFTER))}; a limit order also has to have been '
        f'priced at or through the price it achieved. Quantities are the order&rsquo;s '
        f'own &mdash; size sent, make executed &mdash; at its average fill price. '
        f'Unfilled quantity is valued at the child order&rsquo;s own price for '
        f'limit orders, and at the auction&rsquo;s closing price for market '
        f'orders, which carry none. The auction is measured over '
        f'{_esc(_ist(V.CLOSE_WINDOW[0]))} to {_esc(_ist(V.CLOSE_WINDOW[1]))}: '
        f'close volume is the size printed in that window and the closing price '
        f'the first price in it. Trading-at-last, after 18:00, is excluded. '
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

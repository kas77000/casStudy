"""The two-page layout: charts first, data behind it.

The v1 page put charts and their tables together and ran straight down.  Most
readers stop after the pictures; the ones who do not want the numbers want all
of them.  So this splits into two:

    page 1   the KPI row and every chart -- market and limit per flow, then the
             top clients of each flow as horizontal bars
    page 2   every table

Both pages live in **one file**, switched by a CSS-only tab.  No JavaScript, so
it still works in an email client that blocks it, and it is still one
attachment rather than two files with a link between them that breaks the moment
one of them is forwarded.  Printing overrides the tab and lays both pages out.

v1 (`report.py`) is still written alongside this, so the two can be compared
until this one replaces it.
"""

from __future__ import annotations

import datetime as dt
import os

import numpy as np
import pandas as pd

from casretro.report import _CSS, _clip, _esc

from . import config as V
from . import metrics as M
from .period import PeriodData
from .report import (
    SERIES_EXECUTED,
    SERIES_UNFILLED,
    _V2_CSS,
    _day,
    _day_axis,
    _flow_label,
    _flows,
    _ist,
    _kpis,
    _legend,
    _ok,
    _pct,
    _qty,
    _stacked_day_chart,
    _table,
    _usd,
)

# --------------------------------------------------------------------------- #
# Page furniture                                                               #
# --------------------------------------------------------------------------- #

_PAGES_CSS = """
/* CSS-only tabs: the radios are the state, so no script has to run for the
   page to work in whatever client it is opened in. */
.pgstate{position:absolute;width:0;height:0;opacity:0;pointer-events:none;}
.tabs{display:flex;gap:6px;margin:0 0 22px;border-bottom:1px solid var(--border);}
.tabs label{padding:9px 16px;font-size:13px;font-weight:600;cursor:pointer;
 color:var(--text-muted);border-bottom:2px solid transparent;margin-bottom:-1px;
 user-select:none;}
.tabs label:hover{color:var(--text-secondary);}
.page{display:none;}
#pg1:checked ~ .tabs label[for="pg1"],
#pg2:checked ~ .tabs label[for="pg2"]{color:var(--text-primary);
 border-bottom-color:var(--series-1);}
#pg1:checked ~ .page.pg-1,
#pg2:checked ~ .page.pg-2{display:block;}
.tabs label .cnt{font-weight:400;color:var(--text-muted);margin-left:6px;}
/* Paper has no tabs.  Print everything, each page starting fresh. */
@media print{
  .tabs{display:none;}
  .page{display:block !important;}
  .page.pg-2{break-before:page;}
}
.flowhead{font-size:14px;font-weight:650;margin:26px 0 2px;}
.flowhead .sub2{font-weight:400;color:var(--text-muted);font-size:12.5px;
 margin-left:8px;}
.flownote{font-size:12.5px;color:var(--text-secondary);margin:0 0 10px;
 padding-left:10px;border-left:2px solid var(--border);}
"""


def _flow_note(flow) -> str:
    """The line a flow needs said before its numbers are read, if it has one."""
    note = V.FLOW_NOTES.get(str(flow))
    return f'<p class="flownote">{_esc(note)}</p>' if note else ""


def _tabs() -> str:
    return (
        '<nav class="tabs">'
        '<label for="pg1">Overview<span class="cnt">charts</span></label>'
        '<label for="pg2">Data<span class="cnt">tables</span></label>'
        "</nav>"
    )


# --------------------------------------------------------------------------- #
# The horizontal chart                                                         #
# --------------------------------------------------------------------------- #

def _hbar_chart(
    rows: list[tuple[str, float, float, float]],
    *,
    width: int = 1440,
    label_w: int = 300,
    row_h: int = 40,
    money: bool = True,
) -> str:
    """One horizontal bar per client: executed, then the part that did not fill.

    The same encoding as the day charts turned on its side -- executed and
    unfilled are the same measure in the same unit, stacked, with a 2px surface
    gap between them and the fill ratio direct-labelled at the end.  Horizontal
    because client names are long and would never fit under a vertical bar.

    Measured in **notional**, not shares, because that is what the clients are
    ranked by: bars in shares beside a ranking by notional would put the longest
    bar somewhere other than the top.
    """
    rows = [r for r in rows if _ok(r[1]) or _ok(r[2])]
    if not rows:
        return '<p class="sub">nothing to plot</p>'

    # Room past the longest bar for its trailing label: "$1,366.06m · 100.0%" is
    # about 140px at 12px, so the label never runs off the viewBox.
    pad_r = 150
    plot_w = width - label_w - pad_r
    height = row_h * len(rows) + 10
    totals = [(e or 0) + (u or 0) for _, e, u, _ in rows]
    vmax = max(totals) or 1.0
    fmt = _usd if money else _qty
    gap = 2.0
    bar_h = min(22.0, row_h * 0.55)

    out = [f'<line x1="{label_w}" y1="0" x2="{label_w}" y2="{height}" '
           f'stroke="var(--axis)" stroke-width="1"/>']

    for i, (label, execd, unfilled, ratio) in enumerate(rows):
        execd = float(execd or 0.0)
        unfilled = float(unfilled or 0.0)
        y = i * row_h + 6
        w_exec = execd / vmax * plot_w
        w_unf = unfilled / vmax * plot_w

        out.append(
            f'<text x="{label_w - 10}" y="{y + bar_h - 5:.1f}" text-anchor="end" '
            f'font-size="12.5" fill="var(--text-secondary)">{_esc(label)}</text>'
        )
        if w_exec > 0:
            out.append(
                f'<g><title>{_esc(label)} — executed {fmt(execd)}</title>'
                f'<rect class="mark" x="{label_w}" y="{y:.1f}" '
                f'width="{max(w_exec, 1.5):.1f}" height="{bar_h:.1f}" rx="4" '
                f'fill="{SERIES_EXECUTED}"/></g>'
            )
        if w_unf > 0:
            out.append(
                f'<g><title>{_esc(label)} — sent but not executed {fmt(unfilled)}'
                f'</title><rect class="mark" x="{label_w + w_exec + gap:.1f}" '
                f'y="{y:.1f}" width="{max(w_unf, 1.5):.1f}" height="{bar_h:.1f}" '
                f'rx="4" fill="{SERIES_UNFILLED}"/></g>'
            )

        end = label_w + w_exec + (gap + w_unf if w_unf > 0 else 0)
        tail = f"{fmt(execd)}"
        if _ok(ratio):
            tail += f" · {_pct(ratio)}"
        out.append(
            f'<text x="{end + 8:.1f}" y="{y + bar_h - 5:.1f}" font-size="12" '
            f'fill="var(--text-primary)">{_esc(tail)}</text>'
        )

    return (f'<svg viewBox="0 0 {width} {height}" width="{width}" '
            f'height="{height}" role="img">{"".join(out)}</svg>')


# --------------------------------------------------------------------------- #
# Page 1 -- the charts                                                         #
# --------------------------------------------------------------------------- #

def _charts_for(sub: pd.DataFrame, title: str, note: str, flow=None) -> str:
    """Market and limit side by side, for one flow (or for everything)."""
    charts = []
    for otype in V.OTYPES:
        part = sub[sub["otype_kind"] == otype]
        head = f"<h3>{_esc(otype.title())}</h3>"
        if part.empty:
            charts.append(f'<div>{head}<p class="sub">no {otype.lower()} child '
                          f'order reached the auction</p></div>')
            continue
        by_day = (part.groupby("date", sort=True)[["exec_qty", "unfilled_qty", "sent_qty"]]
                  .sum().reset_index())
        by_day["fill"] = np.where(
            by_day["sent_qty"] > 0, by_day["exec_qty"] / by_day["sent_qty"] * 100.0, np.nan
        )
        rows = [
            (_day_axis(r["date"], len(by_day)), r["exec_qty"], r["unfilled_qty"], r["fill"])
            for _, r in by_day.iterrows()
        ]
        charts.append(f'<div>{head}<div class="card">'
                      f'{_stacked_day_chart(rows)}</div></div>')

    return (f'<p class="flowhead">{_esc(title)}<span class="sub2">{_esc(note)}</span></p>'
            + _flow_note(flow)
            + f'<div class="grid2">{"".join(charts)}</div>')


def _execution_charts(data: PeriodData) -> str:
    """One section per flow when the run covers both, otherwise just the one."""
    fl = data.flows
    if fl is None or fl.empty:
        return '<p class="sub">no close child order in the period</p>'

    intro = (
        '<p class="take">One bar per day, in shares: the size of the orders that '
        'competed in the auction, split into the part that traded and the part '
        'that did not. The number above each bar is the fill ratio. Orders that '
        'traded nothing at all are not counted &mdash; they never competed.</p>'
        + _legend([("Executed", SERIES_EXECUTED),
                   ("Sent, not executed", SERIES_UNFILLED)])
    )

    flows = data.flows_present
    if len(flows) <= 1:
        only = flows[0] if flows else data.flow
        return intro + _charts_for(fl, str(only).title(), "market and limit",
                                   flow=only)

    # Both flows: a section each, so SILK and agency are never read as one book.
    out = [intro]
    for flow in flows:
        sub = fl[fl["flow"] == flow]
        traded = _usd(pd.to_numeric(sub["exec_notional_usd"], errors="coerce").sum())
        out.append(_charts_for(sub, str(flow).title(),
                               f"market and limit · {traded} traded in the close",
                               flow=flow))
    return "".join(out)


def _client_charts(data: PeriodData) -> str:
    """Top clients of each flow, as horizontal bars in notional."""
    flows = data.flows_present
    if not flows:
        return ""

    parts = [
        f'<p class="take">The {V.TOP_CLIENTS} biggest {V.CLIENT_COLUMN}s of each '
        f'flow, by notional traded in the closing auction. The bar is that '
        f'client&rsquo;s notional, split into what traded and what did not; the '
        f'figure at the end is the traded notional and the fill ratio.</p>'
        + _legend([("Executed", SERIES_EXECUTED),
                   ("Sent, not executed", SERIES_UNFILLED)])
    ]
    for flow in flows:
        top = data.clients(flow)
        parts.append(f"<h3>{_esc(str(flow).title())}</h3>")
        if top is None or top.empty:
            parts.append('<p class="sub">nothing in this flow</p>')
            continue
        # The label gutter is 300px; a basket name past ~34 characters would run
        # out of it and over the axis.  The table on page 2 carries it in full.
        rows = [
            (_clip(str(r[V.CLIENT_COLUMN]) or "(none)", 34),
             r.get("exec_notional_usd"),
             r.get("unfilled_notional_usd"),
             r.get("fill_rate_pct"))
            for _, r in top.iterrows()
        ]
        parts.append(f'<div class="card">{_hbar_chart(rows)}</div>')
    return "".join(parts)


# --------------------------------------------------------------------------- #
# Page 2 -- the data                                                           #
# --------------------------------------------------------------------------- #

#: Columns of an execution-quality table, market or limit.
_EQ_HEAD = [("Day", ""), ("Sent", "num"), ("Executed", "num"),
            ("Fill ratio", "num"), ("Executed notional", "num"),
            ("Total notional sent", "num")]


def _eq_pair(frame: pd.DataFrame) -> str:
    """Market and limit tables side by side, over whatever frame is passed."""
    totals = M.execution_quality_totals(frame)
    tables = []
    for otype in V.OTYPES:
        sub = frame[frame["otype_kind"] == otype].sort_values("date")
        title = f"<h3>{_esc(otype.title())}</h3>"
        if sub.empty:
            tables.append(f'<div>{title}<p class="sub">nothing to show</p></div>')
            continue
        body = [
            [_esc(_day(r["date"])), _qty(r["sent_qty"]), _qty(r["exec_qty"]),
             _pct(r["fill_rate_pct"]), _usd(r["exec_notional_usd"]),
             _usd(r["sent_notional_usd"])]
            for _, r in sub.iterrows()
        ]
        trow = totals[totals["otype_kind"] == otype] if not totals.empty else totals
        total = None
        if trow is not None and not trow.empty:
            t = trow.iloc[0]
            total = ["Period", _qty(t["sent_qty"]), _qty(t["exec_qty"]),
                     _pct(t["fill_rate_pct"]), _usd(t["exec_notional_usd"]),
                     _usd(t["sent_notional_usd"])]
        tables.append(f"<div>{title}{_table(_EQ_HEAD, body, total)}</div>")
    return f'<div class="grid2">{"".join(tables)}</div>'


def _execution_tables(data: PeriodData) -> str:
    """Market and limit, per flow -- and combined when the run covers both.

    The combined tables answer "how did the close go", the per-flow ones answer
    "for whom", and the second question is not recoverable from the first: SILK
    and agency fill at different rates, so their sum describes neither.
    """
    eq = data.execution_quality
    if eq is None or eq.empty:
        return '<p class="sub">nothing to show</p>'

    flows = data.flows_present
    fl = data.flows

    # One flow: the combined tables already are that flow's tables.
    if len(flows) <= 1 or fl is None or fl.empty:
        only = flows[0] if flows else None
        head = (f'<p class="flowhead">{_esc(str(only).title())}</p>' + _flow_note(only)
                if only else "")
        return head + _eq_pair(eq)

    parts = ['<p class="take">Each flow on its own, then the two together. '
             'SILK and agency fill at different rates, so the combined row '
             'describes neither of them on its own.</p>']
    for flow in flows:
        sub = fl[fl["flow"] == flow]
        traded = _usd(pd.to_numeric(sub["exec_notional_usd"], errors="coerce").sum())
        parts.append(f'<p class="flowhead">{_esc(str(flow).title())}'
                     f'<span class="sub2">{_esc(traded)} traded in the close</span></p>')
        parts.append(_flow_note(flow))
        parts.append(_eq_pair(sub))

    parts.append('<p class="flowhead">All flows'
                 '<span class="sub2">SILK and agency together</span></p>')
    parts.append(_eq_pair(eq))
    return "".join(parts)


def _client_tables(data: PeriodData) -> str:
    flows = data.flows_present
    if not flows:
        return '<p class="sub">nothing to show</p>'
    head = [
        (V.CLIENT_COLUMN.title(), ""), ("Days", "num"), ("Orders", "num"),
        ("Child orders", "num"), ("Symbols", "num"),
        ("Notional traded in close", "num"), ("Fill rate", "num"),
        ("% of market notional", "num"),
    ]
    parts = []
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
    """Write the two-page layout.  Returns the path written."""
    header = (
        "<h1>Closing auction &ndash; execution review</h1>"
        f'<p class="sub">{_esc(data.label)}'
        + (f' &nbsp;&middot;&nbsp; {len(data.dates)} trading days'
           if data.is_multi_day else "")
        + f' &nbsp;&middot;&nbsp; {_esc(_flow_label(data.flow))}'
        + " &nbsp;&middot;&nbsp; NSE closing auction"
        + (f' &nbsp;&middot;&nbsp; {_esc(data.fx_note)}' if data.fx_note else "")
        + "</p>"
        + _missing(data)
    )

    page1 = (
        '<section class="page pg-1">'
        + _kpis(data)
        + "<h2>Execution Quality</h2>"
        + _execution_charts(data)
        + f"<h2>Top {V.TOP_CLIENTS} clients</h2>"
        + _client_charts(data)
        + "</section>"
    )

    page2 = (
        '<section class="page pg-2">'
        "<h2>Execution Quality</h2>"
        + _execution_tables(data)
        + "<h2>Flows</h2>"
        + _flows(data)
        + f"<h2>Top {V.TOP_CLIENTS} clients</h2>"
        + _client_tables(data)
        + "</section>"
    )

    foot = (
        f'<p class="foot">Covers child orders sent to a CLOSE venue that traded '
        f'at least in part and were still on the market after '
        f'{_esc(_ist(V.OFF_MARKET_AFTER))}; a limit order also has to have been '
        f'priced at or through the price it achieved. Quantities are the '
        f'order&rsquo;s own &mdash; size sent, make executed &mdash; at its '
        f'average fill price. The auction is measured over '
        f'{_esc(_ist(V.CLOSE_WINDOW[0]))} to {_esc(_ist(V.CLOSE_WINDOW[1]))}: '
        f'close volume is the size printed in that window and the closing price '
        f'the first price in it. Trading-at-last, after 18:00, is excluded. '
        f'Generated {dt.datetime.now():%Y-%m-%d %H:%M}.</p>'
    )

    # The radios come first so `~` can reach both the tabs and the pages.
    body = (
        header
        + '<input class="pgstate" type="radio" name="pg" id="pg1" checked>'
        + '<input class="pgstate" type="radio" name="pg" id="pg2">'
        + _tabs() + page1 + page2 + foot
    )

    doc = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>Closing auction review {_esc(data.label)}</title>"
        f"<style>{_CSS}{_V2_CSS}{_PAGES_CSS}</style></head>"
        f'<body class="viz-root"><div class="wrap">{body}</div></body></html>'
    )
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(doc)
    return path

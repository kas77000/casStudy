"""The trader / client version of the retrospective.

Same numbers as `report.write_html`, a different reader.  The quant page is a
worksheet: every section, every column, every row, so a number can be traced.
This one answers four questions in the order a trader asks them --

    did we get into the close, what did it cost us, what kept us out,
    and which names does that leave to do something about

-- and nothing else.  No column dumps, no reason codes, no HKT-only clocks, no
`id_target`.  Every table is short enough to read on the screen it is opened on,
and every number that is a share of something says what of.

Two rules held throughout, because this page leaves the desk:

* nothing is stated that the daily pipeline did not compute.  Where a figure is
  unavailable (no market data, no reference price) the row says so instead of
  showing a zero;
* percentages are recomputed from quantities, never averaged from percentages.
"""

from __future__ import annotations

import datetime as dt
import os

import numpy as np
import pandas as pd

from . import classify as CL
from . import config as C
from .build import ReportData
from .report import (
    _CSS,
    _bar_chart,
    _column_chart,
    _esc,
    _grouped_bar_chart,
    _tile,
)

# --------------------------------------------------------------------------- #
# Vocabulary: the same rules, said to a trader                                 #
# --------------------------------------------------------------------------- #

#: Reason code -> what to say to someone who does not read this codebase.
TRADER_REASONS: dict[str, str] = {
    # never reached the auction
    "NOT_CAS_ELIGIBLE": "The name is not in the closing-auction universe",
    "NO_CLOSE_INSTRUCTION": "The order was not set up to trade the close",
    "FULLY_FILLED_BEFORE_CAS": "Already complete before the auction",
    "PARENT_CANCELLED_BEFORE_CAS": "Cancelled before the auction",
    "PARENT_DONE_BEFORE_CAS": "Finished before the auction",
    "ORDER_END_BEFORE_CAS": "The order was set to stop before the auction started",
    "ORDER_ARRIVED_AFTER_ENTRY_CLOSED": "Reached us after auction order entry had closed",
    "LIMIT_OUTSIDE_PRICE_BAND": "The limit sat outside the ±3% auction price band",
    "ALGO_NEVER_COMMITTED_TO_CLOSE": "The balance stayed live but was never committed to the auction",
    "BLOCKING_ALERT": "An alert stopped the order during the auction window",
    "NO_MARKET_DATA": "The stock did not print all day — likely halted",
    # reached the auction but did not trade
    "CLOSE_ORDER_REJECTED": "The exchange rejected our auction order",
    "CLOSE_ORDER_CANCELLED": "Our auction order was cancelled before the match",
    "SENT_AFTER_ENTRY_CLOSED": "We sent it after auction order entry had closed",
    "MARKET_ORDER_IN_LIMIT_ONLY_PHASE": "A market order went in during the limit-only phase",
    "PRICE_OUTSIDE_PRICE_BAND": "Our price sat outside the ±3% auction band",
    "NOT_MATCHED_IN_AUCTION": "We stood in the auction but the clearing price never reached us",
    "UNEXPLAINED": "Not yet explained — under review",
}

#: Where the fix sits.  The point of the column is that half of these are not
#: ours to fix, and saying so is more useful than a longer list of causes.
REASON_OWNER: dict[str, str] = {
    "NOT_CAS_ELIGIBLE": "Nothing to fix",
    "NO_CLOSE_INSTRUCTION": "Order set-up",
    "FULLY_FILLED_BEFORE_CAS": "Nothing to fix",
    "PARENT_DONE_BEFORE_CAS": "Nothing to fix",
    "PARENT_CANCELLED_BEFORE_CAS": "Client instruction",
    "ORDER_END_BEFORE_CAS": "Order set-up",
    "ORDER_ARRIVED_AFTER_ENTRY_CLOSED": "Order timing",
    "LIMIT_OUTSIDE_PRICE_BAND": "Client limit",
    "ALGO_NEVER_COMMITTED_TO_CLOSE": "Our side",
    "BLOCKING_ALERT": "Our side",
    "NO_MARKET_DATA": "The market",
    "CLOSE_ORDER_REJECTED": "Our side / exchange",
    "CLOSE_ORDER_CANCELLED": "Our side",
    "SENT_AFTER_ENTRY_CLOSED": "Our side",
    "MARKET_ORDER_IN_LIMIT_ONLY_PHASE": "Our side",
    "PRICE_OUTSIDE_PRICE_BAND": "Our side",
    "NOT_MATCHED_IN_AUCTION": "The market",
    "UNEXPLAINED": "Under review",
}


def reason_text(code: str) -> str:
    """Plain-English cause, falling back to the quant wording, then the code."""
    if not code:
        return "Not classified"
    return (
        TRADER_REASONS.get(code)
        or CL.NOT_SENT_REASONS.get(code)
        or CL.SENT_REASONS.get(code)
        or str(code).replace("_", " ").capitalize()
    )


#: How many rows a name table shows.  Past this it stops being a page a trader
#: reads and becomes the workbook they already have.
TOP_N = 10

#: Below this many names, "best five" and "worst five" would print the same rows
#: twice, so one honest table is shown instead.
SPLIT_MIN_NAMES = 12


# --------------------------------------------------------------------------- #
# Formatting                                                                   #
# --------------------------------------------------------------------------- #

DASH = "–"          # en dash, used for "no number here"
CRORE = 1e7
LAKH = 1e5


def _is_num(v) -> bool:
    return v is not None and not (isinstance(v, float) and (np.isnan(v) or np.isinf(v)))


def _qty(v) -> str:
    return f"{float(v):,.0f}" if _is_num(v) else DASH


def _pct(v, nd: int = 1) -> str:
    return f"{float(v):,.{nd}f}%" if _is_num(v) else DASH


def _bps(v, nd: int = 1) -> str:
    return f"{float(v):+,.{nd}f}" if _is_num(v) else DASH


def _inr(v) -> str:
    """INR in the units an India desk quotes: crore, then lakh, then rupees."""
    if not _is_num(v):
        return DASH
    x = float(v)
    sign = "-" if x < 0 else ""
    a = abs(x)
    if a >= CRORE:
        return f"{sign}₹{a / CRORE:,.2f} cr"
    if a >= LAKH:
        return f"{sign}₹{a / LAKH:,.2f} lakh"
    return f"{sign}₹{a:,.0f}"


def _ist(t: dt.time) -> str:
    """'15:30 IST (18:00 HKT)' -- the trader's clock first."""
    return f"{C.to_ist(t):%H:%M} IST ({t:%H:%M} HKT)"


def _flow_label(flow: str) -> str:
    """`--flow both` is a CLI argument, not something to print on a client page."""
    return {
        "both": "SILK and agency flow",
        "silk": "SILK flow",
        "agency": "Agency flow",
    }.get(str(flow).lower(), f"{flow} flow")


def _num(df: pd.DataFrame, col: str) -> pd.Series:
    """A numeric column, or a column of NaN when the pipeline never made it."""
    if df is None or df.empty or col not in df.columns:
        return pd.Series(np.nan, index=(df.index if df is not None else None), dtype=float)
    return pd.to_numeric(df[col], errors="coerce")


def _wmean(values: pd.Series, weights: pd.Series) -> float:
    """Weighted mean, ignoring rows where either side is missing."""
    v = pd.to_numeric(values, errors="coerce")
    w = pd.to_numeric(weights, errors="coerce")
    ok = v.notna() & w.notna() & (w > 0)
    if not ok.any():
        return float("nan")
    return float((v[ok] * w[ok]).sum() / w[ok].sum())


# --------------------------------------------------------------------------- #
# Page furniture                                                               #
# --------------------------------------------------------------------------- #

_TRADER_CSS = """
.lede{font-size:15px;line-height:1.55;color:var(--text-primary);max-width:78ch;
 margin:0 0 22px;}
.lede strong{font-weight:650;}
.note{background:var(--surface-1);border:1px solid var(--border);
 border-left:3px solid var(--warning);border-radius:8px;padding:11px 14px;
 margin:0 0 16px;font-size:13px;color:var(--text-secondary);}
.note.good{border-left-color:var(--good);}
.note b{color:var(--text-primary);font-weight:600;}
.two{display:flex;flex-wrap:wrap;gap:16px;}
.two > *{flex:1 1 420px;min-width:0;}
.take{font-size:13.5px;color:var(--text-secondary);margin:0 0 14px;max-width:80ch;}
.defs{display:grid;grid-template-columns:auto 1fr;gap:6px 16px;font-size:12.5px;
 color:var(--text-secondary);margin-top:10px;}
.defs dt{font-weight:600;color:var(--text-primary);}
.defs dd{margin:0;}
table.plain{border-collapse:collapse;width:100%;font-size:13px;
 font-variant-numeric:tabular-nums;}
table.plain th,table.plain td{padding:7px 12px;border-bottom:1px solid var(--grid);
 text-align:left;white-space:nowrap;}
table.plain th{font-size:11px;text-transform:uppercase;letter-spacing:.05em;
 color:var(--text-muted);font-weight:600;}
table.plain td.num{text-align:right;}
table.plain td.wrap{white-space:normal;min-width:280px;}
table.plain tr:last-child td{border-bottom:none;}
.pill{display:inline-block;padding:1px 8px;border-radius:20px;font-size:11px;
 border:1px solid var(--border);color:var(--text-secondary);}
"""


def _table(headers: list[tuple[str, str]], rows: list[list[str]]) -> str:
    """`headers` is (label, css class) -- 'num', 'wrap' or ''."""
    if not rows:
        return '<p class="sub">nothing to show</p>'
    head = "".join(f"<th>{_esc(h)}</th>" for h, _ in headers)
    body = []
    for r in rows:
        cells = []
        for (_h, cls), v in zip(headers, r):
            c = f' class="{cls}"' if cls else ""
            cells.append(f"<td{c}>{v}</td>")   # values are pre-escaped by callers
        body.append("<tr>" + "".join(cells) + "</tr>")
    return (
        f'<div class="scroll"><table class="plain"><thead><tr>{head}</tr></thead>'
        f'<tbody>{"".join(body)}</tbody></table></div>'
    )


def _card(inner: str) -> str:
    return f'<div class="card">{inner}</div>'


# --------------------------------------------------------------------------- #
# The page                                                                     #
# --------------------------------------------------------------------------- #

def write_trader_html(data: ReportData, path: str) -> str:
    """Write the trader / client page.  Returns the path written."""
    parts: list[str] = []
    period = data.period_label
    weekly = data.is_weekly

    parts.append(
        f"<h1>Closing auction &ndash; how our India orders traded</h1>"
    )
    parts.append(
        f'<p class="sub">{_esc(period)}'
        + (f' &nbsp;&middot;&nbsp; {len(data.dates)} trading days' if weekly else "")
        + f' &nbsp;&middot;&nbsp; {_esc(_flow_label(data.flow))}'
        f' &nbsp;&middot;&nbsp; NSE closing auction, {_esc(_ist(C.MATCH_START))}</p>'
    )

    if data.orders.empty:
        parts.append(_card("<p>No order in the closing-auction universe traded in "
                           "this period.</p>"))
        return _write(parts, data, path)

    o = data.orders
    row = _headline(data)

    parts.append(_lede(data, row, weekly))
    parts.append(_provisional_note(data))
    parts.append(_quality_note(data))

    # 1 ------------------------------------------------------------------- #
    parts.append("<h2>The period in numbers</h2>")
    parts.append(_tiles(o, row))

    # 2 ------------------------------------------------------------------- #
    if weekly and data.by_day is not None and not data.by_day.empty:
        parts.append("<h2>Day by day</h2>")
        parts.append(_by_day_block(data.by_day))

    # 3 ------------------------------------------------------------------- #
    parts.append("<h2>Did we get into the close?</h2>")
    parts.append(_participation_block(o))

    # 4 ------------------------------------------------------------------- #
    parts.append("<h2>What kept us out</h2>")
    parts.append(_reasons_block(data))

    # 5 ------------------------------------------------------------------- #
    parts.append("<h2>Names that mattered</h2>")
    parts.append(_names_block(o))

    # 6 ------------------------------------------------------------------- #
    parts.append("<h2>What the auction cost, or saved</h2>")
    parts.append(_price_block(o))

    # 7 ------------------------------------------------------------------- #
    parts.append("<h2>Friction: what got refused or pulled</h2>")
    parts.append(_friction_block(data))

    # 8 ------------------------------------------------------------------- #
    if data.benchmark is not None and not data.benchmark.empty:
        parts.append("<h2>How the market traded the close</h2>")
        parts.append(_market_block(data))

    parts.append(_definitions())
    return _write(parts, data, path)


def _write(parts: list[str], data: ReportData, path: str) -> str:
    doc = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>Closing auction {_esc(data.period_label)} "
        f"- {_esc(_flow_label(data.flow))}</title>"
        f"<style>{_CSS}{_TRADER_CSS}</style></head>"
        f'<body class="viz-root"><div class="wrap">{"".join(parts)}</div></body></html>'
    )
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(doc)
    return path


# --------------------------------------------------------------------------- #
# Blocks                                                                       #
# --------------------------------------------------------------------------- #

def _headline(data: ReportData) -> pd.Series:
    """The TOTAL summary row, or the only one when a single flow ran."""
    s = data.summary
    if s is None or s.empty:
        return pd.Series(dtype=float)
    tot = s[s["flow"] == "TOTAL"]
    return (tot if not tot.empty else s).iloc[0]


def _lede(data: ReportData, row: pd.Series, weekly: bool) -> str:
    """Three sentences: what we did, how much of it landed, what is left."""
    o = data.orders
    n = int(row.get("parent_orders", len(o)))
    syms = int(row.get("syms", o["sym"].nunique()))
    in_close = int(row.get("orders_filled_in_close", 0))
    missed = n - in_close
    close_share = row.get("close_pct_of_executed", np.nan)
    part = row.get("participation_rate_pct", np.nan)

    # The balance carried by the orders that missed -- not the book's whole
    # residual, which includes orders that did trade in the auction and still
    # had something left.
    miss = data.non_participation
    miss_qty = float(_num(miss, "residual").sum(skipna=True)) if miss is not None else np.nan
    miss_val = (
        float(_num(miss, "residual_notional_at_close").sum(skipna=True))
        if miss is not None and not miss.empty else np.nan
    )

    span = f"over {len(data.dates)} trading days" if weekly else "on the day"
    lead = (
        f"We worked <strong>{n:,}</strong> order{'s' if n != 1 else ''} in "
        f"<strong>{syms:,}</strong> name{'s' if syms != 1 else ''} {span}. "
        f"<strong>{_pct(close_share)}</strong> of everything we executed went "
        f"through the closing auction, and "
        f"<strong>{in_close:,} of {n:,}</strong> orders ({_pct(part)}) traded in it."
    )
    if missed > 0:
        tail = (
            f" {missed:,} order{'s' if missed != 1 else ''} did not, carrying "
            f"{_qty(miss_qty)} shares"
        )
        tail += (f" — {_inr(miss_val)} at the closing price."
                 if _is_num(miss_val) and miss_val else ".")
    else:
        tail = " Every order traded in the auction."
    return f'<p class="lede">{lead}{tail}</p>'


def _provisional_note(data: ReportData) -> str:
    """Flag any day taken off the live tapes before the end-of-day write-down.

    A page that goes out has to say when a figure can still move.  This is the
    normal case for a Friday-evening review, not an error -- but the reader
    decides what to do about it, so they are told.
    """
    bd = data.by_day
    live: list[str] = []
    if bd is not None and not bd.empty and "source" in bd.columns:
        live = [str(r["date"]) for _, r in bd.iterrows() if str(r["source"]) == "RT"]
    elif str(data.mode).lower() == "rt" and data.date:
        live = [data.date.isoformat()]
    if not live:
        return ""
    days = ", ".join(live)
    return (
        f'<div class="note"><b>Provisional for {_esc(days)}.</b> That day was '
        f'read from the live trading records, before the end-of-day books were '
        f'finalised. The figures are what we hold now and can still move '
        f'slightly; every earlier day is final.</div>'
    )


def _quality_note(data: ReportData) -> str:
    """Say it plainly when a figure on this page rests on a failed check."""
    rec = data.reconciliation
    if rec is None or rec.empty or "status" not in rec.columns:
        return ""
    bad = rec[rec["status"] != "OK"]
    if bad.empty:
        return ('<div class="note good">All internal consistency checks passed: '
                'executed and residual quantities reconcile against the order '
                'book and the fill tape.</div>')
    checks = ", ".join(sorted({str(c) for c in bad["check"].head(4)}))
    return (
        f'<div class="note"><b>{len(bad)} internal check'
        f'{"s" if len(bad) != 1 else ""} did not pass</b> ({_esc(checks)}'
        f'{", …" if len(bad) > 4 else ""}). The figures below still stand on '
        f'the order and fill records, but the detail sits in the full '
        f'retrospective — worth a look before this goes further.</div>'
    )


def _tiles(o: pd.DataFrame, row: pd.Series) -> str:
    close_qty = row.get("close_qty", np.nan)
    close_px = _num(o, "close_px")
    close_notional = float((_num(o, "close_qty") * close_px).sum(skipna=True))
    capture = _wmean(_num(o, "close_capture_bps"), _num(o, "close_qty"))
    residual_val = row.get("residual_notional_at_close", np.nan)

    tiles = [
        _tile("Orders worked", f"{int(row.get('parent_orders', len(o))):,}",
              f"{int(row.get('syms', o['sym'].nunique())):,} names"),
        _tile("Executed", _qty(row.get("executed_qty")),
              f"{_pct(row.get('fill_pct'))} of what was ordered"),
        _tile("Traded in the auction", _qty(close_qty),
              f"{_pct(row.get('close_pct_of_executed'))} of what we executed"
              + (f" · {_inr(close_notional)}" if close_notional else "")),
        _tile("Orders in the close", _pct(row.get("participation_rate_pct")),
              f"{int(row.get('orders_filled_in_close', 0)):,} of "
              f"{int(row.get('parent_orders', len(o))):,}"),
        _tile("Auction price capture", f"{_bps(capture)} bps",
              "vs the closing print, weighted by size"),
        _tile("Left behind", _qty(row.get("residual_qty")),
              f"{_inr(residual_val)} at the close" if _is_num(residual_val)
              else "shares not executed"),
    ]
    return f'<div class="tiles">{"".join(tiles)}</div>'


def _by_day_block(bd: pd.DataFrame) -> str:
    labels = [f"{r['weekday']} {r['date']}" for _, r in bd.iterrows()]
    out = []

    part = pd.to_numeric(bd["participation_rate_pct"], errors="coerce")
    out.append(
        '<p class="take">Share of the day’s orders that traded in the '
        'auction. The dashed line is the period average.</p>'
    )
    out.append(_card(_column_chart(
        list(zip(labels, part)), unit="%", decimals=1, color="var(--series-1)",
        baseline=None if part.isna().all() else float(part.mean()),
        baseline_label=f"average {part.mean():,.1f}%" if part.notna().any() else "",
    )))

    out.append("<h3>Shares we traded in the auction</h3>")
    out.append(_card(_column_chart(
        list(zip(labels, pd.to_numeric(bd["close_qty"], errors="coerce"))),
        decimals=0, color="var(--seq-450)",
    )))

    headers = [
        ("Day", ""), ("Orders", "num"), ("Executed", "num"),
        ("In the auction", "num"), ("% of executed", "num"),
        ("Orders in the close", "num"), ("Price capture, bps", "num"),
        ("Refused in the auction", "num"),
    ]
    # Weighted, so the column agrees with the headline tile rather than quietly
    # answering a different question.
    cap_col = ("close_capture_bps_wtd" if "close_capture_bps_wtd" in bd.columns
               else "mean_close_capture_bps")
    rows = []
    for _, r in bd.iterrows():
        rows.append([
            _esc(f"{r['weekday']} {r['date']}"),
            _qty(r["parent_orders"]),
            _qty(r["executed_qty"]),
            _qty(r["close_qty"]),
            _pct(r["close_pct_of_executed"]),
            f"{int(r['orders_filled_in_close']):,} / {int(r['parent_orders']):,}",
            _bps(r[cap_col]),
            _qty(r["rejections_close"]),
        ])
    out.append(_table(headers, rows))
    return "".join(out)


def _participation_block(o: pd.DataFrame) -> str:
    counts = o["participation"].value_counts()
    n = len(o)
    labels = {
        "FILLED_IN_CLOSE": "Traded in the auction",
        "SENT_NOT_FILLED": "Reached the auction, did not trade",
        "NOT_SENT": "Never reached the auction",
    }
    colours = {
        "FILLED_IN_CLOSE": "var(--series-1)",
        "SENT_NOT_FILLED": "var(--series-2)",
        "NOT_SENT": "var(--series-3)",
    }
    items, legend = [], []
    for key, label in labels.items():
        c = int(counts.get(key, 0))
        if not c and key == "NOT_SENT":
            # The standard run only covers orders that reached the auction, so an
            # empty bucket here means "excluded", not "none" -- do not draw a zero.
            continue
        items.append((label, float(c)))
        legend.append(
            f'<span><i style="background:{colours[key]}"></i>{_esc(label)} '
            f'{c:,} ({c / n * 100:,.0f}%)</span>'
        )
    chart = _bar_chart(items, unit=" orders", label_w=280)
    take = (
        '<p class="take">Every order that put at least one child order into the '
        'auction is counted here. An order can reach the auction and still not '
        'trade — the clearing price simply never came to it.</p>'
    )
    return take + _card(f'<div class="legend">{"".join(legend)}</div>{chart}')


def _reasons_block(data: ReportData) -> str:
    miss = data.non_participation
    if miss is None or miss.empty or "reason_code" not in miss.columns:
        return _card("<p>Every order traded in the auction — nothing to "
                     "explain.</p>")

    g = miss.groupby("reason_code", dropna=False)
    agg = pd.DataFrame({
        "orders": g.size(),
        "shares": g["residual"].sum() if "residual" in miss.columns else g.size() * np.nan,
    })
    if "residual_notional_at_close" in miss.columns:
        agg["value"] = g["residual_notional_at_close"].sum()
    else:
        agg["value"] = np.nan
    agg = agg.sort_values("orders", ascending=False)

    chart = _bar_chart(
        [(reason_text(str(code))[:58], float(r["orders"])) for code, r in agg.iterrows()],
        unit=" orders", label_w=400, color="var(--series-2)",
    )
    headers = [
        ("What happened", "wrap"), ("Where the fix sits", ""),
        ("Orders", "num"), ("Shares left", "num"), ("Value at the close", "num"),
    ]
    rows = [
        [
            _esc(reason_text(str(code))),
            f'<span class="pill">{_esc(REASON_OWNER.get(str(code), "Under review"))}</span>',
            _qty(r["orders"]), _qty(r["shares"]), _inr(r["value"]),
        ]
        for code, r in agg.iterrows()
    ]
    take = (
        '<p class="take">Ranked by how many orders each cause held up. '
        '“Where the fix sits” says whose change would remove it — '
        'several of these are order set-up or client instructions rather than '
        'anything the desk can act on.</p>'
    )
    return take + _card(chart) + _table(headers, rows)


def _names_block(o: pd.DataFrame) -> str:
    close_px = _num(o, "close_px")
    df = pd.DataFrame({
        "sym": o["sym"].astype(str),
        "orders": 1,
        "exec_qty": _num(o, "exec_qty"),
        "close_qty": _num(o, "close_qty"),
        "residual": _num(o, "residual"),
        "close_notional": _num(o, "close_qty") * close_px,
        "residual_value": _num(o, "residual_notional_at_close"),
        "missed_pnl": _num(o, "missed_close_pnl"),
    })
    g = df.groupby("sym", sort=False).sum(min_count=1).reset_index()
    g["close_pct"] = np.where(
        g["exec_qty"] > 0, g["close_qty"] / g["exec_qty"] * 100.0, np.nan
    )
    g["name"] = g["sym"].str.replace(".IN", "", regex=False)

    # Only names that actually traded in the auction: a name with nothing in it
    # does not belong in a table headed "most traded in the auction".
    done = g[g["close_qty"] > 0].sort_values("close_qty", ascending=False).head(TOP_N)
    left = g[g["residual"] > 0].sort_values(
        "residual_value", ascending=False, na_position="last"
    ).head(TOP_N)

    done_tbl = _table(
        [("Name", ""), ("Orders", "num"), ("Executed", "num"),
         ("In the auction", "num"), ("% of executed", "num"), ("Value", "num")],
        [[_esc(r["name"]), _qty(r["orders"]), _qty(r["exec_qty"]),
          _qty(r["close_qty"]), _pct(r["close_pct"]), _inr(r["close_notional"])]
         for _, r in done.iterrows()],
    ) if not done.empty else '<p class="sub">nothing traded in the auction</p>'
    left_tbl = _table(
        [("Name", ""), ("Orders", "num"), ("Shares left", "num"),
         ("Value at the close", "num"), ("Cost of missing it", "num")],
        [[_esc(r["name"]), _qty(r["orders"]), _qty(r["residual"]),
          _inr(r["residual_value"]), _inr(-r["missed_pnl"]) if _is_num(r["missed_pnl"]) else DASH]
         for _, r in left.iterrows()],
    ) if not left.empty else '<p class="sub">nothing left unexecuted</p>'

    return (
        '<p class="take">The names we put through the auction, and the names we '
        'did not finish. “Cost of missing it” values the balance at the closing '
        'price against the last continuous price: positive means finishing in '
        'the auction would have been the better fill, negative means we were '
        'better off without it.</p>'
        f'<div class="two"><div><h3>Most traded in the auction</h3>{done_tbl}</div>'
        f'<div><h3>Largest balances left</h3>{left_tbl}</div></div>'
    )


def _price_block(o: pd.DataFrame) -> str:
    capture = _num(o, "close_capture_bps")
    close_qty = _num(o, "close_qty")
    if capture.isna().all():
        return _card(
            "<p>No reference price was available for this period, so auction "
            "price capture could not be measured.</p>"
        )

    by_flow = []
    for flow, sub in o.groupby("flow", sort=True):
        w = _wmean(_num(sub, "close_capture_bps"), _num(sub, "close_qty"))
        if _is_num(w):
            by_flow.append((str(flow), float(w)))
    overall = _wmean(capture, close_qty)

    # Per name, not per order: a name traded on three days is one line, and its
    # capture is weighted by what actually went through the auction.
    traded = o[(close_qty > 0) & capture.notna()].copy()
    traded["name"] = traded["sym"].astype(str).str.replace(".IN", "", regex=False)
    traded["_w"] = _num(traded, "close_capture_bps") * _num(traded, "close_qty")
    per_name = (
        traded.groupby("name", sort=False)
        .agg(close_qty=("close_qty", "sum"), _w=("_w", "sum"),
             sides=("side", lambda s: "/".join(sorted({str(x) for x in s}))))
        .reset_index()
    )
    per_name["_cap"] = per_name["_w"] / per_name["close_qty"].replace(0, np.nan)
    per_name = per_name.sort_values("_cap", ascending=False)

    def name_rows(df: pd.DataFrame) -> str:
        return _table(
            [("Name", ""), ("Side", ""), ("In the auction", "num"),
             ("Capture, bps", "num")],
            [[_esc(r["name"]), _esc(r["sides"]), _qty(r["close_qty"]), _bps(r["_cap"])]
             for _, r in df.iterrows()],
        )

    # Best five and worst five only make sense once there are enough names for
    # the two tables not to be the same rows twice.
    if len(per_name) <= SPLIT_MIN_NAMES:
        names_html = f"<h3>By name</h3>{name_rows(per_name)}"
    else:
        names_html = (
            f'<div class="two">'
            f'<div><h3>Best five</h3>{name_rows(per_name.head(5))}</div>'
            f'<div><h3>Worst five</h3>'
            f'{name_rows(per_name.tail(5).iloc[::-1])}</div></div>'
        )

    chart = _bar_chart(
        by_flow, unit=" bps", label_w=260, color="var(--series-3)",
    ) if by_flow else '<p class="sub">nothing to plot</p>'

    missed = _num(o, "missed_close_pnl").sum(skipna=True)
    adverse = _wmean(_num(o, "adverse_move_bps"), _num(o, "exec_qty"))

    # `missed_close_pnl` is positive when missing the close was the cheaper
    # outcome, so the sign decides which sentence is true -- printing a negative
    # "cost" and leaving the reader to invert it would not do.
    cost = -float(missed) if _is_num(missed) else np.nan
    if _is_num(cost):
        cost_note = ("cost us, valuing the balance at the close" if cost > 0
                     else "saved, the close was the worse price for us")
    else:
        cost_note = "no closing price available"

    tiles = [
        _tile("Auction price capture", f"{_bps(overall)} bps",
              "positive = we did better than the closing print"),
        _tile("Move into the close", f"{_bps(adverse)} bps",
              "against our side" if _is_num(adverse) and adverse > 0
              else "in our favour"),
        _tile("Not finishing in the auction",
              _inr(abs(cost)) if _is_num(cost) else DASH, cost_note),
    ]
    return (
        '<p class="take">Price capture compares what we paid or received in the '
        'auction against the auction’s own clearing price. It is weighted by '
        'the size traded, so a large order counts for more than a small one.</p>'
        f'<div class="tiles">{"".join(tiles)}</div>'
        f'<h3>By flow</h3>{_card(chart)}'
        f'{names_html}'
    )


def _friction_block(data: ReportData) -> str:
    rej, cxl = data.rejections, data.cancellations

    def n(df: pd.DataFrame, col: str, val: str) -> int:
        if df is None or df.empty or col not in df.columns:
            return 0
        return int((df[col] == val).sum())

    late_rej = n(rej, "rejection_type", C.REJECTION_AFTER_CLOSE)
    late_cxl = n(cxl, "cancel_type", C.CANCEL_AFTER_CLOSE)
    tiles = [
        _tile("Refused in the auction", f"{n(rej, 'phase', 'CLOSE'):,}",
              "child orders the exchange would not accept"),
        _tile("Refused during continuous", f"{n(rej, 'phase', 'CONTINUOUS'):,}",
              "before the auction began"),
        _tile("Pulled in the auction", f"{n(cxl, 'phase', 'CLOSE'):,}",
              "child orders we cancelled once the auction was running"),
        _tile("After the freeze window opened", f"{late_rej + late_cxl:,}",
              f"from {_ist(C.AFTER_CLOSE_FROM)} — too late to correct and resend"),
    ]

    body = ""
    if cxl is not None and not cxl.empty and "reason" in cxl.columns:
        top = (
            cxl.groupby(["phase", "reason"]).size()
            .sort_values(ascending=False).head(8)
        )
        rows = [[_esc(str(p).title()), _esc(str(r) or "not stated"), _qty(v)]
                for (p, r), v in top.items()]
        body = "<h3>Why child orders were pulled</h3>" + _table(
            [("Phase", ""), ("Reason given", "wrap"), ("Child orders", "num")], rows
        )

    take = (
        '<p class="take">These are our own child orders, not the client’s. '
        f'The last tile is the one that matters: from {_esc(_ist(C.AFTER_CLOSE_FROM))} '
        'the auction can freeze at any moment, so anything refused or pulled after '
        'that had no runway to be corrected and sent again.</p>'
    )
    return take + f'<div class="tiles">{"".join(tiles)}</div>' + body


def _market_block(data: ReportData) -> str:
    b = data.benchmark
    labels = {
        "Market: closing bin share (17:30-18:00 HKT / 15:00-15:30 IST)":
            "How much of the day the market traded in the last 30 minutes",
        "Market: CTS window share (17:45-18:00 HKT / 15:15-15:30 IST)":
            "… in the final continuous session (15:15–15:30 IST)",
        "Market: close auction share (18:00-18:05 HKT / 15:30-15:35 IST)":
            "… in the closing auction itself",
        "Market: combined end-of-day activity":
            "… in both together",
        "Us: share of the auction print we accounted for":
            "Our share of everything that printed in the auction",
        "Us: share of our day's volume done in the auction":
            "Our own volume that went through the auction",
    }
    rows = [
        (labels.get(str(r["metric"]), str(r["metric"])), r["value_pct"], r["benchmark_pct"])
        for _, r in b.iterrows()
    ]
    chart = _grouped_bar_chart(rows, ("this period", "reference"), label_w=430)
    take = (
        '<p class="take">Context for everything above: how much of the trading day '
        'the whole market now does at the close, against the historical closing bin '
        'and the first day of the new mechanism. Our own two lines are shares of '
        'the market, not of our order book.</p>'
    )
    return take + _card(chart)


def _definitions() -> str:
    return (
        '<h2>What these words mean</h2>'
        '<div class="card"><dl class="defs">'
        '<dt>Closing auction</dt><dd>NSE’s call auction that sets the official '
        f'close. Order entry {_esc(_ist(C.ENTRY_LM_START))} to '
        f'{_esc(_ist(C.ENTRY_LO_END))}, matching from {_esc(_ist(C.MATCH_START))}; '
        f'from {_esc(_ist(C.RANDOM_CLOSE_START))} it can freeze at any moment.</dd>'
        '<dt>Reached the auction</dt><dd>We sent at least one child order to the '
        'closing venue after continuous trading ended.</dd>'
        '<dt>Price capture</dt><dd>Our auction fill price against the auction’s '
        'clearing price, in basis points, signed so positive is always better for '
        'the side we were on.</dd>'
        '<dt>Balance left</dt><dd>Order quantity that never executed, anywhere — '
        'valued here at the closing price.</dd>'
        f'<dt>Price band</dt><dd>Every auction order must sit within '
        f'±{C.CAS_PRICE_BAND:.0%} of the exchange’s reference price, or it '
        f'is refused.</dd>'
        '</dl></div>'
        f'<p class="foot">Reference price = the exchange’s VWAP over '
        f'{_esc(_ist(C.REF_VWAP_START))}–{_esc(_ist(C.REF_VWAP_END))}. '
        f'Covers orders in the CAS-eligible universe that executed at least in part '
        f'and sent an order to the closing venue. Values in Indian rupees at the '
        f'closing price; cr = crore, 10 million. '
        f'Generated {dt.datetime.now():%Y-%m-%d %H:%M} — the full '
        f'retrospective, with every order and every check, is in the companion '
        f'quant report.</p>'
    )

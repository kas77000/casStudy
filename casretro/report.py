"""Writers: console summary, CSV, Excel workbook, self-contained HTML page."""

from __future__ import annotations

import datetime as dt
import html
import os
import sys
from typing import Iterable

import numpy as np
import pandas as pd

from . import classify as CL
from . import config as C
from .build import ReportData
from .kdbio import td_to_str

# --------------------------------------------------------------------------- #
# Sheet layout                                                                 #
# --------------------------------------------------------------------------- #

#: (attribute on ReportData, sheet name, human title)
SECTIONS: list[tuple[str, str, str]] = [
    ("summary", "summary", "Headline numbers"),
    ("benchmark", "benchmark", "Volume share vs desk benchmarks"),
    ("orders", "orders", "Parent orders"),
    ("non_participation", "non_participation", "Orders that did not trade in the close"),
    ("rejections", "rejections", "Rejected child orders"),
    ("cancellations", "cancellations", "Cancelled child orders"),
    ("mix_otype_basket", "mix_otype_basket",
     "Size / make / fill rate by order type and basket"),
    ("mix_flow_venue_otype", "mix_flow_venue_otype",
     "Size / make / fill rate by flow, venue and order type"),
    ("timing", "timing", "CAS timing and compliance"),
    ("sym_stats", "sym_stats", "Per-symbol volume and participation"),
    ("ref_prices", "ref_price_band", "Reference price and +/-3% band"),
    ("alerts", "alerts", "Alerts on parent orders"),
    ("workorders", "workorders", "Child orders"),
    ("reconciliation", "reconciliation", "Data quality checks"),
    ("sessions", "session_calendar", "CAS session calendar (HKT / IST)"),
]

#: Column order preferred at the front of the `orders` sheet.
ORDER_COLS_FRONT = [
    "date", "flow", "basket", "id_target", "sym", "side", "size", "exec_qty",
    "residual", "fill_pct", "qty_source", "exec_qty_fills",
    "participation", "reason_code", "reason_label",
    "reason_detail", "close_qty", "close_pct_of_order", "close_pct_of_executed",
    "n_close_wo", "close_wo_rejected", "close_wo_cancelled",
    "close_wo_reject_reasons", "close_wo_cancel_reasons",
    "doclose", "docash", "otype", "tif", "limit_price",
    "ref_price", "ref_source", "band_lo", "band_hi",
    "parent_limit_in_band", "parent_limit_blocks_close", "parent_limit_bps_from_ref",
    "close_px", "cts_end_px", "close_vwap", "exec_vwap", "cont_vwap",
    "close_capture_bps", "perf_vs_close_bps", "adverse_move_bps",
    "residual_notional_at_close", "missed_close_pnl",
    "t_start", "t_end", "first_state_time", "last_state_time",
    "final_state", "state_at_cas", "terminal_state", "terminal_time",
    "open_at_cas", "final_open", "max_make_close", "max_commit_close",
    "algo", "trader", "portfolio", "wave",
]


# --------------------------------------------------------------------------- #
# Frame preparation                                                            #
# --------------------------------------------------------------------------- #

#: Percentages are reported to 2 decimals; everything else keeps 6, which is
#: enough for a price and stops float noise (100.38000000000001) reaching a CSV.
PCT_DECIMALS = 2
FLOAT_DECIMALS = 6


def _is_pct(col: str) -> bool:
    c = str(col).lower()
    return "pct" in c or c.endswith("_pp")


def _stringify(df: pd.DataFrame) -> pd.DataFrame:
    """Timedeltas -> 'HH:MM:SS.mmm'; booleans -> Y/N; floats rounded."""
    if df is None or df.empty:
        return pd.DataFrame() if df is None else df
    out = df.copy()
    for col in out.columns:
        s = out[col]
        if pd.api.types.is_timedelta64_dtype(s):
            out[col] = s.map(td_to_str)
        elif s.dtype == bool:
            out[col] = np.where(s, "Y", "N")
        elif pd.api.types.is_float_dtype(s):
            out[col] = s.round(PCT_DECIMALS if _is_pct(col) else FLOAT_DECIMALS)
    return out


def _order_columns(df: pd.DataFrame, front: Iterable[str]) -> pd.DataFrame:
    if df is None or df.empty:
        return df
    lead = [c for c in front if c in df.columns]
    rest = [c for c in df.columns if c not in lead]
    return df[lead + rest]


def prepared(data: ReportData) -> dict[str, pd.DataFrame]:
    """Every section, formatted and column-ordered, keyed by sheet name."""
    out: dict[str, pd.DataFrame] = {}
    for attr, sheet, _title in SECTIONS:
        df = getattr(data, attr, None)
        if df is None:
            continue
        if attr in ("orders", "non_participation"):
            df = _order_columns(df, ORDER_COLS_FRONT)
        out[sheet] = _stringify(df)
    return out


# --------------------------------------------------------------------------- #
# Console                                                                      #
# --------------------------------------------------------------------------- #

def _fmt(v, nd: int = 2, pct: bool = False) -> str:
    if v is None or (isinstance(v, float) and (np.isnan(v) or np.isinf(v))):
        return "-"
    if pct:
        return f"{v:,.{nd}f}%"
    if isinstance(v, (int, np.integer)):
        return f"{v:,}"
    return f"{v:,.{nd}f}"


def _print_mix(df: pd.DataFrame, keys: list[str], title: str, max_rows: int = 20) -> None:
    """Console rendering of a mix table -- keys, then size / make / fill rate."""
    if df is None or df.empty:
        return
    print(f"\n  {title}")
    label_w = 46
    print(f"    {'':<{label_w}} {'child':>7} {'parent':>7} {'syms':>6} "
          f"{'size':>13} {'make':>13} {'fill%':>7}")
    for _, r in df.head(max_rows).iterrows():
        label = " / ".join(str(r[k]) for k in keys if str(r[k]))
        print(
            f"    {label[:label_w]:<{label_w}} {_fmt(r['n_child_orders'],0):>7} "
            f"{_fmt(r['n_parents'],0):>7} {_fmt(r['n_syms'],0):>6} "
            f"{_fmt(r['size'],0):>13} {_fmt(r['make'],0):>13} "
            f"{_fmt(r['fill_rate_pct'],PCT_DECIMALS):>7}"
        )
    if len(df) > max_rows:
        print(f"    ... {len(df) - max_rows} more rows in the CSV / workbook")


def print_console(data: ReportData) -> None:
    d = data.date.isoformat() if data.date else "(real time)"
    print()
    print("=" * 78)
    print(f"  CAS India execution retrospective -- {d}   flow={data.flow}   mode={data.mode}")
    print(f"  all times HKT (IST = HKT - 02:30)")
    print("=" * 78)

    if data.orders.empty:
        print("\n  no parent order matched the filters\n")
        return

    for _, r in data.summary.iterrows():
        print(f"\n  [{r['flow']}]")
        print(f"    parent orders          : {_fmt(r['parent_orders'], 0)}  over {_fmt(r['syms'], 0)} syms")
        print(f"    order / executed / left: {_fmt(r['order_qty'],0)} / {_fmt(r['executed_qty'],0)} / {_fmt(r['residual_qty'],0)}"
              f"   ({_fmt(r['fill_pct'],1,pct=True)} filled)")
        print(f"    traded in the auction  : {_fmt(r['close_qty'],0)} shares "
              f"= {_fmt(r['close_pct_of_executed'],1,pct=True)} of what we executed")
        print(f"    participation          : {_fmt(r['orders_filled_in_close'],0)} filled / "
              f"{_fmt(r['orders_sent_not_filled'],0)} sent-not-filled / "
              f"{_fmt(r['orders_not_sent'],0)} never sent"
              f"   ({_fmt(r['participation_rate_pct'],1,pct=True)} of orders)")
        print(f"    rejections             : {_fmt(r['rejections_continuous'],0)} continuous / "
              f"{_fmt(r['rejections_close'],0)} close")
        print(f"    cancellations          : {_fmt(r['cancellations_continuous'],0)} continuous / "
              f"{_fmt(r['cancellations_close'],0)} close")
        print(f"    close capture          : {_fmt(r['mean_close_capture_bps'],1)} bps mean "
              f"(positive = better than the auction print)")

    # Why we missed the close
    miss = data.non_participation
    if not miss.empty and "reason_code" in miss.columns:
        print("\n  why orders did not trade in the auction")
        counts = (
            miss.groupby(["participation", "reason_code"])
            .agg(orders=("id_target", "count"), qty=("residual", "sum"))
            .sort_values("orders", ascending=False)
        )
        for (part, code), row in counts.iterrows():
            label = CL.NOT_SENT_REASONS.get(code) or CL.SENT_REASONS.get(code, "")
            print(f"    {part:<16} {code:<34} {int(row['orders']):>5} orders  "
                  f"{_fmt(row['qty'],0):>14} shares")
            if label:
                print(f"      {label}")

    _print_mix(data.mix_otype_basket, ["otype_kind", "basket"],
               "size / make / fill rate by order type and basket")
    _print_mix(data.mix_flow_venue_otype, ["flow", "venue", "otype_kind"],
               "size / make / fill rate by flow, venue and order type")

    if not data.benchmark.empty:
        print("\n  volume share vs the desk benchmarks")
        for _, r in data.benchmark.iterrows():
            bench = f"  vs {r['benchmark_label']}" if r.get("benchmark_label") else ""
            delta = ""
            if pd.notna(r.get("delta_pp")):
                delta = f"   ({r['delta_pp']:+.2f} pp)"
            print(f"    {r['metric']:<62} {_fmt(r['value_pct'],2,pct=True):>9}{bench}{delta}")

    review = data.reconciliation[data.reconciliation["status"] != "OK"] if not data.reconciliation.empty else pd.DataFrame()
    if not review.empty:
        print("\n  data quality -- needs a look")
        for _, r in review.iterrows():
            print(f"    [{r['status']}] {r['check']}: {r['detail']}")

    for w in data.warnings:
        print(f"\n  [warn] {w}")
    print()


# --------------------------------------------------------------------------- #
# CSV                                                                          #
# --------------------------------------------------------------------------- #

def write_csvs(data: ReportData, outdir: str) -> list[str]:
    os.makedirs(outdir, exist_ok=True)
    written = []
    for sheet, df in prepared(data).items():
        if df is None or df.empty:
            continue
        path = os.path.join(outdir, f"{sheet}.csv")
        # No float_format: the frames are already rounded by `_stringify`, and a
        # fixed one would pad every percentage back out to 6 decimals.
        df.to_csv(path, index=False)
        written.append(path)
    return written


# --------------------------------------------------------------------------- #
# Excel                                                                        #
# --------------------------------------------------------------------------- #

def write_excel(data: ReportData, path: str) -> str | None:
    try:
        import openpyxl  # noqa: F401
    except ImportError:
        print(
            "[warn] openpyxl is not installed -- skipping the workbook "
            "(pip install openpyxl)",
            file=sys.stderr,
        )
        return None

    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    sheets = prepared(data)

    with pd.ExcelWriter(path, engine="openpyxl") as xl:
        _cover(data).to_excel(xl, sheet_name="cover", index=False)
        for _attr, sheet, _title in SECTIONS:
            df = sheets.get(sheet)
            if df is None or df.empty:
                continue
            df.to_excel(xl, sheet_name=sheet[:31], index=False)
            ws = xl.sheets[sheet[:31]]
            ws.freeze_panes = "A2"
            for i, col in enumerate(df.columns, start=1):
                width = max(len(str(col)), *(len(str(v)) for v in df[col].head(200))) if len(df) else len(str(col))
                ws.column_dimensions[
                    openpyxl.utils.get_column_letter(i)
                ].width = min(max(width + 2, 10), 48)
    return path


def _cover(data: ReportData) -> pd.DataFrame:
    rows = [
        ("report", "CAS India execution retrospective"),
        ("date", data.date.isoformat() if data.date else "real time (no date filter)"),
        ("flow", data.flow),
        ("kdb mode", data.mode),
        ("timezone", "HKT (IST = HKT - 02:30)"),
        ("generated", dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        ("price band", f"+/-{C.CAS_PRICE_BAND:.0%} of the CAS reference price"),
        ("universe", f"{len(data.universe)} CAS-eligible syms"),
    ]
    for w in data.warnings:
        rows.append(("warning", w))
    return pd.DataFrame(rows, columns=["field", "value"])


# --------------------------------------------------------------------------- #
# HTML                                                                         #
# --------------------------------------------------------------------------- #

_CSS = """
.viz-root{color-scheme:light;
 --surface-1:#fcfcfb;--plane:#f9f9f7;
 --text-primary:#0b0b0b;--text-secondary:#52514e;--text-muted:#898781;
 --grid:#e1e0d9;--axis:#c3c2b7;--border:rgba(11,11,11,.10);
 --series-1:#2a78d6;--series-2:#eb6834;--series-3:#1baf7a;
 --seq-250:#86b6ef;--seq-450:#2a78d6;--seq-600:#184f95;
 --good:#0ca30c;--warning:#fab219;--serious:#ec835a;--critical:#d03b3b;}
@media (prefers-color-scheme:dark){:root:where(:not([data-theme="light"])) .viz-root{
 color-scheme:dark;
 --surface-1:#1a1a19;--plane:#0d0d0d;
 --text-primary:#fff;--text-secondary:#c3c2b7;--text-muted:#898781;
 --grid:#2c2c2a;--axis:#383835;--border:rgba(255,255,255,.10);
 --series-1:#3987e5;--series-2:#d95926;--series-3:#199e70;
 --seq-250:#184f95;--seq-450:#3987e5;--seq-600:#86b6ef;}}
:root[data-theme="dark"] .viz-root{color-scheme:dark;
 --surface-1:#1a1a19;--plane:#0d0d0d;
 --text-primary:#fff;--text-secondary:#c3c2b7;--text-muted:#898781;
 --grid:#2c2c2a;--axis:#383835;--border:rgba(255,255,255,.10);
 --series-1:#3987e5;--series-2:#d95926;--series-3:#199e70;
 --seq-250:#184f95;--seq-450:#3987e5;--seq-600:#86b6ef;}

.viz-root{font-family:system-ui,-apple-system,"Segoe UI",sans-serif;
 background:var(--plane);color:var(--text-primary);
 margin:0;padding:28px 20px 64px;line-height:1.45;font-size:14px;}
.wrap{max-width:1200px;margin:0 auto;}
h1{font-size:22px;margin:0 0 4px;font-weight:650;}
h2{font-size:15px;margin:36px 0 10px;font-weight:650;letter-spacing:.01em;}
h3{font-size:13px;margin:20px 0 8px;font-weight:600;color:var(--text-secondary);}
.sub{color:var(--text-secondary);font-size:13px;margin:0 0 20px;}
.card{background:var(--surface-1);border:1px solid var(--border);border-radius:10px;
 padding:16px 18px;margin:0 0 16px;}
.tiles{display:flex;flex-wrap:wrap;gap:10px;margin:0 0 8px;}
.tile{background:var(--surface-1);border:1px solid var(--border);border-radius:10px;
 padding:12px 14px;min-width:150px;flex:1 1 150px;}
.tile .k{font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--text-muted);}
.tile .v{font-size:24px;font-weight:600;margin-top:3px;}
.tile .n{font-size:12px;color:var(--text-secondary);margin-top:2px;}
table{border-collapse:collapse;width:100%;font-size:12.5px;
 font-variant-numeric:tabular-nums;}
th,td{text-align:left;padding:6px 10px;border-bottom:1px solid var(--grid);
 white-space:nowrap;}
th{color:var(--text-muted);font-weight:600;font-size:11px;text-transform:uppercase;
 letter-spacing:.05em;position:sticky;top:0;background:var(--surface-1);}
td.num{text-align:right;}
.scroll{overflow-x:auto;max-height:520px;overflow-y:auto;
 border:1px solid var(--border);border-radius:10px;background:var(--surface-1);}
.legend{display:flex;flex-wrap:wrap;gap:14px;margin:0 0 10px;font-size:12px;
 color:var(--text-secondary);}
.legend i{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:6px;
 vertical-align:-1px;}
svg{display:block;max-width:100%;}
svg .mark{transition:opacity .12s;}
svg g:hover .mark{opacity:.78;}
.badge{display:inline-block;padding:1px 7px;border-radius:20px;font-size:11px;
 font-weight:600;border:1px solid var(--border);}
.badge.ok{color:var(--good);}
.badge.review{color:var(--critical);}
.foot{color:var(--text-muted);font-size:11.5px;margin-top:28px;}
"""


def _esc(v) -> str:
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return ""
    return html.escape(str(v))


def _clip(s: str, n: int) -> str:
    """Keep an SVG label inside its gutter -- the tables carry the full text."""
    s = str(s)
    return s if len(s) <= n else s[: n - 1] + "…"


def _html_table(df: pd.DataFrame, max_rows: int = 400) -> str:
    if df is None or df.empty:
        return '<p class="sub">nothing to show</p>'
    view = df.head(max_rows)
    num = {c for c in view.columns if pd.api.types.is_numeric_dtype(view[c])}
    head = "".join(f"<th>{_esc(c)}</th>" for c in view.columns)
    body = []
    for _, r in view.iterrows():
        cells = []
        for c in view.columns:
            v = r[c]
            cls = ' class="num"' if c in num else ""
            if isinstance(v, float) and not np.isnan(v):
                v = f"{v:,.4f}".rstrip("0").rstrip(".") if abs(v) < 1e6 else f"{v:,.0f}"
            cells.append(f"<td{cls}>{_esc(v)}</td>")
        body.append("<tr>" + "".join(cells) + "</tr>")
    more = ""
    if len(df) > max_rows:
        more = f'<p class="sub">showing the first {max_rows:,} of {len(df):,} rows - the CSV and workbook carry all of them</p>'
    return (
        f'<div class="scroll"><table><thead><tr>{head}</tr></thead>'
        f'<tbody>{"".join(body)}</tbody></table></div>{more}'
    )


def _bar_chart(
    items: list[tuple[str, float]],
    *,
    unit: str = "",
    color: str = "var(--seq-450)",
    width: int = 1080,
    row_h: int = 26,
    label_w: int = 330,
) -> str:
    """Horizontal bars, one series, direct-labelled. Empty list -> a note."""
    items = [(k, float(v)) for k, v in items if v is not None and not (isinstance(v, float) and np.isnan(v))]
    if not items:
        return '<p class="sub">nothing to plot</p>'
    vmax = max(abs(v) for _, v in items) or 1.0
    plot_w = width - label_w - 90
    height = row_h * len(items) + 8
    rows = []
    for i, (k, v) in enumerate(items):
        y = i * row_h + 4
        w = max(2.0, abs(v) / vmax * plot_w)
        val = f"{v:,.2f}{unit}" if abs(v) < 1000 else f"{v:,.0f}{unit}"
        rows.append(
            f'<g><title>{_esc(k)}: {_esc(val)}</title>'
            f'<text x="{label_w - 10}" y="{y + 13}" text-anchor="end" font-size="12" '
            f'fill="var(--text-secondary)">{_esc(k)}</text>'
            f'<rect class="mark" x="{label_w}" y="{y + 3}" width="{w:.1f}" height="{row_h - 12}" '
            f'rx="4" fill="{color}"/>'
            f'<text x="{label_w + w + 8:.1f}" y="{y + 13}" font-size="12" '
            f'fill="var(--text-primary)">{_esc(val)}</text></g>'
        )
    return (
        f'<svg viewBox="0 0 {width} {height}" width="{width}" height="{height}" role="img">'
        f'<line x1="{label_w}" y1="0" x2="{label_w}" y2="{height}" stroke="var(--axis)" '
        f'stroke-width="1"/>{"".join(rows)}</svg>'
    )


def _grouped_bar_chart(
    rows: list[tuple[str, float, float]],
    labels: tuple[str, str],
    *,
    unit: str = "%",
    width: int = 1160,
    label_w: int = 470,
) -> str:
    """Two series per category: today vs benchmark."""
    rows = [(k, a, b) for k, a, b in rows if not (pd.isna(a) and pd.isna(b))]
    if not rows:
        return '<p class="sub">nothing to plot</p>'
    vals = [v for _, a, b in rows for v in (a, b) if pd.notna(v)]
    vmax = max(abs(v) for v in vals) or 1.0
    plot_w = width - label_w - 110
    bar_h, gap, grp_h = 11, 3, 34
    height = grp_h * len(rows) + 8
    out = []
    for i, (k, a, b) in enumerate(rows):
        y = i * grp_h + 4
        for j, (v, colour, name) in enumerate(
            ((a, "var(--series-1)", labels[0]), (b, "var(--series-2)", labels[1]))
        ):
            if pd.isna(v):
                continue
            w = max(2.0, abs(v) / vmax * plot_w)
            yy = y + j * (bar_h + gap)
            out.append(
                f'<g><title>{_esc(k)} - {_esc(name)}: {v:,.2f}{unit}</title>'
                f'<rect class="mark" x="{label_w}" y="{yy}" width="{w:.1f}" height="{bar_h}" '
                f'rx="4" fill="{colour}"/>'
                f'<text x="{label_w + w + 7:.1f}" y="{yy + bar_h - 1}" font-size="11" '
                f'fill="var(--text-primary)">{v:,.2f}{unit}</text></g>'
            )
        out.append(
            f'<text x="{label_w - 10}" y="{y + 17}" text-anchor="end" font-size="12" '
            f'fill="var(--text-secondary)">{_esc(k)}</text>'
        )
    legend = (
        f'<div class="legend">'
        f'<span><i style="background:var(--series-1)"></i>{_esc(labels[0])}</span>'
        f'<span><i style="background:var(--series-2)"></i>{_esc(labels[1])}</span></div>'
    )
    return legend + (
        f'<svg viewBox="0 0 {width} {height}" width="{width}" height="{height}" role="img">'
        f'<line x1="{label_w}" y1="0" x2="{label_w}" y2="{height}" stroke="var(--axis)" '
        f'stroke-width="1"/>{"".join(out)}</svg>'
    )


def _tile(k: str, v: str, note: str = "") -> str:
    note_html = f'<div class="n">{_esc(note)}</div>' if note else ""
    return f'<div class="tile"><div class="k">{_esc(k)}</div><div class="v">{_esc(v)}</div>{note_html}</div>'


def write_html(data: ReportData, path: str) -> str:
    d = data.date.isoformat() if data.date else "real time"
    parts: list[str] = []

    parts.append(f"<h1>CAS India execution retrospective</h1>")
    parts.append(
        f'<p class="sub">{_esc(d)} &nbsp;&middot;&nbsp; flow <strong>{_esc(data.flow)}</strong>'
        f' &nbsp;&middot;&nbsp; {_esc(data.mode.upper())} tapes'
        f' &nbsp;&middot;&nbsp; all times HKT (IST = HKT &minus; 02:30)'
        f' &nbsp;&middot;&nbsp; {len(data.universe):,} CAS-eligible syms</p>'
    )

    if data.orders.empty:
        parts.append('<div class="card"><p>No parent order matched the filters.</p></div>')
    else:
        tot = data.summary[data.summary["flow"] == "TOTAL"]
        row = (tot if not tot.empty else data.summary).iloc[0]

        tiles = [
            _tile("Parent orders", f"{int(row['parent_orders']):,}", f"{int(row['syms']):,} symbols"),
            _tile("Order quantity", f"{row['order_qty']:,.0f}", "shares"),
            _tile("Executed", f"{row['executed_qty']:,.0f}", f"{row['fill_pct']:,.1f}% of the order"),
            _tile("Residual", f"{row['residual_qty']:,.0f}", "shares left unexecuted"),
            _tile("Traded in the auction", f"{row['close_qty']:,.0f}",
                  f"{row['close_pct_of_executed']:,.1f}% of what we executed"),
            _tile("Orders in the close", f"{row['participation_rate_pct']:,.1f}%",
                  f"{int(row['orders_filled_in_close']):,} of {int(row['parent_orders']):,}"),
        ]
        parts.append(f'<div class="tiles">{"".join(tiles)}</div>')

        # -- participation ------------------------------------------------- #
        parts.append("<h2>Close participation</h2>")
        counts = data.orders["participation"].value_counts()
        legend = (
            '<div class="legend">'
            '<span><i style="background:var(--series-1)"></i>Traded in the auction</span>'
            '<span><i style="background:var(--series-2)"></i>Sent, never traded</span>'
            '<span><i style="background:var(--series-3)"></i>Never sent</span></div>'
        )
        colours = {
            "FILLED_IN_CLOSE": "var(--series-1)",
            "SENT_NOT_FILLED": "var(--series-2)",
            "NOT_SENT": "var(--series-3)",
        }
        bars = []
        vmax = counts.max() if len(counts) else 1
        for i, key in enumerate(("FILLED_IN_CLOSE", "SENT_NOT_FILLED", "NOT_SENT")):
            n = int(counts.get(key, 0))
            w = max(2.0, n / (vmax or 1) * 600)
            y = i * 30 + 4
            bars.append(
                f'<g><title>{_esc(CL.PARTICIPATION[key])}: {n:,} orders</title>'
                f'<text x="350" y="{y + 15}" text-anchor="end" font-size="12" '
                f'fill="var(--text-secondary)">{_esc(CL.PARTICIPATION[key])}</text>'
                f'<rect class="mark" x="360" y="{y + 4}" width="{w:.1f}" height="15" rx="4" '
                f'fill="{colours[key]}"/>'
                f'<text x="{368 + w:.1f}" y="{y + 16}" font-size="12" '
                f'fill="var(--text-primary)">{n:,}</text></g>'
            )
        parts.append(
            f'<div class="card">{legend}'
            f'<svg viewBox="0 0 1080 100" width="1080" height="100" role="img">'
            f'<line x1="360" y1="0" x2="360" y2="100" stroke="var(--axis)"/>'
            f'{"".join(bars)}</svg></div>'
        )

        # -- reasons -------------------------------------------------------- #
        miss = data.non_participation
        if not miss.empty and "reason_code" in miss.columns:
            agg = (
                miss.groupby("reason_code")["id_target"].count()
                .sort_values(ascending=False)
            )
            items = [(_clip(code, 46), float(n)) for code, n in agg.items()]
            parts.append("<h2>Why orders did not trade in the auction</h2>")
            parts.append(
                f'<div class="card">{_bar_chart(items, unit=" orders", label_w=330)}</div>'
                f'<p class="sub">Reason codes are spelled out in the '
                f'&ldquo;{_esc("Orders that did not trade in the close")}&rdquo; table below.</p>'
            )

        # -- benchmark ------------------------------------------------------ #
        if not data.benchmark.empty:
            rows = [
                (_clip(r["metric"], 66), r["value_pct"], r["benchmark_pct"])
                for _, r in data.benchmark.iterrows()
            ]
            parts.append("<h2>Volume share vs the desk benchmarks</h2>")
            parts.append(
                '<div class="card">'
                + _grouped_bar_chart(rows, ("today", "benchmark"))
                + "</div>"
            )

        # -- rejections ----------------------------------------------------- #
        rej, cxl = data.rejections, data.cancellations
        rj_tiles = [
            _tile("Rejections - continuous", f"{int((rej['phase'] == 'CONTINUOUS').sum()) if not rej.empty else 0:,}",
                  "child orders refused before 17:45 HKT"),
            _tile("Rejections - close", f"{int((rej['phase'] == 'CLOSE').sum()) if not rej.empty else 0:,}",
                  "child orders refused during CAS"),
            _tile("Cancellations - continuous", f"{int((cxl['phase'] == 'CONTINUOUS').sum()) if not cxl.empty else 0:,}", ""),
            _tile("Cancellations - close", f"{int((cxl['phase'] == 'CLOSE').sum()) if not cxl.empty else 0:,}", ""),
        ]
        parts.append("<h2>Rejections and cancellations</h2>")
        parts.append(f'<div class="tiles">{"".join(rj_tiles)}</div>')
        if not cxl.empty:
            reasons = cxl.groupby(["phase", "reason"])["id_work"].count().sort_values(ascending=False).head(20)
            items = [(_clip(f"{p} - {r}", 58), float(n)) for (p, r), n in reasons.items()]
            parts.append("<h3>Cancellation reasons</h3>")
            parts.append(
                f'<div class="card">'
                f'{_bar_chart(items, unit=" orders", color="var(--seq-250)", label_w=400)}</div>'
            )

    # -- tables ------------------------------------------------------------- #
    sheets = prepared(data)
    for _attr, sheet, title in SECTIONS:
        df = sheets.get(sheet)
        if df is None or df.empty:
            continue
        parts.append(f"<h2>{_esc(title)}</h2>")
        parts.append(_html_table(df))

    parts.append(
        '<p class="foot">Reference price = VWAP 17:30-17:45 HKT (15:00-15:15 IST), '
        'falling back to the last print of the day and then to the previous adjusted close. '
        f'CAS price band = &plusmn;{C.CAS_PRICE_BAND:.0%} of that reference. '
        f'Generated {dt.datetime.now():%Y-%m-%d %H:%M:%S}.</p>'
    )

    doc = (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        f"<title>CAS retrospective {_esc(d)} - {_esc(data.flow)}</title>"
        f"<style>{_CSS}</style></head>"
        f'<body class="viz-root"><div class="wrap">{"".join(parts)}</div></body></html>'
    )

    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(doc)
    return path

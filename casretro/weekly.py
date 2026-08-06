"""Weekly retrospective: run the daily pipeline over a range and roll it up.

The daily report answers "what happened in yesterday's auction".  Run on a Friday
this module answers the question the desk actually asks at the end of the week --
"is this getting better or worse, and on which names" -- which a single day can
never answer, because one bad print looks the same as a habit.

Two pieces:

* `resolve_dates()` turns `--weekly` / `--from` / `--to` into a list of business
  days.  A holiday needs no special casing: the day comes back with no parent
  order and is dropped with a note.

* `combine_days()` folds a list of daily `ReportData` into one week-wide
  `ReportData`, so every existing writer -- console, CSV, workbook, both HTML
  pages -- works on a week without knowing it is looking at one.  It is pure
  pandas, takes no connection, and is exercised by `tools/selftest.py`.

Nothing here re-derives a number the daily pipeline already computed.  Quantities
are summed, shares are recomputed from the summed quantities (never averaged --
a 5-day average of five daily percentages is not the week's percentage), and the
per-child mix tables are rebuilt from each day's own base frame, so a parent id
that repeats across days can never be collapsed into one.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from . import config as C
from . import metrics as M
from . import sessions as S
from .build import ReportData, _build_benchmark, _build_summary, build_report

#: Monday-Friday.  The Indian exchange calendar has holidays this does not know
#: about; they arrive as a day with no parent order and are dropped with a note.
_WEEKEND = (5, 6)

#: How many days `--weekly` looks back over, anchor included.
WEEK_LENGTH = 5


#: What to do about the **live day** -- the most recent business day on or before
#: today.  It is the only day whose source is ever in question: it may or may not
#: have been written down to the HDB yet.  Every earlier day comes off the HDB,
#: always.
#:
#:   auto  -- the RT tapes, falling back to the HDB when they no longer hold the
#:            day because the write-down has already happened.  Default.
#:            Run on a Thursday: Thursday off RT, Monday-Wednesday off the HDB.
#:            Run on Friday evening: Friday off RT, Monday-Thursday off the HDB.
#:            Run once the day has been transferred: everything off the HDB.
#:   force -- RT only for the live day, no fallback, so a rolled tape shows up as
#:            a missing day instead of being quietly served from the HDB.
#:   off   -- HDB only.  The live day is simply absent until write-down.
RT_TODAY_POLICIES = ("auto", "force", "off")


@dataclass
class WeekReport:
    """The week-wide report, plus the daily ones it was built from."""

    combined: ReportData
    days: list[ReportData] = field(default_factory=list)
    missing: list[dt.date] = field(default_factory=list)

    @property
    def dates(self) -> list[dt.date]:
        return [d.date for d in self.days if d.date]

    @property
    def rt_dates(self) -> list[dt.date]:
        """The days that came off the real-time tapes rather than the HDB."""
        return [d.date for d in self.days if d.mode == "rt" and d.date]


# --------------------------------------------------------------------------- #
# Dates                                                                        #
# --------------------------------------------------------------------------- #

def is_business_day(d: dt.date) -> bool:
    return d.weekday() not in _WEEKEND


def business_days(start: dt.date, end: dt.date) -> list[dt.date]:
    """Every Mon-Fri in [start, end], inclusive."""
    if end < start:
        start, end = end, start
    out, day = [], start
    while day <= end:
        if is_business_day(day):
            out.append(day)
        day += dt.timedelta(days=1)
    return out


def week_of(anchor: dt.date, length: int = WEEK_LENGTH) -> list[dt.date]:
    """The business days of `anchor`'s week, up to and including `anchor`.

    Run on Friday evening this is Monday-Friday.  Run on a Wednesday it is
    Monday-Wednesday rather than a week padded with days that have not happened
    yet.  Run on a weekend it is the whole of the preceding week.
    """
    if not is_business_day(anchor):
        # Saturday / Sunday -> the Friday that just closed, and its week.
        anchor -= dt.timedelta(days=anchor.weekday() - 4)
    monday = anchor - dt.timedelta(days=anchor.weekday())
    start = monday - dt.timedelta(days=max(0, length - 5))
    return business_days(start, anchor)


def latest_business_day(today: dt.date) -> dt.date:
    """The most recent Mon-Fri on or before `today`.  Saturday -> Friday."""
    d = today
    while not is_business_day(d):
        d -= dt.timedelta(days=1)
    return d


def sources_for_day(
    date: dt.date,
    today: dt.date | None,
    *,
    rt_available: bool,
    policy: str = "auto",
) -> tuple[str, ...]:
    """Which tapes to read a day from, in the order to try them.

    Only the **live day** is ever in question -- the most recent business day on
    or before today.  Run on Thursday that is Thursday; run on Friday evening it
    is Friday; run on the Saturday or Sunday after, it is still Friday, because
    the RT tapes have not rolled into a new session yet.

    Anything older is written down in the HDB by definition, and reading it off a
    real-time tape would be reading whatever that tape holds *now*, stamped with
    a date it does not belong to.  That is why this is a whitelist and not a
    fallback chain: by the Monday, `--weekly --date <last Friday>` must go
    nowhere near RT.

    The live day itself is read from the tapes first, because that is where it
    is while the session is still the current one, and from the HDB second, for
    once it has been transferred.  Whichever answers first wins, so the same
    command works before and after the write-down without being told which side
    of it you are on.

    Pure, and separately tested: getting it wrong means a review that reports
    four days as five, or one that labels today's tape as last week.
    """
    if today is None or not rt_available or policy == "off":
        return ("ht",)
    if date != latest_business_day(today):
        return ("ht",)
    if policy == "force":
        return ("rt",)
    return ("rt", "ht")


def resolve_dates(
    *,
    anchor: dt.date | None = None,
    date_from: dt.date | None = None,
    date_to: dt.date | None = None,
    length: int = WEEK_LENGTH,
) -> list[dt.date]:
    """`--from/--to` when given, otherwise the week ending at `anchor`."""
    if date_from or date_to:
        start = date_from or date_to
        end = date_to or date_from
        return business_days(start, end)
    if anchor is None:
        raise ValueError("resolve_dates needs an anchor or a from/to range")
    return week_of(anchor, length)


# --------------------------------------------------------------------------- #
# Roll-up helpers                                                              #
# --------------------------------------------------------------------------- #

def total_row(summary: pd.DataFrame) -> pd.Series | None:
    """The TOTAL row of a summary frame, or the only row when there is one flow."""
    if summary is None or summary.empty:
        return None
    tot = summary[summary["flow"] == "TOTAL"]
    return (tot if not tot.empty else summary).iloc[0]


def _stamp(df: pd.DataFrame, date: dt.date | None) -> pd.DataFrame:
    """Put `date` at the front of a frame, so a week-wide table stays sortable."""
    if df is None or df.empty:
        return pd.DataFrame() if df is None else df
    out = df.copy()
    out["date"] = date
    cols = ["date"] + [c for c in out.columns if c != "date"]
    return out[cols]


def _concat(days: list[ReportData], attr: str) -> pd.DataFrame:
    frames = []
    for d in days:
        df = getattr(d, attr, None)
        if df is not None and not df.empty:
            frames.append(_stamp(df, d.date))
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True, sort=False)


def _wpct(sym_stats: pd.DataFrame, num_col: str, den_col: str) -> float:
    """Volume-weighted percentage, the same way `build._build_benchmark` does it."""
    if sym_stats is None or sym_stats.empty:
        return float("nan")
    if num_col not in sym_stats or den_col not in sym_stats:
        return float("nan")
    num = pd.to_numeric(sym_stats[num_col], errors="coerce").fillna(0).sum()
    den = pd.to_numeric(sym_stats[den_col], errors="coerce").fillna(0).sum()
    return float(num / den * 100.0) if den else float("nan")


#: Columns of `sym_stats` that are *not* additive across days -- shares, rates and
#: comparisons.  Everything else numeric is a quantity and gets summed; the ones
#: listed here are recomputed from the summed quantities afterwards.
_SYM_DERIVED = (
    "mkt_cts_window_pct", "mkt_cas_window_pct", "mkt_cts_plus_cas_pct",
    "mkt_clsbin_pct", "vs_hist_clsbin_pp", "vs_day1_total_pp",
    "our_share_day_pct", "our_share_close_pct", "our_close_pct_of_our_day",
    "participation_rate_pct", "close_pct_of_executed", "fill_pct",
    "dayVwap", "totalVolume",
)


def _combine_sym_stats(days: list[ReportData]) -> pd.DataFrame:
    """Per-sym quantities summed over the week, shares recomputed from the sums."""
    stacked = _concat(days, "sym_stats")
    if stacked.empty:
        return pd.DataFrame()

    numeric = [
        c for c in stacked.columns
        if c not in ("sym", "date") and c not in _SYM_DERIVED
        and pd.api.types.is_numeric_dtype(stacked[c])
    ]
    g = stacked.groupby("sym", sort=False)
    out = g[numeric].sum(min_count=1).reset_index()
    out.insert(1, "n_days", g.size().to_numpy())

    # Market shares are recomputed from the summed quantities.  Averaging five
    # daily percentages would weight a quiet Monday like a heavy Friday.
    if "mkt_day_qty" in out.columns:
        denom = pd.to_numeric(out["mkt_day_qty"], errors="coerce").replace(0, np.nan)
        for name, col in (
            ("mkt_cts_window_pct", "mkt_cts_window_qty"),
            ("mkt_cas_window_pct", "mkt_cas_window_qty"),
            ("mkt_clsbin_pct", "mkt_clsbin_qty"),
        ):
            if col in out.columns:
                out[name] = pd.to_numeric(out[col], errors="coerce") / denom * 100.0
        if {"mkt_cts_window_pct", "mkt_cas_window_pct"} <= set(out.columns):
            out["mkt_cts_plus_cas_pct"] = (
                out["mkt_cts_window_pct"] + out["mkt_cas_window_pct"]
            )
            out["vs_day1_total_pp"] = (
                out["mkt_cts_plus_cas_pct"] - C.BENCHMARKS.day1_total_share
            )
        if "mkt_clsbin_pct" in out.columns:
            out["vs_hist_clsbin_pp"] = out["mkt_clsbin_pct"] - C.BENCHMARKS.hist_clsbin_avg

    out = M.participation_rates(out)
    return out.sort_values("sym", ignore_index=True)


def _combine_mix(days: list[ReportData], keys: list[str]) -> pd.DataFrame:
    """Rebuild a mix table over the week from each day's own child-order base.

    Rebuilt rather than added up: `id_target` is only unique *within* a day, so
    summing daily tables would count a repeated id once and stacking the raw
    frames would collapse two different parents into one.  Stamping the date onto
    the id first keeps `n_parents` honest, and `n_syms` genuinely distinct.
    """
    bases = []
    for d in days:
        base = M.child_order_mix_base(d.workorders, d.executions, d.orders)
        if base is None or base.empty:
            continue
        base = base.copy()
        base["id_target"] = (
            f"{d.date}:" + base["id_target"].astype(str) if d.date
            else base["id_target"].astype(str)
        )
        bases.append(base)
    if not bases:
        return pd.DataFrame()
    return M.mix_by(pd.concat(bases, ignore_index=True, sort=False), keys)


def _combine_warnings(days: list[ReportData]) -> list[str]:
    """One line per distinct warning, with the days it fired on."""
    seen: dict[str, list[str]] = {}
    for d in days:
        stamp = d.date.isoformat() if d.date else "?"
        for w in d.warnings:
            seen.setdefault(w, []).append(stamp)
    out = []
    for w, dates in seen.items():
        if len(dates) == len(days) and len(days) > 1:
            out.append(f"{w}  [every day]")
        elif len(days) > 1:
            out.append(f"{w}  [{', '.join(dates)}]")
        else:
            out.append(w)
    return out


def weighted_close_capture(orders: pd.DataFrame) -> float:
    """Close capture weighted by the size that traded in the auction.

    The summary's `mean_close_capture_bps` is a straight mean over parents, which
    lets a 100-share order pull as hard as a 100,000-share one.  Every headline
    figure on the trader page is size-weighted, and the day-by-day table has to
    agree with it or the two contradict each other on the same page.
    """
    if orders is None or orders.empty:
        return float("nan")
    v = pd.to_numeric(orders.get("close_capture_bps"), errors="coerce")
    w = pd.to_numeric(orders.get("close_qty"), errors="coerce")
    if v is None or w is None:
        return float("nan")
    ok = v.notna() & w.notna() & (w > 0)
    if not ok.any():
        return float("nan")
    return float((v[ok] * w[ok]).sum() / w[ok].sum())


def build_by_day(days: list[ReportData]) -> pd.DataFrame:
    """One row per trading day -- the trend the week exists to show."""
    rows = []
    for d in days:
        row = total_row(d.summary)
        if row is None:
            continue
        ss = d.sym_stats
        rows.append({
            "date": d.date,
            "weekday": d.date.strftime("%a") if d.date else "",
            # Which tape the day came off, so a week that mixes them says so on
            # the face of the table rather than in a warning underneath it.
            "source": str(d.mode).upper(),
            "parent_orders": int(row["parent_orders"]),
            "syms": int(row["syms"]),
            "order_qty": float(row["order_qty"]),
            "executed_qty": float(row["executed_qty"]),
            "fill_pct": float(row["fill_pct"]),
            "close_qty": float(row["close_qty"]),
            "close_pct_of_executed": float(row["close_pct_of_executed"]),
            "orders_filled_in_close": int(row["orders_filled_in_close"]),
            "orders_sent_not_filled": int(row["orders_sent_not_filled"]),
            "orders_not_sent": int(row["orders_not_sent"]),
            "participation_rate_pct": float(row["participation_rate_pct"]),
            "rejections_close": int(row["rejections_close"]),
            "rejections_after_close": int(row.get("rejections_after_close", 0) or 0),
            "cancellations_close": int(row["cancellations_close"]),
            "cancellations_after_close": int(row.get("cancellations_after_close", 0) or 0),
            "mean_close_capture_bps": float(row["mean_close_capture_bps"]),
            "close_capture_bps_wtd": weighted_close_capture(d.orders),
            "residual_qty": float(row["residual_qty"]),
            "residual_notional_at_close": float(row.get("residual_notional_at_close", np.nan)),
            "mkt_clsbin_pct": _wpct(ss, "mkt_clsbin_qty", "mkt_day_qty"),
            "mkt_cts_window_pct": _wpct(ss, "mkt_cts_window_qty", "mkt_day_qty"),
            "mkt_cas_window_pct": _wpct(ss, "mkt_cas_window_qty", "mkt_day_qty"),
            "our_share_of_auction_pct": _wpct(ss, "our_close_qty", "mkt_cas_window_qty"),
        })
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------- #
# The fold                                                                     #
# --------------------------------------------------------------------------- #

def combine_days(days: list[ReportData], flow: str, mode: str) -> ReportData:
    """Fold daily reports into one week-wide `ReportData`.

    Every writer takes it as-is; `dates` and `by_day` are what tell them they are
    looking at a week rather than a day.
    """
    days = [d for d in days if d is not None]
    if not days:
        raise ValueError("combine_days needs at least one day")

    orders = _concat(days, "orders")
    rej = _concat(days, "rejections")
    cxl = _concat(days, "cancellations")

    universe = _concat(days, "universe")
    if not universe.empty and "sym" in universe.columns:
        universe = (
            universe.drop(columns=["date"])
            .drop_duplicates(subset=["sym"])
            .reset_index(drop=True)
        )

    sym_stats = _combine_sym_stats(days)

    return ReportData(
        date=days[-1].date,
        mode=mode,
        flow=flow,
        universe=universe,
        orders=orders,
        non_participation=_concat(days, "non_participation"),
        rejections=rej,
        cancellations=cxl,
        workorders=_concat(days, "workorders"),
        executions=_concat(days, "executions"),
        mix_otype_basket=_combine_mix(days, ["otype_kind", "basket"]),
        mix_flow_venue_otype=_combine_mix(days, ["flow", "venue", "otype_kind"]),
        sym_stats=sym_stats,
        benchmark=_build_benchmark(sym_stats),
        ref_prices=_concat(days, "ref_prices"),
        timing=_concat(days, "timing"),
        alerts=_concat(days, "alerts"),
        reconciliation=_concat(days, "reconciliation"),
        summary=_build_summary(orders, rej, cxl) if not orders.empty else pd.DataFrame(),
        sessions=S.session_table(),
        warnings=_combine_warnings(days),
        by_day=build_by_day(days),
        dates=[d.date for d in days if d.date],
    )


# --------------------------------------------------------------------------- #
# Driver                                                                       #
# --------------------------------------------------------------------------- #

def build_week(
    pool,
    dates: list[dt.date],
    flow: str,
    *,
    rt_pool=None,
    rt_available: bool | None = None,
    today: dt.date | None = None,
    rt_today: str = "auto",
    verbose: bool = True,
    **kwargs,
) -> WeekReport:
    """Run the daily pipeline over `dates`, then fold the results together.

    `pool` is the HDB.  `rt_pool` is the real-time one, used for the live day
    only and in the order `sources_for_day` decides -- HDB first, tapes second,
    so a review run after the write-down never touches them and one run before it
    still sees the day.  It may be a **callable**, in which case the connection
    is only opened if a day actually needs it; `rt_available` then says whether
    an RT instance is configured at all, before anything is opened.

    A day that produced no parent order on any of its sources is dropped rather
    than folded in as a zero: a holiday is not a day we participated in nothing.
    It is listed in `WeekReport.missing` and reported by the caller.
    """
    if rt_available is None:
        rt_available = rt_pool is not None
    resolved: dict[str, object] = {}

    def pool_for(source: str):
        if source == "ht":
            return pool
        if "rt" not in resolved:
            resolved["rt"] = rt_pool() if callable(rt_pool) else rt_pool
        return resolved["rt"]

    days: list[ReportData] = []
    missing: list[dt.date] = []

    for i, date in enumerate(dates, start=1):
        sources = sources_for_day(
            date, today, rt_available=bool(rt_available), policy=rt_today
        )
        if verbose:
            print(f"\n[info] ---- day {i} of {len(dates)}: {date} "
                  f"({' then '.join(s.upper() for s in sources)}) ----", flush=True)

        data = None
        for n, source in enumerate(sources):
            src_pool = pool_for(source)
            if src_pool is None:
                # An RT instance was configured but could not be opened.  Not
                # fatal: the HDB either already answered or is about to.
                continue
            try:
                data = build_report(
                    src_pool, date, flow,
                    # The RT tapes get no date predicate server-side, so the day
                    # is enforced here instead -- and a tape that has rolled
                    # comes back empty and hands over to the HDB.
                    enforce_date=(source == "rt"),
                    verbose=verbose, **kwargs,
                )
            except SystemExit as exc:
                # The universe coming back empty is fatal for a day, not a week.
                data = None
                if verbose:
                    print(f"[warn] {date} on {source.upper()}: {exc}", flush=True)
            if data is not None and not data.orders.empty:
                if source == "rt":
                    data.warnings.append(
                        f"{date} was read from the real-time tapes, not the HDB: "
                        f"they carry no date predicate, so the day was enforced "
                        f"row by row on the order tables. Market-data figures "
                        f"for this day are only this day's if that tape holds "
                        f"only this day."
                    )
                break
            data = None
            if verbose and n + 1 < len(sources):
                print(f"[info] {date}: nothing on {source.upper()} -- "
                      f"trying {sources[n + 1].upper()}", flush=True)

        if data is None:
            missing.append(date)
            if verbose:
                print(f"[info] {date}: no parent order on "
                      f"{'/'.join(s.upper() for s in sources)} -- holiday, "
                      f"nothing traded, or not yet written down", flush=True)
            continue
        days.append(data)

    if not days:
        raise SystemExit(
            "[fatal] no trading day in the requested range produced a parent "
            "order.  Check the dates, the flow filter and the ISIN whitelist."
        )

    modes = {d.mode for d in days}
    combined = combine_days(days, flow, "+".join(sorted(modes)))
    if missing:
        combined.warnings.append(
            "no data on " + ", ".join(d.isoformat() for d in missing)
            + " -- excluded from every number in this report"
        )
    return WeekReport(combined=combined, days=days, missing=missing)

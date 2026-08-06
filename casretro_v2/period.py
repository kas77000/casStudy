"""Run a period, day by day, and stack the days.

v2 is weekly by construction -- every chart is one bar per day and every table
has a Day column -- so a single day is just a period of length one rather than a
separate code path.

Date resolution and the HT/RT decision live in `days.py`; this module is the
loop that uses them.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

import pandas as pd

from . import build as B
from . import fx as FX
from . import metrics as M
from .days import (                      # noqa: F401  (re-exported for the CLI)
    RT_TODAY_POLICIES,
    business_days,
    latest_business_day,
    resolve_dates,
    sources_for_day,
    week_of,
)


@dataclass
class PeriodData:
    """Every day of the period, stacked, plus the three sections."""

    dates: list[dt.date]
    flow: str
    children: pd.DataFrame
    market: pd.DataFrame
    execution_quality: pd.DataFrame
    flows: pd.DataFrame
    flows_total: pd.Series | None
    fx_note: str = ""
    modes: list[str] = field(default_factory=list)
    missing: list[dt.date] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        if not self.dates:
            return "no data"
        if self.dates[0] == self.dates[-1]:
            return self.dates[0].isoformat()
        return f"{self.dates[0].isoformat()} to {self.dates[-1].isoformat()}"

    @property
    def is_multi_day(self) -> bool:
        return len(self.dates) > 1

    @property
    def rt_dates(self) -> list[dt.date]:
        return [d for d, m in zip(self.dates, self.modes) if m == "rt"]

    def clients(self, flow: str) -> pd.DataFrame:
        return M.top_clients(self.children, self.market, flow)

    @property
    def flows_present(self) -> list[str]:
        return M.flows_present(self.children)


def assemble(days: list[B.DayData], flow: str) -> PeriodData:
    """Stack the days and derive the three sections.  Pure -- no connection."""
    days = [d for d in days if d is not None]
    children = pd.concat(
        [d.children for d in days if d.children is not None and not d.children.empty],
        ignore_index=True, sort=False,
    ) if days else pd.DataFrame()
    market = pd.concat(
        [d.market for d in days if d.market is not None and not d.market.empty],
        ignore_index=True, sort=False,
    ) if days else pd.DataFrame()

    warnings: list[str] = []
    for d in days:
        warnings += d.warnings

    # Each day was converted at its own `fx_last`; the note quotes the last
    # day's rate as the illustrative one, and says that is what it is.
    fx_note = ""
    priced = [d for d in days if d.fx_factors is not None and not d.fx_factors.empty]
    if priced:
        fx_note = FX.describe(priced[-1].fx_factors)
        if len(days) > 1:
            fx_note = (f"Each day converted at its own fx_last. "
                       f"{fx_note.replace('Converted at', 'Last day at')}")

    return PeriodData(
        dates=[d.date for d in days if d.date],
        flow=flow,
        children=children,
        market=market,
        execution_quality=M.execution_quality(children),
        flows=M.flows(children, market),
        flows_total=M.flows_total(children, market),
        fx_note=fx_note,
        modes=[d.mode for d in days],
        warnings=warnings,
    )


def build_period(
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
) -> PeriodData:
    """Load every day of `dates`, then assemble.

    Source selection per day is `casretro.weekly.sources_for_day`: the live day
    off the RT tapes, falling back to the HDB once it has been transferred, and
    every earlier day off the HDB.
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

    days: list[B.DayData] = []
    missing: list[dt.date] = []
    failed: dict[dt.date, list[str]] = {}

    for i, date in enumerate(dates, start=1):
        sources = sources_for_day(
            date, today, rt_available=bool(rt_available), policy=rt_today
        )
        if verbose:
            print(f"\n[info] ---- day {i} of {len(dates)}: {date} "
                  f"({' then '.join(s.upper() for s in sources)}) ----", flush=True)

        day = None
        attempts: list[str] = []       # why each source did not answer
        for n, source in enumerate(sources):
            src_pool = pool_for(source)
            if src_pool is None:
                attempts.append(f"{source.upper()} not reachable")
                continue

            got, why = None, None
            try:
                got = B.load_day(
                    src_pool, date, flow,
                    # The RT tapes carry no date predicate, so the day is
                    # enforced row by row -- and a rolled tape then comes back
                    # empty and hands over to the HDB.
                    enforce_date=(source == "rt"),
                    verbose=verbose, **kwargs,
                )
            except SystemExit as exc:
                why = str(exc)
            except Exception as exc:
                # A tape that errors -- a dropped connection, a table the RT
                # instance does not carry, a q error -- must not take the run
                # down with it.  That is precisely when the other source is
                # wanted.  The reason is kept and reported rather than swallowed.
                why = f"{type(exc).__name__}: {exc}"

            if why is None and got is not None and not got.children.empty:
                day = got
                if source == "rt":
                    day.warnings.append(
                        f"{date} was read from the real-time tapes, not the HDB "
                        f"-- these figures are provisional until the write-down"
                    )
                break

            attempts.append(f"{source.upper()}: {why or 'no close child order'}")
            if verbose:
                print(f"[info] {date}: {attempts[-1]}"
                      + (f" -- trying {sources[n + 1].upper()}"
                         if n + 1 < len(sources) else ""), flush=True)

        if day is None:
            missing.append(date)
            failed[date] = attempts
            if verbose:
                print(f"[info] {date}: no data on any source "
                      f"({'; '.join(attempts)})", flush=True)
            continue
        days.append(day)

    if not days:
        raise SystemExit(
            "[fatal] no day in the requested range produced a close child order. "
            "Check the dates, the flow filter and the ISIN whitelist."
        )

    out = assemble(days, flow)
    out.missing = missing
    if missing:
        out.warnings.append(
            "no data on " + ", ".join(d.isoformat() for d in missing)
            + " -- excluded from every number in this report"
        )
        # A day that failed for a *reason* is not the same as a holiday, so the
        # reason is carried rather than folded into "no data".
        for date, attempts in failed.items():
            errored = [a for a in attempts if "no close child order" not in a]
            if errored:
                out.warnings.append(f"{date}: {'; '.join(errored)}")
    return out

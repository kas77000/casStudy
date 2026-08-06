#!/usr/bin/env python3
"""End-to-end smoke test of the v2 report -- no kdb, no pykx.

Builds a three-day fixture whose every number is known by hand, pushes it
through `build_children`, the three measures and the writer, and asserts the
arithmetic the page rests on:

  * a child order's event log collapses to one row, so an amended order is
    counted once and not once per amendment;
  * executed quantity is priced off the fill tape, unfilled limit quantity off
    the workorder, and unfilled market quantity off the auction close;
  * fill ratios are computed from summed quantities, never averaged from daily
    ratios;
  * USD conversion survives either direction of the fx_last quote;
  * the market denominator counts a symbol once per day however many times we
    traded it.

    python tools/selftest_v2.py [--out DIR] [--keep]
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
import tempfile

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from casretro import config as C                     # noqa: E402
from casretro_v2 import build as B                   # noqa: E402
from casretro_v2 import config as V                  # noqa: E402
from casretro_v2 import days as D                    # noqa: E402
from casretro_v2 import fx as FX                     # noqa: E402
from casretro_v2 import metrics as M                 # noqa: E402
from casretro_v2 import report as R                  # noqa: E402
from casretro_v2.period import PeriodData, assemble  # noqa: E402

DATES = [dt.date(2026, 8, 3), dt.date(2026, 8, 4), dt.date(2026, 8, 5)]

FX_INR = 85.0          # INR per USD, the "divide" direction
CLOSE_PX = 100.0       # what the auction prints, every sym, every day
LIMIT_PX = 99.0        # what our unfilled limit child orders were priced at
FILL_PX = 100.5        # what our fills got

#: (id_work, id_target, sym, basket, otype, sent, filled)
#: 501 is the amended one: it appears twice in the workorder log.
CHILDREN = [
    (501, 101, "RELIANCE.IN", "SILK_ASIA",  "limit",  10_000, 6_000),
    (502, 102, "TCS.IN",      "SILK_ASIA",  "market",  5_000, 5_000),
    (503, 103, "INFY.IN",     "AGENCY_LOW", "limit",   8_000, 2_000),
    (504, 104, "RELIANCE.IN", "AGENCY_LOW", "market",  4_000, 1_000),
]

SYMS = sorted({c[2] for c in CHILDREN})


def build_workorders(date: dt.date) -> pd.DataFrame:
    rows = []
    for id_work, id_target, sym, _basket, otype, sent, _filled in CHILDREN:
        base = {
            "date": date, "id_work": id_work, "id_target": id_target, "sym": sym,
            "venue": "NSE_CLOSE", "venuetype": "CLOSE", "otype": otype,
            "size": sent, "price": LIMIT_PX if otype == "limit" else np.nan,
            "state": "filled", "side": "buy",
        }
        if id_work == 501:
            # The event log: an amend, then the final state.  Summing `size`
            # across both would report 20,000 sent for a 10,000-share order.
            rows.append({**base, "time": pd.Timedelta("17:51:00"), "size": 9_000,
                         "state": "sent"})
            rows.append({**base, "time": pd.Timedelta("17:53:00")})
        else:
            rows.append({**base, "time": pd.Timedelta("17:52:00")})
    # A continuous child order, which every v2 number must ignore.
    rows.append({
        "date": date, "id_work": 599, "id_target": 101, "sym": "RELIANCE.IN",
        "venue": "NSE", "venuetype": "LIT", "otype": "limit", "size": 50_000,
        "price": 98.0, "state": "filled", "side": "buy",
        "time": pd.Timedelta("10:00:00"),
    })
    return pd.DataFrame(rows)


def build_executions(date: dt.date) -> pd.DataFrame:
    rows = []
    for id_work, id_target, sym, _basket, _otype, _sent, filled in CHILDREN:
        if filled <= 0:
            continue
        rows.append({
            "date": date, "time": pd.Timedelta("18:00:01"), "id_work": id_work,
            "id_target": id_target, "sym": sym, "fillsize": filled,
            "fillprice": FILL_PX, "ostat": "fill", "side": "buy",
        })
    rows.append({
        "date": date, "time": pd.Timedelta("10:00:01"), "id_work": 599,
        "id_target": 101, "sym": "RELIANCE.IN", "fillsize": 50_000,
        "fillprice": 98.0, "ostat": "fill", "side": "buy",
    })
    return pd.DataFrame(rows)


def build_targets() -> pd.DataFrame:
    seen, rows = set(), []
    for _w, id_target, sym, basket, _o, _s, _f in CHILDREN:
        if id_target in seen:
            continue
        seen.add(id_target)
        rows.append({"id_target": id_target, "sym": sym, "basket": basket,
                     "trader": "T1", "portfolio": "P1", "side": "buy"})
    return pd.DataFrame(rows)


def build_market(date: dt.date) -> pd.DataFrame:
    return pd.DataFrame({
        "date": date,
        "sym": SYMS,
        "mkt_close_qty": [1_000_000.0] * len(SYMS),
        "mkt_close_px": [CLOSE_PX] * len(SYMS),
        "mkt_close_notional_local": [1_000_000.0 * CLOSE_PX] * len(SYMS),
        "mkt_close_notional_usd": [1_000_000.0 * CLOSE_PX / FX_INR] * len(SYMS),
    })


def build_universe() -> pd.DataFrame:
    return pd.DataFrame({"sym": SYMS, "CRNCY": "INR", "fx_last": FX_INR})


def day(date: dt.date, factors: pd.DataFrame) -> B.DayData:
    market = build_market(date)
    children = B.build_children(
        date, build_targets(), build_workorders(date), build_executions(date),
        market, factors,
    )
    return B.DayData(date=date, children=children, market=market,
                     universe=build_universe(), fx_factors=factors, mode="ht")


def close(a, b, tol=1e-6) -> bool:
    return a is not None and b is not None and abs(float(a) - float(b)) <= tol


def check_fallback(factors: pd.DataFrame) -> list[str]:
    """The live day falls back to the HDB when the tapes cannot answer.

    Driven through `build_period` with stub pools, because the decision is not
    in `sources_for_day` alone -- it is the loop that has to keep going after an
    empty result *or* a raised exception, and an exception on the RT tape is
    exactly when the HDB is wanted most.
    """
    from casretro_v2 import period as P

    out: list[str] = []
    thu = dt.date(2026, 8, 6)                     # the live day
    week = D.week_of(thu)                         # Mon..Thu
    real = B.load_day

    class Pool:
        def __init__(self, mode):
            self.mode = mode

    def run(rt_behaviour):
        """rt_behaviour: 'has' | 'empty' | 'raises' | 'exits'."""
        def fake(pool, date, flow, **kw):
            if pool.mode == "rt":
                if rt_behaviour == "raises":
                    raise RuntimeError("connection reset by peer")
                if rt_behaviour == "exits":
                    raise SystemExit("[fatal] the CAS universe came back empty")
                if rt_behaviour == "empty" or date != thu:
                    return B.DayData(date=date, children=pd.DataFrame(),
                                     market=pd.DataFrame(),
                                     universe=pd.DataFrame(), mode="rt")
            if pool.mode == "ht" and date == thu and rt_behaviour == "has":
                # The HDB has not written today down yet.
                return B.DayData(date=date, children=pd.DataFrame(),
                                 market=pd.DataFrame(),
                                 universe=pd.DataFrame(), mode="ht")
            d = day(date, factors)
            d.mode = pool.mode
            return d

        B.load_day = fake
        try:
            return P.build_period(
                Pool("ht"), week, "both", rt_pool=Pool("rt"), rt_available=True,
                today=thu, verbose=False,
            )
        finally:
            B.load_day = real

    # 1. the tapes have the live day -> RT for it, HT for the rest
    got = run("has")
    if not got.modes or got.modes[-1] != "rt":
        out.append(f"  fallback: live day came off {got.modes[-1:] or 'nothing'}, "
                   f"expected RT")
    if got.rt_dates != [thu]:
        out.append(f"  fallback: rt_dates is {got.rt_dates}, expected [{thu}]")
    if set(got.modes[:-1]) != {"ht"}:
        out.append(f"  fallback: earlier days came off {set(got.modes[:-1])}, "
                   f"expected HT only")

    # 2. the tapes have rolled -> the live day comes off the HDB instead
    for behaviour, label in (("empty", "an empty RT tape"),
                             ("raises", "an RT tape that errors"),
                             ("exits", "an RT tape with no universe")):
        got = run(behaviour)
        if len(got.dates) != len(week):
            out.append(f"  fallback: {label} lost days -- got {len(got.dates)} "
                       f"of {len(week)}")
        if got.modes and got.modes[-1] != "ht":
            out.append(f"  fallback: with {label} the live day came off "
                       f"{got.modes[-1].upper()}, expected HT")
        if thu in got.missing:
            out.append(f"  fallback: with {label} the live day was dropped "
                       f"instead of being read from the HDB")
        if got.rt_dates:
            out.append(f"  fallback: with {label} a day was still marked RT")

    # An error must be reported, not swallowed into a silent "no data".
    got = run("raises")
    if not any("connection reset" in w for w in got.warnings) and thu in got.missing:
        out.append("  fallback: an RT error was swallowed without a reason")
    return out


def check_day_sources() -> list[str]:
    """Which days a run covers, and which tape each is read from.

    Pure scheduling logic, so it is checked without a database.  Both failures it
    guards are silent in production: a Friday review that misses Friday because
    the HDB has not written it down, or -- worse -- a review of last week that
    stamps today's real-time tape with last Friday's date.
    """
    out: list[str] = []
    fri = dt.date(2026, 8, 7)
    week = D.week_of(dt.date(2026, 8, 8))          # run on the Saturday
    if week != [fri - dt.timedelta(days=i) for i in range(4, -1, -1)]:
        out.append(f"  sources: a Saturday run covers {week[0]}..{week[-1]}, "
                   f"expected the Mon-Fri that just closed")

    def sources(day, today, policy="auto", rt=True):
        return D.sources_for_day(day, today, rt_available=rt, policy=policy)

    # Friday, Saturday and Sunday all treat Friday as the live day: the RT tapes
    # have not rolled into a new session yet.
    for today in (fri, fri + dt.timedelta(days=1), fri + dt.timedelta(days=2)):
        if sources(fri, today) != ("rt", "ht"):
            out.append(f"  sources: run on {today:%a}, Friday reads "
                       f"{sources(fri, today)} -- expected RT then HT")
        for earlier in week[:-1]:
            if sources(earlier, today) != ("ht",):
                out.append(f"  sources: run on {today:%a}, {earlier:%a} reads "
                           f"{sources(earlier, today)} -- only the live day may "
                           f"come off RT")

    # Mid-week is the same shape one day earlier: run on the Thursday, Thursday
    # is live and Monday-Wednesday are written down.
    thu = dt.date(2026, 8, 6)
    midweek = D.week_of(thu)
    if [d.strftime("%a") for d in midweek] != ["Mon", "Tue", "Wed", "Thu"]:
        out.append(f"  sources: a Thursday run covers {midweek} -- expected "
                   f"Monday to Thursday, not a padded week")
    if sources(thu, thu) != ("rt", "ht"):
        out.append(f"  sources: run on Thursday, Thursday reads "
                   f"{sources(thu, thu)} -- expected RT then HT")
    for earlier in midweek[:-1]:
        if sources(earlier, thu) != ("ht",):
            out.append(f"  sources: run on Thursday, {earlier:%a} reads "
                       f"{sources(earlier, thu)} -- expected HT")

    # By the Monday the write-down has happened and RT holds another session.
    for today in (fri + dt.timedelta(days=3), fri + dt.timedelta(days=14)):
        if sources(fri, today) != ("ht",):
            out.append(f"  sources: run on {today}, Friday reads "
                       f"{sources(fri, today)} -- a past day must never come "
                       f"off the real-time tape")

    if sources(fri, fri, policy="force") != ("rt",):
        out.append("  sources: --rt-today force still falls back to the HDB")
    if sources(fri, fri, policy="off") != ("ht",):
        out.append("  sources: --rt-today off still reaches for the RT tapes")
    if sources(fri, fri, rt=False) != ("ht",):
        out.append("  sources: RT was chosen with no RT instance configured")

    # -- an explicit range, which must never reach for RT once it is history -- #
    span = D.resolve_dates(date_from=dt.date(2026, 7, 27), date_to=fri)
    if len(span) != 10:
        out.append(f"  sources: --from/--to over two weeks gave {len(span)} "
                   f"business days, expected 10")

    # -- the row-level date guard on a non-partitioned tape ------------------ #
    tape = pd.DataFrame({"date": [fri, fri, fri - dt.timedelta(days=1)],
                         "id_target": [1, 2, 3]})
    kept, dropped = D.clip_to_date(tape, fri)
    if dropped != 1 or list(kept["id_target"]) != [1, 2]:
        out.append(f"  sources: clip_to_date kept {list(kept['id_target'])}, "
                   f"dropped {dropped} -- expected the other day's row to go")
    if not D.clip_to_date(tape, fri - dt.timedelta(days=9))[0].empty:
        out.append("  sources: a tape holding none of the requested day was not "
                   "emptied -- the RT-to-HDB handover depends on that")
    if len(D.clip_to_date(pd.DataFrame({"id_target": [1, 2]}), fri)[0]) != 2:
        out.append("  sources: a tape with no date column lost rows")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", help="write the report here (default: a temp dir)")
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()

    failures: list[str] = check_day_sources()

    # -- FX ----------------------------------------------------------------- #
    factors, fx_warn = FX.usd_factors(build_universe())
    if not close(factors["usd_factor"].iloc[0], 1.0 / FX_INR):
        failures.append(f"  fx: factor {factors['usd_factor'].iloc[0]} for a rate "
                        f"of {FX_INR}, expected 1/{FX_INR}")
    # The reciprocal quote has to land on the same USD number.
    flipped = build_universe().assign(fx_last=1.0 / FX_INR)
    if not close(FX.usd_factors(flipped)[0]["usd_factor"].iloc[0], 1.0 / FX_INR):
        failures.append("  fx: the reciprocal quote did not convert to the same USD")
    if not close(FX.factor_from_rate(FX_INR, V.FX_MULTIPLY), FX_INR):
        failures.append("  fx: --fx multiply was not honoured")
    if FX.usd_factors(build_universe().assign(CRNCY="USD"))[0]["usd_factor"].iloc[0] != 1.0:
        failures.append("  fx: a USD-quoted sym was still converted")

    failures += check_fallback(factors)

    # -- the child-order frame ---------------------------------------------- #
    days = [day(d, factors) for d in DATES]
    ch = days[0].children

    if len(ch) != len(CHILDREN):
        failures.append(f"  children: {len(ch)} rows for {len(CHILDREN)} close "
                        f"child orders -- the event log did not collapse, or the "
                        f"continuous child order was not excluded")
    if 599 in set(ch["id_work"]):
        failures.append("  children: a continuous-venue child order reached v2")

    amended = ch[ch["id_work"] == 501]
    if not amended.empty and not close(amended["sent_qty"].iloc[0], 10_000):
        failures.append(f"  children: the amended order reports "
                        f"{amended['sent_qty'].iloc[0]:,.0f} sent, expected 10,000 "
                        f"(the last event wins)")

    # Pricing: executed off the tape, unfilled limit off the workorder,
    # unfilled market off the auction close.
    for id_work, want_px, want_src in ((501, LIMIT_PX, "workorder"),
                                       (504, CLOSE_PX, "auction close")):
        row = ch[ch["id_work"] == id_work]
        if row.empty:
            continue
        if not close(row["unfilled_px"].iloc[0], want_px):
            failures.append(f"  children: id_work {id_work} priced its unfilled "
                            f"quantity at {row['unfilled_px'].iloc[0]}, expected {want_px}")
        if row["unfilled_px_source"].iloc[0] != want_src:
            failures.append(f"  children: id_work {id_work} sourced its price from "
                            f"{row['unfilled_px_source'].iloc[0]!r}, expected {want_src!r}")

    r501 = ch[ch["id_work"] == 501].iloc[0]
    want_exec = 6_000 * FILL_PX
    want_unfilled = 4_000 * LIMIT_PX
    if not close(r501["exec_notional_local"], want_exec):
        failures.append(f"  children: executed notional {r501['exec_notional_local']:,.0f} "
                        f"!= {want_exec:,.0f}")
    if not close(r501["unfilled_notional_local"], want_unfilled):
        failures.append(f"  children: unfilled notional {r501['unfilled_notional_local']:,.0f} "
                        f"!= {want_unfilled:,.0f}")
    if not close(r501["sent_notional_usd"], (want_exec + want_unfilled) / FX_INR):
        failures.append("  children: sent notional in USD does not match the local "
                        "notional over the rate")

    # -- section 1 ----------------------------------------------------------- #
    period = assemble(days, "both")
    eq = period.execution_quality
    if len(eq) != len(DATES) * 2:
        failures.append(f"  execution quality: {len(eq)} rows for "
                        f"{len(DATES)} days x 2 order types")

    limit_day = eq[(eq["otype_kind"] == "LIMIT") & (eq["date"] == DATES[0])]
    if not limit_day.empty:
        r = limit_day.iloc[0]
        if not close(r["sent_qty"], 18_000) or not close(r["exec_qty"], 8_000):
            failures.append(f"  execution quality: limit sent/executed "
                            f"{r['sent_qty']:,.0f}/{r['exec_qty']:,.0f}, "
                            f"expected 18,000/8,000")
        if not close(r["fill_rate_pct"], 8_000 / 18_000 * 100):
            failures.append("  execution quality: limit fill ratio is not "
                            "executed / sent")
    market_day = eq[(eq["otype_kind"] == "MARKET") & (eq["date"] == DATES[0])]
    if not market_day.empty and not close(market_day.iloc[0]["exec_qty"], 6_000):
        failures.append("  execution quality: market executed quantity is wrong")

    # The period ratio must come off summed quantities, not the mean of the days.
    totals = M.execution_quality_totals(eq)
    lim = totals[totals["otype_kind"] == "LIMIT"]
    if not lim.empty:
        want = lim["exec_qty"].iloc[0] / lim["sent_qty"].iloc[0] * 100
        if not close(lim["fill_rate_pct"].iloc[0], want):
            failures.append("  execution quality: the period fill ratio is not "
                            "summed-executed / summed-sent")
        if not close(lim["sent_qty"].iloc[0], 18_000 * len(DATES)):
            failures.append("  execution quality: the period did not add up over days")

    # -- section 2 ----------------------------------------------------------- #
    fl = period.flows
    if fl.empty:
        failures.append("  flows: came back empty")
    else:
        want_rows = len(DATES) * 2 * 2       # days x flows x types
        if len(fl) != want_rows:
            failures.append(f"  flows: {len(fl)} rows, expected {want_rows}")

        # AGENCY on day 1 traded INFY (limit) and RELIANCE (market): one distinct
        # symbol each, so each row's market denominator is ONE symbol's close.
        row = fl[(fl["date"] == DATES[0]) & (fl["flow"] == C.FLOW_AGENCY)
                 & (fl["otype_kind"] == "LIMIT")]
        if not row.empty:
            r = row.iloc[0]
            if not close(r["mkt_close_qty"], 1_000_000):
                failures.append(f"  flows: market close volume {r['mkt_close_qty']:,.0f} "
                                f"for one symbol, expected 1,000,000")
            want_pct = r["exec_notional_usd"] / (1_000_000 * CLOSE_PX / FX_INR) * 100
            if not close(r["our_pct_of_market_notional"], want_pct):
                failures.append("  flows: our % of market notional is not our "
                                "notional over the market's")

        # SILK on day 1 traded RELIANCE and TCS across its two rows; the total
        # row must count each symbol once per day, not once per row.
        t = period.flows_total
        if t is not None:
            want_qty = 1_000_000 * len(SYMS) * len(DATES)
            if not close(t["mkt_close_qty"], want_qty):
                failures.append(
                    f"  flows: the period market volume is {t['mkt_close_qty']:,.0f}, "
                    f"expected {want_qty:,.0f} -- a symbol traded twice in a day "
                    f"must be counted once")

    # -- section 3 ----------------------------------------------------------- #
    silk = period.clients(C.FLOW_SILK)
    if silk.empty:
        failures.append("  clients: SILK came back empty")
    elif silk[V.CLIENT_COLUMN].iloc[0] != "SILK_ASIA":
        failures.append(f"  clients: top SILK basket is {silk[V.CLIENT_COLUMN].iloc[0]!r}")
    elif not close(silk["n_days"].iloc[0], len(DATES)):
        failures.append("  clients: n_days does not count the period's days")
    if period.flows_present != [C.FLOW_SILK, C.FLOW_AGENCY]:
        failures.append(f"  clients: flows present {period.flows_present}")
    ranked = period.clients(C.FLOW_SILK)["exec_notional_usd"].tolist()
    if ranked != sorted(ranked, reverse=True):
        failures.append("  clients: not ranked by notional traded in the close")

    # -- the writer ---------------------------------------------------------- #
    outdir = args.out or tempfile.mkdtemp(prefix="cas_v2_selftest_")
    html = R.write_html(period, os.path.join(outdir, "cas_v2_selftest.html"))
    R.write_csvs(period, os.path.join(outdir, "csv"))
    body = open(html, encoding="utf-8").read()

    for token in ("Execution Quality", "Flows", "Top 5 clients", "Market", "Limit"):
        if token not in body:
            failures.append(f"  page: no {token!r} section")
    for token in ("id_work", "id_target", "otype_kind", "nan"):
        if token in body:
            failures.append(f"  page: leaks {token!r}")
    if "<link" in body or 'src="http' in body:
        failures.append("  page: pulls an external resource -- it must be "
                        "self-contained to survive being emailed")

    # A single day still renders: one bar per chart.
    one = assemble([days[0]], "both")
    R.write_html(one, os.path.join(outdir, "cas_v2_selftest_oneday.html"))
    if one.is_multi_day:
        failures.append("  page: a one-day period reports itself as multi-day")

    print(f"  v2 selftest output -> {outdir}\n")
    if failures:
        print("SELFTEST v2 FAILED")
        print("\n".join(failures))
        return 1

    print("SELFTEST v2 OK")
    print(f"  children  : {len(ch)} close child orders/day, event log collapsed, "
          f"continuous excluded")
    print(f"  pricing   : fills off the tape, unfilled limits off the workorder, "
          f"unfilled markets off the auction close")
    print(f"  ratios    : computed from summed quantities over {len(DATES)} days")
    print(f"  fx        : both quote directions land on the same USD")
    print(f"  market    : each symbol counted once per day in the denominator")
    print(f"  days      : the live day reads RT then HT, every earlier day HT, "
          f"on Thursday, Friday, Saturday and Sunday alike")
    print(f"  fallback  : an empty, erroring or universe-less RT tape hands the "
          f"live day to the HDB without losing it")
    return 0


if __name__ == "__main__":
    sys.exit(main())

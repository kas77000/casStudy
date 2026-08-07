#!/usr/bin/env python3
"""End-to-end smoke test of the v2 report -- no kdb, no pykx.

Builds a three-day fixture whose every number is known by hand, pushes it
through `build_children`, the three measures and the writer, and asserts the
arithmetic the page rests on:

  * the close population predicate still says what `temp.q` says -- it is the
    denominator of every fill rate on the page;
  * quantities come off the workorder (`size` sent, `make` executed) and the
    execution tape is not re-added behind them;
  * unfilled limit quantity is priced at the order's own price and unfilled
    market quantity at the auction close;
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
from casretro_v2 import report2 as R2                # noqa: E402
from casretro_v2.period import PeriodData, assemble  # noqa: E402

DATES = [dt.date(2026, 8, 3), dt.date(2026, 8, 4), dt.date(2026, 8, 5)]

FX_INR = 85.0          # INR per USD, the "divide" direction
CLOSE_PX = 100.0       # what the auction prints, every sym, every day
LIMIT_PX = 101.0       # our limit price -- at or through the fill, so marketable
FILL_PX = 100.5        # workorder.avg_fill_price

#: (id_work, id_target, sym, basket, otype, size, make)
#: These are rows that have ALREADY passed the server-side filter in
#: `loaders.load_close_workorders`: close venue, make > 0, make <= size,
#: t_off_market after 17:58, and a marketable limit.  The filter itself is q, so
#: it is checked against the generated query text in `check_close_workorder_query`
#: rather than reproduced here in pandas -- one definition, not two.
CHILDREN = [
    (501, 101, "RELIANCE.IN", "SILK_ASIA",  "limit",  10_000, 6_000),
    (502, 102, "TCS.IN",      "SILK_ASIA",  "market",  5_000, 5_000),
    (503, 103, "INFY.IN",     "AGENCY_LOW", "limit",   8_000, 2_000),
    (504, 104, "RELIANCE.IN", "AGENCY_LOW", "market",  4_000, 1_000),
]

SYMS = sorted({c[2] for c in CHILDREN})


def build_close_workorders(date: dt.date) -> pd.DataFrame:
    """What `load_close_workorders` hands back: one row per surviving child."""
    rows = []
    for id_work, id_target, sym, _basket, otype, size, make in CHILDREN:
        rows.append({
            "date": date, "id_target": id_target, "id_work": id_work, "sym": sym,
            "side": "buy", "otype": otype, "venue": "NSE_CLOSE",
            "size": float(size), "make": float(make),
            "price": LIMIT_PX if otype == "limit" else np.nan,
            "avg_fill_price": FILL_PX,
            "t_off_market": pd.Timedelta("18:02:00"),
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
        date, build_targets(), build_close_workorders(date), market, factors,
    )
    return B.DayData(date=date, children=children, market=market,
                     universe=build_universe(), fx_factors=factors, mode="ht")


def close(a, b, tol=1e-6) -> bool:
    return a is not None and b is not None and abs(float(a) - float(b)) <= tol


def check_flow_breakdown(period) -> list[str]:
    """The per-flow tables add up to the combined one.

    Page 2 now shows market and limit for each flow *and* for both together.
    If those disagree the reader has no way to tell which is wrong, so the
    reconciliation is asserted rather than assumed.
    """
    out: list[str] = []
    eq, fl = period.execution_quality, period.flows
    if eq is None or eq.empty or fl is None or fl.empty:
        return ["  breakdown: nothing to reconcile"]

    for otype in V.OTYPES:
        combined = eq[eq["otype_kind"] == otype]
        per_flow = fl[fl["otype_kind"] == otype]
        if combined.empty:
            continue
        for col in ("sent_qty", "exec_qty", "exec_notional_usd"):
            want = float(pd.to_numeric(combined[col], errors="coerce").sum())
            got = float(pd.to_numeric(per_flow[col], errors="coerce").sum())
            if not close(got, want, tol=1e-6 * max(1.0, abs(want))):
                out.append(f"  breakdown: {otype} {col} is {got:,.2f} across the "
                           f"flows but {want:,.2f} combined")

    # And each flow really is a strict part, not a copy of the whole.
    flows = period.flows_present
    if len(flows) > 1:
        for flow in flows:
            sub = fl[fl["flow"] == flow]
            if sub.empty:
                out.append(f"  breakdown: {flow} has no rows of its own")
            elif float(sub["exec_qty"].sum()) >= float(eq["exec_qty"].sum()):
                out.append(f"  breakdown: {flow} alone accounts for every share "
                           f"executed -- the flow filter is not reaching the table")
    return out


def check_two_page_layout(path: str, period) -> list[str]:
    """The v2 layout: charts on page 1, tables on page 2, no script to run it."""
    out: list[str] = []
    body = open(path, encoding="utf-8").read()
    name = os.path.basename(path)

    page1 = body[body.index('class="page pg-1"'):body.index('class="page pg-2"')]
    page2 = body[body.index('class="page pg-2"'):]

    # Page 1 is charts, page 2 is tables.  Neither should carry the other's job.
    if "<svg" not in page1:
        out.append(f"  {name}: page 1 has no chart")
    if "<table" in page1:
        out.append(f"  {name}: page 1 carries a table -- the data belongs on page 2")
    if "<table" not in page2:
        out.append(f"  {name}: page 2 has no table")
    if "<svg" in page2:
        out.append(f"  {name}: page 2 carries a chart -- the charts belong on page 1")

    # The tab is CSS, not script: an email client that blocks JS must still work.
    if "<script" in body or "onclick" in body:
        out.append(f"  {name}: the tab needs script to work")
    for rule in ("#pg1:checked", "#pg2:checked", "@media print"):
        if rule not in body:
            out.append(f"  {name}: no {rule!r} rule -- the tab or the print "
                       f"fallback is missing")

    # Both flows get their own chart section, and their own client chart.
    flows = period.flows_present
    for flow in flows:
        if str(flow).title() not in page1:
            out.append(f"  {name}: page 1 has no section for {flow}")
    n_charts = page1.count("<svg")
    want = 2 * len(flows) + len(flows)      # market + limit per flow, + clients
    if n_charts != want:
        out.append(f"  {name}: page 1 draws {n_charts} charts, expected {want} "
                   f"({len(flows)} flow(s) x market+limit, plus one client chart "
                   f"each)")
    return out


def check_close_workorder_query() -> list[str]:
    """The population predicate, asserted against the q that goes on the wire.

    This is the single most consequential thing in the report -- it is the
    denominator of every fill rate -- and it lives in q, so it cannot be
    exercised by a pandas fixture.  What can be checked without a database is
    that the query still *says* what `temp.q` says, which is what catches a
    predicate being dropped or inverted in an edit.
    """
    out: list[str] = []
    from casretro import kdbio as K
    from casretro import loaders as L
    from casretro_v2 import loaders as VL

    real_require, real_sym = K.require_pykx, K.sym_vector
    K.require_pykx = lambda: None
    K.sym_vector = lambda v: list(v)

    class Rec:
        def __init__(self, inst, cols):
            self.instance, self._cols, self.q = inst, cols, None
        def columns_of(self, t): return self._cols
        def query_pd(self, expr, *a):
            self.q = expr
            return pd.DataFrame()

    try:
        inst = C.Instance(role="oms", mode="ht", label="OMS", host="h", port=1,
                          partitioned=True, tables={"workorder": "workorder"})
        conn = Rec(inst, L.WORKORDER_COLS)
        VL.load_close_workorders(conn, DATES[0], ["A.IN"])
        q = " ".join((conn.q or "").split())
    finally:
        K.require_pykx, K.sym_vector = real_require, real_sym

    for token, why in (
        ('venue like "*CLOSE*"', "the auction, not continuous"),
        ("make <= size", "a child order cannot fill more than it asked for"),
        ("make > 0", "orders that traded nothing must not sit in the denominator"),
        ("t_off_market > `time$t", "it had to be live when the auction could freeze"),
        ("(otype<>`limit)", "market orders are exempt from the marketability test"),
        ("((side=`sell) & price <= avg_fill_price)", "a sell limit at or below its fill"),
        ("((side=`buy) & price >= avg_fill_price)", "a buy limit at or above its fill"),
        ("date=d", "the day"),
        ("sym in syms", "the universe"),
    ):
        if " ".join(token.split()) not in q:
            out.append(f"  close query: no {token!r} -- {why}")

    for col in ("size", "make", "avg_fill_price", "price", "t_off_market"):
        if col not in q:
            out.append(f"  close query: {col} is not selected, but is read downstream")

    # The cutoff pushed to the server is the configured one, not a literal.
    from casretro import kdbio as K2
    if K2.time_ms(V.OFF_MARKET_AFTER) != 64680000:
        out.append(f"  close query: the off-market cutoff is "
                   f"{V.OFF_MARKET_AFTER}, expected 17:58 HKT")
    return out


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


def check_clients_are_period_wide() -> list[str]:
    """The client tables rank on the whole period, not on any single day.

    Built to be unambiguous: STEADY trades a little on every day of the period
    and BURST trades more than all of that on one day alone.  If the ranking were
    per-day, or if it silently took the last day, STEADY would win.
    """
    out: list[str] = []
    rows = []
    for i, date in enumerate(DATES):
        rows.append({"date": date, "flow": "SILK", "basket": "STEADY",
                     "sym": f"S{i}.IN", "otype_kind": "LIMIT",
                     "id_target": 900 + i, "id_work": 9000 + i,
                     "sent_qty": 100.0, "exec_qty": 100.0, "unfilled_qty": 0.0,
                     "exec_notional_usd": 10.0, "unfilled_notional_usd": 0.0,
                     "sent_notional_usd": 10.0})
    rows.append({"date": DATES[0], "flow": "SILK", "basket": "BURST",
                 "sym": "B0.IN", "otype_kind": "LIMIT",
                 "id_target": 950, "id_work": 9500,
                 "sent_qty": 100.0, "exec_qty": 50.0, "unfilled_qty": 50.0,
                 "exec_notional_usd": 31.0, "unfilled_notional_usd": 31.0,
                 "sent_notional_usd": 62.0})
    children = pd.DataFrame(rows)

    top = M.top_clients(children, pd.DataFrame(), "SILK")
    if top.empty:
        return ["  clients: top_clients came back empty"]

    order = list(top[V.CLIENT_COLUMN])
    if order[:2] != ["BURST", "STEADY"]:
        out.append(f"  clients: ranked {order}, expected BURST first -- one big "
                   f"day must outrank three small ones, which is what makes the "
                   f"ranking period-wide rather than per-day")

    steady = top[top[V.CLIENT_COLUMN] == "STEADY"]
    if not steady.empty:
        r = steady.iloc[0]
        if not close(r["exec_notional_usd"], 10.0 * len(DATES)):
            out.append(f"  clients: STEADY shows {r['exec_notional_usd']} traded, "
                       f"expected {10.0 * len(DATES)} -- the period is summed, "
                       f"not sampled")
        if not close(r["n_days"], len(DATES)):
            out.append("  clients: n_days does not count every day of the period")
        if not close(r["n_syms"], len(DATES)):
            out.append("  clients: n_syms does not count distinct names over "
                       "the period")

    burst = top[top[V.CLIENT_COLUMN] == "BURST"]
    if not burst.empty and not close(burst.iloc[0]["fill_rate_pct"], 50.0):
        out.append("  clients: fill rate is not executed / sent over the period")
    return out


def check_market_coverage() -> list[str]:
    """Our share of the auction compares like with like.

    A symbol that never printed between 17:58 and 18:00 carries no closing price,
    so it contributes nothing to the market notional.  If its own executed
    notional stayed in the numerator the share would be overstated -- here, 100%
    instead of 10% -- so both sides are held to the same names and what that
    excluded is reported as coverage.
    """
    out: list[str] = []
    d = DATES[0]

    def child(sym, notional):
        return dict(date=d, flow="SILK", basket="B", sym=sym, otype_kind="LIMIT",
                    id_target=hash(sym) % 1000, id_work=hash(sym) % 1000,
                    sent_qty=notional, exec_qty=notional, unfilled_qty=0.0,
                    exec_notional_usd=notional, unfilled_notional_usd=0.0,
                    sent_notional_usd=notional)

    children = pd.DataFrame([child("A.IN", 100.0), child("NOPRINT.IN", 900.0)])
    market = pd.DataFrame([dict(date=d, sym="A.IN", mkt_close_qty=1000.0,
                                mkt_close_notional_usd=1000.0)])

    t = M.flows_total(children, market)
    if t is None:
        return ["  coverage: flows_total came back empty"]
    if not close(t["our_pct_of_market_notional"], 10.0):
        out.append(f"  coverage: share of the auction is "
                   f"{t['our_pct_of_market_notional']}%, expected 10% -- an "
                   f"unpriced name must leave the numerator as well as the "
                   f"denominator")
    if not close(t["covered_exec_notional_usd"], 100.0):
        out.append("  coverage: the numerator was not restricted to priced names")
    if not close(t["market_coverage_pct"], 10.0):
        out.append("  coverage: market_coverage_pct does not report how much of "
                   "our notional could be compared")

    k = M.headline(children, market)
    if not close(k.get("share_of_auction_pct"), 10.0):
        out.append("  coverage: the KPI tile disagrees with the Flows total")

    # Every symbol priced -> full coverage, and the share is the plain ratio.
    market2 = pd.concat([market, pd.DataFrame([
        dict(date=d, sym="NOPRINT.IN", mkt_close_qty=9000.0,
             mkt_close_notional_usd=9000.0)])], ignore_index=True)
    t2 = M.flows_total(children, market2)
    if not close(t2["market_coverage_pct"], 100.0) or not close(
            t2["our_pct_of_market_notional"], 10.0):
        out.append("  coverage: with every name priced the share should be "
                   "1,000 / 10,000 at 100% coverage")
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
    failures += check_clients_are_period_wide()
    failures += check_market_coverage()
    failures += check_close_workorder_query()

    # -- the child-order frame ---------------------------------------------- #
    days = [day(d, factors) for d in DATES]
    ch = days[0].children

    if len(ch) != len(CHILDREN):
        failures.append(f"  children: {len(ch)} rows for {len(CHILDREN)} close "
                        f"child orders that came back from the server")

    # Quantities are the workorder's own, not re-derived from anywhere else:
    # `size` is what was asked for and `make` is what traded.
    for id_work, _t, _s, _b, _o, want_sent, want_exec in CHILDREN:
        row = ch[ch["id_work"] == id_work]
        if row.empty:
            failures.append(f"  children: id_work {id_work} is missing")
            continue
        r = row.iloc[0]
        if not close(r["sent_qty"], want_sent):
            failures.append(f"  children: id_work {id_work} sent {r['sent_qty']:,.0f}, "
                            f"expected workorder.size = {want_sent:,.0f}")
        if not close(r["exec_qty"], want_exec):
            failures.append(f"  children: id_work {id_work} executed "
                            f"{r['exec_qty']:,.0f}, expected workorder.make = "
                            f"{want_exec:,.0f}")
        if not close(r["unfilled_qty"], want_sent - want_exec):
            failures.append(f"  children: id_work {id_work} unfilled quantity is "
                            f"not size - make")
        if not close(r["exec_px"], FILL_PX):
            failures.append(f"  children: id_work {id_work} priced its execution at "
                            f"{r['exec_px']}, expected workorder.avg_fill_price")

    # Pricing: executed at avg_fill_price, unfilled limit at the order's own
    # price, unfilled market at the auction close.
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

    # -- the writers --------------------------------------------------------- #
    outdir = args.out or tempfile.mkdtemp(prefix="cas_v2_selftest_")
    html = R.write_html(period, os.path.join(outdir, "cas_v2_selftest_v1.html"))
    html2 = R2.write_html(period, os.path.join(outdir, "cas_v2_selftest_v2.html"))
    R.write_csvs(period, os.path.join(outdir, "csv"))

    for path in (html, html2):
        body = open(path, encoding="utf-8").read()
        name = os.path.basename(path)
        for token in ("Execution Quality", "Flows", "Top 5 clients", "Market", "Limit"):
            if token not in body:
                failures.append(f"  {name}: no {token!r} section")
        for token in ("id_work", "id_target", "otype_kind", "nan"):
            if token in body:
                failures.append(f"  {name}: leaks {token!r}")
        if "<link" in body or 'src="http' in body:
            failures.append(f"  {name}: pulls an external resource -- it must be "
                            f"self-contained to survive being emailed")

    body = open(html, encoding="utf-8").read()
    failures += check_two_page_layout(html2, period)
    failures += check_flow_breakdown(period)

    # A single day still renders: one bar per chart.
    one = assemble([days[0]], "both")
    R.write_html(one, os.path.join(outdir, "cas_v2_selftest_oneday_v1.html"))
    R2.write_html(one, os.path.join(outdir, "cas_v2_selftest_oneday_v2.html"))
    if one.is_multi_day:
        failures.append("  page: a one-day period reports itself as multi-day")

    print(f"  v2 selftest output -> {outdir}\n")
    if failures:
        print("SELFTEST v2 FAILED")
        print("\n".join(failures))
        return 1

    print("SELFTEST v2 OK")
    print(f"  children  : {len(ch)} close child orders/day, size sent and make "
          f"executed straight off the workorder")
    print(f"  pricing   : executions at avg_fill_price, unfilled limits at their "
          f"own price, unfilled markets at the auction close")
    print(f"  ratios    : computed from summed quantities over {len(DATES)} days")
    print(f"  fx        : both quote directions land on the same USD")
    print(f"  market    : each symbol counted once per day in the denominator")
    print(f"  days      : the live day reads RT then HT, every earlier day HT, "
          f"on Thursday, Friday, Saturday and Sunday alike")
    print(f"  fallback  : an empty, erroring or universe-less RT tape hands the "
          f"live day to the HDB without losing it")
    print(f"  clients   : ranked on the whole period -- one big day outranks "
          f"three small ones")
    print(f"  coverage  : our share of the auction compares like with like, and "
          f"says how much it could compare")
    print(f"  population: the close query still carries every predicate from "
          f"temp.q")
    print(f"  layout    : v1 and v2 both written; v2 keeps charts on page 1 and "
          f"tables on page 2, with a CSS-only tab")
    print(f"  breakdown : the per-flow tables reconcile to the combined ones")
    return 0


if __name__ == "__main__":
    sys.exit(main())

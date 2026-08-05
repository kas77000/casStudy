#!/usr/bin/env python3
"""End-to-end smoke test of the analytical layer -- no kdb, no pykx.

Builds synthetic `target` / `target_state` / `workorder` / `execution` / `alerts`
frames whose columns match the real schemas, pushes them through
`build.assemble()` and the writers, then asserts that every branch of the
non-participation waterfall fired at least once.

    python tools/selftest.py [--out DIR] [--keep]

Exit code 0 means the pipeline runs clean and each expected reason was produced.
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

from casretro import config as C            # noqa: E402
from casretro import metrics as M           # noqa: E402
from casretro import report as R            # noqa: E402
from casretro.build import RawFrames, assemble  # noqa: E402

DATE = dt.date(2026, 8, 3)


def t(s: str) -> pd.Timedelta:
    return pd.Timedelta(s)


# --------------------------------------------------------------------------- #
# Scenario table -- one parent order per row                                   #
# --------------------------------------------------------------------------- #
#
# expect_reason is what the waterfall should conclude.  `None` means the order
# is expected to have traded in the auction.
#
# The waterfall is asserted against an *unfiltered* build, so every branch stays
# covered; the filtered build -- what a real run produces -- is then checked to
# contain exactly the scenarios that executed something.

SCENARIOS = [
    # id, sym, basket, side, sign, size, doclose, limit, expect_participation, expect_reason
    (101, "RELIANCE.IN", "SILK_ASIA",   "buy",  1, 10000, 1, 0.0,   "FILLED_IN_CLOSE", None),
    (102, "TCS.IN",      "SILK_ASIA",   "sell", -1, 5000, 1, 0.0,   "SENT_NOT_FILLED", "CLOSE_ORDER_REJECTED"),
    (103, "INFY.IN",     "AGENCY_LOW",  "buy",  1,  8000, 0, 0.0,   "NOT_SENT",        "NO_CLOSE_INSTRUCTION"),
    (104, "HDFCBANK.IN", "AGENCY_LOW",  "buy",  1,  4000, 1, 0.0,   "NOT_SENT",        "FULLY_FILLED_BEFORE_CAS"),
    (105, "ICICIBANK.IN","SILK_EU",     "buy",  1,  6000, 1, 0.0,   "NOT_SENT",        "PARENT_CANCELLED_BEFORE_CAS"),
    (106, "SBIN.IN",     "AGENCY_US",   "sell", -1, 3000, 1, 0.0,   "NOT_SENT",        "ORDER_END_BEFORE_CAS"),
    (107, "ITC.IN",      "SILK_ASIA",   "buy",  1,  7000, 1, 0.0,   "NOT_SENT",        "ORDER_ARRIVED_AFTER_ENTRY_CLOSED"),
    (108, "WIPRO.IN",    "AGENCY_LOW",  "buy",  1,  9000, 1, 80.0,  "NOT_SENT",        "LIMIT_OUTSIDE_PRICE_BAND"),
    (109, "AXISBANK.IN", "SILK_EU",     "sell", -1, 2500, 1, 0.0,   "NOT_SENT",        "ALGO_NEVER_COMMITTED_TO_CLOSE"),
    (110, "LT.IN",       "AGENCY_US",   "buy",  1,  5500, 1, 0.0,   "SENT_NOT_FILLED", "CLOSE_ORDER_CANCELLED"),
    (111, "MARUTI.IN",   "SILK_ASIA",   "buy",  1, 12000, 1, 0.0,   "SENT_NOT_FILLED", "NOT_MATCHED_IN_AUCTION"),
    (112, "TITAN.IN",    "AGENCY_LOW",  "sell", -1, 4500, 1, 0.0,   "FILLED_IN_CLOSE", None),
    # pulled by the client with nothing done
    (113, "NESTLEIND.IN","AGENCY_LOW",  "buy",  1,  5000, 1, 0.0,   "NOT_SENT",        "PARENT_CANCELLED_BEFORE_CAS"),
]

REF_PX = 100.0        # every synthetic sym trades around 100
CLOSE_PRINT_PX = 100.4  # what the auction prints
CLOSE_FILL_PX = 100.38  # what we get, so close capture is not trivially zero

#: id_target -> (quantity filled during continuous, quantity filled in the auction).
#: The state history, the child orders and the executions are all derived from
#: this, so the reconciliation checks in the report have to come back clean.
FILLS = {
    101: (4000, 6000),
    102: (1000, 0),
    103: (3000, 0),
    104: (4000, 0),     # == size, so the parent is done before the auction
    105: (2000, 0),
    106: (1500, 0),
    107: (0, 0),
    108: (0, 0),
    109: (500, 0),
    110: (2000, 0),
    111: (5000, 0),
    112: (2000, 2500),
    113: (0, 0),        # nothing at all, and then cancelled
}

#: Scenarios that executed nothing: a real run must drop them.
EXPECT_DROPPED = {tid for tid, (cont, close) in FILLS.items() if cont + close <= 0}


def build_universe() -> pd.DataFrame:
    syms = [s for _, s, *_ in SCENARIOS]
    return pd.DataFrame({
        "sym": syms,
        "ID_ISIN": [f"INE{i:03d}A0102{i % 10}" for i in range(len(syms))],
        "TICKER": [s.split(".")[0] for s in syms],
        "NAME": [s.split(".")[0].title() for s in syms],
        "CRNCY": "INR",
        "adv": 5_000_000.0,
        "px_last_prev": REF_PX,
        "fx_last": 1.0,
    })


def build_targets() -> pd.DataFrame:
    rows = []
    for tid, sym, basket, side, sign, size, doclose, limit, _p, _r in SCENARIOS:
        rows.append({
            "date": DATE, "id_server": 1, "time": t("09:30:00"),
            "id_target": tid, "trader": "JDOE", "basket": basket,
            "portfolio": "PF1", "wave": "W1",
            "oes_oid": f"OID{tid}", "oes_primoid": f"POID{tid}",
            "sym": sym, "side": side, "sidesign": sign, "size": size,
            "tif": "DAY", "otype": "limit" if limit else "market",
            "limit_price": limit,
            "t_oes_load": t("09:25:00"), "t_gen": t("09:29:00"),
            "t_start": t("18:05:00") if tid == 107 else t("09:30:00"),
            # 106 is the "participate in close = N" profile: it stops well before
            # the cutoff.  Everyone else carries a t_end inside the last minutes
            # of continuous, which is what a participating parent looks like on
            # the desk (see config.TEND_NO_CLOSE_CUTOFF).
            "t_end": t("17:30:00") if tid == 106 else t("17:44:00"),
            "p_start": "", "p_end": "CLOSE",
            "algo": "CLOSE_SEEKER", "alpha": 0.5, "beta": 0.0, "gamma": 0.0,
            "delta": 0.0, "iwould": 0.0, "stealth": 0,
            "doopen": 0, "doclose": doclose, "docash": 0,
            "cashratio": 0.0, "cashrange": 0.0,
        })
    return pd.DataFrame(rows)


def build_states() -> pd.DataFrame:
    """Snapshots at 17:40 (pre-CAS), 17:57 (inside CAS) and 18:10 (final)."""
    rows = []

    def snap(tid, sym, side, time, state, open_qty, done_qty, make_close):
        rows.append({
            "date": DATE, "time": time, "t_algo": time,
            "id_target": tid, "trader": "JDOE", "sym": sym, "side": side,
            "state": state, "ack": "Y",
            # `make` is the executed quantity and `open` what is left -- the two
            # columns the report reads off the latest row.  The close commitment
            # lives in make_close / leave_close / commit_close.
            "open": open_qty, "make": done_qty, "leave": make_close,
            "leave_vendor": 0, "commit": done_qty,
            "last_fill_price": REF_PX if done_qty else np.nan,
            "last_fill_size": 100 if done_qty else 0,
            "avg_fill_price": REF_PX if done_qty else np.nan,
            "make_close": make_close, "leave_close": make_close,
            "commit_close": make_close, "commit_open": 0, "count_send": 5,
        })

    for tid, sym, _b, side, _sg, size, _dc, _lim, _p, reason in SCENARIOS:
        cont, close = FILLS[tid]
        residual_at_cas = size - cont
        committed = 0 if reason == "ALGO_NEVER_COMMITTED_TO_CLOSE" else residual_at_cas

        if tid == 107:
            # arrives after order entry has already shut
            snap(tid, sym, side, t("18:05:30"), "new", size, 0, 0)
            continue

        cancelled = reason == "PARENT_CANCELLED_BEFORE_CAS"
        snap(tid, sym, side, t("17:40:00"),
             "cxl:client request" if cancelled else "working",
             residual_at_cas, cont, 0)
        snap(tid, sym, side, t("17:57:00"), "working", residual_at_cas, cont, committed)
        snap(tid, sym, side, t("18:10:00"), "done", size - cont - close, cont + close, committed)

    return pd.DataFrame(rows).sort_values(["id_target", "time"], ignore_index=True)


def build_workorders() -> pd.DataFrame:
    rows = []
    wid = 1000

    def wo(**kw):
        nonlocal wid
        wid += 1
        base = {
            "date": DATE, "time": t("11:00:00"), "id_work": wid,
            "id_candidate": 0, "id_ref": 0, "id_cxl": 0, "trader": "JDOE",
            "request": "new", "oes_oid": f"W{wid}", "oes_primoid": f"W{wid}",
            "display": 0, "tif": "DAY", "otype": "limit", "price": REF_PX,
            "limit_target": REF_PX, "limit_candidate": REF_PX,
            "bps_candidate": 0.0, "venue": "NSE", "venuetype": "LIT",
            "state": "filled", "count_send": 1, "count_chaseprice": 0,
            "make": 0, "avg_fill_price": REF_PX,
            "t_gen": t("11:00:00"), "t_start": t("11:00:00"),
            "t_end": t("11:00:05"), "t_transmit": t("11:00:00.100"),
            "t_oes_send": t("11:00:00.150"), "t_on_market": t("11:00:00.400"),
            "t_off_market": t("11:00:05"), "t_close": t("11:00:05"),
            "onmkt_bidprice": REF_PX - 0.05, "onmkt_askprice": REF_PX + 0.05,
        }
        base.update(kw)
        base.setdefault("size", 0)
        if not base.get("make"):
            base["make"] = base["size"]   # the algo made everything it sent
        rows.append(base)

    for tid, sym, _b, side, _sg, size, _dc, _lim, _p, _reason in SCENARIOS:
        cont, close = FILLS[tid]
        residual_at_cas = size - cont

        if cont > 0:        # what we did during continuous trading
            # 103's continuous child carries a venuetype that reads CLOSE while
            # its venue does not: only the venue may decide, so this fill has to
            # stay continuous and 103 has to stay NO_CLOSE_INSTRUCTION.
            wo(id_target=tid, sym=sym, side=side, size=cont,
               venuetype="LIT_CLOSE_ELIGIBLE" if tid == 103 else "LIT")

        if tid == 102:      # rejected in the auction on a band breach
            wo(id_target=tid, sym=sym, side=side, size=residual_at_cas,
               venue="NSE_CLOSE", venuetype="CLOSE", state="rejected:price outside CAS band",
               price=REF_PX * 1.08, time=t("17:52:00"),
               t_oes_send=t("17:52:00"), t_transmit=t("17:51:59"),
               t_on_market=pd.NaT, t_gen=t("17:51:58"))
        if tid == 110:      # cancelled before the match
            wo(id_target=tid, sym=sym, side=side, size=residual_at_cas,
               venue="NSE_CLOSE", venuetype="CLOSE", state="cxl:client amend",
               time=t("17:57:00"), t_oes_send=t("17:52:30"),
               t_gen=t("17:52:29"), t_on_market=t("17:52:31"))
        if tid == 111:      # stood in the auction, never matched
            wo(id_target=tid, sym=sym, side=side, size=residual_at_cas,
               venue="NSE_CLOSE", venuetype="CLOSE", state="live",
               price=REF_PX * 0.985, time=t("17:53:00"),
               t_oes_send=t("17:53:00"), t_gen=t("17:52:59"),
               t_on_market=t("17:53:01"))
        if close > 0:       # traded in the auction
            # 101 goes in as a market order during the limit-and-market phase,
            # so both otype kinds show up in the mix tables.
            wo(id_target=tid, sym=sym, side=side, size=close,
               venue="NSE_CLOSE", venuetype="CLOSE", state="filled",
               otype="market" if tid == 101 else "limit",
               price=CLOSE_FILL_PX, time=t("18:00:30"), t_oes_send=t("17:51:00"),
               t_gen=t("17:50:59"), t_on_market=t("17:51:01"))

    # a rejection during continuous trading, so the split has both sides
    wo(id_target=103, sym="INFY.IN", side="buy", size=1000,
       state="rejected:risk limit breached", time=t("14:22:00"),
       t_oes_send=t("14:22:00"), t_gen=t("14:21:59"), t_on_market=pd.NaT)
    wo(id_target=109, sym="AXISBANK.IN", side="sell", size=800,
       state="cxl:price moved away", time=t("15:10:00"),
       t_oes_send=t("15:09:00"), t_gen=t("15:08:59"))

    return pd.DataFrame(rows)


def build_executions(wo: pd.DataFrame) -> pd.DataFrame:
    """One execution report per filled child order."""
    rows = []
    filled = wo[wo["state"] == "filled"]
    for _, w in filled.iterrows():
        is_close = "CLOSE" in str(w["venue"]).upper()
        px = CLOSE_FILL_PX if is_close else REF_PX * 0.999
        # 109's continuous fill reports at 18:01, inside the auction window, on a
        # continuous venue.  Only the venue may decide, so it has to stay
        # continuous and 109 has to stay ALGO_NEVER_COMMITTED_TO_CLOSE.
        late_continuous = not is_close and int(w["id_target"]) == 109
        rows.append({
            "date": DATE,
            "time": t("18:00:45") if is_close else (
                t("18:01:00") if late_continuous else w["time"]),
            "id_target": w["id_target"], "id_candidate": 0, "id_work": w["id_work"],
            "trader": "JDOE", "oes_oid": w["oes_oid"], "oes_primoid": w["oes_primoid"],
            "wave": "W1", "sym": w["sym"], "side": w["side"],
            "sidesign": 1 if w["side"] == "buy" else -1,
            "size": w["size"], "tif": "DAY", "otype": w["otype"], "price": px,
            "t_algo": w["time"], "t_oes_send": w["t_oes_send"],
            "t_oes_real": w["time"], "t_oes_xact": w["time"],
            "destination": "NSE", "exec_broker": "BSG",
            "last_mkt": "XNSE_CLOSE" if is_close else "XNSE",
            "ostat": "filled", "fillprice": px, "fillsize": w["size"],
            "cum_apr": 0.0, "cum_qty": w["size"], "comment": "",
            "target_strike": REF_PX * 0.998, "target_vwap": REF_PX,
            "bidprice": px - 0.05, "askprice": px + 0.05, "lastprice": px,
            "adv1t": 5_000_000,
        })

    # a reject echoed on the execution tape during the auction
    rows.append({
        "date": DATE, "time": t("17:52:01"), "id_target": 102, "id_candidate": 0,
        "id_work": int(wo[wo["state"].str.startswith("rejected", na=False)]["id_work"].iloc[0]),
        "trader": "JDOE", "oes_oid": "X1", "oes_primoid": "X1", "wave": "W1",
        "sym": "TCS.IN", "side": "sell", "sidesign": -1, "size": 4000,
        "tif": "DAY", "otype": "limit", "price": REF_PX * 1.08,
        "t_algo": t("17:52:00"), "t_oes_send": t("17:52:00"),
        "t_oes_real": t("17:52:01"), "t_oes_xact": t("17:52:01"),
        "destination": "NSE", "exec_broker": "BSG", "last_mkt": "XNSE_CLOSE",
        "ostat": "rejected", "fillprice": np.nan, "fillsize": 0,
        "cum_apr": 0.0, "cum_qty": 0,
        "comment": "58=Order price outside the CAS +/-3% band",
        "target_strike": REF_PX, "target_vwap": REF_PX,
        "bidprice": np.nan, "askprice": np.nan, "lastprice": REF_PX,
        "adv1t": 5_000_000,
    })
    return pd.DataFrame(rows)


def build_alerts() -> pd.DataFrame:
    return pd.DataFrame([{
        "date": DATE, "time": t("17:56:00"), "trader": "JDOE",
        "id_target": 109, "sym": "AXISBANK.IN", "side": "sell",
        "limit": 0.0, "size": 2000, "alertmode": 1,
        "triggertime": t("17:56:00"), "ntrigger": 1,
        "alerttype": "CLOSE_NOT_COMMITTED",
        "alertstr": "residual left with no close commitment",
        "istate": 1, "dstate": 0.0, "sstate": "open",
    }])


def build_market() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Synthetic qatt aggregates: session profile, CTS window stats, last trade."""
    syms = [s for _, s, *_ in SCENARIOS]
    bucket_qty = {
        "CTS_EARLY": 800_000, "CTS_FINAL15": 90_000, "CAS_REFCALC": 0,
        "CAS_ENTRY_LM": 0, "CAS_ENTRY_LO": 0, "CAS_MATCH": 60_000,
        "CAS_BUFFER": 0, "POST_CLOSE": 5_000, "AFTER_HOURS": 0,
    }
    bucket_px = {
        "CTS_EARLY": 99.5, "CTS_FINAL15": 99.9, "CAS_REFCALC": np.nan,
        "CAS_ENTRY_LM": np.nan, "CAS_ENTRY_LO": np.nan, "CAS_MATCH": 100.4,
        "CAS_BUFFER": np.nan, "POST_CLOSE": 100.4, "AFTER_HOURS": np.nan,
    }
    rows = []
    for sym in syms:
        for bucket, qty in bucket_qty.items():
            if qty <= 0:
                continue
            px = bucket_px[bucket]
            rows.append({
                "sym": sym, "bucket": bucket, "n": qty // 100, "qty": qty,
                "notional": qty * px, "pxFirst": px, "pxLast": px,
                "tFirst": t("10:00:00"), "tLast": t("10:00:00"),
            })
    profile = pd.DataFrame(rows)

    cts = pd.DataFrame({
        "sym": syms,
        "cts_vwap": REF_PX,
        "cts_qty": 90_000,
        "cts_n": 900,
        "cts_pxFirst": 99.8,
        "cts_pxLast": 99.9,
    })
    last_trade = pd.DataFrame({
        "sym": syms, "pxLastTrade": 99.9, "tLastTrade": t("17:44:59"),
        "qtyBefore": 890_000,
    })
    day_vol = pd.DataFrame({
        "sym": syms, "dayQty": 955_000, "dayNotional": 955_000 * 99.8,
        "dayVwap": 99.8, "totalVolume": 955_000,
    })
    return profile, cts, last_trade, day_vol


# --------------------------------------------------------------------------- #

def check_venue_only() -> list[str]:
    """Regression: the clock must never make something count as the close.

    A parent whose child orders all sat on a continuous venue can still have
    them refused, cancelled and filled *inside* the CAS time window -- the algo
    is working the continuous book right up to 17:45, and reports arrive later
    still.  Classifying any of that as "the close" credited auction activity to
    parents that never had a close-venue child order at all.
    """
    from casretro import classify as CL

    wo = pd.DataFrame([
        # continuous venue, but every timestamp lands inside the CAS window
        {"id_work": 9001, "id_target": 900, "sym": "X.IN", "side": "buy",
         "size": 100, "otype": "limit", "price": 10.0, "venue": "NSE",
         "venuetype": "CLOSE_ELIGIBLE", "state": "rejected:risk",
         "time": t("17:52:00"), "t_oes_send": t("17:51:59")},
        {"id_work": 9002, "id_target": 900, "sym": "X.IN", "side": "buy",
         "size": 200, "otype": "limit", "price": 10.0, "venue": "NSE",
         "venuetype": "LIT", "state": "cxl:too late",
         "time": t("18:01:00"), "t_oes_send": t("17:59:00")},
        {"id_work": 9003, "id_target": 900, "sym": "X.IN", "side": "buy",
         "size": 300, "otype": "limit", "price": 10.0, "venue": "NSE",
         "venuetype": "LIT", "state": "filled",
         "time": t("18:02:00"), "t_oes_send": t("17:44:00")},
    ])
    ex = pd.DataFrame([
        {"id_work": 9003, "id_target": 900, "sym": "X.IN", "side": "buy",
         "time": t("18:02:30"), "fillsize": 300, "fillprice": 10.0,
         "ostat": "filled", "size": 300, "otype": "limit"},
        # an execution we cannot trace to any child order
        {"id_work": 9999, "id_target": 900, "sym": "X.IN", "side": "buy",
         "time": t("18:03:00"), "fillsize": 50, "fillprice": 10.0,
         "ostat": "filled", "size": 50, "otype": "limit"},
    ])

    ewo = CL.enrich_workorders(wo)
    eex = CL.enrich_executions(ex, ewo)

    out = []
    if ewo["is_close"].any():
        out.append("  venue-only: a non-CLOSE venue was classified as close")
    if set(ewo["phase"]) != {"CONTINUOUS"}:
        out.append(f"  venue-only: workorder phase leaked the clock -> "
                   f"{sorted(set(ewo['phase']))}")
    if eex["is_close"].any():
        out.append("  venue-only: an execution on a continuous venue was "
                   "classified as close")
    if set(eex["phase"]) != {"CONTINUOUS"}:
        out.append(f"  venue-only: execution phase leaked the clock -> "
                   f"{sorted(set(eex['phase']))}")
    # venuetype reading CLOSE must not be enough on its own
    if bool(ewo.loc[ewo["id_work"] == 9001, "is_close"].iloc[0]):
        out.append("  venue-only: venuetype=CLOSE_ELIGIBLE was treated as close")
    # the untraceable fill must be visible, not silently continuous
    if eex["traced_to_workorder"].all():
        out.append("  venue-only: an untraceable execution was not flagged")
    if not CL.summarise_close_workorders(ewo).empty:
        out.append("  venue-only: a close-workorder summary was produced with no "
                   "close-venue child order")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", help="write the report here (default: a temp dir)")
    ap.add_argument("--keep", action="store_true", help="do not delete the temp dir")
    args = ap.parse_args()

    uni = build_universe()
    targets = build_targets()
    states = build_states()
    wo = build_workorders()
    ex = build_executions(wo)
    alerts = build_alerts()
    profile, cts, last_trade, day_vol = build_market()

    profile_wide = M.pivot_market_profile(profile)
    ref_px = M.reference_price(uni, cts, last_trade)
    sym_market = M.market_volume_shares(profile_wide, day_vol)

    raw = RawFrames(
        universe=uni, targets=targets, states=states, workorders=wo,
        executions=ex, alerts=alerts, profile_wide=profile_wide,
        sym_market=sym_market, ref_px=ref_px,
    )

    note = ["synthetic data -- selftest only"]
    # What a real run produces: only the orders that executed something.
    data = assemble(DATE, "ht", "both", raw, note)
    # The same book with the filter off, so every waterfall branch is still
    # reachable -- several of them describe orders that never traded.
    data_all = assemble(DATE, "ht", "both", raw, note, drop_unfilled=False)
    R.print_console(data)

    outdir = args.out or tempfile.mkdtemp(prefix="cas_selftest_")
    R.write_csvs(data, os.path.join(outdir, "csv"))
    R.write_excel(data, os.path.join(outdir, "cas_selftest.xlsx"))
    R.write_html(data, os.path.join(outdir, "cas_selftest.html"))
    print(f"  selftest output -> {outdir}\n")

    # -- assertions --------------------------------------------------------- #
    failures = []

    # 1. the waterfall, against the unfiltered book
    got = data_all.orders.set_index("id_target")[["participation", "reason_code"]]
    for tid, _sym, _b, _side, _sg, _size, _dc, _lim, exp_part, exp_reason in SCENARIOS:
        if tid not in got.index:
            failures.append(f"  id_target {tid}: missing from the unfiltered report")
            continue
        row = got.loc[tid]
        if row["participation"] != exp_part:
            failures.append(
                f"  id_target {tid}: participation {row['participation']!r}, expected {exp_part!r}"
            )
        if exp_reason and row["reason_code"] != exp_reason:
            failures.append(
                f"  id_target {tid}: reason {row['reason_code']!r}, expected {exp_reason!r}"
            )

    # 2. the filter: everything that traded survives, everything else is gone
    kept = set(data.orders["id_target"])
    expected_kept = {tid for tid, *_ in SCENARIOS} - EXPECT_DROPPED
    if kept != expected_kept:
        for tid in sorted(EXPECT_DROPPED & kept):
            failures.append(f"  id_target {tid}: executed nothing but is still in the report")
        for tid in sorted(expected_kept - kept):
            failures.append(f"  id_target {tid}: executed something but was dropped")
    if not data.orders.empty and float(data.orders["exec_qty"].min()) <= 0:
        failures.append("  a surviving parent order has exec_qty <= 0")

    rej = data.rejections
    n_cont = int((rej["phase"] == "CONTINUOUS").sum()) if not rej.empty else 0
    n_close = int((rej["phase"] == "CLOSE").sum()) if not rej.empty else 0
    if n_cont < 1:
        failures.append("  no continuous-phase rejection was produced")
    if n_close < 1:
        failures.append("  no close-phase rejection was produced")

    cxl = data.cancellations
    if cxl.empty or "price moved away" not in set(cxl["reason"]):
        failures.append("  the cxl:<reason> decoding did not produce 'price moved away'")

    for name, keys in (("mix_otype_basket", ["otype_kind", "basket"]),
                       ("mix_flow_venue_otype", ["flow", "venue", "otype_kind"])):
        mix = getattr(data, name)
        if mix.empty:
            failures.append(f"  {name} came back empty")
            continue
        body = mix[mix[keys[0]] != "TOTAL"]
        if not {"MARKET", "LIMIT"} <= set(body["otype_kind"]):
            failures.append(f"  {name} does not split market vs limit: "
                            f"{sorted(set(body['otype_kind']))}")
        if abs(body["size"].sum() - wo["size"].sum()) > 1e-6:
            failures.append(f"  {name} size {body['size'].sum():,.0f} != "
                            f"child-order size {wo['size'].sum():,.0f}")
        filled = ex[ex["fillsize"] > 0]["fillsize"].sum()
        if abs(body["make"].sum() - filled) > 1e-6:
            failures.append(f"  {name} make {body['make'].sum():,.0f} != "
                            f"executed {filled:,.0f}")
        rate = body["make"].sum() / body["size"].sum() * 100.0
        tot = mix[mix[keys[0]] == "TOTAL"]
        if not tot.empty and abs(float(tot["fill_rate_pct"].iloc[0]) - rate) > 1e-6:
            failures.append(f"  {name} TOTAL fill_rate_pct is not make / size")

    if EXPECT_DROPPED and not any("nothing executed at all" in w for w in data.warnings):
        failures.append("  the unfilled-order exclusion was not reported as a warning")

    failures += check_venue_only()

    for _, r in data.reconciliation.iterrows():
        if r["status"] != "OK":
            failures.append(f"  reconciliation: {r['check']} -- {r['detail']}")

    if failures:
        print("SELFTEST FAILED")
        print("\n".join(failures))
        return 1

    print("SELFTEST OK -- every waterfall branch fired and the writers ran clean")
    print(f"  rejections: {n_cont} continuous / {n_close} close")
    print(f"  reasons   : {sorted(set(data.orders['reason_code']) - {''})}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

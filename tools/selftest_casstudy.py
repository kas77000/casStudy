#!/usr/bin/env python3
"""Offline check of casStudy's analytical layer -- no kdb, no pykx.

Builds a synthetic tape result whose columns match what the q query returns,
pushes it through `build_syms` / `aggregate` / `reconcile_day` / `print_report`,
and asserts the arithmetic that everything else rests on:

  * contributions sum, exactly, to the weighted index effect
  * the effect is measured against the basis the caller asked for, and the
    17:30-18:00 basis is demonstrably contaminated by the auction print
  * ticks resolve off the right NSE band
  * the control adjustment subtracts, and is withheld when the control arm is
    too thin to mean anything
  * the whole-day reconciliation reproduces a known index return
  * a window with no prints at all does not crash the run

    python tools/selftest_casstudy.py

Exit code 0 means the analytical layer is sound; run it before and after any
change to the study.
"""

from __future__ import annotations

import datetime as dt
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import casStudy as S                                   # noqa: E402

DATE = dt.date(2026, 8, 4)
PRINT_TIME = pd.Timedelta("17:59:12.348")

#: sym, isin, cas eligible, weight, old-rule vwap, 17:30-18:00 vwap, auction print
#: The NIFTY names carry real ISINs so the weights file matches on them.
NIFTY_ISINS = ["INE002A01018", "INE397D01024", "INE040A01034"]   # RELIANCE, BHARTIARTL, HDFCBANK


def build_raw() -> tuple[pd.DataFrame, pd.DataFrame]:
    rows = [
        # sym,          isin,             old,     win,     close,  prev,   dayqty
        ("RELIANCE.IN",  NIFTY_ISINS[0], 1450.00, 1451.50, 1455.00, 1440.0, 5e6),
        ("BHARTIARTL.IN", NIFTY_ISINS[1], 1900.00, 1899.00, 1897.00, 1890.0, 4e6),
        ("HDFCBANK.IN",  NIFTY_ISINS[2], 2000.00, 2003.00, 2010.00, 1995.0, 3e6),
        ("SMALLCAS.IN",  "INE999X01011",  300.00,  300.40,  299.00,  298.0, 1e6),
    ]
    # 25 control names, so the arm clears MIN_CONTROL_NAMES, all drifting +20 bps
    for i in range(25):
        px = 100.0 + i
        rows.append((f"NONCAS{i:02d}.IN", f"INE{i:03d}Y01010", px, px * 1.0005,
                     px * 1.002, px * 0.99, 2e6))

    uni = pd.DataFrame({
        "sym": [r[0] for r in rows],
        "isin": [r[1] for r in rows],
        "px_prev_close": [r[5] for r in rows],
    })
    n = len(rows)
    raw = pd.DataFrame({
        "sym": [r[0] for r in rows],
        "px_old_rule_last30_continuous": [r[2] for r in rows],
        "vol_last30_continuous": [1e6] * n,
        "n_trades_last30_continuous": [500] * n,
        "px_old_rule_clock_1730_1800": [r[3] for r in rows],
        "vol_clock_1730_1800": [1.1e6] * n,
        "n_trades_clock_1730_1800": [520] * n,
        "px_cas_reference": [r[2] * 1.0001 for r in rows],
        "vol_ref_window": [5e5] * n,
        "n_trades_ref_window": [300] * n,
        "px_last_continuous": [r[2] * 1.0002 for r in rows],
        "time_last_continuous": [pd.Timedelta("17:44:59")] * n,
        "n_trades_continuous": [900] * n,
        "px_auction_close": [r[4] for r in rows],
        "time_auction_print": [PRINT_TIME] * n,
        "vol_close_window": [5e4] * n,
        "n_trades_close_window": [3] * n,
        "vol_after_continuous": [2e5] * n,
        "n_trades_after_continuous": [40] * n,
        "vol_day": [r[6] for r in rows],
        "vwap_day": [r[2] for r in rows],
    })
    return raw, uni


def main() -> int:
    failures: list[str] = []

    def check(ok: bool, msg: str) -> None:
        if not ok:
            failures.append(f"  {msg}")

    weights = S.load_weights(S.WEIGHTS_FILE)
    if weights is None:
        print(f"[fatal] {S.WEIGHTS_FILE} is missing -- the study needs it")
        return 2
    cas_isins = set(NIFTY_ISINS) | {"INE999X01011"}

    raw, uni = build_raw()
    syms = S.build_syms(raw, uni, weights, DATE, cas_isins)
    agg = S.aggregate(syms, DATE, 24_800.0)
    row = agg.set_index("group")

    # -- 1. attribution is an identity, not an approximation ------------------ #
    contrib = pd.to_numeric(syms["contribution_bps"], errors="coerce").dropna().sum()
    weighted = float(row.loc[S.GROUP_NIFTY, "effect_bps_index_weighted"])
    check(abs(contrib - weighted) < 1e-9,
          f"contributions sum to {contrib:.6f} bps, weighted effect is {weighted:.6f}")

    # -- 2. the basis is the one that was asked for --------------------------- #
    r = syms.set_index("sym")
    check(abs(r.loc["RELIANCE.IN", "effect_bps"]
              - (1455.0 / 1450.0 - 1) * 1e4) < 1e-9,
          "effect_bps is not measured against the last30-continuous vwap")
    other = S.build_syms(raw, uni, weights, DATE, cas_isins, "clock-1730-1800")
    contaminated = float(other.set_index("sym").loc["RELIANCE.IN", "effect_bps"])
    check(abs(contaminated) < abs(r.loc["RELIANCE.IN", "effect_bps"]),
          "the 17:30-18:00 basis should read smaller -- it contains the print")

    # -- 3. ticks come off the right band ------------------------------------- #
    check(float(r.loc["RELIANCE.IN", "tick_size"]) == 0.10,
          "a 1450-rupee stock should tick at 0.10")
    check(float(r.loc["NONCAS00.IN", "tick_size"]) == 0.01,
          "a 100-rupee stock should tick at 0.01")
    check(abs(float(r.loc["RELIANCE.IN", "effect_ticks"]) - 50.0) < 1e-9,
          "a 5.00 move at a 0.10 tick is 50 ticks")

    # -- 4. the control subtracts, and only when it is real ------------------- #
    ctrl = float(row.loc[S.GROUP_NONCAS_MATCHED, "effect_bps_mean"])
    net = float(row.loc[S.GROUP_NIFTY, "effect_bps_net_of_control_drift"])
    check(abs(net - (weighted - ctrl)) < 1e-9,
          "net of control is not the effect minus the control drift")

    thin_uni = uni.head(6).copy()
    thin = S.build_syms(raw[raw["sym"].isin(thin_uni["sym"])], thin_uni,
                        weights, DATE, cas_isins)
    thin_agg = S.aggregate(thin, DATE, None).set_index("group")
    check(pd.isna(thin_agg.loc[S.GROUP_NIFTY, "effect_bps_net_of_control_drift"]),
          f"a control arm of 2 names must not produce a drift adjustment "
          f"(MIN_CONTROL_NAMES={S.MIN_CONTROL_NAMES})")

    # -- 5. the whole-day reconciliation reproduces a known return ------------ #
    recon = S.reconcile_day(syms, None, None)
    w = weights.set_index(weights["isin"].str.upper())["weight_pct"]
    expect = sum(
        w[i] * ((c / p) - 1) * 1e4
        for i, c, p in ((NIFTY_ISINS[0], 1455.0, 1440.0),
                        (NIFTY_ISINS[1], 1897.0, 1890.0),
                        (NIFTY_ISINS[2], 2010.0, 1995.0))
    ) / sum(w[i] for i in NIFTY_ISINS)
    check(recon["available"] and abs(recon["reconstructed_bps"] - expect) < 1e-6,
          f"reconstructed day return {recon.get('reconstructed_bps')} != {expect}")
    gapped = S.reconcile_day(syms, 24_800.0, 24_700.0)
    check(abs(gapped["official_bps"] - (24_800.0 / 24_700.0 - 1) * 1e4) < 1e-6,
          "official return is not computed from the two index levels")

    # -- 6. an empty window must not crash ------------------------------------ #
    blank = raw.copy()
    for c in ("px_auction_close", "time_auction_print", "vol_close_window"):
        blank[c] = np.nan if c != "time_auction_print" else pd.NaT
    blank["n_trades_close_window"] = 0
    empty = S.build_syms(blank, uni, weights, DATE, cas_isins)
    check(bool((empty["status"] == "no_close_price").all()),
          "a day with no auction print should read no_close_price throughout")
    S.aggregate(empty, DATE, None)          # must not raise

    dropped = raw.drop(columns=["vol_close_window"])
    S.build_syms(dropped, uni, weights, DATE, cas_isins)   # must not raise

    # -- the report itself has to render -------------------------------------- #
    S.print_report(syms, agg, DATE, 24_800.0, S.reconcile_day(syms, 24_800.0, 24_700.0))

    if failures:
        print("SELFTEST FAILED")
        print("\n".join(failures))
        return 1
    print("SELFTEST OK -- attribution, basis, ticks, control, reconciliation and "
          "the empty-window paths all hold")
    return 0


if __name__ == "__main__":
    sys.exit(main())

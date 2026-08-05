#!/usr/bin/env python3
"""How does the closing auction (CAS) impact the NIFTY 50?

The narrative, the reasoning behind each step and how to read the output live in
`docs/cas_study_method.md`.  In brief:

  S1  universe      every Indian listing, split into CAS-eligible and not.
                    The non-eligible names are the control arm, not an oversight.
  S2  prices        one round of queries per sym: the old-rule close proxy, the
                    CAS reference price, the last continuous print, the auction
                    print, and the day's volume.
  S3  counterfactual  effectBps = auction print vs the close the *old* rule would
                    have produced for that same stock, same day.  Within-name, so
                    no size or liquidity confound.
  S4  attribution   weight it by index weight: contribBps sums exactly to the
                    index effect, and ranks the names responsible for it.
  S5  control       the same clock window measured on names that have no auction
                    tells you how much of S3 is simply 15 minutes of market
                    drift.  What is left is the auction.

Everything is HKT -- the raw `time` column of the qatt table.  IST = HKT - 02:30.

The heavy lifting that is already correct in `cas_price_move.py` (ISIN parsing,
tick bands, weights, the kdb connection) is imported rather than copied, so the
two scripts cannot drift apart.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sys

import numpy as np
import pandas as pd

from cas_price_move import (            # noqa: E402  -- shared, deliberately
    EQUITY_TABLE,
    KDB_HOST,
    KDB_PORT,
    QATT_TABLE,
    SYM_CHUNK,
    SYM_SUFFIXES,
    TYP_FILTER,
    WEIGHTS_FILE,
    _sym_vector,
    connect,
    load_isins,
    load_weights,
    resolve_date,
    tick_size,
)

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
ISIN_FILE = os.path.join(PROJECT_DIR, "config", "cas_isins.txt")
OUTPUT_DIR = os.path.join(PROJECT_DIR, "output")
PANEL_FILE = os.path.join(OUTPUT_DIR, "casstudy_panel.csv")


# --------------------------------------------------------------------------- #
# Windows (HKT).  Milliseconds are not decoration: `17:45:00` is a second atom  #
# in q and would not compare cleanly against a millisecond `time` column.       #
# --------------------------------------------------------------------------- #

#: End of continuous trading for a CAS name -- 15:15 IST.
CTS_END = "17:45:00.000"

#: The old closing rule: the close is the VWAP of the **last 30 minutes of
#: continuous trading**.  That rule resolves to two different clock windows.
#:
#: Post-CAS the session ends at 17:45, so applying the rule to today's session
#: gives 17:15-17:45.  This is the counterfactual: what today's trading would
#: have produced under the rule CAS replaced.
OLD_RULE_START = "17:15:00.000"
OLD_RULE_END = CTS_END

#: The window the rule occupied under the old regime, when continuous ran to
#: 18:00: 15:00-15:30 IST.  Carried for two reasons.  For a **control** name it
#: is the actual official close, so it measures how much of any effect is simply
#: the 15-minute shift between two adjacent windows rather than the auction.  For
#: a **CAS** name it is unusable as a counterfactual -- the auction print at
#: ~17:59 falls inside it, so it would contain the very thing being measured.
OLD_WINDOW_START = "17:30:00.000"
OLD_WINDOW_END = "18:00:00.000"

#: Which price `effectBps` is measured against.  The default is the faithful
#: reading of the rule; the alternative is there to be argued with.
OLD_RULE_CHOICES = {
    "last30-continuous": "pxOldRule",     # 17:15-17:45, the rule applied today
    "clock-1730-1800": "pxOldRuleWin",    # 15:00-15:30 IST, the old clock window
}

#: The exchange's CAS reference price: 15:00-15:15 IST.  Half-open.
REF_START = "17:30:00.000"
REF_END = CTS_END

#: The auction print.  Order entry stops at a random instant in this window and
#: the close is struck there, so the print time is exogenous -- nobody can time
#: it.  Kept as the desk runs it rather than the deck's 18:00-18:05 matching slot.
CLOSE_START = "17:58:00.000"
CLOSE_END = "18:00:00.000"

#: Everything after continuous ends, for the auction-share numbers.
POST_FROM = CTS_END

GROUP_NIFTY = "NIFTY50"
GROUP_CAS_OTHER = "CAS_OTHER"
GROUP_NONCAS = "NONCAS"
GROUP_NONCAS_MATCHED = "NONCAS_MATCHED"

#: A move smaller than this is not a move -- it is the grid.
MIN_TICKS = 1.0


# --------------------------------------------------------------------------- #
# S1 -- universe                                                               #
# --------------------------------------------------------------------------- #

_UNIVERSE_Q = """
{{[d] select distinct sym, ID_ISIN from {tbl} where date=d, {like} }}
"""


def fetch_universe(conn, date: dt.date) -> pd.DataFrame:
    """Every Indian listing on the date, with its ISIN.

    Both arms come out of one query: the CAS whitelist splits them afterwards.
    Carrying the ISIN here is what lets the index weights attach directly,
    without the Bloomberg / NSE symbol-mapping detour `cas_price_move` needs.
    """
    like = " | ".join(f'(sym like "{p}")' for p in SYM_SUFFIXES)
    qry = _UNIVERSE_Q.format(tbl=EQUITY_TABLE, like=like)
    df = conn(qry, date).pd()
    df.columns = [str(c) for c in df.columns]
    df = df.rename(columns={"ID_ISIN": "isin"})
    df["sym"] = df["sym"].astype(str)
    df["isin"] = df["isin"].astype(str).str.strip().str.upper()
    return df.drop_duplicates("sym").reset_index(drop=True)


# --------------------------------------------------------------------------- #
# S2 -- prices                                                                 #
# --------------------------------------------------------------------------- #

# One statement per window, joined left-folded explicitly.  Written as
# `base lj a lj b` q reads it right to left and a sym present in b but not in a
# loses its columns.
_PRICES_Q = """
{{[d;syms]
  old: select pxOldRule: size wavg price, volOld: sum size, nOld: count i
       by sym from {tbl}
       where date=d, sym in syms, time >= {o1}, time < {o2},
             not null price, not null size{typ};
  oldw: select pxOldRuleWin: size wavg price, volOldWin: sum size, nOldWin: count i
       by sym from {tbl}
       where date=d, sym in syms, time >= {w1}, time < {w2},
             not null price, not null size{typ};
  ref: select pxRef: size wavg price, volRef: sum size, nRef: count i
       by sym from {tbl}
       where date=d, sym in syms, time >= {r1}, time < {r2},
             not null price, not null size{typ};
  pre: select pxPre: last price, tPre: last time, nPre: count i
       by sym from {tbl}
       where date=d, sym in syms, time < {c1}, not null price{typ};
  cls: select pxClose: first price, tClose: first time, nClose: count i
       by sym from {tbl}
       where date=d, sym in syms, time within ({k1};{k2}), not null price{typ};
  pst: select volPost: sum size, nPost: count i
       by sym from {tbl}
       where date=d, sym in syms, time >= {p1},
             not null price, not null size{typ};
  day: select dayQty: sum size, dayVwap: size wavg price
       by sym from {tbl}
       where date=d, sym in syms, not null price, not null size{typ};
  r: `sym xkey ([] sym:syms);
  r: r lj old;
  r: r lj oldw;
  r: r lj ref;
  r: r lj pre;
  r: r lj cls;
  r: r lj pst;
  r: r lj day;
  0!r }}
"""


def prices_query() -> str:
    """The q text with every window substituted -- handy for review."""
    return _PRICES_Q.format(
        tbl=QATT_TABLE,
        o1=OLD_RULE_START, o2=OLD_RULE_END,
        w1=OLD_WINDOW_START, w2=OLD_WINDOW_END,
        r1=REF_START, r2=REF_END,
        c1=CTS_END,
        k1=CLOSE_START, k2=CLOSE_END,
        p1=POST_FROM,
        typ=(", typ in `" + "`".join(TYP_FILTER)) if TYP_FILTER else "",
    )


def fetch_prices(conn, date: dt.date, syms: list[str]) -> pd.DataFrame:
    qry = prices_query()
    frames = []
    for i in range(0, len(syms), SYM_CHUNK):
        frames.append(conn(qry, date, _sym_vector(syms[i:i + SYM_CHUNK])).pd())
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


# --------------------------------------------------------------------------- #
# S3 / S4 -- the counterfactual and its attribution                            #
# --------------------------------------------------------------------------- #

def build_syms(
    raw: pd.DataFrame,
    universe: pd.DataFrame,
    weights: pd.DataFrame | None,
    date: dt.date,
    cas_isins: set[str],
    old_rule: str = "last30-continuous",
) -> pd.DataFrame:
    """One row per sym: the counterfactual effect and its index contribution."""
    df = raw.copy()
    df["sym"] = df["sym"].astype(str)
    df = df.merge(universe, on="sym", how="left")
    df.insert(0, "date", date)

    for col in ("nOld", "nOldWin", "nRef", "nPre", "nClose", "nPost"):
        df[col] = pd.to_numeric(df.get(col), errors="coerce").fillna(0).astype("int64")
    for col in ("volOld", "volOldWin", "volRef", "volPost", "dayQty"):
        df[col] = pd.to_numeric(df.get(col), errors="coerce").fillna(0.0)

    df["cas_eligible"] = df["isin"].isin(cas_isins)

    # -- index membership and weight, straight off the ISIN ------------------ #
    df["weight_pct"] = np.nan
    df["name"] = pd.NA
    if weights is not None and not weights.empty:
        by_isin = weights.set_index(weights["isin"].str.upper())
        df["weight_pct"] = df["isin"].map(by_isin["weight_pct"])
        df["name"] = df["isin"].map(by_isin["name"])
        df["in_nifty50"] = df["isin"].isin(set(by_isin.index))
    else:
        df["in_nifty50"] = False

    df["group"] = np.where(
        df["in_nifty50"], GROUP_NIFTY,
        np.where(df["cas_eligible"], GROUP_CAS_OTHER, GROUP_NONCAS),
    )

    # -- S3: the counterfactual --------------------------------------------- #
    # Same stock, same day, same information.  The only difference between the
    # two prices is the mechanism that produced them.
    base_col = OLD_RULE_CHOICES[old_rule]
    base = df[base_col]
    df["old_rule_basis"] = old_rule
    df["effect"] = df["pxClose"] - base
    df["tickSize"] = base.where(base.notna(), df["pxClose"]).map(tick_size)
    df["effectTicks"] = (df["effect"] / df["tickSize"]).round(2)
    df["effectBps"] = (df["pxClose"] / base - 1.0) * 10_000.0

    # The 15-minute shift between the two readings of "the last 30 minutes".
    # On a control name -- no auction in either window -- this is pure window
    # artefact, which is what makes it the yardstick for how much of the treated
    # effect could be the same artefact rather than the auction.
    df["windowShiftBps"] = (df["pxOldRuleWin"] / df["pxOldRule"] - 1.0) * 10_000.0

    # Secondary readings, kept because they disagree in informative ways: a gap
    # between moveBps and effectBps is stale-last-print bias, and closeVsRefBps
    # is what the exchange's own band is measured against.
    df["moveBps"] = (df["pxClose"] / df["pxPre"] - 1.0) * 10_000.0
    df["closeVsRefBps"] = (df["pxClose"] / df["pxRef"] - 1.0) * 10_000.0

    df["postShare"] = np.where(
        df["dayQty"] > 0, df["volPost"] / df["dayQty"] * 100.0, np.nan)

    df["status"] = [
        "ok" if (o and c) else
        "no_old_rule_price" if c else
        "no_close_price" if o else
        "no_data"
        for o, c in zip(base.notna(), df["pxClose"].notna())
    ]

    # -- S4: attribution ----------------------------------------------------- #
    # contribBps sums to the index effect by construction, so a name's number is
    # its share of the answer -- not a big move in a name nobody weights.
    w = pd.to_numeric(df["weight_pct"], errors="coerce")
    usable = w.notna() & df["effectBps"].notna()
    total_w = float(w[usable].sum())
    df["contribBps"] = np.where(
        usable & (total_w > 0), w / total_w * df["effectBps"], np.nan)
    gross = float(np.nansum(np.abs(df["contribBps"]))) if total_w else 0.0
    df["contribShare"] = (df["contribBps"].abs() / gross * 100.0) if gross else np.nan

    cols = [
        "date", "sym", "isin", "name", "group", "cas_eligible", "in_nifty50",
        "weight_pct", "status", "old_rule_basis",
        "pxOldRule", "volOld", "nOld",
        "pxOldRuleWin", "volOldWin", "nOldWin", "windowShiftBps",
        "pxRef", "volRef", "nRef",
        "pxPre", "tPre", "nPre",
        "pxClose", "tClose", "nClose",
        "dayQty", "volPost", "postShare",
        "tickSize", "effect", "effectTicks", "effectBps",
        "moveBps", "closeVsRefBps",
        "contribBps", "contribShare",
    ]
    for c in cols:
        if c not in df.columns:
            df[c] = pd.NA
    return df[cols].sort_values(
        ["group", "sym"], ignore_index=True)


# --------------------------------------------------------------------------- #
# S5 -- aggregates, per group                                                  #
# --------------------------------------------------------------------------- #

def _stats(df: pd.DataFrame, label: str, date: dt.date) -> dict:
    ok = df[df["status"] == "ok"]
    eff = pd.to_numeric(ok["effectBps"], errors="coerce").dropna()
    ticks = pd.to_numeric(ok["effectTicks"], errors="coerce").dropna()
    w = pd.to_numeric(ok["weight_pct"], errors="coerce")

    weighted = np.nan
    covered = np.nan
    m = w.notna() & ok["effectBps"].notna()
    if m.any() and w[m].sum():
        weighted = float((ok.loc[m, "effectBps"] * w[m]).sum() / w[m].sum())
        covered = float(w[m].sum())

    return {
        "date": date,
        "group": label,
        "n_syms": len(df),
        "n_ok": len(ok),
        "weight_covered_pct": covered,
        "eff_bps_weighted": weighted,
        "eff_bps_mean": float(eff.mean()) if len(eff) else np.nan,
        "eff_bps_median": float(eff.median()) if len(eff) else np.nan,
        "eff_bps_std": float(eff.std()) if len(eff) > 1 else np.nan,
        "eff_abs_bps_mean": float(eff.abs().mean()) if len(eff) else np.nan,
        "eff_abs_ticks_mean": float(ticks.abs().mean()) if len(ticks) else np.nan,
        "pct_moved_ge_1_tick": (
            float((ticks.abs() >= MIN_TICKS).mean() * 100.0) if len(ticks) else np.nan),
        "post_share_mean": float(
            pd.to_numeric(ok["postShare"], errors="coerce").mean()) if len(ok) else np.nan,
        "window_shift_bps_mean": float(
            pd.to_numeric(ok["windowShiftBps"], errors="coerce").mean()) if len(ok) else np.nan,
        "up": int((eff > 0).sum()),
        "down": int((eff < 0).sum()),
        "flat": int((eff == 0).sum()),
    }


def matched_control(syms: pd.DataFrame) -> pd.DataFrame:
    """Non-CAS names whose day volume sits inside the CAS group's p10-p90.

    A raw CAS vs non-CAS comparison confounds the mechanism with size: the
    eligible names are the liquid ones.  Restricting the control to the overlap
    of the two volume distributions is the cheap version of matching, and it is
    honest about what it does -- names outside the common support are dropped,
    not extrapolated over.
    """
    cas = syms[syms["cas_eligible"] & (syms["status"] == "ok")]
    non = syms[~syms["cas_eligible"] & (syms["status"] == "ok")]
    if cas.empty or non.empty:
        return non.iloc[0:0]
    q = pd.to_numeric(cas["dayQty"], errors="coerce").replace(0, np.nan).dropna()
    if q.empty:
        return non.iloc[0:0]
    lo, hi = q.quantile(0.10), q.quantile(0.90)
    nq = pd.to_numeric(non["dayQty"], errors="coerce")
    return non[(nq >= lo) & (nq <= hi)]


def aggregate(syms: pd.DataFrame, date: dt.date, index_level: float | None) -> pd.DataFrame:
    """One row per group, plus the control-adjusted answer."""
    nifty = syms[syms["in_nifty50"]]
    cas = syms[syms["cas_eligible"]]
    non = syms[~syms["cas_eligible"]]
    matched = matched_control(syms)

    rows = [
        _stats(nifty, GROUP_NIFTY, date),
        _stats(cas, "CAS_ALL", date),
        _stats(non, GROUP_NONCAS, date),
        _stats(matched, GROUP_NONCAS_MATCHED, date),
    ]
    out = pd.DataFrame(rows)

    # The control mean is the drift any name saw over the same clock window
    # while the CAS names were in auction.  Subtracting it leaves the auction.
    ctrl = out.loc[out["group"] == GROUP_NONCAS_MATCHED, "eff_bps_mean"]
    ctrl = float(ctrl.iloc[0]) if len(ctrl) and pd.notna(ctrl.iloc[0]) else np.nan
    out["control_bps"] = ctrl
    out["net_of_control_bps"] = np.where(
        out["group"].isin([GROUP_NIFTY, "CAS_ALL"]),
        out["eff_bps_weighted"].fillna(out["eff_bps_mean"]) - ctrl,
        np.nan,
    )

    out["index_level"] = index_level if index_level else np.nan
    out["index_points"] = np.where(
        (out["group"] == GROUP_NIFTY) & pd.notna(out["eff_bps_weighted"]) & bool(index_level),
        out["eff_bps_weighted"] / 10_000.0 * (index_level or np.nan),
        np.nan,
    )
    return out


# --------------------------------------------------------------------------- #
# Console -- the narrative, in the same order as the doc                       #
# --------------------------------------------------------------------------- #

def _hhmmss(td) -> str:
    """kdb `time` reaches pandas as a timedelta, not a clock time."""
    if td is None or pd.isna(td):
        return "-"
    ms = int(pd.Timedelta(td).total_seconds() * 1000)
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


def _f(v, nd: int = 2, sign: bool = False) -> str:
    if v is None or (isinstance(v, float) and (np.isnan(v) or np.isinf(v))):
        return "-"
    return f"{v:+,.{nd}f}" if sign else f"{v:,.{nd}f}"


def print_report(
    syms: pd.DataFrame, agg: pd.DataFrame, date: dt.date, index_level: float | None
) -> None:
    row = agg.set_index("group")
    print()
    print("=" * 78)
    print(f"  How does CAS impact the NIFTY 50?   {date}")
    print("=" * 78)

    # -- S1 / S2 ------------------------------------------------------------- #
    print(f"\n  S1  universe")
    print(f"      CAS-eligible        : {int(syms['cas_eligible'].sum())} syms "
          f"({int(syms['in_nifty50'].sum())} of them NIFTY 50 members)")
    print(f"      control (non-CAS)   : {int((~syms['cas_eligible']).sum())} syms")

    print(f"\n  S2  windows (HKT)")
    print(f"      old-rule close      : size wavg price {OLD_RULE_START[:8]} - {OLD_RULE_END[:8]}")
    print(f"      CAS reference price : size wavg price {REF_START[:8]} - {REF_END[:8]}")
    print(f"      auction print       : first price {CLOSE_START[:8]} - {CLOSE_END[:8]}")

    # Print-time diagnostic.  The freeze is system-driven and market-wide, so the
    # prints should cluster on one instant; a wide spread means the window is
    # catching ordinary trades rather than the auction.
    tc = pd.to_timedelta(syms.loc[syms["status"] == "ok", "tClose"], errors="coerce").dropna()
    if not tc.empty:
        print(f"      print times         : {tc.nunique()} distinct, "
              f"{_hhmmss(tc.min())} - {_hhmmss(tc.max())}")
        for t, n in tc.value_counts().head(3).items():
            print(f"        {_hhmmss(t)}  {n} syms")
    missing = int((syms["status"] == "no_close_price").sum())
    if missing:
        print(f"      [!] {missing} syms have no print in the close window -- if this is "
              f"large the window is missing the auction")

    # -- S3 / S5 ------------------------------------------------------------- #
    basis = syms["old_rule_basis"].dropna().iloc[0] if len(syms) else "?"
    win = (f"{OLD_RULE_START[:8]}-{OLD_RULE_END[:8]}" if basis == "last30-continuous"
           else f"{OLD_WINDOW_START[:8]}-{OLD_WINDOW_END[:8]}")
    print(f"\n  S3  the counterfactual: auction print vs the old rule -- the VWAP of "
          f"the last 30 min of continuous")
    print(f"      basis: {basis} ({win} HKT)")
    print(f"      {'group':<16}{'n':>6}{'mean bps':>11}{'|mean| bps':>12}"
          f"{'|mean| ticks':>14}{'moved>=1t':>11}")
    for g in (GROUP_NIFTY, "CAS_ALL", GROUP_NONCAS, GROUP_NONCAS_MATCHED):
        if g not in row.index:
            continue
        r = row.loc[g]
        print(f"      {g:<16}{int(r['n_ok']):>6}{_f(r['eff_bps_mean'],2,True):>11}"
              f"{_f(r['eff_abs_bps_mean']):>12}{_f(r['eff_abs_ticks_mean']):>14}"
              f"{_f(r['pct_moved_ge_1_tick'],1):>11}%")

    # -- S4 ------------------------------------------------------------------ #
    if GROUP_NIFTY in row.index and pd.notna(row.loc[GROUP_NIFTY, "eff_bps_weighted"]):
        r = row.loc[GROUP_NIFTY]
        print(f"\n  S4  the index effect (weight-weighted, the identity the index is built on)")
        print(f"      NIFTY 50 effect     : {_f(r['eff_bps_weighted'],2,True)} bps"
              + (f"   = {_f(r['index_points'],1,True)} points on {_f(index_level,0)}"
                 if index_level else ""))
        print(f"      weight covered      : {_f(r['weight_covered_pct'])}% of the index")

        top = syms.reindex(
            syms["contribBps"].abs().sort_values(ascending=False).index).head(10)
        print(f"\n      who moved it")
        print(f"        {'sym':<16}{'weight%':>9}{'eff bps':>10}{'ticks':>8}"
              f"{'contrib bps':>13}{'share%':>9}")
        for t in top.itertuples():
            if pd.isna(t.contribBps):
                continue
            print(f"        {t.sym:<16}{_f(t.weight_pct):>9}{_f(t.effectBps,1,True):>10}"
                  f"{_f(t.effectTicks,1,True):>8}{_f(t.contribBps,2,True):>13}"
                  f"{_f(t.contribShare,1):>9}")

        contrib = pd.to_numeric(syms["contribBps"], errors="coerce").dropna()
        net, gross = contrib.sum(), contrib.abs().sum()
        top5 = contrib.abs().sort_values(ascending=False).head(5).sum()
        print(f"\n      net {_f(net,2,True)} bps against gross {_f(gross)} bps "
              f"-- {_f(net / gross * 100.0 if gross else np.nan,0)}% of the movement "
              f"survived cancelling out")
        print(f"      top 5 names carry {_f(top5 / gross * 100.0 if gross else np.nan,0)}% "
              f"of the gross movement")

    # -- S5 ------------------------------------------------------------------ #
    ctrl = row.loc[GROUP_NONCAS_MATCHED] if GROUP_NONCAS_MATCHED in row.index else None
    if ctrl is not None and pd.notna(ctrl["eff_bps_mean"]):
        print(f"\n  S5  control: names with no auction, same clock window")
        print(f"      drift             : {_f(ctrl['eff_bps_mean'],2,True)} bps "
              f"over {int(ctrl['n_ok'])} matched names")
        print(f"      window-shift      : {_f(ctrl['window_shift_bps_mean'],2,True)} bps "
              f"-- 17:30-18:00 vs 17:15-17:45 on names with no auction, i.e. what "
              f"the 15-minute\n{'':<26}shift alone is worth. Anything smaller than "
              f"this in S4 is window artefact, not CAS.")
        if GROUP_NIFTY in row.index:
            net_row = row.loc[GROUP_NIFTY, "net_of_control_bps"]
            print(f"      NIFTY 50 net of drift: {_f(net_row,2,True)} bps "
                  f"<- the auction-specific part")
    else:
        print(f"\n  S5  control: no usable non-CAS names -- the drift adjustment is "
              f"unavailable, so S4 is a realised move, not an isolated effect")
    print()


# --------------------------------------------------------------------------- #

def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default=KDB_HOST)
    ap.add_argument("--port", type=int, default=KDB_PORT)
    ap.add_argument("--date", help="YYYY-MM-DD; default = last business day (server side)")
    ap.add_argument("--isin-file", default=ISIN_FILE, help="CAS ISIN whitelist")
    ap.add_argument("--weights-file", default=WEIGHTS_FILE,
                    help="NIFTY 50 index weights (nse_symbol, isin, weight_pct)")
    ap.add_argument("--index-level", type=float,
                    help="official NIFTY 50 close, to quote the effect in points")
    ap.add_argument("--old-rule-window", choices=tuple(OLD_RULE_CHOICES),
                    default="last30-continuous",
                    help="which VWAP stands in for the old close. "
                         "last30-continuous (default) = 17:15-17:45, the rule "
                         "applied to today's session; clock-1730-1800 = the window "
                         "it occupied pre-CAS, which for a CAS name contains the "
                         "auction print itself and is only meaningful for controls")
    ap.add_argument("--out-dir", default=OUTPUT_DIR)
    ap.add_argument("--append-panel", action="store_true",
                    help=f"append the group rows to {os.path.basename(PANEL_FILE)}, "
                         f"which is what turns single days into evidence")
    ap.add_argument("--print-query", action="store_true",
                    help="print the q query and exit, without connecting")
    args = ap.parse_args()

    if args.print_query:
        print(prices_query())
        return 0

    cas_isins = set(load_isins(args.isin_file))
    if not cas_isins:
        print(f"[fatal] no ISIN in {args.isin_file} -- the study needs the CAS "
              f"whitelist to split treated from control names.", file=sys.stderr)
        return 2
    print(f"[info] {len(cas_isins)} CAS ISINs loaded")

    weights = load_weights(args.weights_file)
    if weights is None:
        print(f"[warn] {args.weights_file} not found -- no index weights, so no "
              f"weighted index effect and no attribution", file=sys.stderr)
    else:
        n_w = int(weights["weight_pct"].notna().sum())
        print(f"[info] index weights: {n_w} of {len(weights)} members, "
              f"{weights['weight_pct'].sum():.2f}% of index weight")

    with connect(args.host, args.port) as conn:
        date = dt.date.fromisoformat(args.date) if args.date else resolve_date(conn)
        print(f"[info] connected to {args.host}:{args.port}, date = {date}")

        universe = fetch_universe(conn, date)
        if universe.empty:
            print("[fatal] empty universe -- check the date", file=sys.stderr)
            return 1
        n_cas = int(universe["isin"].isin(cas_isins).sum())
        print(f"[info] {len(universe)} Indian listings: {n_cas} CAS-eligible, "
              f"{len(universe) - n_cas} control")

        raw = fetch_prices(conn, date, universe["sym"].tolist())

    if args.old_rule_window == "clock-1730-1800":
        print("[warn] the 17:30-18:00 window contains the auction print for CAS "
              "names, so their counterfactual is contaminated -- this setting is "
              "for inspecting the control arm, not for the headline number",
              file=sys.stderr)
    syms = build_syms(raw, universe, weights, date, cas_isins, args.old_rule_window)
    agg = aggregate(syms, date, args.index_level)
    print_report(syms, agg, date, args.index_level)

    os.makedirs(args.out_dir, exist_ok=True)
    stamp = f"{date:%Y%m%d}"
    p_syms = os.path.join(args.out_dir, f"casstudy_syms_{stamp}.csv")
    p_agg = os.path.join(args.out_dir, f"casstudy_index_{stamp}.csv")
    syms.to_csv(p_syms, index=False, float_format="%.6f")
    agg.to_csv(p_agg, index=False, float_format="%.6f")
    written = [p_syms, p_agg]

    if args.append_panel:
        os.makedirs(os.path.dirname(PANEL_FILE), exist_ok=True)
        header = not os.path.exists(PANEL_FILE)
        agg.to_csv(PANEL_FILE, mode="a", header=header, index=False, float_format="%.6f")
        written.append(PANEL_FILE)

    for p in written:
        print(f"  written -> {p}")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())

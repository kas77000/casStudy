#!/usr/bin/env python3
"""How does the closing auction (CAS) impact the NIFTY 50?

The narrative, the reasoning behind each step and how to read the output live in
`docs/cas_study_method.md`.  In brief:

  S1  universe      every Indian listing, split into CAS-eligible and not.
                    The non-eligible names are the control arm, not an oversight.
  S2  prices        one round of queries per sym: the old-rule close proxy, the
                    CAS reference price, the last continuous print, the auction
                    print, and the day's volume.
  S3  counterfactual  effect_bps = auction print vs the close the *old* rule would
                    have produced for that same stock, same day.  Within-name, so
                    no size or liquidity confound.
  S4  attribution   weight it by index weight: contribution_bps sums exactly to the
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
import time

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

#: Optional snapshots of the `equity` reference data, shared with `casretro` and
#: written by tools/export_cas_universe.py.  When one exists the universe is read
#: from it instead of querying kdb.
#:
#: The whole-book file comes first here, and it is the only one this study can
#: actually use: the non-CAS names are the control arm, so a CAS-only snapshot
#: leaves S5 with nothing to measure.  The CAS-only path is still tried, so that
#: a run with only that file present fails with an explanation rather than
#: silently querying kdb behind your back.
INDIA_UNIVERSE_FILE = os.path.join(PROJECT_DIR, "config", "india_universe.csv")
CAS_UNIVERSE_FILE = os.path.join(PROJECT_DIR, "config", "cas_universe.csv")
UNIVERSE_FILE_CANDIDATES = (INDIA_UNIVERSE_FILE, CAS_UNIVERSE_FILE)

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

#: Which price `effect_bps` is measured against.  The default is the faithful
#: reading of the rule; the alternative is there to be argued with.
OLD_RULE_CHOICES = {
    "last30-continuous": "px_old_rule_last30_continuous",     # 17:15-17:45, the rule applied today
    "clock-1730-1800": "px_old_rule_clock_1730_1800",    # 15:00-15:30 IST, the old clock window
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
GROUP_CAS_ALL = "CAS_ALL"
GROUP_NONCAS = "NONCAS"
GROUP_NONCAS_MATCHED = "NONCAS_MATCHED"

#: Below this many usable control names the drift adjustment is not reported.
#: A "control" of three illiquid stocks is worse than no control, because it
#: looks like one.
MIN_CONTROL_NAMES = 20

#: A move smaller than this is not a move -- it is the grid.
MIN_TICKS = 1.0

#: Decimals on every float in the csv output, matching casretro's report layer.
FLOAT_DECIMALS = 2


# --------------------------------------------------------------------------- #
# S1 -- universe                                                               #
# --------------------------------------------------------------------------- #

_UNIVERSE_Q = """
{{[d] select distinct sym, ID_ISIN{extra} from {tbl} where date=d, {like} }}
"""

#: Column names the `equity` table might use for the previous close.  Probed at
#: runtime rather than assumed, because it is only needed for the optional
#: whole-day reconciliation and its absence must not be fatal.
PREV_CLOSE_CANDIDATES = ("px_last_prev", "PX_LAST_PREV", "px_prev_close",
                         "prev_close", "PX_LAST")


def _as_text(s: pd.Series) -> pd.Series:
    """pykx hands back bytes for symbol and char columns; normalise to str."""
    return s.map(
        lambda v: v.decode(errors="replace") if isinstance(v, (bytes, bytearray))
        else ("" if v is None else str(v))
    )


def equity_columns(conn) -> set[str]:
    """Column names of the reference table, so optional ones can be probed."""
    try:
        return {c.decode() if isinstance(c, (bytes, bytearray)) else str(c)
                for c in conn(f"cols {EQUITY_TABLE}").py()}
    except Exception:                                   # pragma: no cover
        return set()


def fetch_universe(conn, date: dt.date, prev_close_col: str | None = None) -> pd.DataFrame:
    """Every Indian listing on the date, with its ISIN.

    Both arms come out of one query: the CAS whitelist splits them afterwards.
    Carrying the ISIN here is what lets the index weights attach directly,
    without the Bloomberg / NSE symbol-mapping detour `cas_price_move` needs.

    `prev_close_col`, when the reference table has one, rides along for the
    whole-day reconciliation.
    """
    like = " | ".join(f'(sym like "{p}")' for p in SYM_SUFFIXES)
    extra = f", px_prev_close: {prev_close_col}" if prev_close_col else ""
    qry = _UNIVERSE_Q.format(tbl=EQUITY_TABLE, like=like, extra=extra)
    df = conn(qry, date).pd()
    df.columns = [c.decode() if isinstance(c, (bytes, bytearray)) else str(c)
                  for c in df.columns]
    df = df.rename(columns={"ID_ISIN": "isin"})
    df["sym"] = _as_text(df["sym"])
    df["isin"] = _as_text(df["isin"]).str.strip().str.upper()
    if "px_prev_close" in df.columns:
        df["px_prev_close"] = pd.to_numeric(df["px_prev_close"], errors="coerce")
    return df.drop_duplicates("sym").reset_index(drop=True)


def load_universe_csv(
    path: str,
    cas_isins: set[str],
    *,
    date: dt.date | None = None,
    verbose: bool = True,
) -> pd.DataFrame | None:
    """The same snapshot `casretro` reads, mapped onto this script's columns.

    Returns None when the file is absent, which is the signal to query kdb.  A
    file that is present but unusable raises instead: it was put there
    deliberately, so a problem with it should surface rather than hide behind a
    query that happens to work.

    One requirement is specific to this script.  `casretro` narrows the snapshot
    with the CAS whitelist, so a CAS-only export serves it perfectly well -- but
    here the non-CAS names *are* the control arm, and a CAS-only file would
    silently leave S5 with nothing to measure and the headline effect
    unattributable to the auction.  So the file is rejected unless it carries
    names outside the whitelist.
    """
    if not os.path.exists(path):
        return None

    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    if df.empty:
        raise SystemExit(f"[fatal] {path} is empty -- delete it to query kdb instead")

    if "sym" not in df.columns:
        raise SystemExit(
            f"[fatal] {path} has no `sym` column. It should be the output of\n"
            f"        python tools/export_cas_universe.py --scope all"
        )

    snapshot = ""
    if "snapshot_date" in df.columns:
        vals = [v for v in df["snapshot_date"].unique() if str(v).strip()]
        snapshot = str(vals[0]) if vals else ""

    isin_col = next((c for c in ("ID_ISIN", "isin", "ISIN") if c in df.columns), None)
    if isin_col is None:
        raise SystemExit(
            f"[fatal] {path} has no ISIN column, so CAS eligibility cannot be "
            f"decided.\n        Re-export it with tools/export_cas_universe.py."
        )

    out = pd.DataFrame({
        "sym": _as_text(df["sym"]).str.strip(),
        "isin": _as_text(df[isin_col]).str.strip().str.upper(),
    })

    prev_col = next((c for c in PREV_CLOSE_CANDIDATES if c in df.columns), None)
    if prev_col:
        out["px_prev_close"] = pd.to_numeric(
            df[prev_col].replace("", None), errors="coerce"
        )

    out = out[out["sym"] != ""]
    like = tuple(p.replace("*", "") for p in SYM_SUFFIXES)      # ".IN", ".IS", ".IB"
    out = out[out["sym"].str.endswith(like)]
    out = out.drop_duplicates("sym").reset_index(drop=True)

    n_cas = int(out["isin"].isin(cas_isins).sum())
    n_control = len(out) - n_cas

    if verbose:
        print(f"[info] universe from {os.path.basename(path)}: {len(out)} listings"
              + (f", previous close from `{prev_col}`" if prev_col else "")
              + (f" (snapshot {snapshot})" if snapshot else ""))

    if n_control == 0:
        raise SystemExit(
            f"[fatal] {path} holds only CAS-eligible names ({n_cas} of {len(out)}), "
            f"so there is\n        no control arm and S5 cannot run. It was almost "
            f"certainly exported with\n        the ISIN filter on. Re-export it "
            f"whole:\n"
            f"          python tools/export_cas_universe.py --scope all "
            f"--force\n"
            f"        casretro still applies the whitelist when it reads that same "
            f"file."
        )

    if snapshot and date is not None and snapshot != date.isoformat():
        print(
            f"[warn] {os.path.basename(path)} was taken on {snapshot} but the study "
            f"is for {date}.\n"
            f"       sym and ISIN are static; the previous close is that day's, and "
            f"it feeds\n"
            f"       the optional whole-day reconciliation only.",
            file=sys.stderr,
        )
    return out


def resolve_universe(
    conn_factory,
    date: dt.date,
    cas_isins: set[str],
    *,
    csv_path: str | None = None,
    prefer_csv: bool = True,
    prev_close_col: str | None = None,
    verbose: bool = True,
):
    """-> (universe, source).  `conn_factory` is only called if kdb is needed."""
    if prefer_csv and csv_path:
        candidates = [csv_path] if isinstance(csv_path, (str, os.PathLike)) else list(csv_path)
        for path in candidates:
            got = load_universe_csv(str(path), cas_isins, date=date, verbose=verbose)
            if got is not None:
                return got, f"csv:{os.path.basename(str(path))}"
    if verbose:
        why = "no snapshot file" if prefer_csv else "--no-universe-file"
        print(f"[info] universe from kdb ({why})")
    return fetch_universe(conn_factory(), date, prev_close_col), "kdb:equity"


# --------------------------------------------------------------------------- #
# S2 -- prices                                                                 #
# --------------------------------------------------------------------------- #

# One statement per window, joined left-folded explicitly.  Written as
# `base lj a lj b` q reads it right to left and a sym present in b but not in a
# loses its columns.
#: One pass over the tape, not seven.
#:
#: The seven windows this study needs overlap -- 17:15-17:45 and 17:30-18:00 and
#: 17:30-17:45 all cover the same prints -- so they cannot be bucketed the way
#: `casretro` buckets its volume profile.  Written as seven `select`s they became
#: seven independent scans of the partition, two of them unbounded in time, over
#: the whole Indian book.  That is ~35 full-day scans on a normal run and it is
#: what made this query take minutes.
#:
#: Instead the slice is read **once** into `t`, each row is tagged with the masks
#: it belongs to, and every aggregate is computed from that one table.  Disk is
#: touched once per chunk; the rest is in memory.
#:
#: `not null price` is the base filter, matching the old `pre` and `cls`
#: selects.  The volume-weighted aggregates additionally need a size, so
#: `hasSize` rides in their masks -- keeping the old per-window semantics exactly.
_PRICES_Q = """
{{[d;syms]
  t: select sym, time, price, size from {tbl}
     where date=d, sym in syms, not null price{typ};
  t: update hasSize: not null size from t;
  t: update m_old : hasSize & (time >= {o1}) & time < {o2},
            m_oldw: hasSize & (time >= {w1}) & time < {w2},
            m_ref : hasSize & (time >= {r1}) & time < {r2},
            m_pre : time < {c1},
            m_cls : (time >= {k1}) & time <= {k2},
            m_pst : hasSize & time >= {p1}
     from t;
  a: select
       px_old_rule_last30_continuous: (size where m_old) wavg (price where m_old),
       vol_last30_continuous       : sum size where m_old,
       n_trades_last30_continuous  : sum m_old,
       px_old_rule_clock_1730_1800 : (size where m_oldw) wavg (price where m_oldw),
       vol_clock_1730_1800         : sum size where m_oldw,
       n_trades_clock_1730_1800    : sum m_oldw,
       px_cas_reference            : (size where m_ref) wavg (price where m_ref),
       vol_ref_window              : sum size where m_ref,
       n_trades_ref_window         : sum m_ref,
       px_last_continuous          : last price where m_pre,
       time_last_continuous        : last time where m_pre,
       n_trades_continuous         : sum m_pre,
       px_auction_close            : first price where m_cls,
       time_auction_print          : first time where m_cls,
       vol_close_window            : sum size where m_cls & hasSize,
       n_trades_close_window       : sum m_cls,
       vol_after_continuous        : sum size where m_pst,
       n_trades_after_continuous   : sum m_pst,
       vol_day                     : sum size where hasSize,
       vwap_day                    : (size where hasSize) wavg (price where hasSize)
     by sym from t;
  0!(`sym xkey ([] sym:syms)) lj a }}
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


#: Everything the query is expected to return.  A window with no prints at all on
#: the day makes q drop the column entirely rather than fill it with nulls, so a
#: quiet day would otherwise surface as a KeyError halfway through the analysis.
PRICE_COLUMNS = (
    "px_old_rule_last30_continuous", "vol_last30_continuous",
    "n_trades_last30_continuous",
    "px_old_rule_clock_1730_1800", "vol_clock_1730_1800",
    "n_trades_clock_1730_1800",
    "px_cas_reference", "vol_ref_window", "n_trades_ref_window",
    "px_last_continuous", "time_last_continuous", "n_trades_continuous",
    "px_auction_close", "time_auction_print", "vol_close_window",
    "n_trades_close_window",
    "vol_after_continuous", "n_trades_after_continuous",
    "vol_day", "vwap_day",
)


def fetch_prices(conn, date: dt.date, syms: list[str],
                 chunk: int = SYM_CHUNK) -> pd.DataFrame:
    """One query per chunk of syms, each a single pass over that day's tape.

    Progress is printed per chunk.  This is the slow part of the run by a wide
    margin -- it reads the whole day for the whole Indian book -- and a silent
    wait is indistinguishable from a hang.
    """
    qry = prices_query()
    frames = []
    n_chunks = (len(syms) + chunk - 1) // chunk
    t_start = time.perf_counter()
    for n, i in enumerate(range(0, len(syms), chunk), start=1):
        batch = syms[i:i + chunk]
        t0 = time.perf_counter()
        frames.append(conn(qry, date, _sym_vector(batch)).pd())
        done = time.perf_counter() - t_start
        eta = done / n * (n_chunks - n)
        print(f"[info] prices {n}/{n_chunks}  ({len(batch)} syms, "
              f"{time.perf_counter() - t0:,.1f}s"
              + (f", ~{eta:,.0f}s left)" if n < n_chunks else ")"),
              flush=True)
    df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    if df.empty:
        return pd.DataFrame(columns=("sym",) + PRICE_COLUMNS)
    df.columns = [c.decode() if isinstance(c, (bytes, bytearray)) else str(c)
                  for c in df.columns]
    df["sym"] = _as_text(df["sym"])
    missing = [c for c in PRICE_COLUMNS if c not in df.columns]
    for c in missing:
        df[c] = pd.NaT if c.startswith("time_") else np.nan
    if missing:
        print(f"[warn] the tape returned no rows at all for: {', '.join(missing)}"
              f" -- those windows are empty on this date", file=sys.stderr)
    return df


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

    for col in PRICE_COLUMNS:
        if col not in df.columns:
            df[col] = pd.NaT if col.startswith("time_") else np.nan
    for col in (c for c in PRICE_COLUMNS if c.startswith("n_trades_")):
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0).astype("int64")
    for col in (c for c in PRICE_COLUMNS if c.startswith("vol_")):
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)

    df["cas_eligible"] = df["isin"].isin(cas_isins)

    # -- index membership and weight, straight off the ISIN ------------------ #
    df["nifty50_weight_pct"] = np.nan
    df["company_name"] = pd.NA
    if weights is not None and not weights.empty:
        by_isin = weights.set_index(weights["isin"].str.upper())
        # `weight_pct` is the column name in config/nifty50_weights.csv -- the
        # input file's schema, not this study's.  It becomes the more explicit
        # nifty50_weight_pct on the way out.
        df["nifty50_weight_pct"] = df["isin"].map(by_isin["weight_pct"])
        df["company_name"] = df["isin"].map(by_isin["name"])
        df["in_nifty50"] = df["isin"].isin(set(by_isin.index))
    else:
        df["in_nifty50"] = False

    df["study_group"] = np.where(
        df["in_nifty50"], GROUP_NIFTY,
        np.where(df["cas_eligible"], GROUP_CAS_OTHER, GROUP_NONCAS),
    )

    # -- S3: the counterfactual --------------------------------------------- #
    # Same stock, same day, same information.  The only difference between the
    # two prices is the mechanism that produced them.
    base_col = OLD_RULE_CHOICES[old_rule]
    base = df[base_col]
    df["old_rule_basis"] = old_rule
    df["effect_price"] = df["px_auction_close"] - base
    df["tick_size"] = base.where(base.notna(), df["px_auction_close"]).map(tick_size)
    df["effect_ticks"] = (df["effect_price"] / df["tick_size"]).round(2)
    df["effect_bps"] = (df["px_auction_close"] / base - 1.0) * 10_000.0

    # The 15-minute shift between the two readings of "the last 30 minutes".
    # On a control name -- no auction in either window -- this is pure window
    # artefact, which is what makes it the yardstick for how much of the treated
    # effect could be the same artefact rather than the auction.
    df["window_shift_bps"] = (
        df["px_old_rule_clock_1730_1800"]
        / df["px_old_rule_last30_continuous"] - 1.0) * 10_000.0

    # Secondary readings, kept because they disagree in informative ways: a gap
    # between move_vs_last_continuous_bps and effect_bps is stale-last-print
    # bias, and close_vs_reference_bps is what the exchange's band is measured
    # against.
    df["move_vs_last_continuous_bps"] = (
        df["px_auction_close"] / df["px_last_continuous"] - 1.0) * 10_000.0
    df["close_vs_reference_bps"] = (
        df["px_auction_close"] / df["px_cas_reference"] - 1.0) * 10_000.0

    df["pct_volume_after_continuous"] = np.where(
        df["vol_day"] > 0, df["vol_after_continuous"] / df["vol_day"] * 100.0, np.nan)
    # How much of the day printed in the close window itself.  If the auction is
    # where the flow went, this is where it shows up -- and it is the regressor
    # for "are the big contributors the names whose flow concentrated there".
    df["pct_volume_in_close_window"] = np.where(
        df["vol_day"] > 0, df["vol_close_window"] / df["vol_day"] * 100.0, np.nan)

    # Whole-day return, for the reconciliation against the official index move.
    if "px_prev_close" in df.columns:
        df["return_day_bps"] = (
            df["px_auction_close"] / pd.to_numeric(df["px_prev_close"],
                                                   errors="coerce") - 1.0) * 10_000.0
    else:
        df["px_prev_close"] = np.nan
        df["return_day_bps"] = np.nan

    df["status"] = [
        "ok" if (o and c) else
        "no_old_rule_price" if c else
        "no_close_price" if o else
        "no_data"
        for o, c in zip(base.notna(), df["px_auction_close"].notna())
    ]

    # -- S4: attribution ----------------------------------------------------- #
    # contribution_bps sums to the index effect by construction, so a name's number is
    # its share of the answer -- not a big move in a name nobody weights.
    w = pd.to_numeric(df["nifty50_weight_pct"], errors="coerce")
    usable = w.notna() & df["effect_bps"].notna()
    total_w = float(w[usable].sum())
    df["contribution_bps"] = np.where(
        usable & (total_w > 0), w / total_w * df["effect_bps"], np.nan)
    gross = float(np.nansum(np.abs(df["contribution_bps"]))) if total_w else 0.0
    df["contribution_share_pct"] = (df["contribution_bps"].abs() / gross * 100.0) if gross else np.nan

    cols = [
        "date", "sym", "isin", "company_name", "study_group", "cas_eligible", "in_nifty50",
        "nifty50_weight_pct", "status", "old_rule_basis",
        "px_old_rule_last30_continuous", "vol_last30_continuous", "n_trades_last30_continuous",
        "px_old_rule_clock_1730_1800", "vol_clock_1730_1800", "n_trades_clock_1730_1800", "window_shift_bps",
        "px_cas_reference", "vol_ref_window", "n_trades_ref_window",
        "px_last_continuous", "time_last_continuous", "n_trades_continuous",
        "px_auction_close", "time_auction_print", "vol_close_window",
        "n_trades_close_window",
        "vol_day", "vol_after_continuous", "pct_volume_after_continuous",
        "pct_volume_in_close_window",
        "px_prev_close", "return_day_bps",
        "tick_size", "effect_price", "effect_ticks", "effect_bps",
        "move_vs_last_continuous_bps", "close_vs_reference_bps",
        "contribution_bps", "contribution_share_pct",
    ]
    for c in cols:
        if c not in df.columns:
            df[c] = pd.NA
    return df[cols].sort_values(
        ["study_group", "sym"], ignore_index=True)


# --------------------------------------------------------------------------- #
# S5 -- aggregates, per group                                                  #
# --------------------------------------------------------------------------- #

def _stats(df: pd.DataFrame, label: str, date: dt.date) -> dict:
    ok = df[df["status"] == "ok"]
    eff = pd.to_numeric(ok["effect_bps"], errors="coerce").dropna()
    ticks = pd.to_numeric(ok["effect_ticks"], errors="coerce").dropna()
    w = pd.to_numeric(ok["nifty50_weight_pct"], errors="coerce")

    weighted = np.nan
    covered = np.nan
    m = w.notna() & ok["effect_bps"].notna()
    if m.any() and w[m].sum():
        weighted = float((ok.loc[m, "effect_bps"] * w[m]).sum() / w[m].sum())
        covered = float(w[m].sum())

    return {
        "date": date,
        "group": label,
        "n_syms": len(df),
        "n_with_both_prices": len(ok),
        "index_weight_covered_pct": covered,
        "effect_bps_index_weighted": weighted,
        "effect_bps_mean": float(eff.mean()) if len(eff) else np.nan,
        "effect_bps_median": float(eff.median()) if len(eff) else np.nan,
        "effect_bps_std": float(eff.std()) if len(eff) > 1 else np.nan,
        "effect_abs_bps_mean": float(eff.abs().mean()) if len(eff) else np.nan,
        "effect_abs_ticks_mean": float(ticks.abs().mean()) if len(ticks) else np.nan,
        "pct_names_moved_ge_1_tick": (
            float((ticks.abs() >= MIN_TICKS).mean() * 100.0) if len(ticks) else np.nan),
        "pct_volume_after_continuous_mean": float(
            pd.to_numeric(ok["pct_volume_after_continuous"], errors="coerce").mean()) if len(ok) else np.nan,
        "window_shift_bps_mean": float(
            pd.to_numeric(ok["window_shift_bps"], errors="coerce").mean()) if len(ok) else np.nan,
        "n_up": int((eff > 0).sum()),
        "n_down": int((eff < 0).sum()),
        "n_flat": int((eff == 0).sum()),
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
    q = pd.to_numeric(cas["vol_day"], errors="coerce").replace(0, np.nan).dropna()
    if q.empty:
        return non.iloc[0:0]
    lo, hi = q.quantile(0.10), q.quantile(0.90)
    nq = pd.to_numeric(non["vol_day"], errors="coerce")
    return non[(nq >= lo) & (nq <= hi)]


def aggregate(syms: pd.DataFrame, date: dt.date, index_level: float | None) -> pd.DataFrame:
    """One row per group, plus the control-adjusted answer."""
    nifty = syms[syms["in_nifty50"]]
    cas = syms[syms["cas_eligible"]]
    non = syms[~syms["cas_eligible"]]
    matched = matched_control(syms)

    rows = [
        _stats(nifty, GROUP_NIFTY, date),
        _stats(cas, GROUP_CAS_ALL, date),
        _stats(non, GROUP_NONCAS, date),
        _stats(matched, GROUP_NONCAS_MATCHED, date),
    ]
    out = pd.DataFrame(rows)

    # The control mean is the drift any name saw over the same clock window
    # while the CAS names were in auction.  Subtracting it leaves the auction.
    #
    # It is only reported when the control arm is genuinely populated: a handful
    # of names is not a control, and subtracting a number built from three
    # illiquid stocks would dress noise up as a causal adjustment.
    ctrl = np.nan
    row_matched = out[out["group"] == GROUP_NONCAS_MATCHED]
    if len(row_matched) and int(row_matched["n_with_both_prices"].iloc[0]) >= MIN_CONTROL_NAMES:
        v = row_matched["effect_bps_mean"].iloc[0]
        ctrl = float(v) if pd.notna(v) else np.nan
    out["control_drift_bps"] = ctrl
    out["effect_bps_net_of_control_drift"] = np.where(
        out["group"].isin([GROUP_NIFTY, GROUP_CAS_ALL]),
        out["effect_bps_index_weighted"].fillna(out["effect_bps_mean"]) - ctrl,
        np.nan,
    )

    out["index_level"] = index_level if index_level else np.nan
    out["index_effect_points"] = np.where(
        (out["group"] == GROUP_NIFTY) & pd.notna(out["effect_bps_index_weighted"]) & bool(index_level),
        out["effect_bps_index_weighted"] / 10_000.0 * (index_level or np.nan),
        np.nan,
    )

    # Cross-sectional standard error of the weighted mean, so a small effect can
    # be told apart from nothing.  Weighted, to match the statistic it qualifies.
    out["effect_bps_stderr"] = np.nan
    for i, r in out.iterrows():
        sub = {GROUP_NIFTY: nifty, GROUP_CAS_ALL: cas,
               GROUP_NONCAS: non, GROUP_NONCAS_MATCHED: matched}[r["group"]]
        ok = sub[sub["status"] == "ok"]
        e = pd.to_numeric(ok["effect_bps"], errors="coerce").dropna()
        if len(e) > 1:
            out.at[i, "effect_bps_stderr"] = float(e.std(ddof=1) / np.sqrt(len(e)))
    return out


def reconcile_day(syms: pd.DataFrame, index_level: float | None,
                  index_level_prev: float | None) -> dict:
    """Rebuild the day's index return from constituents and compare to official.

    The whole study rests on two things being right: the weights and the prices.
    This is the one check that tests both at once against a number nobody in this
    codebase produced.  Agreement to a few bps means the machinery is sound; a
    wide gap means every effect number above is suspect, and the usual causes are
    a stale weight file or a constituent whose close we did not read.
    """
    out = {"available": False, "reconstructed_bps": np.nan,
           "official_bps": np.nan, "gap_bps": np.nan, "n_names": 0,
           "weight_covered_pct": np.nan}
    nifty = syms[syms["in_nifty50"]]
    w = pd.to_numeric(nifty.get("nifty50_weight_pct"), errors="coerce")
    r = pd.to_numeric(nifty.get("return_day_bps"), errors="coerce")
    m = w.notna() & r.notna()
    if not m.any() or not w[m].sum():
        return out
    out.update(
        available=True,
        reconstructed_bps=float((r[m] * w[m]).sum() / w[m].sum()),
        n_names=int(m.sum()),
        weight_covered_pct=float(w[m].sum()),
    )
    if index_level and index_level_prev:
        out["official_bps"] = (index_level / index_level_prev - 1.0) * 10_000.0
        out["gap_bps"] = out["reconstructed_bps"] - out["official_bps"]
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
    syms: pd.DataFrame, agg: pd.DataFrame, date: dt.date, index_level: float | None,
    recon: dict | None = None,
) -> None:
    row = agg.set_index("group")
    print()
    print("=" * 78)
    print(f"  How does CAS impact the NIFTY 50?   {date}")
    print("=" * 78)

    # -- S1 / S2 ------------------------------------------------------------- #
    control = syms[~syms["cas_eligible"]]
    live = control[control["n_trades_close_window"] > 0]
    print(f"\n  S1  universe")
    print(f"      CAS-eligible        : {int(syms['cas_eligible'].sum())} syms "
          f"({int(syms['in_nifty50'].sum())} of them NIFTY 50 members)")
    print(f"      control (non-CAS)   : {len(control)} syms, {len(live)} of them "
          f"printing in the close window")
    # The control arm only exists if those names are still trading through the
    # window the CAS names spend in auction.  If they are not -- migrated too, or
    # simply not traded -- S5 has nothing to say and must not pretend otherwise.
    if len(control) and not len(live):
        print(f"      [!] no control name printed after {CLOSE_START[:8]} -- either they "
              f"moved to CAS as well or they do not trade this late.\n"
              f"          The drift adjustment in S5 is unavailable; S3/S4 remain "
              f"realised effects, not isolated ones.")
    elif len(live) < MIN_CONTROL_NAMES:
        print(f"      [!] only {len(live)} control names print in the window "
              f"(minimum {MIN_CONTROL_NAMES}) -- too thin to subtract")

    print(f"\n  S2  windows (HKT)")
    print(f"      old-rule close      : size wavg price {OLD_RULE_START[:8]} - {OLD_RULE_END[:8]}")
    print(f"      CAS reference price : size wavg price {REF_START[:8]} - {REF_END[:8]}")
    print(f"      auction print       : first price {CLOSE_START[:8]} - {CLOSE_END[:8]}")

    # Print-time diagnostic.  The freeze is system-driven and market-wide, so the
    # prints should cluster on one instant; a wide spread means the window is
    # catching ordinary trades rather than the auction.
    tc = pd.to_timedelta(syms.loc[syms["status"] == "ok", "time_auction_print"], errors="coerce").dropna()
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
    for g in (GROUP_NIFTY, GROUP_CAS_ALL, GROUP_NONCAS, GROUP_NONCAS_MATCHED):
        if g not in row.index:
            continue
        r = row.loc[g]
        print(f"      {g:<16}{int(r['n_with_both_prices']):>6}{_f(r['effect_bps_mean'],2,True):>11}"
              f"{_f(r['effect_abs_bps_mean']):>12}{_f(r['effect_abs_ticks_mean']):>14}"
              f"{_f(r['pct_names_moved_ge_1_tick'],1):>11}%")

    # -- S4 ------------------------------------------------------------------ #
    if GROUP_NIFTY in row.index and pd.notna(row.loc[GROUP_NIFTY, "effect_bps_index_weighted"]):
        r = row.loc[GROUP_NIFTY]
        print(f"\n  S4  the index effect (weight-weighted, the identity the index is built on)")
        print(f"      NIFTY 50 effect     : {_f(r['effect_bps_index_weighted'],2,True)} bps"
              + (f"   = {_f(r['index_effect_points'],1,True)} points on {_f(index_level,0)}"
                 if index_level else ""))
        se = r.get("effect_bps_stderr")
        if pd.notna(se) and se:
            t = r["effect_bps_index_weighted"] / se
            print(f"      cross-sectional se  : +/-{_f(se)} bps  (t = {_f(t,1)}"
                  f"{'' if abs(t) >= 2 else ', i.e. not distinguishable from zero'})")
        print(f"      weight covered      : {_f(r['index_weight_covered_pct'])}% of the index")

        top = syms.reindex(
            syms["contribution_bps"].abs().sort_values(ascending=False).index).head(10)
        print(f"\n      who moved it")
        print(f"        {'sym':<16}{'weight%':>9}{'eff bps':>10}{'ticks':>8}"
              f"{'contrib bps':>13}{'share%':>9}")
        for t in top.itertuples():
            if pd.isna(t.contribution_bps):
                continue
            print(f"        {t.sym:<16}{_f(t.nifty50_weight_pct):>9}{_f(t.effect_bps,1,True):>10}"
                  f"{_f(t.effect_ticks,1,True):>8}{_f(t.contribution_bps,2,True):>13}"
                  f"{_f(t.contribution_share_pct,1):>9}")

        contrib = pd.to_numeric(syms["contribution_bps"], errors="coerce").dropna()
        net, gross = contrib.sum(), contrib.abs().sum()
        top5 = contrib.abs().sort_values(ascending=False).head(5).sum()
        print(f"\n      net {_f(net,2,True)} bps against gross {_f(gross)} bps "
              f"-- {_f(net / gross * 100.0 if gross else np.nan,0)}% of the movement "
              f"survived cancelling out")
        print(f"      top 5 names carry {_f(top5 / gross * 100.0 if gross else np.nan,0)}% "
              f"of the gross movement")

    # -- S5 ------------------------------------------------------------------ #
    ctrl = row.loc[GROUP_NONCAS_MATCHED] if GROUP_NONCAS_MATCHED in row.index else None
    if ctrl is not None and pd.notna(ctrl["effect_bps_mean"]):
        n_ctrl = int(ctrl["n_with_both_prices"])
        usable = n_ctrl >= MIN_CONTROL_NAMES
        print(f"\n  S5  control: names with no auction, same clock window")
        print(f"      drift             : {_f(ctrl['effect_bps_mean'],2,True)} bps "
              f"over {n_ctrl} matched names"
              + ("" if usable else
                 f"   [not subtracted -- under the {MIN_CONTROL_NAMES}-name minimum]"))
        print(f"      window-shift      : {_f(ctrl['window_shift_bps_mean'],2,True)} bps "
              f"-- 17:30-18:00 vs 17:15-17:45 on names with no auction, i.e. what "
              f"the 15-minute\n{'':<26}shift alone is worth. Anything smaller than "
              f"this in S4 is window artefact, not CAS.")
        if GROUP_NIFTY in row.index and usable:
            net_row = row.loc[GROUP_NIFTY, "effect_bps_net_of_control_drift"]
            print(f"      NIFTY 50 net of drift: {_f(net_row,2,True)} bps "
                  f"<- the auction-specific part")
        elif GROUP_NIFTY in row.index:
            print(f"      the S4 effect stands as a realised move -- there is no "
                  f"credible control to isolate the auction with")
    else:
        print(f"\n  S5  control: no usable non-CAS names -- the drift adjustment is "
              f"unavailable, so S4 is a realised move, not an isolated effect")

    # -- validation ----------------------------------------------------------- #
    # The one number in this report that comes from outside the codebase.
    if recon and recon.get("available"):
        print(f"\n  check  whole-day index return rebuilt from constituents")
        print(f"      reconstructed     : {_f(recon['reconstructed_bps'],2,True)} bps "
              f"over {recon['n_names']} names ({_f(recon['weight_covered_pct'])}% of weight)")
        if pd.notna(recon.get("official_bps")):
            gap = recon["gap_bps"]
            verdict = ("OK" if abs(gap) <= 5 else
                       "REVIEW -- weights or prices are off, so every number above "
                       "is suspect")
            print(f"      official          : {_f(recon['official_bps'],2,True)} bps")
            print(f"      gap               : {_f(gap,2,True)} bps   {verdict}")
        else:
            print(f"      pass --index-level and --index-level-prev to compare it "
                  f"against the official close-to-close move")
    else:
        print(f"\n  check  whole-day reconciliation unavailable -- no previous close "
              f"in the reference table (see --prev-close-col)")
    print()


# --------------------------------------------------------------------------- #

def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default=KDB_HOST)
    ap.add_argument("--port", type=int, default=KDB_PORT)
    ap.add_argument("--date", help="YYYY-MM-DD; default = last business day (server side)")
    ap.add_argument("--isin-file", default=ISIN_FILE, help="CAS ISIN whitelist")
    ap.add_argument(
        "--universe-file", default=None,
        help="csv snapshot of the equity reference data. Default: "
             "config/india_universe.csv, then config/cas_universe.csv, then kdb. "
             "This study needs the whole book -- the non-CAS names are its "
             "control arm -- so export it with tools/export_cas_universe.py "
             "--scope all",
    )
    ap.add_argument(
        "--no-universe-file", action="store_true",
        help="ignore the csv snapshot and always query the equity table",
    )
    ap.add_argument("--weights-file", default=WEIGHTS_FILE,
                    help="NIFTY 50 index weights (nse_symbol, isin, nifty50_weight_pct)")
    ap.add_argument("--index-level", type=float,
                    help="official NIFTY 50 close, to quote the effect in points")
    ap.add_argument("--index-level-prev", type=float,
                    help="official NIFTY 50 close of the previous session. With "
                         "--index-level it turns the whole-day reconciliation into "
                         "a pass/fail against a number this codebase did not produce")
    ap.add_argument("--prev-close-col",
                    help="column in the equity table holding the previous close; "
                         f"probed automatically among {', '.join(PREV_CLOSE_CANDIDATES)}")
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
    ap.add_argument("--qatt-table", default=QATT_TABLE,
                    help=f"market-data table name (default: {QATT_TABLE}). "
                         f"17034 / 17031 are ports, not table names")
    ap.add_argument("--equity-table", default=EQUITY_TABLE,
                    help=f"reference table name (default: {EQUITY_TABLE})")
    ap.add_argument("--print-query", action="store_true",
                    help="print the q query and exit, without connecting")
    args = ap.parse_args()

    # Every query builds its table name from these at call time, so rebinding the
    # module globals is enough -- no need to thread a name through each function.
    globals()["QATT_TABLE"] = args.qatt_table
    globals()["EQUITY_TABLE"] = args.equity_table

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

        # A weights file stamped weeks before the study date has probably missed
        # a rebalance, which silently corrupts every weighted number.
        if weights is not None and weights["asof"].str.strip().any():
            asof = max(a for a in weights["asof"] if a.strip())
            try:
                stale = (date - dt.date.fromisoformat(asof)).days
                if stale > 45:
                    print(f"[warn] index weights are as of {asof}, {stale} days before "
                          f"{date} -- check whether the index rebalanced since",
                          file=sys.stderr)
            except ValueError:
                pass

        _cands = ([args.universe_file] if args.universe_file
                  else list(UNIVERSE_FILE_CANDIDATES))
        use_csv = (not args.no_universe_file) and any(os.path.exists(p) for p in _cands)

        prev_col = args.prev_close_col
        if prev_col is None and not use_csv:
            # Only probe the table when we are actually going to query it; the
            # snapshot names its own previous-close column.
            have = equity_columns(conn)
            prev_col = next((c for c in PREV_CLOSE_CANDIDATES if c in have), None)
            print(f"[info] previous close column: {prev_col or 'not found -- '
                  'whole-day reconciliation disabled'}")

        universe, uni_source = resolve_universe(
            lambda: conn, date, cas_isins,
            csv_path=args.universe_file or UNIVERSE_FILE_CANDIDATES,
            prefer_csv=not args.no_universe_file,
            prev_close_col=prev_col,
        )
        print(f"[info] universe source: {uni_source}")
        if universe.empty:
            print("[fatal] empty universe -- check the date", file=sys.stderr)
            return 1
        n_cas = int(universe["isin"].isin(cas_isins).sum())
        print(f"[info] {len(universe)} Indian listings: {n_cas} CAS-eligible, "
              f"{len(universe) - n_cas} control")
        if n_cas == 0:
            print("[fatal] no listing matched the CAS whitelist -- the ISIN file and "
                  "the equity table disagree", file=sys.stderr)
            return 1

        raw = fetch_prices(conn, date, universe["sym"].tolist())

    if args.old_rule_window == "clock-1730-1800":
        print("[warn] the 17:30-18:00 window contains the auction print for CAS "
              "names, so their counterfactual is contaminated -- this setting is "
              "for inspecting the control arm, not for the headline number",
              file=sys.stderr)
    syms = build_syms(raw, universe, weights, date, cas_isins, args.old_rule_window)
    agg = aggregate(syms, date, args.index_level)
    recon = reconcile_day(syms, args.index_level, args.index_level_prev)
    print_report(syms, agg, date, args.index_level, recon)

    os.makedirs(args.out_dir, exist_ok=True)
    stamp = f"{date:%Y%m%d}"
    p_syms = os.path.join(args.out_dir, f"casstudy_syms_{stamp}.csv")
    p_agg = os.path.join(args.out_dir, f"casstudy_index_{stamp}.csv")
    # Two decimals on every float, integers left as integers -- same rule as the
    # retro, so numbers copied between the two reports look alike.
    syms.to_csv(p_syms, index=False, float_format=f"%.{FLOAT_DECIMALS}f")
    agg.to_csv(p_agg, index=False, float_format=f"%.{FLOAT_DECIMALS}f")
    written = [p_syms, p_agg]

    if args.append_panel:
        os.makedirs(os.path.dirname(PANEL_FILE), exist_ok=True)
        header = not os.path.exists(PANEL_FILE)
        agg.to_csv(PANEL_FILE, mode="a", header=header, index=False,
                   float_format=f"%.{FLOAT_DECIMALS}f")
        written.append(PANEL_FILE)

    for p in written:
        print(f"  written -> {p}")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())

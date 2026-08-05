#!/usr/bin/env python3
"""
India CAS -- price move between the end of continuous and the close.

By default it produces **two studies from one round of queries**:

    universe   every CAS-eligible Indian sym
    nifty50    the NIFTY 50 constituents listed in config/nifty50.csv

The subset lives inside the universe, so the prices are pulled once and sliced
afterwards.  Both files carry an `in_nifty50` column, so the universe file alone
is enough to reproduce the subset.  `--scope universe|nifty` runs just one.

Step 1: pull the CAS universe from the `equity` reference table (same query as
        temp.q: last business day + .IN syms + ISIN whitelist).
Step 2: for every one of those syms, pull out of `qatt`:
          px_pre_close     -> last price strictly before CUTOFF_CONTINUOUS
          px_close         -> first price inside [CLOSE_WINDOW_START; CLOSE_WINDOW_END]
          px_cas_reference -> size wavg price over [REF_WINDOW_START; REF_WINDOW_END)
          vol_ref_window   -> volume over that same reference window
          vol_after_cutoff -> volume from POST_VOLUME_FROM onward
Step 3: write a csv with the move (absolute / ticks / bps) and print a summary.

Every output column says what it is -- `docs/cas_price_move_columns.csv` is the
full list with definitions, and the q query itself carries the same names, so
nothing is renamed between the tape and the csv.

The move is reported three ways.  `move_price` is the raw price difference,
`move_ticks` divides it by the exchange tick applicable at that price (NSE CM
bands, effective 15 Apr 2025 -- see TICK_BANDS), and `move_bps` is the relative
move.  Ticks are the comparable unit across names: a 5 paise move is one tick on
a 300-rupee stock and five on a 100-rupee one.

`config/nifty50_weights.csv` carries the index weight of each NIFTY 50 member.
When it is present the weight rides along in the output and the summary adds an
index-weighted mean move, which is what the index actually did, as opposed to the
equal-weighted average across names.

`px_cas_reference` is the exchange's CAS reference price: the 15:00-15:15 IST
VWAP, i.e. 17:30-17:45 HKT.  Every CAS order has to sit within +/-3% (=300 bps)
of it, so `close_vs_reference_bps` says how close the print came to the band edge.

The NIFTY 50 file comes from either
    tools/nifty50_from_nse.py --resolve-syms          (no Bloomberg needed)
or
    tools/bloomberg_nifty50.py  on the Bloomberg box  -> members
    tools/map_nifty50_syms.py   on the kdb box        -> adds the `sym`
If it is missing, the universe study still runs and the subset one is skipped.

All times are HKT, i.e. the raw `time` column of the qatt table -- no timezone
conversion is applied anywhere.  IST = HKT - 02:30.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import sys

import pandas as pd

try:
    import pykx as kx
except ImportError:  # pragma: no cover
    kx = None       # only fatal once we actually connect, so --print-query works


# --------------------------------------------------------------------------- #
# Constants -- edit here                                                       #
# --------------------------------------------------------------------------- #

KDB_HOST = "localhost"
KDB_PORT = 5000
KDB_USER = None          # e.g. "user"  (None -> no credentials sent)
KDB_PASS = None          # e.g. "pwd"
KDB_TIMEOUT = 120.0      # seconds

EQUITY_TABLE = "equity"        # REF/equity.csv
#: The market-data table is called `qatt` on every instance.  17034 / 17031 are
#: the HT and RT *ports*, not table names -- overridable with --qatt-table.
QATT_TABLE = "qatt"            # QATT-HT / QATT-RT

# sym suffixes that identify the Indian listings in the ref table.  NSE `.IN`
# only, matching casretro.config.SYM_SUFFIXES: the other Indian listing lines
# lengthen the universe considerably and the auction being measured is NSE's.
SYM_SUFFIXES = ("*.IN",)

# HKT. Must keep the milliseconds -- `17:50:00` is a *second* atom in q and
# would compare wrong against the `time` (ms) column.
CUTOFF_CONTINUOUS = "17:50:00.000"    # end of continuous
CLOSE_WINDOW_START = "17:58:00.000"   # close window, inclusive
CLOSE_WINDOW_END = "18:00:00.000"     # close window, inclusive

# CAS reference-price window: 15:00-15:15 IST.  Half-open, so a print exactly at
# 17:45:00.000 belongs to the close and not to the reference VWAP.
REF_WINDOW_START = "17:30:00.000"
REF_WINDOW_END = "17:45:00.000"

# Volume is accumulated from here to the end of the day, no upper bound.
POST_VOLUME_FROM = "17:50:00.000"

# Optional filter on the qatt `typ` column, e.g. ("trade",) to keep only
# trades.  None -> take every record that carries a non-null price.
TYP_FILTER: tuple[str, ...] | None = None

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
ISIN_FILE = os.path.join(PROJECT_DIR, "config", "cas_isins.txt")
NIFTY_FILE = os.path.join(PROJECT_DIR, "config", "nifty50.csv")
WEIGHTS_FILE = os.path.join(PROJECT_DIR, "config", "nifty50_weights.csv")
OUTPUT_DIR = os.path.join(PROJECT_DIR, "output")

# NSE cash-segment tick size by price band, effective 15 April 2025.  Each entry
# is (upper bound exclusive, tick); the last band has no upper bound.  A move is
# expressed in ticks of the band its *starting* price sits in.
TICK_BANDS: tuple[tuple[float | None, float], ...] = (
    (250.0, 0.01),
    (1_000.0, 0.05),
    (5_000.0, 0.10),
    (10_000.0, 0.50),
    (20_000.0, 1.00),
    (None, 5.00),
)

# syms are sent to kdb in batches of this size
SYM_CHUNK = 500


# --------------------------------------------------------------------------- #
# ISIN universe                                                                #
# --------------------------------------------------------------------------- #

_ISIN_RE = re.compile(r"\b[A-Z]{2}[A-Z0-9]{9}[0-9]\b")


def isin_check_digit_ok(isin: str) -> bool:
    """ISIN check digit (Luhn over the letters-expanded-to-digits string)."""
    if len(isin) != 12:
        return False
    digits = "".join(str(ord(c) - 55) if c.isalpha() else c for c in isin)
    total, double = 0, True
    for ch in reversed(digits[:-1]):
        d = int(ch)
        if double:
            d *= 2
            if d > 9:
                d -= 9
        total += d
        double = not double
    return (10 - total % 10) % 10 == int(digits[-1])


def load_isins(path: str) -> list[str]:
    """Extract ISINs from any text file: backtick list, csv, one per line.

    Lines starting with '#' or '/' are ignored so the file can be commented.
    """
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        body = "\n".join(
            line for line in fh.read().splitlines()
            if not line.lstrip().startswith(("#", "/"))
        )

    seen, out, bad = set(), [], []
    for isin in _ISIN_RE.findall(body.upper()):
        if isin in seen:
            continue
        seen.add(isin)
        (out if isin_check_digit_ok(isin) else bad).append(isin)

    if bad:
        print(
            f"[warn] {len(bad)} entr{'y' if len(bad) == 1 else 'ies'} in "
            f"{os.path.basename(path)} failed the ISIN check digit and were "
            f"dropped: {', '.join(bad[:10])}"
            f"{' ...' if len(bad) > 10 else ''}",
            file=sys.stderr,
        )
    return out


# --------------------------------------------------------------------------- #
# NIFTY 50 subset                                                              #
# --------------------------------------------------------------------------- #

_NIFTY_HOWTO = (
    "        Build it either way:\n"
    "          A. no Bloomberg needed:\n"
    "             python tools/nifty50_from_nse.py --out {path} --resolve-syms\n"
    "          B. on the Bloomberg machine:\n"
    "             python tools/bloomberg_nifty50.py --out {path}\n"
    "             then copy it across and, on the kdb machine:\n"
    "             python tools/map_nifty50_syms.py --file {path}"
)


def load_nifty50(path: str, *, required: bool = True) -> pd.DataFrame | None:
    """Read config/nifty50.csv and return the rows that carry a resolved `sym`.

    The file is the hand-off between the two helper scripts: one supplies the
    members and their ISINs, the kdb side supplies `sym`.  A file with an empty
    `sym` column means the second script has not run yet, which is worth an
    explicit error -- filtering on nothing would silently produce an empty
    report.

    `required=False` returns None instead of exiting when the file is absent, so
    a default run can still produce the whole-universe study.
    """
    if not os.path.exists(path):
        if not required:
            print(
                f"[warn] {path} not found -- skipping the NIFTY 50 study.\n"
                + _NIFTY_HOWTO.format(path=path),
                file=sys.stderr,
            )
            return None
        raise SystemExit(
            f"[fatal] {path} not found.\n" + _NIFTY_HOWTO.format(path=path)
        )

    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    if "sym" not in df.columns:
        raise SystemExit(
            f"[fatal] {path} has no `sym` column -- run tools/map_nifty50_syms.py "
            f"on it first."
        )

    resolved = df[df["sym"].str.strip() != ""].copy()
    if resolved.empty:
        raise SystemExit(
            f"[fatal] no row in {path} has a `sym` yet -- run\n"
            f"          python tools/map_nifty50_syms.py --file {path}\n"
            f"        on the kdb machine to fill the column in."
        )

    resolved["sym"] = resolved["sym"].str.strip()

    unmapped = len(df) - len(resolved)
    if unmapped:
        print(
            f"[warn] {unmapped} of {len(df)} NIFTY members in "
            f"{os.path.basename(path)} have no sym and were skipped",
            file=sys.stderr,
        )
    return resolved.drop_duplicates(subset=["sym"]).reset_index(drop=True)


# --------------------------------------------------------------------------- #
# Tick size                                                                    #
# --------------------------------------------------------------------------- #

def tick_size(price) -> float | None:
    """Exchange tick applicable at `price`, or None when there is no price.

    NSE quotes in bands, so the same 5 paise is one tick on a 300-rupee stock and
    five on a 100-rupee one -- which is exactly why a move in ticks compares
    across names and a move in rupees does not.
    """
    if price is None or pd.isna(price) or price <= 0:
        return None
    for upper, tick in TICK_BANDS:
        if upper is None or price < upper:
            return tick
    return TICK_BANDS[-1][1]


# --------------------------------------------------------------------------- #
# NIFTY 50 index weights                                                       #
# --------------------------------------------------------------------------- #

WEIGHT_KEYS = (("isin", "isin"), ("nse_symbol", "nse_symbol"))


def load_weights(path: str) -> pd.DataFrame | None:
    """Read config/nifty50_weights.csv -- one row per index member.

    Columns: nse_symbol, isin, name, weight_pct, asof, source.  A member whose
    weight the source did not publish keeps an empty `weight_pct` rather than a
    guessed one, so a gap stays visible in the coverage line instead of quietly
    redistributing itself over the other names.
    """
    if not os.path.exists(path):
        return None
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    if "weight_pct" not in df.columns:
        print(f"[warn] {path} has no weight_pct column -- ignoring it", file=sys.stderr)
        return None
    df["weight_pct"] = pd.to_numeric(df["weight_pct"], errors="coerce")
    for col in ("isin", "nse_symbol", "name", "asof", "source"):
        if col not in df.columns:
            df[col] = ""
        df[col] = df[col].astype(str).str.strip()
    return df


def attach_weights(
    nifty: pd.DataFrame, weights: pd.DataFrame | None, *, verbose: bool = True
) -> pd.DataFrame:
    """Add `weight_pct` to the NIFTY member frame, matching on ISIN then symbol.

    Two keys because the two files come from different places: the members file
    is built from NSE (ISIN) or Bloomberg (ticker), the weights file carries both.
    """
    out = nifty.copy()
    if weights is None or weights.empty:
        out["weight_pct"] = pd.NA
        return out

    matched = pd.Series(float("nan"), index=out.index, dtype="float64")
    for left, right in WEIGHT_KEYS:
        if left not in out.columns or right not in weights.columns:
            continue
        lookup = dict(zip(weights[right].str.upper(), weights["weight_pct"]))
        cand = out[left].astype(str).str.strip().str.upper().map(lookup)
        matched = matched.where(matched.notna(), cand)
    out["weight_pct"] = matched

    if verbose:
        n_hit = int(matched.notna().sum())
        covered = float(matched.sum(skipna=True))
        print(f"[info] index weights: {n_hit} of {len(out)} members matched, "
              f"{covered:.2f}% of index weight covered "
              f"(source {weights['source'].replace('', pd.NA).dropna().iloc[0] if len(weights) else '?'}, "
              f"as of {weights['asof'].replace('', pd.NA).dropna().max() or '?'})")
        gaps = weights[weights["weight_pct"].isna()]
        if not gaps.empty:
            print(f"[warn] {len(gaps)} member(s) have no published weight and are "
                  f"excluded from the weighted mean: "
                  f"{', '.join(gaps['nse_symbol'].head(10))}", file=sys.stderr)
    return out


# --------------------------------------------------------------------------- #
# kdb+                                                                         #
# --------------------------------------------------------------------------- #

def connect(host: str, port: int):
    if kx is None:  # pragma: no cover
        sys.exit("pykx is not installed -- run:  pip install pykx")
    kwargs = {"host": host, "port": port, "timeout": KDB_TIMEOUT}
    if KDB_USER is not None:
        kwargs["username"] = KDB_USER
    if KDB_PASS is not None:
        kwargs["password"] = KDB_PASS
    return kx.SyncQConnection(**kwargs)


def resolve_date(conn) -> dt.date:
    """Last business day, computed server-side -- identical to temp.q:

        {d:x-1; while[(d mod 7) in 0 1; d-:1]; d} .z.D
    """
    return conn("{d:x-1; while[(d mod 7) in 0 1; d-:1]; d} .z.D").py()


def _sym_vector(values):
    """Python list of str -> q symbol vector (a bare list would become chars)."""
    try:
        return kx.toq(list(values), ktype=kx.SymbolVector)
    except Exception:
        return kx.SymbolVector(list(values))


def fetch_universe(conn, date: dt.date, isins: list[str]) -> list[str]:
    """temp.q, parameterised on the ISIN list."""
    like = " | ".join(f'(sym like "{p}")' for p in SYM_SUFFIXES)
    if isins:
        qry = (
            f"{{[d;isins] exec distinct sym from {EQUITY_TABLE} "
            f"where date=d, {like}, ID_ISIN in isins}}"
        )
        res = conn(qry, date, _sym_vector(isins))
    else:
        qry = (
            f"{{[d] exec distinct sym from {EQUITY_TABLE} "
            f"where date=d, {like}}}"
        )
        res = conn(qry, date)
    return [str(s) for s in res.py()]


# The joins are folded left explicitly, one `lj` per statement.  Written as
# `base lj pre lj cls` q would read it right-to-left as `base lj (pre lj cls)`,
# and a sym with a close print but no pre print would lose its close price.
#
# Column names are spelled out in the query itself rather than renamed on the
# pandas side, so what you read here is what lands in the csv.
_PRICES_Q = """
{{[d;syms]
  pre: select px_pre_close: last price, time_pre_close: last time,
              n_trades_pre_close: count i
       by sym from {tbl}
       where date=d, sym in syms, time < {t1}, not null price{typ};
  cls: select px_close: first price, time_close: first time,
              n_trades_close: count i
       by sym from {tbl}
       where date=d, sym in syms, time within ({t2a};{t2b}), not null price{typ};
  ref: select px_cas_reference: size wavg price, vol_ref_window: sum size,
              n_trades_ref_window: count i
       by sym from {tbl}
       where date=d, sym in syms, time >= {tr1}, time < {tr2},
             not null price, not null size{typ};
  pst: select vol_after_cutoff: sum size, vwap_after_cutoff: size wavg price,
              n_trades_after_cutoff: count i
       by sym from {tbl}
       where date=d, sym in syms, time >= {tp},
             not null price, not null size{typ};
  r: `sym xkey ([] sym:syms);
  r: r lj pre;
  r: r lj cls;
  r: r lj ref;
  r: r lj pst;
  0!r }}
"""


def prices_query() -> str:
    """The q text, with every window substituted -- handy for review."""
    return _PRICES_Q.format(
        tbl=QATT_TABLE,
        t1=CUTOFF_CONTINUOUS,
        t2a=CLOSE_WINDOW_START,
        t2b=CLOSE_WINDOW_END,
        tr1=REF_WINDOW_START,
        tr2=REF_WINDOW_END,
        tp=POST_VOLUME_FROM,
        typ=(", typ in `" + "`".join(TYP_FILTER)) if TYP_FILTER else "",
    )


def fetch_prices(conn, date: dt.date, syms: list[str]) -> pd.DataFrame:
    qry = prices_query()

    frames = []
    for i in range(0, len(syms), SYM_CHUNK):
        chunk = syms[i:i + SYM_CHUNK]
        frames.append(conn(qry, date, _sym_vector(chunk)).pd())
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


# --------------------------------------------------------------------------- #
# Report                                                                       #
# --------------------------------------------------------------------------- #

def build_report(
    df: pd.DataFrame, date: dt.date, nifty: pd.DataFrame | None = None
) -> pd.DataFrame:
    df = df.copy()
    df["sym"] = df["sym"].astype(str)
    df.insert(0, "date", date)

    for col in ("n_trades_pre_close", "n_trades_close", "n_trades_ref_window",
                "n_trades_after_cutoff"):
        df[col] = df[col].fillna(0).astype("int64")
    for col in ("vol_ref_window", "vol_after_cutoff"):
        df[col] = df[col].fillna(0.0)

    df["move_price"] = df["px_close"] - df["px_pre_close"]
    # Ticks are taken off the price the move starts from, so a name that crosses
    # a band boundary during the move is still measured in the tick it was
    # trading in.  The close price stands in when there is no pre price.
    df["tick_size"] = df["px_pre_close"].where(
        df["px_pre_close"].notna(), df["px_close"]).map(tick_size)
    df["move_ticks"] = (df["move_price"] / df["tick_size"]).round(2)
    df["move_bps"] = (df["px_close"] / df["px_pre_close"] - 1.0) * 10_000.0
    # How far the close print sits from the CAS reference price.  The exchange
    # band is +/-3%, i.e. +/-300 bps, so this is directly comparable to it.
    df["close_vs_reference_bps"] = (
        df["px_close"] / df["px_cas_reference"] - 1.0) * 10_000.0
    df["move_direction"] = df["move_price"].apply(
        lambda m: "" if pd.isna(m) else ("up" if m > 0 else ("down" if m < 0 else "flat"))
    )
    df["status"] = [
        "ok" if (nb and na) else
        "no_pre_price" if (na and not nb) else
        "no_close_price" if (nb and not na) else
        "no_data"
        for nb, na in zip(df["n_trades_pre_close"] > 0, df["n_trades_close"] > 0)
    ]

    # `weight_pct` is the published index weight, carried in from
    # config/nifty50_weights.csv by `attach_weights`.  It is not derived here:
    # anything computable from the reference data we hold is full market cap
    # rather than free float, which would be an approximation dressed up as a
    # fact.  Empty for non-members, and for a member whose weight the source did
    # not publish.
    if nifty is not None and not nifty.empty:
        cols = [c for c in ("sym", "bbg_ticker", "name", "weight_pct")
                if c in nifty.columns]
        df = df.merge(nifty[cols], on="sym", how="left")
        # Carried in both studies, so the whole-universe file is self-sufficient:
        # you can pull the subset out of it without needing the other file.
        df["in_nifty50"] = df["sym"].isin(set(nifty["sym"]))
    else:
        df["in_nifty50"] = pd.NA
    for col in ("bbg_ticker", "name", "weight_pct"):
        if col not in df.columns:
            df[col] = pd.NA
    df = df.rename(columns={"name": "company_name",
                            "weight_pct": "nifty50_weight_pct"})

    return df[[
        "date", "sym", "bbg_ticker", "company_name", "in_nifty50",
        "nifty50_weight_pct", "status", "move_direction",
        "px_pre_close", "time_pre_close", "n_trades_pre_close",
        "px_close", "time_close", "n_trades_close",
        "px_cas_reference", "vol_ref_window", "n_trades_ref_window",
        "vol_after_cutoff", "vwap_after_cutoff", "n_trades_after_cutoff",
        "move_price", "tick_size", "move_ticks", "move_bps",
        "close_vs_reference_bps",
    ]].sort_values("sym", ignore_index=True)


def print_summary(rep: pd.DataFrame, date: dt.date, label: str = "") -> None:
    ok = rep[rep["status"] == "ok"]
    print()
    print("=" * 72)
    print(f"  {label or 'study'}")
    print("=" * 72)
    print(f"  date                 : {date}")
    print(f"  end of continuous    : last price before  {CUTOFF_CONTINUOUS} HKT")
    print(f"  close                : first price within {CLOSE_WINDOW_START}"
          f" - {CLOSE_WINDOW_END} HKT")
    print(f"  reference price      : size wavg price over {REF_WINDOW_START}"
          f" - {REF_WINDOW_END} HKT (15:00-15:15 IST)")
    print(f"  post volume          : from {POST_VOLUME_FROM} HKT to end of day")
    print(f"  universe             : {len(rep)} syms")
    print(f"  both prices found    : {len(ok)}")
    for status, n in rep["status"].value_counts().items():
        if status != "ok":
            print(f"    {status:<18} : {n}")

    vol_ref = rep["vol_ref_window"].sum()
    vol_post = rep["vol_after_cutoff"].sum()
    print()
    label_ref = f"volume {REF_WINDOW_START[:5]}-{REF_WINDOW_END[:5]}"
    label_post = f"volume {POST_VOLUME_FROM[:5]} onward"
    print(f"  {label_ref:<21}: {vol_ref:,.0f}")
    print(f"  {label_post:<21}: {vol_post:,.0f}")
    if vol_ref:
        print(f"  {'post / reference':<21}: {vol_post / vol_ref:.2f}x")
    n_ref = int((rep["n_trades_ref_window"] > 0).sum())
    if n_ref < len(rep):
        print(f"  {'no reference price':<21}: {len(rep) - n_ref} sym(s) "
              f"had no print in the window")

    if ok.empty:
        print("\n  no sym has both prices -- nothing to summarise")
        return

    bps = ok["move_bps"]
    ticks = pd.to_numeric(ok.get("move_ticks"), errors="coerce")
    print()
    print(f"  mean move            : {bps.mean():+.2f} bps   ({ticks.mean():+.2f} ticks)")
    print(f"  median move          : {bps.median():+.2f} bps   ({ticks.median():+.2f} ticks)")
    print(f"  mean |move|          : {bps.abs().mean():.2f} bps   ({ticks.abs().mean():.2f} ticks)")
    print(f"  up / down / flat     : {(bps > 0).sum()} / {(bps < 0).sum()} / {(bps == 0).sum()}")

    # What the index did, as opposed to what the average name did.
    w = pd.to_numeric(ok.get("nifty50_weight_pct"), errors="coerce")
    if w is not None and w.notna().any():
        m = w.notna() & bps.notna()
        wsum = w[m].sum()
        if wsum:
            print(f"  index-weighted move  : {(bps[m] * w[m]).sum() / wsum:+.2f} bps "
                  f"over {int(m.sum())} weighted names ({wsum:.2f}% of index weight)")

    ref_bps = pd.to_numeric(ok.get("close_vs_reference_bps"), errors="coerce")
    if ref_bps is not None and ref_bps.notna().any():
        outside = int((ref_bps.abs() > 300).sum())
        print(f"  close vs reference   : {ref_bps.mean():+.2f} bps mean, "
              f"{ref_bps.abs().max():.0f} bps max"
              + (f", {outside} outside the +/-3% band" if outside else ""))

    top = ok.reindex(bps.abs().sort_values(ascending=False).index).head(10)
    print("\n  10 largest moves")
    print(f"    {'sym':<18}{'pxPre':>12}{'pxClose':>12}{'refPx':>12}{'ticks':>9}{'bps':>10}")
    for r in top.itertuples():
        ref = f"{r.px_cas_reference:>12.4f}" if pd.notna(r.px_cas_reference) else f"{'-':>12}"
        tks = f"{r.move_ticks:>+9.1f}" if pd.notna(r.move_ticks) else f"{'-':>9}"
        print(f"    {r.sym:<18}{r.px_pre_close:>12.4f}{r.px_close:>12.4f}{ref}{tks}{r.move_bps:>+10.1f}")


def print_comparison(studies: list[tuple[str, str, pd.DataFrame]]) -> None:
    """Side by side, so the subset can be read against the whole book."""
    print()
    print("=" * 72)
    print("  side by side")
    print("=" * 72)
    print(f"  {'study':<12}{'syms':>7}{'both px':>9}{'mean bps':>11}"
          f"{'|mean| bps':>12}{'vol ref':>16}{'vol post':>16}")
    for key, _label, rep in studies:
        ok = rep[rep["status"] == "ok"]
        bps = ok["move_bps"]
        mean = f"{bps.mean():+.2f}" if len(bps) else "-"
        amean = f"{bps.abs().mean():.2f}" if len(bps) else "-"
        print(f"  {key:<12}{len(rep):>7}{len(ok):>9}{mean:>11}{amean:>12}"
              f"{rep['vol_ref_window'].sum():>16,.0f}{rep['vol_after_cutoff'].sum():>16,.0f}")


# --------------------------------------------------------------------------- #

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default=KDB_HOST)
    ap.add_argument("--port", type=int, default=KDB_PORT)
    ap.add_argument("--date", help="YYYY-MM-DD; default = last business day (server side)")
    ap.add_argument("--isin-file", default=ISIN_FILE)
    ap.add_argument("--no-isin-filter", action="store_true",
                    help="take every .IN sym instead of the CAS ISIN list")
    ap.add_argument("--nifty-file", default=NIFTY_FILE,
                    help="NIFTY 50 members with their kdb sym (see tools/)")
    ap.add_argument("--weights-file", default=WEIGHTS_FILE,
                    help="NIFTY 50 index weights (nse_symbol, isin, weight_pct); "
                         "absent means the study runs without a weighted mean")
    ap.add_argument("--scope", choices=("both", "universe", "nifty"), default="both",
                    help="which studies to produce (default: both)")
    ap.add_argument("--out-dir", default=OUTPUT_DIR,
                    help="where the csv files go (default: output/)")
    ap.add_argument("--out", help="output csv path; only valid for a single scope")
    ap.add_argument("--qatt-table", default=QATT_TABLE,
                    help=f"market-data table name (default: {QATT_TABLE})")
    ap.add_argument("--equity-table", default=EQUITY_TABLE,
                    help=f"reference table name (default: {EQUITY_TABLE})")
    ap.add_argument("--print-query", action="store_true",
                    help="print the q query and exit, without connecting")
    args = ap.parse_args()

    # The queries read these at call time, so rebinding here is enough.
    globals()["QATT_TABLE"] = args.qatt_table
    globals()["EQUITY_TABLE"] = args.equity_table

    if args.print_query:
        print(prices_query())
        return 0

    if args.out and args.scope == "both":
        print("[fatal] --out names one file, but --scope both writes two. Use "
              "--out-dir, or pick a single --scope.", file=sys.stderr)
        return 2

    # The subset lives inside the universe, so a missing NIFTY file only costs
    # the subset study -- the universe one still runs.  Asking for it by name is
    # a different matter and is a hard error.
    nifty: pd.DataFrame | None = None
    if args.scope in ("both", "nifty"):
        nifty = load_nifty50(args.nifty_file, required=(args.scope == "nifty"))
        if nifty is not None:
            print(f"[info] {len(nifty)} NIFTY 50 members with a sym loaded from "
                  f"{os.path.basename(args.nifty_file)}")
            weights = load_weights(args.weights_file)
            if weights is None:
                print(f"[warn] {args.weights_file} not found -- no index weights, "
                      f"so no weighted mean move", file=sys.stderr)
            nifty = attach_weights(nifty, weights)
    if args.scope == "nifty" and nifty is None:
        return 2

    isins: list[str] = []
    if not args.no_isin_filter:
        isins = load_isins(args.isin_file)
        if not isins:
            print(
                f"No ISIN found in {args.isin_file}.\n"
                f"Paste the CAS ISIN list into that file (the raw `INE...`INE... "
                f"form from temp.q is fine),\nor pass --no-isin-filter to run on "
                f"every .IN sym.",
                file=sys.stderr,
            )
            return 2
        print(f"[info] {len(isins)} distinct ISINs loaded from {os.path.basename(args.isin_file)}")

    with connect(args.host, args.port) as conn:
        date = (dt.date.fromisoformat(args.date) if args.date else resolve_date(conn))
        print(f"[info] connected to {args.host}:{args.port}, date = {date}")

        syms = fetch_universe(conn, date, isins)
        print(f"[info] {len(syms)} syms in the CAS universe")
        if not syms:
            print("[warn] empty universe -- check the date and the ISIN list", file=sys.stderr)
            return 1

        # The NIFTY 50 study is a subset of the universe, so the prices are
        # pulled once and sliced afterwards.  Restricting the query up front
        # would mean a second round trip for no extra information.
        if args.scope == "nifty" and nifty is not None:
            wanted = set(nifty["sym"])
            syms = [s for s in syms if s in wanted]
            if not syms:
                print("[warn] no NIFTY 50 sym survived the CAS filter -- check that "
                      "the syms in the nifty file match the equity table",
                      file=sys.stderr)
                return 1

        raw = fetch_prices(conn, date, syms)

    universe_syms = set(syms)
    if nifty is not None:
        absent = sorted(set(nifty["sym"]) - universe_syms)
        if absent:
            print(
                f"[warn] {len(absent)} NIFTY 50 sym(s) are not in the CAS universe "
                f"for {date} and are absent from the subset study: "
                f"{', '.join(absent[:10])}{' ...' if len(absent) > 10 else ''}",
                file=sys.stderr,
            )

    # -- build the studies ------------------------------------------------- #
    studies: list[tuple[str, str, pd.DataFrame]] = []   # (key, label, frame)

    if args.scope in ("both", "universe"):
        studies.append((
            "universe",
            f"CAS universe -- {len(universe_syms)} syms",
            build_report(raw, date, nifty),
        ))

    if nifty is not None and args.scope in ("both", "nifty"):
        subset = raw[raw["sym"].astype(str).isin(set(nifty["sym"]))]
        studies.append((
            "nifty50",
            f"NIFTY 50 subset -- {subset['sym'].nunique()} of "
            f"{len(nifty)} members in the CAS universe",
            build_report(subset, date, nifty),
        ))

    if not studies:
        print("[warn] nothing to do", file=sys.stderr)
        return 1

    # -- write ------------------------------------------------------------- #
    written = []
    for key, label, rep in studies:
        if args.out and len(studies) == 1:
            out = args.out
        else:
            out = os.path.join(args.out_dir, f"cas_price_move_{key}_{date:%Y%m%d}.csv")
        os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
        rep.to_csv(out, index=False, float_format="%.6f")
        written.append((label, out))
        print_summary(rep, date, label)

    if len(studies) > 1:
        print_comparison(studies)

    print()
    for label, out in written:
        print(f"  written -> {out}")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())

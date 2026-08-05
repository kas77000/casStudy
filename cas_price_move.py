#!/usr/bin/env python3
"""
India CAS -- price move between the end of continuous and the close.

Step 1: pull the CAS universe from the `equity` reference table (same query as
        temp.q: last business day + .IN/.IS/.IB syms + ISIN whitelist), then
        narrow it to the NIFTY 50 constituents listed in config/nifty50.csv.
Step 2: for every one of those syms, pull out of `qatt_17034`:
          pxPre         -> last price strictly before CUTOFF_CONTINUOUS
          pxClose       -> first price inside [CLOSE_WINDOW_START; CLOSE_WINDOW_END]
          closeRefPrice -> size wavg price over [REF_WINDOW_START; REF_WINDOW_END)
          volRef        -> volume over that same reference window
          volPost       -> volume from POST_VOLUME_FROM onward
Step 3: write a csv with the move (absolute / bps) and print a summary.

`closeRefPrice` is the exchange's CAS reference price: the 15:00-15:15 IST VWAP,
i.e. 17:30-17:45 HKT.  Every CAS order has to sit within +/-3% (=300 bps) of it,
so `closeVsRefBps` in the output says how close the print came to the band edge.

The NIFTY 50 file is produced by two scripts, in this order:
    tools/bloomberg_nifty50.py    on the Bloomberg machine  -> members + weights
    tools/map_nifty50_syms.py     on the kdb machine        -> adds the `sym`
Pass --no-nifty-filter to run on the full CAS universe instead.

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
QATT_TABLE = "qatt_17034"      # QATT-HT/qatt.csv

# sym suffixes that identify the Indian listings in the ref table
SYM_SUFFIXES = ("*.IN", "*.IS", "*.IB")

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
OUTPUT_DIR = os.path.join(PROJECT_DIR, "output")

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

def load_nifty50(path: str) -> pd.DataFrame:
    """Read config/nifty50.csv and return the rows that carry a resolved `sym`.

    The file is the hand-off between the two helper scripts: Bloomberg supplies
    the members and weights, the kdb side supplies `sym`.  A file with an empty
    `sym` column means the second script has not run yet, which is worth an
    explicit error -- filtering on nothing would silently produce an empty
    report.
    """
    if not os.path.exists(path):
        raise SystemExit(
            f"[fatal] {path} not found.\n"
            f"        Build it in two steps:\n"
            f"          1. on the Bloomberg machine:\n"
            f"             python tools/bloomberg_nifty50.py --out {path}\n"
            f"          2. copy it across, then on the kdb machine:\n"
            f"             python tools/map_nifty50_syms.py --file {path}\n"
            f"        Or pass --no-nifty-filter to run on the whole CAS universe."
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
    if "weight_pct" in resolved.columns:
        resolved["weight_pct"] = pd.to_numeric(resolved["weight_pct"], errors="coerce")
    else:
        resolved["weight_pct"] = float("nan")

    unmapped = len(df) - len(resolved)
    if unmapped:
        print(
            f"[warn] {unmapped} of {len(df)} NIFTY members in "
            f"{os.path.basename(path)} have no sym and were skipped",
            file=sys.stderr,
        )
    return resolved.drop_duplicates(subset=["sym"]).reset_index(drop=True)


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
# and a sym with a close print but no pre print would lose its pxClose.
_PRICES_Q = """
{{[d;syms]
  pre: select pxPre: last price, tPre: last time, nPre: count i
       by sym from {tbl}
       where date=d, sym in syms, time < {t1}, not null price{typ};
  cls: select pxClose: first price, tClose: first time, nClose: count i
       by sym from {tbl}
       where date=d, sym in syms, time within ({t2a};{t2b}), not null price{typ};
  ref: select closeRefPrice: size wavg price, volRef: sum size, nRef: count i
       by sym from {tbl}
       where date=d, sym in syms, time >= {tr1}, time < {tr2},
             not null price, not null size{typ};
  pst: select volPost: sum size, vwapPost: size wavg price, nPost: count i
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

    for col in ("nPre", "nClose", "nRef", "nPost"):
        df[col] = df[col].fillna(0).astype("int64")
    for col in ("volRef", "volPost"):
        df[col] = df[col].fillna(0.0)

    df["move"] = df["pxClose"] - df["pxPre"]
    df["moveBps"] = (df["pxClose"] / df["pxPre"] - 1.0) * 10_000.0
    # How far the close print sits from the CAS reference price.  The exchange
    # band is +/-3%, i.e. +/-300 bps, so this is directly comparable to it.
    df["closeVsRefBps"] = (df["pxClose"] / df["closeRefPrice"] - 1.0) * 10_000.0
    df["direction"] = df["move"].apply(
        lambda m: "" if pd.isna(m) else ("up" if m > 0 else ("down" if m < 0 else "flat"))
    )
    df["status"] = [
        "ok" if (nb and na) else
        "no_pre_price" if (na and not nb) else
        "no_close_price" if (nb and not na) else
        "no_data"
        for nb, na in zip(df["nPre"] > 0, df["nClose"] > 0)
    ]

    if nifty is not None and not nifty.empty:
        cols = [c for c in ("sym", "weight_pct", "bbg_ticker", "name") if c in nifty.columns]
        df = df.merge(nifty[cols], on="sym", how="left")
    for col in ("weight_pct", "bbg_ticker", "name"):
        if col not in df.columns:
            df[col] = pd.NA

    return df[[
        "date", "sym", "bbg_ticker", "name", "weight_pct", "status", "direction",
        "pxPre", "tPre", "nPre",
        "pxClose", "tClose", "nClose",
        "closeRefPrice", "volRef", "nRef",
        "volPost", "vwapPost", "nPost",
        "move", "moveBps", "closeVsRefBps",
    ]].sort_values("sym", ignore_index=True)


def print_summary(rep: pd.DataFrame, date: dt.date) -> None:
    ok = rep[rep["status"] == "ok"]
    print()
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

    vol_ref = rep["volRef"].sum()
    vol_post = rep["volPost"].sum()
    print()
    label_ref = f"volume {REF_WINDOW_START[:5]}-{REF_WINDOW_END[:5]}"
    label_post = f"volume {POST_VOLUME_FROM[:5]} onward"
    print(f"  {label_ref:<21}: {vol_ref:,.0f}")
    print(f"  {label_post:<21}: {vol_post:,.0f}")
    if vol_ref:
        print(f"  {'post / reference':<21}: {vol_post / vol_ref:.2f}x")
    n_ref = int((rep["nRef"] > 0).sum())
    if n_ref < len(rep):
        print(f"  {'no reference price':<21}: {len(rep) - n_ref} sym(s) "
              f"had no print in the window")

    if ok.empty:
        print("\n  no sym has both prices -- nothing to summarise")
        return

    bps = ok["moveBps"]
    print()
    print(f"  mean move            : {bps.mean():+.2f} bps")
    print(f"  median move          : {bps.median():+.2f} bps")
    print(f"  mean |move|          : {bps.abs().mean():.2f} bps")
    print(f"  up / down / flat     : {(bps > 0).sum()} / {(bps < 0).sum()} / {(bps == 0).sum()}")

    w = pd.to_numeric(ok.get("weight_pct"), errors="coerce")
    if w is not None and w.notna().any() and w.sum():
        print(f"  index-weighted move  : {(bps * w).sum() / w.sum():+.2f} bps"
              f"   ({w.sum():.1f}% of the index covered)")

    ref_bps = pd.to_numeric(ok.get("closeVsRefBps"), errors="coerce")
    if ref_bps is not None and ref_bps.notna().any():
        outside = int((ref_bps.abs() > 300).sum())
        print(f"  close vs reference   : {ref_bps.mean():+.2f} bps mean, "
              f"{ref_bps.abs().max():.0f} bps max"
              + (f", {outside} outside the +/-3% band" if outside else ""))

    top = ok.reindex(bps.abs().sort_values(ascending=False).index).head(10)
    print("\n  10 largest moves")
    print(f"    {'sym':<18}{'pxPre':>12}{'pxClose':>12}{'refPx':>12}{'bps':>10}")
    for r in top.itertuples():
        ref = f"{r.closeRefPrice:>12.4f}" if pd.notna(r.closeRefPrice) else f"{'-':>12}"
        print(f"    {r.sym:<18}{r.pxPre:>12.4f}{r.pxClose:>12.4f}{ref}{r.moveBps:>+10.1f}")


# --------------------------------------------------------------------------- #

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default=KDB_HOST)
    ap.add_argument("--port", type=int, default=KDB_PORT)
    ap.add_argument("--date", help="YYYY-MM-DD; default = last business day (server side)")
    ap.add_argument("--isin-file", default=ISIN_FILE)
    ap.add_argument("--no-isin-filter", action="store_true",
                    help="take every .IN/.IS/.IB sym instead of the CAS ISIN list")
    ap.add_argument("--nifty-file", default=NIFTY_FILE,
                    help="NIFTY 50 members with their kdb sym (see tools/)")
    ap.add_argument("--no-nifty-filter", action="store_true",
                    help="run on the whole CAS universe instead of the NIFTY 50 subset")
    ap.add_argument("--out", help="output csv path")
    ap.add_argument("--print-query", action="store_true",
                    help="print the q query and exit, without connecting")
    args = ap.parse_args()

    if args.print_query:
        print(prices_query())
        return 0

    nifty: pd.DataFrame | None = None
    if not args.no_nifty_filter:
        nifty = load_nifty50(args.nifty_file)
        print(f"[info] {len(nifty)} NIFTY 50 members with a sym loaded from "
              f"{os.path.basename(args.nifty_file)}")

    isins: list[str] = []
    if not args.no_isin_filter:
        isins = load_isins(args.isin_file)
        if not isins:
            print(
                f"No ISIN found in {args.isin_file}.\n"
                f"Paste the CAS ISIN list into that file (the raw `INE...`INE... "
                f"form from temp.q is fine),\nor pass --no-isin-filter to run on "
                f"every .IN/.IS/.IB sym.",
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

        if nifty is not None:
            wanted = set(nifty["sym"])
            kept = [s for s in syms if s in wanted]
            absent = sorted(wanted - set(syms))
            if absent:
                print(
                    f"[warn] {len(absent)} NIFTY 50 sym(s) are not in the CAS "
                    f"universe for {date} and were dropped: "
                    f"{', '.join(absent[:10])}{' ...' if len(absent) > 10 else ''}",
                    file=sys.stderr,
                )
            syms = kept
            print(f"[info] {len(syms)} syms after the NIFTY 50 filter")
            if not syms:
                print("[warn] no NIFTY 50 sym survived the CAS filter -- check that "
                      "the syms in the nifty file match the equity table",
                      file=sys.stderr)
                return 1

        raw = fetch_prices(conn, date, syms)

    rep = build_report(raw, date, nifty)

    suffix = "" if nifty is None else "_nifty50"
    out = args.out or os.path.join(
        OUTPUT_DIR, f"cas_price_move{suffix}_{date:%Y%m%d}.csv"
    )
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    rep.to_csv(out, index=False, float_format="%.6f")

    print_summary(rep, date)
    print(f"\n  written -> {out}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

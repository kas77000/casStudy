#!/usr/bin/env python3
"""
India CAS -- price move between the end of continuous and the close.

Step 1: pull the CAS universe from the `equity` reference table (same query as
        temp.q: last business day + .IN/.IS/.IB syms + ISIN whitelist).
Step 2: for every one of those syms, pull two prices out of `qatt_17034`:
          pxPre   -> last price strictly before CUTOFF_CONTINUOUS
          pxClose -> first price inside [CLOSE_WINDOW_START; CLOSE_WINDOW_END]
Step 3: write a csv with the move (absolute / bps) and print a summary.

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
    sys.exit("pykx is not installed -- run:  pip install pykx")


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

# Optional filter on the qatt `typ` column, e.g. ("trade",) to keep only
# trades.  None -> take every record that carries a non-null price.
TYP_FILTER: tuple[str, ...] | None = None

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
ISIN_FILE = os.path.join(PROJECT_DIR, "config", "cas_isins.txt")
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
# kdb+                                                                         #
# --------------------------------------------------------------------------- #

def connect(host: str, port: int) -> kx.SyncQConnection:
    kwargs = {"host": host, "port": port, "timeout": KDB_TIMEOUT}
    if KDB_USER is not None:
        kwargs["username"] = KDB_USER
    if KDB_PASS is not None:
        kwargs["password"] = KDB_PASS
    return kx.SyncQConnection(**kwargs)


def resolve_date(conn: kx.SyncQConnection) -> dt.date:
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


_PRICES_Q = """
{{[d;syms]
  pre: select pxPre: last price, tPre: last time, nPre: count i
       by sym from {tbl}
       where date=d, sym in syms, time < {t1}, not null price{typ};
  cls: select pxClose: first price, tClose: first time, nClose: count i
       by sym from {tbl}
       where date=d, sym in syms, time within ({t2a};{t2b}), not null price{typ};
  0!((`sym xkey ([] sym:syms)) lj pre lj cls) }}
"""


def fetch_prices(conn, date: dt.date, syms: list[str]) -> pd.DataFrame:
    qry = _PRICES_Q.format(
        tbl=QATT_TABLE,
        t1=CUTOFF_CONTINUOUS,
        t2a=CLOSE_WINDOW_START,
        t2b=CLOSE_WINDOW_END,
        typ=(", typ in `" + "`".join(TYP_FILTER)) if TYP_FILTER else "",
    )

    frames = []
    for i in range(0, len(syms), SYM_CHUNK):
        chunk = syms[i:i + SYM_CHUNK]
        frames.append(conn(qry, date, _sym_vector(chunk)).pd())
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


# --------------------------------------------------------------------------- #
# Report                                                                       #
# --------------------------------------------------------------------------- #

def build_report(df: pd.DataFrame, date: dt.date) -> pd.DataFrame:
    df = df.copy()
    df["sym"] = df["sym"].astype(str)
    df.insert(0, "date", date)

    for col in ("nPre", "nClose"):
        df[col] = df[col].fillna(0).astype("int64")

    df["move"] = df["pxClose"] - df["pxPre"]
    df["moveBps"] = (df["pxClose"] / df["pxPre"] - 1.0) * 10_000.0
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

    return df[[
        "date", "sym", "status", "direction",
        "pxPre", "tPre", "nPre",
        "pxClose", "tClose", "nClose",
        "move", "moveBps",
    ]].sort_values("sym", ignore_index=True)


def print_summary(rep: pd.DataFrame, date: dt.date) -> None:
    ok = rep[rep["status"] == "ok"]
    print()
    print(f"  date                 : {date}")
    print(f"  end of continuous    : last price before  {CUTOFF_CONTINUOUS} HKT")
    print(f"  close                : first price within {CLOSE_WINDOW_START}"
          f" - {CLOSE_WINDOW_END} HKT")
    print(f"  universe             : {len(rep)} syms")
    print(f"  both prices found    : {len(ok)}")
    for status, n in rep["status"].value_counts().items():
        if status != "ok":
            print(f"    {status:<18} : {n}")

    if ok.empty:
        print("\n  no sym has both prices -- nothing to summarise")
        return

    bps = ok["moveBps"]
    print()
    print(f"  mean move            : {bps.mean():+.2f} bps")
    print(f"  median move          : {bps.median():+.2f} bps")
    print(f"  mean |move|          : {bps.abs().mean():.2f} bps")
    print(f"  up / down / flat     : {(bps > 0).sum()} / {(bps < 0).sum()} / {(bps == 0).sum()}")

    top = ok.reindex(bps.abs().sort_values(ascending=False).index).head(10)
    print("\n  10 largest moves")
    print(f"    {'sym':<18}{'pxPre':>12}{'pxClose':>12}{'bps':>10}")
    for r in top.itertuples():
        print(f"    {r.sym:<18}{r.pxPre:>12.4f}{r.pxClose:>12.4f}{r.moveBps:>+10.1f}")


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
    ap.add_argument("--out", help="output csv path")
    args = ap.parse_args()

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
        print(f"[info] {len(syms)} syms in the universe")
        if not syms:
            print("[warn] empty universe -- check the date and the ISIN list", file=sys.stderr)
            return 1

        raw = fetch_prices(conn, date, syms)

    rep = build_report(raw, date)

    out = args.out or os.path.join(OUTPUT_DIR, f"cas_price_move_{date:%Y%m%d}.csv")
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    rep.to_csv(out, index=False, float_format="%.6f")

    print_summary(rep, date)
    print(f"\n  written -> {out}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Fill in the kdb `sym` for every Bloomberg code in the NIFTY 50 file.

**Runs on the kdb machine**, as the second half of the hand-off from
`tools/bloomberg_nifty50.py`.  It reads that script's CSV, resolves each member
against the `equity` reference table, and writes the `sym` and `sym_match_rule`
columns back into the same file.

    python tools/map_nifty50_syms.py                       # config/nifty50.csv, in place
    python tools/map_nifty50_syms.py --file config/nifty50.csv --date 2026-08-04
    python tools/map_nifty50_syms.py --equity-csv dump/equity.csv   # no kdb needed

Matching is a waterfall, most reliable first, and the rule that won is recorded
per row so a doubtful mapping is visible rather than buried:

    1. ISIN            ID_ISIN == isin
    2. BBG code        sym_blp / sym_blp_prm / sym_bpipe == the member ticker
    3. ticker + exch   TICKER == ticker and COMPOSITE_EXCH_CODE == exchange code
    4. ticker only     TICKER == ticker, and only if it resolves to one listing

A member that matches several listings (a dual .IN/.IB line, say) is resolved by
the suffix preference in `SYM_PREFERENCE`; every candidate is still written to
`sym_candidates`, so nothing is silently dropped.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from casretro import config as C  # noqa: E402
from casretro import kdbio as K  # noqa: E402

#: When one Bloomberg code maps to several listings, keep this one.
#: `.IN` first because the NIFTY 50 is an NSE index.
SYM_PREFERENCE = ("IN", "IS", "IB")

#: Columns pulled from `equity` for the mapping.
EQUITY_MAP_COLS = [
    "sym", "ID_ISIN", "TICKER", "COMPOSITE_EXCH_CODE", "EQY_PRIM_EXCH_SHRT",
    "sym_blp", "sym_blp_prm", "sym_bpipe", "sym_wombat", "NAME", "CRNCY",
]

#: `equity` columns that hold a Bloomberg-style code.
BBG_COLS = ("sym_blp", "sym_blp_prm", "sym_bpipe")

_YELLOW_KEYS = {"equity", "index", "curncy", "comdty", "corp", "govt", "mtge", "pfd"}


# --------------------------------------------------------------------------- #
# Normalisation                                                                #
# --------------------------------------------------------------------------- #

def norm_bbg(v) -> str:
    """`RELIANCE IN Equity` / `reliance  in` -> `RELIANCE IN`."""
    s = str(v or "").strip()
    if not s:
        return ""
    parts = s.split()
    if parts and parts[-1].lower() in _YELLOW_KEYS:
        parts = parts[:-1]
    return " ".join(p.upper() for p in parts)


def norm_plain(v) -> str:
    return str(v or "").strip().upper()


def _suffix_rank(sym: str) -> int:
    suffix = str(sym).rsplit(".", 1)[-1].upper()
    return SYM_PREFERENCE.index(suffix) if suffix in SYM_PREFERENCE else len(SYM_PREFERENCE)


def pick(candidates: list[str]) -> str:
    """Prefer the primary listing, then alphabetical, so runs are reproducible."""
    if not candidates:
        return ""
    return sorted(set(candidates), key=lambda s: (_suffix_rank(s), s))[0]


# --------------------------------------------------------------------------- #
# equity reference data                                                        #
# --------------------------------------------------------------------------- #

def load_equity_from_kdb(date: dt.date | None, instances_file: str) -> pd.DataFrame:
    K.require_pykx()
    instances = C.load_instances(instances_file)
    inst = C.resolve(instances, "ref", "ht")

    with K.connect(inst) as conn:
        if date is None:
            from casretro.universe import last_business_day
            date = last_business_day(conn)
            print(f"[info] date = {date} (last business day, resolved server side)")

        tbl = inst.table("equity")
        have = set(conn.columns_of(tbl))
        cols = [c for c in EQUITY_MAP_COLS if c in have]
        if "sym" not in cols:
            raise SystemExit(f"[fatal] {tbl} has no `sym` column")
        missing = [c for c in EQUITY_MAP_COLS if c not in have]
        if missing:
            print(f"[warn] {tbl} has no {', '.join(missing)} -- those match rules "
                  f"will be skipped", file=sys.stderr)

        like = " | ".join(f'(sym like "{p}")' for p in C.SYM_SUFFIXES)
        where_d = K.where_date(inst)
        qry = K.q_lambda(
            ["d"] if inst.partitioned else [],
            f"select {', '.join(cols)} from {tbl} where {where_d}({like})",
        )
        return conn.query_pd(qry, *K.date_params(inst, date))


def load_equity_from_csv(path: str) -> pd.DataFrame:
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    if list(df.columns[:4]) == ["c", "t", "f", "a"]:
        raise SystemExit(
            f"[fatal] {path} is a *schema* dump (columns c,t,f,a), not table data.\n"
            f"        Export the rows instead, e.g. in q:\n"
            f"          `:{os.path.basename(path)} 0: csv 0: select "
            f"{', '.join(EQUITY_MAP_COLS)} from equity where date=last date\n"
            f"        or drop --equity-csv and let the script query kdb directly."
        )
    if "sym" not in df.columns:
        raise SystemExit(f"[fatal] {path} has no `sym` column")
    return df


# --------------------------------------------------------------------------- #
# Matching                                                                     #
# --------------------------------------------------------------------------- #

def build_indexes(eq: pd.DataFrame) -> dict[str, dict[str, list[str]]]:
    """Lookup tables keyed by ISIN, Bloomberg code, ticker+exch and ticker."""
    idx: dict[str, dict[str, list[str]]] = {
        "isin": {}, "bbg": {}, "ticker_exch": {}, "ticker": {}
    }

    def add(bucket: str, key: str, sym: str) -> None:
        if key:
            idx[bucket].setdefault(key, []).append(sym)

    for r in eq.to_dict("records"):
        sym = str(r.get("sym", "")).strip()
        if not sym:
            continue
        add("isin", norm_plain(r.get("ID_ISIN")), sym)
        for col in BBG_COLS:
            add("bbg", norm_bbg(r.get(col)), sym)
        ticker = norm_plain(r.get("TICKER"))
        exch = norm_plain(r.get("COMPOSITE_EXCH_CODE"))
        add("ticker_exch", f"{ticker} {exch}".strip(), sym)
        add("ticker", ticker, sym)
    return idx


def match_row(row: dict, idx: dict) -> tuple[str, str, list[str]]:
    """-> (sym, rule, all candidates).  Empty sym means no rule fired."""
    isin = norm_plain(row.get("isin"))
    if isin:
        hits = idx["isin"].get(isin, [])
        if hits:
            return pick(hits), "isin", hits

    member = norm_bbg(row.get("bbg_member") or row.get("bbg_ticker"))
    if member:
        hits = idx["bbg"].get(member, [])
        if hits:
            return pick(hits), "bbg_code", hits

    ticker = norm_plain(row.get("ticker"))
    if not ticker and member:
        ticker = member.split()[0]
    exch = norm_plain(row.get("composite_exch_code"))
    if not exch and member and len(member.split()) > 1:
        exch = member.split()[1]

    if ticker and exch:
        hits = idx["ticker_exch"].get(f"{ticker} {exch}", [])
        if hits:
            return pick(hits), "ticker_exch", hits

    if ticker:
        hits = idx["ticker"].get(ticker, [])
        if len(set(hits)) == 1:
            return hits[0], "ticker", hits
        if hits:
            # ambiguous: report every candidate but do not guess
            return "", "ticker_ambiguous", hits

    return "", "", []


# --------------------------------------------------------------------------- #

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--file", default=os.path.join("config", "nifty50.csv"),
                    help="the CSV produced by tools/bloomberg_nifty50.py")
    ap.add_argument("--out", help="write here instead of updating --file in place")
    ap.add_argument("--date", help="YYYY-MM-DD for the equity snapshot; "
                                   "default = last business day")
    ap.add_argument("--equity-csv", help="a local dump of the equity table, "
                                         "instead of querying kdb")
    ap.add_argument("--instances", default=C.INSTANCES_FILE)
    ap.add_argument("--fail-on-unmatched", action="store_true",
                    help="exit non-zero if any member could not be mapped")
    args = ap.parse_args(argv)

    if not os.path.exists(args.file):
        print(f"[fatal] {args.file} not found -- run tools/bloomberg_nifty50.py on the "
              f"Bloomberg machine first, then copy the file across", file=sys.stderr)
        return 2

    nifty = pd.read_csv(args.file, dtype=str, keep_default_na=False)
    if nifty.empty:
        print(f"[fatal] {args.file} is empty", file=sys.stderr)
        return 2
    print(f"[info] {len(nifty)} members read from {args.file}")

    if args.equity_csv:
        eq = load_equity_from_csv(args.equity_csv)
        print(f"[info] {len(eq)} equity rows from {args.equity_csv}")
    else:
        date = dt.date.fromisoformat(args.date) if args.date else None
        eq = load_equity_from_kdb(date, args.instances)
        print(f"[info] {len(eq)} Indian listings from the equity table")

    if eq.empty:
        print("[fatal] the equity reference came back empty", file=sys.stderr)
        return 1

    idx = build_indexes(eq)

    syms, rules, cands = [], [], []
    for row in nifty.to_dict("records"):
        sym, rule, hits = match_row(row, idx)
        syms.append(sym)
        rules.append(rule)
        cands.append(";".join(sorted(set(hits))) if len(set(hits)) > 1 else "")

    nifty["sym"] = syms
    nifty["sym_match_rule"] = rules
    nifty["sym_candidates"] = cands

    out = args.out or args.file
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    nifty.to_csv(out, index=False)

    # -- report ------------------------------------------------------------- #
    matched = nifty[nifty["sym"] != ""]
    unmatched = nifty[nifty["sym"] == ""]

    print(f"\n  matched : {len(matched)} / {len(nifty)}")
    for rule, n in matched["sym_match_rule"].value_counts().items():
        print(f"    by {rule:<14} {n}")

    multi = nifty[nifty["sym_candidates"] != ""]
    resolved_multi = multi[multi["sym"] != ""]
    if not resolved_multi.empty:
        print(f"\n  {len(resolved_multi)} member(s) matched several listings, resolved "
              f"by the {'/'.join(SYM_PREFERENCE)} preference:")
        for r in resolved_multi.to_dict("records")[:20]:
            print(f"    {r['bbg_member']:<22} -> {r['sym']:<16} from {r['sym_candidates']}")

    if not unmatched.empty:
        sys.stdout.flush()
        print(f"\n  UNMATCHED: {len(unmatched)}", file=sys.stderr)
        for r in unmatched.to_dict("records"):
            if r["sym_match_rule"] == "ticker_ambiguous":
                why = f"ticker matches {r['sym_candidates']} -- pick one by hand"
            else:
                why = "no ISIN, Bloomberg code or ticker hit"
            print(f"    {r['bbg_member']:<22} isin={r['isin'] or '-':<14} {why}",
                  file=sys.stderr)
        print("\n  Fix by hand in the csv, or check that the equity snapshot date "
              "covers these names.", file=sys.stderr)

    weight_covered = pd.to_numeric(matched.get("weight_pct"), errors="coerce").fillna(0).sum()
    print(f"\n  index weight covered: {weight_covered:.2f}%")
    print(f"  written -> {out}\n")

    return 1 if (args.fail_on_unmatched and not unmatched.empty) else 0


if __name__ == "__main__":
    sys.exit(main())

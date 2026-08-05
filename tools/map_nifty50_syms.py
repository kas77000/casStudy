#!/usr/bin/env python3
"""Fill in the kdb `sym` for every NIFTY 50 member, matching on ISIN.

**Runs on the kdb machine**, as the second half of the hand-off from
`tools/bloomberg_nifty50.py`.  It reads that script's CSV, resolves each member
against the `equity` reference table, and writes the `sym` and `sym_match_rule`
columns back into the same file.

    python tools/map_nifty50_syms.py                       # config/nifty50.csv, in place
    python tools/map_nifty50_syms.py --file config/nifty50.csv --date 2026-08-04
    python tools/map_nifty50_syms.py --equity-csv dump/equity.csv   # no kdb needed

**ISIN is the only key.**  `equity.ID_ISIN` == the member's Bloomberg `ID_ISIN`,
and nothing else.  Ticker strings drift between vendors and exchanges -- a
`TICKER` or `sym_blp` fallback would quietly map the wrong instrument on the day
one of them changes, and a wrong sym is far more expensive than a missing one.
A member without an ISIN, or whose ISIN is absent from `equity`, is reported and
left blank for a manual fix.

One ISIN can still legitimately hit several listings -- a dual `.IN`/`.IB` line
is the same security twice.  That is resolved by `SYM_PREFERENCE`, and every
candidate is written to `sym_candidates` so the choice stays visible.
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

#: When one ISIN maps to several listings, keep this one.
#: `.IN` first because the NIFTY 50 is an NSE index.
SYM_PREFERENCE = ("IN", "IS", "IB")

#: Columns pulled from `equity`.  Only `sym` and `ID_ISIN` drive the match; the
#: rest ride along so the report can say *what* an ISIN resolved to.
EQUITY_MAP_COLS = [
    "sym", "ID_ISIN", "TICKER", "COMPOSITE_EXCH_CODE", "EQY_PRIM_EXCH_SHRT",
    "NAME", "CRNCY",
]

MATCH_RULE = "isin"


# --------------------------------------------------------------------------- #
# Normalisation                                                                #
# --------------------------------------------------------------------------- #

def norm_isin(v) -> str:
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
        for required in ("sym", "ID_ISIN"):
            if required not in have:
                raise SystemExit(f"[fatal] {tbl} has no `{required}` column -- "
                                 f"ISIN matching is not possible")
        cols = [c for c in EQUITY_MAP_COLS if c in have]

        like = " | ".join(f'(sym like "{p}")' for p in C.SYM_SUFFIXES)
        where_d = K.where_date(inst)
        qry = K.q_lambda(
            ["d"] if inst.partitioned else [],
            f"select {', '.join(cols)} from {tbl} "
            f"where {where_d}({like}), not null ID_ISIN",
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
    for required in ("sym", "ID_ISIN"):
        if required not in df.columns:
            raise SystemExit(f"[fatal] {path} has no `{required}` column")
    return df


# --------------------------------------------------------------------------- #
# Matching                                                                     #
# --------------------------------------------------------------------------- #

def build_isin_index(eq: pd.DataFrame) -> dict[str, list[str]]:
    """ISIN -> every sym carrying it (a dual listing gives more than one)."""
    idx: dict[str, list[str]] = {}
    for r in eq.to_dict("records"):
        sym = str(r.get("sym", "")).strip()
        isin = norm_isin(r.get("ID_ISIN"))
        if sym and isin:
            idx.setdefault(isin, []).append(sym)
    return idx


def match_row(row: dict, idx: dict[str, list[str]]) -> tuple[str, str, list[str]]:
    """-> (sym, rule, candidates).  Empty sym means no ISIN hit."""
    isin = norm_isin(row.get("isin"))
    if not isin:
        return "", "no_isin", []
    hits = idx.get(isin, [])
    if not hits:
        return "", "isin_not_in_equity", []
    return pick(hits), MATCH_RULE, hits


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
    if "isin" not in nifty.columns:
        print(f"[fatal] {args.file} has no `isin` column -- ISIN is the only match "
              f"key, so re-run tools/bloomberg_nifty50.py to produce it",
              file=sys.stderr)
        return 2
    print(f"[info] {len(nifty)} members read from {args.file}")

    if args.equity_csv:
        eq = load_equity_from_csv(args.equity_csv)
        print(f"[info] {len(eq)} equity rows from {args.equity_csv}")
    else:
        date = dt.date.fromisoformat(args.date) if args.date else None
        eq = load_equity_from_kdb(date, args.instances)
        print(f"[info] {len(eq)} Indian listings with an ISIN from the equity table")

    if eq.empty:
        print("[fatal] the equity reference came back empty", file=sys.stderr)
        return 1

    idx = build_isin_index(eq)

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
    no_isin = nifty[nifty["sym_match_rule"] == "no_isin"]
    not_in_eq = nifty[nifty["sym_match_rule"] == "isin_not_in_equity"]

    print(f"\n  matched on ISIN : {len(matched)} / {len(nifty)}")

    dual = matched[matched["sym_candidates"] != ""]
    if not dual.empty:
        print(f"\n  {len(dual)} ISIN(s) carried by more than one listing, resolved by "
              f"the {'/'.join(SYM_PREFERENCE)} preference:")
        for r in dual.to_dict("records")[:20]:
            print(f"    {r['bbg_member']:<22} {r['isin']:<14} -> {r['sym']:<16} "
                  f"from {r['sym_candidates']}")

    if not no_isin.empty or not not_in_eq.empty:
        sys.stdout.flush()
        print(f"\n  UNMATCHED: {len(no_isin) + len(not_in_eq)}", file=sys.stderr)

        if not no_isin.empty:
            print(f"\n  {len(no_isin)} member(s) came back from Bloomberg without an "
                  f"ISIN -- re-run tools/bloomberg_nifty50.py, or add the ISIN by hand:",
                  file=sys.stderr)
            for r in no_isin.to_dict("records"):
                print(f"    {r['bbg_member']:<22} {r.get('name', '')}", file=sys.stderr)

        if not not_in_eq.empty:
            print(f"\n  {len(not_in_eq)} ISIN(s) are not in the equity table for this "
                  f"date -- check the snapshot date, or whether the name is carried "
                  f"under a different listing:", file=sys.stderr)
            for r in not_in_eq.to_dict("records"):
                print(f"    {r['bbg_member']:<22} {r['isin']:<14} {r.get('name', '')}",
                      file=sys.stderr)

    weight_covered = pd.to_numeric(matched.get("weight_pct"), errors="coerce").fillna(0).sum()
    print(f"\n  index weight covered: {weight_covered:.2f}%")
    print(f"  written -> {out}\n")

    unmatched = len(no_isin) + len(not_in_eq)
    return 1 if (args.fail_on_unmatched and unmatched) else 0


if __name__ == "__main__":
    sys.exit(main())

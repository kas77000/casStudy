#!/usr/bin/env python3
"""Build the NIFTY 50 file from NSE's public constituent list -- no Bloomberg.

A drop-in alternative to `tools/bloomberg_nifty50.py`.  It writes the same
columns, so `tools/map_nifty50_syms.py` and `cas_price_move.py` consume it
unchanged.

    python tools/nifty50_from_nse.py --out config/nifty50.csv
    python tools/nifty50_from_nse.py --file ind_nifty50list.csv    # offline
    python tools/nifty50_from_nse.py --weights-from-equity         # see below
    python tools/nifty50_from_nse.py --resolve-syms                # do both steps

**What you get and what you do not.**  NSE publishes the constituent list as

    Company Name, Industry, Symbol, Series, ISIN Code

so it carries **ISIN**, which is the only key `map_nifty50_syms.py` matches on.
The universe half of the job is therefore fully covered by a free, public file.

What it does *not* carry is **index weights**.  NSE publishes those only in the
monthly factsheet PDF.  Three ways to fill the column, in descending order of
trustworthiness:

  --weights-file FILE      a CSV you produce yourself with an ISIN (or symbol)
                           column and a weight column.  Exact, if the file is.
  --weights-from-equity    derive them from `equity.CUR_MKT_CAP` in kdb.  This is
                           **full** market cap, not free float, so it is an
                           approximation -- promoter-heavy names come out too
                           heavy.  Labelled as such in `weight_source`.
  (nothing)                leave `weight_pct` blank.  Everything except the one
                           index-weighted average line in cas_price_move.py works
                           without it.

If the kdb box has no internet, download the CSV on any machine and pass it with
`--file`; the script never needs the network in that mode.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
import os
import sys
import urllib.error
import urllib.request

#: Tried in order.  The archives host is the one that answers reliably from
#: inside corporate networks; niftyindices.com is the canonical home.
DEFAULT_SOURCES = [
    "https://archives.nseindia.com/content/indices/{list_name}.csv",
    "https://www.niftyindices.com/IndexConstituent/{list_name}.csv",
]

DEFAULT_LIST = "ind_nifty50list"
DEFAULT_INDEX_NAME = "NIFTY 50"

#: NSE blocks the default python-urllib user agent outright.
HTTP_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept": "text/csv,application/csv,text/plain,*/*",
    "Accept-Language": "en-US,en;q=0.9",
}

HTTP_TIMEOUT = 30

#: Same schema as tools/bloomberg_nifty50.py, plus the NSE-native extras.
OUTPUT_COLUMNS = [
    "index_ticker", "asof", "weight_source",
    "bbg_member", "bbg_ticker", "isin", "name",
    "ticker", "composite_exch_code", "prim_exch", "crncy",
    "cur_mkt_cap", "px_last", "weight_pct",
    "sym", "sym_match_rule",
    "nse_symbol", "industry", "series",
]

#: Header spellings seen on the NSE files.  The lower-case entries are this
#: script's own output columns, so a file it wrote can be fed back in -- which is
#: what --file plus --weights-file does.
COL_ISIN = ("ISIN Code", "ISIN", "ISIN_CODE", "isin")
COL_SYMBOL = ("Symbol", "SYMBOL", "symbol", "nse_symbol", "ticker", "bbg_member")
COL_NAME = ("Company Name", "COMPANY NAME", "Company_Name", "name")
COL_INDUSTRY = ("Industry", "INDUSTRY", "industry")
COL_SERIES = ("Series", "SERIES", "series")
COL_WEIGHT = ("weight_pct", "Weight", "WEIGHT", "Weightage", "Weight(%)",
              "Percent Weight", "weight")


def _pick(row: dict, keys) -> str:
    for k in keys:
        if k in row and str(row[k]).strip():
            return str(row[k]).strip()
    return ""


def _to_float(v):
    try:
        return float(str(v).replace("%", "").replace(",", "").strip())
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# Fetching                                                                     #
# --------------------------------------------------------------------------- #

def fetch_csv(list_name: str, sources: list[str]) -> tuple[str, str]:
    """-> (csv text, the URL it came from). Tries each source in turn."""
    errors = []
    for template in sources:
        url = template.format(list_name=list_name)
        try:
            req = urllib.request.Request(url, headers=HTTP_HEADERS)
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                raw = resp.read()
            text = raw.decode("utf-8-sig", errors="replace")
            if "," not in text.splitlines()[0]:
                raise ValueError("response does not look like a CSV")
            return text, url
        except Exception as exc:
            errors.append(f"{url}\n      {type(exc).__name__}: {exc}")
    raise SystemExit(
        "[fatal] could not download the constituent list from any source:\n    "
        + "\n    ".join(errors)
        + "\n\n  If this machine has no internet access, download the file on one "
          "that does\n  and pass it with --file:\n"
          f"    {sources[0].format(list_name=list_name)}"
    )


def parse_constituents(text: str) -> list[dict]:
    rows = list(csv.DictReader(io.StringIO(text)))
    if not rows:
        raise SystemExit("[fatal] the constituent list is empty")
    out = []
    for r in rows:
        isin = _pick(r, COL_ISIN)
        symbol = _pick(r, COL_SYMBOL)
        if not (isin or symbol):
            continue
        out.append({
            "isin": isin.upper(),
            "nse_symbol": symbol.upper(),
            "name": _pick(r, COL_NAME),
            "industry": _pick(r, COL_INDUSTRY),
            "series": _pick(r, COL_SERIES),
            "weight_pct": _pick(r, COL_WEIGHT),
        })
    if not out:
        raise SystemExit(
            "[fatal] no usable rows -- the file has none of the expected columns "
            f"{COL_ISIN} / {COL_SYMBOL}"
        )
    return out


# --------------------------------------------------------------------------- #
# Weights                                                                      #
# --------------------------------------------------------------------------- #

def weights_from_file(members: list[dict], path: str) -> str:
    """Merge weights from a user-supplied CSV, keyed on ISIN then symbol."""
    with open(path, "r", encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    by_isin, by_symbol = {}, {}
    for r in rows:
        w = _to_float(_pick(r, COL_WEIGHT))
        if w is None:
            continue
        isin = _pick(r, COL_ISIN).upper()
        sym = _pick(r, COL_SYMBOL).upper()
        if isin:
            by_isin[isin] = w
        if sym:
            by_symbol[sym] = w

    hit = 0
    for m in members:
        w = by_isin.get(m["isin"])
        if w is None:
            w = by_symbol.get(m["nse_symbol"])
        if w is not None:
            m["weight_pct"] = f"{w:.6f}"
            hit += 1
    print(f"[info] weights: {hit}/{len(members)} matched from "
          f"{os.path.basename(path)}", flush=True)
    if hit < len(members):
        missing = [m["nse_symbol"] or m["isin"] for m in members
                   if not m.get("weight_pct")]
        print(f"[warn] {len(missing)} member(s) got no weight from that file: "
              f"{', '.join(missing[:10])}{' ...' if len(missing) > 10 else ''}",
              file=sys.stderr)
    return f"FILE:{os.path.basename(path)}"


def weights_from_equity(members: list[dict], date, instances_file: str) -> str:
    """Approximate the weights from `equity.CUR_MKT_CAP`.

    Full market cap, not free float, so these will not reproduce the published
    index weights -- promoter-heavy names come out too heavy.  Good enough to
    rank and to weight an average; not good enough to quote.
    """
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from casretro import config as C
    from casretro import kdbio as K
    from casretro.universe import last_business_day

    K.require_pykx()
    inst = C.resolve(C.load_instances(instances_file), "ref", "ht")
    with K.connect(inst) as conn:
        if date is None:
            date = last_business_day(conn)
            print(f"[info] equity snapshot date = {date}")
        tbl = inst.table("equity")
        have = set(conn.columns_of(tbl))
        for required in ("ID_ISIN", "CUR_MKT_CAP"):
            if required not in have:
                raise SystemExit(f"[fatal] {tbl} has no `{required}` column, so "
                                 f"--weights-from-equity is not possible")
        like = " | ".join(f'(sym like "{p}")' for p in C.SYM_SUFFIXES)
        qry = K.q_lambda(
            ["d"] if inst.partitioned else [],
            f"select ID_ISIN, CUR_MKT_CAP from {tbl} "
            f"where {K.where_date(inst)}({like}), not null ID_ISIN, "
            f"not null CUR_MKT_CAP",
        )
        eq = conn.query_pd(qry, *K.date_params(inst, date))

    caps = {}
    for r in eq.to_dict("records"):
        isin = str(r.get("ID_ISIN", "")).strip().upper()
        cap = _to_float(r.get("CUR_MKT_CAP"))
        if isin and cap and cap > 0:
            caps[isin] = max(caps.get(isin, 0.0), cap)

    mine = {m["isin"]: caps.get(m["isin"]) for m in members}
    total = sum(v for v in mine.values() if v)
    missing = [m["nse_symbol"] for m in members if not mine.get(m["isin"])]
    if not total:
        raise SystemExit("[fatal] no member matched a CUR_MKT_CAP -- check the "
                         "snapshot date")

    for m in members:
        cap = mine.get(m["isin"])
        m["weight_pct"] = f"{cap / total * 100.0:.6f}" if cap else ""

    print(f"[info] weights: derived from CUR_MKT_CAP for "
          f"{len(members) - len(missing)}/{len(members)} members")
    print(
        "[warn] these are FULL market-cap weights, not the free-float weights NSE\n"
        "       publishes. They will not match the factsheet -- promoter-heavy\n"
        "       names come out too heavy. Use --weights-file for exact numbers.",
        file=sys.stderr,
    )
    if missing:
        print(f"[warn] no CUR_MKT_CAP for: {', '.join(missing[:10])}"
              f"{' ...' if len(missing) > 10 else ''}", file=sys.stderr)
    return "EQUITY_CUR_MKT_CAP_APPROX"


# --------------------------------------------------------------------------- #

def build_rows(members: list[dict], index_name: str, asof: dt.date,
               weight_source: str, origin: str) -> list[dict]:
    out = []
    for m in members:
        symbol = m["nse_symbol"]
        out.append({
            "index_ticker": index_name,
            "asof": asof.isoformat(),
            "weight_source": weight_source,
            # No Bloomberg code from this source.  bbg_member carries the NSE
            # symbol so the downstream reports have something to print; it is
            # never used for matching, which is ISIN-only.
            "bbg_member": symbol,
            "bbg_ticker": "",
            "isin": m["isin"],
            "name": m["name"],
            "ticker": symbol,
            "composite_exch_code": "IN",
            "prim_exch": "NS",
            "crncy": "INR",
            "cur_mkt_cap": "",
            "px_last": "",
            "weight_pct": m.get("weight_pct", ""),
            "sym": "",
            "sym_match_rule": "",
            "nse_symbol": symbol,
            "industry": m.get("industry", ""),
            "series": m.get("series", ""),
        })
    out.sort(key=lambda r: (-(_to_float(r["weight_pct"]) or 0.0), r["nse_symbol"]))
    print(f"[info] source: {origin}")
    return out


def write_csv(rows: list[dict], path: str) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=OUTPUT_COLUMNS)
        w.writeheader()
        w.writerows(rows)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--out", default=os.path.join("config", "nifty50.csv"))
    ap.add_argument("--file", help="a constituent CSV already on disk "
                                   "(skips the download entirely)")
    ap.add_argument("--list-name", default=DEFAULT_LIST,
                    help=f"NSE list file name (default: {DEFAULT_LIST}; "
                         f"e.g. ind_nifty500list, ind_niftynext50list)")
    ap.add_argument("--index-name", default=DEFAULT_INDEX_NAME,
                    help="label written to the index_ticker column")
    ap.add_argument("--url", action="append",
                    help="override the source URL; repeatable, tried in order. "
                         "Use {list_name} as a placeholder")
    ap.add_argument("--date", help="YYYY-MM-DD stamped into asof, and used as the "
                                   "equity snapshot for --weights-from-equity")
    ap.add_argument("--expect", type=int, default=0,
                    help="expected member count; warns if it differs (e.g. 50)")

    g = ap.add_mutually_exclusive_group()
    g.add_argument("--weights-file", help="CSV with an ISIN/symbol column and a "
                                          "weight column")
    g.add_argument("--weights-from-equity", action="store_true",
                   help="approximate weights from equity.CUR_MKT_CAP (NOT free "
                        "float -- see --help)")

    ap.add_argument("--instances", help="path to instances.json "
                                        "(only for --weights-from-equity)")
    ap.add_argument("--resolve-syms", action="store_true",
                    help="also run tools/map_nifty50_syms.py on the result")
    args = ap.parse_args(argv)

    asof = dt.date.fromisoformat(args.date) if args.date else dt.date.today()

    if args.file:
        with open(args.file, "r", encoding="utf-8-sig") as fh:
            text = fh.read()
        origin = args.file
    else:
        sources = args.url or DEFAULT_SOURCES
        text, origin = fetch_csv(args.list_name, sources)

    members = parse_constituents(text)
    print(f"[info] {len(members)} members, "
          f"{sum(1 for m in members if m['isin'])} with an ISIN")

    weight_source = "NONE"
    if args.weights_file:
        weight_source = weights_from_file(members, args.weights_file)
    elif args.weights_from_equity:
        # casretro is only imported on this branch, so the script keeps working
        # on a machine that has no kdb stack installed.
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from casretro import config as C
        weight_source = weights_from_equity(
            members,
            dt.date.fromisoformat(args.date) if args.date else None,
            args.instances or C.INSTANCES_FILE,
        )
    elif any(m.get("weight_pct") for m in members):
        weight_source = "SOURCE_FILE"
        print("[info] weights: taken from a weight column in the source file")
    else:
        print("[info] weights: none available from this source -- weight_pct left "
              "blank (only the index-weighted average line in cas_price_move.py "
              "needs it)")

    rows = build_rows(members, args.index_name, asof, weight_source, origin)
    write_csv(rows, args.out)

    no_isin = sum(1 for r in rows if not r["isin"])
    if no_isin:
        print(f"[warn] {no_isin} member(s) have no ISIN and will not map to a sym",
              file=sys.stderr)
    if args.expect and len(rows) != args.expect:
        print(f"[warn] got {len(rows)} members, expected {args.expect} -- check "
              f"--list-name", file=sys.stderr)

    print(f"\n  written -> {args.out}")

    if args.resolve_syms:
        print()
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import map_nifty50_syms
        rc = map_nifty50_syms.main(["--file", args.out]
                                   + (["--instances", args.instances] if args.instances else []))
        return rc

    print(f"  next    -> python tools/map_nifty50_syms.py --file {args.out}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

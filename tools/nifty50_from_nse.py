#!/usr/bin/env python3
"""Build the NIFTY 50 file from NSE's public constituent list -- no Bloomberg.

A drop-in alternative to `tools/bloomberg_nifty50.py`.  It writes the same
columns, so `tools/map_nifty50_syms.py` and `cas_price_move.py` consume it
unchanged.

    python tools/nifty50_from_nse.py --out config/nifty50.csv
    python tools/nifty50_from_nse.py --file ind_nifty50list.csv    # offline
    python tools/nifty50_from_nse.py --resolve-syms                # do both steps

**What you get.**  NSE publishes the constituent list as

    Company Name, Industry, Symbol, Series, ISIN Code

so it carries **ISIN**, which is the only key `map_nifty50_syms.py` matches on.
That is the whole requirement: `cas_price_move.py` filters on `sym`, and `sym`
comes from ISIN.

**Index weights are not collected.**  NSE publishes them only in the monthly
factsheet PDF, and anything derivable from the reference data we hold is full
market cap rather than free float -- an approximation dressed up as a fact.
Nothing downstream reads a weight, so the column is written empty unless the
source file happens to carry one.

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
#: script's own output columns, so a file it wrote can be fed back in with --file.
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
    ap.add_argument("--date", help="YYYY-MM-DD stamped into the asof column")
    ap.add_argument("--expect", type=int, default=0,
                    help="expected member count; warns if it differs (e.g. 50)")
    ap.add_argument("--instances", help="path to instances.json "
                                        "(only for --resolve-syms)")
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

    # Weights are passed through if the source happened to carry them, and are
    # otherwise left empty.  Nothing downstream reads the column.
    weight_source = "SOURCE_FILE" if any(m.get("weight_pct") for m in members) else "NONE"

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

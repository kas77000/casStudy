#!/usr/bin/env python3
"""Snapshot the `equity` reference data into a csv, so kdb need not be queried.

Two scopes, because the two consumers want different things:

    cas    the CAS-eligible subset          -> config/cas_universe.csv
    all    every Indian listing (.IN)       -> config/india_universe.csv

`casretro` narrows whatever it reads with the CAS ISIN whitelist, so **either**
file serves it.  `casStudy.py` splits the book into CAS and non-CAS and uses the
non-CAS names as its control arm, so it needs the **all** file -- a CAS-only
snapshot leaves it with nothing to control against, and it refuses to run on one.

    python tools/export_cas_universe.py                    # both scopes, both files
    python tools/export_cas_universe.py --scope cas
    python tools/export_cas_universe.py --scope all
    python tools/export_cas_universe.py --date 2026-08-04 --force

The whole book is queried **once** and the CAS subset is taken from it in memory,
so the two files cannot disagree and asking for both costs no extra round trip.

The export calls the same `fetch_universe` the report calls, so the snapshot and
the live path cannot drift apart in shape.  Each file records its scope and its
snapshot date, so a consumer can say precisely what is wrong rather than infer it.

The ISIN whitelist is applied *again* at read time, so narrowing
`config/cas_isins.txt` takes effect immediately -- only a change of **reference
data** needs a re-export.

**What ages.** `sym`, `ID_ISIN`, `TICKER`, `NAME` are static. `adv`, `fx_last`,
`CUR_MKT_CAP` and especially `px_last_prev` are that day's values, and
`px_last_prev` is the last fallback of the CAS reference price. The snapshot date
is written into the file and the consumers warn when it does not match the day
being reported.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from casretro import config as C  # noqa: E402
from casretro import kdbio as K  # noqa: E402
from casretro import universe as U  # noqa: E402

SCOPES = {
    "cas": (C.SCOPE_CAS, C.CAS_UNIVERSE_FILE, "the CAS-eligible subset"),
    "all": (C.SCOPE_ALL, C.INDIA_UNIVERSE_FILE, "the whole Indian book"),
}


def write_snapshot(uni, date: dt.date, scope: str, path: str,
                   suffixes: tuple[str, ...] = ()) -> int:
    out = uni.copy()
    # Recorded so a file that looks short can be told apart from a truncated one.
    out.insert(0, C.SUFFIXES_COLUMN,
               ",".join(s.replace("*", "") for s in suffixes) if suffixes else "")
    out.insert(0, C.SCOPE_COLUMN, scope)
    out.insert(0, U.SNAPSHOT_DATE_COLUMN, date.isoformat())
    # Stable column and row order, so two exports of the same day are byte
    # identical and a diff shows real changes only.
    lead = [U.SNAPSHOT_DATE_COLUMN, C.SCOPE_COLUMN, C.SUFFIXES_COLUMN]
    lead += [c for c in U.EQUITY_COLUMNS if c in out.columns]
    out = out[lead + [c for c in out.columns if c not in lead]]
    out = out.sort_values("sym", ignore_index=True)

    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    out.to_csv(path, index=False)
    return len(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--scope", choices=("both", "cas", "all"), default="both",
                    help="which snapshot(s) to write (default: both)")
    ap.add_argument("--out", help="output csv path; only valid for a single --scope")
    ap.add_argument("--out-dir", default=C.CONFIG_DIR,
                    help=f"where the files go (default: {C.CONFIG_DIR})")
    ap.add_argument("--date", help="YYYY-MM-DD; default = last business day, "
                                   "resolved server side")
    ap.add_argument("--isin-file", default=C.ISIN_FILE, help="CAS ISIN whitelist")
    ap.add_argument("--instances", default=C.INSTANCES_FILE)
    ap.add_argument("--mode", choices=("ht", "rt"), default="ht")
    ap.add_argument("--suffixes", default=",".join(
                        p.replace("*", "") for p in C.SYM_SUFFIXES),
                    help="comma-separated sym suffixes to export (default: "
                         f"{','.join(p.replace('*', '') for p in C.SYM_SUFFIXES)}). "
                         "Pass .IN,.IS,.IB for every Indian listing line")
    ap.add_argument("--show-queries", action="store_true",
                    help="print the q queries sent (stderr)")
    ap.add_argument("--force", action="store_true",
                    help="overwrite output files that already exist")
    args = ap.parse_args(argv)

    if args.out and args.scope == "both":
        print("[fatal] --out names one file, but --scope both writes two. Use "
              "--out-dir, or pick a single --scope.", file=sys.stderr)
        return 2

    suffixes = tuple(
        f"*{s.strip()}" if not s.strip().startswith("*") else s.strip()
        for s in args.suffixes.split(",") if s.strip()
    )
    if not suffixes:
        print("[fatal] --suffixes is empty -- nothing would be exported",
              file=sys.stderr)
        return 2
    print(f"[info] listings: {', '.join(s.replace('*', '') for s in suffixes)}")

    K.require_pykx()
    K.set_trace_queries(args.show_queries)

    wanted = ("cas", "all") if args.scope == "both" else (args.scope,)
    targets = {}
    for key in wanted:
        scope_label, default_path, _desc = SCOPES[key]
        path = args.out if args.out else os.path.join(
            args.out_dir, os.path.basename(default_path)
        )
        if os.path.exists(path) and not args.force:
            print(f"[fatal] {path} already exists -- pass --force to overwrite it, "
                  f"or --out/--out-dir to write elsewhere", file=sys.stderr)
            return 2
        targets[key] = (scope_label, path)

    # The CAS whitelist is needed for the cas scope, and for the row counts that
    # tell you whether the two files are what you expect.
    isins = U.load_isins(args.isin_file)
    if not isins and "cas" in wanted:
        print(
            f"No ISIN found in {args.isin_file}, so the CAS subset cannot be "
            f"taken.\nPaste the list from temp.q into that file, or export only "
            f"the whole book:\n  python tools/export_cas_universe.py --scope all",
            file=sys.stderr,
        )
        return 2
    if isins:
        print(f"[info] {len(isins)} ISINs loaded from {os.path.basename(args.isin_file)}")

    instances = C.load_instances(args.instances)
    inst = C.resolve(instances, "ref", args.mode)
    print(f"[info] {inst.label} {inst.host}:{inst.port}")

    with K.connect(inst) as conn:
        date = (dt.date.fromisoformat(args.date) if args.date
                else U.last_business_day(conn))
        print(f"[info] date = {date}"
              + ("" if args.date else "   (last business day, server side)"))
        # One query for the whole book; the CAS subset is a filter on it, so the
        # two snapshots are guaranteed consistent.
        everything = U.fetch_universe(conn, date, [], suffixes=suffixes)

    if everything.empty:
        print(
            "[fatal] the query returned nothing. Check the date is a loaded "
            "partition and\n        that some sym matches "
            f"{suffixes}. Re-run with --show-queries to see\n        exactly "
            "what was sent.",
            file=sys.stderr,
        )
        return 1

    n_all = len(everything)
    if isins and "ID_ISIN" in everything.columns:
        in_cas = everything["ID_ISIN"].astype(str).str.strip().str.upper().isin(set(isins))
    else:
        in_cas = None
    n_cas = int(in_cas.sum()) if in_cas is not None else 0
    print(f"[info] {n_all:,} Indian listings"
          + (f": {n_cas:,} CAS-eligible, {n_all - n_cas:,} not" if in_cas is not None else ""))

    if in_cas is not None and n_cas == 0 and "cas" in wanted:
        print(
            "[fatal] no listing matched the CAS whitelist -- the ISIN file and the "
            "equity\n        table disagree, so the cas snapshot would be empty.",
            file=sys.stderr,
        )
        return 1

    written = []
    for key in wanted:
        scope_label, path = targets[key]
        frame = everything if key == "all" else everything[in_cas]
        n = write_snapshot(frame, date, scope_label, path, suffixes)
        written.append((key, path, n))

    print()
    for key, path, n in written:
        print(f"  {SCOPES[key][2]:<32} {n:>7,} syms -> {path}")

    missing = [c for c in U.EQUITY_COLUMNS if c not in everything.columns]
    if missing:
        print(f"\n  [warn] not in the equity table, so absent from the snapshots: "
              f"{', '.join(missing)}", file=sys.stderr)

    have = {k for k, _p, _n in written}
    print()
    print(f"  casretro   -> {'ready' if have else 'unchanged'}"
          + (" (reads either file, narrows it with the whitelist)" if have else ""))
    if "all" in have:
        print(f"  casStudy   -> ready (the whole book, which its control arm needs)")
    else:
        print(f"  casStudy   -> still queries kdb: it needs the whole book, so run\n"
              f"                --scope all as well")
    print(f"\n  Delete a file, or run with --no-universe-file, to query kdb again.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

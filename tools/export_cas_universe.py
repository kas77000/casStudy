#!/usr/bin/env python3
"""Snapshot the `equity` reference data for the CAS universe into a csv.

Once `config/cas_universe.csv` exists, `casretro` reads the universe from it and
never queries the `equity` table -- with `--date` supplied it does not open the
REF connection at all.  Delete the file, or pass `--no-universe-file`, to go back
to querying kdb.

    python tools/export_cas_universe.py                      # -> config/cas_universe.csv
    python tools/export_cas_universe.py --date 2026-08-04
    python tools/export_cas_universe.py --no-isin-filter     # every .IN/.IS/.IB listing
    python tools/export_cas_universe.py --show-queries

The export runs exactly the query the report would have run, so the snapshot and
the live path cannot drift apart in shape.  The ISIN whitelist is applied again
at read time, so narrowing `config/cas_isins.txt` takes effect immediately --
only a change of *reference data* needs a re-export.

**Two consumers, and they want different scopes.**  `casretro` narrows whatever
it reads with the whitelist, so either export serves it.  `casStudy.py` splits
the file into CAS and non-CAS and uses the non-CAS names as its control arm, so a
CAS-only snapshot leaves it with nothing to control against -- it refuses to run
on one.  Exporting with `--no-isin-filter` produces a file that satisfies both,
which is why it is the recommended form.

**What ages.** `sym`, `ID_ISIN`, `TICKER`, `NAME` are static. `adv`, `fx_last`,
`CUR_MKT_CAP` and especially `px_last_prev` are that day's values, and
`px_last_prev` is the last fallback of the CAS reference price. The snapshot date
is written into the file and the report warns when it does not match the day
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


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--out", default=C.CAS_UNIVERSE_FILE,
                    help=f"output csv (default: {C.CAS_UNIVERSE_FILE})")
    ap.add_argument("--date", help="YYYY-MM-DD; default = last business day, "
                                   "resolved server side")
    ap.add_argument("--isin-file", default=C.ISIN_FILE, help="CAS ISIN whitelist")
    ap.add_argument("--no-isin-filter", action="store_true",
                    help="export every .IN/.IS/.IB listing rather than the CAS "
                         "subset; the whitelist is still applied when the report "
                         "reads the file")
    ap.add_argument("--instances", default=C.INSTANCES_FILE)
    ap.add_argument("--mode", choices=("ht", "rt"), default="ht")
    ap.add_argument("--show-queries", action="store_true",
                    help="print the q queries sent (stderr)")
    ap.add_argument("--force", action="store_true",
                    help="overwrite the output file if it already exists")
    args = ap.parse_args(argv)

    K.require_pykx()
    K.set_trace_queries(args.show_queries)

    if os.path.exists(args.out) and not args.force:
        print(f"[fatal] {args.out} already exists -- pass --force to overwrite it, "
              f"or --out to write elsewhere", file=sys.stderr)
        return 2

    isins: list[str] = []
    if not args.no_isin_filter:
        isins = U.load_isins(args.isin_file)
        if not isins:
            print(
                f"No ISIN found in {args.isin_file}.\n"
                f"Paste the CAS ISIN list from temp.q into that file, or pass\n"
                f"--no-isin-filter to export every .IN/.IS/.IB listing.",
                file=sys.stderr,
            )
            return 2
        print(f"[info] {len(isins)} ISINs loaded from {os.path.basename(args.isin_file)}")

    instances = C.load_instances(args.instances)
    inst = C.resolve(instances, "ref", args.mode)
    print(f"[info] {inst.label} {inst.host}:{inst.port}")

    with K.connect(inst) as conn:
        date = (dt.date.fromisoformat(args.date) if args.date
                else U.last_business_day(conn))
        print(f"[info] date = {date}"
              + ("" if args.date else "   (last business day, server side)"))
        uni = U.fetch_universe(conn, date, isins)

    if uni.empty:
        print(
            "[fatal] the query returned nothing. Check the date is a loaded "
            "partition, that\n        the ISINs match ID_ISIN, and that some sym "
            f"matches {C.SYM_SUFFIXES}.\n        Re-run with --show-queries to see "
            "exactly what was sent.",
            file=sys.stderr,
        )
        return 1

    out = uni.copy()
    out.insert(0, U.SNAPSHOT_DATE_COLUMN, date.isoformat())
    # Stable column order and stable row order, so two exports of the same day
    # produce byte-identical files and a diff shows real changes only.
    lead = [U.SNAPSHOT_DATE_COLUMN] + [c for c in U.EQUITY_COLUMNS if c in out.columns]
    out = out[lead + [c for c in out.columns if c not in lead]]
    out = out.sort_values("sym", ignore_index=True)

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    out.to_csv(args.out, index=False)

    missing = [c for c in U.EQUITY_COLUMNS if c not in out.columns]
    print(f"\n  {len(out):,} syms written -> {args.out}")
    print(f"  columns: {', '.join(out.columns)}")
    if missing:
        print(f"  [warn] not in the equity table, so absent from the snapshot: "
              f"{', '.join(missing)}", file=sys.stderr)
    for col in ("ID_ISIN", "px_last_prev"):
        if col in out.columns:
            n_null = int(out[col].isna().sum() + (out[col] == "").sum()
                         if out[col].dtype == object else out[col].isna().sum())
            if n_null:
                print(f"  [warn] {n_null} row(s) have no {col}", file=sys.stderr)

    print(f"\n  casretro will now read the universe from this file.")
    if isins:
        print(f"  casStudy.py will REFUSE it: {len(out):,} CAS-eligible names and no\n"
              f"  control arm. Re-export with --no-isin-filter --force to serve both.")
    else:
        print(f"  casStudy.py will use it too -- the whole Indian book, which is what\n"
              f"  its control arm needs.")
    print(f"  Delete it, or run with --no-universe-file, to query kdb again.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

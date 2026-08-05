"""Command line entry point."""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sys

from . import config as C
from . import kdbio as K
from . import report as R
from . import universe as U
from .build import build_report

DESCRIPTION = """\
Retrospective report on the execution of CAS-eligible Indian stocks.

Covers every parent order that traded on the day: the ones that made it into the
closing auction and the ones that did not, with a diagnosed reason for each miss,
and rejections split between continuous trading and the CAS window.

All times are HKT (IST = HKT - 02:30).
"""

EPILOG = """\
examples:
  # yesterday's SILK flow, everything written to output/
  python -m casretro --flow silk

  # a specific date, both flows, Excel only
  python -m casretro --date 2026-08-03 --flow both --formats xlsx

  # intraday, against the real-time tapes (no date predicate)
  python -m casretro --mode rt --flow both

  # check the wiring without running a report
  python -m casretro --check-config
"""


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        prog="casretro",
        description=DESCRIPTION,
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--date", help="YYYY-MM-DD; default = last business day (resolved server side)")
    ap.add_argument(
        "--flow", choices=("silk", "agency", "both"), default="both",
        help="basket contains SILK -> silk, otherwise agency (default: both)",
    )
    ap.add_argument(
        "--mode", choices=("ht", "rt"), default="ht",
        help="ht = historical, date-partitioned; rt = real-time tapes, no date predicate",
    )
    ap.add_argument("--instances", default=C.INSTANCES_FILE, help="path to instances.json")
    ap.add_argument("--isin-file", default=C.ISIN_FILE, help="CAS ISIN whitelist")
    ap.add_argument(
        "--universe-file", default=C.CAS_UNIVERSE_FILE,
        help="csv snapshot of the equity reference data; used when it exists, "
             "otherwise kdb is queried (see tools/export_cas_universe.py)",
    )
    ap.add_argument(
        "--no-universe-file", action="store_true",
        help="ignore the csv snapshot and always query the equity table",
    )
    ap.add_argument(
        "--no-isin-filter", action="store_true",
        help="take every .IN/.IS/.IB listing instead of the CAS ISIN whitelist",
    )
    ap.add_argument("--out", help="output directory (default: output/cas_retro_<date>_<flow>)")
    ap.add_argument(
        "--formats", default="csv,xlsx,html",
        help="comma separated subset of csv,xlsx,html (default: all three)",
    )
    ap.add_argument(
        "--no-market-data", action="store_true",
        help="skip the qatt queries - no reference price, band check, volume share or slippage",
    )
    ap.add_argument(
        "--keep-unfilled", action="store_true",
        help="keep parent orders that executed nothing at all (dropped by "
             "default - the report covers orders that traded, fully or in part; "
             "a rejected order that still completed a percentage is always kept)",
    )
    ap.add_argument(
        "--keep-no-close", action="store_true",
        help="keep parent orders that never sent a CLOSE-venue child order after "
             "17:45 HKT (dropped by default - the report covers close "
             "participants). Pass this to get the NOT_SENT population and the "
             "non-participation waterfall back",
    )
    ap.add_argument(
        "--show-queries", action="store_true",
        help="print every q query sent, with its arguments, elapsed time and "
             "result shape. Goes to stderr, so:  --show-queries 2> queries.log",
    )
    ap.add_argument("--quiet", action="store_true", help="suppress progress output")
    ap.add_argument(
        "--check-config", action="store_true",
        help="connect to every configured instance, report what is reachable, then exit",
    )
    return ap.parse_args(argv)


def check_config(instances: dict, mode: str) -> int:
    print(f"instances file : {C.INSTANCES_FILE}")
    print(f"mode           : {mode}\n")
    bad = 0
    for role in instances:
        try:
            inst = C.resolve(instances, role, mode)
        except KeyError as exc:
            print(f"  {role:<6} !! {exc}")
            bad += 1
            continue
        status = "not configured"
        if inst.configured:
            try:
                with K.connect(inst) as conn:
                    tables = conn("tables[]").py()
                    known = [K.as_str(t) for t in tables]
                    missing = [t for t in inst.tables.values() if t not in known]
                    status = "ok" if not missing else f"connected, missing tables: {missing}"
                    if missing:
                        bad += 1
            except Exception as exc:
                status = f"UNREACHABLE ({exc})"
                bad += 1
        else:
            bad += 1
        print(f"  {role:<6} {inst.label:<10} {inst.host}:{inst.port:<6} "
              f"partitioned={str(inst.partitioned):<5} -> {status}")
    print()
    return 1 if bad else 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    K.require_pykx()
    K.set_trace_queries(args.show_queries)

    instances = C.load_instances(args.instances)

    if args.check_config:
        return check_config(instances, args.mode)

    isins: list[str] = []
    if not args.no_isin_filter:
        isins = U.load_isins(args.isin_file)
        if not isins:
            print(
                f"No ISIN found in {args.isin_file}.\n"
                f"Paste the CAS ISIN list from temp.q into that file -- the raw\n"
                f"  `INE180A01020`INE935A01035`...\n"
                f"backtick form is fine -- or pass --no-isin-filter to run on every\n"
                f".IN/.IS/.IB listing instead.",
                file=sys.stderr,
            )
            return 2
        if not args.quiet:
            print(f"[info] {len(isins)} ISINs loaded from {os.path.basename(args.isin_file)}")

    date: dt.date | None = None
    with K.ConnectionPool(instances, args.mode) as pool:
        # A date is always resolved, even in rt mode: the non-partitioned RT
        # tables drop the predicate anyway (see kdbio.where_date), but REF has no
        # rt instance and falls back to the partitioned HDB, which needs one.
        if args.date:
            date = dt.date.fromisoformat(args.date)
        elif args.mode == "ht":
            date = U.last_business_day(pool.get("ref"))
        else:
            date = pool.get("ref")(".z.D").py()
        if not args.quiet:
            print(f"[info] date = {date}, mode = {args.mode}, flow = {args.flow}")
            if args.mode == "rt":
                print("[info] rt: the date predicate is dropped on every "
                      "non-partitioned table")

        data = build_report(
            pool,
            date,
            args.flow,
            isins=isins,
            skip_market_data=args.no_market_data,
            universe_csv=args.universe_file,
            use_universe_csv=not args.no_universe_file,
            drop_unfilled=not args.keep_unfilled,
            require_close_wo=not args.keep_no_close,
            verbose=not args.quiet,
        )

    stamp = date.strftime("%Y%m%d") if date else dt.datetime.now().strftime("%Y%m%d_%H%M")
    outdir = args.out or os.path.join(C.OUTPUT_DIR, f"cas_retro_{stamp}_{args.flow}")
    formats = {f.strip().lower() for f in args.formats.split(",") if f.strip()}

    R.print_console(data)

    written: list[str] = []
    if "csv" in formats:
        written += R.write_csvs(data, os.path.join(outdir, "csv"))
    if "xlsx" in formats:
        p = R.write_excel(data, os.path.join(outdir, f"cas_retro_{stamp}_{args.flow}.xlsx"))
        if p:
            written.append(p)
    if "html" in formats:
        written.append(
            R.write_html(data, os.path.join(outdir, f"cas_retro_{stamp}_{args.flow}.html"))
        )

    if written:
        print(f"  written -> {outdir}")
        for p in written[:6]:
            print(f"    {os.path.relpath(p, outdir)}")
        if len(written) > 6:
            print(f"    ... and {len(written) - 6} more")
        print()
    return 0

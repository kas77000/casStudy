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
    ap.add_argument(
        "--check-universe", action="store_true",
        help="run the universe query one predicate at a time and report which of "
             "the date, the sym suffixes or the ISIN whitelist empties it, then exit",
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


def check_universe(instances: dict, mode: str, isin_file: str,
                   date_str: str | None) -> int:
    """Take the universe query apart and say which predicate empties it.

    `fetch_universe` applies three things at once -- the partition date, the
    .IN/.IS/.IB suffix filter and the ISIN whitelist -- so an empty result never
    says which one is responsible.  This runs them separately and counts.
    """
    inst = C.resolve(instances, "ref", mode)
    tbl = inst.table("equity")
    isins = U.load_isins(isin_file)
    print(f"instance    : {inst.label} {inst.host}:{inst.port} "
          f"(partitioned={inst.partitioned})")
    print(f"table       : {tbl}")
    print(f"isin file   : {isin_file}")
    print(f"isins found : {len(isins)}")
    if isins[:3]:
        print(f"  first 3   : {', '.join(isins[:3])}")

    with K.connect(inst) as conn:
        date = (dt.date.fromisoformat(date_str) if date_str
                else U.last_business_day(conn))
        print(f"date        : {date}"
              + ("" if date_str else "   (last business day, server side)"))

        if inst.partitioned:
            try:
                parts = conn("-5#.Q.PV").py()
                print(f"last 5 partitions of the db : "
                      f"{', '.join(str(p) for p in parts)}")
                if date not in list(parts) and parts:
                    print(f"  !! {date} is not among them -- that alone empties "
                          f"every query", file=sys.stderr)
            except Exception:
                pass                      # .Q.PV is not always available

        where_d = K.where_date(inst)
        dparams = K.date_params(inst, date)
        like = U._like_clause()

        def count(body: str, *args, params=()) -> int:
            q = K.q_lambda(list(params), f"count select from {tbl} where {body}")
            try:
                return int(conn(q, *args).py())
            except Exception as exc:
                print(f"  query failed: {exc}", file=sys.stderr)
                return -1

        dp = ["d"] if inst.partitioned else []
        print("\nrow counts, one predicate at a time")
        n_all = count(f"{where_d}1b" if where_d else "1b", *dparams, params=dp)
        print(f"  {'rows on that date':<38}{n_all:>12,}")

        n_sym = count(f"{where_d}({like})", *dparams, params=dp)
        print(f"  {'+ sym like ' + '/'.join(C.SYM_SUFFIXES):<38}{n_sym:>12,}")

        if isins:
            sv = K.sym_vector(isins)
            n_isin = count(f"{where_d}ID_ISIN in isins", *dparams, sv,
                           params=dp + ["isins"])
            print(f"  {'+ ID_ISIN in the whitelist (alone)':<38}{n_isin:>12,}")
            n_both = count(f"{where_d}({like}), ID_ISIN in isins", *dparams, sv,
                           params=dp + ["isins"])
            print(f"  {'+ both (what the report uses)':<38}{n_both:>12,}")
        else:
            n_isin = n_both = -1

        # -- samples, so a format mismatch is visible ----------------------- #
        try:
            q = K.q_lambda(dp, f"10#exec distinct sym from {tbl} "
                               f"where {where_d}({like})")
            syms = [K.as_str(s) for s in conn(q, *dparams).py()]
            print(f"\nsample syms matching the suffix filter:\n  "
                  f"{', '.join(syms) if syms else '(none)'}")
        except Exception as exc:
            print(f"\n  sym sample failed: {exc}", file=sys.stderr)

        try:
            q = K.q_lambda(dp, f"10#exec distinct ID_ISIN from {tbl} "
                               f"where {where_d}({like})")
            got = [K.as_str(s) for s in conn(q, *dparams).py()]
            print(f"sample ID_ISIN on those rows:\n  "
                  f"{', '.join(got) if got else '(none)'}")
            if isins and got:
                overlap = set(got) & set(isins)
                print(f"  of those 10, {len(overlap)} are in your whitelist")
        except Exception as exc:
            print(f"  ID_ISIN sample failed: {exc}", file=sys.stderr)

    # -- verdict ------------------------------------------------------------ #
    print("\nverdict")
    if n_all == 0:
        print("  the date has no rows at all -- wrong date, or the REF instance "
              "does not\n  carry that partition. Try --date with a day you know is "
              "loaded.")
    elif n_sym == 0:
        print(f"  the date is fine but no sym matches {C.SYM_SUFFIXES}.\n"
              f"  Compare the sample syms above with those patterns and adjust\n"
              f"  config.SYM_SUFFIXES.")
    elif not isins:
        print(f"  no ISIN was parsed out of {isin_file}.\n"
              f"  Paste the backtick list from temp.q into it, or run with\n"
              f"  --no-isin-filter to take every {'/'.join(C.SYM_SUFFIXES)} listing.")
    elif n_isin == 0:
        print("  the whitelist matches nothing anywhere in the table -- the ISINs\n"
              "  in the file are not the ones in ID_ISIN. Compare the two samples "
              "above.")
    elif n_both == 0:
        print("  each predicate works alone but not together: the whitelisted "
              "ISINs\n  sit on syms that do not carry an .IN/.IS/.IB suffix.")
    else:
        print(f"  the universe query returns {n_both:,} rows -- this path is "
              f"healthy.\n  If the report still says the universe is empty, the "
              f"instance it uses\n  differs from this one; check the `ref` entry "
              f"in {C.INSTANCES_FILE}.")
    return 0 if (n_all > 0 and n_sym > 0) else 1


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    K.require_pykx()
    K.set_trace_queries(args.show_queries)

    instances = C.load_instances(args.instances)

    if args.check_config:
        return check_config(instances, args.mode)

    if args.check_universe:
        return check_universe(instances, args.mode, args.isin_file, args.date)

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

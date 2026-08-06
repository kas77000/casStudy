"""Command line entry point."""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sys

from . import config as C
from . import kdbio as K
from . import report as R
from . import trader as T
from . import universe as U
from . import weekly as W
from .build import build_report

DESCRIPTION = """\
Retrospective report on the execution of CAS-eligible Indian stocks.

Covers every parent order that traded: the ones that made it into the closing
auction and the ones that did not, with a diagnosed reason for each miss, and
rejections split between continuous trading and the CAS window.

One day by default.  --weekly rolls the same pipeline over a whole week, which
is the Friday review: a single day cannot tell a bad print from a habit.

Two HTML pages come out of every run -- the full one for the desk's own reading,
and a trader / client version that answers four questions and stops.

All times are HKT (IST = HKT - 02:30) unless a page says otherwise.
"""

EPILOG = """\
examples:
  # yesterday's SILK flow, everything written to output/
  python -m casretro --flow silk

  # the Friday review: Monday to today, both flows, both HTML pages
  python -m casretro --weekly

  # last week, reviewed on the Monday after
  python -m casretro --weekly --date 2026-08-07

  # an explicit range, and each day's own report alongside the roll-up
  python -m casretro --from 2026-07-27 --to 2026-08-07 --per-day

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
        "--weekly", action="store_true",
        help="review a whole week instead of a day: every business day from "
             "Monday up to --date (default: today, so a Friday run covers "
             "Mon-Fri). Days with no order are dropped and listed.",
    )
    ap.add_argument(
        "--from", dest="date_from", metavar="YYYY-MM-DD",
        help="start of an explicit range; implies a multi-day run",
    )
    ap.add_argument(
        "--to", dest="date_to", metavar="YYYY-MM-DD",
        help="end of an explicit range (default: --from)",
    )
    ap.add_argument(
        "--rt-today", choices=W.RT_TODAY_POLICIES, default="auto",
        help="multi-day runs only: where the live day is read from. The live day "
             "is the most recent business day -- Thursday on a Thursday run, "
             "Friday on a Friday, Saturday or Sunday run. auto = the RT tapes, "
             "falling back to the HDB once the day has been transferred; "
             "force = RT only; off = HDB only. Every earlier day always comes "
             "from the HDB (default: auto)",
    )
    ap.add_argument(
        "--per-day", action="store_true",
        help="on a multi-day run, also write each day's own report under "
             "<out>/days/<date>/",
    )
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
        "--universe-file", default=None,
        help="csv snapshot of the equity reference data. Default: "
             "config/cas_universe.csv, then config/india_universe.csv, then kdb "
             "(see tools/export_cas_universe.py)",
    )
    ap.add_argument(
        "--no-universe-file", action="store_true",
        help="ignore the csv snapshot and always query the equity table",
    )
    ap.add_argument(
        "--no-isin-filter", action="store_true",
        help="take every .IN listing instead of the CAS ISIN whitelist",
    )
    ap.add_argument("--out", help="output directory (default: output/cas_retro_<date>_<flow>)")
    ap.add_argument(
        "--formats", default="csv,xlsx,html,trader",
        help="comma separated subset of csv,xlsx,html,trader (default: all four). "
             "html = the full page for the desk; trader = the client-facing one",
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


def _rt_configured(instances: dict) -> bool:
    inst = instances.get("oms", {}).get("rt")
    return inst is not None and inst.configured


def _open_rt_pool(instances: dict) -> K.ConnectionPool | None:
    """Open the RT pool, or return None with a reason.

    Not reaching the RT tapes is not fatal: the run carries on against the HDB,
    which is the right answer once the write-down has happened anyway.  It is
    said out loud either way, because "Friday is missing" and "Friday was never
    looked for" are different problems.
    """
    try:
        rt = K.ConnectionPool(instances, "rt")
        rt.get("oms")            # fail here rather than mid-week
        return rt
    except Exception as exc:
        print(f"[warn] the RT tapes are unreachable ({exc}) -- the live day will "
              f"fall back to the HDB, where it only exists once written down",
              file=sys.stderr)
        return None


def _write_all(data, outdir: str, base: str, formats: set[str]) -> list[str]:
    """Every requested artefact for one report, in a fixed order."""
    written: list[str] = []
    if "csv" in formats:
        written += R.write_csvs(data, os.path.join(outdir, "csv"))
    if "xlsx" in formats:
        p = R.write_excel(data, os.path.join(outdir, f"{base}.xlsx"))
        if p:
            written.append(p)
    if "html" in formats:
        written.append(R.write_html(data, os.path.join(outdir, f"{base}.html")))
    if "trader" in formats:
        written.append(
            T.write_trader_html(data, os.path.join(outdir, f"{base}_trader.html"))
        )
    return written


def _report_written(written: list[str], outdir: str) -> None:
    if not written:
        return
    print(f"  written -> {outdir}")
    for p in written[:6]:
        print(f"    {os.path.relpath(p, outdir)}")
    if len(written) > 6:
        print(f"    ... and {len(written) - 6} more")
    print()


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
                f".IN listing instead.",
                file=sys.stderr,
            )
            return 2
        if not args.quiet:
            print(f"[info] {len(isins)} ISINs loaded from {os.path.basename(args.isin_file)}")

    explicit_range = bool(args.date_from or args.date_to)
    multi = bool(args.weekly or explicit_range)
    if multi and args.mode == "rt":
        print("[fatal] a multi-day review is driven off the HDB: the RT tables "
              "carry no date, so every day would return the same rows. Drop "
              "--mode rt -- the live day is taken from the RT tapes anyway, "
              "which is what --rt-today controls.", file=sys.stderr)
        return 2

    build_kwargs = dict(
        isins=isins,
        skip_market_data=args.no_market_data,
        universe_csv=args.universe_file or C.UNIVERSE_FILE_CANDIDATES,
        use_universe_csv=not args.no_universe_file,
        drop_unfilled=not args.keep_unfilled,
        require_close_wo=not args.keep_no_close,
    )

    date: dt.date | None = None
    week: W.WeekReport | None = None
    with K.ConnectionPool(instances, args.mode) as pool:
        # A date is always resolved, even in rt mode: the non-partitioned RT
        # tables drop the predicate anyway (see kdbio.where_date), but REF has no
        # rt instance and falls back to the partitioned HDB, which needs one.
        if multi:
            # `today` decides which day is the live one, so it is resolved server
            # side even when --from/--to already fixed the range.  The anchor for
            # --weekly is today rather than the last business day: the review is
            # run after Friday's close and has to include Friday.
            today = pool.get("ref")(".z.D").py()
            date = dt.date.fromisoformat(args.date) if args.date else today
        elif args.date:
            date = dt.date.fromisoformat(args.date)
        elif args.mode == "ht":
            date = U.last_business_day(pool.get("ref"))
        else:
            date = pool.get("ref")(".z.D").py()

        if multi:
            dates = W.resolve_dates(
                anchor=date,
                date_from=dt.date.fromisoformat(args.date_from) if args.date_from else None,
                date_to=dt.date.fromisoformat(args.date_to) if args.date_to else None,
            )
            if not dates:
                print("[fatal] the requested range contains no business day.",
                      file=sys.stderr)
                return 2

            live = W.latest_business_day(today)
            rt_available = args.rt_today != "off" and _rt_configured(instances)
            if not rt_available and args.rt_today != "off" and not args.quiet:
                print("[info] no RT instance configured for oms -- every day "
                      "comes from the HDB, so the live day is only there once "
                      "written down")

            # Opened on demand: a range that does not contain the live day, or a
            # policy of `off`, never touches the tapes.
            opened: list[K.ConnectionPool] = []

            def rt_factory():
                p = _open_rt_pool(instances)
                if p is not None:
                    opened.append(p)
                return p

            if not args.quiet:
                print(f"[info] weekly review: {dates[0]} to {dates[-1]} "
                      f"({len(dates)} business days), flow = {args.flow}")
                order = W.sources_for_day(
                    live, today, rt_available=rt_available, policy=args.rt_today,
                )
                if live in dates:
                    print(f"[info] live day {live} ({live:%a}) reads "
                          f"{' then '.join(s.upper() for s in order)}; every "
                          f"earlier day reads HT")
                else:
                    print(f"[info] the live day ({live}) is outside the range -- "
                          f"every day reads HT")
            try:
                week = W.build_week(
                    pool, dates, args.flow,
                    rt_pool=rt_factory, rt_available=rt_available,
                    today=today, rt_today=args.rt_today,
                    verbose=not args.quiet, **build_kwargs
                )
            finally:
                for p in opened:
                    p.close()
            data = week.combined
            date = data.date
        else:
            if not args.quiet:
                print(f"[info] date = {date}, mode = {args.mode}, flow = {args.flow}")
                if args.mode == "rt":
                    print("[info] rt: the date predicate is dropped on every "
                          "non-partitioned table")
            data = build_report(
                pool, date, args.flow, verbose=not args.quiet, **build_kwargs
            )

    formats = {f.strip().lower() for f in args.formats.split(",") if f.strip()}
    if week is not None:
        span = f"{week.dates[0]:%Y%m%d}_{week.dates[-1]:%Y%m%d}"
        base = f"cas_retro_week_{span}_{args.flow}"
        outdir = args.out or os.path.join(C.OUTPUT_DIR, base)
    else:
        stamp = date.strftime("%Y%m%d") if date else dt.datetime.now().strftime("%Y%m%d_%H%M")
        base = f"cas_retro_{stamp}_{args.flow}"
        outdir = args.out or os.path.join(C.OUTPUT_DIR, base)

    R.print_console(data)

    written = _write_all(data, outdir, base, formats)
    _report_written(written, outdir)

    if week is not None and args.per_day:
        for day in week.days:
            stamp = day.date.strftime("%Y%m%d")
            day_dir = os.path.join(outdir, "days", stamp)
            day_written = _write_all(
                day, day_dir, f"cas_retro_{stamp}_{args.flow}", formats
            )
            print(f"  {day.date}: {len(day_written)} file(s) -> "
                  f"{os.path.relpath(day_dir, outdir)}")
        print()

    return 0

"""Command line entry point for the v2 trader report."""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sys

from casretro import config as C
from casretro import kdbio as K
from casretro import universe as U

from . import config as V
from . import period as P
from . import report as R
from . import report2 as R2

DESCRIPTION = """\
Closing-auction execution review -- the trader / client page.

Three sections and nothing else:

  Execution Quality  per day, per order type: what we sent to the auction, what
                     traded, the ratio between them, and both in USD
  Flows              day x flow x order type, against the market's own close
                     volume and notional in the same names
  Top clients        the biggest baskets of each flow, by notional traded

A period, not a day: every chart is one bar per day.  Weekly by default.
All times HKT (IST = HKT - 02:30) unless the page says otherwise.
"""

EPILOG = """\
examples:
  # the Friday review: Monday to today
  python -m casretro_v2

  # an explicit period
  python -m casretro_v2 --from 2026-07-27 --to 2026-08-07

  # one day
  python -m casretro_v2 --date 2026-08-04

  # SILK only, and force the fx_last direction
  python -m casretro_v2 --flow silk --fx divide
"""


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        prog="casretro_v2",
        description=DESCRIPTION,
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "--date", metavar="YYYY-MM-DD",
        help="a single day, instead of the week",
    )
    ap.add_argument(
        "--from", dest="date_from", metavar="YYYY-MM-DD",
        help="start of an explicit period",
    )
    ap.add_argument(
        "--to", dest="date_to", metavar="YYYY-MM-DD",
        help="end of an explicit period (default: --from)",
    )
    ap.add_argument(
        "--weekly", action="store_true",
        help="Monday to today (the default when no date argument is given)",
    )
    ap.add_argument(
        "--rt-today", choices=P.RT_TODAY_POLICIES, default="auto",
        help="where the live day is read from: auto = the RT tapes, falling "
             "back to the HDB once the day has been transferred; force = RT "
             "only; off = HDB only (default: auto)",
    )
    ap.add_argument(
        "--flow", choices=("silk", "agency", "both"), default="both",
        help="basket contains SILK -> silk, otherwise agency (default: both)",
    )
    ap.add_argument(
        "--fx", choices=V.FX_CONVENTIONS, default=V.FX_AUTO,
        help="direction of the equity.fx_last quote. auto reads it off the "
             "magnitude, which is unambiguous for a currency far from parity "
             "(default: auto)",
    )
    ap.add_argument("--instances", default=C.INSTANCES_FILE, help="path to instances.json")
    ap.add_argument("--isin-file", default=C.ISIN_FILE, help="CAS ISIN whitelist")
    ap.add_argument(
        "--universe-file", default=None,
        help="csv snapshot of the equity reference data (the sym list only -- "
             "fx_last is always read from that day's equity partition)",
    )
    ap.add_argument("--no-universe-file", action="store_true",
                    help="ignore the csv snapshot and query the equity table")
    ap.add_argument("--no-isin-filter", action="store_true",
                    help="take every .IN listing instead of the CAS ISIN whitelist")
    ap.add_argument("--out", help="output directory")
    ap.add_argument("--formats", default="html,csv",
                    help="comma separated subset of html,csv (default: both). "
                         "html writes both layouts: _v1 (one column) and _v2 "
                         "(charts, then data)")
    ap.add_argument("--show-queries", action="store_true",
                    help="print every q query sent, to stderr")
    ap.add_argument("--quiet", action="store_true", help="suppress progress output")
    return ap.parse_args(argv)


def _rt_configured(instances: dict) -> bool:
    inst = instances.get("oms", {}).get("rt")
    return inst is not None and inst.configured


def _open_rt_pool(instances: dict) -> K.ConnectionPool | None:
    try:
        rt = K.ConnectionPool(instances, "rt")
        rt.get("oms")
        return rt
    except Exception as exc:
        print(f"[warn] the RT tapes are unreachable ({exc}) -- the live day will "
              f"fall back to the HDB", file=sys.stderr)
        return None


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    K.require_pykx()
    K.set_trace_queries(args.show_queries)

    instances = C.load_instances(args.instances)

    isins: list[str] = []
    if not args.no_isin_filter:
        isins = U.load_isins(args.isin_file)
        if not isins:
            print(f"No ISIN found in {args.isin_file} -- paste the CAS list in, "
                  f"or pass --no-isin-filter.", file=sys.stderr)
            return 2
        if not args.quiet:
            print(f"[info] {len(isins)} ISINs loaded from {os.path.basename(args.isin_file)}")

    load_kwargs = dict(
        isins=isins,
        universe_csv=args.universe_file or C.UNIVERSE_FILE_CANDIDATES,
        use_universe_csv=not args.no_universe_file,
        fx_convention=args.fx,
    )

    with K.ConnectionPool(instances, "ht") as pool:
        today = pool.get("ref")(".z.D").py()
        single = None
        if args.date and not (args.date_from or args.date_to):
            single = dt.date.fromisoformat(args.date)
            dates = [single]
        else:
            dates = P.resolve_dates(
                anchor=dt.date.fromisoformat(args.date) if args.date else today,
                date_from=dt.date.fromisoformat(args.date_from) if args.date_from else None,
                date_to=dt.date.fromisoformat(args.date_to) if args.date_to else None,
            )
        if not dates:
            print("[fatal] the requested period contains no business day.",
                  file=sys.stderr)
            return 2

        live = P.latest_business_day(today)
        rt_available = args.rt_today != "off" and _rt_configured(instances)
        opened: list[K.ConnectionPool] = []

        def rt_factory():
            p = _open_rt_pool(instances)
            if p is not None:
                opened.append(p)
            return p

        if not args.quiet:
            print(f"[info] period: {dates[0]} to {dates[-1]} "
                  f"({len(dates)} business days), flow = {args.flow}")
            order = P.sources_for_day(live, today, rt_available=rt_available,
                                      policy=args.rt_today)
            if live in dates:
                print(f"[info] live day {live} ({live:%a}) reads "
                      f"{' then '.join(s.upper() for s in order)}; every earlier "
                      f"day reads HT")

        try:
            data = P.build_period(
                pool, dates, args.flow,
                rt_pool=rt_factory, rt_available=rt_available,
                today=today, rt_today=args.rt_today,
                verbose=not args.quiet, **load_kwargs
            )
        finally:
            for p in opened:
                p.close()

    span = (f"{data.dates[0]:%Y%m%d}_{data.dates[-1]:%Y%m%d}"
            if len(data.dates) > 1 else f"{data.dates[0]:%Y%m%d}")
    base = f"cas_v2_{span}_{args.flow}"
    outdir = args.out or os.path.join(C.OUTPUT_DIR, base)
    formats = {f.strip().lower() for f in args.formats.split(",") if f.strip()}

    written: list[str] = []
    if "html" in formats:
        # Two layouts, side by side until the newer one has earned the job:
        #   <base>_v1.html   one column, charts and their tables together
        #   <base>_v2.html   two pages -- charts, then the data behind them
        written.append(R.write_html(data, os.path.join(outdir, f"{base}_v1.html")))
        written.append(R2.write_html(data, os.path.join(outdir, f"{base}_v2.html")))
    if "csv" in formats:
        written += R.write_csvs(data, os.path.join(outdir, "csv"))

    print()
    for w in data.warnings:
        print(f"  [note] {w}")
    if written:
        print(f"\n  written -> {outdir}")
        for p in written[:8]:
            print(f"    {os.path.relpath(p, outdir)}")
        print()
    return 0

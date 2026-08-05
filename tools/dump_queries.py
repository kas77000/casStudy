#!/usr/bin/env python3
"""Print every q query the report sends, without touching a database.

Each loader is called against a fake connection that records the query text and
the argument types instead of running anything, so what you read here is exactly
what would go over IPC -- including the column list, which is intersected with
the real table schema at run time.

    python tools/dump_queries.py                  # both modes, to stdout
    python tools/dump_queries.py --mode rt        # just the real-time variant
    python tools/dump_queries.py --out docs/queries.md   # markdown, for review

`--mode ht` shows the date-partitioned form (`where date=d, ...`); `--mode rt`
shows the same queries against the non-partitioned real-time tapes, where the
date predicate and its lambda parameter both disappear.

The column lists come from the schema CSVs under no_git/instances/ when they are
present, and fall back to the loader's own wish-list otherwise.  That only
affects which columns appear in the SELECT -- never the shape of the query.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd  # noqa: E402

from casretro import config as C  # noqa: E402
from casretro import kdbio as K  # noqa: E402
from casretro import loaders as L  # noqa: E402
from casretro import universe as U  # noqa: E402

# pykx is not needed to build query *text*.  Stub the vector constructors so the
# tool runs on a laptop with no kdb stack installed.
K.sym_vector = lambda v: list(v)          # noqa: E731
K.int_vector = lambda v: list(v)          # noqa: E731
K.time_ms_vector = lambda ts: [K.time_ms(t) for t in ts]  # noqa: E731

SCHEMA_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "no_git", "instances",
)

#: table -> the loader constant listing the columns we ask for
FALLBACK_COLS = {
    "target": L.TARGET_COLS,
    "target_state": L.STATE_COLS,
    "workorder": L.WORKORDER_COLS,
    "execution": L.EXECUTION_COLS,
    "alerts": L.ALERT_COLS,
    "equity": ["sym", "ID_ISIN", "TICKER", "NAME", "CRNCY", "COUNTRY", "adv",
               "adv_std", "px_last_prev", "fx_last", "CUR_MKT_CAP",
               "INDUSTRY_SECTOR", "MARKET_STATUS"],
    "fx_last": ["date", "CRNCY", "fx_last"],
    # One table name, two instances: HT carries `date`, RT does not.
    "qatt": ["date", "sym", "time", "typ", "price", "size", "totalVolume"],
}


def load_schema_columns() -> dict[str, list[str]]:
    """Read the column lists out of the schema CSVs, if they are around."""
    cols = dict(FALLBACK_COLS)
    if not os.path.isdir(SCHEMA_DIR):
        return cols
    for root, _dirs, files in os.walk(SCHEMA_DIR):
        for fn in files:
            if not fn.endswith(".csv"):
                continue
            table = os.path.splitext(fn)[0]
            with open(os.path.join(root, fn), newline="", encoding="utf-8") as fh:
                rows = list(csv.reader(fh))
            if not rows or rows[0][:1] != ["c"]:
                continue
            cols[table] = [r[0] for r in rows[1:] if r and r[0]]
    return cols


class RecordingConn:
    """Stands in for kdbio.Conn: records queries instead of running them."""

    def __init__(self, instance: C.Instance, columns: dict[str, list[str]]):
        self.instance = instance
        self._columns = columns
        self.captured: list[tuple[str, list[str]]] = []

    def columns_of(self, table: str) -> list[str]:
        return self._columns.get(table, [])

    def query_pd(self, expr: str, *args) -> pd.DataFrame:
        self.captured.append((expr.strip(), [type(a).__name__ for a in args]))
        return pd.DataFrame()

    def __call__(self, expr: str, *args):
        self.captured.append((expr.strip(), [type(a).__name__ for a in args]))
        return None


DATE = dt.date(2026, 8, 3)
SYMS = ["RELIANCE.IN", "TCS.IN"]
IDS = [1001, 1002]

#: (role, heading, callable(conn, date)) in the order the report issues them.
PLAN = [
    ("ref", "Universe -- CAS-eligible Indian syms (temp.q)",
     lambda c, d: U.fetch_universe(c, d, ["INE180A01020", "INE935A01035"])),
    ("ref", "Universe -- with --no-isin-filter",
     lambda c, d: U.fetch_universe(c, d, [])),
    ("ref", "FX rates",
     lambda c, d: U.fetch_fx(c, d)),
    ("oms", "Parent orders (target)",
     lambda c, d: L.load_targets(c, d, SYMS)),
    ("oms", "Parent-order state history (target_state)",
     lambda c, d: L.load_target_states(c, d, IDS)),
    ("oms", "Child orders (workorder)",
     lambda c, d: L.load_workorders(c, d, IDS)),
    ("oms", "Executions (execution)",
     lambda c, d: L.load_executions(c, d, IDS)),
    ("oms", "Alerts (alerts)",
     lambda c, d: L.load_alerts(c, d, IDS)),
    ("qatt", "Market volume profile per CAS session bucket",
     lambda c, d: L.load_volume_profile(c, d, SYMS)),
    ("qatt", "Reference-price VWAP window (17:30-17:45 HKT / 15:00-15:15 IST)",
     lambda c, d: L.load_window_stats(c, d, SYMS, C.REF_VWAP_START, C.REF_VWAP_END, "cts_")),
    ("qatt", "Last print before the close (17:45 HKT)",
     lambda c, d: L.load_last_trade_before(c, d, SYMS, C.CTS_END)),
    ("qatt", "Whole-day volume",
     lambda c, d: L.load_day_volume(c, d, SYMS)),
]

STANDALONE = [
    ("ref", "Last business day (resolved server side)",
     "{d:x-1; while[(d mod 7) in 0 1; d-:1]; d} .z.D"),
]


def collect(mode: str, columns: dict[str, list[str]]) -> list[dict]:
    instances = C.load_instances()
    out = []

    for role, title, expr in STANDALONE:
        inst = C.resolve(instances, role, mode)
        out.append({"role": role, "instance": inst.label, "title": title,
                    "query": expr, "args": []})

    for role, title, call in PLAN:
        inst = C.resolve(instances, role, mode)
        conn = RecordingConn(inst, columns)
        date = DATE if inst.partitioned else None
        call(conn, date)
        for query, argtypes in conn.captured:
            out.append({"role": role, "instance": inst.label, "title": title,
                        "query": query, "args": argtypes})
    return out


def render_text(rows: list[dict], mode: str) -> str:
    lines = [
        "=" * 78,
        f"  queries issued in --mode {mode}",
        f"  trade filter: {C.QATT_TRADE_FILTER}",
        "=" * 78,
    ]
    for r in rows:
        lines += [
            "",
            f"-- [{r['instance']}] {r['title']}",
            f"   args: {', '.join(r['args']) or '(none)'}",
            "",
            r["query"],
        ]
    return "\n".join(lines) + "\n"


def render_markdown(by_mode: dict[str, list[dict]]) -> str:
    out = [
        "# Queries",
        "",
        "Generated by `python tools/dump_queries.py --out docs/queries.md`.",
        "Re-run it after changing anything in `casretro/loaders.py`,",
        "`casretro/universe.py` or the session calendar in `casretro/config.py`.",
        "",
        f"Trade predicate (`config.QATT_TRADE_FILTER`): `{C.QATT_TRADE_FILTER}`",
        "",
        "Symbols are pushed in batches of "
        f"{C.SYM_CHUNK}, parent-order ids in batches of {C.ID_CHUNK}.",
        "Nothing is interpolated into a query except table and column *names* --",
        "every value travels as a positional pykx argument.",
        "",
    ]
    for mode, rows in by_mode.items():
        label = "date-partitioned HDB" if mode == "ht" else "real-time tapes (no date predicate)"
        out += [f"## `--mode {mode}` -- {label}", ""]
        for r in rows:
            out += [
                f"### [{r['instance']}] {r['title']}",
                "",
                f"args: {', '.join(f'`{a}`' for a in r['args']) or '_none_'}",
                "",
                "```q",
                r["query"],
                "```",
                "",
            ]
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--mode", choices=("ht", "rt", "both"), default="both")
    ap.add_argument("--out", help="write markdown here instead of text to stdout")
    args = ap.parse_args()

    columns = load_schema_columns()
    modes = ("ht", "rt") if args.mode == "both" else (args.mode,)
    by_mode = {m: collect(m, columns) for m in modes}

    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(render_markdown(by_mode))
        print(f"written -> {args.out}")
    else:
        for mode, rows in by_mode.items():
            print(render_text(rows, mode))
    return 0


if __name__ == "__main__":
    sys.exit(main())

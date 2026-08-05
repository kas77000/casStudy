"""The CAS-eligible Indian universe.

Same shape as `temp.q`:

    select sym from equity
    where date = <last business day>,
          (sym like "*.IN") | (sym like "*.IS") | (sym like "*.IB"),
          ID_ISIN in `INE180A01020`INE935A01035`...

The ISIN whitelist lives in `config/cas_isins.txt` so the exchange list can be
refreshed without touching code.  `--no-isin-filter` falls back to every
.IN/.IS/.IB listing, which is the whole Indian book rather than the CAS subset --
useful to sanity-check that the whitelist is not silently dropping names.

The reference data itself can come from either side: if
`config/cas_universe.csv` exists it is read from there, otherwise the `equity`
table is queried.  `tools/export_cas_universe.py` writes that file.  The ISIN
whitelist is applied either way, so refreshing `cas_isins.txt` narrows the
universe without needing a new export.
"""

from __future__ import annotations

import datetime as dt
import fnmatch
import os
import re
import sys

import pandas as pd

from . import config as C
from . import kdbio as K

_ISIN_RE = re.compile(r"\b[A-Z]{2}[A-Z0-9]{9}[0-9]\b")

#: Reference columns the report uses.  The CSV snapshot carries the same set,
#: plus `snapshot_date` recording the day it was taken.
EQUITY_COLUMNS = [
    "sym", "ID_ISIN", "TICKER", "NAME", "CRNCY", "COUNTRY",
    "adv", "adv_std", "px_last_prev", "fx_last",
    "CUR_MKT_CAP", "INDUSTRY_SECTOR", "MARKET_STATUS",
]

#: Columns that must come back as numbers rather than strings after a CSV round
#: trip -- `px_last_prev` in particular feeds the reference-price fallback.
EQUITY_NUMERIC = ("adv", "adv_std", "px_last_prev", "fx_last", "CUR_MKT_CAP")

SNAPSHOT_DATE_COLUMN = "snapshot_date"


# --------------------------------------------------------------------------- #
# ISIN whitelist                                                               #
# --------------------------------------------------------------------------- #

def isin_check_digit_ok(isin: str) -> bool:
    """ISIN check digit -- Luhn over the letters-expanded-to-digits string."""
    if len(isin) != 12:
        return False
    digits = "".join(str(ord(c) - 55) if c.isalpha() else c for c in isin)
    total, double = 0, True
    for ch in reversed(digits[:-1]):
        d = int(ch)
        if double:
            d *= 2
            if d > 9:
                d -= 9
        total += d
        double = not double
    return (10 - total % 10) % 10 == int(digits[-1])


def load_isins(path: str = C.ISIN_FILE, *, verbose: bool = True) -> list[str]:
    """Pull every ISIN token out of a text file.

    Any layout works -- the raw q backtick list, one per line, CSV.  Lines
    starting with `#` or `/` are ignored so the file can carry comments.
    Duplicates are dropped and every ISIN is check-digit validated, so a mangled
    paste is reported rather than silently shrinking the universe.
    """
    if not os.path.exists(path):
        return []

    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        body = "\n".join(
            line for line in fh.read().splitlines()
            if not line.lstrip().startswith(("#", "/"))
        )

    seen, good, bad = set(), [], []
    for isin in _ISIN_RE.findall(body.upper()):
        if isin in seen:
            continue
        seen.add(isin)
        (good if isin_check_digit_ok(isin) else bad).append(isin)

    if bad and verbose:
        print(
            f"[warn] {len(bad)} token(s) in {os.path.basename(path)} failed the "
            f"ISIN check digit and were dropped: {', '.join(bad[:10])}"
            f"{' ...' if len(bad) > 10 else ''}",
            file=sys.stderr,
        )
    return good


# --------------------------------------------------------------------------- #
# Universe query                                                               #
# --------------------------------------------------------------------------- #

def last_business_day(conn: K.Conn) -> dt.date:
    """Server-side, identical to temp.q: {d:x-1; while[(d mod 7) in 0 1; d-:1]; d}."""
    return conn("{d:x-1; while[(d mod 7) in 0 1; d-:1]; d} .z.D").py()


def _like_clause() -> str:
    return " | ".join(f'(sym like "{p}")' for p in C.SYM_SUFFIXES)


def fetch_universe(
    conn: K.Conn,
    date: dt.date | None,
    isins: list[str],
    *,
    extra_cols: bool = True,
) -> pd.DataFrame:
    """CAS universe as a frame: sym plus the reference data the report needs.

    Returns columns: sym, ID_ISIN, TICKER, NAME, CRNCY, adv, px_last_prev,
    fx_last, CUR_MKT_CAP, INDUSTRY_SECTOR (whatever of those exist).
    """
    inst = conn.instance
    tbl = inst.table("equity")
    where_d = K.where_date(inst)

    wanted = list(EQUITY_COLUMNS)
    have = set(conn.columns_of(tbl))
    cols = [c for c in wanted if c in have] if extra_cols else ["sym"]
    if "sym" not in cols:
        cols.insert(0, "sym")
    select = ", ".join(cols)

    params = (["d"] if inst.partitioned else []) + (["isins"] if isins else [])
    isin_clause = ", ID_ISIN in isins" if isins else ""
    qry = K.q_lambda(
        params,
        f"select {select} from {tbl} where {where_d}({_like_clause()}){isin_clause}",
    )
    args = K.date_params(inst, date) + ([K.sym_vector(isins)] if isins else [])

    df = conn.query_pd(qry, *args)
    if df.empty:
        return df

    df = df.drop_duplicates(subset=["sym"]).reset_index(drop=True)
    return df


# --------------------------------------------------------------------------- #
# CSV snapshot                                                                 #
# --------------------------------------------------------------------------- #

def _matches_suffix(sym: str) -> bool:
    s = str(sym)
    return any(fnmatch.fnmatch(s, p) for p in C.SYM_SUFFIXES)


def load_universe_csv(
    path: str,
    isins: list[str],
    *,
    date: dt.date | None = None,
    verbose: bool = True,
) -> pd.DataFrame | None:
    """Read the reference snapshot written by tools/export_cas_universe.py.

    Returns None when the file is absent, which is the signal to fall back to
    kdb.  A file that exists but cannot be used raises instead of returning None:
    silently querying the database when someone deliberately placed a snapshot
    would hide the problem rather than surface it.

    The ISIN whitelist is applied here as well as at export time, so narrowing
    `cas_isins.txt` takes effect without a re-export.
    """
    if not os.path.exists(path):
        return None

    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    if df.empty:
        raise SystemExit(f"[fatal] {path} is empty -- delete it to fall back to kdb")
    if "sym" not in df.columns:
        raise SystemExit(
            f"[fatal] {path} has no `sym` column. It should be the output of\n"
            f"        python tools/export_cas_universe.py --out {path}"
        )

    snapshot = ""
    if SNAPSHOT_DATE_COLUMN in df.columns:
        vals = [v for v in df[SNAPSHOT_DATE_COLUMN].unique() if str(v).strip()]
        snapshot = str(vals[0]) if vals else ""
        df = df.drop(columns=[SNAPSHOT_DATE_COLUMN])

    scope = ""
    if C.SCOPE_COLUMN in df.columns:
        vals = [v for v in df[C.SCOPE_COLUMN].unique() if str(v).strip()]
        scope = str(vals[0]) if vals else ""
        df = df.drop(columns=[C.SCOPE_COLUMN])

    for col in EQUITY_NUMERIC:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col].replace("", None), errors="coerce")

    df["sym"] = df["sym"].astype(str).str.strip()
    df = df[df["sym"] != ""]

    n_read = len(df)
    df = df[df["sym"].map(_matches_suffix)]
    n_suffix = len(df)

    if isins and "ID_ISIN" in df.columns:
        wanted = set(isins)
        df = df[df["ID_ISIN"].astype(str).str.strip().str.upper().isin(wanted)]
    elif isins:
        raise SystemExit(
            f"[fatal] {path} has no `ID_ISIN` column, so the CAS ISIN whitelist "
            f"cannot be applied.\n        Re-export it, or run with "
            f"--no-isin-filter."
        )

    df = df.drop_duplicates(subset=["sym"]).reset_index(drop=True)

    if verbose:
        tag = " / ".join(x for x in (scope, snapshot) if x)
        print(f"[info] universe from {os.path.basename(path)}: {n_read} rows, "
              f"{n_suffix} after the suffix filter, {len(df)} after the ISIN "
              f"whitelist" + (f" ({tag})" if tag else ""))
    if snapshot and date is not None and str(snapshot) != date.isoformat():
        print(
            f"[warn] {os.path.basename(path)} was taken on {snapshot} but the "
            f"report is for {date}.\n"
            f"       Static fields (sym, ISIN, name) are fine; adv and "
            f"px_last_prev are that day's,\n"
            f"       and px_last_prev feeds the reference-price fallback. "
            f"Re-export for an exact match.",
            file=sys.stderr,
        )
    if df.empty:
        raise SystemExit(
            f"[fatal] {path} produced an empty universe: {n_read} rows read, "
            f"{n_suffix} matched {C.SYM_SUFFIXES},\n"
            f"        0 matched the {len(isins)} ISINs in the whitelist. Check "
            f"that the two are the same vintage."
        )
    return df


def resolve_universe(
    conn_factory,
    date: dt.date | None,
    isins: list[str],
    *,
    csv_path=None,
    prefer_csv: bool = True,
    verbose: bool = True,
) -> tuple[pd.DataFrame, str]:
    """-> (universe, where it came from).

    `csv_path` is one path or several tried in order -- the CAS-only snapshot
    first, then the whole-book one, since both give the same answer here once the
    whitelist is applied and the smaller file is cheaper to read.

    `conn_factory` is called only if no CSV is used, so a run with a snapshot in
    place never opens the REF connection at all.
    """
    if prefer_csv and csv_path:
        candidates = [csv_path] if isinstance(csv_path, (str, os.PathLike)) else list(csv_path)
        for path in candidates:
            got = load_universe_csv(str(path), isins, date=date, verbose=verbose)
            if got is not None:
                return got, f"csv:{os.path.basename(str(path))}"
    if verbose:
        why = "no snapshot file" if prefer_csv else "--no-universe-file"
        print(f"[info] universe from kdb ({why})")
    return fetch_universe(conn_factory(), date, isins), "kdb:equity"


def fetch_fx(conn: K.Conn, date: dt.date | None) -> pd.DataFrame:
    """CRNCY -> fx_last, used to express notionals in a common currency."""
    inst = conn.instance
    try:
        tbl = inst.table("fx_last")
    except KeyError:
        return pd.DataFrame(columns=["CRNCY", "fx_last"])
    where_d = K.where_date(inst)
    body = f"select CRNCY, fx_last from {tbl}" + (f" where {where_d[:-2]}" if where_d else "")
    qry = K.q_lambda(["d"] if inst.partitioned else [], body)
    try:
        return conn.query_pd(qry, *K.date_params(inst, date))
    except Exception as exc:  # pragma: no cover
        print(f"[warn] fx_last unavailable: {exc}", file=sys.stderr)
        return pd.DataFrame(columns=["CRNCY", "fx_last"])

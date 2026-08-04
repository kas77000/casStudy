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
"""

from __future__ import annotations

import datetime as dt
import os
import re
import sys

import pandas as pd

from . import config as C
from . import kdbio as K

_ISIN_RE = re.compile(r"\b[A-Z]{2}[A-Z0-9]{9}[0-9]\b")


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

    wanted = [
        "sym", "ID_ISIN", "TICKER", "NAME", "CRNCY", "COUNTRY",
        "adv", "adv_std", "px_last_prev", "fx_last",
        "CUR_MKT_CAP", "INDUSTRY_SECTOR", "MARKET_STATUS",
    ]
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

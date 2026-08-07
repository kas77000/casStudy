"""The market side: what the whole auction did, per symbol per day -- and the
day's FX rate.

Both market numbers come out of one call to `casretro.loaders.load_window_stats`
-- an audited, parameterised query that aggregates server-side -- so no new q
text is introduced for them and `tools/dump_queries.py` still describes
everything that crosses the wire.

    close volume  the SUM of size printed 17:58-18:00 HKT
    close price   the FIRST price printed in the same window

One window, because both describe the auction itself.  Nothing after 18:00 is
counted: that is trading-at-last, struck at the closing price but not part of
the auction, and including it would inflate what our share is measured against.

`load_fx` is the one genuinely new query, and it exists because `fx_last` is a
**daily** column on `equity`.  A week converted at one snapshot's rate is a week
of notionals struck on the wrong day; every day gets its own partition read.
"""

from __future__ import annotations

import datetime as dt
from typing import Sequence

import pandas as pd

from casretro import config as C
from casretro import kdbio as K
from casretro import loaders as L
from casretro import universe as U

from . import config as V

#: What the market frame is keyed on and carries.
MARKET_COLS = ["sym", "mkt_close_qty", "mkt_close_notional_local",
               "mkt_close_px", "mkt_close_px_time"]

# --------------------------------------------------------------------------- #
# The close child orders                                                       #
# --------------------------------------------------------------------------- #

#: Columns the close-child-order query returns.  `make` is the executed quantity
#: and `avg_fill_price` the price it executed at -- both off the workorder, which
#: is the OMS's own word on the child order, rather than re-added from the
#: execution tape.
CLOSE_WO_COLS = [
    "date", "id_target", "id_work", "sym", "side", "otype", "venue",
    "size", "make", "price", "avg_fill_price", "t_off_market",
]

#: One q lambda, holding the desk's definition of a close child order that
#: counts.  Written to match `temp.q` predicate for predicate, so the numbers
#: this report quotes are the numbers that query returns.
_CLOSE_WO_Q = """
{{[{sig}]
  select {cols} from {tbl}
  where {where_d}sym in syms,
        venue like "*CLOSE*",
        make <= size,
        make > 0,
        t_off_market > `time$t{limit_clause} }}
"""

#: The marketability test, limit orders only.  A separate string so it can be
#: switched off in config without disturbing the rest of the predicate.
_LIMIT_CLAUSE = """,
        (
            (otype<>`{limit})
            |
            (
                (otype=`{limit})
                &
                (
                    ((side=`sell) & price <= avg_fill_price)
                    |
                    ((side=`buy)  & price >= avg_fill_price)
                )
            )
        )"""


def load_close_workorders(
    conn: K.Conn, date: dt.date | None, syms: Sequence[str]
) -> pd.DataFrame:
    """Close child orders that traded, filtered the way the desk defines them.

    Filtered **server-side**: the frame that arrives is already the population
    every number on the page is computed over, so there is no second, quieter
    definition applied later in pandas.

    Selected by `sym` rather than by parent id, which is what lets this run
    without first fetching the parent orders -- the parents are joined on
    afterwards for the basket, and only the ones that own a surviving child
    order matter.
    """
    inst = conn.instance
    tbl = inst.table("workorder")
    have = set(conn.columns_of(tbl))

    required = {"sym", "venue", "make", "size", "otype", "side", "price",
                "avg_fill_price", "t_off_market"}
    missing = sorted(required - have)
    if missing:
        raise SystemExit(
            f"[fatal] {tbl} has no {', '.join(missing)} column(s). The close "
            f"population is defined by those columns (see temp.q), so the report "
            f"cannot be built without them."
        )

    cols = ", ".join(c for c in CLOSE_WO_COLS if c in have)
    limit_clause = (
        _LIMIT_CLAUSE.format(limit=V.QTYPE_LIMIT) if V.LIMIT_MUST_BE_MARKETABLE else ""
    )
    sig = "d;syms;t" if inst.partitioned else "syms;t"
    qry = _CLOSE_WO_Q.format(
        sig=sig, cols=cols, tbl=tbl, where_d=K.where_date(inst),
        limit_clause=limit_clause,
    )

    frames = []
    for chunk in K.chunks(list(syms), C.SYM_CHUNK):
        frames.append(conn.query_pd(
            qry, *K.date_params(inst, date), K.sym_vector(chunk),
            K.time_ms(V.OFF_MARKET_AFTER),
        ))
    df = K.concat(frames)
    if df.empty:
        return pd.DataFrame(columns=CLOSE_WO_COLS)
    for col in ("size", "make", "price", "avg_fill_price"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.reset_index(drop=True)


#: The reference columns the FX rate needs, and nothing else.
FX_COLS = ["sym", "CRNCY", "fx_last"]


def load_fx(
    conn: K.Conn, date: dt.date | None, syms: Sequence[str]
) -> pd.DataFrame:
    """That day's `fx_last` and currency per sym, straight off `equity`.

    `equity` is date-partitioned, so this is the rate as it stood on the day
    being reported.  It is read per day and never carried across days: a weekly
    report converted at Friday's rate would restate Monday's notionals at a
    price nobody traded on.

    Deliberately narrow -- three columns, no `adv`, no `px_last_prev` -- because
    the sym list has usually already come from a snapshot and this is the only
    thing that has to be fresh.
    """
    inst = conn.instance
    tbl = inst.table("equity")
    where_d = K.where_date(inst)
    have = set(conn.columns_of(tbl))
    cols = [c for c in FX_COLS if c in have]
    if "sym" not in cols:
        return pd.DataFrame(columns=FX_COLS)

    sig = "d;syms" if inst.partitioned else "syms"
    qry = (f"{{[{sig}] select {', '.join(cols)} from {tbl} "
           f"where {where_d}sym in syms }}")

    frames = []
    for chunk in K.chunks(list(syms), C.SYM_CHUNK):
        frames.append(conn.query_pd(qry, *K.date_params(inst, date), K.sym_vector(chunk)))
    df = K.concat(frames)
    if df.empty:
        return pd.DataFrame(columns=FX_COLS)
    if "fx_last" in df.columns:
        df["fx_last"] = pd.to_numeric(df["fx_last"], errors="coerce")
    return df.drop_duplicates(subset=["sym"]).reset_index(drop=True)


def load_market(
    conn: K.Conn, date: dt.date | None, syms: Sequence[str]
) -> pd.DataFrame:
    """Per sym: the auction's own volume and price, and the notional they imply.

    One query, because volume and price now come from the same window: the sum
    of size printed 17:58-18:00 and the first price printed in it.  Nothing
    after 18:00 counts -- that is trading-at-last, done at the closing price but
    not part of the auction.

    `mkt_close_notional_local` is close volume x close price rather than the
    window's own traded notional, so our close notional and the auction's are
    struck on the same price.  The window's VWAP is carried too, for anyone who
    wants to see how far the two differ.
    """
    out = L.load_window_stats(
        conn, date, syms, V.CLOSE_WINDOW[0], V.CLOSE_WINDOW[1], prefix="cls_"
    )
    if out is None or out.empty:
        return pd.DataFrame(columns=MARKET_COLS)

    out = out.rename(columns={
        "cls_qty": "mkt_close_qty",
        "cls_vwap": "mkt_close_vwap",
        "cls_n": "mkt_close_prints",
        "cls_pxFirst": "mkt_close_px",
        "cls_tFirst": "mkt_close_px_time",
    })
    for col in ("mkt_close_qty", "mkt_close_px", "mkt_close_vwap"):
        if col not in out.columns:
            out[col] = float("nan")
        out[col] = pd.to_numeric(out[col], errors="coerce")
    if "mkt_close_px_time" not in out.columns:
        out["mkt_close_px_time"] = pd.NaT

    out["mkt_close_notional_local"] = out["mkt_close_qty"] * out["mkt_close_px"]
    keep = MARKET_COLS + [c for c in ("mkt_close_vwap", "mkt_close_prints")
                          if c in out.columns]
    return out[[c for c in keep if c in out.columns]].reset_index(drop=True)

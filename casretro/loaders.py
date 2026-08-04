"""Table loaders.  Every function returns a normalised pandas frame.

Query shape is deliberately boring: a q lambda with explicit parameters, called
with pykx positional args.  Nothing is interpolated into a query except column
and table *names*, so a stray quote in a symbol can never break out.

Symbols and ids are pushed in chunks (see `config.SYM_CHUNK` / `ID_CHUNK`) so a
1500-name universe does not build a monster IPC message.
"""

from __future__ import annotations

import datetime as dt
from typing import Sequence

import pandas as pd

from . import config as C
from . import kdbio as K


# --------------------------------------------------------------------------- #
# OMS: parent orders                                                           #
# --------------------------------------------------------------------------- #

TARGET_COLS = [
    "date", "id_server", "time", "id_target", "trader", "basket", "portfolio",
    "wave", "oes_oid", "oes_primoid", "sym", "side", "sidesign", "size", "tif",
    "otype", "limit_price", "t_oes_load", "t_gen", "t_start", "t_end",
    "p_start", "p_end", "algo", "alpha", "beta", "gamma", "delta", "iwould",
    "stealth", "doopen", "doclose", "docash", "cashratio", "cashrange",
]


def load_targets(conn: K.Conn, date: dt.date | None, syms: Sequence[str]) -> pd.DataFrame:
    """Parent orders on the CAS universe for the date."""
    inst = conn.instance
    tbl = inst.table("target")
    where_d = K.where_date(inst)
    have = set(conn.columns_of(tbl))
    cols = ", ".join(c for c in TARGET_COLS if c in have)

    sig = "d;syms" if inst.partitioned else "syms"
    qry = f"{{[{sig}] select {cols} from {tbl} where {where_d}sym in syms }}"

    frames = []
    for chunk in K.chunks(list(syms), C.SYM_CHUNK):
        frames.append(conn.query_pd(qry, *K.date_params(inst, date), K.sym_vector(chunk)))
    return K.concat(frames)


# --------------------------------------------------------------------------- #
# OMS: parent-order state history                                              #
# --------------------------------------------------------------------------- #

STATE_COLS = [
    "date", "time", "t_algo", "id_target", "trader", "sym", "side", "state",
    "ack", "open", "make", "leave", "leave_vendor", "commit",
    "last_fill_price", "last_fill_size", "avg_fill_price",
    "make_take", "make_post", "make_dark", "make_open", "make_close",
    "make_iwould", "make_work", "make_cross",
    "leave_take", "leave_post", "leave_dark", "leave_open", "leave_close",
    "leave_iwould", "leave_work", "leave_atlimit",
    "commit_open", "commit_close", "count_send",
]


def load_target_states(
    conn: K.Conn, date: dt.date | None, ids: Sequence[int]
) -> pd.DataFrame:
    """Full state history of the given parent orders, time-ordered."""
    inst = conn.instance
    tbl = inst.table("target_state")
    where_d = K.where_date(inst)
    have = set(conn.columns_of(tbl))
    cols = ", ".join(c for c in STATE_COLS if c in have)

    sig = "d;ids" if inst.partitioned else "ids"
    qry = f"{{[{sig}] `time xasc select {cols} from {tbl} where {where_d}id_target in ids }}"

    frames = []
    for chunk in K.chunks(list(ids), C.ID_CHUNK):
        frames.append(conn.query_pd(qry, *K.date_params(inst, date), K.int_vector(chunk)))
    df = K.concat(frames)
    if not df.empty:
        df = df.sort_values(["id_target", "time"], kind="mergesort").reset_index(drop=True)
    return df


# --------------------------------------------------------------------------- #
# OMS: child orders                                                            #
# --------------------------------------------------------------------------- #

WORKORDER_COLS = [
    "date", "time", "id_work", "id_target", "id_candidate", "id_ref", "id_cxl",
    "trader", "request", "oes_oid", "oes_primoid", "sym", "side", "size",
    "display", "tif", "otype", "price", "limit_target", "limit_candidate",
    "bps_candidate", "venue", "venuetype", "state", "count_send",
    "count_chaseprice", "make", "avg_fill_price",
    "t_gen", "t_start", "t_end", "t_transmit", "t_oes_send",
    "t_on_market", "t_off_market", "t_close",
    "transmit_bidprice", "transmit_askprice", "transmit_lastprice",
    "onmkt_bidprice", "onmkt_askprice", "onmkt_lastprice",
    "offmkt_bidprice", "offmkt_askprice", "offmkt_lastprice",
    "onmkt_adv1t", "offmkt_adv1t",
]


def load_workorders(
    conn: K.Conn, date: dt.date | None, ids: Sequence[int]
) -> pd.DataFrame:
    """Child orders belonging to the given parents."""
    inst = conn.instance
    tbl = inst.table("workorder")
    where_d = K.where_date(inst)
    have = set(conn.columns_of(tbl))
    cols = ", ".join(c for c in WORKORDER_COLS if c in have)

    sig = "d;ids" if inst.partitioned else "ids"
    qry = f"{{[{sig}] select {cols} from {tbl} where {where_d}id_target in ids }}"

    frames = []
    for chunk in K.chunks(list(ids), C.ID_CHUNK):
        frames.append(conn.query_pd(qry, *K.date_params(inst, date), K.int_vector(chunk)))
    df = K.concat(frames)
    if not df.empty:
        df = df.sort_values(["id_target", "time"], kind="mergesort").reset_index(drop=True)
    return df


# --------------------------------------------------------------------------- #
# OMS: executions                                                              #
# --------------------------------------------------------------------------- #

EXECUTION_COLS = [
    "date", "time", "id_target", "id_candidate", "id_work", "trader",
    "oes_oid", "oes_primoid", "wave", "sym", "side", "sidesign", "size",
    "tif", "otype", "price", "t_algo", "t_oes_send", "t_oes_real", "t_oes_xact",
    "destination", "exec_broker", "last_mkt", "ostat", "fillprice", "fillsize",
    "cum_apr", "cum_qty", "comment", "target_strike", "target_vwap",
    "bidprice", "askprice", "lastprice", "adv1t",
]


def load_executions(
    conn: K.Conn, date: dt.date | None, ids: Sequence[int]
) -> pd.DataFrame:
    """Execution reports for the given parents (fills *and* status messages)."""
    inst = conn.instance
    tbl = inst.table("execution")
    where_d = K.where_date(inst)
    have = set(conn.columns_of(tbl))
    cols = ", ".join(c for c in EXECUTION_COLS if c in have)

    sig = "d;ids" if inst.partitioned else "ids"
    qry = f"{{[{sig}] select {cols} from {tbl} where {where_d}id_target in ids }}"

    frames = []
    for chunk in K.chunks(list(ids), C.ID_CHUNK):
        frames.append(conn.query_pd(qry, *K.date_params(inst, date), K.int_vector(chunk)))
    df = K.concat(frames)
    if not df.empty:
        df = df.sort_values(["id_target", "time"], kind="mergesort").reset_index(drop=True)
    return df


# --------------------------------------------------------------------------- #
# OMS: alerts                                                                  #
# --------------------------------------------------------------------------- #

ALERT_COLS = [
    "date", "time", "trader", "id_target", "sym", "side", "limit", "size",
    "alertmode", "triggertime", "ntrigger", "alerttype", "alertstr",
    "istate", "dstate", "sstate",
]


def load_alerts(conn: K.Conn, date: dt.date | None, ids: Sequence[int]) -> pd.DataFrame:
    inst = conn.instance
    try:
        tbl = inst.table("alerts")
    except KeyError:
        return pd.DataFrame()
    where_d = K.where_date(inst)
    have = set(conn.columns_of(tbl))
    cols = ", ".join(c for c in ALERT_COLS if c in have)

    sig = "d;ids" if inst.partitioned else "ids"
    qry = f"{{[{sig}] select {cols} from {tbl} where {where_d}id_target in ids }}"

    frames = []
    for chunk in K.chunks(list(ids), C.ID_CHUNK):
        frames.append(conn.query_pd(qry, *K.date_params(inst, date), K.int_vector(chunk)))
    return K.concat(frames)


# --------------------------------------------------------------------------- #
# Market data: session volume profile                                          #
# --------------------------------------------------------------------------- #

_PROFILE_Q = """
{{[{sig}]
  t:select sym,time,price,size from {tbl}
    where {where_d}sym in syms, {trade};
  t:update bucket:names (`time$bnds) bin time from t;
  0!select n:count i,
           qty:sum size,
           notional:sum price*size,
           pxFirst:first price,
           pxLast:last price,
           tFirst:first time,
           tLast:last time
    by sym,bucket from t where not null bucket }}
"""


def load_volume_profile(
    conn: K.Conn, date: dt.date | None, syms: Sequence[str]
) -> pd.DataFrame:
    """Traded volume / notional per sym per CAS session bucket.

    The bucketing is done server-side with `bin` against the boundary vector, so
    the whole day's tape never crosses the wire -- only ~9 rows per sym.  The
    first bucket starts at 00:00, so `bin` only returns -1 (and therefore a null
    bucket, which is filtered out) for a null timestamp.
    """
    inst = conn.instance
    tbl = inst.table("qatt")
    sig = "d;syms;bnds;names" if inst.partitioned else "syms;bnds;names"

    qry = _PROFILE_Q.format(
        sig=sig, tbl=tbl, where_d=K.where_date(inst), trade=C.QATT_TRADE_FILTER
    )
    bnds = K.time_ms_vector([t for _, t in C.SESSION_BUCKETS])
    names = K.sym_vector(n for n, _ in C.SESSION_BUCKETS)

    frames = []
    for chunk in K.chunks(list(syms), C.SYM_CHUNK):
        frames.append(
            conn.query_pd(
                qry, *K.date_params(inst, date), K.sym_vector(chunk), bnds, names
            )
        )
    return K.concat(frames)


_WINDOW_Q = """
{{[{sig}]
  0!select vwap:size wavg price,
           qty:sum size,
           n:count i,
           pxFirst:first price,
           pxLast:last price,
           pxHigh:max price,
           pxLow:min price,
           tFirst:first time,
           tLast:last time
    by sym from {tbl}
    where {where_d}sym in syms, time >= `time$t1, time < `time$t2, {trade} }}
"""


def load_window_stats(
    conn: K.Conn,
    date: dt.date | None,
    syms: Sequence[str],
    t1: dt.time,
    t2: dt.time,
    prefix: str = "",
) -> pd.DataFrame:
    """VWAP / first / last / high / low over one [t1;t2) window, per sym.

    Half-open on purpose, so it lines up with the `bin` bucketing in
    `load_volume_profile` and a print exactly on a boundary is counted once.
    """
    inst = conn.instance
    tbl = inst.table("qatt")
    sig = "d;syms;t1;t2" if inst.partitioned else "syms;t1;t2"

    qry = _WINDOW_Q.format(
        sig=sig, tbl=tbl, where_d=K.where_date(inst), trade=C.QATT_TRADE_FILTER
    )

    frames = []
    for chunk in K.chunks(list(syms), C.SYM_CHUNK):
        frames.append(
            conn.query_pd(
                qry,
                *K.date_params(inst, date),
                K.sym_vector(chunk),
                K.time_ms(t1),
                K.time_ms(t2),
            )
        )
    df = K.concat(frames)
    if not df.empty and prefix:
        df = df.rename(columns={c: f"{prefix}{c}" for c in df.columns if c != "sym"})
    return df


_LASTTRADE_Q = """
{{[{sig}]
  0!select pxLastTrade:last price, tLastTrade:last time, qtyBefore:sum size
    by sym from {tbl}
    where {where_d}sym in syms, time < `time$t1, {trade} }}
"""


def load_last_trade_before(
    conn: K.Conn, date: dt.date | None, syms: Sequence[str], t1: dt.time
) -> pd.DataFrame:
    """Last print strictly before `t1`, plus the volume traded up to it.

    Feeds step 2 of the reference-price waterfall ("any trades earlier today?").
    """
    inst = conn.instance
    tbl = inst.table("qatt")
    sig = "d;syms;t1" if inst.partitioned else "syms;t1"

    qry = _LASTTRADE_Q.format(
        sig=sig, tbl=tbl, where_d=K.where_date(inst), trade=C.QATT_TRADE_FILTER
    )

    frames = []
    for chunk in K.chunks(list(syms), C.SYM_CHUNK):
        frames.append(
            conn.query_pd(
                qry, *K.date_params(inst, date), K.sym_vector(chunk), K.time_ms(t1)
            )
        )
    return K.concat(frames)


_DAYVOL_COLS = [
    "dayQty:sum size",
    "dayNotional:sum price*size",
    "dayVwap:size wavg price",
]


def load_day_volume(
    conn: K.Conn, date: dt.date | None, syms: Sequence[str]
) -> pd.DataFrame:
    """Whole-day traded volume per sym, plus the feed's own cumulative counter.

    Both are reported: `dayQty` sums the prints we can see, `totalVolume` is what
    the venue publishes.  A large gap between them means the trade predicate in
    `config.QATT_TRADE_FILTER` is letting non-trade records through.
    """
    inst = conn.instance
    tbl = inst.table("qatt")
    sig = "d;syms" if inst.partitioned else "syms"

    cols = list(_DAYVOL_COLS)
    if "totalVolume" in set(conn.columns_of(tbl)):
        cols.append("totalVolume:last totalVolume")

    qry = (
        f"{{[{sig}] 0!select {', '.join(cols)} by sym from {tbl} "
        f"where {K.where_date(inst)}sym in syms, {C.QATT_TRADE_FILTER} }}"
    )

    frames = []
    for chunk in K.chunks(list(syms), C.SYM_CHUNK):
        frames.append(conn.query_pd(qry, *K.date_params(inst, date), K.sym_vector(chunk)))
    return K.concat(frames)

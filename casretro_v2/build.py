"""One row per close child order, per day -- everything v2 measures sits on it.

Split the same way v1 is, and for the same reason: `load_day()` is the only part
that talks to kdb, `build_children()` is pure pandas.  That is what lets
`tools/selftest_v2.py` exercise the three sections of the report against
synthetic frames with no database in the loop.

The pricing rule the desk asked for, in one place:

    executed quantity   priced at the fill price, off the execution tape
    unfilled quantity   LIMIT  -> the child order's own price, off the workorder
                        MARKET -> the auction's closing price, off qatt

A market child order carries no price to be unfilled at, so the auction price is
what the quantity would have been worth had it traded; that substitution is
tagged per row in `unfilled_px_source`, and the page reports how much of the
total rests on it rather than burying it.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from casretro import classify as CL
from casretro import config as C
from casretro import kdbio as K
from casretro import loaders as L
from casretro import universe as U

from . import config as V
from . import days as D
from . import fx as FX
from . import loaders as VL

#: Where an unfilled quantity's price came from.  Reported, never assumed.
PX_FROM_WORKORDER = "workorder"
PX_FROM_AUCTION = "auction close"
PX_FROM_FILL = "own fills"
PX_NONE = "none"


@dataclass
class DayData:
    """One trading day, ready to measure."""

    date: dt.date | None
    children: pd.DataFrame
    market: pd.DataFrame
    universe: pd.DataFrame
    fx_factors: pd.DataFrame = field(default_factory=pd.DataFrame)
    mode: str = "ht"
    warnings: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Pure                                                                         #
# --------------------------------------------------------------------------- #

def _first_available(df: pd.DataFrame, cols) -> pd.Series:
    """The first of `cols` that carries a number, per row."""
    out = pd.Series(np.nan, index=df.index, dtype=float)
    for col in cols:
        if col in df.columns:
            out = out.fillna(pd.to_numeric(df[col], errors="coerce"))
    return out


def _one_row_per_child(wo: pd.DataFrame) -> pd.DataFrame:
    """Collapse the child-order event log to one row per `id_work`.

    The workorder table carries one row per *event*, so summing `size` across it
    counts an amended order once per amendment.  The last event is taken as the
    child order's final word on itself -- its size, its price, its state.
    """
    if wo is None or wo.empty or "id_work" not in wo.columns:
        return wo if wo is not None else pd.DataFrame()
    df = wo.copy()
    if "time" in df.columns:
        df["_t"] = pd.to_timedelta(df["time"], errors="coerce")
        df = df.sort_values(["id_work", "_t"], kind="mergesort")
        df = df.drop(columns=["_t"])
    return df.groupby("id_work", as_index=False, sort=False).tail(1).reset_index(drop=True)


def build_children(
    date: dt.date | None,
    targets: pd.DataFrame,
    wo: pd.DataFrame,
    ex: pd.DataFrame,
    market: pd.DataFrame | None = None,
    fx_factors: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """One row per close child order, with quantities and notionals attached."""
    wo = CL.enrich_workorders(wo if wo is not None else pd.DataFrame())
    ex = CL.enrich_executions(ex if ex is not None else pd.DataFrame(), wo)

    if wo is None or wo.empty or "is_close" not in wo.columns:
        return pd.DataFrame(columns=CHILD_COLS)
    close = wo[wo["is_close"]]
    if close.empty:
        return pd.DataFrame(columns=CHILD_COLS)

    df = _one_row_per_child(close)
    df["date"] = date
    df["sym"] = df.get("sym", "").astype(str)
    df["otype_kind"] = df.get("otype", "").map(CL.otype_kind)
    df["sent_qty"] = pd.to_numeric(df.get("size"), errors="coerce").fillna(0.0)
    df["wo_price"] = _first_available(df, ("price", "limit_target", "limit_candidate"))

    # -- the parent's identity -------------------------------------------- #
    if targets is not None and not targets.empty:
        keep = [c for c in ("id_target", "basket", "trader", "portfolio", "wave", "side")
                if c in targets.columns]
        parents = targets[keep].drop_duplicates("id_target")
        df = df.merge(parents, on="id_target", how="left", suffixes=("", "_parent"))
    for col in ("basket", "trader", "portfolio"):
        if col not in df.columns:
            df[col] = ""
        df[col] = df[col].fillna("").astype(str)
    df["flow"] = df["basket"].map(C.flow_of)

    # -- what actually traded --------------------------------------------- #
    fills = ex[ex["is_fill"] & ex["is_close"]] if not ex.empty else pd.DataFrame()
    if not fills.empty and "id_work" in fills.columns:
        f = fills.copy()
        f["_notional"] = f["fillsize"] * f["fillprice"]
        agg = f.groupby("id_work").agg(
            exec_qty=("fillsize", "sum"),
            exec_notional_local=("_notional", "sum"),
            n_fills=("fillsize", "size"),
        )
        df = df.merge(agg, left_on="id_work", right_index=True, how="left")
    for col in ("exec_qty", "exec_notional_local", "n_fills"):
        if col not in df.columns:
            df[col] = 0.0
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)

    df["exec_px"] = np.where(
        df["exec_qty"] > 0, df["exec_notional_local"] / df["exec_qty"].replace(0, np.nan), np.nan
    )
    df["unfilled_qty"] = (df["sent_qty"] - df["exec_qty"]).clip(lower=0.0)

    # -- what the unfilled quantity was worth ------------------------------ #
    if market is not None and not market.empty and "mkt_close_px" in market.columns:
        df = df.merge(market[["sym", "mkt_close_px"]], on="sym", how="left")
    if "mkt_close_px" not in df.columns:
        df["mkt_close_px"] = np.nan

    is_market = df["otype_kind"] == V.OTYPE_MARKET
    # A limit order has a price of its own; a market order does not, so the
    # auction's own close is what its unfilled quantity would have been worth.
    preferred = np.where(is_market, df["mkt_close_px"], df["wo_price"])
    source = np.where(is_market, PX_FROM_AUCTION, PX_FROM_WORKORDER)

    px = pd.Series(preferred, index=df.index, dtype=float)
    src = pd.Series(source, index=df.index, dtype=object)
    for fallback, label in ((df["wo_price"], PX_FROM_WORKORDER),
                            (df["mkt_close_px"], PX_FROM_AUCTION),
                            (df["exec_px"], PX_FROM_FILL)):
        take = px.isna() & fallback.notna()
        px = px.where(~take, fallback)
        src = src.where(~take, label)
    src = src.where(px.notna(), PX_NONE)

    df["unfilled_px"] = px
    df["unfilled_px_source"] = src
    df["unfilled_notional_local"] = df["unfilled_qty"] * df["unfilled_px"]
    # A row we cannot price is not worth zero.  The quantity still counts; only
    # its notional is unknown, and `_priced` is what lets the page say how much.
    df["priced"] = df["unfilled_qty"].le(0) | df["unfilled_px"].notna()
    df["sent_notional_local"] = (
        df["exec_notional_local"] + df["unfilled_notional_local"].fillna(0.0)
    )

    # -- USD ---------------------------------------------------------------- #
    if fx_factors is not None and not fx_factors.empty:
        df = FX.attach(df, fx_factors, {
            "exec_notional_local": "exec_notional_usd",
            "unfilled_notional_local": "unfilled_notional_usd",
            "sent_notional_local": "sent_notional_usd",
        })
    for col in ("exec_notional_usd", "unfilled_notional_usd", "sent_notional_usd"):
        if col not in df.columns:
            df[col] = np.nan

    return df[[c for c in CHILD_COLS if c in df.columns]].reset_index(drop=True)


#: Column order of the child-order frame, and the contract the metrics read.
CHILD_COLS = [
    "date", "flow", "basket", "sym", "otype_kind", "side", "venue",
    "id_target", "id_work", "state", "state_kind",
    "sent_qty", "exec_qty", "unfilled_qty", "n_fills",
    "wo_price", "exec_px", "mkt_close_px", "unfilled_px", "unfilled_px_source",
    "priced",
    "exec_notional_local", "unfilled_notional_local", "sent_notional_local",
    "exec_notional_usd", "unfilled_notional_usd", "sent_notional_usd",
    "usd_factor", "trader", "portfolio",
]


# --------------------------------------------------------------------------- #
# Impure                                                                       #
# --------------------------------------------------------------------------- #

def load_day(
    pool: K.ConnectionPool,
    date: dt.date | None,
    flow: str,
    *,
    isins: list[str] | None = None,
    universe_csv=C.UNIVERSE_FILE_CANDIDATES,
    use_universe_csv: bool = True,
    fx_convention: str = V.FX_AUTO,
    enforce_date: bool = False,
    verbose: bool = True,
) -> DayData:
    """Pull one day and turn it into a child-order frame."""
    warnings: list[str] = []

    def say(msg: str) -> None:
        if verbose:
            print(msg, flush=True)

    uni, uni_source = U.resolve_universe(
        lambda: pool.get("ref"), date, isins or [],
        csv_path=universe_csv, prefer_csv=use_universe_csv, verbose=verbose,
    )
    if uni.empty:
        raise SystemExit(
            "[fatal] the CAS universe came back empty -- check the date, the "
            f"ISIN whitelist in {C.ISIN_FILE}, and the REF instance."
        )
    warnings.append(f"universe source: {uni_source}")
    syms = sorted(uni["sym"].astype(str).unique())

    # -- the day's own FX rate --------------------------------------------- #
    # `fx_last` is a daily column on `equity`.  The snapshot carries whichever
    # day it was exported on, so it is fine for the sym list and wrong for the
    # rate: a week converted at one day's rate restates every other day at a
    # price nobody traded on.  Read the partition; fall back only if it fails,
    # and say so when it does.
    fx_ref = uni
    try:
        daily = VL.load_fx(pool.get("ref"), date, syms)
        if daily.empty:
            raise ValueError("equity returned no fx_last row for this date")
        fx_ref = daily
        say(f"[info] {date}: fx_last for {len(daily)} syms from equity")
    except Exception as exc:
        if str(uni_source).startswith("csv"):
            warnings.append(
                f"{date}: could not read fx_last from equity ({exc}) -- falling "
                f"back to the rate in {uni_source}, which is that snapshot's "
                f"day, not this one. USD notionals for this day are approximate."
            )
        else:
            warnings.append(f"{date}: could not read fx_last from equity ({exc})")

    factors, fx_warnings = FX.usd_factors(fx_ref, fx_convention)
    warnings += [f"{date}: {w}" for w in fx_warnings]

    oms = pool.get("oms")

    # On a non-partitioned instance the server was sent no date predicate, so
    # whatever the tape holds came back.  The requested day is enforced here
    # instead -- and a tape that has already rolled comes back empty, which is
    # what hands the day over to the HDB (see days.sources_for_day).
    clip = enforce_date and not oms.instance.partitioned

    def _clip(df: pd.DataFrame, what: str) -> pd.DataFrame:
        if not clip:
            return df
        out, dropped = D.clip_to_date(df, date)
        if dropped:
            say(f"[info] {date}: {what}: {dropped} row(s) from another date "
                f"dropped ({oms.instance.label} is not partitioned)")
        return out

    targets = _clip(L.load_targets(oms, date, syms), "targets")
    if not targets.empty:
        targets["flow"] = targets["basket"].map(C.flow_of)
        if flow != "both":
            want = C.FLOW_SILK if flow == "silk" else C.FLOW_AGENCY
            targets = targets[targets["flow"] == want].reset_index(drop=True)
    ids = sorted(targets["id_target"].dropna().astype(int).unique()) if not targets.empty else []
    say(f"[info] {date}: {len(targets)} parent orders ({flow})")

    wo = _clip(L.load_workorders(oms, date, ids), "workorders") if ids else pd.DataFrame()
    ex = _clip(L.load_executions(oms, date, ids), "executions") if ids else pd.DataFrame()

    market = pd.DataFrame(columns=VL.MARKET_COLS)
    try:
        market = VL.load_market(pool.get("qatt"), date, syms)
        say(f"[info] {date}: market close data for {len(market)} syms")
    except Exception as exc:  # pragma: no cover
        warnings.append(f"{date}: market data unavailable: {exc}")

    if not market.empty:
        # Converted with the same day's rate the order side uses, so our share
        # of the market is a ratio of two numbers struck identically.
        market = FX.attach(
            market, factors,
            {"mkt_close_notional_local": "mkt_close_notional_usd"},
        )
        market["date"] = date
        no_px = int(market["mkt_close_px"].isna().sum())
        if no_px:
            warnings.append(
                f"{date}: {no_px} of {len(market)} symbols never printed between "
                f"{V.CLOSE_PRICE_WINDOW[0]:%H:%M} and "
                f"{V.CLOSE_PRICE_WINDOW[1]:%H:%M} HKT, so they carry no closing "
                f"price and drop out of the market notional"
            )

    children = build_children(date, targets, wo, ex, market, factors)
    say(f"[info] {date}: {len(children)} close child orders")

    if not children.empty:
        unpriced = int((~children["priced"]).sum())
        if unpriced:
            warnings.append(
                f"{date}: {unpriced} close child order(s) have unfilled quantity "
                f"that could not be priced -- their quantity counts, their "
                f"notional does not"
            )

    return DayData(
        date=date, children=children, market=market, universe=uni,
        fx_factors=factors, mode=pool.mode, warnings=warnings,
    )

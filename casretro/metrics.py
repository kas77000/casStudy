"""Derived market metrics: reference price, price band, volume share, slippage.

Nothing here talks to kdb -- it consumes the frames produced by `loaders` so the
whole analytical layer stays testable against CSV fixtures.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from . import classify as CL
from . import config as C
from . import sessions as S
from .kdbio import td

BPS = 10_000.0


# --------------------------------------------------------------------------- #
# Reference price waterfall                                                    #
# --------------------------------------------------------------------------- #

def reference_price(
    universe: pd.DataFrame,
    cts_window: pd.DataFrame,
    last_trade: pd.DataFrame,
) -> pd.DataFrame:
    """Rebuild the exchange's CAS reference price, per sym.

        VWAP over 15:00-15:15 IST (17:30-17:45 HKT)
          -> no trades in that window? last traded price earlier today
             -> no trades at all today?  previous adjusted close

    Returns sym, ref_price, ref_source, band_lo, band_hi and the inputs, so the
    number can be audited rather than trusted.
    """
    base = universe[["sym"]].drop_duplicates().copy()

    if cts_window is not None and not cts_window.empty:
        base = base.merge(cts_window, on="sym", how="left")
    if last_trade is not None and not last_trade.empty:
        base = base.merge(last_trade, on="sym", how="left")

    prev_col = "px_last_prev" if "px_last_prev" in universe.columns else None
    if prev_col:
        base = base.merge(universe[["sym", prev_col]].drop_duplicates("sym"), on="sym", how="left")

    vwap = pd.to_numeric(base.get("cts_vwap"), errors="coerce")
    n_cts = pd.to_numeric(base.get("cts_n"), errors="coerce").fillna(0)
    last_px = pd.to_numeric(base.get("pxLastTrade"), errors="coerce")
    prev_px = pd.to_numeric(base.get(prev_col), errors="coerce") if prev_col else pd.Series(np.nan, index=base.index)

    ref = pd.Series(np.nan, index=base.index, dtype="float64")
    src = pd.Series("", index=base.index, dtype=object)

    use_vwap = (n_cts > 0) & vwap.notna() & (vwap > 0)
    ref = ref.mask(use_vwap, vwap)
    src = src.mask(use_vwap, "VWAP_1500_1515_IST")

    use_last = ref.isna() & last_px.notna() & (last_px > 0)
    ref = ref.mask(use_last, last_px)
    src = src.mask(use_last, "LAST_TRADE_TODAY")

    use_prev = ref.isna() & prev_px.notna() & (prev_px > 0)
    ref = ref.mask(use_prev, prev_px)
    src = src.mask(use_prev, "PREV_ADJ_CLOSE")

    src = src.mask(ref.isna(), "UNAVAILABLE")

    base["ref_price"] = ref
    base["ref_source"] = src
    base["band_lo"] = ref * (1.0 - C.CAS_PRICE_BAND)
    base["band_hi"] = ref * (1.0 + C.CAS_PRICE_BAND)
    base["no_market_data"] = (n_cts <= 0) & last_px.isna()
    return base


def apply_price_band(orders: pd.DataFrame) -> pd.DataFrame:
    """Flag parent limits and close child prices against the +/-3% band.

    Two different questions:

    * `parent_limit_blocks_close` -- directional.  A buy whose limit is *below*
      the lower band can never be priced into the auction, so the algo has
      nothing legal to send.  That is a structural non-participation cause.
    * `close_price_outside_band` -- symmetric.  Any child order priced outside
      the band gets rejected by the exchange, whichever side it is.
    """
    df = orders.copy()
    lim = pd.to_numeric(df.get("limit_price"), errors="coerce")
    lo = pd.to_numeric(df.get("band_lo"), errors="coerce")
    hi = pd.to_numeric(df.get("band_hi"), errors="coerce")
    sign = pd.to_numeric(df.get("sidesign"), errors="coerce")

    has_limit = lim.notna() & (lim > 0) & lo.notna()
    is_buy = sign > 0

    df["parent_limit_blocks_close"] = (
        has_limit & ((is_buy & (lim < lo)) | (~is_buy & (lim > hi)))
    ).fillna(False)
    df["parent_limit_in_band"] = (has_limit & (lim >= lo) & (lim <= hi)).fillna(False)
    df["parent_limit_bps_from_ref"] = np.where(
        has_limit & df.get("ref_price").notna(),
        (lim / pd.to_numeric(df.get("ref_price"), errors="coerce") - 1.0) * BPS,
        np.nan,
    )

    cmin = pd.to_numeric(df.get("close_wo_min_price"), errors="coerce")
    cmax = pd.to_numeric(df.get("close_wo_max_price"), errors="coerce")
    df["close_price_outside_band"] = (
        (cmin.notna() & lo.notna() & (cmin < lo)) | (cmax.notna() & hi.notna() & (cmax > hi))
    ).fillna(False)
    return df


# --------------------------------------------------------------------------- #
# Market volume profile                                                        #
# --------------------------------------------------------------------------- #

def pivot_market_profile(profile: pd.DataFrame) -> pd.DataFrame:
    """Long bucket rows -> one row per sym with `mkt_<bucket>_qty` columns."""
    if profile is None or profile.empty:
        return pd.DataFrame(columns=["sym"])

    p = profile.copy()
    p["bucket"] = p["bucket"].astype(str)
    p["qty"] = pd.to_numeric(p["qty"], errors="coerce").fillna(0.0)
    p["notional"] = pd.to_numeric(p["notional"], errors="coerce").fillna(0.0)

    qty = p.pivot_table(index="sym", columns="bucket", values="qty", aggfunc="sum").fillna(0.0)
    qty.columns = [f"mkt_{c}_qty" for c in qty.columns]

    noti = p.pivot_table(index="sym", columns="bucket", values="notional", aggfunc="sum").fillna(0.0)
    noti.columns = [f"mkt_{c}_notional" for c in noti.columns]

    last_px = p.pivot_table(index="sym", columns="bucket", values="pxLast", aggfunc="last")
    last_px.columns = [f"mkt_{c}_pxLast" for c in last_px.columns]

    out = qty.join(noti).join(last_px).reset_index()

    for name, _ in C.SESSION_BUCKETS:
        for suffix in ("qty", "notional"):
            col = f"mkt_{name}_{suffix}"
            if col not in out.columns:
                out[col] = 0.0
        if f"mkt_{name}_pxLast" not in out.columns:
            out[f"mkt_{name}_pxLast"] = np.nan

    # Prices the report leans on.
    out["close_px"] = np.where(
        out[f"mkt_{C.AUCTION_PRINT_BUCKET}_qty"] > 0,
        out[f"mkt_{C.AUCTION_PRINT_BUCKET}_notional"] / out[f"mkt_{C.AUCTION_PRINT_BUCKET}_qty"].replace(0, np.nan),
        np.nan,
    )
    out["close_print_qty"] = out[f"mkt_{C.AUCTION_PRINT_BUCKET}_qty"]
    out["cts_end_px"] = out["mkt_CTS_FINAL15_pxLast"]
    return out


def _window_qty(profile_wide: pd.DataFrame, buckets: tuple[str, ...]) -> pd.Series:
    cols = [f"mkt_{b}_qty" for b in buckets if f"mkt_{b}_qty" in profile_wide.columns]
    if not cols:
        return pd.Series(0.0, index=profile_wide.index)
    return profile_wide[cols].sum(axis=1)


def market_volume_shares(profile_wide: pd.DataFrame, day_vol: pd.DataFrame) -> pd.DataFrame:
    """Per sym: what share of the day the market traded in each CAS window.

    Compared against the desk benchmarks -- the 17.29% historical closing bin and
    the 9.90% / 2.09% / 11.99% day-1 readout.
    """
    df = profile_wide.copy()
    if day_vol is not None and not day_vol.empty:
        df = df.merge(day_vol, on="sym", how="left")

    bucket_cols = [f"mkt_{n}_qty" for n, _ in C.SESSION_BUCKETS if f"mkt_{n}_qty" in df.columns]
    df["mkt_day_qty_from_buckets"] = df[bucket_cols].sum(axis=1) if bucket_cols else 0.0

    day = pd.to_numeric(df.get("dayQty"), errors="coerce")
    day = day.where(day.notna() & (day > 0), df["mkt_day_qty_from_buckets"])
    df["mkt_day_qty"] = day

    denom = df["mkt_day_qty"].replace(0, np.nan)

    # 17:45-18:00 HKT = 15:15-15:30 IST, the window the day-1 mail calls CTS.
    df["mkt_cts_window_qty"] = _window_qty(df, ("CAS_REFCALC", "CAS_ENTRY_LM", "CAS_ENTRY_LO"))
    # 18:00-18:05 HKT = the auction print.
    df["mkt_cas_window_qty"] = _window_qty(df, (C.AUCTION_PRINT_BUCKET,))
    # 17:30-18:00 HKT = 15:00-15:30 IST, the historical closing bin.
    df["mkt_clsbin_qty"] = _window_qty(
        df, ("CTS_FINAL15", "CAS_REFCALC", "CAS_ENTRY_LM", "CAS_ENTRY_LO")
    )

    df["mkt_cts_window_pct"] = df["mkt_cts_window_qty"] / denom * 100.0
    df["mkt_cas_window_pct"] = df["mkt_cas_window_qty"] / denom * 100.0
    df["mkt_cts_plus_cas_pct"] = df["mkt_cts_window_pct"] + df["mkt_cas_window_pct"]
    df["mkt_clsbin_pct"] = df["mkt_clsbin_qty"] / denom * 100.0

    df["vs_hist_clsbin_pp"] = df["mkt_clsbin_pct"] - C.BENCHMARKS.hist_clsbin_avg
    df["vs_day1_total_pp"] = df["mkt_cts_plus_cas_pct"] - C.BENCHMARKS.day1_total_share
    return df


def our_volume_profile(ex: pd.DataFrame) -> pd.DataFrame:
    """Our own filled quantity per sym per session bucket."""
    if ex is None or ex.empty:
        return pd.DataFrame(columns=["sym"])
    f = ex[ex["is_fill"]].copy()
    if f.empty:
        return pd.DataFrame(columns=["sym"])
    f["bucket"] = np.where(f["is_close"] & (f["bucket"] == ""), C.AUCTION_PRINT_BUCKET, f["bucket"])
    piv = f.pivot_table(index="sym", columns="bucket", values="fillsize", aggfunc="sum").fillna(0.0)
    piv.columns = [f"our_{c}_qty" for c in piv.columns]
    piv["our_day_qty"] = piv.sum(axis=1)
    close = f[f["is_close"]].groupby("sym")["fillsize"].sum()
    piv["our_close_qty"] = close
    piv["our_close_qty"] = piv["our_close_qty"].fillna(0.0)
    return piv.reset_index()


def participation_rates(sym_stats: pd.DataFrame) -> pd.DataFrame:
    """Our share of the printed volume, overall and in the auction."""
    df = sym_stats.copy()
    df["our_share_day_pct"] = (
        pd.to_numeric(df.get("our_day_qty"), errors="coerce")
        / pd.to_numeric(df.get("mkt_day_qty"), errors="coerce").replace(0, np.nan)
        * 100.0
    )
    df["our_share_close_pct"] = (
        pd.to_numeric(df.get("our_close_qty"), errors="coerce")
        / pd.to_numeric(df.get("mkt_cas_window_qty"), errors="coerce").replace(0, np.nan)
        * 100.0
    )
    df["our_close_pct_of_our_day"] = (
        pd.to_numeric(df.get("our_close_qty"), errors="coerce")
        / pd.to_numeric(df.get("our_day_qty"), errors="coerce").replace(0, np.nan)
        * 100.0
    )
    return df


# --------------------------------------------------------------------------- #
# Slippage                                                                     #
# --------------------------------------------------------------------------- #

def _signed_bps(bench: pd.Series, achieved: pd.Series, sign: pd.Series) -> pd.Series:
    """Positive = we beat the benchmark, whichever side we were on."""
    bench = pd.to_numeric(bench, errors="coerce")
    achieved = pd.to_numeric(achieved, errors="coerce")
    sign = pd.to_numeric(sign, errors="coerce")
    return (bench - achieved) / bench.replace(0, np.nan) * BPS * sign


def slippage(orders: pd.DataFrame) -> pd.DataFrame:
    """Close capture, benchmark performance, and the cost of the residual."""
    df = orders.copy()
    sign = pd.to_numeric(df.get("sidesign"), errors="coerce")
    close_px = pd.to_numeric(df.get("close_px"), errors="coerce")
    cts_px = pd.to_numeric(df.get("cts_end_px"), errors="coerce")

    df["close_capture_bps"] = _signed_bps(close_px, df.get("close_vwap"), sign)
    df["perf_vs_close_bps"] = _signed_bps(close_px, df.get("exec_vwap"), sign)
    if "target_strike" in df.columns:
        df["perf_vs_strike_bps"] = _signed_bps(df["target_strike"], df.get("exec_vwap"), sign)
    if "target_vwap" in df.columns:
        df["perf_vs_vwap_bps"] = _signed_bps(df["target_vwap"], df.get("exec_vwap"), sign)

    # How the stock behaved between the end of continuous and the close print.
    # Positive = the move went against the side of the order.
    df["close_move_bps"] = (close_px / cts_px.replace(0, np.nan) - 1.0) * BPS
    df["adverse_move_bps"] = df["close_move_bps"] * sign

    residual = pd.to_numeric(df.get("residual"), errors="coerce").clip(lower=0)
    df["residual_notional_at_close"] = residual * close_px
    # What completing the residual in the auction would have cost, measured
    # against the last continuous print.  Positive = missing the close was the
    # cheaper outcome; negative = the close was the better price and we missed it.
    df["missed_close_pnl"] = residual * (close_px - cts_px) * sign
    return df


# --------------------------------------------------------------------------- #
# Timing & compliance                                                          #
# --------------------------------------------------------------------------- #

def timing_flags(wo: pd.DataFrame) -> pd.DataFrame:
    """Per parent: when close child orders hit the market vs the CAS deadlines."""
    if wo is None or wo.empty:
        return pd.DataFrame(columns=["id_target"])
    cw = wo[wo["is_close"]].copy()
    if cw.empty:
        return pd.DataFrame(columns=["id_target"])

    send = pd.to_timedelta(cw["send_time"], errors="coerce")
    onmkt = pd.to_timedelta(cw.get("t_on_market"), errors="coerce") if "t_on_market" in cw.columns else send
    gen = pd.to_timedelta(cw.get("t_gen"), errors="coerce") if "t_gen" in cw.columns else send

    cw["_send"] = send
    cw["_onmkt"] = onmkt
    cw["_latency_ms"] = (onmkt - gen).dt.total_seconds() * 1000.0

    cw["_in_no_action"] = (send >= td(C.CAS_START)) & (send < td(C.REF_CALC_END))
    cw["_after_random"] = send >= td(C.RANDOM_CLOSE_START)
    cw["_after_entry"] = send >= td(C.ENTRY_LO_END)
    cw["_mkt_in_limit_only"] = (
        cw["is_market_order"]
        & (send >= td(C.ENTRY_LO_START))
        & (send < td(C.ENTRY_LO_END))
    )

    g = cw.groupby("id_target", sort=False)
    out = pd.DataFrame({
        "close_first_send": g["_send"].min(),
        "close_last_send": g["_send"].max(),
        "close_first_on_market": g["_onmkt"].min(),
        "close_send_latency_ms_max": g["_latency_ms"].max(),
        "flag_action_in_no_action_window": g["_in_no_action"].any(),
        "flag_sent_after_random_close": g["_after_random"].any(),
        "flag_sent_after_entry_closed": g["_after_entry"].any(),
        "flag_market_order_in_limit_only": g["_mkt_in_limit_only"].any(),
    })
    out["close_first_send_bucket"] = S.session_of(out["close_first_send"])
    return out.reset_index()


# --------------------------------------------------------------------------- #
# Mix tables: size / make / fill rate by order type, basket, flow, venue        #
# --------------------------------------------------------------------------- #

#: Columns every mix table carries, in this order, after its grouping keys.
MIX_METRIC_COLS = [
    "n_child_orders", "n_parents", "n_syms",
    "size", "make", "filled_qty",
    "fill_rate_pct", "make_pct_of_size", "pct_of_size",
]


def child_order_mix_base(
    wo: pd.DataFrame, ex: pd.DataFrame, orders: pd.DataFrame
) -> pd.DataFrame:
    """Child orders carrying their parent's flow/basket and their filled qty.

    The mix tables live at the *child* level: `venue` only exists there, and
    market-vs-limit is a child-order property -- that is the distinction the
    exchange enforces during the limit-only phase.  `filled_qty` comes from the
    execution tape keyed on `id_work`, not from the child order's own state, so
    a partial fill counts for what it actually did.
    """
    if wo is None or wo.empty:
        return pd.DataFrame(
            columns=["id_target", "sym", "flow", "basket", "venue", "otype_kind",
                     "phase", "size", "make", "filled_qty"]
        )

    df = wo.copy()
    df["otype_kind"] = df.get("otype", "").map(CL.otype_kind)
    df["venue"] = df.get("venue", "").astype(str).replace("", "(unknown)")
    df["size"] = pd.to_numeric(df.get("size"), errors="coerce").fillna(0.0)
    df["make"] = (
        pd.to_numeric(df["make"], errors="coerce").fillna(0.0)
        if "make" in df.columns else 0.0
    )

    filled = pd.Series(dtype=float)
    if ex is not None and not ex.empty and "id_work" in ex.columns:
        f = ex[ex["is_fill"]]
        if not f.empty:
            filled = f.groupby("id_work")["fillsize"].sum()
    df["filled_qty"] = (
        df["id_work"].map(filled).fillna(0.0)
        if "id_work" in df.columns and not filled.empty else 0.0
    )

    # Parent attributes.  `flow` is only on the parent, and the parent frame has
    # already been filtered, so an inner-style lookup also drops child orders
    # whose parent was excluded.
    if orders is not None and not orders.empty:
        keep = [c for c in ("id_target", "flow", "basket") if c in orders.columns]
        df = df.merge(orders[keep].drop_duplicates("id_target"), on="id_target", how="left")
    for col in ("flow", "basket"):
        if col not in df.columns:
            df[col] = ""
        df[col] = df[col].fillna("(unknown)").astype(str)
    return df


def mix_by(base: pd.DataFrame, keys: list[str], *, total_row: bool = True) -> pd.DataFrame:
    """Aggregate `child_order_mix_base` over `keys`.

    size / make / filled quantity, plus the two rates the desk reads them by:
    `fill_rate_pct` = filled / size and `make_pct_of_size` = make / size.
    `pct_of_size` is the group's share of the whole table, so a rate on a
    rounding-error-sized bucket is easy to discount.
    """
    cols = keys + MIX_METRIC_COLS
    if base is None or base.empty or not set(keys).issubset(base.columns):
        return pd.DataFrame(columns=cols)

    g = base.groupby(keys, dropna=False, sort=False)
    out = pd.DataFrame({
        "n_child_orders": g.size(),
        "n_parents": g["id_target"].nunique(),
        "n_syms": g["sym"].nunique() if "sym" in base.columns else g.size() * 0,
        "size": g["size"].sum(),
        "make": g["make"].sum(),
        "filled_qty": g["filled_qty"].sum(),
    }).reset_index()

    if total_row and len(out) > 1:
        tot = {k: "TOTAL" if i == 0 else "" for i, k in enumerate(keys)}
        tot |= {
            "n_child_orders": len(base),
            "n_parents": base["id_target"].nunique(),
            "n_syms": base["sym"].nunique() if "sym" in base.columns else 0,
            "size": base["size"].sum(),
            "make": base["make"].sum(),
            "filled_qty": base["filled_qty"].sum(),
        }
        out = pd.concat([out.sort_values("size", ascending=False),
                         pd.DataFrame([tot])], ignore_index=True)
    else:
        out = out.sort_values("size", ascending=False, ignore_index=True)

    size = out["size"].replace(0, np.nan)
    grand = float(base["size"].sum())
    out["fill_rate_pct"] = out["filled_qty"] / size * 100.0
    out["make_pct_of_size"] = out["make"] / size * 100.0
    out["pct_of_size"] = out["size"] / grand * 100.0 if grand else np.nan
    return out[cols]

"""Pipeline: pull, join, classify, measure.

Split in two on purpose:

* `load_frames()` is the only part that talks to kdb;
* `assemble()` is pure pandas.

That split is what makes `tools/selftest.py` able to exercise the whole
analytical layer -- the waterfall, the band checks, the volume shares, the
writers -- against synthetic frames, with no database in the loop.
"""

from __future__ import annotations

import datetime as dt
import sys
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from . import classify as CL
from . import config as C
from . import kdbio as K
from . import loaders as L
from . import metrics as M
from . import sessions as S
from . import universe as U


@dataclass
class RawFrames:
    """Everything read out of kdb, before any joining."""

    universe: pd.DataFrame
    targets: pd.DataFrame
    states: pd.DataFrame
    workorders: pd.DataFrame
    executions: pd.DataFrame
    alerts: pd.DataFrame
    profile_wide: pd.DataFrame = field(default_factory=lambda: pd.DataFrame(columns=["sym"]))
    sym_market: pd.DataFrame = field(default_factory=lambda: pd.DataFrame(columns=["sym"]))
    ref_px: pd.DataFrame = field(default_factory=lambda: pd.DataFrame(columns=["sym"]))


@dataclass
class ReportData:
    date: dt.date | None
    mode: str
    flow: str
    universe: pd.DataFrame
    orders: pd.DataFrame
    non_participation: pd.DataFrame
    rejections: pd.DataFrame
    cancellations: pd.DataFrame
    workorders: pd.DataFrame
    executions: pd.DataFrame
    mix_otype_basket: pd.DataFrame
    mix_flow_venue_otype: pd.DataFrame
    sym_stats: pd.DataFrame
    benchmark: pd.DataFrame
    ref_prices: pd.DataFrame
    timing: pd.DataFrame
    alerts: pd.DataFrame
    reconciliation: pd.DataFrame
    summary: pd.DataFrame
    sessions: pd.DataFrame
    warnings: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #

def _flow_mask(targets: pd.DataFrame, flow: str) -> pd.Series:
    f = targets["basket"].map(C.flow_of)
    if flow == "both":
        return pd.Series(True, index=targets.index)
    return f == (C.FLOW_SILK if flow == "silk" else C.FLOW_AGENCY)


def _as_td(df: pd.DataFrame, cols) -> None:
    for c in cols:
        if c in df.columns:
            df[c] = pd.to_timedelta(df[c], errors="coerce")


def _without_ids(df: pd.DataFrame, ids: set) -> pd.DataFrame:
    """Drop every row belonging to one of `ids` (no-op if there is no id_target)."""
    if df is None or df.empty or "id_target" not in df.columns:
        return df
    return df[~df["id_target"].isin(ids)].reset_index(drop=True)


# --------------------------------------------------------------------------- #
# 1. Load                                                                      #
# --------------------------------------------------------------------------- #

def load_frames(
    pool: K.ConnectionPool,
    date: dt.date | None,
    flow: str,
    *,
    isins: list[str] | None = None,
    skip_market_data: bool = False,
    universe_csv=C.UNIVERSE_FILE_CANDIDATES,
    use_universe_csv: bool = True,
    verbose: bool = True,
) -> tuple[RawFrames, list[str]]:
    warnings: list[str] = []

    def say(msg: str) -> None:
        if verbose:
            print(msg, flush=True)

    # -- universe ----------------------------------------------------------- #
    # `pool.get` is passed unevaluated: with a snapshot csv in place the REF
    # connection is never opened.
    uni, uni_source = U.resolve_universe(
        lambda: pool.get("ref"), date, isins or [],
        csv_path=universe_csv, prefer_csv=use_universe_csv, verbose=verbose,
    )
    warnings.append(f"universe source: {uni_source}")
    if uni.empty:
        raise SystemExit(
            "[fatal] the CAS universe came back empty -- check the date, the "
            f"ISIN whitelist in {C.ISIN_FILE}, and the REF instance."
        )
    syms = sorted(uni["sym"].astype(str).unique())
    say(f"[info] universe: {len(syms)} CAS-eligible syms")

    # -- parent orders ------------------------------------------------------ #
    oms = pool.get("oms")
    targets = L.load_targets(oms, date, syms)
    if targets.empty:
        warnings.append("no parent order touched the CAS universe on this date")
        say("[warn] no targets found")
    else:
        targets["flow"] = targets["basket"].map(C.flow_of)
        targets = targets[_flow_mask(targets, flow)].reset_index(drop=True)
    say(f"[info] parent orders ({flow}): {len(targets)}")

    ids = sorted(targets["id_target"].dropna().astype(int).unique()) if not targets.empty else []

    states = L.load_target_states(oms, date, ids) if ids else pd.DataFrame()
    wo_raw = L.load_workorders(oms, date, ids) if ids else pd.DataFrame()
    ex_raw = L.load_executions(oms, date, ids) if ids else pd.DataFrame()
    alerts = L.load_alerts(oms, date, ids) if ids else pd.DataFrame()
    say(
        f"[info] states={len(states)} workorders={len(wo_raw)} "
        f"executions={len(ex_raw)} alerts={len(alerts)}"
    )

    raw = RawFrames(
        universe=uni, targets=targets, states=states,
        workorders=wo_raw, executions=ex_raw, alerts=alerts,
    )

    # -- market data -------------------------------------------------------- #
    if skip_market_data:
        warnings.append("market-data section skipped (--no-market-data)")
        return raw, warnings

    try:
        qatt = pool.get("qatt")
        profile = L.load_volume_profile(qatt, date, syms)
        raw.profile_wide = M.pivot_market_profile(profile)

        cts = L.load_window_stats(
            qatt, date, syms, C.REF_VWAP_START, C.REF_VWAP_END, prefix="cts_"
        )
        last_trade = L.load_last_trade_before(qatt, date, syms, C.CTS_END)
        day_vol = L.load_day_volume(qatt, date, syms)

        raw.ref_px = M.reference_price(uni, cts, last_trade)
        raw.sym_market = M.market_volume_shares(raw.profile_wide, day_vol)
        say(f"[info] market data: {len(raw.profile_wide)} syms with a volume profile")
    except Exception as exc:  # pragma: no cover
        warnings.append(f"market data unavailable: {exc}")
        print(f"[warn] market data unavailable: {exc}", file=sys.stderr)

    return raw, warnings


# --------------------------------------------------------------------------- #
# 2. Assemble                                                                  #
# --------------------------------------------------------------------------- #

def assemble(
    date: dt.date | None,
    mode: str,
    flow: str,
    raw: RawFrames,
    warnings: list[str] | None = None,
    *,
    drop_unfilled: bool = C.DROP_UNFILLED_ORDERS,
    require_close_wo: bool = C.REQUIRE_CLOSE_WORKORDER,
) -> ReportData:
    warnings = list(warnings or [])

    wo = CL.enrich_workorders(raw.workorders)
    ex = CL.enrich_executions(raw.executions, wo)
    states = raw.states
    alerts = raw.alerts

    orders = raw.targets.copy()
    if not orders.empty:
        if "flow" not in orders.columns:
            orders["flow"] = orders["basket"].map(C.flow_of)
        _as_td(orders, ["time", "t_start", "t_end", "t_gen", "t_oes_load"])

        for extra in (
            CL.summarise_states(states),
            CL.summarise_fills(ex),
            CL.summarise_close_workorders(wo),
            CL.summarise_alerts(alerts),
            M.timing_flags(wo),
        ):
            if extra is not None and not extra.empty:
                orders = orders.merge(extra, on="id_target", how="left")

        orders = _fill_defaults(orders)

        # orders that executed nothing --------------------------------------- #
        # Dropped here, before anything is counted, so the waterfall, the mix
        # tables, the per-sym stats and the participation rate all see the same
        # population: orders that traded, fully or in part.  A rejected order
        # that still completed a percentage stays -- the test is on quantity.
        if drop_unfilled:
            drop = CL.nothing_executed_mask(orders)
            n_drop = int(drop.sum())
            if n_drop:
                dropped_ids = set(orders.loc[drop, "id_target"])
                dropped_qty = float(orders.loc[drop, "size"].sum())
                orders = orders[~drop].reset_index(drop=True)
                wo = _without_ids(wo, dropped_ids)
                ex = _without_ids(ex, dropped_ids)
                states = _without_ids(states, dropped_ids)
                alerts = _without_ids(alerts, dropped_ids)
                warnings.append(
                    f"{n_drop} parent order{'s' if n_drop != 1 else ''} "
                    f"({dropped_qty:,.0f} shares) excluded: nothing executed "
                    f"at all (--keep-unfilled to keep them)"
                )

        # orders that never reached the auction ------------------------------ #
        # Narrows the report to close participants.  Note what this costs: the
        # NOT_SENT population disappears, and so does the non-participation
        # waterfall, which can only fire on orders that never got there.
        if require_close_wo:
            cutoff = C.CLOSE_WORKORDER_AFTER
            qualifying = CL.targets_with_close_workorder(wo, cutoff)
            drop = ~orders["id_target"].isin(qualifying)
            n_drop = int(drop.sum())
            if n_drop:
                dropped_ids = set(orders.loc[drop, "id_target"])
                dropped_qty = float(orders.loc[drop, "size"].sum())
                orders = orders[~drop].reset_index(drop=True)
                wo = _without_ids(wo, dropped_ids)
                ex = _without_ids(ex, dropped_ids)
                states = _without_ids(states, dropped_ids)
                alerts = _without_ids(alerts, dropped_ids)
                warnings.append(
                    f"{n_drop} parent order{'s' if n_drop != 1 else ''} "
                    f"({dropped_qty:,.0f} shares) excluded: no CLOSE-venue child "
                    f"order sent at or after {cutoff.strftime('%H:%M')} HKT "
                    f"(--keep-no-close to keep them, and to get the "
                    f"non-participation waterfall back)"
                )

        # market context ---------------------------------------------------- #
        if raw.ref_px is not None and not raw.ref_px.empty:
            keep = [c for c in ("sym", "ref_price", "ref_source", "band_lo", "band_hi",
                                "no_market_data") if c in raw.ref_px.columns]
            orders = orders.merge(raw.ref_px[keep], on="sym", how="left")
        for col in ("ref_price", "band_lo", "band_hi"):
            if col not in orders.columns:
                orders[col] = np.nan
        if "no_market_data" not in orders.columns:
            orders["no_market_data"] = False
        orders["no_market_data"] = orders["no_market_data"].fillna(False).astype(bool)

        if raw.profile_wide is not None and not raw.profile_wide.empty:
            keep = [c for c in ("sym", "close_px", "cts_end_px", "close_print_qty")
                    if c in raw.profile_wide.columns]
            orders = orders.merge(raw.profile_wide[keep], on="sym", how="left")
        for col in ("close_px", "cts_end_px", "close_print_qty"):
            if col not in orders.columns:
                orders[col] = np.nan

        orders = M.apply_price_band(orders)
        orders = CL.diagnose(orders)
        orders = M.slippage(orders)
        orders = orders.sort_values(["flow", "sym", "id_target"], ignore_index=True)

    non_part = (
        orders[orders["participation"] != "FILLED_IN_CLOSE"].copy()
        if not orders.empty else pd.DataFrame()
    )
    rej = CL.rejections(wo, ex, orders)
    cxl = CL.cancellations(wo, orders)

    mix_base = M.child_order_mix_base(wo, ex, orders)
    mix_otype_basket = M.mix_by(mix_base, ["otype_kind", "basket"])
    mix_flow_venue_otype = M.mix_by(mix_base, ["flow", "venue", "otype_kind"])

    sym_stats = _build_sym_stats(orders, raw.sym_market, ex)
    bench = _build_benchmark(sym_stats)
    recon = _build_reconciliation(orders, ex, states, wo)
    summary = _build_summary(orders, rej, cxl)
    timing = _build_timing(orders)

    return ReportData(
        date=date, mode=mode, flow=flow,
        universe=raw.universe,
        orders=orders,
        non_participation=non_part,
        rejections=rej,
        cancellations=cxl,
        workorders=wo,
        executions=ex,
        mix_otype_basket=mix_otype_basket,
        mix_flow_venue_otype=mix_flow_venue_otype,
        sym_stats=sym_stats,
        benchmark=bench,
        ref_prices=raw.ref_px,
        timing=timing,
        alerts=alerts,
        reconciliation=recon,
        summary=summary,
        sessions=S.session_table(),
        warnings=warnings,
    )


def build_report(
    pool: K.ConnectionPool,
    date: dt.date | None,
    flow: str,
    *,
    isins: list[str] | None = None,
    skip_market_data: bool = False,
    universe_csv=C.UNIVERSE_FILE_CANDIDATES,
    use_universe_csv: bool = True,
    drop_unfilled: bool = C.DROP_UNFILLED_ORDERS,
    require_close_wo: bool = C.REQUIRE_CLOSE_WORKORDER,
    verbose: bool = True,
) -> ReportData:
    raw, warnings = load_frames(
        pool, date, flow, isins=isins,
        skip_market_data=skip_market_data,
        universe_csv=universe_csv, use_universe_csv=use_universe_csv,
        verbose=verbose,
    )
    data = assemble(
        date, pool.mode, flow, raw, warnings,
        drop_unfilled=drop_unfilled, require_close_wo=require_close_wo,
    )
    if verbose:
        for w in data.warnings[len(warnings):]:
            print(f"[info] {w}", flush=True)
    return data


# --------------------------------------------------------------------------- #
# Column hygiene                                                               #
# --------------------------------------------------------------------------- #

_NUMERIC_DEFAULTS = {
    "exec_qty": 0.0, "close_qty": 0.0, "cont_qty": 0.0,
    "n_close_wo": 0.0, "n_alerts": 0.0, "n_alerts_cas": 0.0,
    "close_wo_market_orders": 0.0,
}
_BOOL_COLS = (
    "close_wo_rejected", "close_wo_cancelled",
    "cancelled_before_cas", "terminal_before_cas",
)
_TEXT_COLS = (
    "close_wo_reject_reasons", "close_wo_cancel_reasons", "alert_types_cas",
    "final_state", "state_at_cas", "terminal_state",
)


def _fill_defaults(orders: pd.DataFrame) -> pd.DataFrame:
    for col, default in _NUMERIC_DEFAULTS.items():
        if col not in orders.columns:
            orders[col] = default
        orders[col] = pd.to_numeric(orders[col], errors="coerce").fillna(default)
    for col in _BOOL_COLS:
        if col not in orders.columns:
            orders[col] = False
        orders[col] = orders[col].fillna(False).astype(bool)
    for col in _TEXT_COLS:
        if col not in orders.columns:
            orders[col] = ""
        orders[col] = orders[col].fillna("")
    for col in ("open_at_cas", "final_make", "final_open", "max_make_close",
                "max_commit_close", "first_state_time", "terminal_time"):
        if col not in orders.columns:
            orders[col] = np.nan

    orders["size"] = pd.to_numeric(orders["size"], errors="coerce").fillna(0.0)

    # Executed and residual quantity come from the *latest* target_state row:
    # `make` is what executed, `open` is what is left.  That row is the OMS's own
    # final word on the parent, so it beats re-adding the execution tape.
    #
    # The tape total is kept as `exec_qty_fills` -- it is what feeds exec_vwap,
    # and `_build_reconciliation` reports every parent where the two disagree.
    # Parents with no state history at all fall back to the tape, flagged by
    # `qty_source`.
    orders["exec_qty_fills"] = orders["exec_qty"]
    state_make = pd.to_numeric(orders["final_make"], errors="coerce")
    state_open = pd.to_numeric(orders["final_open"], errors="coerce")
    orders["qty_source"] = np.where(state_make.notna(), "TARGET_STATE", "EXECUTIONS")
    orders["exec_qty"] = state_make.fillna(orders["exec_qty_fills"])
    orders["residual"] = state_open.fillna(orders["size"] - orders["exec_qty"])
    orders["fill_pct"] = np.where(
        orders["size"] > 0, orders["exec_qty"] / orders["size"] * 100.0, np.nan
    )
    orders["close_pct_of_order"] = np.where(
        orders["size"] > 0, orders["close_qty"] / orders["size"] * 100.0, np.nan
    )
    orders["close_pct_of_executed"] = np.where(
        orders["exec_qty"] > 0, orders["close_qty"] / orders["exec_qty"] * 100.0, np.nan
    )
    orders["cas_eligible"] = True
    src = orders["first_state_time"] if orders["first_state_time"].notna().any() else orders.get("time")
    orders["arrival_bucket"] = S.session_of(pd.to_timedelta(src, errors="coerce"))
    return orders


# --------------------------------------------------------------------------- #
# Aggregates                                                                   #
# --------------------------------------------------------------------------- #

def _build_sym_stats(
    orders: pd.DataFrame, sym_market: pd.DataFrame, ex: pd.DataFrame
) -> pd.DataFrame:
    ours = M.our_volume_profile(ex)

    if orders.empty:
        base = pd.DataFrame(columns=["sym"])
    else:
        g = orders.groupby("sym", sort=False)
        base = pd.DataFrame({
            "n_orders": g.size(),
            "order_qty": g["size"].sum(),
            "exec_qty": g["exec_qty"].sum(),
            "close_qty": g["close_qty"].sum(),
            "residual": g["residual"].sum(),
            "n_participated": g["participation"].apply(lambda s: int((s == "FILLED_IN_CLOSE").sum())),
            "n_not_sent": g["participation"].apply(lambda s: int((s == "NOT_SENT").sum())),
            "n_sent_not_filled": g["participation"].apply(lambda s: int((s == "SENT_NOT_FILLED").sum())),
        }).reset_index()

    out = base
    if sym_market is not None and not sym_market.empty:
        out = out.merge(sym_market, on="sym", how="left")
    if ours is not None and not ours.empty:
        out = out.merge(ours, on="sym", how="left")
    if out.empty:
        return out

    out = M.participation_rates(out)
    if "n_orders" in out.columns:
        out = out[pd.to_numeric(out["n_orders"], errors="coerce").fillna(0) > 0]
    return out.sort_values("sym", ignore_index=True)


def _build_benchmark(sym_stats: pd.DataFrame) -> pd.DataFrame:
    """Volume-weighted market shares next to the desk's reference numbers."""
    b = C.BENCHMARKS

    def wpct(num_col: str, den_col: str) -> float:
        if sym_stats is None or sym_stats.empty:
            return float("nan")
        if num_col not in sym_stats or den_col not in sym_stats:
            return float("nan")
        num = pd.to_numeric(sym_stats[num_col], errors="coerce").fillna(0).sum()
        den = pd.to_numeric(sym_stats[den_col], errors="coerce").fillna(0).sum()
        return float(num / den * 100.0) if den else float("nan")

    cts = wpct("mkt_cts_window_qty", "mkt_day_qty")
    cas = wpct("mkt_cas_window_qty", "mkt_day_qty")

    rows = [
        {
            "metric": "Market: closing bin share (17:30-18:00 HKT / 15:00-15:30 IST)",
            "value_pct": wpct("mkt_clsbin_qty", "mkt_day_qty"),
            "benchmark_pct": b.hist_clsbin_avg,
            "benchmark_label": f"historical avg {b.hist_clsbin_avg:.2f}% "
                               f"(range {b.hist_clsbin_min:.2f}-{b.hist_clsbin_max:.2f}%)",
        },
        {
            "metric": "Market: CTS window share (17:45-18:00 HKT / 15:15-15:30 IST)",
            "value_pct": cts,
            "benchmark_pct": b.day1_cts_share,
            "benchmark_label": f"day 1 {b.day1_cts_share:.2f}%",
        },
        {
            "metric": "Market: close auction share (18:00-18:05 HKT / 15:30-15:35 IST)",
            "value_pct": cas,
            "benchmark_pct": b.day1_cas_share,
            "benchmark_label": f"day 1 {b.day1_cas_share:.2f}%",
        },
        {
            "metric": "Market: combined end-of-day activity",
            "value_pct": cts + cas,
            "benchmark_pct": b.day1_total_share,
            "benchmark_label": f"day 1 {b.day1_total_share:.2f}%",
        },
        {
            "metric": "Us: share of the auction print we accounted for",
            "value_pct": wpct("our_close_qty", "mkt_cas_window_qty"),
            "benchmark_pct": float("nan"),
            "benchmark_label": "",
        },
        {
            "metric": "Us: share of our day's volume done in the auction",
            "value_pct": wpct("our_close_qty", "our_day_qty"),
            "benchmark_pct": float("nan"),
            "benchmark_label": "",
        },
    ]
    df = pd.DataFrame(rows)
    df["delta_pp"] = df["value_pct"] - df["benchmark_pct"]
    return df


def _build_timing(orders: pd.DataFrame) -> pd.DataFrame:
    if orders.empty:
        return pd.DataFrame()
    cols = [c for c in (
        "id_target", "flow", "basket", "sym", "side", "size", "algo", "trader",
        "participation", "close_first_send", "close_first_send_bucket",
        "close_first_on_market", "close_send_latency_ms_max",
        "flag_action_in_no_action_window", "flag_sent_after_random_close",
        "flag_sent_after_entry_closed", "flag_market_order_in_limit_only",
        "close_wo_market_orders", "t_end",
    ) if c in orders.columns]
    df = orders[cols].copy()
    flags = [c for c in df.columns if c.startswith("flag_")]
    if flags:
        for c in flags:
            df[c] = df[c].fillna(False).astype(bool)
        df["n_flags"] = df[flags].sum(axis=1)
        if "close_first_send" in df.columns:
            df = df[(df["n_flags"] > 0) | df["close_first_send"].notna()]
        else:
            df = df[df["n_flags"] > 0]
    return df.reset_index(drop=True)


def _build_reconciliation(
    orders: pd.DataFrame, ex: pd.DataFrame, states: pd.DataFrame,
    wo: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Cross-checks whose failure means a number in the report is suspect."""
    rows = []

    def add(check: str, ok: bool, detail: str) -> None:
        rows.append({"check": check, "status": "OK" if ok else "REVIEW", "detail": detail})

    if orders.empty:
        add("parent orders present", False, "no parent orders after the flow filter")
        return pd.DataFrame(rows)

    # `make` / `open` off the last state row are the reported numbers; the tape
    # is the independent one.  A gap means one of the two feeds is incomplete.
    if "exec_qty_fills" in orders.columns:
        on_state = orders["qty_source"] == "TARGET_STATE"
        diff = (orders["exec_qty"] - orders["exec_qty_fills"]).abs().where(on_state)
        bad = int((diff.fillna(0) > 0).sum())
        add(
            "target_state.make agrees with the execution tape",
            bad == 0,
            f"{bad} of {int(on_state.sum())} parent orders disagree"
            + (f"; worst gap {diff.max():,.0f} shares" if bad else ""),
        )

    no_state = int((orders["qty_source"] != "TARGET_STATE").sum())
    add("every parent order has a usable final state row", no_state == 0,
        f"{no_state} parent orders fell back to the execution tape for "
        f"executed / residual quantity")

    # make + open should account for the whole parent.
    gap = (orders["size"] - orders["exec_qty"] - orders["residual"]).abs()
    bad = int((gap.fillna(0) > 0).sum())
    add("executed + residual matches the order quantity", bad == 0,
        f"{bad} of {len(orders)} parent orders disagree"
        + (f"; worst gap {gap.max():,.0f} shares" if bad else ""))

    over = int((orders["exec_qty"] > orders["size"]).sum())
    add("executed quantity never exceeds order quantity", over == 0,
        f"{over} parent orders over-filled")

    # close_qty stays tape-based while exec_qty is state-based, so the ratio can
    # only be trusted while this holds.
    over_close = int((orders["close_qty"] > orders["exec_qty"]).sum())
    add("close quantity never exceeds executed quantity", over_close == 0,
        f"{over_close} parent orders show more traded in the auction than "
        f"target_state.make reports in total")

    # The invariant behind the whole close section: nothing is credited to the
    # auction unless a child order actually went to a CLOSE venue.  If the clock
    # ever creeps back into the classification this is what catches it.
    ghost = int(((orders["close_qty"] > 0) & (orders["n_close_wo"] <= 0)).sum())
    add("close quantity only ever comes from a close-venue child order", ghost == 0,
        f"{ghost} parent orders show auction volume without owning a single "
        f"workorder whose venue contains CLOSE")

    if "participation" in orders.columns:
        ghost_part = int(
            ((orders["participation"] != "NOT_SENT") & (orders["n_close_wo"] <= 0)).sum()
        )
        add("only parents with a close-venue child order leave the NOT_SENT bucket",
            ghost_part == 0,
            f"{ghost_part} parent orders were classified as having reached the "
            f"auction without a close-venue workorder")

    if wo is not None and not wo.empty and "id_work" in wo.columns:
        # A child order has one row per event; they must all agree on the venue,
        # otherwise `is_close` depends on which row wins.
        per_work = wo.groupby("id_work")["is_close"].nunique(dropna=False)
        split = int((per_work > 1).sum())
        add("each child order's venue is consistent across its rows", split == 0,
            f"{split} id_work values carry both a CLOSE and a non-CLOSE venue - "
            f"they are treated as close")

    if ex is not None and not ex.empty and "id_work" in ex.columns:
        # Close participation is decided purely by the child order's venue, so an
        # execution that cannot be traced to one is a fill we cannot attribute --
        # it silently counts as continuous.
        fills = ex[ex["is_fill"]] if "is_fill" in ex.columns else ex
        orphan = int(fills["id_work"].isna().sum())
        unmapped = 0
        if wo is not None and not wo.empty and "id_work" in wo.columns:
            known = set(wo["id_work"].dropna())
            unmapped = int((~fills["id_work"].dropna().isin(known)).sum())
        add("every fill traces back to a child order", orphan + unmapped == 0,
            f"{orphan} fills without an id_work, {unmapped} whose id_work is not "
            f"in the workorder table - those cannot be credited to the close")

    if states is not None and not states.empty:
        missing = set(orders["id_target"]) - set(states["id_target"])
        add("every parent order has a state history", not missing,
            f"{len(missing)} parent orders have no target_state rows")

    if "ref_price" in orders.columns:
        n_missing = int(orders["ref_price"].isna().sum())
        add("reference price resolved for every order", n_missing == 0,
            f"{n_missing} orders without a reference price - band checks skipped for those")

    unexplained = int((orders.get("reason_code", pd.Series(dtype=object)) == "UNEXPLAINED").sum())
    add("every non-participation has a diagnosed cause", unexplained == 0,
        f"{unexplained} parent orders fell through the waterfall")
    return pd.DataFrame(rows)


def _build_summary(
    orders: pd.DataFrame, rej: pd.DataFrame, cxl: pd.DataFrame
) -> pd.DataFrame:
    """One row per flow, plus TOTAL when both are present."""
    if orders.empty:
        return pd.DataFrame()

    def block(df: pd.DataFrame, label: str) -> dict:
        n = len(df)
        size = df["size"].sum()
        execd = df["exec_qty"].sum()
        closed = df["close_qty"].sum()
        part = df["participation"]

        rj = rej
        cx = cxl
        if label != "TOTAL":
            if rej is not None and not rej.empty and "flow" in rej.columns:
                rj = rej[rej["flow"] == label]
            if cxl is not None and not cxl.empty and "flow" in cxl.columns:
                cx = cxl[cxl["flow"] == label]

        def _count_col(df_: pd.DataFrame, col: str, value: str) -> int:
            if df_ is None or df_.empty or col not in df_.columns:
                return 0
            return int((df_[col] == value).sum())

        def phase_count(df_: pd.DataFrame, phase: str) -> int:
            if df_ is None or df_.empty or "phase" not in df_.columns:
                return 0
            return int((df_["phase"] == phase).sum())

        return {
            "flow": label,
            "parent_orders": n,
            "syms": df["sym"].nunique(),
            "order_qty": size,
            "executed_qty": execd,
            "residual_qty": df["residual"].sum(),
            "fill_pct": execd / size * 100.0 if size else np.nan,
            "close_qty": closed,
            "close_pct_of_executed": closed / execd * 100.0 if execd else np.nan,
            "orders_filled_in_close": int((part == "FILLED_IN_CLOSE").sum()),
            "orders_sent_not_filled": int((part == "SENT_NOT_FILLED").sum()),
            "orders_not_sent": int((part == "NOT_SENT").sum()),
            "participation_rate_pct": (part == "FILLED_IN_CLOSE").mean() * 100.0 if n else np.nan,
            "rejections_continuous": phase_count(rj, "CONTINUOUS"),
            "rejections_close": phase_count(rj, "CLOSE"),
            "rejections_plain": _count_col(rj, "rejection_type", C.REJECTION_PLAIN),
            "rejections_after_close": _count_col(
                rj, "rejection_type", C.REJECTION_AFTER_CLOSE),
            "cancellations_continuous": phase_count(cx, "CONTINUOUS"),
            "cancellations_close": phase_count(cx, "CLOSE"),
            "cancellations_plain": _count_col(cx, "cancel_type", C.CANCEL_PLAIN),
            "cancellations_after_close": _count_col(
                cx, "cancel_type", C.CANCEL_AFTER_CLOSE),
            "mean_close_capture_bps": df.get("close_capture_bps", pd.Series(dtype=float)).mean(),
            "mean_perf_vs_close_bps": df.get("perf_vs_close_bps", pd.Series(dtype=float)).mean(),
            "residual_notional_at_close": df.get(
                "residual_notional_at_close", pd.Series(dtype=float)
            ).sum(),
        }

    rows = [block(sub, flow) for flow, sub in orders.groupby("flow", sort=True)]
    if len(rows) > 1:
        rows.append(block(orders, "TOTAL"))
    return pd.DataFrame(rows)

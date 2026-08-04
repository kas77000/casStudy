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
    verbose: bool = True,
) -> tuple[RawFrames, list[str]]:
    warnings: list[str] = []

    def say(msg: str) -> None:
        if verbose:
            print(msg, flush=True)

    # -- universe ----------------------------------------------------------- #
    uni = U.fetch_universe(pool.get("ref"), date, isins or [])
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
) -> ReportData:
    warnings = list(warnings or [])

    wo = CL.enrich_workorders(raw.workorders)
    ex = CL.enrich_executions(raw.executions, wo)

    orders = raw.targets.copy()
    if not orders.empty:
        if "flow" not in orders.columns:
            orders["flow"] = orders["basket"].map(C.flow_of)
        _as_td(orders, ["time", "t_start", "t_end", "t_gen", "t_oes_load"])

        for extra in (
            CL.summarise_states(raw.states),
            CL.summarise_fills(ex),
            CL.summarise_close_workorders(wo),
            CL.summarise_alerts(raw.alerts),
            M.timing_flags(wo),
        ):
            if extra is not None and not extra.empty:
                orders = orders.merge(extra, on="id_target", how="left")

        orders = _fill_defaults(orders)

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

    sym_stats = _build_sym_stats(orders, raw.sym_market, ex)
    bench = _build_benchmark(sym_stats)
    recon = _build_reconciliation(orders, ex, raw.states)
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
        sym_stats=sym_stats,
        benchmark=bench,
        ref_prices=raw.ref_px,
        timing=timing,
        alerts=raw.alerts,
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
    verbose: bool = True,
) -> ReportData:
    raw, warnings = load_frames(
        pool, date, flow, isins=isins,
        skip_market_data=skip_market_data, verbose=verbose,
    )
    return assemble(date, pool.mode, flow, raw, warnings)


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
    for col in ("open_at_cas", "final_open", "max_make_close", "max_commit_close",
                "first_state_time", "terminal_time"):
        if col not in orders.columns:
            orders[col] = np.nan

    orders["size"] = pd.to_numeric(orders["size"], errors="coerce").fillna(0.0)
    orders["residual"] = orders["size"] - orders["exec_qty"]
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
    orders: pd.DataFrame, ex: pd.DataFrame, states: pd.DataFrame
) -> pd.DataFrame:
    """Cross-checks whose failure means a number in the report is suspect."""
    rows = []

    def add(check: str, ok: bool, detail: str) -> None:
        rows.append({"check": check, "status": "OK" if ok else "REVIEW", "detail": detail})

    if orders.empty:
        add("parent orders present", False, "no parent orders after the flow filter")
        return pd.DataFrame(rows)

    if "final_open" in orders.columns and orders["final_open"].notna().any():
        diff = (orders["residual"] - pd.to_numeric(orders["final_open"], errors="coerce")).abs()
        bad = int((diff.fillna(0) > 0).sum())
        add(
            "residual (size - fills) matches the final target_state.open",
            bad == 0,
            f"{bad} of {len(orders)} parent orders disagree"
            + (f"; worst gap {diff.max():,.0f} shares" if bad else ""),
        )

    over = int((orders["exec_qty"] > orders["size"]).sum())
    add("executed quantity never exceeds order quantity", over == 0,
        f"{over} parent orders over-filled")

    if ex is not None and not ex.empty and "id_work" in ex.columns:
        orphan = int(ex["id_work"].isna().sum())
        add("every execution carries an id_work", orphan == 0,
            f"{orphan} executions without a child order")

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
            "cancellations_continuous": phase_count(cx, "CONTINUOUS"),
            "cancellations_close": phase_count(cx, "CLOSE"),
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

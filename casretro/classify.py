"""Classification: order states, close participation, and the "why not" waterfall.

The three questions this module answers, in order:

1. *Did the parent order participate in the close?*  -> `PARTICIPATION`
2. *If not, why?*                                    -> `NOT_SENT_REASONS` / `SENT_REASONS`
3. *What got rejected, and on which side of 17:45?*  -> `rejections()`

The "why" is a waterfall, not a set of independent flags: the first condition
that fires wins, and the ones after it are still recorded in `reason_detail` so a
second cause is never hidden by the first.
"""

from __future__ import annotations

import re

import numpy as np
import pandas as pd

from . import config as C
from . import sessions as S
from .kdbio import td, td_to_str

# --------------------------------------------------------------------------- #
# Vocabulary                                                                   #
# --------------------------------------------------------------------------- #

PARTICIPATION = {
    "FILLED_IN_CLOSE": "Traded in the auction",
    "SENT_NOT_FILLED": "Child order reached the auction but did not trade",
    "NOT_SENT": "No child order was ever sent to the auction",
}

NOT_SENT_REASONS = {
    "NOT_CAS_ELIGIBLE": "Symbol is not in the CAS-eligible universe",
    "NO_CLOSE_INSTRUCTION": "Parent order carries doclose=0 - close participation was switched off",
    "FULLY_FILLED_BEFORE_CAS": "Parent order was already complete when continuous trading ended",
    "PARENT_CANCELLED_BEFORE_CAS": "Parent order was cancelled before the auction",
    "PARENT_DONE_BEFORE_CAS": "Parent order reached a terminal state before the auction",
    "ORDER_END_BEFORE_CAS": "t_end sits at or before the end of continuous (17:45 HKT)",
    "ORDER_ARRIVED_AFTER_ENTRY_CLOSED": "Order reached the algo after order entry closed (18:00 HKT)",
    "LIMIT_OUTSIDE_PRICE_BAND": "Client limit sits outside the +/-3% CAS price band - no child order could be priced",
    "ALGO_NEVER_COMMITTED_TO_CLOSE": "Residual was live but the algo never committed any quantity to the close",
    "BLOCKING_ALERT": "An alert fired on the parent order during the CAS window",
    "NO_MARKET_DATA": "No prints at all on the day - likely halted or not traded",
    "UNEXPLAINED": "No rule matched - needs manual investigation",
}

SENT_REASONS = {
    "CLOSE_ORDER_REJECTED": "The close child order was rejected",
    "CLOSE_ORDER_CANCELLED": "The close child order was cancelled before the match",
    "SENT_AFTER_ENTRY_CLOSED": "The close child order left after order entry closed (18:00 HKT)",
    "MARKET_ORDER_IN_LIMIT_ONLY_PHASE": "A market child order was sent during the limit-only phase (17:55-18:00 HKT)",
    "PRICE_OUTSIDE_PRICE_BAND": "The child order was priced outside the +/-3% CAS band",
    "NOT_MATCHED_IN_AUCTION": "The order stood in the auction but the clearing price never reached it",
    "UNEXPLAINED": "No rule matched - needs manual investigation",
}

STATE_FILLED = "FILLED"
STATE_REJECTED = "REJECTED"
STATE_CANCELLED = "CANCELLED"
STATE_LIVE = "LIVE"
STATE_OTHER = "OTHER"

_TERMINAL_PARENT = ("done", "complete", "finished", "filled", "end", "cxl", "cancel")

_RE_CXL = re.compile(r"^\s*(cxl|cancel)", re.I)
_RE_REJ = re.compile(r"rej", re.I)
_RE_FILL = re.compile(r"^\s*(fill|done|complete)", re.I)
_RE_MARKET_OTYPE = re.compile(r"^\s*(mkt|market|mo)\b", re.I)


# --------------------------------------------------------------------------- #
# Small string helpers                                                         #
# --------------------------------------------------------------------------- #

def workorder_state_kind(state: str) -> str:
    """Bucket a raw child-order state string."""
    x = (state or "").strip()
    if not x:
        return STATE_OTHER
    if _RE_CXL.match(x):
        return STATE_CANCELLED
    if _RE_REJ.search(x):
        return STATE_REJECTED
    if _RE_FILL.match(x):
        return STATE_FILLED
    return STATE_LIVE if x.lower() in ("new", "live", "open", "onmarket", "working", "ack") else STATE_OTHER


def state_reason(state: str) -> str:
    """`cxl:client request` -> `client request`; `rejected:band` -> `band`."""
    x = (state or "").strip()
    if ":" in x:
        return x.split(":", 1)[1].strip()
    return ""


def is_terminal_parent_state(state: str) -> bool:
    x = (state or "").strip().lower()
    return any(x.startswith(p) for p in _TERMINAL_PARENT)


def _truthy(v) -> bool:
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return False
    try:
        return int(v) != 0
    except (TypeError, ValueError):
        return bool(str(v).strip().upper() in ("Y", "YES", "TRUE", "1"))


def _num(v, default=np.nan) -> float:
    try:
        f = float(v)
        return default if np.isnan(f) else f
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------- #
# Child orders                                                                 #
# --------------------------------------------------------------------------- #

def enrich_workorders(wo: pd.DataFrame) -> pd.DataFrame:
    """Add venue / state / phase classification to the child-order frame."""
    if wo.empty:
        return wo.assign(
            is_close=pd.Series(dtype=bool),
            state_kind=pd.Series(dtype=object),
            cancel_reason=pd.Series(dtype=object),
            send_time=pd.Series(dtype="timedelta64[ns]"),
            send_bucket=pd.Series(dtype=object),
            send_phase=pd.Series(dtype=object),
            event_phase=pd.Series(dtype=object),
        )

    df = wo.copy()
    df["venue"] = df.get("venue", "").astype(str)
    df["venuetype"] = df.get("venuetype", "").astype(str)
    df["is_close"] = (
        df["venue"].str.upper().str.contains("CLOSE", na=False)
        | df["venuetype"].str.upper().str.contains("CLOSE", na=False)
    )

    df["state"] = df.get("state", "").astype(str)
    df["state_kind"] = df["state"].map(workorder_state_kind)
    df["cancel_reason"] = np.where(
        df["state_kind"] == STATE_CANCELLED,
        df["state"].map(state_reason),
        "",
    )
    df["reject_reason"] = np.where(
        df["state_kind"] == STATE_REJECTED,
        df["state"].map(state_reason),
        "",
    )

    # Best available "when did this leave us" stamp.
    send = None
    for col in ("t_oes_send", "t_transmit", "t_gen", "time"):
        if col in df.columns:
            cand = pd.to_timedelta(df[col], errors="coerce")
            send = cand if send is None else send.fillna(cand)
    df["send_time"] = send

    df["send_bucket"] = S.session_of(df["send_time"])
    df["send_phase"] = S.phase_of(df["send_time"])
    df["event_phase"] = S.phase_of(pd.to_timedelta(df["time"], errors="coerce"))

    # A close child order is "of the close" whatever the clock says; otherwise
    # fall back to when the terminal event happened.
    df["phase"] = np.where(df["is_close"], "CLOSE", df["event_phase"])

    df["is_market_order"] = df.get("otype", "").astype(str).str.match(_RE_MARKET_OTYPE).fillna(False)
    return df


def enrich_executions(ex: pd.DataFrame, wo: pd.DataFrame) -> pd.DataFrame:
    """Tag executions with the venue of their child order and a phase."""
    if ex.empty:
        return ex.assign(
            is_close=pd.Series(dtype=bool),
            phase=pd.Series(dtype=object),
            bucket=pd.Series(dtype=object),
            is_fill=pd.Series(dtype=bool),
        )

    df = ex.copy()
    df["bucket"] = S.session_of(pd.to_timedelta(df["time"], errors="coerce"))
    df["time_phase"] = S.phase_of(pd.to_timedelta(df["time"], errors="coerce"))

    venue_by_work = {}
    if not wo.empty and "id_work" in wo.columns:
        venue_by_work = dict(zip(wo["id_work"], wo["is_close"]))

    if "id_work" in df.columns and venue_by_work:
        mapped = df["id_work"].map(venue_by_work)
    else:
        mapped = pd.Series(np.nan, index=df.index)

    # venue wins when we know it; otherwise the auction print window decides.
    df["is_close"] = mapped.where(
        mapped.notna(), df["bucket"].isin(C.CAS_BUCKETS)
    ).astype(bool)
    df["phase"] = np.where(df["is_close"], "CLOSE", df["time_phase"])

    fillsize = pd.to_numeric(df.get("fillsize"), errors="coerce").fillna(0)
    df["fillsize"] = fillsize
    df["fillprice"] = pd.to_numeric(df.get("fillprice"), errors="coerce")
    df["is_fill"] = fillsize > 0
    df["ostat"] = df.get("ostat", "").astype(str)
    df["ostat_kind"] = df["ostat"].map(workorder_state_kind)
    return df


# --------------------------------------------------------------------------- #
# Parent-order state history                                                   #
# --------------------------------------------------------------------------- #

def summarise_states(states: pd.DataFrame) -> pd.DataFrame:
    """One row per parent order distilled from its whole state history."""
    if states.empty:
        return pd.DataFrame(columns=["id_target"])

    df = states.copy()
    df["time"] = pd.to_timedelta(df["time"], errors="coerce")
    df["state"] = df.get("state", "").astype(str)

    g = df.groupby("id_target", sort=False)
    last = g.tail(1).set_index("id_target")
    first = g.head(1).set_index("id_target")

    out = pd.DataFrame(index=last.index)
    out["n_states"] = g.size()
    out["first_state_time"] = first["time"]
    out["last_state_time"] = last["time"]
    out["final_state"] = last["state"]
    for col in ("open", "make", "leave", "commit", "avg_fill_price"):
        if col in last.columns:
            out[f"final_{col}"] = pd.to_numeric(last[col], errors="coerce")

    # Snapshot as continuous trading ends.
    pre = df[df["time"] <= td(C.CTS_END)]
    if not pre.empty:
        pre_last = pre.groupby("id_target", sort=False).tail(1).set_index("id_target")
        out["state_at_cas"] = pre_last["state"]
        for col in ("open", "make", "leave", "commit"):
            if col in pre_last.columns:
                out[f"{col}_at_cas"] = pd.to_numeric(pre_last[col], errors="coerce")
    for col in ("state_at_cas", "open_at_cas", "make_at_cas", "leave_at_cas", "commit_at_cas"):
        if col not in out.columns:
            out[col] = np.nan

    # Did the algo ever earmark quantity for the close?
    cas = df[df["time"] >= td(C.CAS_START)]
    for col in ("make_close", "leave_close", "commit_close"):
        if col in df.columns:
            out[f"max_{col}"] = pd.to_numeric(df[col], errors="coerce").groupby(df["id_target"]).max()
            src = cas if not cas.empty else df.iloc[0:0]
            out[f"max_{col}_in_cas"] = (
                pd.to_numeric(src[col], errors="coerce").groupby(src["id_target"]).max()
                if not src.empty else np.nan
            )
        else:
            out[f"max_{col}"] = np.nan
            out[f"max_{col}_in_cas"] = np.nan

    # When did the parent first reach a terminal state?
    term = df[df["state"].map(is_terminal_parent_state)]
    if not term.empty:
        t0 = term.groupby("id_target", sort=False).head(1).set_index("id_target")
        out["terminal_time"] = t0["time"]
        out["terminal_state"] = t0["state"]
    else:
        out["terminal_time"] = pd.NaT
        out["terminal_state"] = ""

    out["terminal_before_cas"] = out["terminal_time"].notna() & (
        out["terminal_time"] < td(C.CAS_START)
    )
    out["cancelled_before_cas"] = out["terminal_before_cas"] & out["terminal_state"].astype(
        str
    ).str.match(_RE_CXL).fillna(False)

    return out.reset_index()


# --------------------------------------------------------------------------- #
# Fills                                                                        #
# --------------------------------------------------------------------------- #

def summarise_fills(ex: pd.DataFrame) -> pd.DataFrame:
    """Executed quantity / vwap per parent, split continuous vs close."""
    if ex.empty:
        return pd.DataFrame(columns=["id_target"])

    f = ex[ex["is_fill"]].copy()
    if f.empty:
        return pd.DataFrame(columns=["id_target"])
    f["notional"] = f["fillprice"] * f["fillsize"]

    def agg(sub: pd.DataFrame, prefix: str) -> pd.DataFrame:
        if sub.empty:
            return pd.DataFrame()
        g = sub.groupby("id_target", sort=False)
        out = pd.DataFrame({
            f"{prefix}qty": g["fillsize"].sum(),
            f"{prefix}notional": g["notional"].sum(),
            f"{prefix}nfills": g.size(),
            f"{prefix}first_time": g["time"].min(),
            f"{prefix}last_time": g["time"].max(),
        })
        out[f"{prefix}vwap"] = out[f"{prefix}notional"] / out[f"{prefix}qty"]
        return out

    total = agg(f, "exec_")
    close = agg(f[f["is_close"]], "close_")
    cont = agg(f[~f["is_close"]], "cont_")

    out = total
    for extra in (close, cont):
        if not extra.empty:
            out = out.join(extra, how="left")
    for prefix in ("close_", "cont_"):
        for suffix in ("qty", "notional", "nfills"):
            col = f"{prefix}{suffix}"
            if col not in out.columns:
                out[col] = 0.0
            out[col] = out[col].fillna(0.0)
        if f"{prefix}vwap" not in out.columns:
            out[f"{prefix}vwap"] = np.nan

    # Arrival / benchmark prices carried on the execution reports.
    for col in ("target_strike", "target_vwap"):
        if col in f.columns:
            out[col] = pd.to_numeric(f[col], errors="coerce").groupby(f["id_target"]).first()

    return out.reset_index()


# --------------------------------------------------------------------------- #
# Close child-order footprint                                                  #
# --------------------------------------------------------------------------- #

def summarise_close_workorders(wo: pd.DataFrame) -> pd.DataFrame:
    """What we actually put into the auction, per parent."""
    if wo.empty:
        return pd.DataFrame(columns=["id_target"])

    cw = wo[wo["is_close"]].copy()
    if cw.empty:
        return pd.DataFrame(columns=["id_target"])

    cw["size"] = pd.to_numeric(cw.get("size"), errors="coerce").fillna(0)
    cw["price"] = pd.to_numeric(cw.get("price"), errors="coerce")
    g = cw.groupby("id_target", sort=False)

    out = pd.DataFrame({
        "n_close_wo": g.size(),
        "close_wo_qty": g["size"].sum(),
        "close_wo_max_qty": g["size"].max(),
        "close_wo_first_send": g["send_time"].min(),
        "close_wo_last_send": g["send_time"].max(),
        "close_wo_min_price": g["price"].min(),
        "close_wo_max_price": g["price"].max(),
    })

    flags = cw.assign(
        _rej=(cw["state_kind"] == STATE_REJECTED).astype(int),
        _cxl=(cw["state_kind"] == STATE_CANCELLED).astype(int),
        _mkt=cw["is_market_order"].astype(int),
    ).groupby("id_target", sort=False)[["_rej", "_cxl", "_mkt"]].sum()

    out["close_wo_rejected"] = flags["_rej"] > 0
    out["close_wo_cancelled"] = flags["_cxl"] > 0
    out["close_wo_market_orders"] = flags["_mkt"]

    out["close_wo_reject_reasons"] = (
        cw.loc[cw["state_kind"] == STATE_REJECTED]
        .groupby("id_target")["state"]
        .apply(lambda s: "; ".join(sorted(set(x for x in s if x))))
    )
    out["close_wo_cancel_reasons"] = (
        cw.loc[cw["state_kind"] == STATE_CANCELLED]
        .groupby("id_target")["state"]
        .apply(lambda s: "; ".join(sorted(set(x for x in s if x))))
    )
    out["close_wo_venues"] = g["venue"].apply(lambda s: "; ".join(sorted(set(str(x) for x in s if str(x)))))
    out["close_wo_send_buckets"] = g["send_bucket"].apply(
        lambda s: "; ".join(sorted(set(str(x) for x in s if str(x))))
    )

    for col in ("close_wo_reject_reasons", "close_wo_cancel_reasons"):
        out[col] = out[col].fillna("")
    for col in ("close_wo_rejected", "close_wo_cancelled"):
        out[col] = out[col].fillna(False).astype(bool)

    return out.reset_index()


def summarise_alerts(alerts: pd.DataFrame) -> pd.DataFrame:
    """Alert counts per parent, with the CAS-window ones split out."""
    if alerts is None or alerts.empty:
        return pd.DataFrame(columns=["id_target"])

    a = alerts.copy()
    a["time"] = pd.to_timedelta(a["time"], errors="coerce")
    a["bucket"] = S.session_of(a["time"])
    a["in_cas"] = a["bucket"].isin(C.CAS_BUCKETS)

    g = a.groupby("id_target", sort=False)
    out = pd.DataFrame({
        "n_alerts": g.size(),
        "n_alerts_cas": g["in_cas"].sum(),
        "alert_types": g["alerttype"].apply(
            lambda s: "; ".join(sorted(set(str(x) for x in s if str(x))))
        ),
    })
    cas = a[a["in_cas"]]
    if not cas.empty:
        out["alert_types_cas"] = cas.groupby("id_target")["alerttype"].apply(
            lambda s: "; ".join(sorted(set(str(x) for x in s if str(x))))
        )
        out["alert_text_cas"] = cas.groupby("id_target")["alertstr"].apply(
            lambda s: " | ".join(str(x)[:200] for x in s if str(x))[:1000]
        )
    else:
        out["alert_types_cas"] = ""
        out["alert_text_cas"] = ""
    out = out.fillna({"alert_types_cas": "", "alert_text_cas": "", "n_alerts_cas": 0})
    return out.reset_index()


# --------------------------------------------------------------------------- #
# The waterfall                                                                #
# --------------------------------------------------------------------------- #

def _residual_is_material(residual: float, size: float) -> bool:
    if np.isnan(residual):
        return False
    if residual <= C.RESIDUAL_ABS_TOL:
        return False
    if size and size > 0 and (residual / size) <= C.RESIDUAL_PCT_TOL:
        return False
    return True


def diagnose_row(r: pd.Series) -> pd.Series:
    """Return (participation, reason_code, reason_label, reason_detail)."""
    notes: list[str] = []

    size = _num(r.get("size"), 0.0)
    exec_qty = _num(r.get("exec_qty"), 0.0) or 0.0
    close_qty = _num(r.get("close_qty"), 0.0) or 0.0
    residual = size - exec_qty
    open_at_cas = _num(r.get("open_at_cas"))
    n_close_wo = _num(r.get("n_close_wo"), 0.0) or 0.0

    # ---- 1. did we trade in the auction? --------------------------------- #
    if close_qty > 0:
        part = "FILLED_IN_CLOSE"
        if _residual_is_material(residual, size):
            notes.append(f"partial: {residual:,.0f} left over after the close")
        return pd.Series({
            "participation": part,
            "reason_code": "",
            "reason_label": "",
            "reason_detail": "; ".join(notes),
        })

    # ---- 2. we put something in but it did not trade --------------------- #
    if n_close_wo > 0:
        part = "SENT_NOT_FILLED"
        first_send = r.get("close_wo_first_send")
        late = pd.notna(first_send) and pd.Timedelta(first_send) >= td(C.ENTRY_LO_END)
        mkt_in_lo = (
            _num(r.get("close_wo_market_orders"), 0.0) > 0
            and pd.notna(first_send)
            and td(C.ENTRY_LO_START) <= pd.Timedelta(first_send) < td(C.ENTRY_LO_END)
        )
        outside = bool(r.get("close_price_outside_band", False))

        if late:
            notes.append(f"first close child order left at {td_to_str(first_send)} HKT")
        if mkt_in_lo:
            notes.append("market child order during the limit-only phase")
        if outside:
            notes.append(
                f"child price {r.get('close_wo_min_price')}-{r.get('close_wo_max_price')} "
                f"vs band {r.get('band_lo')}-{r.get('band_hi')}"
            )

        if bool(r.get("close_wo_rejected", False)):
            code = "CLOSE_ORDER_REJECTED"
            notes.insert(0, str(r.get("close_wo_reject_reasons", "")))
        elif bool(r.get("close_wo_cancelled", False)):
            code = "CLOSE_ORDER_CANCELLED"
            notes.insert(0, str(r.get("close_wo_cancel_reasons", "")))
        elif late:
            code = "SENT_AFTER_ENTRY_CLOSED"
        elif mkt_in_lo:
            code = "MARKET_ORDER_IN_LIMIT_ONLY_PHASE"
        elif outside:
            code = "PRICE_OUTSIDE_PRICE_BAND"
        else:
            code = "NOT_MATCHED_IN_AUCTION"
        return pd.Series({
            "participation": part,
            "reason_code": code,
            "reason_label": SENT_REASONS[code],
            "reason_detail": "; ".join(n for n in notes if n),
        })

    # ---- 3. nothing was ever sent to the auction ------------------------- #
    part = "NOT_SENT"
    code = None

    if not bool(r.get("cas_eligible", True)):
        code = "NOT_CAS_ELIGIBLE"

    if code is None and not _truthy(r.get("doclose")):
        code = "NO_CLOSE_INSTRUCTION"

    if code is None:
        residual_at_cas = open_at_cas if not np.isnan(open_at_cas) else residual
        if not _residual_is_material(residual_at_cas, size):
            code = "FULLY_FILLED_BEFORE_CAS"
            notes.append(f"open at 17:45 HKT = {residual_at_cas:,.0f}")

    if code is None and bool(r.get("cancelled_before_cas", False)):
        code = "PARENT_CANCELLED_BEFORE_CAS"
        notes.append(f"{r.get('terminal_state','')} at {td_to_str(r.get('terminal_time'))} HKT")

    if code is None and bool(r.get("terminal_before_cas", False)):
        code = "PARENT_DONE_BEFORE_CAS"
        notes.append(f"{r.get('terminal_state','')} at {td_to_str(r.get('terminal_time'))} HKT")

    if code is None:
        t_end = r.get("t_end")
        if pd.notna(t_end) and pd.Timedelta(t_end) <= td(C.CTS_END):
            code = "ORDER_END_BEFORE_CAS"
            notes.append(
                f"t_end={td_to_str(t_end)} HKT "
                f"(a CAS name that participates carries "
                f"{C.EXPECTED_TEND_CAS_PARTICIPATING.strftime('%H:%M')})"
            )

    if code is None:
        arrival = r.get("first_state_time")
        if pd.isna(arrival):
            arrival = r.get("t_start")
        if pd.notna(arrival) and pd.Timedelta(arrival) >= td(C.ENTRY_LO_END):
            code = "ORDER_ARRIVED_AFTER_ENTRY_CLOSED"
            notes.append(f"arrived {td_to_str(arrival)} HKT, entry shut at 18:00 HKT")

    if code is None and bool(r.get("parent_limit_blocks_close", False)):
        code = "LIMIT_OUTSIDE_PRICE_BAND"
        notes.append(
            f"limit {r.get('limit_price')} vs band "
            f"{r.get('band_lo')}-{r.get('band_hi')} (ref {r.get('ref_price')})"
        )

    if code is None and bool(r.get("no_market_data", False)):
        code = "NO_MARKET_DATA"

    if code is None:
        made = _num(r.get("max_make_close"), 0.0) or 0.0
        committed = _num(r.get("max_commit_close"), 0.0) or 0.0
        if made <= 0 and committed <= 0:
            code = "ALGO_NEVER_COMMITTED_TO_CLOSE"
            notes.append(f"residual {residual:,.0f} but make_close/commit_close stayed at 0")

    if code is None and _num(r.get("n_alerts_cas"), 0.0) > 0:
        code = "BLOCKING_ALERT"
        notes.append(str(r.get("alert_types_cas", "")))

    if code is None:
        code = "UNEXPLAINED"

    return pd.Series({
        "participation": part,
        "reason_code": code,
        "reason_label": NOT_SENT_REASONS[code],
        "reason_detail": "; ".join(n for n in notes if n),
    })


def diagnose(orders: pd.DataFrame) -> pd.DataFrame:
    if orders.empty:
        for col in ("participation", "reason_code", "reason_label", "reason_detail"):
            orders[col] = pd.Series(dtype=object)
        return orders
    diag = orders.apply(diagnose_row, axis=1)
    return pd.concat([orders.drop(columns=diag.columns, errors="ignore"), diag], axis=1)


# --------------------------------------------------------------------------- #
# Rejections & cancellations                                                   #
# --------------------------------------------------------------------------- #

def rejections(wo: pd.DataFrame, ex: pd.DataFrame, orders: pd.DataFrame) -> pd.DataFrame:
    """Every rejected child order, tagged CONTINUOUS vs CLOSE.

    Two sources are merged:
      * `workorder.state`  -- the algo's own view of the child order
      * `execution.ostat`  -- the exchange/OMS status message, which usually
                              carries the free-text reason in `comment`
    """
    frames = []

    if not wo.empty:
        rej = wo[wo["state_kind"] == STATE_REJECTED].copy()
        if not rej.empty:
            frames.append(pd.DataFrame({
                "source": "workorder",
                "id_target": rej["id_target"],
                "id_work": rej.get("id_work"),
                "sym": rej.get("sym"),
                "side": rej.get("side"),
                "time": pd.to_timedelta(rej["time"], errors="coerce"),
                "qty": pd.to_numeric(rej.get("size"), errors="coerce"),
                "price": pd.to_numeric(rej.get("price"), errors="coerce"),
                "venue": rej.get("venue"),
                "otype": rej.get("otype"),
                "raw_state": rej["state"],
                "reason": rej["state"].map(state_reason),
                "is_close": rej["is_close"],
                "bucket": S.session_of(pd.to_timedelta(rej["time"], errors="coerce")),
                "phase": rej["phase"],
            }))

    if not ex.empty and "ostat_kind" in ex.columns:
        rex = ex[ex["ostat_kind"] == STATE_REJECTED].copy()
        if not rex.empty:
            frames.append(pd.DataFrame({
                "source": "execution",
                "id_target": rex["id_target"],
                "id_work": rex.get("id_work"),
                "sym": rex.get("sym"),
                "side": rex.get("side"),
                "time": pd.to_timedelta(rex["time"], errors="coerce"),
                "qty": pd.to_numeric(rex.get("size"), errors="coerce"),
                "price": pd.to_numeric(rex.get("price"), errors="coerce"),
                "venue": rex.get("last_mkt", ""),
                "otype": rex.get("otype"),
                "raw_state": rex["ostat"],
                "reason": rex.get("comment", "").astype(str).str.slice(0, 300),
                "is_close": rex["is_close"],
                "bucket": rex["bucket"],
                "phase": rex["phase"],
            }))

    if not frames:
        return pd.DataFrame(columns=[
            "source", "id_target", "id_work", "sym", "side", "time", "qty",
            "price", "venue", "otype", "raw_state", "reason", "is_close",
            "bucket", "phase", "flow", "basket", "trader", "algo",
        ])

    out = pd.concat(frames, ignore_index=True)
    out["phase"] = np.where(out["is_close"], "CLOSE", out["phase"])

    # A refused child order usually lands twice: once as a `workorder.state` and
    # once as an execution report carrying the exchange's free text.  Counting
    # both would double the rejection numbers, so keep one row per child order
    # and pull the exchange text across onto it.
    if "id_work" in out.columns:
        from_exec = out[out["source"] == "execution"].dropna(subset=["id_work"])
        text = (
            from_exec.groupby("id_work")["reason"].first()
            if not from_exec.empty else pd.Series(dtype=object)
        )
        out["exchange_text"] = out["id_work"].map(text).fillna("") if len(text) else ""
        dup = out["id_work"].notna() & out.duplicated(subset=["id_work"], keep=False)
        out = out[~(dup & (out["source"] == "execution"))].reset_index(drop=True)

    return _attach_order_context(out, orders).sort_values(
        ["phase", "sym", "time"], ignore_index=True
    )


def cancellations(wo: pd.DataFrame, orders: pd.DataFrame) -> pd.DataFrame:
    """Every cancelled child order with its `cxl:<reason>` decoded."""
    if wo.empty:
        return pd.DataFrame(columns=[
            "id_target", "id_work", "sym", "side", "time", "qty", "venue",
            "raw_state", "reason", "is_close", "phase",
        ])
    cxl = wo[wo["state_kind"] == STATE_CANCELLED].copy()
    if cxl.empty:
        return pd.DataFrame(columns=[
            "id_target", "id_work", "sym", "side", "time", "qty", "venue",
            "raw_state", "reason", "is_close", "phase",
        ])

    out = pd.DataFrame({
        "id_target": cxl["id_target"],
        "id_work": cxl.get("id_work"),
        "sym": cxl.get("sym"),
        "side": cxl.get("side"),
        "time": pd.to_timedelta(cxl["time"], errors="coerce"),
        "qty": pd.to_numeric(cxl.get("size"), errors="coerce"),
        "venue": cxl.get("venue"),
        "raw_state": cxl["state"],
        "reason": cxl["cancel_reason"].replace("", "(no reason given)"),
        "is_close": cxl["is_close"],
        "bucket": S.session_of(pd.to_timedelta(cxl["time"], errors="coerce")),
        "phase": cxl["phase"],
    })
    return _attach_order_context(out, orders).sort_values(
        ["phase", "sym", "time"], ignore_index=True
    )


def _attach_order_context(df: pd.DataFrame, orders: pd.DataFrame) -> pd.DataFrame:
    keep = [c for c in ("id_target", "flow", "basket", "trader", "algo", "portfolio")
            if c in orders.columns]
    if not keep or "id_target" not in keep or df.empty:
        return df
    return df.merge(orders[keep], on="id_target", how="left")

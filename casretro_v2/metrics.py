"""The three sections, as three frames.  Pure pandas, no connection.

One rule runs through all of them: **a ratio is computed from summed
quantities, never averaged from daily ratios.**  A week is not the mean of five
days unless all five days were the same size, and they never are.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from casretro import config as C

from . import config as V


def _pct(num, den) -> pd.Series:
    num = pd.to_numeric(num, errors="coerce")
    den = pd.to_numeric(den, errors="coerce")
    return num / den.replace(0, np.nan) * 100.0


def _agg_children(children: pd.DataFrame, keys: list[str]) -> pd.DataFrame:
    """The measures every section shares, over whatever keys it groups on."""
    g = children.groupby(keys, dropna=False, sort=False)
    out = pd.DataFrame({
        "n_orders": g["id_target"].nunique(),
        "n_children": g["id_work"].nunique(),
        "n_syms": g["sym"].nunique(),
        "sent_qty": g["sent_qty"].sum(),
        "exec_qty": g["exec_qty"].sum(),
        "unfilled_qty": g["unfilled_qty"].sum(),
        "exec_notional_usd": g["exec_notional_usd"].sum(min_count=1),
        "unfilled_notional_usd": g["unfilled_notional_usd"].sum(min_count=1),
        "sent_notional_usd": g["sent_notional_usd"].sum(min_count=1),
    }).reset_index()
    out["fill_rate_pct"] = _pct(out["exec_qty"], out["sent_qty"])
    return out


# --------------------------------------------------------------------------- #
# 1. Execution quality                                                         #
# --------------------------------------------------------------------------- #

def execution_quality(children: pd.DataFrame) -> pd.DataFrame:
    """Per day per order type: what we sent, what traded, and what it was worth.

    The bar chart reads off `sent_qty` split into `exec_qty` and `unfilled_qty`;
    `fill_rate_pct` is the ratio the section exists to show, and the two
    notionals are the money the ratio is moving.
    """
    if children is None or children.empty:
        return pd.DataFrame(columns=["date", "otype_kind"])
    out = _agg_children(children, ["date", "otype_kind"])

    # How much of the notional rests on a price we substituted rather than
    # observed -- a market order's unfilled quantity has no price of its own.
    sub = children[children["unfilled_px_source"] == "auction close"]
    if not sub.empty:
        s = (sub.groupby(["date", "otype_kind"])["unfilled_notional_usd"]
             .sum(min_count=1).rename("substituted_notional_usd").reset_index())
        out = out.merge(s, on=["date", "otype_kind"], how="left")
    if "substituted_notional_usd" not in out.columns:
        out["substituted_notional_usd"] = np.nan
    out["substituted_pct"] = _pct(out["substituted_notional_usd"], out["sent_notional_usd"])

    return out.sort_values(["otype_kind", "date"], ignore_index=True)


def execution_quality_totals(eq: pd.DataFrame) -> pd.DataFrame:
    """One row per order type over the whole period, for the table footers."""
    if eq is None or eq.empty:
        return pd.DataFrame()
    g = eq.groupby("otype_kind", sort=False)
    out = pd.DataFrame({
        "n_orders": g["n_orders"].sum(),
        "n_children": g["n_children"].sum(),
        "sent_qty": g["sent_qty"].sum(),
        "exec_qty": g["exec_qty"].sum(),
        "unfilled_qty": g["unfilled_qty"].sum(),
        "exec_notional_usd": g["exec_notional_usd"].sum(min_count=1),
        "sent_notional_usd": g["sent_notional_usd"].sum(min_count=1),
    }).reset_index()
    out["fill_rate_pct"] = _pct(out["exec_qty"], out["sent_qty"])
    return out


# --------------------------------------------------------------------------- #
# 2. Flows                                                                     #
# --------------------------------------------------------------------------- #

def _market_for_groups(
    children: pd.DataFrame, market: pd.DataFrame, keys: list[str]
) -> pd.DataFrame:
    """Close volume and close notional over the symbols each group traded.

    The denominator is built from the group's own distinct (date, sym) pairs, so
    a name we touched twice in a day is counted once.  It follows that two rows'
    denominators overlap wherever they share a name -- the market notionals down
    a column are **not** additive, and the page says so rather than inviting the
    sum.
    """
    empty = pd.DataFrame(columns=keys + ["mkt_close_qty", "mkt_close_notional_usd"])
    if children is None or children.empty or market is None or market.empty:
        return empty

    # The market frame is per (date, sym), so the join key carries the date
    # whenever both sides have one -- that is what makes a symbol traded twice
    # in a day count once *for that day* while still counting again tomorrow.
    on = ["date", "sym"] if ("date" in market.columns and "date" in children.columns) else ["sym"]
    cols = list(dict.fromkeys(keys + on))
    pairs = children[cols].drop_duplicates()
    pairs = pairs.merge(market[on + [c for c in ("mkt_close_qty",
                                                 "mkt_close_notional_usd")
                                     if c in market.columns]],
                        on=on, how="left")

    cols = {}
    if "mkt_close_qty" in pairs.columns:
        cols["mkt_close_qty"] = ("mkt_close_qty", "sum")
    if "mkt_close_notional_usd" in pairs.columns:
        cols["mkt_close_notional_usd"] = ("mkt_close_notional_usd", "sum")
    if not cols:
        return empty
    return pairs.groupby(keys, dropna=False, sort=False).agg(**cols).reset_index()


def flows(children: pd.DataFrame, market: pd.DataFrame) -> pd.DataFrame:
    """Day x flow x order type, against the market in the same names."""
    if children is None or children.empty:
        return pd.DataFrame()

    keys = ["date", "flow", "otype_kind"]
    out = _agg_children(children, keys)
    denom = _market_for_groups(children, market, keys)
    if not denom.empty:
        out = out.merge(denom, on=keys, how="left")
    for col in ("mkt_close_qty", "mkt_close_notional_usd"):
        if col not in out.columns:
            out[col] = np.nan

    out["our_pct_of_market_notional"] = _pct(
        out["exec_notional_usd"], out["mkt_close_notional_usd"]
    )
    return out.sort_values(["date", "flow", "otype_kind"], ignore_index=True)


def flows_total(children: pd.DataFrame, market: pd.DataFrame) -> pd.Series | None:
    """The whole period as one row, with a denominator built the same way.

    Recomputed from the child orders rather than summed down the table: the
    market notionals overlap wherever two rows share a name, so adding the
    column would count those names more than once.
    """
    if children is None or children.empty:
        return None
    tmp = children.copy()
    tmp["_all"] = "TOTAL"
    out = _agg_children(tmp, ["_all"])
    denom = _market_for_groups(tmp, market, ["_all"])
    if not denom.empty:
        out = out.merge(denom, on="_all", how="left")
    for col in ("mkt_close_qty", "mkt_close_notional_usd"):
        if col not in out.columns:
            out[col] = np.nan
    out["our_pct_of_market_notional"] = _pct(
        out["exec_notional_usd"], out["mkt_close_notional_usd"]
    )
    return out.iloc[0]


# --------------------------------------------------------------------------- #
# 3. Clients                                                                   #
# --------------------------------------------------------------------------- #

def top_clients(
    children: pd.DataFrame,
    market: pd.DataFrame,
    flow: str,
    n: int = V.TOP_CLIENTS,
) -> pd.DataFrame:
    """The `n` biggest clients of one flow, by notional traded in the close."""
    if children is None or children.empty:
        return pd.DataFrame()
    sub = children[children["flow"] == flow]
    if sub.empty:
        return pd.DataFrame()

    key = V.CLIENT_COLUMN
    out = _agg_children(sub, [key])
    denom = _market_for_groups(sub, market, [key])
    if not denom.empty:
        out = out.merge(denom, on=key, how="left")
    if "mkt_close_notional_usd" not in out.columns:
        out["mkt_close_notional_usd"] = np.nan
    out["our_pct_of_market_notional"] = _pct(
        out["exec_notional_usd"], out["mkt_close_notional_usd"]
    )
    out["n_days"] = (
        sub.groupby(key)["date"].nunique().reindex(out[key]).to_numpy()
    )
    out = out.sort_values("exec_notional_usd", ascending=False, na_position="last")
    return out.head(n).reset_index(drop=True)


def flows_present(children: pd.DataFrame) -> list[str]:
    """Which flows actually have child orders, in a fixed display order."""
    if children is None or children.empty:
        return []
    seen = set(children["flow"].dropna().astype(str))
    return [f for f in (C.FLOW_SILK, C.FLOW_AGENCY) if f in seen] + sorted(
        seen - {C.FLOW_SILK, C.FLOW_AGENCY}
    )

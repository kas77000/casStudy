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
    sub = (children[children["unfilled_px_source"] == "auction close"]
           if "unfilled_px_source" in children.columns else pd.DataFrame())
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

#: What `_market_for_groups` returns beyond the grouping keys.
_MARKET_COLS = ["mkt_close_qty", "mkt_close_notional_usd",
                "covered_exec_notional_usd", "market_coverage_pct"]


def _market_for_groups(
    children: pd.DataFrame, market: pd.DataFrame, keys: list[str]
) -> pd.DataFrame:
    """The auction, and our part of it, over the symbols each group traded.

    The denominator is built from the group's own distinct (date, sym) pairs, so
    a name we touched twice in a day is counted once.  It follows that two rows'
    denominators overlap wherever they share a name -- the market notionals down
    a column are **not** additive, and the page says so rather than inviting the
    sum.

    `covered_exec_notional_usd` is the matching numerator: our executed notional
    **on those same pairs**.  It exists because the two sides can otherwise
    disagree about which names they cover -- a symbol that never printed between
    17:58 and 18:00 carries no closing price and so contributes nothing to the
    market notional, while its own executed notional would still sit in the
    numerator and push the share above 100%.  `market_coverage_pct` says how much
    of the group's notional survived that test, so a thin denominator is visible
    rather than flattering.
    """
    empty = pd.DataFrame(columns=keys + _MARKET_COLS)
    if children is None or children.empty or market is None or market.empty:
        return empty
    have = [c for c in ("mkt_close_qty", "mkt_close_notional_usd") if c in market.columns]
    if "mkt_close_notional_usd" not in have:
        return empty

    # The market frame is per (date, sym), so the join key carries the date
    # whenever both sides have one -- that is what makes a symbol traded twice
    # in a day count once *for that day* while still counting again tomorrow.
    on = ["date", "sym"] if ("date" in market.columns and "date" in children.columns) else ["sym"]
    cols = list(dict.fromkeys(keys + on + ["exec_notional_usd"]))
    j = children[[c for c in cols if c in children.columns]].merge(
        market[on + have].drop_duplicates(on), on=on, how="left"
    )
    covered = j["mkt_close_notional_usd"].notna()

    # Denominator: each covered (date, sym) counted once per group.
    pair_keys = list(dict.fromkeys(keys + on))
    den = (j[covered][pair_keys + have].drop_duplicates(pair_keys)
           .groupby(keys, dropna=False, sort=False)[have].sum().reset_index())

    # Numerator: our executed notional on exactly those pairs.
    num = (j[covered].groupby(keys, dropna=False, sort=False)["exec_notional_usd"]
           .sum(min_count=1).rename("covered_exec_notional_usd").reset_index())
    allof = (j.groupby(keys, dropna=False, sort=False)["exec_notional_usd"]
             .sum(min_count=1).rename("_all_exec").reset_index())

    out = den.merge(num, on=keys, how="outer").merge(allof, on=keys, how="outer")
    out["market_coverage_pct"] = _pct(out["covered_exec_notional_usd"], out["_all_exec"])
    return out.drop(columns=["_all_exec"])


def flows(children: pd.DataFrame, market: pd.DataFrame) -> pd.DataFrame:
    """Day x flow x order type, against the market in the same names."""
    if children is None or children.empty:
        return pd.DataFrame()

    keys = ["date", "flow", "otype_kind"]
    out = _agg_children(children, keys)
    denom = _market_for_groups(children, market, keys)
    if not denom.empty:
        out = out.merge(denom, on=keys, how="left")
    for col in _MARKET_COLS:
        if col not in out.columns:
            out[col] = np.nan

    # Our notional over the market's, both restricted to the same names.
    out["our_pct_of_market_notional"] = _pct(
        out["covered_exec_notional_usd"], out["mkt_close_notional_usd"]
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
    for col in _MARKET_COLS:
        if col not in out.columns:
            out[col] = np.nan
    out["our_pct_of_market_notional"] = _pct(
        out["covered_exec_notional_usd"], out["mkt_close_notional_usd"]
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
    for col in _MARKET_COLS:
        if col not in out.columns:
            out[col] = np.nan
    out["our_pct_of_market_notional"] = _pct(
        out["covered_exec_notional_usd"], out["mkt_close_notional_usd"]
    )
    out["n_days"] = (
        sub.groupby(key)["date"].nunique().reindex(out[key]).to_numpy()
    )
    out = out.sort_values("exec_notional_usd", ascending=False, na_position="last")
    return out.head(n).reset_index(drop=True)


# --------------------------------------------------------------------------- #
# 0. The headline                                                              #
# --------------------------------------------------------------------------- #

def headline(children: pd.DataFrame, market: pd.DataFrame) -> dict:
    """Period-wide KPIs for the tile row, all anchored on notional executed.

    Every ratio is recomputed from the summed quantities of the whole period --
    the row is what a manager reads first, so it cannot be a mean of daily
    percentages that no day actually printed.

    Notional executed is the anchor: it is what we put through the auction, and
    everything else on the row either explains it (what we sent, what filled) or
    sizes it (against the auction, against ourselves).
    """
    out: dict = {}
    if children is None or children.empty:
        return out

    num = lambda c: pd.to_numeric(children.get(c), errors="coerce")  # noqa: E731

    exec_usd = float(num("exec_notional_usd").sum(skipna=True))
    sent_usd = float(num("sent_notional_usd").sum(skipna=True))
    unfilled_usd = float(num("unfilled_notional_usd").sum(skipna=True))
    exec_qty = float(num("exec_qty").sum())
    sent_qty = float(num("sent_qty").sum())

    out["exec_notional_usd"] = exec_usd
    out["sent_notional_usd"] = sent_usd
    out["unfilled_notional_usd"] = unfilled_usd
    out["exec_qty"] = exec_qty
    out["sent_qty"] = sent_qty
    out["fill_rate_notional_pct"] = exec_usd / sent_usd * 100.0 if sent_usd else float("nan")
    out["fill_rate_qty_pct"] = exec_qty / sent_qty * 100.0 if sent_qty else float("nan")

    # Market vs limit, because one number over both hides which is which: a book
    # that fills 100% of its market orders and 2% of its limits does not have a
    # single fill rate worth quoting.
    out["by_otype"] = {}
    for otype, sub in children.groupby("otype_kind", sort=False):
        e = float(pd.to_numeric(sub["exec_notional_usd"], errors="coerce").sum(skipna=True))
        eq = float(pd.to_numeric(sub["exec_qty"], errors="coerce").sum())
        sq = float(pd.to_numeric(sub["sent_qty"], errors="coerce").sum())
        out["by_otype"][str(otype)] = {
            "exec_notional_usd": e,
            "fill_rate_qty_pct": eq / sq * 100.0 if sq else float("nan"),
        }

    out["n_syms"] = int(children["sym"].nunique())
    out["n_clients"] = int(children[V.CLIENT_COLUMN].nunique()) if V.CLIENT_COLUMN in children else 0
    out["n_days"] = int(children["date"].nunique()) if "date" in children else 0
    out["n_orders"] = int(children["id_target"].nunique())
    out["n_children"] = int(children["id_work"].nunique())

    # Our size against the auction itself, in the names we actually traded.
    # `flows_total` holds numerator and denominator to the same names, so a
    # symbol with no closing price cannot push the share above 100%.
    total = flows_total(children, market)
    if total is not None:
        out["mkt_close_notional_usd"] = float(total.get("mkt_close_notional_usd", np.nan))
        out["share_of_auction_pct"] = float(total.get("our_pct_of_market_notional", np.nan))
        out["auction_coverage_pct"] = float(total.get("market_coverage_pct", np.nan))

    # Concentration: one client carrying the week is a different business from
    # thirty sharing it, and the tile row is where that should be visible.
    if V.CLIENT_COLUMN in children.columns and exec_usd:
        by_client = (children.groupby(V.CLIENT_COLUMN)["exec_notional_usd"]
                     .sum(min_count=1).sort_values(ascending=False))
        if not by_client.empty and pd.notna(by_client.iloc[0]):
            out["top_client"] = str(by_client.index[0])
            out["top_client_pct"] = float(by_client.iloc[0] / exec_usd * 100.0)

    # The biggest day, so a week carried by one session says so.
    if "date" in children.columns:
        by_day = (children.groupby("date")["exec_notional_usd"]
                  .sum(min_count=1).sort_values(ascending=False))
        if not by_day.empty and pd.notna(by_day.iloc[0]):
            out["best_day"] = by_day.index[0]
            out["best_day_usd"] = float(by_day.iloc[0])
            out["best_day_pct"] = (
                float(by_day.iloc[0] / exec_usd * 100.0) if exec_usd else float("nan")
            )
    return out


def flows_present(children: pd.DataFrame) -> list[str]:
    """Which flows actually have child orders, in a fixed display order."""
    if children is None or children.empty:
        return []
    seen = set(children["flow"].dropna().astype(str))
    return [f for f in (C.FLOW_SILK, C.FLOW_AGENCY) if f in seen] + sorted(
        seen - {C.FLOW_SILK, C.FLOW_AGENCY}
    )

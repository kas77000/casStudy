"""Local-currency notionals into USD.

`equity.fx_last` is a rate whose direction the schema does not document, which is
why the v1 report left USD notionals out entirely.  It is recoverable rather than
unknowable: for a currency far from parity the two candidate quotes are
reciprocals sitting on opposite sides of 1, so the magnitude names the direction
and both readings then produce the same USD number.  INR is either ~85 or
~0.0117; nothing else it could plausibly be.

Near parity that argument fails, and the module says so instead of guessing --
`--fx divide|multiply` settles it, and `describe()` puts whichever was used on
the page.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from . import config as V

#: A rate this close to 1 cannot have its direction read off its magnitude.
NEAR_PARITY = (0.5, 2.0)


def factor_from_rate(rate: float, convention: str = V.FX_AUTO) -> float:
    """Multiplier taking one unit of local currency to USD."""
    if rate is None or not np.isfinite(rate) or rate <= 0:
        return float("nan")
    if convention == V.FX_DIVIDE:
        return 1.0 / rate
    if convention == V.FX_MULTIPLY:
        return float(rate)
    return 1.0 / rate if rate > 1.0 else float(rate)


def is_ambiguous(rate: float) -> bool:
    return bool(rate is not None and np.isfinite(rate)
                and NEAR_PARITY[0] <= rate <= NEAR_PARITY[1])


def usd_factors(
    universe: pd.DataFrame, convention: str = V.FX_AUTO
) -> tuple[pd.DataFrame, list[str]]:
    """Per sym: the multiplier from local notional to USD, plus any warnings.

    A sym already quoted in USD gets 1.0 whatever `fx_last` says.  A sym with no
    usable rate gets NaN and every USD figure touching it stays NaN -- an
    unconvertible name drops out of a total rather than joining it at its local
    face value, which would silently overstate by ~85x.
    """
    warnings: list[str] = []
    if universe is None or universe.empty or "sym" not in universe.columns:
        return pd.DataFrame(columns=["sym", "usd_factor"]), ["no universe: USD notionals unavailable"]

    out = pd.DataFrame({"sym": universe["sym"].astype(str)})
    crncy = (universe["CRNCY"].astype(str).str.upper()
             if "CRNCY" in universe.columns else pd.Series("", index=universe.index))
    rate = (pd.to_numeric(universe["fx_last"], errors="coerce")
            if "fx_last" in universe.columns else pd.Series(np.nan, index=universe.index))

    out["ccy"] = crncy.to_numpy()
    out["fx_last"] = rate.to_numpy()
    out["usd_factor"] = [
        1.0 if c == V.REPORT_CCY else factor_from_rate(r, convention)
        for c, r in zip(out["ccy"], out["fx_last"])
    ]

    if "fx_last" not in universe.columns:
        warnings.append(
            "the universe carries no fx_last column -- every USD notional on "
            "this report is unavailable. Re-export the snapshot, or run with "
            "--no-universe-file to read the equity table."
        )
    else:
        missing = int(out["usd_factor"].isna().sum())
        if missing:
            warnings.append(
                f"{missing} of {len(out)} symbols have no usable fx_last: their "
                f"notionals are left out of the USD totals rather than added at "
                f"their local face value"
            )
        live = out[(out["ccy"] != V.REPORT_CCY) & out["fx_last"].notna()]
        if not live.empty and convention == V.FX_AUTO:
            ambiguous = sorted({c for c, r in zip(live["ccy"], live["fx_last"])
                                if is_ambiguous(r)})
            if ambiguous:
                warnings.append(
                    f"fx_last for {', '.join(ambiguous)} sits near parity, where "
                    f"the direction of the quote cannot be read off its "
                    f"magnitude -- pass --fx divide or --fx multiply to settle it"
                )
    return out[["sym", "ccy", "fx_last", "usd_factor"]], warnings


def describe(factors: pd.DataFrame, convention: str = V.FX_AUTO) -> str:
    """One line for the page, naming the rate actually applied."""
    if factors is None or factors.empty:
        return "USD conversion unavailable."
    live = factors[(factors["ccy"] != V.REPORT_CCY) & factors["fx_last"].notna()]
    if live.empty:
        return f"Notionals in {V.REPORT_CCY}."
    ccy = live["ccy"].mode().iloc[0] if not live["ccy"].mode().empty else "local"
    rate = float(live.loc[live["ccy"] == ccy, "fx_last"].median())
    how = ("read from its magnitude" if convention == V.FX_AUTO
           else f"forced with --fx {convention}")
    return (f"Converted at fx_last = {rate:,.4f} {ccy} per {V.REPORT_CCY} "
            f"(direction {how}).")


def attach(df: pd.DataFrame, factors: pd.DataFrame, cols: dict[str, str]) -> pd.DataFrame:
    """Add USD twins of `cols` ({local_col: usd_col}) by sym."""
    if df is None or df.empty:
        return df
    out = df.merge(factors[["sym", "usd_factor"]], on="sym", how="left")
    for local, usd in cols.items():
        out[usd] = (pd.to_numeric(out.get(local), errors="coerce")
                    * pd.to_numeric(out["usd_factor"], errors="coerce"))
    return out

"""Mapping timestamps onto the CAS session calendar.

Everything downstream that needs to answer "was this during continuous or during
the auction?" goes through here, so there is exactly one definition of the
boundaries and it lives in `config`.
"""

from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd

from . import config as C
from .kdbio import td

# Boundary vector in the order the buckets appear.
_BUCKET_NAMES = [n for n, _ in C.SESSION_BUCKETS]
_BUCKET_EDGES = [td(t) for _, t in C.SESSION_BUCKETS]


def session_of(series: pd.Series) -> pd.Series:
    """Timedelta series -> session bucket name (`''` where the time is null)."""
    if series is None or len(series) == 0:
        return pd.Series([], dtype=object)
    s = pd.to_timedelta(series, errors="coerce")
    ns = s.dt.total_seconds()
    edges = [e.total_seconds() for e in _BUCKET_EDGES]
    idx = np.searchsorted(edges, ns.to_numpy(dtype="float64"), side="right") - 1
    out = np.where(
        np.isnan(ns.to_numpy(dtype="float64")) | (idx < 0),
        "",
        np.array(_BUCKET_NAMES, dtype=object)[np.clip(idx, 0, len(_BUCKET_NAMES) - 1)],
    )
    return pd.Series(out, index=series.index, dtype=object)


def phase_of(series: pd.Series) -> pd.Series:
    """Coarse phase: CONTINUOUS (before 17:45) / CLOSE (17:45-18:05) / POST /
    UNKNOWN.  This is the split the report uses for rejections."""
    bucket = session_of(series)
    mapping = {
        "CTS_EARLY": "CONTINUOUS",
        "CTS_FINAL15": "CONTINUOUS",
        "CAS_REFCALC": "CLOSE",
        "CAS_ENTRY_LM": "CLOSE",
        "CAS_ENTRY_LO": "CLOSE",
        "CAS_MATCH": "CLOSE",
        "CAS_BUFFER": "POST",
        "POST_CLOSE": "POST",
        "AFTER_HOURS": "POST",
        "": "UNKNOWN",
    }
    return bucket.map(mapping).fillna("UNKNOWN")


def in_window(series: pd.Series, start: dt.time, end: dt.time) -> pd.Series:
    """Boolean mask for `start <= t < end` on a Timedelta series."""
    s = pd.to_timedelta(series, errors="coerce")
    return (s >= td(start)) & (s < td(end))


def at_or_after(series: pd.Series, t: dt.time) -> pd.Series:
    s = pd.to_timedelta(series, errors="coerce")
    return s >= td(t)


def before(series: pd.Series, t: dt.time) -> pd.Series:
    s = pd.to_timedelta(series, errors="coerce")
    return s < td(t)


def session_table() -> pd.DataFrame:
    """The calendar itself, for the front page of the report."""
    rows = []
    edges = C.SESSION_BUCKETS + [("", C.T("23:59:59.999"))]
    labels = {
        "CTS_EARLY": "Continuous trading",
        "CTS_FINAL15": "Continuous - final 15 min (feeds the reference price VWAP)",
        "CAS_REFCALC": "1. Reference price calculation / CTS to CAS transition - no order action",
        "CAS_ENTRY_LM": "2. Order entry - limit AND market, within +/-3% of reference",
        "CAS_ENTRY_LO": "3. Order entry - limit ONLY (random close from 17:58 HKT)",
        "CAS_MATCH": "4. Order matching - the close price prints here",
        "CAS_BUFFER": "5. Buffer - no trading, settlement processing",
        "POST_CLOSE": "6. Post close - trading at last",
        "AFTER_HOURS": "After hours",
    }
    for i, (name, start) in enumerate(C.SESSION_BUCKETS):
        end = edges[i + 1][1]
        rows.append({
            "bucket": name,
            "hkt_start": start.strftime("%H:%M"),
            "hkt_end": end.strftime("%H:%M"),
            "ist_start": C.to_ist(start).strftime("%H:%M"),
            "ist_end": C.to_ist(end).strftime("%H:%M"),
            "description": labels.get(name, ""),
        })
    return pd.DataFrame(rows)

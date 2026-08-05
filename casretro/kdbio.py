"""pykx plumbing: connections, parameterised queries, pandas normalisation.

Two things this module exists to hide:

* **the date predicate.**  HDB tables are partitioned by `date`, the RT tapes are
  not (the RT qatt has no `date` column at all).  `where_date()` emits the
  predicate only when the instance says it is partitioned, so the exact same
  query text serves both modes.

* **kdb -> pandas type noise.**  q symbols and char vectors arrive as `bytes`,
  q times arrive as `timedelta64`, q nulls arrive as sentinel values.  Every
  loader pushes its frame through `normalise()` so the rest of the codebase only
  ever sees `str`, `pd.Timedelta` and `NaN`.
"""

from __future__ import annotations

import datetime as dt
import itertools
import sys
import time
from contextlib import contextmanager
from typing import Any, Iterable, Iterator, Sequence

import numpy as np
import pandas as pd

from . import config as C

try:
    import pykx as kx
except ImportError:  # pragma: no cover
    kx = None


# --------------------------------------------------------------------------- #
# Connections                                                                  #
# --------------------------------------------------------------------------- #

def require_pykx() -> None:
    if kx is None:  # pragma: no cover
        sys.exit("pykx is not installed -- run:  pip install pykx")


# --------------------------------------------------------------------------- #
# Query tracing                                                                #
# --------------------------------------------------------------------------- #

#: When on, every query that crosses the wire is written to stderr with its
#: instance, arguments, elapsed time and result shape.  stderr rather than
#: stdout so the trace can be split off:  ... --show-queries 2> queries.log
TRACE_QUERIES = False

_trace_seq = itertools.count(1)


def set_trace_queries(on: bool) -> None:
    global TRACE_QUERIES
    TRACE_QUERIES = bool(on)


def _describe_arg(a: Any) -> str:
    """Type and length, so a 500-symbol vector does not print 500 symbols."""
    name = type(a).__name__
    if isinstance(a, (dt.date, dt.time, int, float, str, bool)):
        return f"{name}={a!r}"
    try:
        return f"{name}[{len(a)}]"
    except (TypeError, AttributeError):
        return name


def _trace(instance: C.Instance, expr: str, args: tuple, t0: float,
           result: Any = None) -> None:
    if not TRACE_QUERIES:
        return
    n = next(_trace_seq)
    ms = (time.perf_counter() - t0) * 1000.0
    shape = ""
    if isinstance(result, pd.DataFrame):
        shape = f"  ->  {len(result):,} rows x {len(result.columns)} cols"
    elif result is not None:
        shape = f"  ->  {type(result).__name__}"
    argdesc = ", ".join(_describe_arg(a) for a in args) or "(no args)"

    out = sys.stderr
    print(f"\n[q #{n}] {instance.label} {instance.host}:{instance.port}"
          f"   {ms:,.1f} ms{shape}", file=out)
    print(f"        args: {argdesc}", file=out)
    for line in str(expr).strip().splitlines():
        print(f"        {line}", file=out)
    out.flush()


class Conn:
    """Thin wrapper around a SyncQConnection that knows its instance."""

    def __init__(self, instance: C.Instance):
        require_pykx()
        if not instance.configured:
            raise RuntimeError(
                f"instance {instance.label!r} is not configured -- set host/port "
                f"in {C.INSTANCES_FILE}"
            )
        self.instance = instance
        self._conn = kx.SyncQConnection(
            host=instance.host, port=instance.port, timeout=C.KDB_TIMEOUT
        )

    # -- lifecycle ---------------------------------------------------------- #

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:  # pragma: no cover
            pass

    def __enter__(self) -> "Conn":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- querying ----------------------------------------------------------- #

    def __call__(self, expr: str, *args: Any):
        t0 = time.perf_counter()
        out = self._conn(expr, *args)
        _trace(self.instance, expr, args, t0, None)
        return out

    def query_pd(self, expr: str, *args: Any) -> pd.DataFrame:
        """Run a q expression returning a table, hand back a normalised frame."""
        t0 = time.perf_counter()
        res = self._conn(expr, *args)
        try:
            df = res.pd()
        except Exception:  # keyed table or unusual shape
            df = self._conn("0!", res).pd()
        if isinstance(df, pd.Series):
            df = df.to_frame()
        out = normalise(df.reset_index(drop=False) if df.index.names != [None] else df)
        _trace(self.instance, expr, args, t0, out)
        return out

    # -- introspection ------------------------------------------------------ #

    def columns_of(self, table: str) -> list[str]:
        # through __call__ so schema probes show up in the trace too
        return [as_str(c) for c in self(f"cols `{table}").py()]

    def table_exists(self, table: str) -> bool:
        try:
            return bool(self._conn(f"`{table} in tables[]").py())
        except Exception:  # pragma: no cover
            return False


@contextmanager
def connect(instance: C.Instance) -> Iterator[Conn]:
    conn = Conn(instance)
    try:
        yield conn
    finally:
        conn.close()


class ConnectionPool:
    """One connection per (role, mode), opened lazily, closed together."""

    def __init__(self, instances: dict[str, dict[str, C.Instance]], mode: str):
        self.instances = instances
        self.mode = mode
        self._open: dict[str, Conn] = {}

    def get(self, role: str) -> Conn:
        if role not in self._open:
            inst = C.resolve(self.instances, role, self.mode)
            self._open[role] = Conn(inst)
        return self._open[role]

    def instance(self, role: str) -> C.Instance:
        return C.resolve(self.instances, role, self.mode)

    def close(self) -> None:
        for conn in self._open.values():
            conn.close()
        self._open.clear()

    def __enter__(self) -> "ConnectionPool":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


# --------------------------------------------------------------------------- #
# q value construction                                                         #
# --------------------------------------------------------------------------- #

def sym_vector(values: Iterable[str]):
    """Python strings -> q symbol vector (a bare list would become a char list)."""
    require_pykx()
    vals = [str(v) for v in values]
    try:
        return kx.toq(vals, ktype=kx.SymbolVector)
    except Exception:  # pragma: no cover
        return kx.SymbolVector(vals)


def int_vector(values: Iterable[int]):
    require_pykx()
    vals = [int(v) for v in values]
    try:
        return kx.toq(vals, ktype=kx.IntVector)
    except Exception:  # pragma: no cover
        return kx.IntVector(vals)


def time_ms(t: dt.time) -> int:
    """datetime.time -> milliseconds since midnight."""
    return t.hour * 3_600_000 + t.minute * 60_000 + t.second * 1000 + t.microsecond // 1000


def time_ms_vector(times: Sequence[dt.time]):
    """datetime.time sequence -> q int vector of milliseconds since midnight.

    Deliberately *not* a q time vector: building one would need `kx.q(...)`,
    which is only available with an embedded q licence.  The queries cast with
    `` `time$ `` on the server instead, so an IPC-only pykx install works.
    """
    return int_vector(time_ms(t) for t in times)


def where_date(instance: C.Instance, var: str = "d") -> str:
    """`date=d, ` for partitioned instances, empty string otherwise."""
    return f"date={var}, " if instance.partitioned else ""


def date_params(instance: C.Instance, date: dt.date | None) -> list[Any]:
    """The positional args a query needs for its date predicate."""
    return [date] if instance.partitioned else []


def q_lambda(params: Sequence[str], body: str) -> str:
    """Wrap `body` in a q lambda -- or not, when there are no parameters.

    `conn("{[] select ...}")` would hand back the *lambda*, not its result, so a
    zero-parameter query has to be sent as a bare expression instead.
    """
    params = [p for p in params if p]
    if not params:
        return body
    return f"{{[{';'.join(params)}] {body} }}"


# --------------------------------------------------------------------------- #
# pandas normalisation                                                         #
# --------------------------------------------------------------------------- #

_NULL_INT = {
    np.int32: np.int32(-2147483648),
    np.int64: np.int64(-9223372036854775808),
}


def as_str(v: Any) -> str:
    """bytes / numpy bytes / str -> str, with `b''` and q nulls becoming ''."""
    if v is None:
        return ""
    if isinstance(v, bytes):
        return v.decode("utf-8", "replace")
    if isinstance(v, (list, tuple, np.ndarray)):
        # q char vector arrives as a list of single bytes
        try:
            return b"".join(
                x if isinstance(x, bytes) else str(x).encode() for x in v
            ).decode("utf-8", "replace")
        except Exception:  # pragma: no cover
            return "".join(str(x) for x in v)
    if isinstance(v, float) and np.isnan(v):
        return ""
    return str(v)


def _decode_object_series(s: pd.Series) -> pd.Series:
    return s.map(as_str)


def normalise(df: pd.DataFrame) -> pd.DataFrame:
    """Decode bytes columns, turn q integer nulls into NaN, keep times as
    Timedelta.  Returns a new frame; column order is preserved."""
    if df is None or df.empty:
        return df if df is not None else pd.DataFrame()

    out = df.copy()
    for col in out.columns:
        s = out[col]
        if s.dtype == object:
            out[col] = _decode_object_series(s)
        elif s.dtype.kind in "iu":
            sentinel = _NULL_INT.get(s.dtype.type)
            if sentinel is not None and (s == sentinel).any():
                out[col] = s.astype("float64").replace(float(sentinel), np.nan)
    out.columns = [as_str(c) for c in out.columns]
    return out


def td(t: dt.time) -> pd.Timedelta:
    """datetime.time -> Timedelta, matching how q `time` lands in pandas."""
    return pd.Timedelta(
        hours=t.hour, minutes=t.minute, seconds=t.second, microseconds=t.microsecond
    )


def td_to_str(v: Any) -> str:
    """Timedelta -> 'HH:MM:SS.mmm' for display; NaT -> ''."""
    if v is None or pd.isna(v):
        return ""
    total = pd.Timedelta(v)
    ms = int(total.total_seconds() * 1000)
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


def chunks(seq: Sequence[Any], size: int) -> Iterator[Sequence[Any]]:
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def concat(frames: list[pd.DataFrame]) -> pd.DataFrame:
    frames = [f for f in frames if f is not None and not f.empty]
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)

#!/usr/bin/env python3
"""Pull the NIFTY 50 constituents, their Bloomberg codes and their weights.

**Runs on the Bloomberg machine.**  It needs `blpapi` and a live Bloomberg
session (a local Terminal, or a B-PIPE/SAPI host) and nothing else from this
project -- no kdb, no pykx -- so it can be copied over on its own.

    python tools/bloomberg_nifty50.py --out config/nifty50.csv

Check the setup first with `tools/bloomberg_check.py`, which walks the same path
one stage at a time.  The output here is the hand-off file for
`tools/map_nifty50_syms.py`, which runs on the kdb side and fills in `sym`.

The weight comes from the index's `INDX_MWEIGHT_HIST` bulk field with an
`END_DATE_OVERRIDE`, so asking for a past date gives that date's basket rather
than today's.  If the historical field is empty (some index/date combinations
have no history entitlement) it falls back to `INDX_MWEIGHT`, which is the
current basket -- and says so, because a "historical" file that silently holds
today's weights is worse than no file.

For each member a second reference request picks up `ID_ISIN`.  That field is
not optional: `tools/map_nifty50_syms.py` matches on ISIN and on nothing else, so
a member that arrives without one cannot be mapped to a kdb sym at all.  The
script counts them and says so before you carry the file to the other machine.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import os
import sys

#: Bloomberg ticker of the index.  NIFTY 50 is `NIFTY Index`.
DEFAULT_INDEX = "NIFTY Index"

#: Bulk field carrying (member, weight) pairs as of an override date.
FIELD_WEIGHT_HIST = "INDX_MWEIGHT_HIST"
#: Same thing, current basket, no override.
FIELD_WEIGHT_NOW = "INDX_MWEIGHT"

#: Sub-element names inside the bulk field.
MEMBER_KEYS = ("Index Member", "Member Ticker and Exchange Code", "Ticker")
WEIGHT_KEYS = ("Percent Weight", "Weight", "Percentage Weight")

#: Per-member reference fields.
MEMBER_FIELDS = [
    "ID_ISIN",
    "NAME",
    "TICKER",
    "COMPOSITE_EXCH_CODE",
    "EQY_PRIM_EXCH_SHRT",
    "CRNCY",
    "CUR_MKT_CAP",
    "PX_LAST",
]

OUTPUT_COLUMNS = [
    "index_ticker", "asof", "weight_source",
    "bbg_member", "bbg_ticker", "isin", "name",
    "ticker", "composite_exch_code", "prim_exch", "crncy",
    "cur_mkt_cap", "px_last", "weight_pct",
    "sym",            # left blank -- filled in by tools/map_nifty50_syms.py
    "sym_match_rule",
]

DEFAULT_HOST = "localhost"
DEFAULT_PORT = 8194
REQUEST_TIMEOUT_MS = 30_000


# --------------------------------------------------------------------------- #
# Loading blpapi                                                               #
# --------------------------------------------------------------------------- #

class BackendUnavailable(Exception):
    """blpapi could not be loaded, with the reason kept apart from the fix."""

    def __init__(self, reason: str, remedy: str = ""):
        super().__init__(reason)
        self.reason = reason
        self.remedy = remedy


PIP_HINT = (
    "pip install --index-url="
    "https://blpapi.bloomberg.com/repository/releases/python/simple/ blpapi"
)

CPP_SDK_HINT = (
    "The blpapi package is installed but its C++ SDK library could not be loaded.\n"
    "    Install the Bloomberg C++ SDK and put its shared library on the loader path:\n"
    "      Windows : <sdk>\\bin on PATH             (blpapi3_64.dll)\n"
    "      Linux   : <sdk>/Linux on LD_LIBRARY_PATH (libblpapi3_64.so)\n"
    "      macOS   : <sdk>/Darwin on DYLD_LIBRARY_PATH\n"
    "    Setting BLPAPI_ROOT to the SDK directory is usually enough.\n"
    "    Run with --diagnose to see exactly what is and is not loadable."
)


def probe_blpapi():
    """Import blpapi, classifying failure precisely.

    The distinction that matters: `ModuleNotFoundError` means blpapi really is
    not installed, while a bare `ImportError` means the package is there but
    would not load -- nearly always the C++ SDK missing from the loader path.
    Reporting the second as "not installed" sends you off to pip, which will
    cheerfully report the requirement is already satisfied.
    """
    try:
        import blpapi
        return blpapi
    except ModuleNotFoundError as exc:
        missing = (getattr(exc, "name", "") or "").split(".")[0]
        if missing and missing != "blpapi":
            raise BackendUnavailable(
                f"blpapi is installed, but its dependency {missing!r} is missing",
                f"pip install {missing}",
            ) from exc
        raise BackendUnavailable(
            "the blpapi package is not installed", PIP_HINT
        ) from exc
    except ImportError as exc:
        raise BackendUnavailable(
            f"blpapi is installed but failed to load: {exc}", CPP_SDK_HINT
        ) from exc


def diagnose() -> int:
    """Report whether blpapi is importable, and from which interpreter."""
    import importlib.util
    import platform

    try:
        import importlib.metadata as md
    except ImportError:  # pragma: no cover
        md = None

    print("environment")
    print(f"  python      : {sys.version.split()[0]}")
    print(f"  executable  : {sys.executable}")
    print(f"  platform    : {platform.platform()}")
    print(f"  BLPAPI_ROOT : {os.environ.get('BLPAPI_ROOT', '(not set)')}")

    print("\nblpapi")
    try:
        spec = importlib.util.find_spec("blpapi")
    except Exception as exc:
        spec = None
        print(f"  find_spec failed: {exc}")

    if spec is None:
        print("  NOT FOUND on this interpreter's path")
        print(
            "\n  If you know you installed it, you are almost certainly running a\n"
            f"  different interpreter than the one you installed into. Try:\n"
            f"    {sys.executable} -m pip install --index-url="
            f"https://blpapi.bloomberg.com/repository/releases/python/simple/ blpapi",
            file=sys.stderr,
        )
        return 1

    version = ""
    if md is not None:
        try:
            version = md.version("blpapi")
        except Exception:
            version = "(no dist metadata)"
    print(f"  found       : {version}")
    print(f"  location    : {getattr(spec, 'origin', '') or '(namespace package)'}")

    try:
        probe_blpapi()
    except BackendUnavailable as exc:
        print(f"  import      : FAILED - {exc.reason}")
        print(f"\n  {exc.remedy}", file=sys.stderr)
        return 1

    print("  import      : OK")
    print("\n  blpapi is usable. Next:  python tools/bloomberg_check.py")
    return 0


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #

def as_equity(member: str) -> str:
    """`RELIANCE IN` -> `RELIANCE IN Equity`; leaves an explicit yellow key be."""
    m = str(member).strip()
    if not m:
        return m
    parts = m.split()
    known = {"equity", "index", "curncy", "comdty", "corp", "govt", "mtge", "pfd"}
    if parts[-1].lower() in known:
        return m
    return f"{m} Equity"


def _first_key(row: dict, keys) -> object:
    for k in keys:
        if k in row and row[k] not in (None, ""):
            return row[k]
    return None


def _to_float(v) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# blpapi session and requests                                                  #
# --------------------------------------------------------------------------- #

def _blp_session(host: str, port: int):
    blpapi = probe_blpapi()

    opts = blpapi.SessionOptions()
    opts.setServerHost(host)
    opts.setServerPort(port)
    session = blpapi.Session(opts)
    if not session.start():
        raise RuntimeError(
            f"could not start a Bloomberg session on {host}:{port} -- is the "
            f"Terminal running and logged in?"
        )
    if not session.openService("//blp/refdata"):
        session.stop()
        raise RuntimeError("could not open //blp/refdata -- check your entitlements")
    return session


def _blp_request(session, securities, fields, overrides: dict | None = None):
    """Send one ReferenceDataRequest and return its securityData elements."""
    blpapi = probe_blpapi()

    service = session.getService("//blp/refdata")
    request = service.createRequest("ReferenceDataRequest")
    for s in securities:
        request.append("securities", s)
    for f in fields:
        request.append("fields", f)
    if overrides:
        ovr = request.getElement("overrides")
        for k, v in overrides.items():
            e = ovr.appendElement()
            e.setElement("fieldId", k)
            e.setElement("value", str(v))

    session.sendRequest(request)

    out = []
    while True:
        ev = session.nextEvent(REQUEST_TIMEOUT_MS)
        for msg in ev:
            if msg.hasElement("responseError"):
                raise RuntimeError(f"Bloomberg responseError: {msg.getElement('responseError')}")
            if not msg.hasElement("securityData"):
                continue
            sd = msg.getElement("securityData")
            for i in range(sd.numValues()):
                out.append(sd.getValueAsElement(i))
        if ev.eventType() == blpapi.Event.RESPONSE:
            break
    return out


def _element_to_dict(elem) -> dict:
    out = {}
    for i in range(elem.numElements()):
        e = elem.getElement(i)
        try:
            out[str(e.name())] = e.getValueAsString()
        except Exception:
            out[str(e.name())] = None
    return out


def _bulk_rows(security_elements, field: str) -> list[dict]:
    rows = []
    for sec in security_elements:
        if not sec.hasElement("fieldData"):
            continue
        fd = sec.getElement("fieldData")
        if not fd.hasElement(field):
            continue
        arr = fd.getElement(field)
        for i in range(arr.numValues()):
            rows.append(_element_to_dict(arr.getValueAsElement(i)))
    return rows


def _ref_rows(security_elements, fields) -> list[dict]:
    rows = []
    for sec_elem in security_elements:
        sec = sec_elem.getElementAsString("security")
        row = {"security": sec}
        if sec_elem.hasElement("fieldData"):
            fd = sec_elem.getElement("fieldData")
            for f in fields:
                if fd.hasElement(f):
                    row[f.lower()] = fd.getElement(f).getValueAsString()
        rows.append(row)
    return rows


# --------------------------------------------------------------------------- #
# The pull                                                                     #
# --------------------------------------------------------------------------- #

def fetch(
    index: str, asof: dt.date | None, host: str, port: int
) -> tuple[list[dict], list[dict], str]:
    """-> (weights, member details, which weight field was used)."""
    session = _blp_session(host, port)
    try:
        source = FIELD_WEIGHT_HIST
        overrides = {"END_DATE_OVERRIDE": asof.strftime("%Y%m%d")} if asof else None
        rows = _bulk_rows(
            _blp_request(session, [index], [FIELD_WEIGHT_HIST], overrides),
            FIELD_WEIGHT_HIST,
        )
        if not rows:
            source = FIELD_WEIGHT_NOW
            rows = _bulk_rows(
                _blp_request(session, [index], [FIELD_WEIGHT_NOW]), FIELD_WEIGHT_NOW
            )
        if not rows:
            raise RuntimeError(
                f"{index}: both {FIELD_WEIGHT_HIST} and {FIELD_WEIGHT_NOW} came "
                f"back empty -- check the ticker and your index entitlement"
            )

        weights = []
        for row in rows:
            member = _first_key(row, MEMBER_KEYS)
            weight = _first_key(row, WEIGHT_KEYS)
            if member is None:                      # unexpected sub-field names
                member = next(iter(row.values()), None)
            if member is None:
                continue
            weights.append({"member": str(member).strip(), "weight": _to_float(weight)})

        securities = [as_equity(w["member"]) for w in weights]
        details = _ref_rows(_blp_request(session, securities, MEMBER_FIELDS),
                            MEMBER_FIELDS)
        return weights, details, source
    finally:
        session.stop()


def combine(
    index: str, asof: dt.date | None, source: str,
    weights: list[dict], details: list[dict],
) -> list[dict]:
    by_sec = {d.get("security"): d for d in details}
    stamp = asof.isoformat() if asof else dt.date.today().isoformat()

    out = []
    for w in weights:
        sec = as_equity(w["member"])
        d = by_sec.get(sec, {})
        out.append({
            "index_ticker": index,
            "asof": stamp,
            "weight_source": source,
            "bbg_member": w["member"],
            "bbg_ticker": sec,
            "isin": d.get("id_isin", ""),
            "name": d.get("name", ""),
            "ticker": d.get("ticker", ""),
            "composite_exch_code": d.get("composite_exch_code", ""),
            "prim_exch": d.get("eqy_prim_exch_shrt", ""),
            "crncy": d.get("crncy", ""),
            "cur_mkt_cap": d.get("cur_mkt_cap", ""),
            "px_last": d.get("px_last", ""),
            "weight_pct": "" if w["weight"] is None else f"{w['weight']:.6f}",
            "sym": "",
            "sym_match_rule": "",
        })
    out.sort(key=lambda r: -(_to_float(r["weight_pct"]) or 0.0))
    return out


def write_csv(rows: list[dict], path: str) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=OUTPUT_COLUMNS)
        w.writeheader()
        w.writerows(rows)


# --------------------------------------------------------------------------- #

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--index", default=DEFAULT_INDEX,
                    help=f"index ticker (default: {DEFAULT_INDEX!r})")
    ap.add_argument("--date", help="YYYY-MM-DD basket date; default = today's basket")
    ap.add_argument("--out", default=os.path.join("config", "nifty50.csv"))
    ap.add_argument("--host", default=DEFAULT_HOST,
                    help=f"blpapi host (default: {DEFAULT_HOST})")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT,
                    help=f"blpapi port (default: {DEFAULT_PORT})")
    ap.add_argument("--diagnose", action="store_true",
                    help="report whether blpapi is importable, and from which "
                         "interpreter, then exit")
    ap.add_argument("--traceback", action="store_true",
                    help="print the full traceback when the pull fails")
    args = ap.parse_args(argv)

    if args.diagnose:
        return diagnose()

    asof = dt.date.fromisoformat(args.date) if args.date else None

    # Loading and calling are separated on purpose: an ImportError raised while
    # importing blpapi means something entirely different from one raised while
    # servicing a request, and conflating them sent people to pip for a package
    # that was already installed.
    try:
        probe_blpapi()
    except BackendUnavailable as exc:
        print(f"blpapi is not usable: {exc.reason}\n", file=sys.stderr)
        print(f"  {exc.remedy}", file=sys.stderr)
        print(f"\n  Run  {sys.executable} {sys.argv[0]} --diagnose  for detail.",
              file=sys.stderr)
        return 2

    try:
        weights, details, source = fetch(args.index, asof, args.host, args.port)
    except Exception as exc:
        if args.traceback:
            import traceback
            traceback.print_exc()
        print(f"blpapi loaded fine, but the pull failed:\n  {exc}\n", file=sys.stderr)
        print(
            f"  This is not an install problem. Check that the Terminal is running\n"
            f"  and logged in, that {args.host}:{args.port} is the right endpoint (a\n"
            f"  local Terminal is localhost:8194; B-PIPE/SAPI is a different host),\n"
            f"  and that {args.index!r} is a ticker you are entitled to.\n"
            f"  Re-run with --traceback for the full stack, or run\n"
            f"  python tools/bloomberg_check.py to find the failing stage.",
            file=sys.stderr,
        )
        return 2

    rows = combine(args.index, asof, source, weights, details)
    write_csv(rows, args.out)

    total = sum(_to_float(r["weight_pct"]) or 0.0 for r in rows)
    missing_isin = sum(1 for r in rows if not r["isin"])

    print(f"[info] {args.index}: {len(rows)} members, weights sum to {total:.4f}%")
    if source == FIELD_WEIGHT_NOW and asof:
        print(
            f"[warn] {FIELD_WEIGHT_HIST} returned nothing for {asof} -- fell back to "
            f"{FIELD_WEIGHT_NOW}, so these are TODAY's weights, not {asof}'s",
            file=sys.stderr,
        )
    if missing_isin:
        print(
            f"[warn] {missing_isin} of {len(rows)} member(s) came back without an "
            f"ID_ISIN.\n"
            f"       ISIN is the only key map_nifty50_syms.py matches on, so those "
            f"members\n"
            f"       will NOT get a sym. Fill the isin column in by hand before "
            f"copying the\n"
            f"       file across, or re-run once the ID_ISIN entitlement is sorted.",
            file=sys.stderr,
        )
    if abs(total - 100.0) > 1.0:
        print(f"[warn] weights sum to {total:.2f}%, not ~100% -- check the index ticker "
              f"and the override date", file=sys.stderr)

    print(f"\n  written -> {args.out}")
    print(f"  next    -> copy it to the kdb machine and run:\n"
          f"             python tools/map_nifty50_syms.py --file {args.out}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

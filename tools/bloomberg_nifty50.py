#!/usr/bin/env python3
"""Pull the NIFTY 50 constituents, their Bloomberg codes and their weights.

**Runs on the Bloomberg machine.**  It needs a live Bloomberg session (a local
Terminal or a B-PIPE/SAPI host) and nothing else from this project -- no kdb, no
pykx -- so it can be copied over on its own.

    python tools/bloomberg_nifty50.py --out config/nifty50.csv

The output is the hand-off file for `tools/map_nifty50_syms.py`, which runs on
the kdb side and fills in the `sym` column.

Two backends, tried in this order:

* **xbbg** -- if installed, one call and done.
* **blpapi** -- the raw SDK, used directly otherwise.

The weight comes from the index's `INDX_MWEIGHT_HIST` bulk field with an
`END_DATE_OVERRIDE`, so asking for a past date gives that date's basket rather
than today's.  If the historical field is empty (some index/date combinations
have no history entitlement) it falls back to `INDX_MWEIGHT`, which is the
current basket -- and says so, because a "historical" file that silently holds
today's weights is worse than no file.

For each member a second reference request picks up `ID_ISIN`, which is what
makes the kdb-side mapping reliable: ticker strings drift, ISINs do not.
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


# --------------------------------------------------------------------------- #
# Backend: xbbg                                                                #
# --------------------------------------------------------------------------- #

def fetch_via_xbbg(index: str, asof: dt.date | None) -> tuple[list[dict], list[dict], str]:
    from xbbg import blp  # noqa: F401  (import error handled by the caller)

    source = FIELD_WEIGHT_HIST
    kwargs = {"END_DATE_OVERRIDE": asof.strftime("%Y%m%d")} if asof else {}
    df = blp.bds(index, FIELD_WEIGHT_HIST, **kwargs)
    if df is None or df.empty:
        source = FIELD_WEIGHT_NOW
        df = blp.bds(index, FIELD_WEIGHT_NOW)
    if df is None or df.empty:
        raise RuntimeError(f"{index}: both {FIELD_WEIGHT_HIST} and {FIELD_WEIGHT_NOW} came back empty")

    df = df.reset_index(drop=True)
    lower = {c.lower().replace("_", " "): c for c in df.columns}
    member_col = next((lower[k.lower()] for k in MEMBER_KEYS if k.lower() in lower), df.columns[0])
    weight_col = next((lower[k.lower()] for k in WEIGHT_KEYS if k.lower() in lower), df.columns[-1])

    weights = [
        {"member": str(r[member_col]).strip(), "weight": _to_float(r[weight_col])}
        for _, r in df.iterrows()
        if str(r[member_col]).strip()
    ]

    securities = [as_equity(w["member"]) for w in weights]
    ref = blp.bdp(securities, MEMBER_FIELDS)
    details = []
    for sec in securities:
        row = {}
        if ref is not None and sec in ref.index:
            row = {k.lower(): v for k, v in ref.loc[sec].to_dict().items()}
        details.append({"security": sec, **row})
    return weights, details, source


# --------------------------------------------------------------------------- #
# Backend: blpapi                                                              #
# --------------------------------------------------------------------------- #

def _blp_session(host: str, port: int):
    import blpapi

    opts = blpapi.SessionOptions()
    opts.setServerHost(host)
    opts.setServerPort(port)
    session = blpapi.Session(opts)
    if not session.start():
        raise RuntimeError(f"could not start a Bloomberg session on {host}:{port}")
    if not session.openService("//blp/refdata"):
        session.stop()
        raise RuntimeError("could not open //blp/refdata")
    return session


def _blp_request(session, securities, fields, overrides: dict | None = None):
    """Send one ReferenceDataRequest and return its securityData messages."""
    import blpapi

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
        ev = session.nextEvent(30_000)
        for msg in ev:
            if msg.hasElement("responseError"):
                raise RuntimeError(f"Bloomberg responseError: {msg.getElement('responseError')}")
            if not msg.hasElement("securityData"):
                continue
            sd = msg.getElement("securityData")
            # ReferenceDataResponse: securityData is an array
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


def fetch_via_blpapi(
    index: str, asof: dt.date | None, host: str, port: int
) -> tuple[list[dict], list[dict], str]:
    session = _blp_session(host, port)
    try:
        source = FIELD_WEIGHT_HIST
        overrides = {"END_DATE_OVERRIDE": asof.strftime("%Y%m%d")} if asof else None
        rows = _bulk_rows(_blp_request(session, [index], [FIELD_WEIGHT_HIST], overrides),
                          FIELD_WEIGHT_HIST)
        if not rows:
            source = FIELD_WEIGHT_NOW
            rows = _bulk_rows(_blp_request(session, [index], [FIELD_WEIGHT_NOW]),
                              FIELD_WEIGHT_NOW)
        if not rows:
            raise RuntimeError(
                f"{index}: both {FIELD_WEIGHT_HIST} and {FIELD_WEIGHT_NOW} came back empty"
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
        details = []
        for sec_elem in _blp_request(session, securities, MEMBER_FIELDS):
            sec = sec_elem.getElementAsString("security")
            row = {"security": sec}
            if sec_elem.hasElement("fieldData"):
                fd = sec_elem.getElement("fieldData")
                for f in MEMBER_FIELDS:
                    if fd.hasElement(f):
                        row[f.lower()] = fd.getElement(f).getValueAsString()
            details.append(row)
        return weights, details, source
    finally:
        session.stop()


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


# --------------------------------------------------------------------------- #

def _to_float(v) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


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


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--index", default=DEFAULT_INDEX,
                    help=f"index ticker (default: {DEFAULT_INDEX!r})")
    ap.add_argument("--date", help="YYYY-MM-DD basket date; default = today's basket")
    ap.add_argument("--out", default=os.path.join("config", "nifty50.csv"))
    ap.add_argument("--host", default="localhost", help="blpapi host (default: localhost)")
    ap.add_argument("--port", type=int, default=8194, help="blpapi port (default: 8194)")
    ap.add_argument("--backend", choices=("auto", "xbbg", "blpapi"), default="auto")
    args = ap.parse_args(argv)

    asof = dt.date.fromisoformat(args.date) if args.date else None

    backends = ("xbbg", "blpapi") if args.backend == "auto" else (args.backend,)
    weights = details = None
    source = ""
    errors = []
    for backend in backends:
        try:
            if backend == "xbbg":
                weights, details, source = fetch_via_xbbg(args.index, asof)
            else:
                weights, details, source = fetch_via_blpapi(args.index, asof, args.host, args.port)
            print(f"[info] backend = {backend}")
            break
        except ImportError as exc:
            errors.append(f"{backend}: not installed ({exc})")
        except Exception as exc:
            errors.append(f"{backend}: {exc}")

    if weights is None:
        print("Could not reach Bloomberg.\n  " + "\n  ".join(errors), file=sys.stderr)
        print(
            "\nInstall one of them on the Bloomberg machine:\n"
            "  pip install xbbg     (wraps blpapi, simplest)\n"
            "  pip install blpapi --index-url=https://blpapi.bloomberg.com/repository/releases/python/simple/",
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
        print(f"[warn] {missing_isin} member(s) came back without an ISIN -- the kdb "
              f"mapping will have to fall back to ticker matching", file=sys.stderr)
    if abs(total - 100.0) > 1.0:
        print(f"[warn] weights sum to {total:.2f}%, not ~100% -- check the index ticker "
              f"and the override date", file=sys.stderr)

    print(f"\n  written -> {args.out}")
    print(f"  next    -> copy it to the kdb machine and run:\n"
          f"             python tools/map_nifty50_syms.py --file {args.out}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Stage-by-stage check that the Bloomberg side is actually working.

**Runs on the Bloomberg machine**, next to `tools/bloomberg_nifty50.py` (it
imports it, so copy both files across).

    python tools/bloomberg_check.py
    python tools/bloomberg_check.py --backend blpapi --date 2026-08-04
    python tools/bloomberg_check.py --verbose        # show the data each stage got

It walks the same path the real script takes, one stage at a time, so a failure
tells you *which* thing is broken instead of just "could not reach Bloomberg":

    1  environment      which interpreter, BLPAPI_ROOT
    2  import           the package loads (wheel present *and* C++ SDK loadable)
    3  session          a session starts and //blp/refdata opens
    4  reference data   a plain BDP on the index returns a name
    5  index members    INDX_MWEIGHT returns a basket with weights
    6  historical       INDX_MWEIGHT_HIST honours END_DATE_OVERRIDE
    7  ISIN             ID_ISIN comes back for the members
    8  end to end       the real fetch path produces rows

Stage 7 is not optional decoration: `tools/map_nifty50_syms.py` matches on ISIN
and nothing else, so a member without one cannot be mapped to a kdb sym at all.

Exit code is 0 only when every required stage passed.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import bloomberg_nifty50 as BN  # noqa: E402

PASS, FAIL, WARN, SKIP = "PASS", "FAIL", "WARN", "SKIP"


def oneline(s: str, limit: int = 150) -> str:
    """Collapse whitespace so a vendor message cannot break the table layout.

    blpapi's C++ SDK error in particular is several paragraphs long.
    """
    flat = " ".join(str(s).split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


class Stage:
    def __init__(self, n: int, name: str):
        self.n = n
        self.name = name
        self.status = SKIP
        self.detail = ""
        self.remedy = ""
        self.notes: list[str] = []
        self.data = None

    def ok(self, detail: str = "", data=None) -> "Stage":
        self.status, self.detail, self.data = PASS, detail, data
        return self

    def warn(self, detail: str, remedy: str = "") -> "Stage":
        self.status, self.detail, self.remedy = WARN, detail, remedy
        return self

    def fail(self, detail: str, remedy: str = "") -> "Stage":
        self.status, self.detail, self.remedy = FAIL, detail, remedy
        return self

    def skip(self, detail: str) -> "Stage":
        self.status, self.detail = SKIP, detail
        return self


# --------------------------------------------------------------------------- #
# Backend-agnostic data access                                                 #
# --------------------------------------------------------------------------- #

def xbbg_ref(mod, securities, fields, overrides=None) -> dict[str, dict]:
    """-> {security: {field_lowercase: value}}."""
    df = mod.bdp(securities, fields, **(overrides or {}))
    out: dict[str, dict] = {}
    if df is None or getattr(df, "empty", True):
        return out
    for sec in df.index:
        out[str(sec)] = {str(k).lower(): v for k, v in df.loc[sec].to_dict().items()}
    return out


class BlpapiClient:
    """Holds one blpapi session so the stages can share it."""

    def __init__(self, host: str, port: int):
        self.session = BN._blp_session(host, port)

    def ref_data(self, securities, fields, overrides=None) -> dict[str, dict]:
        out = {}
        for sec_elem in BN._blp_request(self.session, securities, fields, overrides):
            sec = sec_elem.getElementAsString("security")
            row = {}
            if sec_elem.hasElement("fieldData"):
                fd = sec_elem.getElement("fieldData")
                for f in fields:
                    if fd.hasElement(f):
                        row[f.lower()] = fd.getElement(f).getValueAsString()
            out[sec] = row
        return out

    def bulk(self, security, field, overrides=None) -> list[dict]:
        return BN._bulk_rows(
            BN._blp_request(self.session, [security], [field], overrides), field
        )

    def close(self):
        try:
            self.session.stop()
        except Exception:
            pass


def xbbg_bulk(mod, security, field, overrides=None) -> list[dict]:
    df = mod.bds(security, field, **(overrides or {}))
    if df is None or getattr(df, "empty", True):
        return []
    rows = []
    for _, r in df.reset_index(drop=True).iterrows():
        rows.append({str(k).replace("_", " ").title(): v for k, v in r.to_dict().items()})
    return rows


def _member_of(row: dict):
    return BN._first_key(row, BN.MEMBER_KEYS) or next(iter(row.values()), None)


def _weight_of(row: dict):
    return BN._to_float(BN._first_key(row, BN.WEIGHT_KEYS))


# --------------------------------------------------------------------------- #
# Stages                                                                       #
# --------------------------------------------------------------------------- #

def run(args) -> list[Stage]:
    stages: list[Stage] = []
    import platform

    # -- 1. environment ---------------------------------------------------- #
    s = Stage(1, "environment")
    s.ok(f"python {sys.version.split()[0]} at {sys.executable}; "
         f"{platform.system()}; BLPAPI_ROOT={os.environ.get('BLPAPI_ROOT', '(not set)')}")
    stages.append(s)

    # -- 2. import ---------------------------------------------------------- #
    backends = ("xbbg", "blpapi") if args.backend == "auto" else (args.backend,)
    s = Stage(2, "import backend")
    backend = mod = None
    tried = []
    for name in backends:
        try:
            mod = BN.probe_backend(name)
            backend = name
            break
        except BN.BackendUnavailable as exc:
            tried.append(exc)
    if backend is None:
        s.fail(f"no usable backend out of {', '.join(backends)}")
        for e in tried:
            s.notes.append(f"{e.backend}: {oneline(e.reason)}")
        remedies = []
        for e in tried:
            if e.remedy and e.remedy not in remedies:
                remedies.append(e.remedy)
        s.remedy = "\n".join(remedies)
        stages.append(s)
        return stages
    version = ""
    try:
        import importlib.metadata as md
        version = md.version(backend)
    except Exception:
        pass
    s.ok(f"{backend} {version} loaded"
         + (f" (skipped: {', '.join(e.backend for e in tried)})" if tried else ""))
    stages.append(s)

    client = None
    try:
        # -- 3. session ----------------------------------------------------- #
        s = Stage(3, "session / //blp/refdata")
        if backend == "blpapi":
            try:
                client = BlpapiClient(args.host, args.port)
                s.ok(f"connected to {args.host}:{args.port}")
            except Exception as exc:
                s.fail(str(exc),
                       "Check the Terminal is running and logged in, and that "
                       f"{args.host}:{args.port} is right\n"
                       "    (a local Terminal is localhost:8194; B-PIPE/SAPI is a "
                       "different host).")
                stages.append(s)
                return stages
        else:
            s.skip("xbbg opens its own session on first use - covered by stage 4")
        stages.append(s)

        def do_ref(securities, fields, overrides=None):
            if backend == "xbbg":
                return xbbg_ref(mod, securities, fields, overrides)
            return client.ref_data(securities, fields, overrides)

        def do_bulk(security, field, overrides=None):
            if backend == "xbbg":
                return xbbg_bulk(mod, security, field, overrides)
            return client.bulk(security, field, overrides)

        # -- 4. plain reference data ---------------------------------------- #
        s = Stage(4, f"reference data on {args.index!r}")
        try:
            got = do_ref([args.index], ["NAME", "CRNCY"])
            row = got.get(args.index) or (next(iter(got.values()), {}) if got else {})
            if row.get("name"):
                s.ok(f"NAME = {row['name']}", row)
            else:
                s.fail(f"no NAME came back for {args.index!r}",
                       "Check the ticker spelling and your entitlement. NIFTY 50 is "
                       "`NIFTY Index`.")
        except Exception as exc:
            s.fail(str(exc), "The session works but the request failed - usually an "
                             "entitlement or ticker problem.")
        stages.append(s)

        # -- 5. current basket ---------------------------------------------- #
        s = Stage(5, f"{BN.FIELD_WEIGHT_NOW} (current basket)")
        members_now = []
        try:
            rows = do_bulk(args.index, BN.FIELD_WEIGHT_NOW)
            members_now = [r for r in rows if _member_of(r)]
            weights = [_weight_of(r) for r in members_now]
            have_w = [w for w in weights if w is not None]
            if not members_now:
                s.fail("returned no members",
                       f"{BN.FIELD_WEIGHT_NOW} is empty for {args.index!r} - check the "
                       "ticker and your index entitlement.")
            elif not have_w:
                s.warn("members came back but no weights - the sub-field names may "
                       f"differ from {BN.WEIGHT_KEYS}",
                       "Re-run with --verbose and send me the field names.")
            else:
                s.ok(f"{len(members_now)} members, weights sum to {sum(have_w):.2f}%",
                     members_now)
        except Exception as exc:
            s.fail(str(exc))
        stages.append(s)

        # -- 6. historical basket ------------------------------------------- #
        asof = dt.date.fromisoformat(args.date) if args.date else None
        s = Stage(6, f"{BN.FIELD_WEIGHT_HIST} + END_DATE_OVERRIDE")
        if asof is None:
            s.skip("no --date given; the real script only overrides when asked")
        else:
            try:
                rows = do_bulk(args.index, BN.FIELD_WEIGHT_HIST,
                               {"END_DATE_OVERRIDE": asof.strftime("%Y%m%d")})
                rows = [r for r in rows if _member_of(r)]
                if not rows:
                    s.warn(f"empty for {asof} - the real script will fall back to "
                           f"{BN.FIELD_WEIGHT_NOW} (today's basket)",
                           "Fine for a same-day run. For a historical run you need "
                           "the index-history entitlement,\n    or the date is not a "
                           "trading day.")
                else:
                    s.ok(f"{len(rows)} members as of {asof}", rows)
            except Exception as exc:
                s.fail(str(exc))
        stages.append(s)

        # -- 7. ISIN entitlement -------------------------------------------- #
        s = Stage(7, "ID_ISIN on the members")
        sample = [BN.as_equity(_member_of(r)) for r in members_now[:args.members]]
        if not sample:
            s.skip("no members to test - stage 5 failed")
        else:
            try:
                got = do_ref(sample, ["ID_ISIN", "NAME"])
                missing = [x for x in sample if not (got.get(x) or {}).get("id_isin")]
                if not missing:
                    s.ok(f"all {len(sample)} sampled members have an ISIN", got)
                elif len(missing) == len(sample):
                    s.fail(f"none of the {len(sample)} sampled members returned an "
                           f"ID_ISIN",
                           "map_nifty50_syms.py matches on ISIN and nothing else, so "
                           "nothing would map.\n    This is an ID_ISIN entitlement "
                           "problem - raise it with your Bloomberg rep.")
                else:
                    s.warn(f"{len(missing)} of {len(sample)} sampled members have no "
                           f"ISIN: {', '.join(missing[:5])}",
                           "Those members will not get a sym; fill the isin column in "
                           "by hand.")
            except Exception as exc:
                s.fail(str(exc))
        stages.append(s)

    finally:
        if client is not None:
            client.close()

    # -- 8. end to end ------------------------------------------------------ #
    s = Stage(8, "end-to-end fetch (the real code path)")
    try:
        asof = dt.date.fromisoformat(args.date) if args.date else None
        if backend == "xbbg":
            weights, details, source = BN.fetch_via_xbbg(args.index, asof)
        else:
            weights, details, source = BN.fetch_via_blpapi(
                args.index, asof, args.host, args.port
            )
        rows = BN.combine(args.index, asof, source, weights, details)
        with_isin = sum(1 for r in rows if r["isin"])
        total = sum(BN._to_float(r["weight_pct"]) or 0.0 for r in rows)
        detail = (f"{len(rows)} rows via {source}, {with_isin} with an ISIN, "
                  f"weights sum to {total:.2f}%")
        if not rows:
            s.fail("produced no rows")
        elif with_isin == 0:
            s.fail(detail + " - nothing would map to a sym")
        else:
            s.ok(detail, rows[:5])
    except Exception as exc:
        s.fail(str(exc), "Re-run bloomberg_nifty50.py with --traceback for the full "
                         "stack.")
    stages.append(s)
    return stages


# --------------------------------------------------------------------------- #

def report(stages: list[Stage], verbose: bool) -> int:
    print()
    width = max(len(s.name) for s in stages) + 2
    for s in stages:
        print(f"  [{s.n}/8] {s.name:<{width}} {s.status}   {oneline(s.detail, 200)}")
        for note in s.notes:
            print(f"          . {note}")
        if s.remedy:
            for line in s.remedy.splitlines():
                print(f"          -> {line}")
        if verbose and s.data is not None:
            preview = s.data if isinstance(s.data, list) else [s.data]
            for row in preview[:5]:
                print(f"          {row}")

    failed = [s for s in stages if s.status == FAIL]
    warned = [s for s in stages if s.status == WARN]
    print()
    if failed:
        print(f"  FAILED at stage {failed[0].n} ({failed[0].name}).")
        return 1
    if warned:
        print(f"  OK with {len(warned)} warning(s) - the pull will work, read them "
              f"before trusting the output.")
        return 0
    print("  OK - Bloomberg is working end to end. Next:")
    print("    python tools/bloomberg_nifty50.py --out config/nifty50.csv")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--index", default=BN.DEFAULT_INDEX)
    ap.add_argument("--date", help="YYYY-MM-DD; also exercises END_DATE_OVERRIDE")
    ap.add_argument("--host", default="localhost")
    ap.add_argument("--port", type=int, default=8194)
    ap.add_argument("--backend", choices=("auto", "xbbg", "blpapi"), default="auto")
    ap.add_argument("--members", type=int, default=5,
                    help="how many members to ISIN-test (default: 5)")
    ap.add_argument("--verbose", action="store_true",
                    help="print a sample of what each stage received")
    args = ap.parse_args(argv)

    print(f"Bloomberg check -- index={args.index!r} backend={args.backend} "
          f"{args.host}:{args.port}")
    return report(run(args), args.verbose)


if __name__ == "__main__":
    sys.exit(main())

"""Static configuration: paths, instance wiring, CAS session calendar, thresholds.

Everything time-related in this project is expressed in **HKT**, because that is
what the raw `time` columns of the kdb tables carry.  IST = HKT - 02:30.

The session calendar comes straight from the India CAS deck:

    #   Session                                   IST            HKT
    1   Ref price calc / CTS->CAS transition      15:15-15:20    17:45-17:50
    2   Order entry - limit AND market            15:20-15:25    17:50-17:55
    3   Order entry - limit ONLY (+random close)  15:25-15:30    17:55-18:00
    3A  Random close (system driven)              15:28-15:30    17:58-18:00
    4   Order matching -> close print             15:30-15:35    18:00-18:05
    5   Buffer (no trading)                       15:35-15:50    18:05-18:20
    6   Post close / trading-at-last              15:50-16:00    18:20-18:30
"""

from __future__ import annotations

import datetime as dt
import json
import os
from dataclasses import dataclass, field
from typing import Any

# --------------------------------------------------------------------------- #
# Paths                                                                        #
# --------------------------------------------------------------------------- #

PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(PACKAGE_DIR)
CONFIG_DIR = os.path.join(PROJECT_DIR, "config")
OUTPUT_DIR = os.path.join(PROJECT_DIR, "output")

INSTANCES_FILE = os.path.join(CONFIG_DIR, "instances.json")
ISIN_FILE = os.path.join(CONFIG_DIR, "cas_isins.txt")


# --------------------------------------------------------------------------- #
# Time helpers                                                                 #
# --------------------------------------------------------------------------- #

IST_OFFSET = dt.timedelta(hours=-2, minutes=-30)  # HKT + this = IST


def T(hhmm: str) -> dt.time:
    """'17:45' / '17:45:30' / '17:45:30.250' -> datetime.time."""
    parts = hhmm.split(":")
    h, m = int(parts[0]), int(parts[1])
    s, us = 0, 0
    if len(parts) > 2:
        sec = float(parts[2])
        s = int(sec)
        us = int(round((sec - s) * 1_000_000))
    return dt.time(h, m, s, us)


def q_time(t: dt.time) -> str:
    """datetime.time -> q time literal with milliseconds (`17:45:00.000`).

    The milliseconds matter: `17:45:00` parses as a *second* atom in q and would
    not compare cleanly against a `time` (millisecond) column.
    """
    return f"{t.hour:02d}:{t.minute:02d}:{t.second:02d}.{t.microsecond // 1000:03d}"


def to_ist(t: dt.time) -> dt.time:
    """HKT time -> IST time (display only)."""
    base = dt.datetime.combine(dt.date(2000, 1, 1), t) + IST_OFFSET
    return base.time()


# --------------------------------------------------------------------------- #
# CAS session calendar (HKT)                                                   #
# --------------------------------------------------------------------------- #

# Continuous trading session ends / the exchange starts computing the reference
# price.  Everything at or after this instant is "the close" for our purposes.
CTS_END = T("17:45")

REF_VWAP_START = T("17:30")   # 15:00 IST -- VWAP window feeding the reference price
REF_VWAP_END = T("17:45")     # 15:15 IST

CAS_START = T("17:45")        # session 1 -- no order action allowed
REF_CALC_END = T("17:50")

ENTRY_LM_START = T("17:50")   # session 2 -- limit AND market orders accepted
ENTRY_LM_END = T("17:55")

ENTRY_LO_START = T("17:55")   # session 3 -- limit only, market orders refused
ENTRY_LO_END = T("18:00")

RANDOM_CLOSE_START = T("17:58")   # session 3A -- the auction can freeze any time now

MATCH_START = T("18:00")      # session 4 -- matching, the close print happens here
MATCH_END = T("18:05")

BUFFER_END = T("18:20")       # session 5
POST_CLOSE_END = T("18:30")   # session 6 -- trading at last

#: Ordered bucket boundaries used to label any timestamp (and to bucket market
#: volume).  Each entry is (name, start).  The bucket runs until the next start.
SESSION_BUCKETS: list[tuple[str, dt.time]] = [
    ("CTS_EARLY", T("00:00")),
    ("CTS_FINAL15", REF_VWAP_START),
    ("CAS_REFCALC", CAS_START),
    ("CAS_ENTRY_LM", ENTRY_LM_START),
    ("CAS_ENTRY_LO", ENTRY_LO_START),
    ("CAS_MATCH", MATCH_START),
    ("CAS_BUFFER", MATCH_END),
    ("POST_CLOSE", BUFFER_END),
    ("AFTER_HOURS", POST_CLOSE_END),
]

#: Buckets that belong to the close auction as a whole.
CAS_BUCKETS = ("CAS_REFCALC", "CAS_ENTRY_LM", "CAS_ENTRY_LO", "CAS_MATCH")

#: The bucket in which the auction actually prints.
AUCTION_PRINT_BUCKET = "CAS_MATCH"


# --------------------------------------------------------------------------- #
# Close participation timing (from the deck's "Close Participation Timing")    #
# --------------------------------------------------------------------------- #

#: End time a CAS-name parent order is expected to carry when it participates in
#: the close (`Y` or blank/default).  18:05 HKT / 15:35 IST.
EXPECTED_TEND_CAS_PARTICIPATING = T("18:05")

#: End time a CAS-name parent order carries when it is flagged `N`.
#: 17:45 HKT / 15:15 IST -- i.e. it stops at the end of continuous.
EXPECTED_TEND_CAS_NOT_PARTICIPATING = T("17:45")

#: Non-CAS names: 18:00 HKT / 15:30 IST whatever the flag.
EXPECTED_TEND_NON_CAS = T("18:00")

#: What we actually observe on the desk, which is *not* what the deck says: most
#: parents that do go on to trade in the close carry a `t_end` inside the last
#: few minutes of continuous -- after 17:40 and before 17:45 HKT -- rather than
#: the 18:05 of the table above.  So `t_end <= 17:45` cannot be read as "this
#: order was never meant to trade in the close": it would flag the participating
#: majority.  Only a `t_end` at or before this cutoff is treated as the "N"
#: profile.  Raise it back to CTS_END if a desk really does book `N` orders at
#: 17:45.
TEND_NO_CLOSE_CUTOFF = T("17:40")

#: The observed `t_end` window of a close-participating parent, used for the
#: explanatory note attached to ORDER_END_BEFORE_CAS.
TEND_CLOSE_PARTICIPATION_WINDOW = (TEND_NO_CLOSE_CUTOFF, CTS_END)


# --------------------------------------------------------------------------- #
# Exchange rules                                                               #
# --------------------------------------------------------------------------- #

#: During CAS every order must sit within +/- this fraction of the reference
#: price.  Orders outside the band are rejected by the exchange.
CAS_PRICE_BAND = 0.03


# --------------------------------------------------------------------------- #
# Benchmarks (from the day-1 mail)                                             #
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Benchmarks:
    """Reference numbers quoted in the desk mail, in percent of daily volume."""

    #: Historical CAS-eligible closing-bin average, 6-30 Jul 2026, 19 trading
    #: days.  The bin is the last 30 minutes of the old regime = 15:00-15:30 IST
    #: = 17:30-18:00 HKT.
    hist_clsbin_avg: float = 17.29
    hist_clsbin_min: float = 13.61
    hist_clsbin_max: float = 22.01

    #: Day 1 of the new mechanism.
    day1_cts_share: float = 9.90       # 15:15-15:30 IST window
    day1_cas_share: float = 2.09       # close auction
    day1_total_share: float = 11.99


BENCHMARKS = Benchmarks()

#: Window the mail calls "the final continuous trading session" (15:15-15:30 IST).
#: Kept configurable because the deck puts the CTS/CAS switch at 15:15 IST, so
#: this window overlaps the CAS order-entry phases -- see README.
BENCHMARK_CTS_WINDOW = (CTS_END, MATCH_START)          # 17:45-18:00 HKT

#: Window in which the auction volume prints.
BENCHMARK_CAS_WINDOW = (MATCH_START, MATCH_END)        # 18:00-18:05 HKT

#: The historical "closing bin" -- last 30 minutes, 15:00-15:30 IST.
BENCHMARK_CLSBIN_WINDOW = (REF_VWAP_START, MATCH_START)  # 17:30-18:00 HKT


# --------------------------------------------------------------------------- #
# Universe                                                                     #
# --------------------------------------------------------------------------- #

#: sym suffixes identifying the Indian listings in the `equity` ref table.
SYM_SUFFIXES = ("*.IN", "*.IS", "*.IB")

#: syms are pushed to kdb in batches of this size; ids likewise.
SYM_CHUNK = 500
ID_CHUNK = 2000


# --------------------------------------------------------------------------- #
# Table-specific predicates                                                    #
# --------------------------------------------------------------------------- #

#: q where-clause fragment isolating *trade* records in the qatt table.  Adjust
#: once you know the exact `typ` domain of your feed -- e.g.
#:     QATT_TRADE_FILTER = "typ in `trade`auction, not null price, size > 0"
QATT_TRADE_FILTER = "not null price, not null size, size > 0"

#: q where-clause fragment isolating records that carry a usable quote.
QATT_QUOTE_FILTER = "not null qbid, not null qask, qbid > 0, qask > 0"


# --------------------------------------------------------------------------- #
# Flow (basket) classification                                                 #
# --------------------------------------------------------------------------- #

SILK_TOKEN = "SILK"
FLOW_SILK = "SILK"
FLOW_AGENCY = "AGENCY"


def flow_of(basket: str | None) -> str:
    """`basket` contains SILK -> SILK, anything else -> Agency."""
    if basket is None:
        return FLOW_AGENCY
    return FLOW_SILK if SILK_TOKEN in str(basket).upper() else FLOW_AGENCY


# --------------------------------------------------------------------------- #
# Misc thresholds                                                              #
# --------------------------------------------------------------------------- #

#: Drop parent orders that executed nothing at all and ended cancelled.  They
#: are pulled orders rather than close misses, so leaving them in only inflates
#: the NOT_SENT bucket and depresses the participation rate.  Flip to False (or
#: pass --keep-unfilled-cancelled) to see them again.
DROP_UNFILLED_CANCELLED = True

#: A residual smaller than this many shares (or than this fraction of the parent)
#: is treated as "done" rather than as a genuine miss.
RESIDUAL_ABS_TOL = 0
RESIDUAL_PCT_TOL = 0.0005

#: Connection timeout, seconds.
KDB_TIMEOUT = 300.0


# --------------------------------------------------------------------------- #
# Instance wiring                                                              #
# --------------------------------------------------------------------------- #

@dataclass
class Instance:
    """One kdb+ process."""

    role: str
    mode: str
    label: str
    host: str
    port: int
    partitioned: bool
    tables: dict[str, str] = field(default_factory=dict)

    def table(self, logical: str) -> str:
        try:
            return self.tables[logical]
        except KeyError:
            raise KeyError(
                f"instance {self.label!r} has no mapping for table {logical!r}; "
                f"known: {sorted(self.tables)}"
            ) from None

    @property
    def configured(self) -> bool:
        return bool(self.host) and self.host != "CHANGE_ME" and int(self.port) > 0

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.label} ({self.host}:{self.port})"


def load_instances(path: str = INSTANCES_FILE) -> dict[str, dict[str, Instance]]:
    """Parse config/instances.json into {role: {mode: Instance}}."""
    with open(path, "r", encoding="utf-8") as fh:
        raw: dict[str, Any] = json.load(fh)

    out: dict[str, dict[str, Instance]] = {}
    for role, modes in raw.get("sources", {}).items():
        out[role] = {}
        for mode, cfg in modes.items():
            out[role][mode] = Instance(
                role=role,
                mode=mode,
                label=cfg.get("label", f"{role}-{mode}".upper()),
                host=cfg.get("host", ""),
                port=int(cfg.get("port", 0) or 0),
                partitioned=bool(cfg.get("partitioned", True)),
                tables=dict(cfg.get("tables", {})),
            )
    return out


def resolve(instances: dict[str, dict[str, Instance]], role: str, mode: str) -> Instance:
    """Pick the instance for a role, falling back to 'ht' when 'rt' is absent."""
    try:
        by_mode = instances[role]
    except KeyError:
        raise KeyError(f"no instance configured for role {role!r}") from None
    if mode in by_mode:
        return by_mode[mode]
    if "ht" in by_mode:
        return by_mode["ht"]
    raise KeyError(f"role {role!r} has no {mode!r} and no 'ht' instance")

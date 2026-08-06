"""Which days a run covers, and which tape each of them is read from.

This is the whole of the period logic, and it lives here because v2 is the only
report that spans days -- `casretro` answers for one date and takes it as an
argument.

Two questions, both pure and both separately tested, because both fail silently
in production if they are wrong: a review that reports four days as five, or one
that stamps today's real-time tape with last Friday's date.
"""

from __future__ import annotations

import datetime as dt

import pandas as pd

#: Monday-Friday.  The Indian exchange calendar has holidays this does not know
#: about; they arrive as a day with no order and are dropped with a note.
_WEEKEND = (5, 6)

#: How many days a default weekly run looks back over, anchor included.
WEEK_LENGTH = 5


# --------------------------------------------------------------------------- #
# The calendar                                                                 #
# --------------------------------------------------------------------------- #

def is_business_day(d: dt.date) -> bool:
    return d.weekday() not in _WEEKEND


def business_days(start: dt.date, end: dt.date) -> list[dt.date]:
    """Every Mon-Fri in [start, end], inclusive."""
    if end < start:
        start, end = end, start
    out, day = [], start
    while day <= end:
        if is_business_day(day):
            out.append(day)
        day += dt.timedelta(days=1)
    return out


def latest_business_day(today: dt.date) -> dt.date:
    """The most recent Mon-Fri on or before `today`.  Saturday -> Friday."""
    d = today
    while not is_business_day(d):
        d -= dt.timedelta(days=1)
    return d


def week_of(anchor: dt.date, length: int = WEEK_LENGTH) -> list[dt.date]:
    """The business days of `anchor`'s week, up to and including `anchor`.

    Run on Friday evening this is Monday-Friday.  Run on a Wednesday it is
    Monday-Wednesday rather than a week padded with days that have not happened
    yet.  Run on a weekend it is the whole of the week that just closed.
    """
    if not is_business_day(anchor):
        anchor -= dt.timedelta(days=anchor.weekday() - 4)
    monday = anchor - dt.timedelta(days=anchor.weekday())
    start = monday - dt.timedelta(days=max(0, length - 5))
    return business_days(start, anchor)


def resolve_dates(
    *,
    anchor: dt.date | None = None,
    date_from: dt.date | None = None,
    date_to: dt.date | None = None,
    length: int = WEEK_LENGTH,
) -> list[dt.date]:
    """`--from/--to` when given, otherwise the week ending at `anchor`."""
    if date_from or date_to:
        start = date_from or date_to
        end = date_to or date_from
        return business_days(start, end)
    if anchor is None:
        raise ValueError("resolve_dates needs an anchor or a from/to range")
    return week_of(anchor, length)


# --------------------------------------------------------------------------- #
# Which tape                                                                   #
# --------------------------------------------------------------------------- #

#: What to do about the **live day** -- the most recent business day on or before
#: today.  It is the only day whose source is ever in question: it may or may not
#: have been written down to the HDB yet.  Every earlier day comes off the HDB.
#:
#:   auto  -- the RT tapes, falling back to the HDB once the day has been
#:            transferred.  Default.  Run on a Thursday: Thursday off RT,
#:            Monday-Wednesday off the HDB.  Run on Friday evening: Friday off
#:            RT, Monday-Thursday off the HDB.  Run after the transfer:
#:            everything off the HDB.
#:   force -- RT only for the live day, no fallback, so a rolled tape shows up as
#:            a missing day instead of being quietly served from the HDB.
#:   off   -- HDB only.  The live day is simply absent until write-down.
RT_TODAY_POLICIES = ("auto", "force", "off")


def sources_for_day(
    date: dt.date,
    today: dt.date | None,
    *,
    rt_available: bool,
    policy: str = "auto",
) -> tuple[str, ...]:
    """Which tapes to read a day from, in the order to try them.

    Only the **live day** is ever in question -- the most recent business day on
    or before today.  Run on Thursday that is Thursday; run on Friday evening it
    is Friday; run on the Saturday or Sunday after, it is still Friday, because
    the RT tapes have not rolled into a new session yet.

    Anything older is written down in the HDB by definition, and reading it off a
    real-time tape would be reading whatever that tape holds *now*, stamped with
    a date it does not belong to.  That is why this is a whitelist and not a
    fallback chain: by the Monday, `--from`/`--to` over last week must go nowhere
    near RT.

    The live day itself is read from the tapes first, because that is where it is
    while the session is still the current one, and from the HDB second, for once
    it has been transferred.  Whichever answers first wins, so the same command
    works on either side of the write-down.
    """
    if today is None or not rt_available or policy == "off":
        return ("ht",)
    if date != latest_business_day(today):
        return ("ht",)
    if policy == "force":
        return ("rt",)
    return ("rt", "ht")


# --------------------------------------------------------------------------- #
# The row-level date guard                                                     #
# --------------------------------------------------------------------------- #

def clip_to_date(df: pd.DataFrame, date: dt.date | None) -> tuple[pd.DataFrame, int]:
    """Drop rows belonging to another date.  Returns (frame, rows dropped).

    Only ever needed on a **non-partitioned** instance: `kdbio.where_date` emits
    no date predicate there, so the server hands back whatever the tape holds.
    That is the intended behaviour intraday -- the RT tape *is* today -- but a
    weekly run reads one specific day from those tapes and has to be sure it got
    that day and nothing else.

    It is also what makes the RT-to-HDB handover work: a tape that has already
    rolled comes back empty here, and the empty result is what sends the day to
    the HDB instead.

    A tape with no `date` column at all cannot be clipped; the caller reports
    that rather than assuming it was clean.
    """
    if df is None or df.empty or date is None or "date" not in df.columns:
        return df, 0
    try:
        seen = pd.to_datetime(df["date"], errors="coerce").dt.date
    except (TypeError, ValueError, AttributeError):  # pragma: no cover
        return df, 0
    if seen.isna().all():
        return df, 0
    keep = seen == date
    dropped = int((~keep).sum())
    if not dropped:
        return df, 0
    return df[keep].reset_index(drop=True), dropped

"""What v2 measures, and the windows it measures it over.

Session times, instance wiring and the CAS calendar all come from
`casretro.config` -- this module only adds what v2 needs on top, so there is one
definition of 17:45 in the repo and not two.

Everything is HKT, like the raw `time` columns.  IST = HKT - 02:30.
"""

from __future__ import annotations

from casretro.config import T

# --------------------------------------------------------------------------- #
# Market windows                                                               #
# --------------------------------------------------------------------------- #

#: "Close volume" is everything the market printed from here to the end of the
#: day.  17:50 HKT / 15:20 IST -- the instant auction order entry opens, so the
#: window is the whole close: the last continuous prints, the auction itself and
#: the post-close session.
CLOSE_VOLUME_FROM = T("17:50")

#: End of the close-volume window.  End of day rather than 18:30, so a late
#: print cannot fall silently outside it.
DAY_END = T("23:59:59.999")

#: "Close price" is the **first** print in this window.  The auction freezes at a
#: random instant inside it and the close is struck there, so the first print
#: after 17:58 is the auction price.  Same window `casStudy` uses, deliberately:
#: two reports quoting different closing prices for the same day is a bug.
CLOSE_PRICE_WINDOW = (T("17:58"), T("18:00"))


# --------------------------------------------------------------------------- #
# Population                                                                   #
# --------------------------------------------------------------------------- #

#: Every number in v2 is about the closing auction, so the child orders counted
#: are the ones whose venue says CLOSE.  Continuous child orders are out of
#: scope -- not filtered late, never loaded into the measure.
CLOSE_VENUE_ONLY = True

# --------------------------------------------------------------------------- #
# Which close child orders count                                               #
# --------------------------------------------------------------------------- #
#
# These four predicates are the desk's own definition, taken from `temp.q` and
# applied server-side so the frame that arrives is already the population being
# measured.  They matter more than anything else on the page, because they set
# the denominator of every fill rate:
#
#   venue like "*CLOSE*"    the auction, not continuous
#   make > 0                the order traded something.  A close child order that
#                           filled nothing is not a bad fill, it is an order that
#                           never competed -- counting it drags the rate toward
#                           zero and says nothing about execution.
#   make <= size            a child order cannot fill more than it asked for;
#                           a row that says otherwise is bad data, not a 300% fill
#   t_off_market > 17:58    it was still live when the auction could freeze
#
# ...plus the marketability test below, for limit orders only.

#: A close child order counts only if it left the market after this.  17:58 HKT
#: is the random-close start: before it, an order that has already gone is an
#: order that was not in the auction.
OFF_MARKET_AFTER = T("17:58")

#: For a **limit** order, the limit has to have been at or through the price that
#: was actually achieved -- buy limit at or above it, sell limit at or below it.
#: A limit that sits the wrong side of its own average fill price is a row whose
#: `price` and `avg_fill_price` do not describe the same thing, and it would
#: otherwise land in the denominator as a fill that never could have happened.
#: Market orders are exempt: they have no limit to test.
LIMIT_MUST_BE_MARKETABLE = True

#: The q symbol identifying a limit order in `workorder.otype`.  The
#: marketability test keys off this exact value, as `temp.q` does; every other
#: `otype` is treated as marketable by definition.
QTYPE_LIMIT = "limit"

#: Order type buckets, in the order they appear on the page.
OTYPE_MARKET = "MARKET"
OTYPE_LIMIT = "LIMIT"
OTYPES = (OTYPE_MARKET, OTYPE_LIMIT)


# --------------------------------------------------------------------------- #
# Client tables                                                                #
# --------------------------------------------------------------------------- #

#: What "client" means on the top-N tables.  `basket` is also what the SILK /
#: agency split is derived from, so a basket belongs to exactly one flow.
CLIENT_COLUMN = "basket"

#: How many clients each flow's table shows.
TOP_CLIENTS = 5


# --------------------------------------------------------------------------- #
# FX                                                                           #
# --------------------------------------------------------------------------- #

#: `equity.fx_last` carries a rate whose direction the schema does not document.
#: For a currency far from parity the two possible quotes are reciprocals on
#: opposite sides of 1 -- INR is either ~85 (local per USD) or ~0.0117 (USD per
#: local) -- so the direction can be read off the magnitude and the answer is the
#: same either way.  `--fx` forces it when a currency sits near parity, where
#: this would genuinely be ambiguous.
FX_AUTO = "auto"
FX_DIVIDE = "divide"      # local / fx_last  -- fx_last is local units per USD
FX_MULTIPLY = "multiply"  # local * fx_last  -- fx_last is USD per local unit
FX_CONVENTIONS = (FX_AUTO, FX_DIVIDE, FX_MULTIPLY)

#: Currency the page reports in.
REPORT_CCY = "USD"

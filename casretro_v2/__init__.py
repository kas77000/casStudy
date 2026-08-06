"""CAS India closing-auction execution review -- the trader / client report.

A second reader, not a second pipeline.  `casretro` stays the desk's full
retrospective; this package answers the three questions the trading floor asked
for and stops:

    Execution Quality  per day, per order type, what we sent to the auction
                       against what traded -- and both in USD
    Flows              day x flow x order type, against the market's own close
                       volume and notional in the same names
    Top clients        the biggest baskets of each flow, by notional traded

It reuses `casretro` for everything already solved -- connections, the session
calendar, the universe, the order and execution loaders, the classification of a
child order, business-day resolution and the HT/RT split -- and adds only what is
genuinely new: the market's close windows, the per-day FX rate, the three
measures and the page.

Entry point: `python -m casretro_v2 --help`.
"""

__version__ = "2.0.0"

__all__ = ["config", "fx", "loaders", "build", "metrics", "period", "report", "cli"]

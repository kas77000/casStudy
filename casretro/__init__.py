"""CAS India execution retrospective.

Reads the algo kdb+ stack through pykx and answers, for one trading day or for a
whole week:

* which CAS-eligible Indian parent orders traded in the closing auction,
* which ones did not and *why*,
* what got rejected, split between continuous trading and the CAS window,
* how our volume sat against the desk's benchmark closing-bin shares.

Two readers, two pages: `report.write_html` is the desk's own worksheet, every
section and every column; `trader.write_trader_html` is the version that leaves
the desk.  `weekly` folds days into the Friday review.

Entry point: `python -m casretro --help`.
"""

__version__ = "1.1.0"

__all__ = ["config", "kdbio", "universe", "loaders", "sessions", "classify",
           "metrics", "build", "weekly", "report", "trader", "cli"]

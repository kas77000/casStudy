"""CAS India execution retrospective.

Reads the algo kdb+ stack through pykx and answers, for one trading day:

* which CAS-eligible Indian parent orders traded in the closing auction,
* which ones did not and *why*,
* what got rejected, split between continuous trading and the CAS window,
* how our volume sat against the desk's benchmark closing-bin shares.

This is the desk's own worksheet: every section, every column, every row, and the
reconciliation checks that say when a number is suspect.

The weekly review, and the version that leaves the desk for a trader or a
client, is its own package -- `casretro_v2` -- which reads this one's loaders and
owns the period logic.

Entry point: `python -m casretro --help`.
"""

__version__ = "1.2.0"

__all__ = ["config", "kdbio", "universe", "loaders", "sessions", "classify",
           "metrics", "build", "report", "cli"]

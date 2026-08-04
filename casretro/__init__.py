"""CAS India execution retrospective.

Reads the algo kdb+ stack through pykx and answers, for one trading day:

* which CAS-eligible Indian parent orders traded in the closing auction,
* which ones did not and *why*,
* what got rejected, split between continuous trading and the CAS window,
* how our volume sat against the desk's benchmark closing-bin shares.

Entry point: `python -m casretro --help`.
"""

__version__ = "1.0.0"

__all__ = ["config", "kdbio", "universe", "loaders", "sessions", "classify",
           "metrics", "build", "report", "cli"]

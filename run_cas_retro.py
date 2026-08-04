#!/usr/bin/env python3
"""Convenience launcher -- identical to `python -m casretro`."""

import sys

from casretro.cli import main

if __name__ == "__main__":
    sys.exit(main())

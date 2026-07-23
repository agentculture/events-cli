"""Entry point for ``python -m events``."""

from __future__ import annotations

import sys

from events.cli import main

if __name__ == "__main__":
    sys.exit(main())

"""Entry point for ``python -m events_cli``."""

from __future__ import annotations

import sys

from events_cli.cli import main

if __name__ == "__main__":
    sys.exit(main())

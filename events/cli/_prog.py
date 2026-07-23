"""Resolve the command name to name back at the user (stable-contract).

Remediation hints tell an agent what to *run next*, so they must name a command
that actually exists in the mode the caller is already using. Two modes ship:

* installed — the ``events`` console script (``[project.scripts]``);
* from a checkout — ``python -m events``, the documented no-install fallback
  (the runtime package has no third-party dependencies, so it runs straight from
  source).

Hard-coding either one makes the hint wrong in the other mode, and argparse's
own default would derive ``__main__.py`` from ``sys.argv[0]`` in module mode,
which is wrong in both. So detect it.
"""

from __future__ import annotations

import sys
from pathlib import Path

_CONSOLE_SCRIPT = "events"
_MODULE_INVOCATION = "python -m events"

# This package's own __main__.py: events/cli/_prog.py -> events/__main__.py.
# Resolved from __file__ rather than imported, because events/__main__.py
# imports events.cli, which imports this module.
_PACKAGE_MAIN = Path(__file__).resolve().parent.parent / "__main__.py"


def prog_name() -> str:
    """Return the command the caller actually typed."""
    # `python -m events` sets argv[0] to the full path of this package's
    # __main__.py; the console script sets it to the script path. Compare the
    # whole path, not the basename — `python -m pytest` (and every other -m
    # invocation) also ends in __main__.py.
    argv0 = sys.argv[0] if sys.argv else ""
    if argv0 and Path(argv0).resolve() == _PACKAGE_MAIN:
        return _MODULE_INVOCATION
    return _CONSOLE_SCRIPT

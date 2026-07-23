"""Explain catalog — markdown keyed by command-path tuples (stable-contract).

Every noun/verb registered in the CLI should have a catalog entry.
"""

from __future__ import annotations

from events.cli._errors import EXIT_USER_ERROR, CliError
from events.cli._prog import prog_name
from events.explain.catalog import ENTRIES


def resolve(path: tuple[str, ...]) -> str:
    if path in ENTRIES:
        return ENTRIES[path]
    display = " ".join(path) if path else "<root>"
    raise CliError(
        code=EXIT_USER_ERROR,
        message=f"no explain entry for: {display}",
        # Name the invocation the caller is already using, not a command that
        # may not be on their PATH.
        remediation=f"list entries with: {prog_name()} explain events",
    )


def known_paths() -> list[tuple[str, ...]]:
    return list(ENTRIES.keys())

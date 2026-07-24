"""``events list`` — the most recent captured events, optionally by type.

Translation layer only, over :meth:`~events_cli.history.HistoryStore.list`: a
pure, dockerless read across every subscription's log, newest first, reporting
one event once even when several subscriptions captured it (see that method's
docstring). Nothing here decides how an event got into the store — that is
the drain's job (:mod:`events_cli.subs.drain`, surfaced as ``events watch``) —
this verb only reads back whatever was captured.

**No paho import.** This module never imports :mod:`events_cli.client`, so
``events list`` runs on a machine with no MQTT client installed at all — it
only reads a file. ``tests/test_cli_list.py`` proves this for both ``list``
and ``get`` by running them in a subprocess with ``paho`` blocked.

Naming: the module is ``list.py`` (that is the verb), but nothing at module
scope is ever bound to the bare name ``list`` — the command function is
``cmd_list``, never ``list``, and every call into the domain layer reads
``open_store().list(...)``, an attribute access rather than a name binding, so
the builtin is never shadowed. This mirrors ``events_cli/history/__init__.py``,
where the *module-level* convenience function is ``list_events`` for exactly
this reason while the *method* on :class:`~events_cli.history.HistoryStore`
stays ``list`` (no shadowing risk on a method, since it is never looked up
bare). A command module has the same shadowing risk a plain module does, so it
follows the same rule: keep ``list`` off the module's own namespace.

Exit codes
----------
``--max`` is bounds-checked here, before any read, the same way
``events_cli/cli/_commands/watch.py`` checks its bounds first: a bad ``--max``
must be an unambiguous user error (exit 1) rather than however the store
happens to report it. A damaged store record surfacing during the scan
(:class:`~events_cli.history.HistoryCorruptError` /
:class:`~events_cli.history.HistoryFormatError`) is an environment fault
(exit 2).
"""

from __future__ import annotations

import argparse

from events_cli.cli._errors import EXIT_ENV_ERROR, EXIT_USER_ERROR, CliError
from events_cli.cli._output import emit_result
from events_cli.history import DEFAULT_MAX, HistoryError, open_store

__all__ = ["cmd_list", "register"]


def _emit(payload: dict[str, object], lines: list[str], *, json_mode: bool) -> None:
    if json_mode:
        emit_result(payload, json_mode=True)
    else:
        emit_result("\n".join(lines), json_mode=False)


def _check_max(max_total: int) -> None:
    if max_total < 1:
        raise CliError(
            code=EXIT_USER_ERROR,
            message=f"--max must be at least 1, got {max_total}",
            remediation="pass a positive --max; there is no unbounded list",
        )


def cmd_list(args: argparse.Namespace) -> int:
    json_mode = bool(args.json)
    max_total = int(args.max)
    event_type = args.type

    _check_max(max_total)

    try:
        records = open_store().list(event_type, max_total)
    except HistoryError as exc:
        raise CliError(code=EXIT_ENV_ERROR, message=str(exc), remediation=exc.remediation) from exc

    payload: dict[str, object] = {"events": [record.to_dict() for record in records]}
    if records:
        lines = [
            f"{record.envelope.type}  id={record.envelope.id}  "
            f"seq={record.seq}  sub={record.subscription}"
            for record in records
        ]
    else:
        lines = ["(no captured events)"]
        if event_type is not None:
            lines.append(f"nothing captured yet of type {event_type!r}")
    _emit(payload, lines, json_mode=json_mode)
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "list",
        help="List the most recently captured events, optionally filtered by type.",
    )
    p.add_argument(
        "--type",
        dest="type",
        metavar="TYPE",
        default=None,
        help="Only events of this dotted event type, e.g. 'task.requested'.",
    )
    p.add_argument(
        "--max",
        type=int,
        default=DEFAULT_MAX,
        metavar="N",
        help=f"Maximum events to return, newest first (default: {DEFAULT_MAX}). There is no "
        "unbounded list.",
    )
    p.add_argument("--json", action="store_true", help="Emit structured JSON.")
    p.set_defaults(func=cmd_list)

"""``events get`` — read one captured event back from the history store.

Translation layer only, over :meth:`~events_cli.history.HistoryStore.get`: a
pure, dockerless read across every subscription's log (see that method's
docstring). Nothing here decides how an event got into the store — that is
the drain's job (:mod:`events_cli.subs.drain`, surfaced as ``events watch``) —
this verb only looks a single event up by id and renders whatever
:class:`~events_cli.history.HistoryRecord` comes back.

**No paho import.** This module never imports :mod:`events_cli.client`, so
``events get`` runs on a machine with no MQTT client installed at all — it
only reads a file. ``tests/test_cli_list.py`` proves this for both ``get`` and
``list`` by running them in a subprocess with ``paho`` blocked from importing.

Exit codes
----------
A missing id is a **user** error (exit 1): the caller asked for an id nothing
ever captured, the same shape as ``events sub show`` on an unknown name
(``events_cli/cli/_commands/sub.py``). A damaged store record
(:class:`~events_cli.history.HistoryCorruptError` /
:class:`~events_cli.history.HistoryFormatError`) is an **environment** fault
(exit 2): nothing the caller typed caused it, and no retry with a different id
fixes it.
"""

from __future__ import annotations

import argparse

from events_cli.cli._errors import EXIT_ENV_ERROR, EXIT_USER_ERROR, CliError
from events_cli.cli._output import emit_result
from events_cli.cli._prog import prog_name
from events_cli.history import HistoryError, open_store

__all__ = ["cmd_get", "register"]


def _emit(payload: dict[str, object], lines: list[str], *, json_mode: bool) -> None:
    if json_mode:
        emit_result(payload, json_mode=True)
    else:
        emit_result("\n".join(lines), json_mode=False)


def cmd_get(args: argparse.Namespace) -> int:
    json_mode = bool(args.json)

    try:
        record = open_store().get(args.id)
    except HistoryError as exc:
        # get() never raises InvalidSubscriptionError (it takes no subscription
        # name) — every HistoryError reaching here is the store itself being
        # unreadable, an environment fault.
        raise CliError(code=EXIT_ENV_ERROR, message=str(exc), remediation=exc.remediation) from exc

    if record is None:
        raise CliError(
            code=EXIT_USER_ERROR,
            message=f"no captured event with id {args.id!r}",
            remediation=f"list recently captured events with '{prog_name()} list'",
        )

    envelope = record.envelope
    lines = [
        f"event {envelope.type}  id={envelope.id}",
        f"  subscription: {record.subscription}",
        f"  seq:          {record.seq}",
        f"  recordedAt:   {record.recorded_at}",
        f"  time:         {envelope.time}",
        f"  source:       {envelope.source}",
    ]
    _emit(record.to_dict(), lines, json_mode=json_mode)
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "get",
        help="Read one captured event back from the history store, by id.",
    )
    p.add_argument("id", help="The event's 'id' field (see 'events emit' / 'events list').")
    p.add_argument("--json", action="store_true", help="Emit structured JSON.")
    p.set_defaults(func=cmd_get)

"""``events sub`` — manage durable subscriptions (registry record + broker session).

A durable subscription is two things kept in step by :mod:`events_cli.subs`: a
**registry record** on disk (name, pattern, owner, client id) and an **MQTT
persistent session** in the broker addressed by that client id. This module is
only the translation layer over that package: it turns
:class:`~events_cli.subs.errors.SubsError` into a
:class:`~events_cli.cli._errors.CliError` with an exit code and a hint, and
turns a :class:`~events_cli.subs.SubscriptionRecord` into stdout. All the
decisions about *when* a session is created, resumed or destroyed live in
:mod:`events_cli.subs`; nothing here talks to a registry file or a broker
socket directly.

Exit codes used here
---------------------
Mirrors the table in ``events_cli/subs/errors.py``: a bad name/pattern, a
duplicate name, or an unknown name are **user** errors (exit 1) — the caller
typed something that was never going to work. Everything else a
:class:`~events_cli.subs.errors.SubsError` can mean (a damaged registry record,
a broker that refused or never answered, an unreadable format) is an
**environment** fault (exit 2) — the invocation was fine, the host is not in a
state where it can succeed. ``_translate`` below implements exactly that split,
defaulting unrecognised subclasses to exit 2 the same way
``events_cli/subs/errors.py`` and ``events_cli/history/errors.py`` do (most of
each hierarchy is environment-shaped).

``sub remove --force``
-----------------------
:func:`~events_cli.subs.remove_subscription` refuses to drop the registry
record when the broker session could not be destroyed, *unless* ``force=True``
— otherwise a broker that is down forever would leave a subscription
un-removable. ``--force`` is that escape hatch, surfaced here because
otherwise the capability domain provides is unreachable from the CLI. The help
text says what it actually risks: a live broker-side session (and anything
still queued for it) is left behind with nothing on disk pointing at it once
the record is gone.
"""

from __future__ import annotations

import argparse

from events_cli.cli._errors import EXIT_ENV_ERROR, EXIT_USER_ERROR, CliError
from events_cli.cli._output import emit_result
from events_cli.cli._prog import prog_name
from events_cli.subs import (
    DuplicateSubscriptionError,
    SubscriptionRecord,
    SubscriptionValidationError,
    SubsError,
    UnknownSubscriptionError,
    add_subscription,
    get_subscription,
    list_subscriptions,
    remove_subscription,
)

# Every one of these names a mistake the caller made (a bad name/pattern, a
# name already registered, a name nothing registered) — exit 1. Every other
# SubsError (RegistryCorruptError, RegistryFormatError, SessionError,
# BrokerUnreachableError, ...) is the environment not being in a state where
# the request could succeed — exit 2. See events_cli/subs/errors.py's table.
_USER_ERROR_TYPES = (
    SubscriptionValidationError,
    DuplicateSubscriptionError,
    UnknownSubscriptionError,
)


def _translate(exc: SubsError) -> CliError:
    code = EXIT_USER_ERROR if isinstance(exc, _USER_ERROR_TYPES) else EXIT_ENV_ERROR
    return CliError(code=code, message=str(exc), remediation=exc.remediation)


def _emit(payload: dict[str, object], lines: list[str], *, json_mode: bool) -> None:
    if json_mode:
        emit_result(payload, json_mode=True)
    else:
        emit_result("\n".join(lines), json_mode=False)


def _record_lines(record: SubscriptionRecord) -> list[str]:
    return [
        f"  pattern:   {record.pattern}",
        f"  filter:    {record.topic_filter}",
        f"  owner:     {record.owner}",
        f"  clientId:  {record.client_id}",
        f"  created:   {record.created}",
    ]


# --- add ---------------------------------------------------------------


def cmd_sub_add(args: argparse.Namespace) -> int:
    try:
        record = add_subscription(args.name, args.pattern, owner=args.owner)
    except SubsError as exc:
        raise _translate(exc) from exc

    lines = [
        f"registered subscription {record.name!r}",
        *_record_lines(record),
        "",
        f"next: {prog_name()} watch {record.name}",
    ]
    _emit(record.to_dict(), lines, json_mode=bool(args.json))
    return 0


# --- list --------------------------------------------------------------


def cmd_sub_list(args: argparse.Namespace) -> int:
    try:
        records = list_subscriptions()
    except SubsError as exc:
        raise _translate(exc) from exc

    payload: dict[str, object] = {"subscriptions": [r.to_dict() for r in records]}
    if records:
        lines = [f"{r.name}  pattern={r.pattern}  owner={r.owner}" for r in records]
    else:
        lines = [
            "(no subscriptions registered)",
            f"register one with: {prog_name()} sub add <name> <pattern>",
        ]
    _emit(payload, lines, json_mode=bool(args.json))
    return 0


# --- show ----------------------------------------------------------------


def cmd_sub_show(args: argparse.Namespace) -> int:
    try:
        record = get_subscription(args.name)
    except SubsError as exc:
        raise _translate(exc) from exc

    if record is None:
        raise CliError(
            code=EXIT_USER_ERROR,
            message=f"no subscription named {args.name!r}",
            remediation=f"list what is registered with '{prog_name()} sub list'",
        )

    lines = [f"subscription {record.name!r}", *_record_lines(record)]
    _emit(record.to_dict(), lines, json_mode=bool(args.json))
    return 0


# --- remove --------------------------------------------------------------


def cmd_sub_remove(args: argparse.Namespace) -> int:
    try:
        record = remove_subscription(args.name, force=bool(args.force))
    except SubsError as exc:
        raise _translate(exc) from exc

    lines = [f"removed subscription {record.name!r}", *_record_lines(record)]
    if bool(args.force):
        lines.append(
            "  --force was set: if the broker session could not be destroyed, it (and "
            "anything still queued for it) is now unreachable through this registry"
        )
    _emit(record.to_dict(), lines, json_mode=bool(args.json))
    return 0


# --- registration ----------------------------------------------------------


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "sub",
        help="Manage durable subscriptions (a registry record plus a broker session).",
    )
    p.add_argument("--json", action="store_true", help="Emit structured JSON.")
    # `events sub` with no sub-verb lists what is registered — the most useful
    # default, mirroring `events cli` aliasing to `cli overview`.
    p.set_defaults(func=cmd_sub_list, json=False)
    # Propagate the structured-error parser class so `sub <verb> --bogus` and
    # `sub <unknown-verb>` route through the CliError contract too.
    noun_sub = p.add_subparsers(dest="sub_command", parser_class=type(p))

    add = noun_sub.add_parser(
        "add",
        help="Register a subscription: create the broker session, then the record.",
    )
    add.add_argument(
        "name",
        help="Subscription name. Becomes a registry filename and an MQTT client id.",
    )
    add.add_argument("pattern", help="Dotted event-type pattern, e.g. 'task.*'.")
    add.add_argument(
        "--owner",
        metavar="NAME",
        help="Owner identity to record. Defaults to this agent's culture.yaml nick.",
    )
    add.add_argument("--json", action="store_true", help="Emit structured JSON.")
    add.set_defaults(func=cmd_sub_add)

    lst = noun_sub.add_parser("list", help="List every registered subscription.")
    lst.add_argument("--json", action="store_true", help="Emit structured JSON.")
    lst.set_defaults(func=cmd_sub_list)

    show = noun_sub.add_parser("show", help="Show one subscription's record.")
    show.add_argument("name", help="Subscription name.")
    show.add_argument("--json", action="store_true", help="Emit structured JSON.")
    show.set_defaults(func=cmd_sub_show)

    remove = noun_sub.add_parser(
        "remove",
        help="Destroy a subscription: end the broker session, then drop the record.",
    )
    remove.add_argument("name", help="Subscription name.")
    remove.add_argument(
        "--force",
        action="store_true",
        help=(
            "Drop the registry record even if the broker session could not be destroyed "
            "(e.g. the broker is down). Risk: a live persistent session — and anything "
            "still queued for it — is left behind in the broker with nothing on disk "
            "pointing at it any more; use only when the broker is gone for good."
        ),
    )
    remove.add_argument("--json", action="store_true", help="Emit structured JSON.")
    remove.set_defaults(func=cmd_sub_remove)

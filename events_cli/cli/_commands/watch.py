"""``events watch`` — the bounded cursor drain over a durable subscription.

Translation layer only, over two domain seams: :mod:`events_cli.history` (a
pure, dockerless read from the store) and :mod:`events_cli.subs.drain` (the
broker-side drain that persists each event before acknowledging it — see its
module docstring). Nothing here decides *how* an event gets from the broker
into the store; it only decides which of the two reads to do, in which order,
and how to render whichever combination comes back.

The ``--since`` composition decision
-------------------------------------
:func:`~events_cli.subs.drain.drain_subscription`'s own docstring is explicit
that it is "the broker side only", and that composing it with the store is
this task's job. The composition implemented here:

1. **Replay persisted history first.** ``HistoryStore.read(name, since, max)``
   is a pure, bounded disk read — no broker connection, no network, and it
   succeeds even if the broker is down or the subscription was since removed.
   This satisfies the constraint that replaying already-persisted history must
   not require a broker connection: if this alone returns a full ``max``
   batch, :func:`drain_subscription` is **never called** and no session is
   opened at all.
2. **Drain the broker only for what is left.** If the history read came back
   short of ``max`` (either the store is genuinely behind, or ``since`` is
   already caught up), the remaining budget — ``max`` minus what history
   already returned — is drained from the broker, floored at the cursor the
   history read ended on. Passing that cursor forward (rather than the
   original ``since``) is what stops a drain from ever returning an event the
   caller already saw: the store's cursor only advances, so anything at or
   below it is exactly what history already replayed.
3. **The two batches concatenate, oldest first.** History records precede
   whatever the broker drain adds, because that is delivery order for a
   caller resuming from a cursor: what was already durable, then what just
   arrived.

Why this order and not the reverse (drain first, then backfill from history)?
Draining first would force a broker connection on every ``watch`` call even
when the caller only wanted to catch up on a backlog the store already has —
exactly the connection the constraint says must not be required. It would also
complicate the bound: a drain that already used up ``max`` on live traffic
would have to *retroactively* make room for older, already-persisted records,
which is a strictly worse ordering for a caller resuming a backlog. Read-then-
drain never wastes a broker round trip when the store alone can answer, and
degrades to "broker only" automatically once the store is caught up (the
common case for a caller that watches often).

No ``--follow``
----------------
Deliberately absent. An unbounded stream would hang whatever is reading it,
which for an agent means a hung turn; ``--max`` and ``--timeout`` (both with
finite defaults) are the only way to get more, and the caller polls again with
the returned ``cursor``. ``tests/test_cli_watch.py`` pins the flag's absence so
nobody adds it back without deciding to.
"""

from __future__ import annotations

import argparse
import math

from events_cli.cli._errors import EXIT_ENV_ERROR, EXIT_USER_ERROR, CliError
from events_cli.cli._output import emit_result
from events_cli.cli._prog import prog_name
from events_cli.history import HistoryError, InvalidSubscriptionError, open_store
from events_cli.subs import SubscriptionValidationError, SubsError, UnknownSubscriptionError
from events_cli.subs.drain import (
    DEFAULT_DRAIN_MAX,
    DEFAULT_DRAIN_TIMEOUT,
    STOPPED_MAX,
    drain_subscription,
)

# Same split as events_cli/cli/_commands/sub.py, extended with the one history
# error the CLI actually needs to distinguish: InvalidSubscriptionError (a bad
# name) is a user error; everything else HistoryError/SubsError can mean
# (a damaged store, a broker refusal) is an environment fault.
_USER_ERROR_TYPES = (
    SubscriptionValidationError,
    UnknownSubscriptionError,
    InvalidSubscriptionError,
)


def _translate(exc: Exception) -> CliError:
    code = EXIT_USER_ERROR if isinstance(exc, _USER_ERROR_TYPES) else EXIT_ENV_ERROR
    return CliError(code=code, message=str(exc), remediation=getattr(exc, "remediation", ""))


def _check_bounds(since: int, max_total: int, timeout: float) -> None:
    """Reject an unusable bound before anything is read or drained.

    :meth:`~events_cli.history.HistoryStore.read`'s own bound check raises the
    *base* :class:`~events_cli.history.HistoryError` for a bad ``max`` or
    ``since`` (see ``_require_max`` / ``_require_since`` in
    ``events_cli/history/__init__.py``) rather than the exit-1-mapped
    :class:`~events_cli.history.InvalidSubscriptionError` — so letting it
    discover the problem would misreport a bad flag as an environment fault.
    Checked here first so a bad bound is unambiguously exit 1 regardless of
    which backend would eventually have caught it, and so it costs neither a
    disk read nor a broker connection.
    """
    if max_total < 1:
        raise CliError(
            code=EXIT_USER_ERROR,
            message=f"--max must be at least 1, got {max_total}",
            remediation="pass a positive --max; there is no unbounded watch",
        )
    if since < 0:
        raise CliError(
            code=EXIT_USER_ERROR,
            message=f"--since must be a non-negative cursor, got {since}",
            remediation=(
                "pass 0 to read from the beginning, or the cursor a previous "
                f"'{prog_name()} watch' returned"
            ),
        )
    if not math.isfinite(timeout) or timeout <= 0:
        raise CliError(
            code=EXIT_USER_ERROR,
            message=f"--timeout must be a positive, finite number of seconds, got {timeout}",
            remediation="pass a positive --timeout in seconds",
        )


def cmd_watch(args: argparse.Namespace) -> int:
    name = args.name
    since = int(args.since)
    max_total = int(args.max)
    timeout = float(args.timeout)
    json_mode = bool(args.json)

    _check_bounds(since, max_total, timeout)

    try:
        history_page = open_store().read(name, since, max_total)
    except HistoryError as exc:
        raise _translate(exc) from exc

    records = list(history_page.records)
    cursor = history_page.cursor
    remaining = max_total - len(records)

    if remaining > 0:
        try:
            drain_result = drain_subscription(name, since=cursor, max=remaining, timeout=timeout)
        except SubsError as exc:
            raise _translate(exc) from exc
        records.extend(drain_result.records)
        cursor = drain_result.cursor
        has_more = drain_result.has_more
        session_present = drain_result.session_present
        consumed = drain_result.consumed
        skipped = drain_result.skipped
        stopped = drain_result.stopped
        served_from = "history+broker" if history_page.records else "broker"
    else:
        # History alone filled the quota: the broker was never touched, so
        # there is nothing to report about a session that was never opened.
        has_more = history_page.has_more
        session_present = None
        consumed = 0
        skipped = ()
        stopped = STOPPED_MAX
        served_from = "history"

    payload: dict[str, object] = {
        "subscription": name,
        "records": [record.to_dict() for record in records],
        "cursor": cursor,
        "hasMore": has_more,
        "servedFrom": served_from,
        "sessionPresent": session_present,
        "consumed": consumed,
        "skipped": [s.to_dict() for s in skipped],
        "stopped": stopped,
    }
    _render(payload, json_mode=json_mode)
    return 0


def _render(payload: dict[str, object], *, json_mode: bool) -> None:
    """One renderer for the batch, whichever source(s) it came from.

    :class:`~events_cli.subs.drain.DrainResult` and
    :class:`~events_cli.history.HistoryPage` already spell ``records`` /
    ``cursor`` / ``hasMore`` identically (see both classes' ``to_dict``
    docstrings); :func:`cmd_watch` normalises history-only, broker-only and
    combined results into the one dict shape above before it ever reaches
    here, so this is the only place that turns a batch into text — not one
    renderer per source.
    """
    if json_mode:
        emit_result(payload, json_mode=True)
        return

    records = payload["records"]
    assert isinstance(records, list)
    lines = [
        f"watch {payload['subscription']}: {len(records)} event(s)  "
        f"servedFrom={payload['servedFrom']}  stopped={payload['stopped']}",
        f"  cursor:  {payload['cursor']}",
        f"  hasMore: {payload['hasMore']}",
    ]
    if payload["sessionPresent"] is not None:
        lines.append(f"  session: present={payload['sessionPresent']}")
    skipped = payload["skipped"]
    assert isinstance(skipped, list)
    if skipped:
        lines.append(f"  skipped: {len(skipped)} unreadable payload(s) on the broker")
    for record in records:
        event = record["event"]
        lines.append(f"  [{record['seq']}] {event['type']}  id={event['id']}")
    if not records:
        lines.append("  (no events)")
    lines.append("")
    lines.append(f"next: {prog_name()} watch {payload['subscription']} --since {payload['cursor']}")
    emit_result("\n".join(lines), json_mode=False)


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "watch",
        help="Bounded cursor drain over a durable subscription: history replay, then broker.",
    )
    p.add_argument("name", help="Subscription name (see 'events sub list').")
    p.add_argument(
        "--since",
        type=int,
        default=0,
        metavar="CURSOR",
        help="Cursor to resume from (default: 0, the beginning). Pass back the 'cursor' a "
        "previous watch returned.",
    )
    p.add_argument(
        "--max",
        type=int,
        default=DEFAULT_DRAIN_MAX,
        metavar="N",
        help=f"Maximum events to return (default: {DEFAULT_DRAIN_MAX}). There is no "
        "unbounded watch.",
    )
    p.add_argument(
        "--timeout",
        type=int,
        default=int(DEFAULT_DRAIN_TIMEOUT),
        metavar="SECONDS",
        help="Seconds to wait on the broker once persisted history is exhausted (default: "
        f"{int(DEFAULT_DRAIN_TIMEOUT)}). There is deliberately no --follow: an unbounded "
        "stream hangs an agent turn.",
    )
    p.add_argument("--json", action="store_true", help="Emit structured JSON.")
    p.set_defaults(func=cmd_watch)

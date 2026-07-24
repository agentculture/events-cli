"""The bounded drain: resume a session, persist before acknowledging, return a cursor.

This is the consume half of a durable subscription. :mod:`events_cli.subs`
created the persistent session and the registry record; :mod:`events_cli.history`
owns the log and assigns the cursor; this module is the loop between them.

Persist, then acknowledge. Never the reverse.
-------------------------------------------
An MQTT QoS 1 message is redelivered until it is acknowledged, and that is the
only durability guarantee in the system that this module does not own. The
consequence is asymmetric, which is why the order is not a preference:

* **acknowledge first, die before persisting** — the broker has been told the
  message was handled, drops it from the session queue, and the event is gone
  **for good**. Nothing anywhere can recover it.
* **persist first, die before acknowledging** — the broker still owns the
  message and redelivers it on the next resume, where
  :meth:`events_cli.history.HistoryStore.append`'s dedupe-on-id makes the
  second arrival a no-op that takes no new sequence.

So every message is persisted and *then* acknowledged, one at a time, on the
caller's thread. Not batched at the end — a batch of acknowledgements is the
first failure mode wearing a different hat: everything delivered since the last
flush is lost if the process dies mid-batch.

The record is also read back *before* the acknowledgement, not after. Reading
back is presentation rather than durability, so it is tempting to do it last —
but a read-back that fails after the ack would leave the store's sequence
advanced past an event no batch ever carried, on a broker that has been told it
was delivered. That is the same unrecoverable corner as acknowledging first,
reached by a longer road. Ordering it before the ack turns it into a
redelivery instead, which the dedupe absorbs.

This is also what makes an MQTT session takeover *lossless* rather than merely
loud: a drainer that is kicked off mid-batch may never have acknowledged what it
already persisted, and the incoming drainer resumes the same session, gets the
same messages again, and dedupes them away.

The bounds
----------
``max`` bounds **messages consumed**, not events returned, and the deadline is
:func:`time.monotonic`. Both are deliberate:

* counting *messages* means a flood of unparseable payloads on a shared broker
  cannot make a drain unbounded — the batch may come back smaller than ``max``,
  never slower;
* a wall-clock deadline can be moved by an NTP step or an operator setting the
  clock, which would either cut a drain short or hang it past its timeout.
  :func:`time.monotonic` cannot move backwards.

An idle subscription — the common case — returns an empty batch at the deadline
rather than blocking. An agent turn must never hang.

What this module does *not* do
------------------------------
It does not replay already-persisted history: reading from a cursor without
touching the broker is exactly :meth:`events_cli.history.HistoryStore.read`,
which is bounded, exact and needs no connection. The drain is the broker side
only, and ``events watch --since`` (t9) composes the two.

Nothing here imports paho at module scope — :class:`PersistentSession` owns that
lazy boundary — so importing this module still costs nothing on a checkout with
nothing installed.
"""

from __future__ import annotations

import logging
import math
import queue
import threading
import time
from dataclasses import dataclass
from typing import Any

from events_cli.core.envelope import Envelope
from events_cli.core.errors import EnvelopeValidationError, FieldError
from events_cli.history import HistoryError, HistoryRecord, HistoryStore, open_store
from events_cli.subs.errors import (
    DrainError,
    SubscriptionValidationError,
    UnknownSubscriptionError,
)
from events_cli.subs.record import SubscriptionRecord
from events_cli.subs.registry import SubscriptionRegistry, open_registry
from events_cli.subs.session import (
    QOS_AT_LEAST_ONCE,
    BrokerAddress,
    ClientFactory,
    PersistentSession,
    default_client_factory,
)

__all__ = [
    "DEFAULT_DRAIN_MAX",
    "DEFAULT_DRAIN_TIMEOUT",
    "STOPPED_MAX",
    "STOPPED_TIMEOUT",
    "DrainResult",
    "SkippedMessage",
    "drain_subscription",
]

_LOG = logging.getLogger("events_cli.subs")

#: How many messages one drain consumes at most. The same bound
#: :data:`events_cli.history.DEFAULT_MAX` puts on a read, for the same reason:
#: there is no unbounded sentinel, because an unbounded default is what
#: eventually hangs an agent turn.
DEFAULT_DRAIN_MAX = 100

#: How long one drain waits, in seconds. Finite by policy — an idle
#: subscription must return an empty batch, not block.
DEFAULT_DRAIN_TIMEOUT = 30.0

#: The drain filled its ``max``; there may well be more queued behind it.
STOPPED_MAX = "max"

#: The deadline arrived with the queue empty. Nothing more is waiting *now*.
STOPPED_TIMEOUT = "timeout"

#: How much of a rejected payload's diagnosis to keep. A malformed payload is
#: attacker-shaped input like any other: its error text must not be able to
#: inflate a ``--json`` result without bound.
_MAX_REASON = 200

_BOUNDS_HINT = (
    "pass finite bounds, e.g. max=100 and timeout=30; there is no unbounded drain, "
    "and 'since' is 0 or the cursor a previous drain returned"
)


@dataclass(frozen=True)
class SkippedMessage:
    """A message the drain consumed, could not read as an event, and dropped.

    Reported rather than silently swallowed: a payload on the contract lane
    that is not an envelope means some producer is publishing something this
    contract does not describe, and an operator needs to be able to see that
    from ``events watch --json`` without turning on debug logging.
    """

    topic: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {"topic": self.topic, "reason": self.reason}


@dataclass(frozen=True)
class DrainResult:
    """One drain's outcome: the batch, the cursor, and how it ended.

    ``records`` are in **delivery order** — the order the broker handed the
    messages over, which is the order they were persisted and acknowledged in.
    Their sequences ascend, except where a redelivered event resolves to a
    sequence the store already held (see :attr:`cursor`).

    ``cursor`` is the store's own sequence, never a count of this call's work,
    and never derived from an event id (``evt_`` ULIDs carry no
    intra-millisecond monotonicity — see :mod:`events_cli.history`). It never
    moves backwards: an empty batch returns the ``since`` it was given, so a
    caller that stores the cursor unconditionally cannot rewind.

    ``consumed`` counts **messages**, so ``consumed == len(records) +
    len(skipped)``; it is what ``max`` bounds.
    """

    subscription: str
    records: tuple[HistoryRecord, ...]
    cursor: int
    has_more: bool
    session_present: bool
    consumed: int
    skipped: tuple[SkippedMessage, ...]
    stopped: str

    @property
    def envelopes(self) -> tuple[Envelope, ...]:
        """The batch as plain events, for a caller that wants no store detail."""
        return tuple(record.envelope for record in self.records)

    def to_dict(self) -> dict[str, Any]:
        """The ``--json`` form. ``records``/``cursor``/``hasMore`` are spelled
        exactly as :class:`events_cli.history.HistoryPage` spells them, so one
        renderer serves a drain and a history read."""
        return {
            "subscription": self.subscription,
            "records": [record.to_dict() for record in self.records],
            "cursor": self.cursor,
            "hasMore": self.has_more,
            "sessionPresent": self.session_present,
            "consumed": self.consumed,
            "skipped": [skipped.to_dict() for skipped in self.skipped],
            "stopped": self.stopped,
        }


def drain_subscription(
    name: str,
    *,
    since: int = 0,
    max: int = DEFAULT_DRAIN_MAX,
    timeout: float = DEFAULT_DRAIN_TIMEOUT,
    address: BrokerAddress | None = None,
    registry: SubscriptionRegistry | None = None,
    store: HistoryStore | None = None,
    client_factory: ClientFactory | None = None,
    logger: logging.Logger | None = None,
) -> DrainResult:
    """Drain up to ``max`` messages from ``name``'s persistent session.

    Resumes the session the registry record names (``clean_start=False``, the
    infinite session expiry — :class:`PersistentSession`'s defaults are the
    durable ones), consumes until ``max`` messages or the ``timeout`` deadline,
    **persists each event to the history store before acknowledging it**, and
    returns the batch plus the next cursor.

    ``since`` is the cursor floor, not a filter: it is what an empty batch
    returns, so a caller looping on ``result.cursor`` never rewinds. Replaying
    events that are *already* persisted is
    :meth:`events_cli.history.HistoryStore.read` — no broker involved.

    The session is subscribed again only when the broker reports it did **not**
    resume (``session_present=False``). A lost session has lost its
    subscription too, and a drain that did not notice would return empty
    forever; the queued backlog is gone either way, so re-subscribing recovers
    everything still recoverable and :attr:`DrainResult.session_present` reports
    that it happened.

    Raises :class:`SubscriptionValidationError` (bad name or bound, exit 1),
    :class:`UnknownSubscriptionError` (exit 1),
    :class:`~events_cli.subs.errors.BrokerUnreachableError` /
    :class:`~events_cli.subs.errors.SessionError` (exit 2) and
    :class:`DrainError` (exit 2). Every one of them is a
    :class:`~events_cli.subs.errors.SubsError` carrying a remediation.
    """
    log = logger or _LOG
    _check_bounds(since, max, timeout)
    record = _resolve(name, registry)
    events = store if store is not None else open_store()

    inbox: queue.Queue[Any] = queue.Queue()
    stopping = threading.Event()

    def on_message(client: Any, userdata: Any, message: Any) -> None:
        # Runs on paho's network thread: enqueue and return. Persisting here
        # would block the very loop that has to send our PUBACKs. Once we are
        # shutting down we drop instead, which loses nothing — an unacknowledged
        # message is still the broker's, and comes back on the next resume.
        #
        # The queue needs no bound of its own: with manual acknowledgement, MQTT
        # flow control already caps what can be in flight to the broker's
        # `max_inflight_messages`, and nothing else can put into it.
        if not stopping.is_set():
            inbox.put(message)

    session = PersistentSession(
        record.client_id,
        address,
        manual_ack=True,
        client_factory=_drain_client_factory(client_factory, on_message),
    )
    try:
        # Inside the try, like `add_subscription`'s: `close()` is idempotent and
        # never raises, and this way a failed open still sets `stopping`, so a
        # late callback cannot queue into an inbox nobody will ever drain.
        session_present = session.open()
        if not session_present:
            log.warning(
                "session %s did not resume; re-subscribing %s (any queued backlog is gone)",
                record.client_id,
                record.topic_filter,
            )
            session.subscribe(record.topic_filter, qos=QOS_AT_LEAST_ONCE)
        return _consume(
            session.client,
            record,
            events,
            inbox,
            since=since,
            max=max,
            timeout=timeout,
            session_present=session_present,
            log=log,
        )
    finally:
        stopping.set()
        session.close()


def _drain_client_factory(inner: ClientFactory | None, on_message: Any) -> ClientFactory:
    """Wrap ``inner`` so the client is fully armed *before* it connects.

    Both things this adds have to happen before CONNECT, and neither can be done
    through :class:`PersistentSession` afterwards:

    * ``manual_ack_set(True)`` — belt and braces over the constructor keyword.
      A client that auto-acks acknowledges *on delivery*, i.e. before the store
      has the event, silently inverting the one ordering this module exists to
      guarantee. It is worth two lines to make that impossible for a
      caller-supplied factory that ignored the keyword.
    * ``on_message`` — a resumed session starts delivering its backlog the
      instant the CONNACK lands, so attaching the handler after ``open()``
      returns is a race. (paho with manual acknowledgement drops an unhandled
      message without acknowledging it, so the race would cost a redelivery
      rather than an event — but a redelivery every drain is not a race worth
      keeping.)
    """
    build = inner if inner is not None else default_client_factory

    def factory(mqtt: Any, client_id: str, *, manual_ack: bool) -> Any:
        client = build(mqtt, client_id, manual_ack=manual_ack)
        client.manual_ack_set(True)
        client.on_message = on_message
        return client

    return factory


def _consume(
    client: Any,
    record: SubscriptionRecord,
    store: HistoryStore,
    inbox: "queue.Queue[Any]",
    *,
    since: int,
    max: int,
    timeout: float,
    session_present: bool,
    log: logging.Logger,
) -> DrainResult:
    """The bounded loop. Everything here runs on the caller's thread."""
    deadline = time.monotonic() + timeout
    records: list[HistoryRecord] = []
    skipped: list[SkippedMessage] = []
    cursor = since
    consumed = 0

    while consumed < max:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            message = inbox.get(timeout=remaining)
        except queue.Empty:
            break

        consumed += 1
        envelope = _parse(message, skipped, log)
        if envelope is None:
            # Acknowledged deliberately: it will never parse on redelivery, so
            # leaving it unacknowledged makes it a poison pill that consumes
            # part of every later drain's bound for as long as the session
            # lives. There is nothing to persist, so nothing is at risk.
            client.ack(message.mid, message.qos)
            continue

        seq = _persist(store, envelope, record.name)
        # Read back BEFORE acknowledging. A read-back failure must leave the
        # message unacknowledged so the broker redelivers it: acking first
        # would advance the store's sequence past an event no batch ever
        # carried, while telling the broker it was delivered — the one state
        # neither a retry nor a redelivery can recover from. Redelivery costs
        # nothing here, because the store dedupes on id and hands back the
        # same sequence.
        #
        # This is the opposite of the malformed-payload case above, and for a
        # reason worth keeping: a bad payload is a property of the *message*
        # and will never parse, so holding it back poisons the queue. A failed
        # read-back is a property of the *store* — an IO fault an operator
        # fixes — so holding the message back is exactly right, and the drain
        # self-heals once the store is readable again.
        row = _read_back(store, record.name, seq)
        client.ack(message.mid, message.qos)
        if seq > cursor:
            cursor = seq
        records.append(row)

    stopped = STOPPED_MAX if consumed >= max else STOPPED_TIMEOUT
    return DrainResult(
        subscription=record.name,
        records=tuple(records),
        cursor=cursor,
        has_more=stopped == STOPPED_MAX,
        session_present=session_present,
        consumed=consumed,
        skipped=tuple(skipped),
        stopped=stopped,
    )


def _parse(message: Any, skipped: list[SkippedMessage], log: logging.Logger) -> Envelope | None:
    """The trust boundary. A payload that is not an event is skipped, not fatal.

    Three options were available and only one of them is safe on a broker any
    producer can publish to. *Raising* would let one malformed publish take down
    every drain of the subscription. *Skipping without acknowledging* would
    leave the message queued forever, burning part of every later drain's bound
    and, at the broker's inflight limit, eventually blocking the queue behind
    it. So: skip it, acknowledge it, count it, and log it — the caller sees it
    in :attr:`DrainResult.skipped` and an operator sees it in the log.
    """
    try:
        return Envelope.from_json(message.payload)
    except EnvelopeValidationError as exc:
        reason = str(exc)[:_MAX_REASON]
        log.warning("skipping an unreadable payload on %s: %s", message.topic, reason)
        skipped.append(SkippedMessage(topic=str(message.topic), reason=reason))
        return None


def _persist(store: HistoryStore, envelope: Envelope, sub: str) -> int:
    """Append to the store, or fail the drain without acknowledging anything."""
    try:
        return store.append(envelope, sub)
    except HistoryError as exc:
        raise DrainError(
            f"could not store event {envelope.id} for subscription {sub!r} ({exc})",
            remediation=(
                "the message was left unacknowledged, so the broker will redeliver it; "
                "fix the history store (EVENTS_HISTORY_DIR, or "
                "$XDG_CONFIG_HOME/events-cli/history by default) and drain again"
            ),
        ) from exc


def _read_back(store: HistoryStore, sub: str, seq: int) -> HistoryRecord:
    """The stored record for ``seq`` — the store's own copy, not a rebuilt one.

    Read rather than reconstructed so the batch carries the store's
    ``recordedAt`` and its sequence exactly as the log holds them; for a
    redelivery that means the record as it was *first* stored, which is the
    truth about that fact.
    """
    try:
        page = store.read(sub, seq - 1, 1)
    except HistoryError as exc:
        raise DrainError(
            f"could not read back event {seq} of subscription {sub!r} ({exc})",
            remediation=(
                "the event was stored and acknowledged; inspect the history store "
                "(EVENTS_HISTORY_DIR, or $XDG_CONFIG_HOME/events-cli/history by default)"
            ),
        ) from exc
    if not page.records:
        raise DrainError(
            f"the history store did not return sequence {seq} of subscription {sub!r} "
            "immediately after assigning it",
            remediation=(
                "the store is inconsistent; inspect it (EVENTS_HISTORY_DIR, or "
                "$XDG_CONFIG_HOME/events-cli/history by default)"
            ),
        )
    return page.records[0]


def _resolve(name: str, registry: SubscriptionRegistry | None) -> SubscriptionRecord:
    """The registry record for ``name``, or a clean named error.

    Resolved *before* anything touches the broker: the record is where the
    stable client id and the compiled filter come from, and a drain of a name
    nobody registered has no session to resume.
    """
    store = registry if registry is not None else open_registry()
    record = store.get(name)
    if record is None:
        raise UnknownSubscriptionError(
            f"no subscription named {name!r}",
            remediation=(
                "list what is registered with 'events sub list', or register it "
                f"with 'events sub add {name} <pattern>'"
            ),
        )
    return record


def _check_bounds(since: int, max: int, timeout: float) -> None:
    """Reject an unbounded or nonsensical drain before it costs a connection.

    Every problem in one pass, field-level, like every other validator in this
    repo: an agent fixing two broken flags should need one round trip, not two.
    """
    errors: list[FieldError] = []
    _check_max(max, errors)
    _check_timeout(timeout, errors)
    _check_since(since, errors)
    if errors:
        raise SubscriptionValidationError(
            errors, summary="invalid drain bounds", remediation=_BOUNDS_HINT
        )


def _check_max(value: Any, errors: list[FieldError]) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        errors.append(
            FieldError("max", "not_an_integer", f"must be an integer, got {type(value).__name__}")
        )
    elif value < 1:
        errors.append(FieldError("max", "out_of_range", f"must be at least 1 message, got {value}"))


def _check_timeout(value: Any, errors: list[FieldError]) -> None:
    number = not isinstance(value, bool) and isinstance(value, (int, float))
    if not number or not math.isfinite(value) or value <= 0:
        errors.append(
            FieldError(
                "timeout",
                "out_of_range",
                f"must be a positive, finite number of seconds, got {value!r}",
            )
        )


def _check_since(value: Any, errors: list[FieldError]) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        errors.append(
            FieldError("since", "not_an_integer", f"must be an integer, got {type(value).__name__}")
        )
    elif value < 0:
        errors.append(
            FieldError("since", "out_of_range", f"must be a non-negative cursor, got {value}")
        )

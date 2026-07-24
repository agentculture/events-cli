"""Contract tests for the bounded drain: resume, persist-then-ack, cursor.

Everything here runs with **no broker and no docker**. The drain is driven
against :class:`FakeDrainPaho` — t7's hand-written :class:`FakePaho` extended
with the two halves a drain needs and a registration lifecycle does not:
*message delivery* (on a background thread, exactly as paho's network loop
delivers) and *manual acknowledgement*. The live-broker proof — that a real
Mosquitto really does redeliver what was never acknowledged — is the
stack-marked suite (t12); what is pinned here is the ordering and the bounds,
which are properties of the code, not of any broker's reply.

The one test that matters most
------------------------------
:func:`test_each_event_is_persisted_before_it_is_acknowledged` asserts the
**exact interleaving** of two side effects recorded in one shared journal::

    persist:<id0>, ack:0, persist:<id1>, ack:1, persist:<id2>, ack:2

It fails if the order is swapped (``ack`` before ``persist``), and it fails
just as loudly if the acknowledgements are batched at the end of the drain —
both of which look harmless in a diff and both of which lose events. An MQTT
QoS 1 message is redelivered until it is acknowledged: acknowledge before the
history store holds the event and a process death in between makes the broker
consider it delivered, so the event is gone for good. Persist first and die
before acknowledging and the broker simply redelivers it, where the store's
dedupe-on-id makes the second arrival a no-op. The asymmetry is the whole
design, and this journal is what holds it in place.

Two more properties get disproportionate attention here because a
plausible-looking implementation gets them wrong:

* **the cursor is the store's sequence**, never a count of what this call
  consumed. The store is pre-seeded in
  :func:`test_the_cursor_is_the_stores_sequence_not_the_count_of_messages_consumed`
  precisely so a count-based cursor produces a different, wrong number.
* **the bounds are real.** ``--max`` bounds *messages consumed* (so a flood of
  malformed payloads cannot make a drain unbounded) and the deadline is
  monotonic, so a system clock adjustment mid-drain cannot extend it.
"""

from __future__ import annotations

import ast
import logging
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from events_cli.core import ERROR_CODES
from events_cli.core.envelope import Envelope
from events_cli.core.topics import type_to_topic
from events_cli.history import DEFAULT_MAX as HISTORY_DEFAULT_MAX
from events_cli.history import HistoryError, HistoryPage, HistoryStore
from events_cli.subs import (
    DEFAULT_DRAIN_MAX,
    DEFAULT_DRAIN_TIMEOUT,
    QOS_AT_LEAST_ONCE,
    SESSION_EXPIRY_INFINITE,
    BrokerAddress,
    BrokerUnreachableError,
    DrainError,
    DrainResult,
    SessionError,
    SkippedMessage,
    SubscriptionRecord,
    SubscriptionRegistry,
    SubscriptionValidationError,
    SubsError,
    UnknownSubscriptionError,
    client_id_for,
    drain_subscription,
)
from events_cli.subs import errors as subs_errors
from tests.test_subs import FakePaho, subs_source_files

# --- the fakes -------------------------------------------------------------
#
# Extensions of t7's hand-written double, for the same reason it was written by
# hand: every assertion below is about *call order*, and a mock that accepts
# anything would happily pass whatever we wrote.


class FakeMessage:
    """Just enough of paho's ``MQTTMessage`` for a drain to consume."""

    def __init__(self, topic: str, payload: bytes, mid: int, qos: int = QOS_AT_LEAST_ONCE) -> None:
        self.topic = topic
        self.payload = payload
        self.mid = mid
        self.qos = qos
        self.retain = False
        self.dup = False


class FakeDrainPaho(FakePaho):
    """:class:`FakePaho` plus delivery and manual acknowledgement.

    Deliveries are pumped from a **background thread** by default, because that
    is where paho's network loop calls ``on_message`` — the drain must not
    assume its own thread produced the message. ``deliver_in_thread=False``
    delivers everything synchronously inside ``loop_start`` instead, which is
    what makes the "arrived while shutting down" assertions deterministic.
    """

    def __init__(
        self,
        client_id: str,
        *,
        manual_ack: bool = False,
        deliveries: tuple[FakeMessage, ...] = (),
        journal: list[str] | None = None,
        deliver_in_thread: bool = True,
        **kwargs: Any,
    ) -> None:
        super().__init__(client_id, manual_ack=manual_ack, **kwargs)
        # What the *constructor* was asked for, before manual_ack_set touches it.
        self.manual_ack_at_construction = manual_ack
        self.deliveries = list(deliveries)
        self.deliver_in_thread = deliver_in_thread
        self.journal = journal if journal is not None else []
        self.acks: list[tuple[int, int]] = []
        self.manual_ack_calls: list[bool] = []
        self.handler_at_connect: Any = None
        self.manual_ack_at_connect: bool | None = None
        self.delivered = threading.Event()

    # -- the two extra methods a drain may use -----------------------------

    def manual_ack_set(self, on: bool) -> None:
        self.manual_ack_calls.append(on)
        self.manual_ack = on
        self.calls.append(("manual_ack_set", {"on": on}))

    def ack(self, mid: int, qos: int) -> int:
        self.acks.append((mid, qos))
        self.journal.append(f"ack:{mid}")
        return 0

    # -- delivery -----------------------------------------------------------

    def connect(self, *args: Any, **kwargs: Any) -> int:
        # Captured *at CONNECT time*: a handler attached afterwards would race
        # the first queued message the broker sends on a resumed session.
        self.handler_at_connect = self.on_message
        self.manual_ack_at_connect = self.manual_ack
        return super().connect(*args, **kwargs)

    def loop_start(self) -> int:
        rc = super().loop_start()
        if self.deliver_in_thread:
            threading.Thread(target=self._deliver_all, daemon=True).start()
        else:
            self._deliver_all()
        return rc

    def _deliver_all(self) -> None:
        for message in self.deliveries:
            self.deliver(message)
        self.delivered.set()

    def deliver(self, message: FakeMessage) -> None:
        handler = self.on_message
        assert handler is not None, "the drain must attach on_message before connecting"
        handler(self, None, message)


def fake_drain_factory(**client_kwargs: Any):
    """A ``client_factory`` handing back :class:`FakeDrainPaho`, remembering each."""
    made: list[FakeDrainPaho] = []

    def factory(mqtt: Any, client_id: str, *, manual_ack: bool) -> FakeDrainPaho:
        client = FakeDrainPaho(client_id, manual_ack=manual_ack, **client_kwargs)
        made.append(client)
        return client

    factory.made = made  # type: ignore[attr-defined]
    return factory


class JournalStore:
    """A real :class:`HistoryStore` that journals appends — and can be made to fail.

    The journal entry is written **after** the wrapped append returns, so a
    ``persist:`` entry means the store really does hold the event; that is what
    lets the ordering assertions be about durability rather than about
    intention.
    """

    def __init__(
        self,
        store: HistoryStore,
        journal: list[str],
        *,
        fail_on: str | None = None,
        blind: bool = False,
        unreadable: bool = False,
        append_delay: float = 0.0,
    ) -> None:
        self._store = store
        self.journal = journal
        self.fail_on = fail_on
        self.blind = blind  # a store that cannot read back what it just wrote
        self.unreadable = unreadable  # a store whose read path is broken outright
        self.append_delay = append_delay

    @property
    def root(self) -> Path:
        return self._store.root

    def append(self, envelope: Envelope, sub: str) -> int:
        if self.fail_on is not None and envelope.id == self.fail_on:
            raise HistoryError("no space left on device", remediation="free some disk")
        if self.append_delay:
            time.sleep(self.append_delay)
        seq = self._store.append(envelope, sub)
        self.journal.append(f"persist:{envelope.id}")
        return seq

    def read(self, sub: str, since: int = 0, max: int = HISTORY_DEFAULT_MAX) -> HistoryPage:
        if self.unreadable:
            raise HistoryError("the log is damaged", remediation="inspect the store")
        if self.blind:
            return HistoryPage(records=(), cursor=since, has_more=False)
        return self._store.read(sub, since, max)

    def get(self, id: str):
        return self._store.get(id)

    def list(self, type: str | None = None, max: int = HISTORY_DEFAULT_MAX):
        return self._store.list(type, max)

    def subscriptions(self) -> tuple[str, ...]:
        return self._store.subscriptions()


# --- fixtures and helpers --------------------------------------------------

SUB = "robot"


@pytest.fixture
def store(tmp_path: Path) -> HistoryStore:
    return HistoryStore(tmp_path / "history")


@pytest.fixture
def registry(tmp_path: Path) -> SubscriptionRegistry:
    registry = SubscriptionRegistry(tmp_path / "registry")
    registry.add(SubscriptionRecord.new(SUB, "task.*", owner="builder"))
    return registry


def event(n: int = 1) -> Envelope:
    return Envelope.new("task.requested", "agent://tester", data={"n": n})


def message(envelope: Envelope, mid: int, *, qos: int = QOS_AT_LEAST_ONCE) -> FakeMessage:
    return FakeMessage(type_to_topic(envelope.type), envelope.to_json().encode("utf-8"), mid, qos)


def broken(payload: bytes, mid: int) -> FakeMessage:
    return FakeMessage("events/task/requested", payload, mid)


def drain(registry: SubscriptionRegistry, store: Any, factory: Any, **kwargs: Any) -> DrainResult:
    """``drain_subscription`` with the seams every test injects."""
    kwargs.setdefault("timeout", 5.0)
    return drain_subscription(SUB, registry=registry, store=store, client_factory=factory, **kwargs)


# =========================================================================
# Criterion 1a — the drain resumes the session with manual acknowledgement
# =========================================================================


def test_the_drain_resumes_the_session_rather_than_starting_a_new_one(
    registry: SubscriptionRegistry, store: HistoryStore
) -> None:
    factory = fake_drain_factory(session_present=True)
    drain(registry, store, factory, timeout=0.05)

    kwargs = factory.made[0].kwargs("connect")
    assert kwargs["clean_start"] is False
    assert kwargs["properties"].SessionExpiryInterval == SESSION_EXPIRY_INFINITE


def test_the_drain_presents_the_subscriptions_stable_client_id(
    registry: SubscriptionRegistry, store: HistoryStore
) -> None:
    """The broker knows a persistent session only by its client id."""
    factory = fake_drain_factory(session_present=True)
    drain(registry, store, factory, timeout=0.05)

    assert factory.made[0].client_id == client_id_for(SUB)


def test_the_drain_reports_whether_the_session_actually_resumed(
    registry: SubscriptionRegistry, store: HistoryStore
) -> None:
    resumed = drain(registry, store, fake_drain_factory(session_present=True), timeout=0.05)
    fresh = drain(registry, store, fake_drain_factory(session_present=False), timeout=0.05)

    assert resumed.session_present is True
    assert fresh.session_present is False


def test_the_drain_turns_manual_acknowledgement_on_before_it_connects(
    registry: SubscriptionRegistry, store: HistoryStore
) -> None:
    """An auto-acking client acknowledges on delivery — i.e. before we persist."""
    factory = fake_drain_factory(session_present=True)
    drain(registry, store, factory, timeout=0.05)

    client = factory.made[0]
    assert client.manual_ack_at_construction is True, "the session seam must request it"
    assert client.manual_ack_calls == [True], "and the drain must insist on it"
    assert client.manual_ack is True
    assert client.manual_ack_at_connect is True


def test_the_message_handler_is_attached_before_the_connect(
    registry: SubscriptionRegistry, store: HistoryStore
) -> None:
    """A resumed session starts delivering its backlog the moment it connects."""
    factory = fake_drain_factory(session_present=True)
    drain(registry, store, factory, timeout=0.05)

    assert factory.made[0].handler_at_connect is not None


def test_a_resumed_session_is_not_resubscribed(
    registry: SubscriptionRegistry, store: HistoryStore
) -> None:
    """The subscription is part of the session; re-sending it buys nothing."""
    factory = fake_drain_factory(session_present=True)
    drain(registry, store, factory, timeout=0.05)

    assert "subscribe" not in factory.made[0].sequence


def test_a_lost_session_is_resubscribed_so_the_drain_does_not_go_deaf(
    registry: SubscriptionRegistry, store: HistoryStore
) -> None:
    """Without the session there is no subscription either — and no backlog ever."""
    factory = fake_drain_factory(session_present=False)
    drain(registry, store, factory, timeout=0.05)

    client = factory.made[0]
    assert client.kwargs("subscribe") == {"topic": "events/task/+", "qos": QOS_AT_LEAST_ONCE}


def test_the_drain_disconnects_gracefully_when_it_is_done(
    registry: SubscriptionRegistry, store: HistoryStore
) -> None:
    """A graceful DISCONNECT leaves the session, and its queue, live in the broker."""
    factory = fake_drain_factory(session_present=True)
    drain(registry, store, factory, timeout=0.05)

    assert factory.made[0].sequence[-2:] == ["disconnect", "loop_stop"]


def test_a_custom_broker_address_reaches_connect(
    registry: SubscriptionRegistry, store: HistoryStore
) -> None:
    factory = fake_drain_factory(session_present=True)
    drain(
        registry, store, factory, timeout=0.05, address=BrokerAddress("broker.internal", 1884, 30)
    )

    kwargs = factory.made[0].kwargs("connect")
    assert (kwargs["host"], kwargs["port"], kwargs["keepalive"]) == ("broker.internal", 1884, 30)


# =========================================================================
# Criterion 1b — persist BEFORE ack. The correctness rule that matters most.
# =========================================================================


def test_each_event_is_persisted_before_it_is_acknowledged(
    registry: SubscriptionRegistry, store: HistoryStore
) -> None:
    """The single most valuable assertion in this file.

    Fails if the two side effects are swapped, and fails if the acknowledgements
    are batched at the end: a QoS 1 message is redelivered until it is acked, so
    acking first turns a process death into permanent event loss, while
    persisting first turns it into a redelivery the store dedupes away.
    """
    journal: list[str] = []
    events = [event(n) for n in range(3)]
    factory = fake_drain_factory(
        session_present=True,
        journal=journal,
        deliveries=tuple(message(envelope, mid) for mid, envelope in enumerate(events)),
    )

    drain(registry, JournalStore(store, journal), factory, max=3)

    assert journal == [
        "persist:" + events[0].id,
        "ack:0",
        "persist:" + events[1].id,
        "ack:1",
        "persist:" + events[2].id,
        "ack:2",
    ]


def test_an_event_the_store_refuses_is_never_acknowledged(
    registry: SubscriptionRegistry, store: HistoryStore
) -> None:
    """Unacknowledged means the broker still owns it: redelivered, not lost."""
    journal: list[str] = []
    first, poison = event(1), event(2)
    factory = fake_drain_factory(
        session_present=True,
        journal=journal,
        deliveries=(message(first, 1), message(poison, 2)),
    )

    journal_store = JournalStore(store, journal, fail_on=poison.id)
    with pytest.raises(DrainError):
        drain(registry, journal_store, factory, max=5)

    assert factory.made[0].acks == [(1, QOS_AT_LEAST_ONCE)]
    assert journal == [f"persist:{first.id}", "ack:1"]


def test_a_store_failure_still_closes_the_session(
    registry: SubscriptionRegistry, store: HistoryStore
) -> None:
    poison = event(1)
    factory = fake_drain_factory(session_present=True, deliveries=(message(poison, 1),))
    journal_store = JournalStore(store, [], fail_on=poison.id)

    with pytest.raises(DrainError):
        drain(registry, journal_store, factory)

    assert factory.made[0].sequence[-2:] == ["disconnect", "loop_stop"]


def test_what_was_persisted_before_a_store_failure_survives_it(
    registry: SubscriptionRegistry, store: HistoryStore
) -> None:
    """The batch is lost to the caller; the events are not — they are in the store."""
    first, poison = event(1), event(2)
    factory = fake_drain_factory(
        session_present=True, deliveries=(message(first, 1), message(poison, 2))
    )
    journal_store = JournalStore(store, [], fail_on=poison.id)

    with pytest.raises(DrainError):
        drain(registry, journal_store, factory)

    page = store.read(SUB, 0, 10)
    assert [record.envelope.id for record in page.records] == [first.id]


def test_the_ack_names_the_messages_own_mid_and_qos(
    registry: SubscriptionRegistry, store: HistoryStore
) -> None:
    factory = fake_drain_factory(
        session_present=True,
        deliveries=(message(event(1), 7), message(event(2), 9)),
    )

    drain(registry, store, factory, max=2)

    assert factory.made[0].acks == [(7, QOS_AT_LEAST_ONCE), (9, QOS_AT_LEAST_ONCE)]


def test_a_qos_0_message_is_persisted_and_acked_with_its_own_qos(
    registry: SubscriptionRegistry, store: HistoryStore
) -> None:
    """paho's ``ack`` is a no-op below QoS 1; passing the message's own qos keeps it so."""
    envelope = event(1)
    factory = fake_drain_factory(session_present=True, deliveries=(message(envelope, 3, qos=0),))

    result = drain(registry, store, factory, max=1)

    assert factory.made[0].acks == [(3, 0)]
    assert [record.envelope.id for record in result.records] == [envelope.id]


# =========================================================================
# Criterion 1c — the bounds: --max, --timeout, and a monotonic deadline
# =========================================================================


def test_the_drain_stops_at_max(registry: SubscriptionRegistry, store: HistoryStore) -> None:
    events = [event(n) for n in range(5)]
    factory = fake_drain_factory(
        session_present=True,
        deliveries=tuple(message(envelope, mid) for mid, envelope in enumerate(events)),
    )

    result = drain(registry, store, factory, max=3)

    assert result.consumed == 3
    assert len(result.records) == 3
    assert result.stopped == "max"
    assert result.has_more is True


def test_a_message_arriving_after_max_is_reached_is_neither_persisted_nor_acked(
    registry: SubscriptionRegistry, store: HistoryStore
) -> None:
    """Left unacknowledged on purpose: the broker redelivers it to the next drain.

    Persisting it would break the ``--max`` bound; acknowledging it without
    persisting would lose it. Doing neither is the only choice that keeps both
    the bound and the event.
    """
    events = [event(n) for n in range(5)]
    factory = fake_drain_factory(
        session_present=True,
        deliver_in_thread=False,  # every message is delivered before the loop runs
        deliveries=tuple(message(envelope, mid) for mid, envelope in enumerate(events)),
    )

    result = drain(registry, store, factory, max=3)

    client = factory.made[0]
    assert client.delivered.is_set()
    assert len(client.acks) == 3
    assert [record.envelope.id for record in result.records] == [e.id for e in events[:3]]
    assert store.get(events[3].id) is None
    assert store.get(events[4].id) is None


def test_an_idle_drain_returns_within_its_timeout(
    registry: SubscriptionRegistry, store: HistoryStore
) -> None:
    """The common case — an idle subscription must never hang an agent turn."""
    started = time.monotonic()
    result = drain(registry, store, fake_drain_factory(session_present=True), timeout=0.05)
    elapsed = time.monotonic() - started

    assert result.records == ()
    assert result.stopped == "timeout"
    assert result.has_more is False
    assert elapsed < 5.0


def test_a_full_batch_returns_without_waiting_out_the_timeout(
    registry: SubscriptionRegistry, store: HistoryStore
) -> None:
    factory = fake_drain_factory(
        session_present=True,
        deliveries=(message(event(1), 1), message(event(2), 2)),
    )

    started = time.monotonic()
    result = drain(registry, store, factory, max=2, timeout=30.0)
    elapsed = time.monotonic() - started

    assert result.stopped == "max"
    assert elapsed < 15.0


def test_the_drain_deadline_uses_the_monotonic_clock() -> None:
    """Static, so it also holds on the branches this suite never runs.

    A wall-clock deadline can be pushed into the future (or the past) by an NTP
    step or a manual clock set, which would either hang the drain or cut it
    short. ``time.monotonic`` cannot move backwards.
    """
    source = (
        Path(__file__).resolve().parent.parent / "events_cli" / "subs" / "drain.py"
    ).read_text(encoding="utf-8")
    assert "time.monotonic()" in source
    assert "time.time()" not in source
    assert "datetime.now" not in source


@pytest.mark.parametrize(
    "kwargs, field",
    [
        ({"max": 0}, "max"),
        ({"max": -1}, "max"),
        ({"max": 1.5}, "max"),
        ({"max": True}, "max"),
        ({"timeout": 0}, "timeout"),
        ({"timeout": -1.0}, "timeout"),
        ({"timeout": float("inf")}, "timeout"),
        ({"timeout": "30"}, "timeout"),
        ({"since": -1}, "since"),
        ({"since": "0"}, "since"),
    ],
)
def test_an_unbounded_or_nonsensical_drain_cannot_be_requested(
    registry: SubscriptionRegistry, store: HistoryStore, kwargs: dict[str, Any], field: str
) -> None:
    """There is no 'unbounded' sentinel; an infinite timeout is not spellable."""
    factory = fake_drain_factory(session_present=True)
    with pytest.raises(SubscriptionValidationError) as excinfo:
        drain(registry, store, factory, **kwargs)

    assert field in excinfo.value.fields
    assert factory.made == [], "a rejected bound must never cost a broker connection"


def test_broken_bounds_are_reported_in_one_pass(
    registry: SubscriptionRegistry, store: HistoryStore
) -> None:
    factory = fake_drain_factory()
    with pytest.raises(SubscriptionValidationError) as excinfo:
        drain(registry, store, factory, max=0, timeout=0, since=-1)

    assert set(excinfo.value.fields) == {"max", "timeout", "since"}
    assert {err.code for err in excinfo.value.errors} <= set(ERROR_CODES)


def test_the_documented_defaults_are_the_bounded_ones() -> None:
    assert DEFAULT_DRAIN_MAX == 100
    assert DEFAULT_DRAIN_TIMEOUT == 30.0


# =========================================================================
# Criterion 1d — the batch and the cursor come from the store's sequence
# =========================================================================


def test_the_batch_holds_the_stored_records_in_delivery_order(
    registry: SubscriptionRegistry, store: HistoryStore
) -> None:
    events = [event(n) for n in range(3)]
    factory = fake_drain_factory(
        session_present=True,
        deliveries=tuple(message(envelope, mid) for mid, envelope in enumerate(events)),
    )

    result = drain(registry, store, factory, max=3)

    assert [record.envelope.id for record in result.records] == [e.id for e in events]
    assert [record.seq for record in result.records] == [1, 2, 3]
    assert all(record.subscription == SUB for record in result.records)


def test_the_cursor_is_the_stores_sequence_not_the_count_of_messages_consumed(
    registry: SubscriptionRegistry, store: HistoryStore
) -> None:
    """Pre-seeded so a count-based cursor gives a different — and wrong — answer."""
    for n in range(7):
        store.append(event(100 + n), SUB)

    factory = fake_drain_factory(
        session_present=True,
        deliveries=(message(event(1), 1), message(event(2), 2)),
    )
    result = drain(registry, store, factory, max=2)

    assert result.cursor == 9
    assert [record.seq for record in result.records] == [8, 9]


def test_the_cursor_resumes_exactly_where_the_last_drain_left_off(
    registry: SubscriptionRegistry, store: HistoryStore
) -> None:
    first = drain(
        registry,
        store,
        fake_drain_factory(session_present=True, deliveries=(message(event(1), 1),)),
        max=1,
    )
    second = drain(
        registry,
        store,
        fake_drain_factory(session_present=True, deliveries=(message(event(2), 2),)),
        max=1,
        since=first.cursor,
    )

    assert (first.cursor, second.cursor) == (1, 2)
    assert store.read(SUB, first.cursor, 10).records[0].seq == 2


def test_an_idle_drain_returns_the_cursor_it_was_given(
    registry: SubscriptionRegistry, store: HistoryStore
) -> None:
    """A drain loop that stores the cursor unconditionally must never rewind."""
    result = drain(
        registry, store, fake_drain_factory(session_present=True), since=41, timeout=0.05
    )

    assert result.cursor == 41


def test_a_redelivered_event_is_stored_once_and_never_takes_a_second_sequence(
    registry: SubscriptionRegistry, store: HistoryStore
) -> None:
    """At-least-once delivery: the second arrival is a fact the store already holds.

    It is still reported and still acknowledged — a takeover may mean no caller
    ever saw the first delivery — but it takes no new sequence, so the cursor
    advances once.
    """
    envelope = event(1)
    factory = fake_drain_factory(
        session_present=True,
        deliveries=(message(envelope, 1), message(envelope, 2)),
    )

    result = drain(registry, store, factory, max=2)

    assert len(store.read(SUB, 0, 10).records) == 1
    assert [record.seq for record in result.records] == [1, 1]
    assert result.cursor == 1
    assert factory.made[0].acks == [(1, QOS_AT_LEAST_ONCE), (2, QOS_AT_LEAST_ONCE)]


def test_a_store_that_cannot_read_back_what_it_just_wrote_is_a_named_error(
    registry: SubscriptionRegistry, store: HistoryStore
) -> None:
    factory = fake_drain_factory(session_present=True, deliveries=(message(event(1), 1),))
    journal_store = JournalStore(store, [], blind=True)

    with pytest.raises(DrainError) as excinfo:
        drain(registry, journal_store, factory, max=1)

    assert excinfo.value.remediation


def test_a_store_whose_read_path_is_broken_is_a_named_error_not_a_traceback(
    registry: SubscriptionRegistry, store: HistoryStore
) -> None:
    """The event is stored but NOT acknowledged, so the broker redelivers it.

    Acking here would advance the store's sequence past an event that no batch
    ever carried, while telling the broker it was delivered — the one state
    from which neither the drain nor a redelivery can recover it. Leaving it
    unacknowledged costs only a redelivery, which the store dedupes back onto
    the same sequence, and the drain self-heals once the store is readable.

    Deliberately the opposite of the malformed-payload policy: that case acks
    because the fault is in the *message* and will never parse; this one holds
    back because the fault is in the *store*.
    """
    factory = fake_drain_factory(session_present=True, deliveries=(message(event(1), 1),))
    journal_store = JournalStore(store, [], unreadable=True)

    with pytest.raises(DrainError) as excinfo:
        drain(registry, journal_store, factory, max=1)

    assert "read back" in str(excinfo.value)
    assert factory.made[0].acks == []


def test_the_deadline_is_rechecked_between_messages_so_a_slow_store_cannot_overrun_it(
    registry: SubscriptionRegistry, store: HistoryStore
) -> None:
    """One slow append must not buy the drain a second one past the deadline."""
    events = [event(n) for n in range(3)]
    factory = fake_drain_factory(
        session_present=True,
        deliver_in_thread=False,
        deliveries=tuple(message(envelope, mid) for mid, envelope in enumerate(events)),
    )

    # A wide margin in both directions: 200ms of headroom before the first
    # message is consumed, and an append that overruns the deadline by 100ms.
    # Neither bound is a performance measurement, so a loaded runner cannot
    # flip the result.
    result = drain(
        registry,
        JournalStore(store, [], append_delay=0.3),
        factory,
        max=3,
        timeout=0.2,
    )

    assert result.consumed == 1
    assert result.stopped == "timeout"


def test_the_batch_is_available_as_plain_envelopes(
    registry: SubscriptionRegistry, store: HistoryStore
) -> None:
    envelope = event(1)
    factory = fake_drain_factory(session_present=True, deliveries=(message(envelope, 1),))

    result = drain(registry, store, factory, max=1)

    assert result.envelopes == (envelope,)


def test_the_result_json_shape_is_pinned(
    registry: SubscriptionRegistry, store: HistoryStore
) -> None:
    """What ``events watch --json`` (t9) renders; ``records``/``cursor``/``hasMore``
    are spelled exactly as :class:`~events_cli.history.HistoryPage` spells them."""
    factory = fake_drain_factory(
        session_present=True,
        deliveries=(message(event(1), 1), broken(b"nope", 2)),
    )
    payload = drain(registry, store, factory, max=2).to_dict()

    assert set(payload) == {
        "subscription",
        "records",
        "cursor",
        "hasMore",
        "sessionPresent",
        "consumed",
        "skipped",
        "stopped",
    }
    assert payload["subscription"] == SUB
    assert payload["cursor"] == 1
    assert payload["records"][0]["seq"] == 1
    assert set(payload["skipped"][0]) == {"topic", "reason"}


# =========================================================================
# Criterion 1e — a malformed payload is skipped, counted, acked; never fatal
# =========================================================================


@pytest.mark.parametrize(
    "payload",
    [
        b"{ not json at all",
        b'{"id": "evt_1"}',  # valid JSON, not a valid envelope
        b"[]",
        b"",
    ],
)
def test_a_malformed_payload_does_not_crash_the_drain(
    registry: SubscriptionRegistry, store: HistoryStore, payload: bytes
) -> None:
    """A shared broker means any producer can publish nonsense onto our filter."""
    envelope = event(1)
    factory = fake_drain_factory(
        session_present=True,
        deliveries=(broken(payload, 1), message(envelope, 2)),
    )

    result = drain(registry, store, factory, max=2)

    assert [record.envelope.id for record in result.records] == [envelope.id]
    assert len(result.skipped) == 1


def test_a_malformed_payload_is_acknowledged_so_it_cannot_poison_the_queue(
    registry: SubscriptionRegistry, store: HistoryStore
) -> None:
    """It will never parse on redelivery; leaving it unacked burns every later drain."""
    factory = fake_drain_factory(session_present=True, deliveries=(broken(b"nope", 4),))

    drain(registry, store, factory, max=1)

    assert factory.made[0].acks == [(4, QOS_AT_LEAST_ONCE)]
    assert store.read(SUB, 0, 10).records == ()


def test_a_malformed_payload_is_reported_not_silently_swallowed(
    registry: SubscriptionRegistry,
    store: HistoryStore,
    caplog: pytest.LogCaptureFixture,
) -> None:
    factory = fake_drain_factory(session_present=True, deliveries=(broken(b"nope", 4),))

    with caplog.at_level(logging.WARNING, logger="events_cli.subs"):
        result = drain(registry, store, factory, max=1)

    assert result.skipped == (
        SkippedMessage(topic="events/task/requested", reason=result.skipped[0].reason),
    )
    assert "not valid JSON" in result.skipped[0].reason
    assert any("events/task/requested" in record.message for record in caplog.records)


def test_a_malformed_payload_counts_against_max_so_a_flood_stays_bounded(
    registry: SubscriptionRegistry, store: HistoryStore
) -> None:
    """``--max`` bounds *messages consumed*, of which the batch holds the valid ones."""
    envelope = event(1)
    factory = fake_drain_factory(
        session_present=True,
        deliver_in_thread=False,
        deliveries=(broken(b"a", 1), broken(b"b", 2), message(envelope, 3)),
    )

    result = drain(registry, store, factory, max=2)

    assert result.consumed == 2
    assert result.records == ()
    assert len(result.skipped) == 2
    assert result.stopped == "max"
    assert store.get(envelope.id) is None


# =========================================================================
# Criterion 1f — every failure is a named SubsError the CLI maps to an exit code
# =========================================================================


def test_a_broker_that_is_not_there_is_a_named_error(
    registry: SubscriptionRegistry, store: HistoryStore
) -> None:
    factory = fake_drain_factory(connect_error=OSError("connection refused"))

    with pytest.raises(BrokerUnreachableError) as excinfo:
        drain(registry, store, factory)

    assert isinstance(excinfo.value, SubsError)
    assert "events up" in excinfo.value.remediation


def test_a_refused_session_is_a_named_error(
    registry: SubscriptionRegistry, store: HistoryStore
) -> None:
    factory = fake_drain_factory(refuse=True)
    with pytest.raises(SessionError):
        drain(registry, store, factory)


def test_draining_an_unregistered_subscription_is_a_named_error(
    registry: SubscriptionRegistry, store: HistoryStore
) -> None:
    factory = fake_drain_factory(session_present=True)
    with pytest.raises(UnknownSubscriptionError) as excinfo:
        drain_subscription("ghost", registry=registry, store=store, client_factory=factory)

    assert "events sub list" in excinfo.value.remediation
    assert factory.made == [], "an unknown name must never cost a broker connection"


def test_draining_an_unusable_name_is_a_named_error(
    registry: SubscriptionRegistry, store: HistoryStore
) -> None:
    with pytest.raises(SubscriptionValidationError):
        drain_subscription("../etc", registry=registry, store=store)


def test_every_drain_failure_is_a_subs_error_carrying_a_remediation(
    registry: SubscriptionRegistry, store: HistoryStore
) -> None:
    poison = event(1)
    failures = [
        (fake_drain_factory(connect_error=OSError("refused")), store, {}),
        (fake_drain_factory(refuse=True), store, {}),
        (
            fake_drain_factory(session_present=True, deliveries=(message(poison, 1),)),
            JournalStore(store, [], fail_on=poison.id),
            {},
        ),
    ]
    for factory, backing, kwargs in failures:
        with pytest.raises(SubsError) as excinfo:
            drain(registry, backing, factory, **kwargs)
        assert excinfo.value.remediation


def test_the_drain_error_is_documented_in_the_exit_code_table() -> None:
    """The taxonomy is extended, not paralleled: exit 2, in the same table."""
    assert "DrainError" in (subs_errors.__doc__ or "")
    assert issubclass(DrainError, SubsError)


# =========================================================================
# Criterion 2 — pure and dockerless; the live-broker path is t12's
# =========================================================================


def test_the_drain_module_is_covered_by_the_lazy_import_guards() -> None:
    """The static paho/CLI guards glob the package — a new module must not escape."""
    assert any(path.name == "drain.py" for path in subs_source_files())


def test_the_drain_names_no_container_runtime_and_no_hard_coded_broker() -> None:
    """Nothing here shells out, and the address is injected, never literal."""
    path = Path(__file__).resolve().parent.parent / "events_cli" / "subs" / "drain.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])

    assert "subprocess" not in imported
    assert "socket" not in imported
    assert "paho" not in imported

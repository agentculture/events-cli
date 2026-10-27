"""Broker round-trip of the envelope content marks (#15) — the ``stack`` lane.

Issue #15 has three acceptance criteria, and only the first needs a broker:

1. **Marks survive a real round trip.** An envelope carrying ``visibility``,
   ``origin``, ``fidelity`` and ``timestamps`` is published at QoS 1 through
   :meth:`events_cli.client.EventClient.publish_event`, captured by a durable
   subscription and drained back with all four marks intact. Proven here by
   :func:`test_content_marks_survive_a_durable_broker_round_trip`.
2. **The vocabulary is enforced on write.** ``Envelope.new()`` refuses a word
   outside the vocabulary and timestamps reuse the UTC-only RFC 3339 rule.
   Dockerless; proven in ``tests/test_envelope.py`` by
   ``test_new_refuses_a_mark_outside_the_vocabulary`` and
   ``test_timestamp_at_reuses_the_utc_only_rfc3339_rule``.
3. **Unmarked envelopes stay valid.** An envelope with no marks is accepted and
   reads back as unknown rather than defaulted. Dockerless; proven in
   ``tests/test_envelope.py`` by
   ``test_an_unmarked_envelope_is_valid_and_reads_back_as_unknown``.

Isolation: the broker comes from ``broker_factory`` (a throwaway container on an
ephemeral loopback port, never 1883), and the registry and history live under
``tmp_path`` — the same lane construction as ``tests/test_subs_integration.py``.
The same gates apply: the ``stack`` marker and the ``EVENTS_STACK_IT=1`` opt-in.
"""

from __future__ import annotations

import secrets
import time
from pathlib import Path

import pytest

from events_cli.client import EventClient
from events_cli.core.envelope import Envelope, Timestamp
from events_cli.core.topics import type_to_topic
from events_cli.history import open_store
from events_cli.subs import (
    REGISTRY_DIRNAME,
    BrokerAddress,
    SubscriptionRegistry,
    add_subscription,
    drain_subscription,
    remove_subscription,
)


@pytest.mark.stack
def test_content_marks_survive_a_durable_broker_round_trip(broker_factory, tmp_path: Path) -> None:
    """visibility/origin/fidelity/timestamps come back from the broker unchanged."""
    broker = broker_factory()
    history_root = tmp_path / "history"
    address = BrokerAddress(broker.host, broker.port)
    registry = SubscriptionRegistry(history_root / REGISTRY_DIRNAME)
    store = open_store(history_root)

    suffix = secrets.token_hex(4)
    name = f"marks-{suffix}"
    event_type = f"marks.r{suffix}"
    sent = Envelope.new(
        event_type,
        "agent://events-cli-it",
        data={"note": "marked"},
        visibility="private",
        origin="human",
        fidelity="transcribed",
        timestamps=[
            Timestamp(type="recorded", at="2026-10-27T09:00:00Z", by="client"),
            Timestamp(type="transcribed", at="2026-10-27T09:00:02.5Z"),
        ],
    )

    add_subscription(name, "marks.*", address=address, registry=registry)
    try:
        with EventClient(broker.host, broker.port) as client:
            # connect() returns at once; paho's background loop owns the socket.
            deadline = time.monotonic() + 10.0
            while not client.is_connected and time.monotonic() < deadline:
                time.sleep(0.05)
            result = client.publish_event(sent, type_to_topic(event_type), qos=1, wait=10.0)
            assert result.ok, f"publish failed: {result.reason}"

        drained = drain_subscription(
            name,
            address=address,
            registry=registry,
            store=store,
            max=1,
            timeout=30.0,
        )
    finally:
        # Destroys the broker session (clean_start + zero expiry) then the record.
        remove_subscription(name, address=address, registry=registry, force=True)

    assert len(drained.records) == 1
    got = drained.records[0].envelope
    assert got == sent
    assert got.visibility == "private"
    assert got.origin == "human"
    assert got.fidelity == "transcribed"
    assert got.timestamps == (
        Timestamp(type="recorded", at="2026-10-27T09:00:00Z", by="client"),
        Timestamp(type="transcribed", at="2026-10-27T09:00:02.5Z"),
    )

"""Docker-backed proof of the persistent-session architecture — the ``stack`` lane.

Everything in ``events_cli/subs/`` rests on claims about a **real broker** that no
dockerless test can check: that an MQTT 5 session with an infinite Session Expiry
Interval survives a broker restart with its queued QoS-1 backlog intact, that the
backlog is bounded and drops the *newest* arrival when it overflows, that a
session takeover is lossless rather than merely loud, and that a contract-lane
subscription cannot see a producer's own topic tree. Those are the properties
this file measures.

Isolation — read this before adding a test
==========================================
This suite runs on machines where ``127.0.0.1:1883`` carries **a production
event broker for a live robot** (container ``events-mosquitto``, compose project
``events-cli``). Every rule below is load-bearing:

* every broker comes from :func:`~tests.test_stack_integration.broker_factory`,
  which gives it a unique ``events-cli-it-<pid>-<rand>`` container name, its own
  named volume and an **ephemeral loopback port** — never 1883, never a name the
  real stack owns;
* no test here invokes ``events up`` / ``events down`` / ``docker compose``, and
  the only container any of them stops, restarts or removes is one the factory
  created in that same test;
* the CLI subprocesses are pointed at the throwaway broker with
  ``EVENTS_BROKER_HOST`` / ``EVENTS_BROKER_PORT``
  (:mod:`events_cli.address`) and at a ``tmp_path`` store with
  ``EVENTS_HISTORY_DIR``, and :func:`_cli_env` **asserts** it is not addressing
  1883 before it hands the environment over;
* :func:`test_the_throwaway_broker_is_isolated_from_the_production_stack` states
  those invariants as an assertion rather than a comment, and
  ``tests/conftest.py`` re-reads the production container's id and start time
  after the whole session, so "nothing restarted it" is measured, not claimed.

Both gates from ``tests/test_stack_integration.py`` apply unchanged: the
``stack`` marker (deselected by ``addopts``, so the dockerless quality gate never
sees these) and the ``EVENTS_STACK_IT=1`` opt-in plus a usable docker + image,
so ``pytest -m stack`` on a machine without docker skips cleanly.

How events get published
------------------------
Through ``mosquitto_pub`` **inside the broker container**, the pattern the sibling
suite established (the host has no mosquitto client tools). Bulk publishes use
``mosquitto_pub -l``, which reads one message per stdin line and exits only once
every PUBACK is in — so a publish is synchronous and ordered, with no "did it
actually leave?" race to sleep around. The payloads are real
:class:`~events_cli.core.envelope.Envelope` objects serialised with
:meth:`~events_cli.core.envelope.Envelope.to_json`, i.e. byte-for-byte what
:class:`events_cli.client.EventClient` puts on the wire. The client lane itself
is exercised end-to-end by the CLI round-trip test, which runs the real ``events
emit``.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Sequence

import pytest

from events_cli.core.envelope import Envelope
from events_cli.core.topics import type_to_topic
from events_cli.history import HistoryRecord, HistoryStore, open_store
from events_cli.stack import (
    BROKER_PORT,
    CONTAINER_NAME,
    LOOPBACK_ADDR,
    MOSQUITTO_CONF_FILENAME,
    PROJECT_NAME,
    template_text,
)
from events_cli.subs import (
    REGISTRY_DIRNAME,
    STOPPED_MAX,
    STOPPED_TIMEOUT,
    BrokerAddress,
    SubscriptionRegistry,
    add_subscription,
    drain_subscription,
)
from tests.conftest import inspect_production_broker

# The `broker_factory` fixture is NOT imported here — `tests/conftest.py`
# re-exports it, which is how pytest shares a fixture and keeps the one broker
# lifecycle in the file where it was proven. Only the type comes from there.
from tests.test_stack_integration import ThrowawayBroker

_REPO_ROOT = Path(__file__).resolve().parent.parent

#: Every contract-lane event in this file is minted by this source.
_SOURCE = "agent://events-cli-it"

#: How long a live drain waits when it must sit out an empty queue to prove a
#: negative ("nothing else arrives"). Long enough to absorb a contended host,
#: short enough that a handful of them do not dominate the run.
_IDLE_TIMEOUT = 15.0

#: How long a drain that is expected to fill its `max` may take. It returns as
#: soon as the bound is met, so this is only a backstop.
_BUSY_TIMEOUT = 60.0

#: How long a CLI subprocess gets. Generous: it absorbs interpreter start-up on
#: a contended host, and `events watch` carries its own `--timeout` inside it.
_CLI_TIMEOUT = 180.0


# --- lane construction ------------------------------------------------------


def _lane(
    broker: ThrowawayBroker, root: Path
) -> tuple[BrokerAddress, SubscriptionRegistry, HistoryStore]:
    """The three seams every domain call in this file takes explicitly.

    Address, registry and store are passed in rather than resolved from the
    environment, so an in-process test can never reach the per-host default
    store — or the default broker. The layout mirrors what the CLI would build
    from ``EVENTS_HISTORY_DIR`` (registry inside the history root), so the
    subprocess round-trip test and the in-process tests describe the same thing.
    """
    history_root = root / "history"
    return (
        BrokerAddress(broker.host, broker.port),
        SubscriptionRegistry(history_root / REGISTRY_DIRNAME),
        open_store(history_root),
    )


def _envelopes(count: int, *, event_type: str = "task.requested", start: int = 0) -> list[Envelope]:
    """``count`` distinct events carrying their own position in ``data.n``.

    The ordinal is what makes "in order" and "the oldest survived" checkable
    without relying on ids, which are ULIDs with no intra-millisecond ordering.
    """
    return [Envelope.new(event_type, _SOURCE, data={"n": start + index}) for index in range(count)]


def _publish(
    broker: ThrowawayBroker, topic: str, payloads: Sequence[str], *, retain: bool = False
) -> None:
    """Publish each payload to ``topic`` at QoS 1, synchronously and in order.

    ``mosquitto_pub -l`` sends one message per stdin line over a single
    connection and exits only when every PUBACK has landed — so on return the
    broker owns all of them, in publish order, and the test never has to sleep.
    """
    args = ["mosquitto_pub", "-h", "127.0.0.1", "-t", topic, "-q", "1", "-l"]
    if retain:
        args.append("-r")
    result = broker.exec_input(*args, stdin_text="".join(f"{line}\n" for line in payloads))
    assert result.returncode == 0, f"mosquitto_pub failed: {result.stderr.strip()}"


def _publish_events(broker: ThrowawayBroker, envelopes: Sequence[Envelope]) -> None:
    """Publish envelopes of ONE type to that type's canonical contract topic."""
    types = {envelope.type for envelope in envelopes}
    assert len(types) == 1, "publish one event type per call — the topic is derived from it"
    _publish(broker, type_to_topic(envelopes[0].type), [e.to_json() for e in envelopes])


def _ids(records: Sequence[HistoryRecord]) -> list[str]:
    return [record.envelope.id for record in records]


def _ordinals(records: Sequence[HistoryRecord]) -> list[int]:
    return [record.envelope.data["n"] for record in records]


# --- isolation, stated as an assertion --------------------------------------


@pytest.mark.stack
def test_the_throwaway_broker_is_isolated_from_the_production_stack(broker_factory) -> None:
    """The factory's broker shares no name, volume or port with the real stack.

    The comments in both suites say this; this fails the run if it stops being
    true. Port is the one that matters most: docker's published-port DNAT would
    happily fight the production broker for 1883, and a test that bound it would
    take a live robot's nervous system off the air.
    """
    broker = broker_factory()

    assert broker.port != BROKER_PORT, "a test broker must never bind the real stack's port"
    assert broker.host == LOOPBACK_ADDR, "a test broker must never be published off loopback"
    assert broker.container != CONTAINER_NAME
    assert not broker.container.startswith(CONTAINER_NAME)
    assert broker.volume != f"{PROJECT_NAME}_events-data"
    assert broker.container.startswith("events-cli-it-")
    assert broker.volume.startswith("events-cli-it-data-")

    # And the production broker, if this host runs one, is still exactly as it
    # was. tests/conftest.py repeats this across the whole session; here it is a
    # point check taken while a throwaway broker is actually up.
    production = inspect_production_broker()
    if production is not None:
        assert " running " in f" {production} ", f"{CONTAINER_NAME} is not running: {production}"


# --- 1. the session and its backlog survive a broker restart ----------------


@pytest.mark.stack
def test_queued_events_survive_a_broker_restart_in_order(broker_factory, tmp_path: Path) -> None:
    """Register, publish with NO drainer connected, restart the broker, drain: all N, in order.

    This is the single claim the whole "no resident control service" architecture
    rests on (``events_cli/subs/session.py``): the broker's own persistence is
    the buffer, so an infinite Session Expiry Interval plus ``persistence true``
    plus a mounted volume must carry both the *session* and its *queued QoS-1
    backlog* across a restart. ``session_present`` is asserted as well as the
    payloads, because a lost session would silently re-subscribe and return an
    empty batch — indistinguishable from "nothing was published" if only the
    records were checked.

    The restart is ``stop`` (SIGTERM, waited out) then ``start`` rather than
    ``docker restart``, for the reason the sibling suite documents: identical
    signals, but the clean-shutdown flush becomes observable instead of racing an
    opaque single command. The container restarted is the throwaway one this test
    created, and no other.
    """
    broker = broker_factory()
    address, registry, store = _lane(broker, tmp_path)
    add_subscription("restart", "task.*", address=address, registry=registry)

    events = _envelopes(12)
    _publish_events(broker, events)

    broker.stop_clean()  # the flush happens here
    broker.start()  # returns only once MQTT is served again

    result = drain_subscription(
        "restart",
        address=address,
        registry=registry,
        store=store,
        max=len(events),
        timeout=_BUSY_TIMEOUT,
    )

    assert result.session_present is True, (
        "the persistent session did NOT survive the broker restart — the backlog "
        "buffer this architecture depends on is gone, not merely empty"
    )
    assert _ids(result.records) == [event.id for event in events]
    assert _ordinals(result.records) == list(range(len(events)))
    assert result.skipped == ()


# --- 2. bounded drain, and an exact cursor resume ---------------------------


@pytest.mark.stack
def test_a_bounded_drain_resumes_from_its_cursor_without_redelivering(
    broker_factory, tmp_path: Path
) -> None:
    """A drain honours ``max`` and its deadline; resuming from its cursor repeats nothing.

    Two properties in one flow because they are one contract: the batch is
    bounded *and* the cursor it returns is enough to continue from. The first
    drain stops at ``max`` well inside its timeout; the second resumes from the
    returned cursor and receives strictly the events the first did not — proved
    by disjoint id sets, not by counting, since a duplicate would still count
    right if the store had assigned it a second sequence.

    The redelivery being tested is the **broker's**: the first drain acknowledged
    what it took, so those messages are gone from the session queue. ``since`` is
    only a cursor floor on the store side (see
    :func:`events_cli.subs.drain.drain_subscription`), so if acknowledgement were
    broken the second drain would hand back the first four again and the
    disjointness assertion would catch it.
    """
    broker = broker_factory()
    address, registry, store = _lane(broker, tmp_path)
    add_subscription("bounded", "task.*", address=address, registry=registry)

    events = _envelopes(10)
    _publish_events(broker, events)

    started = time.monotonic()
    first = drain_subscription(
        "bounded",
        address=address,
        registry=registry,
        store=store,
        max=4,
        timeout=_IDLE_TIMEOUT,
    )
    elapsed = time.monotonic() - started

    assert elapsed < _IDLE_TIMEOUT, "a drain that filled its max must not wait out the timeout"
    assert len(first.records) == 4
    assert first.stopped == STOPPED_MAX
    assert first.has_more is True
    assert first.cursor == first.records[-1].seq
    assert _ordinals(first.records) == [0, 1, 2, 3]

    second = drain_subscription(
        "bounded",
        since=first.cursor,
        address=address,
        registry=registry,
        store=store,
        max=len(events),
        timeout=_IDLE_TIMEOUT,
    )

    assert set(_ids(second.records)).isdisjoint(_ids(first.records)), (
        "the resumed drain re-delivered events the first drain had already "
        "acknowledged — persist-then-ack is not acknowledging"
    )
    assert _ordinals(second.records) == [4, 5, 6, 7, 8, 9]
    assert second.cursor > first.cursor
    assert second.stopped == STOPPED_TIMEOUT  # the queue emptied before the bound
    assert second.has_more is False


# --- 3. overflow drops the newest, at exactly the configured bound ----------

#: A deliberately tiny queue bound for the overflow test. See the test docstring
#: for why testing at 20 rather than the template's 1000 is the same property.
_TEST_QUEUE_BOUND = 20
#: How far past the bound to publish. Enough that "the newest were dropped" and
#: "the oldest were dropped" give visibly different answers.
_TEST_QUEUE_OVERFLOW = 13


@pytest.mark.stack
def test_overflow_drops_the_newest_at_the_configured_bound(broker_factory, tmp_path: Path) -> None:
    """Publish ``bound + K`` to an offline session: exactly the OLDEST ``bound`` arrive, in order.

    Keeping this honest
    -------------------
    The shipped ``mosquitto.conf`` sets ``max_queued_messages 1000``; this test
    runs a throwaway broker configured with **20**. It is testing the behaviour
    that setting selects, not a different behaviour — three things keep that
    claim true rather than convenient:

    1. the knob is the same one, spelled the same way, in the same config file
       position — the throwaway broker's config is the sibling suite's template
       plus one ``max_queued_messages`` line;
    2. the property under test is *shape*, not magnitude: at the bound, an
       arrival is dropped and the queue's head is kept. Nothing about "which end
       is discarded" depends on the number, and the same shape was measured at
       1000 on 2026-07-24 (1200 published, exactly ``m00000..m00999`` delivered
       — the finding recorded in the template's own comments);
    3. the assertion below **reads the shipped template** and requires it to
       still configure an explicit numeric ``max_queued_messages``. If someone
       removes the setting, or replaces it with a different mechanism, this test
       fails rather than quietly continuing to prove something about a knob the
       product no longer uses.

    What this buys is runtime: proving the same property at 1000 costs a
    1200-message publish and a 1000-record drain per run, for an answer that is
    identical at 20. The cost is that the *specific number* 1000 is pinned by
    ``tests/test_stack_templates.py`` (a text assertion) rather than measured
    live here — which is the honest statement of what is and is not covered.
    """
    shipped = template_text(MOSQUITTO_CONF_FILENAME)
    assert re.search(r"^max_queued_messages \d+$", shipped, re.MULTILINE), (
        "the shipped mosquitto.conf no longer configures an explicit "
        "max_queued_messages — this test would be proving a property of a knob "
        "the product does not set"
    )

    broker = broker_factory(max_queued_messages=_TEST_QUEUE_BOUND)
    address, registry, store = _lane(broker, tmp_path)
    add_subscription("overflow", "task.*", address=address, registry=registry)

    total = _TEST_QUEUE_BOUND + _TEST_QUEUE_OVERFLOW
    events = _envelopes(total)
    _publish_events(broker, events)

    result = drain_subscription(
        "overflow",
        address=address,
        registry=registry,
        store=store,
        max=total,
        timeout=_IDLE_TIMEOUT,
    )

    assert len(result.records) == _TEST_QUEUE_BOUND, (
        f"expected exactly the configured bound of {_TEST_QUEUE_BOUND} queued messages, "
        f"got {len(result.records)}"
    )
    assert _ordinals(result.records) == list(range(_TEST_QUEUE_BOUND)), (
        "the queue did not keep the OLDEST messages in order — overflow behaviour "
        "is not drop-newest, and docs/contract.md's backlog section is wrong"
    )
    assert _ids(result.records) == [event.id for event in events[:_TEST_QUEUE_BOUND]]


# --- 4. a session takeover leaves the store exactly-once --------------------


@pytest.mark.stack
def test_concurrent_drains_leave_the_store_holding_every_event_exactly_once(
    broker_factory, tmp_path: Path
) -> None:
    """Two drains race for one subscription's session; afterwards nothing is duplicated or lost.

    A durable subscription is addressed by a **stable client id**, so a second
    drainer connecting kicks the first (Mosquitto logs ``session taken over``).
    That is the mechanism enforcing single-drainer semantics, and the reason
    ``events_cli/subs/drain.py`` persists **before** acknowledging: a drainer cut
    off mid-batch may have stored events it never acknowledged, and the survivor
    resumes the same session, receives them again, and
    :meth:`~events_cli.history.HistoryStore.append`'s dedupe-on-id folds the
    redelivery into the sequence already assigned.

    Two things are asserted, and the distinction is deliberate:

    * **no duplicates, immediately after the race.** This must hold with nothing
      else run in between — it is the dedupe half, and a sweep could not repair
      it. The two drains use *separate store handles* on one directory, the same
      shape two ``events watch`` processes have, so the dedupe being exercised is
      the cross-handle one (an advisory ``flock`` plus a re-read of the id file,
      see ``events_cli/history/jsonl.py``), not one instance's warm cache.
    * **no loss, after a bounded solo sweep.** Losslessness is the claim that
      nothing is *dropped* — never that one contended drainer necessarily
      collects everything inside its own ``--timeout``. A drainer that is kicked
      returns early with a partial batch by design, and whatever it did not
      acknowledge is still the broker's. The sweep is how that is observed; if
      persist-then-ack were inverted, the kicked drainer's unstored-but-acked
      events would be gone for good and the sweep would come back short.
    """
    broker = broker_factory()
    address, registry, _ = _lane(broker, tmp_path)
    history_root = tmp_path / "history"

    add_subscription("takeover", "task.*", address=address, registry=registry)
    events = _envelopes(30)
    _publish_events(broker, events)

    outcomes: dict[str, object] = {}

    def race(tag: str) -> None:
        try:
            outcomes[tag] = drain_subscription(
                "takeover",
                address=address,
                registry=registry,
                # A separate handle per "process": one directory, two stores.
                store=open_store(history_root),
                max=len(events),
                timeout=_IDLE_TIMEOUT,
            )
        except BaseException as exc:  # noqa: BLE001 - recorded, asserted on below
            outcomes[tag] = exc

    threads = [threading.Thread(target=race, args=(tag,), name=tag) for tag in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=_IDLE_TIMEOUT * 4)
    assert not any(thread.is_alive() for thread in threads), "a racing drain never returned"

    # A takeover must not surface as a crash in either racer: being kicked is a
    # normal outcome of the single-drainer rule, not an error.
    for tag, outcome in outcomes.items():
        assert not isinstance(outcome, BaseException), f"drain {tag} raised: {outcome!r}"

    audit = open_store(history_root)
    after_race = audit.read("takeover", 0, len(events) * 2).records
    seen = _ids(after_race)
    assert len(seen) == len(set(seen)), (
        "the store holds a DUPLICATE after a session takeover — dedupe-on-id did "
        f"not hold across two concurrent drains: {sorted(seen)}"
    )

    # Bounded solo sweep: whatever the race left unacknowledged is still queued.
    drain_subscription(
        "takeover",
        address=address,
        registry=registry,
        store=audit,
        max=len(events),
        timeout=_IDLE_TIMEOUT,
    )

    final = audit.read("takeover", 0, len(events) * 2).records
    final_ids = _ids(final)
    assert len(final_ids) == len(set(final_ids)), "the sweep introduced a duplicate"
    assert set(final_ids) == {event.id for event in events}, (
        "an event was LOST across the takeover — persist-then-ack should make a "
        "kicked drainer's unacknowledged work redeliverable"
    )
    assert [record.seq for record in final] == list(range(1, len(events) + 1))


# --- 5. the capture boundary ------------------------------------------------


@pytest.mark.stack
def test_only_events_published_after_a_subscription_exists_are_captured(
    broker_factory, tmp_path: Path
) -> None:
    """An event published before ``sub add`` is never in a later drain; one published after is.

    The consequence consumers most often get wrong, and the reason
    ``events get``/``events list`` can return nothing for an event that was
    definitely published: the store holds what a **registered subscription's
    session queued**, and a session that did not exist queued nothing. There is
    no replay of the broker's past — retained messages are the last value on a
    topic, not a log (``docs/contract.md``).

    Both events are the same type on the same topic, so the only difference
    between them is *when* they were published relative to the subscription.
    """
    broker = broker_factory()
    address, registry, store = _lane(broker, tmp_path)

    before, after = _envelopes(2)
    _publish_events(broker, [before])

    add_subscription("boundary", "task.*", address=address, registry=registry)

    _publish_events(broker, [after])

    result = drain_subscription(
        "boundary",
        address=address,
        registry=registry,
        store=store,
        max=10,
        timeout=_IDLE_TIMEOUT,
    )

    captured = _ids(result.records)
    assert after.id in captured, "the event published after 'sub add' was not captured"
    assert before.id not in captured, (
        "an event published BEFORE the subscription existed was captured — the "
        "broker cannot queue for a session that did not exist, so this would mean "
        "the drain is reading something other than its own session"
    )
    assert captured == [after.id]
    assert store.get(before.id) is None


# --- 6. two CLI processes, an emit -> watch round-trip ----------------------


def _events_cmd() -> list[str]:
    """The real ``events`` console script when it is installed, else ``python -m``.

    The console script is the honest surface — it is what an operator and an
    agent actually type, and it exercises ``[project.scripts]`` — so it is
    preferred. The module fallback keeps the test meaningful (still a separate
    real process running the same parser) on a tree that was never installed.
    """
    console = Path(sys.executable).parent / "events"
    if console.is_file() and os.access(console, os.X_OK):
        return [str(console)]
    return [sys.executable, "-m", "events_cli"]


def _cli_env(broker: ThrowawayBroker, root: Path) -> dict[str, str]:
    """The environment that points a CLI process at the THROWAWAY broker and store.

    The assertions are the safety gate, not decoration: without the override
    every ``events`` process resolves ``127.0.0.1:1883``
    (:mod:`events_cli.address`), which on this host is the production broker.
    Refusing to build an environment that names 1883 makes "a subprocess talked
    to production" a test failure rather than a silent side effect.
    """
    env = dict(os.environ)
    env["EVENTS_BROKER_HOST"] = broker.host
    env["EVENTS_BROKER_PORT"] = str(broker.port)
    env["EVENTS_HISTORY_DIR"] = str(root / "history")
    env["PYTHONPATH"] = str(_REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")

    assert env["EVENTS_BROKER_PORT"] != str(BROKER_PORT), "refusing to point a CLI at 1883"
    assert env["EVENTS_BROKER_HOST"] == LOOPBACK_ADDR
    return env


def _run_cli(args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess:
    """One real ``events`` invocation, with stdin closed and a hard time bound."""
    return subprocess.run(
        [*_events_cmd(), *args],
        capture_output=True,
        text=True,
        timeout=_CLI_TIMEOUT,
        env=env,
        cwd=str(_REPO_ROOT),
        stdin=subprocess.DEVNULL,
    )


def _json_stdout(proc: subprocess.CompletedProcess, verb: str) -> dict:
    assert proc.returncode == 0, f"'{verb}' exited {proc.returncode}\nstderr: {proc.stderr}"
    # The stdout/stderr split is part of the contract: a --json result must be
    # parseable on its own, with no diagnostics mixed in.
    return json.loads(proc.stdout)


@pytest.mark.stack
def test_two_cli_processes_complete_an_emit_to_watch_round_trip(
    broker_factory, tmp_path: Path
) -> None:
    """``events emit`` in one process, ``events watch`` in another — the event arrives.

    The end-to-end proof that the four moving parts agree in the shape a caller
    actually uses: separate OS processes, the installed console script, the real
    :class:`~events_cli.client.EventClient` publishing at QoS 1, a persistent
    session created by an earlier process, and a store on disk shared through
    ``EVENTS_HISTORY_DIR``. Nothing is monkeypatched and no seam is injected —
    which is exactly why it needs the ``EVENTS_BROKER_HOST``/``PORT`` override
    to exist at all: without it these processes could only ever address 1883.

    A correlation id is passed through ``emit`` and asserted on the far side, so
    the test proves the *envelope* survived the round trip rather than merely
    that something with the right id did.
    """
    broker = broker_factory()
    env = _cli_env(broker, tmp_path)
    marker = f"it-{secrets.token_hex(4)}"

    added = _json_stdout(_run_cli(["sub", "add", "roundtrip", "task.*", "--json"], env), "sub add")
    assert added["name"] == "roundtrip"
    assert added["filter"] == "events/task/+"

    data_file = tmp_path / "payload.json"
    data_file.write_text(json.dumps({"marker": marker}), encoding="utf-8")
    emitted = _json_stdout(
        _run_cli(
            [
                "emit",
                "task.requested",
                "--data",
                str(data_file),
                "--correlation-id",
                marker,
                "--json",
            ],
            env,
        ),
        "emit",
    )
    assert emitted["publish"]["ok"] is True, emitted["publish"]
    assert emitted["qos"] == 1
    assert emitted["topic"] == "events/task/requested"
    event_id = emitted["event"]["id"]

    watched = _json_stdout(
        _run_cli(
            ["watch", "roundtrip", "--max", "5", "--timeout", str(int(_IDLE_TIMEOUT)), "--json"],
            env,
        ),
        "watch",
    )

    assert watched["subscription"] == "roundtrip"
    assert watched["sessionPresent"] is True, "watch did not resume the session sub add created"
    events = [record["event"] for record in watched["records"]]
    assert [event["id"] for event in events] == [event_id], watched
    assert events[0]["data"] == {"marker": marker}
    assert events[0]["correlationId"] == marker
    assert watched["cursor"] >= 1

    # And a second process resuming from the returned cursor sees it again from
    # the store alone — no broker round trip, `servedFrom` says so.
    replay = _json_stdout(
        _run_cli(["watch", "roundtrip", "--since", "0", "--max", "1", "--json"], env), "watch"
    )
    assert replay["servedFrom"] == "history"
    assert [record["event"]["id"] for record in replay["records"]] == [event_id]


# --- 7. producer-owned topic trees are never captured -----------------------


@pytest.mark.stack
def test_producer_owned_topic_trees_are_never_captured(broker_factory, tmp_path: Path) -> None:
    """``reachy/events/...`` and retained ``reachy/state/...`` are invisible to every drain.

    The raw MQTT port is a first-class surface: ``reachy-mini-cli`` publishes
    into its **own** topic trees from a 50 Hz control loop and does not
    participate in the contract lane. That separation is enforced structurally —
    every contract topic and filter is ``events/``-prefixed, and a dotted pattern
    compiles only to the single-level ``+``, never ``#``
    (:mod:`events_cli.core.topics`) — but "structurally impossible" is a claim
    about a live broker's subscription matching, so it is measured here.

    The two producer publishes are **valid envelopes**, not junk: if a filter did
    reach them they would parse, store and appear in the batch. Junk would only
    ever land in ``skipped``, which is a weaker result — it would prove the
    payload was rejected, not that the topic was never matched. ``skipped`` is
    asserted empty for the same reason.

    Both subscriptions here are as wide as the contract lane allows: ``*.*``
    compiles to ``events/+/+`` and ``reachy.*`` to ``events/reachy/+``. Neither
    can spell its way out of the prefix.
    """
    broker = broker_factory()
    address, registry, store = _lane(broker, tmp_path)

    add_subscription("wide", "*.*", address=address, registry=registry)
    add_subscription("reachy-lane", "reachy.*", address=address, registry=registry)

    task_event, reachy_event = (
        Envelope.new("task.requested", _SOURCE, data={"n": 0}),
        Envelope.new("reachy.moved", _SOURCE, data={"n": 1}),
    )
    # Producer-owned trees: same shape of payload, entirely different topics.
    producer_live = Envelope.new("head.moved", "app://reachy-mini-cli", data={"angle": 12})
    producer_state = Envelope.new("pose.updated", "app://reachy-mini-cli", data={"yaw": 0.5})

    _publish(broker, "reachy/events/head/moved", [producer_live.to_json()])
    _publish(broker, "reachy/state/pose", [producer_state.to_json()], retain=True)
    _publish_events(broker, [task_event])
    _publish_events(broker, [reachy_event])

    forbidden = {producer_live.id, producer_state.id}

    wide = drain_subscription(
        "wide",
        address=address,
        registry=registry,
        store=store,
        max=10,
        timeout=_IDLE_TIMEOUT,
    )
    assert _ids(wide.records) == [task_event.id, reachy_event.id], (
        "the 'events/+/+' subscription did not capture the contract-lane events — "
        "an empty result would make the negative assertions below vacuous"
    )
    assert forbidden.isdisjoint(_ids(wide.records))
    assert wide.skipped == ()

    narrow = drain_subscription(
        "reachy-lane",
        address=address,
        registry=registry,
        store=store,
        max=10,
        timeout=_IDLE_TIMEOUT,
    )
    assert _ids(narrow.records) == [
        reachy_event.id
    ], "'reachy.*' must capture the contract-lane 'reachy.moved' and nothing else"
    assert forbidden.isdisjoint(_ids(narrow.records))
    assert narrow.skipped == ()

    # Nothing from a producer tree reached the store by any route.
    for event_id in forbidden:
        assert (
            store.get(event_id) is None
        ), f"a producer-owned publish ({event_id}) was captured by the contract lane"

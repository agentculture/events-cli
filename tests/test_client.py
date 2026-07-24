"""Tests for the importable publish client (``events_cli.client``).

The default selection here NEVER needs a live broker and NEVER asserts
wall-clock timing — those live behind the ``perf`` and ``stack`` markers, which
``addopts`` deselects. Everything below connects to a *dead* port so "broker
unreachable" is the case under test; nothing here talks to a real broker (in
particular never the host's 1883).
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
import time
from unittest.mock import MagicMock

import pytest

from events_cli.client import (
    ConnectionState,
    EventClient,
    MqttDependencyError,
    PublishResult,
    Will,
)
from events_cli.core.envelope import Envelope

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _dead_port() -> int:
    """A TCP port on loopback that nothing is listening on."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture
def dead_port() -> int:
    return _dead_port()


# --- never raises on an unreachable broker ---------------------------------


def test_construct_against_dead_broker_never_raises(dead_port: int) -> None:
    client = EventClient("127.0.0.1", dead_port)
    try:
        assert client.is_connected is False
        assert client.state in (ConnectionState.CONNECTING, ConnectionState.DISCONNECTED)
    finally:
        client.close()


def test_publish_while_disconnected_returns_not_raises(dead_port: int) -> None:
    with EventClient("127.0.0.1", dead_port) as client:
        result = client.publish("reachy/events/head/moved", "{}")
    assert isinstance(result, PublishResult)
    assert result.ok is False
    assert result.connected is False
    assert "conn" in result.reason


def test_publish_accepts_str_bytes_mapping_and_envelope(dead_port: int) -> None:
    env = Envelope.new("head.moved", "app://reachy-mini-cli", data={"angle": 12})
    payloads = ["hello", b"\x00\x01\x02", {"k": "v"}, env, None]
    with EventClient("127.0.0.1", dead_port) as client:
        results = [client.publish("reachy/events/a/b", p) for p in payloads]
    assert all(isinstance(r, PublishResult) for r in results)  # never raised


def test_close_is_idempotent_and_never_raises(dead_port: int) -> None:
    client = EventClient("127.0.0.1", dead_port)
    client.close()
    client.close()  # a second close must be a no-op, not an error
    assert client.state is ConnectionState.CLOSED


# --- retained publishes, QoS, and the envelope wire form -------------------


def test_retained_and_qos_reach_paho(dead_port: int, monkeypatch: pytest.MonkeyPatch) -> None:
    import paho.mqtt.client as mqtt

    seen: list[tuple] = []

    def spy(topic, payload=None, qos=0, retain=False, properties=None):
        seen.append((topic, qos, retain))
        return mqtt.MQTTMessageInfo(1)

    with EventClient("127.0.0.1", dead_port) as client:
        monkeypatch.setattr(client._paho, "publish", spy)
        result = client.publish("reachy/state/online", "online", qos=0, retain=True)
    assert seen == [("reachy/state/online", 0, True)]
    assert result.ok is True  # our spy returned a success MQTTMessageInfo


def test_publish_event_serialises_the_envelope_wire_form(
    dead_port: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    import paho.mqtt.client as mqtt

    env = Envelope.new("head.moved", "app://reachy-mini-cli", data={"angle": 12})
    seen: dict = {}

    def spy(topic, payload=None, qos=0, retain=False, properties=None):
        seen.update(topic=topic, payload=payload, qos=qos, retain=retain)
        return mqtt.MQTTMessageInfo(1)

    with EventClient("127.0.0.1", dead_port) as client:
        monkeypatch.setattr(client._paho, "publish", spy)
        client.publish_event(env, "reachy/events/head/moved")
    assert seen["topic"] == "reachy/events/head/moved"
    assert seen["payload"] == env.to_json()
    assert seen["qos"] == 0
    assert seen["retain"] is False


# --- Last Will / availability ----------------------------------------------


def test_availability_registers_lwt_before_connect(
    dead_port: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    import paho.mqtt.client as mqtt

    order: list[tuple] = []
    orig_will = mqtt.Client.will_set
    orig_conn = mqtt.Client.connect_async

    def rec_will(self, *a, **k):
        order.append(("will_set", a, k))
        return orig_will(self, *a, **k)

    def rec_conn(self, *a, **k):
        order.append(("connect_async", a, k))
        return orig_conn(self, *a, **k)

    monkeypatch.setattr(mqtt.Client, "will_set", rec_will)
    monkeypatch.setattr(mqtt.Client, "connect_async", rec_conn)

    client = EventClient("127.0.0.1", dead_port, availability_topic="reachy/state/online")
    try:
        names = [call[0] for call in order]
        assert "will_set" in names and "connect_async" in names
        assert names.index("will_set") < names.index("connect_async")
        will_call = next(call for call in order if call[0] == "will_set")
        assert will_call[1][0] == "reachy/state/online"
        assert will_call[2].get("retain") is True
    finally:
        client.close()


def _connack(success: bool = True):
    from paho.mqtt.packettypes import PacketTypes
    from paho.mqtt.reasoncodes import ReasonCode

    return ReasonCode(PacketTypes.CONNACK, "Success" if success else "Server unavailable")


def test_on_connect_announces_online_retained() -> None:
    client = EventClient(
        "127.0.0.1",
        1,
        availability_topic="reachy/state/online",
        online_payload="online",
        connect=False,
    )
    fake = MagicMock()
    client._on_connect(fake, None, {}, _connack(True), None)
    assert client.state is ConnectionState.CONNECTED
    fake.publish.assert_called_once()
    args, kwargs = fake.publish.call_args
    assert args[0] == "reachy/state/online"
    assert args[1] == "online"
    assert kwargs.get("retain") is True


def test_on_connect_failure_marks_disconnected_and_stays_quiet() -> None:
    client = EventClient("127.0.0.1", 1, availability_topic="reachy/state/online", connect=False)
    fake = MagicMock()
    client._on_connect(fake, None, {}, _connack(False), None)
    assert client.state is ConnectionState.DISCONNECTED
    fake.publish.assert_not_called()  # no online announce on a refused connect


def test_on_disconnect_marks_disconnected() -> None:
    from paho.mqtt.packettypes import PacketTypes
    from paho.mqtt.reasoncodes import ReasonCode

    client = EventClient("127.0.0.1", 1, connect=False)
    client._state = ConnectionState.CONNECTED
    client._on_disconnect(MagicMock(), None, {}, ReasonCode(PacketTypes.DISCONNECT), None)
    assert client.state is ConnectionState.DISCONNECTED


def test_close_announces_offline_retained_when_connected(monkeypatch: pytest.MonkeyPatch) -> None:
    client = EventClient(
        "127.0.0.1",
        1,
        availability_topic="reachy/state/online",
        offline_payload="offline",
        connect=False,
    )
    seen: list = []
    monkeypatch.setattr(client._paho, "is_connected", lambda: True)
    monkeypatch.setattr(client._paho, "publish", lambda *a, **k: seen.append(("publish", a, k)))
    monkeypatch.setattr(client._paho, "disconnect", lambda: seen.append(("disconnect",)))
    client.close()
    assert seen[0][0] == "publish"
    assert seen[0][1][0] == "reachy/state/online"
    assert seen[0][1][1] == "offline"
    assert seen[0][2].get("retain") is True
    assert ("disconnect",) in seen
    assert client.state is ConnectionState.CLOSED


# --- O(1) enqueue: the background loop owns the socket ----------------------


def test_loop_is_running_after_construction(dead_port: int) -> None:
    with EventClient("127.0.0.1", dead_port) as client:
        assert client.loop_running is True
    assert client.loop_running is False  # stopped after close


def test_publish_from_a_worker_thread_never_raises(dead_port: int) -> None:
    results: list = []
    errors: list = []

    with EventClient("127.0.0.1", dead_port) as client:
        assert client.loop_running is True

        def worker() -> None:
            try:
                for _ in range(200):
                    results.append(client.publish("reachy/events/x/y", "{}"))
            except BaseException as exc:  # pragma: no cover - the contract is never-raise
                errors.append(exc)

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join(10)
    assert not errors
    assert len(results) == 200
    assert all(isinstance(r, PublishResult) for r in results)


# --- unique client ids ------------------------------------------------------


def test_default_client_ids_are_unique_per_process() -> None:
    a = EventClient("127.0.0.1", 1, connect=False)
    b = EventClient("127.0.0.1", 1, connect=False)
    assert a.client_id != b.client_id
    assert a.client_id.startswith("events-cli-")
    assert str(os.getpid()) in a.client_id


def test_explicit_client_id_is_respected() -> None:
    client = EventClient("127.0.0.1", 1, client_id="reachy-head", connect=False)
    assert client.client_id == "reachy-head"


# --- config validation raises (genuine programmer error) -------------------


@pytest.mark.parametrize(
    "override",
    [
        {"host": ""},
        {"host": 123},
        {"port": 0},
        {"port": 70000},
        {"port": True},
        {"keepalive": 0},
    ],
)
def test_invalid_config_raises_valueerror(override: dict) -> None:
    kwargs = {"host": "127.0.0.1", "port": 1, "connect": False}
    kwargs.update(override)
    with pytest.raises(ValueError):
        EventClient(**kwargs)


def test_will_and_availability_together_is_a_programmer_error() -> None:
    # Built outside the raises block so the ValueError can only have come from
    # EventClient — inside, a Will() that threw would pass this test for the
    # wrong reason.
    will = Will("a/b")
    with pytest.raises(ValueError):
        EventClient(
            "127.0.0.1",
            1,
            connect=False,
            will=will,
            availability_topic="reachy/state/online",
        )


# --- the lazy paho boundary -------------------------------------------------


def test_constructing_without_paho_raises_named_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """With paho blocked, constructing the client names the missing package clearly."""
    for name in ("paho", "paho.mqtt", "paho.mqtt.client"):
        monkeypatch.setitem(sys.modules, name, None)
    with pytest.raises(MqttDependencyError) as excinfo:
        EventClient("127.0.0.1", 1, connect=False)
    assert "paho-mqtt" in str(excinfo.value)


_VERBS = (
    ["whoami", "--json"],
    ["learn", "--json"],
    ["explain", "whoami"],
    ["overview", "--json"],
    ["doctor", "--json"],
    ["cli", "overview", "--json"],
)

_LAZY_SNIPPET = """
import sys
assert "paho" not in sys.modules
import events_cli
from events_cli.cli import main
for argv in {verbs!r}:
    assert main(argv) in (0, 1), argv
assert "paho" not in sys.modules, "an introspection verb imported paho"
import events_cli.client  # noqa: F401
assert "paho" not in sys.modules, "importing events_cli.client eagerly imported paho"
print("LAZY_OK")
""".format(verbs=list(_VERBS))


_ABSENT_SNIPPET = """
import sys
for name in ("paho", "paho.mqtt", "paho.mqtt.client"):
    sys.modules[name] = None
try:
    import paho  # noqa: F401
except ImportError:
    pass
else:
    raise SystemExit("paho importable despite the block")
import events_cli
from events_cli.cli import main
for argv in {verbs!r}:
    assert main(argv) in (0, 1), argv
cls = events_cli.EventClient
from events_cli.client import MqttDependencyError
try:
    cls("127.0.0.1", 1, connect=False)
except MqttDependencyError as exc:
    assert "paho-mqtt" in str(exc), exc
else:
    raise SystemExit("expected MqttDependencyError with paho absent")
print("ABSENT_OK")
""".format(verbs=list(_VERBS))


def _run_snippet(snippet: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONPATH"] = _REPO_ROOT + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run(
        [sys.executable, "-c", snippet],
        capture_output=True,
        text=True,
        timeout=90,
        env=env,
    )


def test_introspection_path_never_imports_paho() -> None:
    """With paho available, importing events_cli + every verb must not load it."""
    proc = _run_snippet(_LAZY_SNIPPET)
    assert proc.returncode == 0, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    assert "LAZY_OK" in proc.stdout


def test_introspection_verbs_run_with_paho_absent() -> None:
    """With paho blocked, the verbs still pass and the client names the missing dep."""
    proc = _run_snippet(_ABSENT_SNIPPET)
    assert proc.returncode == 0, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    assert "ABSENT_OK" in proc.stdout


# --- marker-gated: the enqueue-latency bound (perf) ------------------------


@pytest.mark.perf
def test_publish_enqueue_latency_is_bounded(dead_port: int) -> None:
    """Enqueue latency from a non-owner thread stays well under a generous bound.

    Measures the reachy hot path: publish() from a worker thread on a client
    whose broker is unreachable — the exact "broker down, keep the 50 Hz loop
    running" case. This exercises the full publish() code (topic validation,
    payload encode, _send_publish) and returns at paho's ``_sock is None`` guard
    without any socket I/O on the caller thread. The connected path adds only a
    deque append plus a one-byte sockpair wake, both non-blocking, so this is a
    faithful lower bound on the enqueue cost.

    Bound: mean per-call < 5 ms. The real cost is on the order of microseconds;
    5 ms is a ~1000x margin chosen so the assertion is robust on a loaded CI
    runner, not a tight benchmark. It is the ONLY wall-clock assertion in the
    suite and is marker-gated out of the default selection.
    """
    iterations = 2000
    latencies: list[float] = []
    done = threading.Event()

    with EventClient("127.0.0.1", dead_port) as client:
        assert client.loop_running is True

        def worker() -> None:
            for _ in range(iterations):
                start = time.perf_counter()
                client.publish("reachy/events/head/moved", "{}")
                latencies.append(time.perf_counter() - start)
            done.set()

        thread = threading.Thread(target=worker)
        thread.start()
        assert done.wait(30), "perf worker did not finish in time"
        thread.join()

    mean = sum(latencies) / len(latencies)
    assert mean < 5e-3, f"mean enqueue {mean * 1e3:.4f} ms exceeded the 5 ms bound"


# --- marker-gated: two clients on a live broker (stack) --------------------


@pytest.mark.stack
def test_two_default_clients_connect_without_kicking_each_other() -> None:
    """Two default-constructed clients stay connected concurrently (needs a broker).

    Point it at a THROWAWAY broker with ``EVENTS_TEST_BROKER=host:port`` — never
    the host's real 1883. Skips when unset or unreachable. This proves unique
    default client ids prevent an MQTT id-collision disconnect: a broker kicks
    the older session when a second client presents the same id, so identical
    default ids would make the two clients fight.
    """
    target = os.environ.get("EVENTS_TEST_BROKER")
    if not target:
        pytest.skip("set EVENTS_TEST_BROKER=host:port (a throwaway broker) to run")
    host, _, port_text = target.partition(":")
    port = int(port_text or "1883")

    a = EventClient(host, port)
    b = EventClient(host, port)
    try:
        assert a.client_id != b.client_id
        deadline = time.time() + 10
        while time.time() < deadline and not (a.is_connected and b.is_connected):
            time.sleep(0.05)
        assert a.is_connected, "client A never connected"
        assert b.is_connected, "client B never connected"
        time.sleep(0.5)  # neither kicks the other a moment later
        assert a.is_connected and b.is_connected
    finally:
        a.close()
        b.close()

#!/usr/bin/env python3
"""The MQTT half of the issue-#3 acceptance run — emit, consume, and prove it.

Driven by ``scripts/acceptance-issue-3.sh``; runnable standalone for debugging.

This covers the criteria that need a live client rather than a shell probe:

* **criterion 3** — retained messages, Last Will and Testament, and QoS 0,
  exercised on ``reachy-mini-cli``'s own topic tree (``reachy/events/…`` and
  retained ``reachy/state/…``) rather than a synthetic one, because that tree is
  what the first consumer actually publishes.
* **criterion 4** — a ``paho-mqtt`` 2.x client connecting from loopback with no
  credentials.

It also proves the two things events-cli adds on top of "mosquitto works":
the :class:`~events_cli.core.Envelope` survives a real broker round-trip
byte-for-byte, and :meth:`~events_cli.client.EventClient.publish` is the O(1)
enqueue the 50 Hz control loop needs.

Every check prints ``PASS``/``FAIL`` and the script exits non-zero if any fail,
so it is usable as a gate and not just as a demo. Emits ``--json`` for the
delivery summary.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import statistics
import sys
import time
from typing import Any

from events_cli.client import EventClient, Will
from events_cli.core import Envelope

# The acceptance run targets the real broker on loopback 1883 — that IS criterion
# 1. The overrides exist so this file can be shaken out against a throwaway
# broker on an ephemeral port without spending the robot's service window; the
# shell orchestrator never sets them, so a real run cannot drift off-target.
HOST = os.environ.get("EVENTS_ACCEPTANCE_HOST", "127.0.0.1")
PORT = int(os.environ.get("EVENTS_ACCEPTANCE_PORT", "1883"))

#: reachy-mini-cli's real topic tree (issue #3), not a synthetic one.
STATE_ONLINE = "reachy/state/online"
STATE_POSE = "reachy/state/pose"
EVENT_TOPIC = "reachy/events/acceptance/check.requested"
#: events-cli's own lane, to show both trees coexist on one broker.
ENVELOPE_TOPIC = "events/acceptance/envelope"

#: Generous enough to survive a contended host; short enough to fail a broken run.
SETTLE = 2.0


class Results:
    """Accumulates check outcomes so one failure does not abort the rest."""

    def __init__(self) -> None:
        self.checks: list[dict[str, Any]] = []

    def record(self, name: str, ok: bool, detail: str) -> None:
        self.checks.append({"check": name, "ok": ok, "detail": detail})
        print(f"  {'PASS' if ok else 'FAIL'}  {name}: {detail}", flush=True)

    @property
    def ok(self) -> bool:
        return all(c["ok"] for c in self.checks)


def _paho() -> Any:
    import paho.mqtt.client as mqtt

    return mqtt


def _subscriber(topics: list[str]) -> tuple[Any, list[Any]]:
    """A plain paho 2.x subscriber — deliberately NOT EventClient.

    Issue #3 criterion 4 asks for a stock ``paho-mqtt`` 2.x client connecting
    without credentials, so the consumer side stays unwrapped: if it worked only
    through our own class, the criterion would not be met.
    """
    mqtt = _paho()
    received: list[Any] = []
    sub = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2, client_id=f"acceptance-sub-{time.time_ns()}"
    )
    sub.on_message = lambda _c, _u, msg: received.append(msg)
    sub.connect(HOST, PORT, keepalive=30)  # no username/password: criterion 4
    sub.loop_start()
    for topic in topics:
        sub.subscribe(topic, qos=1)
    time.sleep(SETTLE)
    return sub, received


def check_paho_credentialless_connect(results: Results) -> None:
    """Criterion 4: paho-mqtt 2.x connects from loopback with no credentials."""
    mqtt = _paho()
    version = getattr(mqtt, "__version__", None) or _paho_version()
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="acceptance-credless")
    client.connect(HOST, PORT, keepalive=30)
    client.loop_start()
    deadline = time.time() + 10
    while time.time() < deadline and not client.is_connected():
        time.sleep(0.05)
    connected = client.is_connected()
    client.loop_stop()
    client.disconnect()
    results.record(
        "paho 2.x connects from loopback without credentials",
        connected and str(version).startswith("2"),
        f"paho-mqtt {version}, connected={connected}, no username/password sent",
    )


def _paho_version() -> str:
    from importlib.metadata import version

    return version("paho-mqtt")


def check_retained_and_qos0(results: Results) -> None:
    """Criterion 3: retained state and a QoS 0 event, on reachy's topic tree."""
    with EventClient(HOST, PORT, client_id="acceptance-producer") as producer:
        _await_connected(producer)
        retained = producer.publish(STATE_POSE, '{"yaw": 0.42}', qos=1, retain=True)
        event = producer.publish(EVENT_TOPIC, '{"probe": true}', qos=0)
        results.record(
            "QoS 0 publish accepted (at-most-once, drop-not-block)",
            event.ok,
            f"ok={event.ok} connected={event.connected}",
        )
        time.sleep(SETTLE)

    # A LATE subscriber must still see the retained value — that is what makes it
    # retained rather than merely delivered.
    sub, received = _subscriber([STATE_POSE])
    sub.loop_stop()
    sub.disconnect()
    got = [m for m in received if m.topic == STATE_POSE]
    flag = got[0].retain if got else "n/a"
    payload = got[0].payload if got else b""
    results.record(
        "retained message replays to a subscriber that connected afterwards",
        bool(retained.ok and got and got[0].retain),
        f"published ok={retained.ok}; late subscriber saw {len(got)} msg(s), "
        f"retain flag={flag}, payload={payload!r}",
    )


def check_last_will(results: Results) -> None:
    """Criterion 3: LWT flips the retained availability topic on an ungraceful drop.

    This is reachy-mini-cli's live-vs-stale discriminator: retained state alone
    cannot tell a consumer whether the publisher is still alive, so the will
    must fire on a hard socket loss — not on a clean ``disconnect()``, which
    would make the test pass for the wrong reason.
    """
    will = Will(topic=STATE_ONLINE, payload="false", qos=1, retain=True)
    producer = EventClient(HOST, PORT, client_id="acceptance-will", will=will)
    _await_connected(producer)
    producer.publish(STATE_ONLINE, "true", qos=1, retain=True)
    time.sleep(SETTLE)

    sub, received = _subscriber([STATE_ONLINE])
    before = [m.payload.decode() for m in received if m.topic == STATE_ONLINE]

    # Kill the socket under paho without sending DISCONNECT: the broker sees the
    # connection drop, not a graceful close, so it publishes the will.
    sock = producer._paho.socket()  # noqa: SLF001 - deliberate ungraceful kill
    if sock is not None:
        sock.shutdown(socket.SHUT_RDWR)
        sock.close()

    deadline = time.time() + 30
    flipped: list[str] = []
    while time.time() < deadline:
        flipped = [m.payload.decode() for m in received if m.topic == STATE_ONLINE]
        if "false" in flipped:
            break
        time.sleep(0.25)
    sub.loop_stop()
    sub.disconnect()
    try:
        producer.close()
    except Exception:  # noqa: BLE001 - already killed; teardown must not mask the result
        pass

    results.record(
        "LWT flips retained availability to false on ungraceful disconnect",
        "false" in flipped,
        f"saw {before} before the kill, {flipped} after",
    )


def check_envelope_roundtrip(results: Results) -> None:
    """events-cli's own contract: an Envelope survives a real broker round-trip."""
    sent = Envelope.new(
        type="acceptance.requested",
        source="agent://events-cli",
        data={"issue": 3, "box": "spark-f8a9"},
    )
    sub, received = _subscriber([ENVELOPE_TOPIC])
    with EventClient(HOST, PORT, client_id="acceptance-envelope") as producer:
        _await_connected(producer)
        producer.publish_event(sent, ENVELOPE_TOPIC, qos=1)
        deadline = time.time() + 20
        while time.time() < deadline and not received:
            time.sleep(0.1)
    sub.loop_stop()
    sub.disconnect()

    if not received:
        results.record("Envelope survives a broker round-trip", False, "nothing received")
        return

    back = Envelope.from_json(received[0].payload.decode())
    results.record(
        "Envelope survives a broker round-trip byte-for-byte",
        back == sent and back.id == sent.id,
        f"id={back.id} type={back.type} source={back.source} equal={back == sent}",
    )


def check_enqueue_latency(results: Results, samples: int = 2000) -> dict[str, float]:
    """The 50 Hz claim: publish() must be an O(1) enqueue, not a network wait.

    A 50 Hz control loop has a 20 ms budget for *everything*; a publish that
    blocked on the socket would blow it. Measured on the caller's thread, which
    is the thread the robot's loop runs on.
    """
    with EventClient(HOST, PORT, client_id="acceptance-latency") as producer:
        _await_connected(producer)
        timings: list[float] = []
        for i in range(samples):
            start = time.perf_counter()
            producer.publish(f"reachy/events/acceptance/tick.{i % 8}", b"x", qos=0)
            timings.append((time.perf_counter() - start) * 1000.0)

    stats = {
        "samples": float(samples),
        "mean_ms": statistics.mean(timings),
        "median_ms": statistics.median(timings),
        "p99_ms": sorted(timings)[int(samples * 0.99)],
        "max_ms": max(timings),
    }
    results.record(
        "publish() enqueue is sub-millisecond at the median (50 Hz budget is 20 ms)",
        stats["median_ms"] < 1.0,
        f"median={stats['median_ms']:.4f} ms  mean={stats['mean_ms']:.4f} ms  "
        f"p99={stats['p99_ms']:.4f} ms  max={stats['max_ms']:.4f} ms  (n={samples})",
    )
    return stats


def check_degrades_without_broker(results: Results) -> None:
    """reachy-mini-cli's stated requirement: no broker must not raise into the loop."""
    client = EventClient(HOST, 1, client_id="acceptance-nobroker", connect=True)
    outcome = client.publish("reachy/events/acceptance/dead", b"x", qos=0)
    client.close()
    results.record(
        "a broker-down publish returns ok=False instead of raising",
        outcome.ok is False,
        f"ok={outcome.ok} reason={outcome.reason!r} — the control loop keeps running",
    )


def _await_connected(client: EventClient, timeout: float = 15.0) -> None:
    """``is_connected`` is a property on EventClient, not a method — do not call it."""
    deadline = time.time() + timeout
    while time.time() < deadline and not client.is_connected:
        time.sleep(0.05)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit the results as JSON")
    args = parser.parse_args()

    results = Results()
    print("issue #3 acceptance — MQTT emit/consume", flush=True)
    check_paho_credentialless_connect(results)
    check_retained_and_qos0(results)
    check_last_will(results)
    check_envelope_roundtrip(results)
    latency = check_enqueue_latency(results)
    check_degrades_without_broker(results)

    if args.json:
        print(
            json.dumps({"ok": results.ok, "checks": results.checks, "latency": latency}, indent=2)
        )
    return 0 if results.ok else 1


if __name__ == "__main__":
    sys.exit(main())

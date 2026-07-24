"""The importable publish client — events-cli's producer lane on paho-mqtt.

This is the fourth surface named in issue #1: ``import events_cli`` and publish.
Its first, defining consumer is ``reachy-mini-cli`` (issue #3), whose 50 Hz robot
control loop constructs one of these and publishes from inside the loop. Three
properties are therefore load-bearing and are the whole point of the module:

O(1) enqueue on the caller's thread
    :meth:`EventClient.publish` does **no network I/O on the calling thread**.
    The client runs ``connect_async`` + ``loop_start``, so paho's background
    loop thread owns every socket operation; ``publish`` only hands the message
    to that thread and returns immediately. This is what keeps a publish inside
    a 20 ms real-time budget.

Never raises into the caller
    Constructing the client with the broker down, and publishing while
    disconnected, are no-ops-or-queued — never exceptions. A paho error
    (including "broker unreachable") becomes a :class:`PublishResult` with
    ``ok=False``, and the connection state is observable via :attr:`state` /
    :attr:`is_connected`. The caller wraps this in its own logging; our job is
    never to throw. The only things that *do* raise are genuine programmer error
    at construction (nonsensical config) and :class:`MqttDependencyError` when
    paho itself is missing.

Lazy import boundary
    paho is imported **only** inside this module, and only when the client is
    actually constructed (:func:`_load_paho`). It is never imported from
    :mod:`events_cli` package init or anywhere under :mod:`events_cli.cli`, so
    the introspection verbs keep running from a bare checkout with nothing
    installed. paho-mqtt is nonetheless a *base* dependency of events-cli, so in
    a normal install it is always present; the lazy import is a boundary, not an
    optional-extra gate.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any

from events_cli.core.envelope import Envelope
from events_cli.core.errors import EventsError

if TYPE_CHECKING:  # pragma: no cover - typing only; never imported at runtime here
    import paho.mqtt.client as mqtt

__all__ = [
    "ConnectionState",
    "EventClient",
    "MqttDependencyError",
    "PublishResult",
    "Will",
]

_LOG = logging.getLogger("events_cli.client")

#: The distribution that carries the transport client, named in the error below.
_PAHO_DISTRIBUTION = "paho-mqtt"

_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT = 1883
_DEFAULT_KEEPALIVE = 60


class MqttDependencyError(EventsError):
    """Raised when the MQTT client is used but ``paho-mqtt`` is not importable.

    paho-mqtt is a base dependency of events-cli, so this only fires in a
    deliberately stripped environment. The message names the missing package so
    the remedy is obvious, rather than an opaque ``ImportError`` surfacing deep
    in a call stack.
    """


def _load_paho() -> Any:
    """Import paho lazily, translating its absence into a named domain error.

    This is the ONLY place paho is imported. Keeping it here — out of
    :mod:`events_cli` package init and the whole :mod:`events_cli.cli` package —
    is what lets the introspection verbs run from a checkout with nothing
    installed.
    """
    try:
        import paho.mqtt.client as mqtt
    except ImportError as exc:
        raise MqttDependencyError(
            f"the MQTT client needs the '{_PAHO_DISTRIBUTION}' package, which is not "
            f"installed. Install it with 'pip install {_PAHO_DISTRIBUTION}' — it ships "
            "as a base dependency of events-cli."
        ) from exc
    return mqtt


class ConnectionState(str, Enum):
    """The observable connection state, readable from any thread.

    A ``str`` enum so it serialises cleanly and compares against plain strings.
    ``is_connected`` is the boolean a hot loop reads; ``state`` adds the nuance
    of *why* it is not connected (never started, still connecting, dropped).
    """

    IDLE = "idle"  # constructed; the network loop has not been started
    CONNECTING = "connecting"  # loop running, not yet connected (or reconnecting)
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"  # loop running, connection lost or refused
    CLOSED = "closed"  # close() called; the loop is stopped


@dataclass(frozen=True)
class Will:
    """A Last Will and Testament: what the broker publishes if we drop ungracefully.

    Registered before connect (paho requires it) — :class:`EventClient` does so
    in its constructor. For the standard availability pattern pass
    ``availability_topic`` to the client instead of building this by hand.
    """

    topic: str
    payload: str | bytes = ""
    qos: int = 0
    retain: bool = False


@dataclass(frozen=True)
class PublishResult:
    """The outcome of a publish. :meth:`EventClient.publish` returns this, never raises.

    ``ok`` is True when paho accepted the message for delivery. When the broker
    is unreachable a QoS 0 publish is dropped (``ok`` False, ``reason``
    ``"no_conn"``) rather than raising, so the caller's loop keeps running. The
    caller decides whether a dropped publish is worth logging.
    """

    ok: bool
    connected: bool
    reason: str
    mid: int | None = None


def _default_client_id() -> str:
    """A per-process-unique MQTT client id.

    Unique per process (the pid) *and* per instance (a random suffix), so two
    default-constructed clients in one process never present the same id — an
    MQTT broker disconnects an existing session when a second client connects
    with a duplicate id, and that self-inflicted kick is exactly what a unique
    default prevents.
    """
    return f"events-cli-{os.getpid()}-{secrets.token_hex(4)}"


def _encode_payload(payload: Any) -> Any:
    """Render a payload for the wire. Envelopes and mappings become JSON text.

    ``str``/``bytes``/``bytearray``/``None`` pass straight through to paho, which
    already knows how to encode them. An :class:`Envelope` uses its canonical
    :meth:`Envelope.to_json` wire form so consumers get the exact envelope
    contract, not an ad-hoc serialisation.
    """
    if isinstance(payload, Envelope):
        return payload.to_json()
    if isinstance(payload, Mapping):
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return payload


def _reason_text(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


def _rc_reason(rc: Any) -> str:
    """A readable slug for a paho return code, e.g. ``MQTT_ERR_NO_CONN`` -> ``no_conn``."""
    name = getattr(rc, "name", None)
    if isinstance(name, str) and name:
        return name.removeprefix("MQTT_ERR_").lower()
    return str(rc)


class EventClient:
    """An importable, thread-safe MQTT publish client that never raises at runtime.

    Construct one and publish; the background loop owns the connection and
    retries it forever if the broker is down. Publishing does no network I/O on
    the caller's thread and returns a :class:`PublishResult` rather than raising.

    Standing state and availability
        Pass ``availability_topic`` to get the standard MQTT availability
        pattern for free: a retained Last Will flips the topic to
        ``offline_payload`` on an ungraceful drop, a retained ``online_payload``
        is announced on every (re)connect, and :meth:`close` announces offline
        retained before a clean disconnect (a graceful DISCONNECT does not fire
        the LWT). Publish other standing state with ``retain=True`` yourself.
    """

    def __init__(
        self,
        host: str = _DEFAULT_HOST,
        port: int = _DEFAULT_PORT,
        *,
        client_id: str | None = None,
        keepalive: int = _DEFAULT_KEEPALIVE,
        will: Will | None = None,
        availability_topic: str | None = None,
        online_payload: str | bytes = "online",
        offline_payload: str | bytes = "offline",
        connect: bool = True,
        username: str | None = None,
        password: str | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        # -- config validation: genuine programmer error raises here, before any
        #    paho object exists. Runtime broker state never reaches this branch.
        if not isinstance(host, str) or not host:
            raise ValueError("host must be a non-empty string")
        if isinstance(port, bool) or not isinstance(port, int) or not 0 < port < 65536:
            raise ValueError("port must be an integer in 1..65535")
        if isinstance(keepalive, bool) or not isinstance(keepalive, int) or keepalive <= 0:
            raise ValueError("keepalive must be a positive integer (seconds)")
        if will is not None and availability_topic is not None:
            raise ValueError(
                "pass either 'will' or 'availability_topic', not both — "
                "availability_topic builds its own Last Will"
            )

        self._host = host
        self._port = port
        self._keepalive = keepalive
        self._log = logger or _LOG
        self._client_id = client_id or _default_client_id()
        self._availability_topic = availability_topic
        self._online_payload = online_payload
        self._offline_payload = offline_payload
        self._state = ConnectionState.IDLE
        self._loop_started = False

        mqtt = _load_paho()
        self._mqtt = mqtt
        self._paho = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=self._client_id,
        )
        self._paho.on_connect = self._on_connect
        self._paho.on_disconnect = self._on_disconnect
        if username is not None:
            self._paho.username_pw_set(username, password)

        # will_set MUST precede connect (paho requirement), so it happens here in
        # the constructor — before connect() runs connect_async below.
        effective_will = will
        if availability_topic is not None:
            effective_will = Will(topic=availability_topic, payload=offline_payload, retain=True)
        if effective_will is not None:
            self._paho.will_set(
                effective_will.topic,
                payload=effective_will.payload,
                qos=effective_will.qos,
                retain=effective_will.retain,
            )

        if connect:
            self.connect()

    # -- lifecycle ---------------------------------------------------------

    def connect(self) -> None:
        """Begin connecting and start the background network loop. Never raises.

        ``connect_async`` only records the target; ``loop_start`` spawns the
        thread that actually connects and, thereafter, retries forever if the
        broker is down. No network I/O happens on the calling thread, and a
        failure to start is swallowed and reflected in :attr:`state`.
        """
        try:
            self._paho.connect_async(self._host, self._port, keepalive=self._keepalive)
            if not self._loop_started:
                self._paho.loop_start()
                self._loop_started = True
            self._state = ConnectionState.CONNECTING
        except Exception as exc:  # noqa: BLE001 - never-raise contract
            self._log.warning("could not start connection: %s", exc)
            self._state = ConnectionState.DISCONNECTED

    def close(self) -> None:
        """Announce offline (if configured), disconnect cleanly, stop the loop. Never raises.

        Safe to call from ``atexit`` or a ``finally``. A clean DISCONNECT does
        not fire the Last Will, so when an availability topic is configured we
        publish the retained offline value ourselves before disconnecting.
        """
        try:
            if self._availability_topic is not None and self.is_connected:
                self._paho.publish(
                    self._availability_topic, self._offline_payload, qos=0, retain=True
                )
            self._paho.disconnect()
        except Exception as exc:  # noqa: BLE001 - never-raise contract
            self._log.warning("clean disconnect failed: %s", exc)
        try:
            if self._loop_started:
                self._paho.loop_stop()
                self._loop_started = False
        except Exception as exc:  # noqa: BLE001 - never-raise contract
            self._log.warning("stopping the network loop failed: %s", exc)
        self._state = ConnectionState.CLOSED

    def __enter__(self) -> "EventClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- publishing --------------------------------------------------------

    def publish(
        self,
        topic: str,
        payload: str | bytes | bytearray | Mapping[str, Any] | Envelope | None = None,
        *,
        qos: int = 0,
        retain: bool = False,
    ) -> PublishResult:
        """Enqueue a message for delivery. O(1) on the caller's thread; never raises.

        The real socket write happens on the background loop thread, so this
        call does no network I/O and is safe from a real-time loop. Any paho
        error — an invalid topic, or simply "broker unreachable" — becomes a
        :class:`PublishResult` with ``ok=False``, never an exception.

        ``payload`` may be an :class:`Envelope` (sent as its JSON wire form), a
        mapping (sent as JSON), or raw ``str``/``bytes``. ``retain=True`` marks
        the message as the topic's last-known value; ``qos`` defaults to 0
        (at-most-once, drop-don't-block) — right for this raw, co-located lane,
        and deliberately unchanged (see :meth:`publish_event` for the lane
        whose default changed). **A QoS 0 publish is never queued for an
        offline persistent session at all**, regardless of whether a
        subscription exists for the topic — it bypasses durable capture
        (:mod:`events_cli.history`) entirely, by construction. Pass ``qos=1``
        whenever a published message must survive being captured by a drain.
        """
        connected = self.is_connected
        try:
            info = self._paho.publish(topic, _encode_payload(payload), qos=qos, retain=retain)
        except Exception as exc:  # noqa: BLE001 - never-raise contract
            self._log.warning("publish to %r failed: %s", topic, exc)
            return PublishResult(ok=False, connected=connected, reason=_reason_text(exc))
        rc = getattr(info, "rc", None)
        ok = rc == self._mqtt.MQTTErrorCode.MQTT_ERR_SUCCESS
        return PublishResult(
            ok=ok,
            connected=connected,
            reason="ok" if ok else _rc_reason(rc),
            mid=getattr(info, "mid", None),
        )

    def publish_event(
        self, envelope: Envelope, topic: str, *, qos: int = 1, retain: bool = False
    ) -> PublishResult:
        """Publish an :class:`Envelope` as its canonical JSON wire form. Never raises.

        A typed convenience over :meth:`publish` that makes the topic explicit;
        the consumer owns its topic tree (e.g. ``reachy/events/{source}/{type}``)
        rather than having the client invent one.

        ``qos`` defaults to **1**, not 0 — a behaviour change from before
        0.10.0 (see ``CHANGELOG.md``). A QoS 0 publish is never queued for an
        offline persistent session at all, so it silently bypasses durable
        capture (:mod:`events_cli.history`, the store ``events watch`` drains
        into) regardless of whether a subscription exists — the exact trap
        this default now closes for the envelope-publishing lane. ``events
        emit`` (``events_cli/cli/_commands/emit.py``) always passes ``qos=1``
        explicitly rather than relying on this default, and it is the only
        thing changing here: :meth:`publish` — the raw lane
        ``reachy-mini-cli``'s 50 Hz control loop binds to — still defaults to
        ``qos=0`` and is deliberately unaffected. Pass ``qos=0`` explicitly if
        an envelope publish genuinely does not need durable capture.
        """
        return self.publish(topic, envelope, qos=qos, retain=retain)

    # -- observable state --------------------------------------------------

    @property
    def state(self) -> ConnectionState:
        """The last observed connection state. Safe to read from any thread."""
        return self._state

    @property
    def is_connected(self) -> bool:
        """True when the transport is currently connected to the broker."""
        try:
            return bool(self._paho.is_connected())
        except Exception:  # noqa: BLE001 - a state read must never raise
            return False

    @property
    def client_id(self) -> str:
        """The MQTT client id this instance connects with."""
        return self._client_id

    @property
    def loop_running(self) -> bool:
        """True when the background network loop thread is alive.

        While this holds, ``publish`` hands work to that thread and does no
        socket I/O on the caller — the O(1)-enqueue guarantee the 50 Hz consumer
        depends on. It is False before :meth:`connect` and after :meth:`close`.
        """
        thread = getattr(self._paho, "_thread", None)
        return bool(self._loop_started and thread is not None and thread.is_alive())

    # -- paho callbacks (VERSION2 signatures) ------------------------------

    def _on_connect(
        self,
        client: "mqtt.Client",
        userdata: Any,
        connect_flags: Any,
        reason_code: Any,
        properties: Any,
    ) -> None:
        if getattr(reason_code, "is_failure", False):
            self._state = ConnectionState.DISCONNECTED
            self._log.warning("broker refused the connection: %s", reason_code)
            return
        self._state = ConnectionState.CONNECTED
        self._log.info("connected to %s:%s as %s", self._host, self._port, self._client_id)
        if self._availability_topic is not None:
            try:
                client.publish(self._availability_topic, self._online_payload, qos=0, retain=True)
            except Exception as exc:  # noqa: BLE001 - never-raise contract
                self._log.warning("availability announce failed: %s", exc)

    def _on_disconnect(
        self,
        client: "mqtt.Client",
        userdata: Any,
        disconnect_flags: Any,
        reason_code: Any,
        properties: Any,
    ) -> None:
        self._state = ConnectionState.DISCONNECTED
        self._log.info("disconnected from %s:%s (%s)", self._host, self._port, reason_code)

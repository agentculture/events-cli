"""The MQTT persistent session: the live half of a durable subscription.

There is **no resident control service in this arc**. A durable subscription is
an MQTT *persistent session* held by the broker itself, so the broker's own
persistence (``persistence true`` plus the mounted volume, see
``events_cli/stack/templates/``) is what buffers the backlog while no drainer is
connected. That is the whole architecture, and it rests on four properties of
the packets this module sends — measured against
``eclipse-mosquitto:2.1.2-alpine`` with this repo's template config on
2026-07-24:

1. **MQTT 5.** Session Expiry Interval is an MQTT 5 CONNECT property; MQTT 3.1.1
   has only the binary ``clean_session`` flag, whose session lifetime is
   entirely the broker's choice. The protocol version is therefore not a
   preference.
2. **``clean_start=False``.** Asks the broker to resume the session belonging to
   our client id rather than discard it. ``session_present`` in the CONNACK is
   how we learn whether it did.
3. **``SessionExpiryInterval = 0xFFFFFFFF``** (:data:`SESSION_EXPIRY_INFINITE`),
   the MQTT 5 "never expire" value. With it, a session created here survives a
   graceful disconnect *and a full broker restart* — the probe confirmed
   ``session_present=True`` across ``docker restart`` with no extra broker
   configuration. A broker may cap this via ``max_session_expiry_interval``;
   nothing in the generated config does, and t12 verifies it live.
4. **A graceful DISCONNECT.** Subscribe, then disconnect cleanly and leave. The
   session — and its subscription, and everything queued for it at QoS 1 —
   stays in the broker. A DISCONNECT carrying ``SessionExpiryInterval=0`` would
   end it, which is precisely how :func:`events_cli.subs.remove_subscription`
   destroys one.

The identity of a session is its **client id**, which is why
:func:`events_cli.subs.record.client_id_for` derives one deterministically from
the subscription name. A consequence worth stating: a second concurrent
connection with the same id makes the broker disconnect the first (Mosquitto
logs ``session taken over``). That is not a bug to defend against — it is what
enforces single-drainer-per-subscription semantics for t8.

The seam
--------
:class:`PersistentSession` owns *connect with session parameters*, and nothing
else. ``sub add`` opens one, subscribes, and closes it; ``sub remove`` opens one
with the destroying parameters; and the drain (t8) opens the **same class** with
``manual_ack=True``, attaches ``on_message`` to :attr:`PersistentSession.client`
and runs its own loop. The lifecycle is written once so the drain resumes
sessions exactly the way registration created them, rather than re-deriving the
protocol details and drifting.

The lazy-import boundary
------------------------
paho is imported only inside :func:`_load_paho`, which delegates to
:mod:`events_cli.client`'s loader so that the whole package has exactly one
place where the transport dependency is named and exactly one
:class:`~events_cli.client.MqttDependencyError` message. Importing
:mod:`events_cli.subs` — building records, validating names, listing the
registry — never imports paho.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Any, Callable

from events_cli.subs.errors import BrokerUnreachableError, SessionError

__all__ = [
    "DEFAULT_CONNECT_TIMEOUT",
    "QOS_AT_LEAST_ONCE",
    "SESSION_EXPIRY_DESTROY",
    "SESSION_EXPIRY_INFINITE",
    "BrokerAddress",
    "PersistentSession",
    "default_client_factory",
]

_LOG = logging.getLogger("events_cli.subs")

#: MQTT 5's "never expire" Session Expiry Interval (UINT32 max). What a durable
#: subscription's CONNECT carries, so the session outlives the process, the
#: drainer and a broker restart.
SESSION_EXPIRY_INFINITE = 0xFFFFFFFF

#: Session Expiry Interval 0 — "end the session when the connection ends".
#: Paired with ``clean_start=True`` it is how a subscription is destroyed.
SESSION_EXPIRY_DESTROY = 0

#: Durable subscriptions subscribe at QoS 1. Only QoS 1+ messages are queued
#: for an offline session, so QoS 0 traffic is transported but never captured —
#: a boundary ``docs/contract.md`` states so no consumer assumes otherwise.
QOS_AT_LEAST_ONCE = 1

#: How long to wait for a CONNACK before calling the broker unreachable. Finite
#: by policy, like every other bound in this repo: an unbounded wait is what
#: hangs an agent turn.
DEFAULT_CONNECT_TIMEOUT = 10.0

_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT = 1883
_DEFAULT_KEEPALIVE = 60

_BROKER_HINT = (
    "start the broker with 'events up', then check it with 'events status' "
    "(the generated stack binds 127.0.0.1:1883)"
)


@dataclass(frozen=True)
class BrokerAddress:
    """Where the broker is. Defaults to the loopback stack ``events up`` runs.

    Loopback by default because that is what the generated Compose file
    publishes (``127.0.0.1:1883:1883``) — remote access is an explicit,
    documented opt-in that edits the template, and this default must not
    quietly widen it.
    """

    host: str = _DEFAULT_HOST
    port: int = _DEFAULT_PORT
    keepalive: int = _DEFAULT_KEEPALIVE


def _load_paho() -> Any:
    """Import paho lazily, via the client module's single boundary.

    Delegated rather than duplicated: :mod:`events_cli.client` already owns the
    one place paho is named and the one
    :class:`~events_cli.client.MqttDependencyError` message. The import of
    :mod:`events_cli.client` itself is inside this function too, so importing
    :mod:`events_cli.subs` pulls in nothing beyond the core.
    """
    from events_cli.client import _load_paho as load

    return load()


def default_client_factory(mqtt: Any, client_id: str, *, manual_ack: bool) -> Any:
    """Build the real paho client a persistent session needs.

    Separated from :class:`PersistentSession` so the whole lifecycle can be
    driven against a fake client with no broker and no docker — the session
    class never constructs a client itself, it calls a factory.

    ``protocol=MQTTv5`` is load-bearing (session expiry is an MQTT 5 property),
    and ``manual_ack`` is threaded through for t8's persist-then-ack drain,
    which must not acknowledge a message before the history store has it.
    """
    return mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id=client_id,
        protocol=mqtt.MQTTv5,
        manual_ack=manual_ack,
    )


ClientFactory = Callable[..., Any]


class PersistentSession:
    """One MQTT persistent session, addressed by its stable client id.

    Not a publish client: :class:`events_cli.client.EventClient` is the producer
    lane, and its per-process-random client id is exactly right there and
    exactly wrong here. This class exists to create, resume and destroy the
    *named* sessions a durable subscription is made of.

    Unlike ``EventClient``, this **does** raise: registering a subscription
    against a broker that is not running is a failure the caller must see, not a
    state to observe later. ``EventClient``'s never-raise contract serves a
    50 Hz control loop that must keep running regardless; a ``sub add`` that
    silently did nothing would leave a record describing a session the broker
    never got.
    """

    def __init__(
        self,
        client_id: str,
        address: BrokerAddress | None = None,
        *,
        manual_ack: bool = False,
        client_factory: ClientFactory | None = None,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
        logger: logging.Logger | None = None,
    ) -> None:
        self.client_id = client_id
        self.address = address or BrokerAddress()
        self._manual_ack = manual_ack
        self._factory = client_factory or default_client_factory
        self._timeout = connect_timeout
        self._log = logger or _LOG
        self._client: Any = None
        self._mqtt: Any = None
        self._connected = threading.Event()
        self._session_present = False
        self._failure: Any = None
        self._loop_started = False

    # -- observable state --------------------------------------------------

    @property
    def client(self) -> Any:
        """The underlying paho client — what a drain attaches ``on_message`` to."""
        if self._client is None:
            raise SessionError(
                "the session is not open",
                remediation="call open() before using the underlying client",
            )
        return self._client

    @property
    def session_present(self) -> bool:
        """Whether the broker resumed an existing session for our client id.

        ``True`` means the subscription and everything queued for it were still
        there — the property the whole durable-subscription design rests on, and
        what a drain checks to know it resumed rather than started over.
        """
        return self._session_present

    # -- lifecycle ---------------------------------------------------------

    def open(
        self,
        *,
        clean_start: bool = False,
        session_expiry: int = SESSION_EXPIRY_INFINITE,
    ) -> bool:
        """Connect with the session parameters and wait for the CONNACK.

        Returns :attr:`session_present`. The defaults are the *durable* ones —
        resume the session, never expire it — so a caller has to be explicit to
        get anything else; ``sub remove`` is the only caller that is (see
        :data:`SESSION_EXPIRY_DESTROY`).

        Raises :class:`BrokerUnreachableError` if the broker is not accepting
        connections or never answers, and :class:`SessionError` if it answers
        with a refusal.
        """
        mqtt = _load_paho()
        self._mqtt = mqtt
        client = self._factory(mqtt, self.client_id, manual_ack=self._manual_ack)
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect

        properties = mqtt.Properties(mqtt.PacketTypes.CONNECT)
        properties.SessionExpiryInterval = session_expiry

        try:
            client.connect(
                self.address.host,
                self.address.port,
                self.address.keepalive,
                clean_start=clean_start,
                properties=properties,
            )
        except OSError as exc:
            raise BrokerUnreachableError(
                f"could not reach the broker at {self.address.host}:{self.address.port} ({exc})",
                remediation=_BROKER_HINT,
            ) from exc

        self._client = client
        client.loop_start()
        self._loop_started = True

        if not self._connected.wait(self._timeout):
            self.close()
            raise BrokerUnreachableError(
                f"the broker at {self.address.host}:{self.address.port} did not answer "
                f"within {self._timeout:g}s",
                remediation=_BROKER_HINT,
            )
        if self._failure is not None:
            failure = self._failure
            self.close()
            raise SessionError(
                f"the broker refused the session for {self.client_id!r}: {failure}",
                remediation=(
                    "check the broker log with 'events logs'; a refusal here is an "
                    "authentication or protocol-level rejection, not a network fault"
                ),
            )
        return self._session_present

    def subscribe(self, topic_filter: str, qos: int = QOS_AT_LEAST_ONCE) -> None:
        """Subscribe the open session to ``topic_filter``.

        QoS 1 by default and by contract: only QoS 1+ messages are queued for an
        offline session, so a QoS 0 subscription would keep the session alive
        while capturing nothing while the drainer is away.
        """
        rc, _mid = self.client.subscribe(topic_filter, qos=qos)
        if rc != self._mqtt.MQTTErrorCode.MQTT_ERR_SUCCESS:
            raise SessionError(
                f"the broker rejected the subscription to {topic_filter!r} ({rc})",
                remediation="check the broker log with 'events logs'",
            )

    def close(self) -> None:
        """Disconnect gracefully and stop the network loop. Never raises.

        A *graceful* DISCONNECT with no expiry property, which is what leaves
        the session — and its queued messages — live in the broker. Idempotent
        and safe from a ``finally``: a close that raised would mask whatever
        sent us here.
        """
        client, self._client = self._client, None
        if client is None:
            return
        try:
            client.disconnect()
        except Exception as exc:  # noqa: BLE001 - close must never raise
            self._log.warning("clean disconnect failed: %s", exc)
        try:
            if self._loop_started:
                client.loop_stop()
                self._loop_started = False
        except Exception as exc:  # noqa: BLE001 - close must never raise
            self._log.warning("stopping the network loop failed: %s", exc)

    def __enter__(self) -> "PersistentSession":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- paho callbacks (VERSION2 signatures) ------------------------------

    def _on_connect(
        self,
        client: Any,
        userdata: Any,
        connect_flags: Any,
        reason_code: Any,
        properties: Any,
    ) -> None:
        if getattr(reason_code, "is_failure", False):
            self._failure = reason_code
        else:
            self._session_present = bool(getattr(connect_flags, "session_present", False))
            self._log.info("session %s (session_present=%s)", self.client_id, self._session_present)
        self._connected.set()

    def _on_disconnect(
        self,
        client: Any,
        userdata: Any,
        disconnect_flags: Any,
        reason_code: Any,
        properties: Any,
    ) -> None:
        self._log.info("session %s disconnected (%s)", self.client_id, reason_code)

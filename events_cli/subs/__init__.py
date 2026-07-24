"""Durable subscriptions — a registry record plus a broker-side MQTT session.

A durable subscription in this arc is deliberately **two things**, and keeping
them in step is what this package is for:

* a **registry record** on disk (:mod:`events_cli.subs.record`,
  :mod:`events_cli.subs.registry`) — name, pattern, compiled filter, owner,
  client id, created — which is what ``events sub list`` reads and what #10's
  dynsec work attaches a real authenticated identity to; and
* an **MQTT persistent session** in the broker
  (:mod:`events_cli.subs.session`), addressed by the record's client id, which
  is what actually queues events at QoS 1 while no drainer is connected.

No resident control service ships here. The broker's own persistence is the
buffer, which is why ``events up``'s generated stack sets ``persistence true``
plus a mounted volume and an explicit ``max_queued_messages`` bound.

The order the two halves are written in is a decision, not an accident.
:func:`add_subscription` establishes the **session first** and persists the
record only once the broker has it: a record describing a session that was
never created is a subscription that silently captures nothing, which is far
worse than a failed ``sub add``. :func:`remove_subscription` mirrors it —
destroy the session, *then* drop the record — so a broker that is down leaves
the record in place rather than orphaning a live queue with nothing left on
disk pointing at it. ``force=True`` is the deliberate escape hatch for a broker
that is gone for good.

What this package is not
------------------------
It does not drain. Consuming from a session, persisting to the history store
before acknowledging, and returning a cursor are t8's
(:mod:`events_cli.history` holds the store side), and the CLI verbs are t9's.
Nothing here imports :mod:`events_cli.cli`: four surfaces consume this registry
and only one of them has exit codes, so this layer raises
:class:`SubsError` and the CLI translates at its edge.

Nothing here imports paho at module scope either — see
:mod:`events_cli.subs.session` — so importing this package, building records
and listing the registry all work from a checkout with nothing installed.
"""

from __future__ import annotations

from events_cli.subs.errors import (
    BrokerUnreachableError,
    DuplicateSubscriptionError,
    RegistryCorruptError,
    RegistryFormatError,
    SessionError,
    SubscriptionValidationError,
    SubsError,
    UnknownSubscriptionError,
)
from events_cli.subs.record import (
    CLIENT_ID_PREFIX,
    MAX_NAME_LENGTH,
    REGISTRY_FORMAT_VERSION,
    SUPPORTED_REGISTRY_FORMATS,
    SubscriptionRecord,
    check_subscription_name,
    client_id_for,
    resolve_owner,
)
from events_cli.subs.registry import (
    REGISTRY_DIRNAME,
    SubscriptionRegistry,
    default_registry_dir,
    open_registry,
)
from events_cli.subs.session import (
    DEFAULT_CONNECT_TIMEOUT,
    QOS_AT_LEAST_ONCE,
    SESSION_EXPIRY_DESTROY,
    SESSION_EXPIRY_INFINITE,
    BrokerAddress,
    ClientFactory,
    PersistentSession,
    default_client_factory,
)

__all__ = [
    "CLIENT_ID_PREFIX",
    "DEFAULT_CONNECT_TIMEOUT",
    "MAX_NAME_LENGTH",
    "QOS_AT_LEAST_ONCE",
    "REGISTRY_DIRNAME",
    "REGISTRY_FORMAT_VERSION",
    "SESSION_EXPIRY_DESTROY",
    "SESSION_EXPIRY_INFINITE",
    "SUPPORTED_REGISTRY_FORMATS",
    "BrokerAddress",
    "BrokerUnreachableError",
    "ClientFactory",
    "DuplicateSubscriptionError",
    "PersistentSession",
    "RegistryCorruptError",
    "RegistryFormatError",
    "SessionError",
    "SubsError",
    "SubscriptionRecord",
    "SubscriptionRegistry",
    "SubscriptionValidationError",
    "UnknownSubscriptionError",
    "add_subscription",
    "check_subscription_name",
    "client_id_for",
    "default_client_factory",
    "default_registry_dir",
    "get_subscription",
    "list_subscriptions",
    "open_registry",
    "remove_subscription",
    "resolve_owner",
]


def _resolve(registry: SubscriptionRegistry | None) -> SubscriptionRegistry:
    return registry if registry is not None else open_registry()


def add_subscription(
    name: str,
    pattern: str,
    *,
    owner: str | None = None,
    address: BrokerAddress | None = None,
    registry: SubscriptionRegistry | None = None,
    client_factory: "ClientFactory | None" = None,
) -> SubscriptionRecord:
    """Register a durable subscription: create the broker session, then record it.

    Validates ``name`` and ``pattern`` in one pass (nothing touches the broker
    if either is bad), refuses a name already registered (the duplicate never
    reaches the broker either), then connects with ``clean_start=False`` and an
    effectively infinite session expiry, subscribes the compiled filter at
    QoS 1, and disconnects **gracefully** — leaving the session, its
    subscription and everything subsequently queued for it live in the broker.

    Only then is the record written. See the module docstring for why that
    order is not negotiable.

    ``owner`` defaults to this agent's ``culture.yaml`` nick — the identity
    #10's dynsec work will authenticate under this same key.

    Raises :class:`SubscriptionValidationError`,
    :class:`DuplicateSubscriptionError`, :class:`BrokerUnreachableError` or
    :class:`SessionError`.
    """
    store = _resolve(registry)
    record = SubscriptionRecord.new(name, pattern, owner=owner)
    if store.get(record.name) is not None:
        raise DuplicateSubscriptionError(
            f"subscription {record.name!r} is already registered",
            remediation=(
                f"choose another name, or remove the existing one first: "
                f"'events sub remove {record.name}'"
            ),
        )

    session = PersistentSession(record.client_id, address, client_factory=client_factory)
    try:
        session.open(clean_start=False, session_expiry=SESSION_EXPIRY_INFINITE)
        session.subscribe(record.topic_filter, qos=QOS_AT_LEAST_ONCE)
    finally:
        session.close()
    return store.add(record)


def remove_subscription(
    name: str,
    *,
    address: BrokerAddress | None = None,
    registry: SubscriptionRegistry | None = None,
    client_factory: "ClientFactory | None" = None,
    force: bool = False,
) -> SubscriptionRecord:
    """Destroy the broker session, then drop the record. Returns what was removed.

    The session is destroyed by connecting with ``clean_start=True`` — which
    discards the existing session at CONNECT — *and* a Session Expiry Interval
    of 0, so the fresh session that connection creates ends the moment it
    disconnects. Belt and braces on purpose: either alone leaves a window in
    which a session survives, and a surviving session keeps queueing events
    nobody will ever drain.

    A broker that is unreachable is an error, not a silent success: dropping
    the record would orphan a live queue with nothing on disk pointing at it.
    ``force=True`` overrides that for a broker that is gone for good — the
    record goes, and any session is left to its expiry.
    """
    store = _resolve(registry)
    record = store.get(name)
    if record is None:
        raise UnknownSubscriptionError(
            f"no subscription named {name!r}",
            remediation="list what is registered with 'events sub list'",
        )

    session = PersistentSession(record.client_id, address, client_factory=client_factory)
    try:
        session.open(clean_start=True, session_expiry=SESSION_EXPIRY_DESTROY)
    except SessionError:
        if not force:
            raise
    finally:
        session.close()
    return store.remove(record.name)


def list_subscriptions(
    *, registry: SubscriptionRegistry | None = None
) -> tuple[SubscriptionRecord, ...]:
    """Every registered subscription, sorted by name — what ``events sub list`` renders."""
    return _resolve(registry).list()


def get_subscription(
    name: str, *, registry: SubscriptionRegistry | None = None
) -> SubscriptionRecord | None:
    """The record registered under ``name``, or ``None`` — what ``events sub show`` renders."""
    return _resolve(registry).get(name)

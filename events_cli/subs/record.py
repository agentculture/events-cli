"""The subscription record: what a durable subscription *is*, on disk.

Standard library only — no transport client, no container runtime, no I/O — for
the same reason :mod:`events_cli.core.envelope` is: validation and the
client-id derivation are the parts worth high coverage, and they must run on a
machine with nothing installed.

The schema
----------
A record is the durable half of a subscription; the broker session (see
:mod:`events_cli.subs.session`) is the live half, and the record is what lets a
later process find that session again::

    {
      "registryFormatVersion": "1",
      "name":     "robot",
      "pattern":  "task.*",
      "filter":   "events/task/+",
      "owner":    "events-cli",
      "clientId": "events-cli-sub-robot",
      "created":  "2026-07-24T09:15:02.481Z"
    }

Four of those need justifying.

``owner`` — **the forward compatibility hinge for #10.** Today it is a declared
name, unauthenticated on loopback: by default the agent's own ``culture.yaml``
nick, falling back to the subscription's client id where no ``culture.yaml``
ships (a wheel install). When dynsec lands, the dynsec *identity name* is this
same string in this same key — a record written today and a record written
after authentication arrives differ only in whether the value was proved. There
is deliberately no second ``identity``/``username`` field to migrate into, and
no schema bump is planned for it.

``filter`` — stored, not re-derived on read. The broker holds a subscription on
this literal string. If :func:`events_cli.core.topics.pattern_to_filter` ever
compiled a pattern differently, a re-derived filter would quietly stop
describing the subscription the broker actually has; the stored one keeps the
record honest about what was really registered.

``clientId`` — likewise stored, for a stronger version of the same reason: it
is the session's *identity in the broker*, and the whole architecture rests on
a later process presenting exactly the same id. It is derived from the name by
:func:`client_id_for` and pinned by a test, so the two can never disagree, but
the record does not depend on that derivation staying fixed forever.

``registryFormatVersion`` — a separate key from the history store's
``storeFormatVersion``, not a shared one. The registry and the event log are
different shapes with different readers and will need to move independently; a
single version line covering both would force a bump on one to describe a
change in the other.

Names are attacker-shaped input
-------------------------------
A name becomes a **filename** in the registry directory *and* an **MQTT client
id**; a pattern becomes a **topic filter**. Both are validated here, at the
boundary, reporting every problem in one pass as a field-level
:class:`~events_cli.core.errors.FieldError`. ``#``, ``+`` and ``/`` are the
characters MQTT gives structural meaning to and are rejected in both; ``..`` is
rejected in a name because it is the traversal segment. The name grammar is a
deliberate **subset** of the history store's subscription-name grammar, because
the drain (t8) appends under this exact name — two grammars would silently
diverge, and ``tests/test_subs.py`` pins them together.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping

from events_cli.core import identity
from events_cli.core.envelope import now_rfc3339
from events_cli.core.errors import FieldError, TopicValidationError
from events_cli.core.topics import pattern_to_filter
from events_cli.subs.errors import (
    RegistryCorruptError,
    RegistryFormatError,
    SubscriptionValidationError,
)

__all__ = [
    "CLIENT_ID_PREFIX",
    "MAX_NAME_LENGTH",
    "REGISTRY_FORMAT_VERSION",
    "SUPPORTED_REGISTRY_FORMATS",
    "SubscriptionRecord",
    "check_subscription_name",
    "client_id_for",
    "resolve_owner",
]

#: The on-disk record format. Bumped only when a record's *shape* changes in a
#: way an older reader would misread; adding an ignored key does not qualify,
#: and #10 giving ``owner`` an authenticated meaning explicitly does not.
REGISTRY_FORMAT_VERSION = "1"

#: Formats this build can read. A record outside this set raises
#: :class:`RegistryFormatError` rather than being silently reinterpreted.
SUPPORTED_REGISTRY_FORMATS: frozenset[str] = frozenset({REGISTRY_FORMAT_VERSION})

#: Every subscription's MQTT client id starts here. The prefix is what makes
#: the derivation injective across the whole id space: it is a fixed string, so
#: two ids are equal only when their names are.
CLIENT_ID_PREFIX = "events-cli-sub-"

#: The same bound the history store puts on a subscription name, so a name this
#: layer accepts is always one the store will take.
MAX_NAME_LENGTH = 64

# A slug: lowercase alphanumerics and the three inner separators, starting with
# an alphanumeric. Deliberately a subset of the history store's name grammar.
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")

# The characters MQTT gives structural meaning to. Rejected in a *name* as well
# as a pattern: the name becomes an MQTT client id, and '/' is also the path
# separator this record's filename is built from.
_RESERVED_MQTT_CHARS = ("#", "+", "/")

_TRAVERSAL = ".."

_NAME_HINT = (
    "use a lowercase slug of at most "
    f"{MAX_NAME_LENGTH} characters — letters, digits, '.', '_' and '-', starting "
    "with a letter or digit (e.g. 'reachy-mini')"
)

_WIRE_VERSION = "registryFormatVersion"
_WIRE_NAME = "name"
_WIRE_PATTERN = "pattern"
_WIRE_FILTER = "filter"
_WIRE_OWNER = "owner"
_WIRE_CLIENT_ID = "clientId"
_WIRE_CREATED = "created"

_WIRE_KEYS = (
    _WIRE_VERSION,
    _WIRE_NAME,
    _WIRE_PATTERN,
    _WIRE_FILTER,
    _WIRE_OWNER,
    _WIRE_CLIENT_ID,
    _WIRE_CREATED,
)


def _name_errors(name: object) -> list[FieldError]:
    """Every reason ``name`` is not a usable subscription name, in one pass."""
    errors: list[FieldError] = []
    if not isinstance(name, str):
        return [FieldError("name", "not_a_string", f"must be a string, got {type(name).__name__}")]
    if not name:
        return [FieldError("name", "empty", "must not be empty")]
    if len(name) > MAX_NAME_LENGTH:
        errors.append(
            FieldError(
                "name",
                "too_long",
                f"must be at most {MAX_NAME_LENGTH} characters, got {len(name)}",
            )
        )
    found = sorted(char for char in _RESERVED_MQTT_CHARS if char in name)
    if found:
        chars = ", ".join(repr(char) for char in found)
        errors.append(
            FieldError(
                "name",
                "reserved_mqtt_char",
                f"must not contain the raw MQTT filter character(s) {chars} — a name "
                "becomes an MQTT client id and a filename",
            )
        )
    if _TRAVERSAL in name:
        errors.append(
            FieldError(
                "name",
                "malformed",
                "must not contain '..' — a name is a filename in the registry",
            )
        )
    if not _NAME_RE.match(name):
        errors.append(FieldError("name", "malformed", _NAME_HINT))
    return errors


def check_subscription_name(name: str) -> str:
    """Return ``name`` if it is a usable subscription name, else raise.

    Raises :class:`SubscriptionValidationError` carrying every field-level
    reason it was rejected. Called on **every** path that turns a name into a
    filename or a client id, so an unvalidated string can never reach the disk
    or the broker.
    """
    errors = _name_errors(name)
    if errors:
        raise SubscriptionValidationError(errors, summary="invalid subscription name")
    return name


def client_id_for(name: str) -> str:
    """The MQTT client id that addresses ``name``'s persistent session.

    A **pure function of the name**, and that is the whole point. The broker
    knows a persistent session only by its client id, so a later ``events
    watch`` can resume the session ``events sub add`` created only if it
    presents exactly the same id. The per-process-random default
    :class:`events_cli.client.EventClient` uses — right for a producer, which
    must never take over a peer's session — would mint a fresh empty session on
    every call here and silently orphan the queue.

    Collision-free by construction: the prefix is a fixed string, so
    ``client_id_for(a) == client_id_for(b)`` exactly when ``a == b``. And
    validated first, so a name that could not be a subscription cannot become a
    client id either.

    The result is longer than MQTT 3.1.1's 23-character *recommendation*, which
    is not a limit: MQTT 5 permits up to 65535 bytes and Mosquitto imposes no
    lower bound of its own. Readability in the broker log is worth more here
    than a hash would be — an operator reading ``session taken over`` needs to
    know *which* subscription took it.
    """
    return f"{CLIENT_ID_PREFIX}{check_subscription_name(name)}"


def resolve_owner(client_id: str) -> str:
    """The default owner identity for a subscription created by this agent.

    This agent's ``culture.yaml`` nick, which is the same string ``events
    whoami`` prints, falling back to ``client_id`` when no ``culture.yaml``
    ships alongside the package (a wheel install). The fallback is the client id
    rather than a literal like ``"unknown"`` on purpose: an owner field exists
    to tell two subscriptions' owners apart, and the client id is the one value
    guaranteed to be unique to this subscription.
    """
    return identity.agent_nick() or client_id


@dataclass(frozen=True)
class SubscriptionRecord:
    """One durable subscription as the registry holds it.

    Frozen: a record describes a broker session that already exists under a
    specific client id and filter. Changing a pattern means removing the
    subscription and adding it again, so the broker session is rebuilt to match
    — mutating the record in place would leave the two describing different
    things.
    """

    name: str
    pattern: str
    topic_filter: str
    owner: str
    client_id: str
    created: str
    registry_format_version: str = REGISTRY_FORMAT_VERSION

    @classmethod
    def new(
        cls,
        name: str,
        pattern: str,
        *,
        owner: str | None = None,
        created: str | None = None,
    ) -> "SubscriptionRecord":
        """Validate ``name`` and ``pattern`` together and build the record.

        Both are validated in a **single pass** and every problem is reported
        together, so ``sub add`` with two broken arguments is one error and one
        fix rather than two round trips. Nothing here touches the disk or the
        broker: a record that cannot be built must not have cost a connection.
        """
        errors: list[FieldError] = _name_errors(name)
        topic_filter: str | None = None
        try:
            topic_filter = pattern_to_filter(pattern)
        except TopicValidationError as exc:
            errors.extend(exc.errors)
        if errors or topic_filter is None:
            raise SubscriptionValidationError(errors)

        client_id = f"{CLIENT_ID_PREFIX}{name}"
        return cls(
            name=name,
            pattern=pattern,
            topic_filter=topic_filter,
            owner=owner if owner is not None else resolve_owner(client_id),
            client_id=client_id,
            created=created or now_rfc3339(),
        )

    def to_dict(self) -> dict[str, Any]:
        """The on-disk / ``--json`` form: camelCase, version first."""
        return {
            _WIRE_VERSION: self.registry_format_version,
            _WIRE_NAME: self.name,
            _WIRE_PATTERN: self.pattern,
            _WIRE_FILTER: self.topic_filter,
            _WIRE_OWNER: self.owner,
            _WIRE_CLIENT_ID: self.client_id,
            _WIRE_CREATED: self.created,
        }

    @classmethod
    def from_dict(
        cls, payload: Mapping[str, Any], *, origin: str = "record"
    ) -> "SubscriptionRecord":
        """Parse a stored record, or raise :class:`RegistryCorruptError`.

        The registry's trust boundary, mirroring
        :meth:`events_cli.core.envelope.Envelope.from_dict` and
        :meth:`events_cli.history.backend.HistoryRecord.from_dict`: everything
        read back from disk comes through here, and a record that cannot be
        interpreted is an environment fault naming the file it came from —
        never a traceback, and never silently skipped, because a subscription
        that disappears from ``sub list`` while its broker session keeps
        queueing events is a leak with no visible cause.
        """
        if not isinstance(payload, Mapping):
            raise RegistryCorruptError(
                f"{origin}: a registry record must be a JSON object, "
                f"got {type(payload).__name__}",
                remediation=_repair_hint(origin),
            )

        version = payload.get(_WIRE_VERSION)
        if version not in SUPPORTED_REGISTRY_FORMATS:
            raise RegistryFormatError(
                f"{origin}: unsupported registryFormatVersion {version!r} "
                f"(this build reads {', '.join(sorted(SUPPORTED_REGISTRY_FORMATS))})",
                remediation=(
                    "the registry was written by a newer events-cli; upgrade it "
                    "(pip install -U events-cli) or point EVENTS_HISTORY_DIR at a fresh store"
                ),
            )

        missing = [key for key in _WIRE_KEYS if key not in payload]
        if missing:
            raise RegistryCorruptError(
                f"{origin}: registry record is missing {', '.join(repr(k) for k in missing)}",
                remediation=_repair_hint(origin),
            )
        values = {key: payload[key] for key in _WIRE_KEYS}
        if any(not isinstance(value, str) for value in values.values()):
            raise RegistryCorruptError(
                f"{origin}: every registry record field must be a string",
                remediation=_repair_hint(origin),
            )
        return cls(
            name=values[_WIRE_NAME],
            pattern=values[_WIRE_PATTERN],
            topic_filter=values[_WIRE_FILTER],
            owner=values[_WIRE_OWNER],
            client_id=values[_WIRE_CLIENT_ID],
            created=values[_WIRE_CREATED],
            registry_format_version=values[_WIRE_VERSION],
        )


def _repair_hint(origin: str) -> str:
    return (
        f"inspect {origin}; the record is damaged on disk. Delete it to drop the "
        "subscription from the registry, then re-add it with 'events sub add'"
    )

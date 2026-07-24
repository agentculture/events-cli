"""The event envelope — the stable public contract of events-cli.

*Mosquitto transports events; events-cli defines what they mean* (issue #1).
This module is the "defines what they mean" half, and it is deliberately the
bottom layer: **standard library only**, no transport client, no container
runtime, no I/O. Every other surface (the CLI, the importable client, and the
derived MCP/HTTP tools) is built on top of it, and the unit suite that gates
code quality can therefore run on a machine with nothing installed.

What consumers may depend on
----------------------------

* The **envelope**, not topic names. Topics are transport-level routing
  details and may change; these field names and rules may not.
* Wire field names are **camelCase** (CloudEvents-compatible); Python
  attributes are idiomatic **snake_case**. :data:`WIRE_FIELD_NAMES` is the
  bridge, and :meth:`Envelope.to_dict` / :meth:`Envelope.from_dict` cross it.
* Absent optional fields are **omitted** from the wire form — never emitted as
  ``null`` — so a consumer never has to special-case a materialised null.
* ``time`` round-trips **verbatim**. Both ``…Z`` and ``…+00:00`` are accepted
  and neither is rewritten into the other.

Idempotency is part of the contract
-----------------------------------

Delivery is at-least-once (QoS 1); exactly-once is an explicit non-goal of the
first release. Consumers **must** therefore dedupe on :attr:`Envelope.id`.
:func:`new_event_id` generates ids suitable for exactly that: unique, stable
once assigned, and lexicographically sortable by generation time.
"""

from __future__ import annotations

import json
import math
import re
import secrets
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from time import time_ns
from types import MappingProxyType
from typing import Any
from urllib.parse import urlsplit

from events_cli.core.errors import ENVELOPE_FIELD, EnvelopeValidationError, FieldError

__all__ = [
    "EVENT_ID_PREFIX",
    "REQUIRED_FIELDS",
    "SCHEMA_VERSION",
    "WIRE_FIELD_NAMES",
    "Envelope",
    "new_event_id",
    "new_id",
    "now_rfc3339",
    "parse_rfc3339",
]

#: Current envelope schema version, emitted as ``schemaVersion``.
SCHEMA_VERSION = "1"

#: Prefix on every generated event id (``evt_01J…``).
EVENT_ID_PREFIX = "evt_"

# --- generated identifiers -------------------------------------------------
#
# ULID layout: 48 bits of millisecond timestamp + 80 bits of entropy, rendered
# in Crockford base32 (26 characters, no I/L/O/U so it survives being read
# aloud or retyped). Two properties matter to this contract:
#
#   * uniqueness — 80 random bits per millisecond, so consumer-side dedup on
#     the id is safe even across machines;
#   * sortability — the timestamp is the high-order part, so ids sort by
#     generation time as plain strings. History windows and cursors are then a
#     string comparison rather than a parsed date.
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_ULID_LENGTH = 26
_RANDOM_BITS = 80
_TIME_MASK = (1 << 48) - 1
_RANDOM_MASK = (1 << _RANDOM_BITS) - 1


def _encode_ulid(millis: int, entropy: int) -> str:
    """Render a timestamp + entropy pair as a 26-character Crockford base32 ULID."""
    value = ((millis & _TIME_MASK) << _RANDOM_BITS) | (entropy & _RANDOM_MASK)
    out = []
    for _ in range(_ULID_LENGTH):
        out.append(_CROCKFORD[value & 0x1F])
        value >>= 5
    return "".join(reversed(out))


def new_id(prefix: str = "") -> str:
    """Return a fresh sortable identifier with ``prefix`` (e.g. ``run_``).

    ``secrets`` supplies the entropy so ids are unguessable as well as unique.
    """
    return f"{prefix}{_encode_ulid(time_ns() // 1_000_000, secrets.randbits(_RANDOM_BITS))}"


def new_event_id() -> str:
    """Return a fresh event id: :data:`EVENT_ID_PREFIX` plus a ULID.

    Suitable as the dedup key a consumer keeps, because delivery is
    at-least-once and the same envelope may arrive more than once.
    """
    return new_id(EVENT_ID_PREFIX)


# --- timestamps ------------------------------------------------------------

_RFC3339_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[Zz]|[+-]\d{2}:\d{2})$"
)


class _TimestampError(ValueError):
    """Internal: a ValueError that also carries the field-level error code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def now_rfc3339() -> str:
    """The current UTC time as an RFC 3339 string with a ``Z`` suffix."""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def parse_rfc3339(value: str) -> datetime:
    """Parse an RFC 3339 **UTC** timestamp, raising ``ValueError`` otherwise.

    Deliberately stricter than :meth:`datetime.fromisoformat`, which also
    accepts date-only and basic-format strings that are not valid RFC 3339
    timestamps, and deliberately UTC-only: a fabric whose events carry mixed
    local offsets cannot be ordered by eye.
    """
    if not isinstance(value, str):
        raise _TimestampError("not_a_string", f"must be a string, got {type(value).__name__}")
    if not _RFC3339_RE.match(value):
        raise _TimestampError(
            "malformed", "must be an RFC 3339 UTC timestamp, e.g. 2026-07-23T15:00:00Z"
        )
    text = f"{value[:-1]}+00:00" if value[-1] in "Zz" else value
    try:
        stamp = datetime.fromisoformat(text)
    except ValueError as exc:  # a real calendar/clock violation, e.g. month 13
        raise _TimestampError("malformed", f"is not a real date and time ({exc})") from exc
    if stamp.utcoffset() != timedelta(0):
        raise _TimestampError("not_utc", "must be UTC (a 'Z' suffix or a +00:00 offset)")
    return stamp


# --- field rules -----------------------------------------------------------
#
# Every rule below is a promise to producers as much as a defence against them,
# so each one is justified rather than merely tight:
#
#   ids       printable, whitespace-free, so an id survives a log line, a URL
#             and a topic segment unescaped.
#   type      lowercase dotted segments (`task.requested`). Case is *not*
#             normalised, it is rejected: silently accepting `Task.Requested`
#             would create two event types that look identical to a human.
#   source    an absolute URI (`agent://builder`, `app://reachy-mini-cli`), so
#             the origin of an event is identifiable without local convention.
#   time      RFC 3339 UTC — see parse_rfc3339.
#   data      a JSON object whose every value JSON can represent.
_MAX_ID_LENGTH = 128
_MAX_TYPE_LENGTH = 128
_MAX_SOURCE_LENGTH = 512
_MAX_SCHEMA_VERSION_LENGTH = 32
_MAX_DATA_DEPTH = 32

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+/=@-]*$")
_TYPE_RE = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$")
# `[0-9]`, deliberately not `\d`: this validates a *wire* format, and Python's
# `\d` matches Unicode decimal digits (Arabic-Indic, Devanagari, …) unless the
# pattern also carries `re.ASCII`. The explicit class cannot be broadened by
# someone later dropping a flag.
_SCHEMA_VERSION_RE = re.compile(r"^[0-9]+(?:\.[0-9]+)*$")
# W3C trace context: version-traceid-parentid-flags, lowercase hex.
_TRACEPARENT_RE = re.compile(r"^[0-9a-f]{2}-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}$")


def _check_text(field_name: str, value: Any, errors: list[FieldError]) -> str | None:
    """Shared string checks. Returns the text, or None if it already failed."""
    if not isinstance(value, str):
        errors.append(
            FieldError(field_name, "not_a_string", f"must be a string, got {type(value).__name__}")
        )
        return None
    if not value:
        errors.append(FieldError(field_name, "empty", "must not be empty"))
        return None
    return value


def _check_identifier(
    field_name: str, value: Any, errors: list[FieldError], *, max_length: int = _MAX_ID_LENGTH
) -> None:
    text = _check_text(field_name, value, errors)
    if text is None:
        return
    if len(text) > max_length:
        errors.append(
            FieldError(field_name, "too_long", f"must be at most {max_length} characters")
        )
        return
    if not _ID_RE.match(text):
        errors.append(
            FieldError(
                field_name,
                "malformed",
                "must start with a letter or digit and contain no whitespace",
            )
        )


def _check_event_type(field_name: str, value: Any, errors: list[FieldError]) -> None:
    text = _check_text(field_name, value, errors)
    if text is None:
        return
    if len(text) > _MAX_TYPE_LENGTH:
        errors.append(
            FieldError(field_name, "too_long", f"must be at most {_MAX_TYPE_LENGTH} characters")
        )
        return
    if not _TYPE_RE.match(text):
        errors.append(
            FieldError(
                field_name,
                "malformed",
                "must be lowercase dotted segments, e.g. 'task.requested'",
            )
        )


def _check_source(field_name: str, value: Any, errors: list[FieldError]) -> None:
    text = _check_text(field_name, value, errors)
    if text is None:
        return
    if len(text) > _MAX_SOURCE_LENGTH:
        errors.append(
            FieldError(field_name, "too_long", f"must be at most {_MAX_SOURCE_LENGTH} characters")
        )
        return
    scheme = ""
    if not any(char.isspace() for char in text):
        try:
            scheme = urlsplit(text).scheme
        except ValueError:  # pragma: no cover - urlsplit rejects only very odd input
            scheme = ""
    if not scheme:
        errors.append(
            FieldError(
                field_name,
                "malformed",
                "must be an absolute URI with a scheme, e.g. 'agent://builder'",
            )
        )


def _check_schema_version(field_name: str, value: Any, errors: list[FieldError]) -> None:
    text = _check_text(field_name, value, errors)
    if text is None:
        return
    if len(text) > _MAX_SCHEMA_VERSION_LENGTH:
        errors.append(FieldError(field_name, "too_long", "must be a short version string"))
        return
    if not _SCHEMA_VERSION_RE.match(text):
        errors.append(
            FieldError(field_name, "malformed", "must be numeric, e.g. '1' or '2.1' (no 'v')")
        )


def _check_time(field_name: str, value: Any, errors: list[FieldError]) -> None:
    if _check_text(field_name, value, errors) is None:
        return
    try:
        parse_rfc3339(value)
    except _TimestampError as exc:
        errors.append(FieldError(field_name, exc.code, str(exc)))


def _check_traceparent(field_name: str, value: Any, errors: list[FieldError]) -> None:
    text = _check_text(field_name, value, errors)
    if text is None:
        return
    malformed = FieldError(
        field_name,
        "malformed",
        "must be a W3C trace context header, e.g. '00-<32 hex>-<16 hex>-01'",
    )
    if not _TRACEPARENT_RE.match(text):
        errors.append(malformed)
        return
    version, trace_id, parent_id, _flags = text.split("-")
    if version == "ff" or set(trace_id) == {"0"} or set(parent_id) == {"0"}:
        errors.append(malformed)


def _check_delivery_attempt(field_name: str, value: Any, errors: list[FieldError]) -> None:
    # bool is a subclass of int, and `True` is not a delivery attempt.
    if isinstance(value, bool) or not isinstance(value, int):
        errors.append(
            FieldError(
                field_name, "not_an_integer", f"must be an integer, got {type(value).__name__}"
            )
        )
        return
    if value < 1:
        errors.append(FieldError(field_name, "out_of_range", "must be 1 or greater"))


def _check_json_value(path: str, value: Any, depth: int, errors: list[FieldError]) -> None:
    """Walk a payload value, reporting anything JSON cannot represent, by path."""
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            errors.append(
                FieldError(
                    path, "malformed", "must be a finite number (NaN and Infinity are not JSON)"
                )
            )
        return
    if isinstance(value, Mapping):
        _check_json_object(path, value, depth, errors)
        return
    if isinstance(value, (list, tuple)):
        _check_json_array(path, value, depth, errors)
        return
    errors.append(
        FieldError(path, "unsupported_type", f"{type(value).__name__} is not a JSON value")
    )


def _check_json_object(
    path: str, value: Mapping[Any, Any], depth: int, errors: list[FieldError]
) -> None:
    if depth >= _MAX_DATA_DEPTH:
        errors.append(FieldError(path, "too_deep", f"nests deeper than {_MAX_DATA_DEPTH} levels"))
        return
    for key, item in value.items():
        if not isinstance(key, str):
            errors.append(
                FieldError(
                    path,
                    "unsupported_type",
                    f"object keys must be strings, got {type(key).__name__}",
                )
            )
            continue
        _check_json_value(f"{path}.{key}", item, depth + 1, errors)


def _check_json_array(path: str, value: Any, depth: int, errors: list[FieldError]) -> None:
    if depth >= _MAX_DATA_DEPTH:
        errors.append(FieldError(path, "too_deep", f"nests deeper than {_MAX_DATA_DEPTH} levels"))
        return
    for index, item in enumerate(value):
        _check_json_value(f"{path}[{index}]", item, depth + 1, errors)


def _check_data(field_name: str, value: Any, errors: list[FieldError]) -> None:
    if not isinstance(value, Mapping):
        errors.append(
            FieldError(
                field_name, "not_an_object", f"must be a JSON object, got {type(value).__name__}"
            )
        )
        return
    _check_json_value(field_name, value, 0, errors)


# --- the envelope ----------------------------------------------------------


# No `slots=True`: with a frozen dataclass the generated __setattr__ closes over
# the pre-slots class, so assigning an *unknown* attribute reports a confusing
# TypeError instead of an AttributeError. A contract type should fail clearly.
@dataclass(frozen=True)
class Envelope:
    """An immutable, CloudEvents-compatible event envelope.

    Frozen because events are facts: once emitted, an event is never edited.
    Derive a changed copy with :func:`dataclasses.replace` instead.

    ``data`` is copied into a plain dict at construction, so a later mutation
    of the caller's dict cannot reach inside an event that has already been
    emitted. That copy is shallow — nested structures are shared, and callers
    must treat ``data`` as read-only. Deep-freezing arbitrary JSON would cost
    every stdlib idiom (``dataclasses.asdict``, ``copy.deepcopy``, ``pickle``)
    that the rest of the stack expects to work.

    Equality compares every field; hashing ignores ``data`` so that envelopes
    can live in a set. Dedup should key on :attr:`id` regardless: delivery is
    at-least-once, so the same envelope may be received more than once.
    """

    id: str
    type: str
    source: str
    time: str
    schema_version: str = SCHEMA_VERSION
    correlation_id: str | None = None
    causation_id: str | None = None
    run_id: str | None = None
    traceparent: str | None = None
    producer: str | None = None
    delivery_attempt: int | None = None
    data: Mapping[str, Any] = field(default_factory=dict, hash=False)

    def __post_init__(self) -> None:
        # Defensive copy; a non-mapping is left alone for validate() to report.
        if isinstance(self.data, Mapping):
            object.__setattr__(self, "data", dict(self.data))

    @classmethod
    def new(
        cls,
        type: str,
        source: str,
        *,
        data: Mapping[str, Any] | None = None,
        schema_version: str = SCHEMA_VERSION,
        correlation_id: str | None = None,
        causation_id: str | None = None,
        run_id: str | None = None,
        traceparent: str | None = None,
        producer: str | None = None,
        delivery_attempt: int | None = None,
        id: str | None = None,
        time: str | None = None,
    ) -> "Envelope":
        """Mint a new event, generating ``id`` and ``time``, and validate it.

        Validating here means a producer learns about a bad event type at the
        point of creation rather than after it has reached the transport.
        """
        envelope = cls(
            id=id if id is not None else new_event_id(),
            type=type,
            source=source,
            time=time if time is not None else now_rfc3339(),
            schema_version=schema_version,
            correlation_id=correlation_id,
            causation_id=causation_id,
            run_id=run_id,
            traceparent=traceparent,
            producer=producer,
            delivery_attempt=delivery_attempt,
            data=data if data is not None else {},
        )
        envelope.validate()
        return envelope

    # -- validation --------------------------------------------------------

    def validation_errors(self) -> tuple[FieldError, ...]:
        """Every field-level problem with this envelope, in field order."""
        return tuple(_collect_errors(self))

    def validate(self) -> None:
        """Raise :class:`EnvelopeValidationError` if this envelope is invalid."""
        errors = self.validation_errors()
        if errors:
            raise EnvelopeValidationError(errors)

    # -- serialisation -----------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """The wire form: camelCase keys, absent optionals omitted entirely."""
        payload: dict[str, Any] = {}
        for name, wire_name in WIRE_FIELD_NAMES.items():
            value = getattr(self, name)
            if name == "data":
                payload[wire_name] = dict(value) if isinstance(value, Mapping) else value
            elif value is not None:
                payload[wire_name] = value
        return payload

    def to_json(self) -> str:
        """The wire form as compact JSON text — what a transport carries."""
        return json.dumps(
            self.to_dict(), ensure_ascii=False, separators=(",", ":"), allow_nan=False
        )

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "Envelope":
        """Parse a wire form, validating it. Raises :class:`EnvelopeValidationError`.

        This is the trust boundary — anything arriving from a transport, a file
        or an agent comes through here — so parsing always validates, and every
        problem in the payload is reported in one pass.
        """
        if not isinstance(payload, Mapping):
            raise EnvelopeValidationError(
                [
                    FieldError(
                        ENVELOPE_FIELD,
                        "not_an_object",
                        f"must be a JSON object, got {type(payload).__name__}",
                    )
                ]
            )

        errors: list[FieldError] = []
        for key in payload:
            if key not in _WIRE_TO_PYTHON:
                errors.append(FieldError(str(key), "unknown_field", _unknown_field_message(key)))

        kwargs = {
            name: payload[wire_name]
            for name, wire_name in WIRE_FIELD_NAMES.items()
            if wire_name in payload
        }
        missing = tuple(name for name in REQUIRED_FIELDS if name not in kwargs)
        for name in missing:
            errors.append(FieldError(WIRE_FIELD_NAMES[name], "missing", "is required"))
            kwargs[name] = None  # placeholder: reported already, not re-checked

        envelope = cls(**kwargs)
        errors.extend(_collect_errors(envelope, skip=frozenset(missing)))
        if errors:
            raise EnvelopeValidationError(errors)
        return envelope

    @classmethod
    def from_json(cls, payload: str | bytes | bytearray) -> "Envelope":
        """Parse JSON text or bytes into a validated envelope.

        Malformed JSON surfaces as an :class:`EnvelopeValidationError` too, so a
        consumer draining a transport has exactly one failure type to handle.
        """
        try:
            decoded = json.loads(payload)
        except (TypeError, ValueError) as exc:
            raise EnvelopeValidationError(
                [FieldError(ENVELOPE_FIELD, "not_json", f"is not valid JSON ({exc})")]
            ) from exc
        return cls.from_dict(decoded)


# --- the snake_case <-> camelCase bridge -----------------------------------
#
# One mapping, declared once, in wire order. Adding a field to Envelope without
# adding it here is caught by the test suite rather than shipping a field that
# silently never reaches the wire.
WIRE_FIELD_NAMES: Mapping[str, str] = MappingProxyType(
    {
        "id": "id",
        "type": "type",
        "source": "source",
        "time": "time",
        "schema_version": "schemaVersion",
        "correlation_id": "correlationId",
        "causation_id": "causationId",
        "run_id": "runId",
        # W3C trace context spells its header lowercase; keep the wire name
        # identical to the header rather than camelCasing it into traceParent.
        "traceparent": "traceparent",
        "producer": "producer",
        "delivery_attempt": "deliveryAttempt",
        "data": "data",
    }
)

_WIRE_TO_PYTHON: Mapping[str, str] = MappingProxyType(
    {wire_name: name for name, wire_name in WIRE_FIELD_NAMES.items()}
)

#: The six fields issue #1 lists as required. Everything else is optional and,
#: when absent, is omitted from the wire form rather than emitted as null.
REQUIRED_FIELDS: tuple[str, ...] = (
    "id",
    "type",
    "source",
    "time",
    "schema_version",
    "data",
)

_CHECKS = {
    "id": _check_identifier,
    "type": _check_event_type,
    "source": _check_source,
    "time": _check_time,
    "schema_version": _check_schema_version,
    "correlation_id": _check_identifier,
    "causation_id": _check_identifier,
    "run_id": _check_identifier,
    "traceparent": _check_traceparent,
    "producer": _check_identifier,
    "delivery_attempt": _check_delivery_attempt,
    "data": _check_data,
}

#: Fields that may be absent (``None``) rather than validated.
_OPTIONAL_FIELDS = frozenset(WIRE_FIELD_NAMES) - frozenset(REQUIRED_FIELDS)


def _collect_errors(envelope: Envelope, skip: frozenset[str] = frozenset()) -> list[FieldError]:
    """Validate every field once, in declaration order, collecting all reasons."""
    errors: list[FieldError] = []
    for name, wire_name in WIRE_FIELD_NAMES.items():
        if name in skip:
            continue
        value = getattr(envelope, name)
        if value is None and name in _OPTIONAL_FIELDS:
            continue
        _CHECKS[name](wire_name, value, errors)
    return errors


def _unknown_field_message(key: Any) -> str:
    suggestion = _suggest_wire_name(key)
    if suggestion is not None:
        return f"is not an envelope field; did you mean {suggestion!r}?"
    return "is not an envelope field (put producer-specific values inside 'data')"


def _suggest_wire_name(key: Any) -> str | None:
    """Map a near-miss key (snake_case, wrong case) onto its wire name."""
    if not isinstance(key, str):
        return None
    direct = WIRE_FIELD_NAMES.get(key)
    if direct is not None:
        return direct
    normalised = key.replace("_", "").replace("-", "").lower()
    for wire_name in WIRE_FIELD_NAMES.values():
        if wire_name.lower() == normalised:
            return wire_name
    return None

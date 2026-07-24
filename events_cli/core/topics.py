"""Canonical mapping between dotted event types and MQTT topics.

*Mosquitto transports events; events-cli defines what they mean* (issue #1).
Topic names are transport-level routing details, not part of the contract a
consumer may depend on (see :mod:`events_cli.core.envelope`'s module
docstring) — but the *mapping* from a validated event ``type`` onto a topic is
itself part of the contract every other layer builds on: the importable
client publishes to it, the subscription registry (#7) compiles patterns with
it, and the drain engine (#8) and CLI verbs (#9/#10) never invent topic
strings by hand. This module is the one place that mapping is defined.

Standard library only — no transport client, no container runtime, no I/O —
for the same reason :mod:`events_cli.core.envelope` is: the unit suite that
gates code quality runs on a machine with nothing installed.

The scheme
----------

A dot in a dotted event ``type`` becomes an MQTT topic **level**; the literal
segment ``events`` is prepended to every topic and filter this module
produces::

    task.requested   -> events/task/requested
    heartbeat        -> events/heartbeat
    a.b.c            -> events/a/b/c

Only ``.`` is a level separator. ``_`` and ``-`` are valid *inside* a segment
(the same grammar :func:`events_cli.core.envelope.Envelope`'s ``type`` field
accepts — ``scope_completed`` and ``v1.a-b`` are both valid types), and
neither ever creates a topic level: ``scope_completed`` maps to
``events/scope_completed``, one level, not two.

The ``events/`` prefix is mandatory and load-bearing. It is what keeps this
contract lane separate from a producer's own topic trees — for example
``reachy-mini-cli``'s ``reachy/events/{source}/{type}`` and retained
``reachy/state/{key}`` (docs/contract.md) — and no dotted event type can spell
its way out of it: an event literally typed ``reachy.state.updated`` still
maps to ``events/reachy/state/updated``, never to ``reachy/state/updated``.

A **pattern** additionally allows ``*`` (:data:`PATTERN_WILDCARD`) as a
stand-in for exactly one dotted segment: ``task.*`` selects every *immediate*
child of ``task`` (``task.requested``, ``task.completed``, ...) but not a
grandchild like ``task.sub.completed``. It compiles to MQTT's *single-level*
wildcard ``+`` (:data:`MQTT_SINGLE_LEVEL_WILDCARD`) — **never** the
multi-level ``#`` (:data:`MQTT_MULTI_LEVEL_WILDCARD`), because ``#`` also
matches every level *below* the one it appears at, which would let a pattern
silently widen with every extra segment a producer later adds.

A pattern containing a **raw MQTT filter character** (``#``, ``+``, or the
level separator ``/`` itself) is rejected outright rather than passed
through: those characters carry MQTT structural meaning that a hand-typed
dotted pattern must never smuggle in — most importantly, they are exactly
what would let a pattern escape the ``events/`` prefix (``events/../reachy/#``
has no dotted equivalent, and it must stay that way). This is a confirmed
finding from the spec's challenge pass, not a defensive guess.
"""

from __future__ import annotations

from events_cli.core.envelope import _check_event_type, _check_text
from events_cli.core.errors import FieldError, TopicValidationError

__all__ = [
    "MQTT_MULTI_LEVEL_WILDCARD",
    "MQTT_SINGLE_LEVEL_WILDCARD",
    "PATTERN_WILDCARD",
    "TOPIC_PREFIX",
    "filter_matches_topic",
    "pattern_to_filter",
    "topic_to_type",
    "type_to_topic",
]

#: The mandatory first topic level of every contract-lane topic and filter.
TOPIC_PREFIX = "events"

#: A pattern's stand-in for exactly one dotted segment, e.g. ``task.*``.
PATTERN_WILDCARD = "*"

#: MQTT's single-level wildcard — what :data:`PATTERN_WILDCARD` compiles to.
MQTT_SINGLE_LEVEL_WILDCARD = "+"

#: MQTT's multi-level wildcard. :func:`pattern_to_filter` never emits this —
#: only :func:`filter_matches_topic` needs to recognise it, e.g. to prove a
#: *foreign* filter like ``reachy/#`` cannot reach into the contract lane.
MQTT_MULTI_LEVEL_WILDCARD = "#"

# The three characters MQTT gives structural meaning to: the multi-level and
# single-level wildcards, and the level separator itself. None may appear
# inside a dotted pattern — see the module docstring.
_RESERVED_MQTT_CHARS = (MQTT_MULTI_LEVEL_WILDCARD, MQTT_SINGLE_LEVEL_WILDCARD, "/")

_EVENTS_PREFIX = f"{TOPIC_PREFIX}/"


def _check_no_reserved_mqtt_chars(field_name: str, text: str, errors: list[FieldError]) -> None:
    found = sorted(ch for ch in _RESERVED_MQTT_CHARS if ch in text)
    if found:
        chars = ", ".join(repr(ch) for ch in found)
        errors.append(
            FieldError(
                field_name,
                "reserved_mqtt_char",
                f"must not contain the raw MQTT filter character(s) {chars} "
                "(write a dotted pattern instead, e.g. 'task.*')",
            )
        )


def type_to_topic(event_type: str) -> str:
    """Map a dotted event type onto its canonical topic.

    ``task.requested`` -> ``events/task/requested``. Validates ``event_type``
    with the exact same grammar :class:`~events_cli.core.envelope.Envelope`
    enforces on its ``type`` field — reusing
    :func:`events_cli.core.envelope._check_event_type` rather than
    re-implementing it, so a value good enough to publish is also good enough
    to route: one grammar, not two that can drift apart.

    Raises :class:`TopicValidationError` (field ``type``) if ``event_type``
    is not a valid event type.
    """
    errors: list[FieldError] = []
    _check_event_type("type", event_type, errors)
    if errors:
        raise TopicValidationError(errors, summary="invalid event type")
    return "/".join((TOPIC_PREFIX, *event_type.split(".")))


def topic_to_type(topic: str) -> str:
    """Invert :func:`type_to_topic`: ``events/task/requested`` -> ``task.requested``.

    Rejects anything outside the contract lane (no literal ``events/``
    prefix), anything with an empty topic level, and anything that would not
    itself be a valid event type once its levels are rejoined with dots — so
    this is the one trust boundary a consumer draining a raw topic string
    must pass through before treating the result as a ``type``.

    Raises :class:`TopicValidationError` (field ``topic``).
    """
    errors: list[FieldError] = []
    text = _check_text("topic", topic, errors)
    if text is None:
        raise TopicValidationError(errors, summary="invalid topic")

    if not text.startswith(_EVENTS_PREFIX):
        errors.append(
            FieldError(
                "topic", "malformed", f"must start with '{_EVENTS_PREFIX}' (the contract lane)"
            )
        )
        raise TopicValidationError(errors, summary="invalid topic")

    remainder = text[len(_EVENTS_PREFIX) :]
    segments = remainder.split("/")
    if not remainder or any(segment == "" for segment in segments):
        errors.append(FieldError("topic", "malformed", "must not contain an empty topic level"))
        raise TopicValidationError(errors, summary="invalid topic")

    candidate = ".".join(segments)
    _check_event_type("topic", candidate, errors)
    if errors:
        raise TopicValidationError(errors, summary="invalid topic")
    return candidate


def pattern_to_filter(pattern: str) -> str:
    """Compile a dotted pattern into an MQTT topic filter.

    ``task.*`` -> ``events/task/+``. :data:`PATTERN_WILDCARD` (``*``) stands
    for exactly one dotted segment and always compiles to MQTT's
    single-level wildcard ``+``, never the multi-level ``#`` — see the module
    docstring for why that distinction is load-bearing. A pattern with no
    wildcard at all compiles identically to :func:`type_to_topic`.

    Every non-wildcard segment is validated by reusing
    :func:`events_cli.core.envelope._check_event_type`, one segment at a
    time — which is why a broken segment is reported precisely (which piece
    is empty or malformed), the same "report the exact path" idiom the
    envelope core uses for nested ``data`` payloads.

    Any raw MQTT filter character in ``pattern`` itself (``#``, ``+``, ``/``)
    is rejected up front, with its own field-error code
    (``reserved_mqtt_char``), rather than silently passed through — a
    confirmed security finding: those characters could otherwise let a
    pattern escape the ``events/`` prefix or open a far broader subscription
    than its dotted spelling suggests.

    Raises :class:`TopicValidationError` (field ``pattern``).
    """
    errors: list[FieldError] = []
    text = _check_text("pattern", pattern, errors)
    if text is None:
        raise TopicValidationError(errors, summary="invalid topic pattern")

    _check_no_reserved_mqtt_chars("pattern", text, errors)
    if errors:
        raise TopicValidationError(errors, summary="invalid topic pattern")

    compiled_segments: list[str] = []
    for segment in text.split("."):
        if segment == PATTERN_WILDCARD:
            compiled_segments.append(MQTT_SINGLE_LEVEL_WILDCARD)
            continue
        segment_errors: list[FieldError] = []
        _check_event_type("pattern", segment, segment_errors)
        if segment_errors:
            errors.extend(segment_errors)
        else:
            compiled_segments.append(segment)

    if errors:
        raise TopicValidationError(errors, summary="invalid topic pattern")
    return "/".join((TOPIC_PREFIX, *compiled_segments))


def filter_matches_topic(topic_filter: str, topic: str) -> bool:
    """Pure MQTT topic-filter matching: ``+`` is single-level, ``#`` is the rest, else literal.

    Deliberately independent of the compiling functions above — it accepts
    any two strings, not only ones this module produced — so it can serve as
    an impartial proof that a contract-lane filter (always ``events/``
    -prefixed) can never match a producer-owned topic such as
    ``reachy/state/updated``, and that a producer's own ``reachy/#``
    subscription can never see contract-lane traffic either.

    Implements the same semantics MQTT brokers use for subscription
    matching: ``#`` matches its own level and every level below (so
    ``sport/#`` matches both ``sport`` and ``sport/tennis/player1``); ``+``
    matches exactly one level, no more and no fewer; anything else must
    match the topic's level literally.
    """
    filter_levels = topic_filter.split("/")
    topic_levels = topic.split("/")
    for index, level in enumerate(filter_levels):
        if level == MQTT_MULTI_LEVEL_WILDCARD:
            return index == len(filter_levels) - 1
        if index >= len(topic_levels):
            return False
        if level != MQTT_SINGLE_LEVEL_WILDCARD and level != topic_levels[index]:
            return False
    return len(filter_levels) == len(topic_levels)

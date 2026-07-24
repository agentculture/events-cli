"""Contract tests for the canonical type <-> topic mapping (events_cli/core/topics.py).

Topic names are transport-level routing details (see events_cli/core/envelope.py's
module docstring: "consumers may depend on the envelope, not topic names"), but the
*mapping* between a dotted event type and its MQTT topic is itself part of the
contract other modules build on (the subscription registry in #7, the drain engine
in #8, `events emit`/`events sub` in #9/#10) — so it is specified and pinned here,
exactly like the envelope is pinned in test_envelope.py.

Every test in this file runs with **no docker and no broker**, matching
test_envelope.py's acceptance criterion 1. `events_cli/core/topics.py` is picked up
automatically by test_envelope.py's whole-of-`core` AST scan
(`test_core_imports_only_the_standard_library` /
`test_core_code_names_no_transport_or_container_machinery`, both of which glob
every ``*.py`` in the package); this file additionally re-proves the import
constraint on its own, so it does not depend on that other test file running.
"""

from __future__ import annotations

import ast
import json
import random
import sys
from pathlib import Path

import pytest

from events_cli.core import (
    ERROR_CODES,
    MQTT_MULTI_LEVEL_WILDCARD,
    MQTT_SINGLE_LEVEL_WILDCARD,
    PATTERN_WILDCARD,
    TOPIC_PREFIX,
    Envelope,
    EnvelopeValidationError,
    EventsError,
    FieldError,
    TopicValidationError,
    filter_matches_topic,
    pattern_to_filter,
    topic_to_type,
    type_to_topic,
)
from events_cli.core.envelope import _check_event_type  # internal: pinned by reuse tests

# --- fixtures / helpers ------------------------------------------------------

# The two producer-owned trees named in docs/contract.md and CLAUDE.md's "raw MQTT
# port" constraint: `reachy-mini-cli` owns `reachy/events/{source}/{type}` (not
# retained) and retained `reachy/state/{key}`. These are the concrete examples the
# exclusion proof below is built against.
REACHY_EVENT_TOPIC = "reachy/events/reachy-mini-cli/battery.low"
REACHY_STATE_TOPIC = "reachy/state/battery"
REACHY_WILDCARD_FILTER = "reachy/#"
REACHY_STATE_WILDCARD_FILTER = "reachy/state/#"

_ALNUM = "abcdefghijklmnopqrstuvwxyz0123456789"


def _random_alnum_run(rng: random.Random) -> str:
    return "".join(rng.choice(_ALNUM) for _ in range(rng.randint(1, 6)))


def _random_segment(rng: random.Random) -> str:
    """One dotted segment: alnum runs joined by '_' or '-' (never '.') internally."""
    runs = [_random_alnum_run(rng) for _ in range(rng.randint(1, 3))]
    pieces = [runs[0]]
    for run in runs[1:]:
        pieces.append(rng.choice("_-"))
        pieces.append(run)
    return "".join(pieces)


def random_event_type(rng: random.Random) -> str:
    """A random dotted event type matching the same grammar test_envelope.py exercises."""
    depth = rng.randint(1, 4)
    return ".".join(_random_segment(rng) for _ in range(depth))


def assert_is_valid_event_type(event_type: str) -> None:
    """Independent confirmation via the real envelope contract, not just this module's opinion."""
    Envelope(
        id="evt_01JZ8QK3W6X7Y8Z9A0B1C2D3E4",
        type=event_type,
        source="agent://builder",
        time="2026-07-23T15:00:00Z",
        data={},
    ).validate()


# --- acceptance criterion: pure, stdlib-only, dockerless ---------------------


def test_topics_module_imports_only_the_standard_library() -> None:
    """Self-contained re-proof (independent of test_envelope.py) that topics.py is pure."""
    import events_cli.core.topics as topics_module

    path = Path(topics_module.__file__).resolve()
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "topics.py must use absolute imports"
            names = [node.module or ""]
        else:
            continue
        for name in names:
            root = name.split(".")[0]
            if root == "events_cli":
                assert name.startswith(
                    "events_cli.core"
                ), f"topics.py may not import {name} (it is core, the bottom layer)"
                continue
            assert root in sys.stdlib_module_names, f"topics.py imports non-stdlib {name}"


def test_topic_error_is_not_a_cli_error() -> None:
    from events_cli.cli._errors import CliError

    assert issubclass(TopicValidationError, EventsError)
    assert not issubclass(TopicValidationError, CliError)


def test_topic_validation_error_is_a_sibling_of_envelope_validation_error() -> None:
    """Distinct error types: a bad topic string is not an invalid envelope."""
    assert not issubclass(TopicValidationError, EnvelopeValidationError)
    assert not issubclass(EnvelopeValidationError, TopicValidationError)


# --- constants ----------------------------------------------------------------


def test_wildcard_and_prefix_constants_are_pinned() -> None:
    assert TOPIC_PREFIX == "events"
    assert PATTERN_WILDCARD == "*"
    assert MQTT_SINGLE_LEVEL_WILDCARD == "+"
    assert MQTT_MULTI_LEVEL_WILDCARD == "#"


# --- type_to_topic --------------------------------------------------------------


@pytest.mark.parametrize(
    "event_type,expected_topic",
    [
        ("task.requested", "events/task/requested"),
        ("heartbeat", "events/heartbeat"),
        ("a.b.c", "events/a/b/c"),
        ("scope.completed", "events/scope/completed"),
        # '_' and '-' are valid *within* a segment (see the type grammar in
        # envelope.py) but only '.' becomes a topic-level separator.
        ("scope_completed", "events/scope_completed"),
        ("v1.a-b", "events/v1/a-b"),
        ("reachy.emotion", "events/reachy/emotion"),
    ],
)
def test_type_to_topic_maps_dots_to_topic_levels(event_type: str, expected_topic: str) -> None:
    assert_is_valid_event_type(event_type)
    assert type_to_topic(event_type) == expected_topic


def test_type_to_topic_reuses_envelope_type_validation_exactly() -> None:
    """Not a re-implementation: same private helper, same FieldError objects.

    This is the literal proof of the plan's "reuse core.envelope's type
    validation for segments" instruction — the expected errors are produced by
    calling envelope's own checker directly, then compared for equality.
    """
    for bad_type in ("Task.Requested", "", "task requested", "task..requested", "x" * 200, 42):
        expected: list[FieldError] = []
        _check_event_type("type", bad_type, expected)
        assert expected, "the fixture must actually be invalid"
        with pytest.raises(TopicValidationError) as exc:
            type_to_topic(bad_type)  # type: ignore[arg-type]
        assert list(exc.value.errors) == expected
        assert exc.value.fields == ("type",)


def test_type_to_topic_rejects_non_string() -> None:
    with pytest.raises(TopicValidationError) as exc:
        type_to_topic(None)  # type: ignore[arg-type]
    assert [e.code for e in exc.value.errors] == ["not_a_string"]


# --- topic_to_type (the inverse) -----------------------------------------------


def test_topic_to_type_round_trips_type_to_topic_property_based() -> None:
    """serialize -> parse preserves the type, property-style (300 cases, no docker)."""
    rng = random.Random(20260724)  # nosec B311 - deterministic test data, not crypto
    for _ in range(300):
        event_type = random_event_type(rng)
        assert_is_valid_event_type(event_type)
        topic = type_to_topic(event_type)
        assert topic.startswith(f"{TOPIC_PREFIX}/")
        assert topic_to_type(topic) == event_type


@pytest.mark.parametrize(
    "event_type,topic",
    [
        ("task.requested", "events/task/requested"),
        ("heartbeat", "events/heartbeat"),
        ("v1.a-b", "events/v1/a-b"),
    ],
)
def test_topic_to_type_is_the_named_inverse(event_type: str, topic: str) -> None:
    assert topic_to_type(topic) == event_type
    assert type_to_topic(event_type) == topic


@pytest.mark.parametrize(
    "topic",
    [
        REACHY_STATE_TOPIC,
        REACHY_EVENT_TOPIC,
        "foo/bar",
        "eventsx/task/requested",  # near-miss prefix, deliberately not matched
        "Events/task/requested",  # case matters: the prefix is not normalised
    ],
)
def test_topic_to_type_rejects_topics_outside_the_events_prefix(topic: str) -> None:
    with pytest.raises(TopicValidationError) as exc:
        topic_to_type(topic)
    assert exc.value.fields == ("topic",)


@pytest.mark.parametrize(
    "topic", ["events", "events/", "events//task", "events/task/", "events/Task/Requested"]
)
def test_topic_to_type_rejects_malformed_shapes(topic: str) -> None:
    with pytest.raises(TopicValidationError) as exc:
        topic_to_type(topic)
    assert exc.value.fields == ("topic",)


def test_topic_to_type_rejects_non_string() -> None:
    with pytest.raises(TopicValidationError) as exc:
        topic_to_type(42)  # type: ignore[arg-type]
    assert [e.code for e in exc.value.errors] == ["not_a_string"]


# --- pattern_to_filter ----------------------------------------------------------


@pytest.mark.parametrize(
    "pattern,expected_filter",
    [
        ("task.requested", "events/task/requested"),  # no wildcard: same as type_to_topic
        ("task.*", "events/task/+"),
        ("*", "events/+"),
        ("*.completed", "events/+/completed"),
        ("task.*.done", "events/task/+/done"),
        ("*.*", "events/+/+"),
    ],
)
def test_pattern_to_filter_compiles_the_documented_wildcard_semantics(
    pattern: str, expected_filter: str
) -> None:
    assert pattern_to_filter(pattern) == expected_filter


def test_pattern_to_filter_without_a_wildcard_matches_type_to_topic() -> None:
    """A plain (non-wildcard) pattern is just a type; the two functions must agree."""
    assert pattern_to_filter("task.requested") == type_to_topic("task.requested")


def test_pattern_wildcard_matches_exactly_one_segment_not_deeper_or_shallower() -> None:
    """The documented semantics: '*' compiles to MQTT '+', which is single-level only."""
    compiled = pattern_to_filter("task.*")
    assert compiled == "events/task/+"
    assert filter_matches_topic(compiled, "events/task/requested")
    assert filter_matches_topic(compiled, "events/task/completed")
    assert not filter_matches_topic(compiled, "events/task/sub/completed"), "too deep"
    assert not filter_matches_topic(compiled, "events/task"), "too shallow"


def test_bare_wildcard_pattern_matches_only_single_segment_types() -> None:
    """'*' alone is not "match everything" — it is still exactly-one-segment."""
    compiled = pattern_to_filter("*")
    assert compiled == "events/+"
    assert filter_matches_topic(compiled, "events/heartbeat")
    assert not filter_matches_topic(compiled, "events/task/requested")


@pytest.mark.parametrize(
    "pattern",
    [
        "task/*",
        "task.#",
        "task.+",
        "#",
        "+",
        "/",
        "task#done",
        "ta+sk.requested",
        REACHY_WILDCARD_FILTER,
        REACHY_STATE_WILDCARD_FILTER,
    ],
)
def test_pattern_to_filter_rejects_raw_mqtt_filter_characters(pattern: str) -> None:
    """A confirmed security finding: a pattern must never smuggle a raw MQTT
    wildcard or level separator past the dotted grammar and into the compiled
    filter — doing so could let a pattern escape the events/ prefix."""
    with pytest.raises(TopicValidationError) as exc:
        pattern_to_filter(pattern)
    (err,) = exc.value.errors
    assert err.field == "pattern"
    assert err.code == "reserved_mqtt_char"
    assert exc.value.to_dict()["error"] == "topic_validation"


@pytest.mark.parametrize(
    "pattern",
    ["Task.*", "task..*", ".*", "*.", "task .requested", "x" * 200 + ".*"],
)
def test_pattern_to_filter_rejects_invalid_segments_reusing_type_grammar(pattern: str) -> None:
    with pytest.raises(TopicValidationError) as exc:
        pattern_to_filter(pattern)
    assert exc.value.fields == ("pattern",)


def test_pattern_to_filter_rejects_empty_and_non_string() -> None:
    with pytest.raises(TopicValidationError) as exc:
        pattern_to_filter("")
    assert [e.code for e in exc.value.errors] == ["empty"]
    with pytest.raises(TopicValidationError) as exc:
        pattern_to_filter(None)  # type: ignore[arg-type]
    assert [e.code for e in exc.value.errors] == ["not_a_string"]


# --- filter_matches_topic: pure MQTT wildcard semantics --------------------------


@pytest.mark.parametrize(
    "topic_filter,topic,expected",
    [
        ("events/task/+", "events/task/requested", True),
        ("events/task/+", "events/task/completed", True),
        ("events/task/+", "events/task/sub/completed", False),
        ("events/task/+", "events/task", False),
        ("events/+", "events/heartbeat", True),
        ("events/+", "events/task/requested", False),
        ("events/#", "events/task/requested", True),
        ("events/#", "events", True),
        ("events/#", "events/a/b/c/d", True),
        (REACHY_WILDCARD_FILTER, "reachy/state/updated", True),
        (REACHY_WILDCARD_FILTER, "events/task/requested", False),
        ("events/task/requested", "events/task/requested", True),
        ("events/task/requested", "events/task/completed", False),
    ],
)
def test_filter_matches_topic_implements_mqtt_wildcard_semantics(
    topic_filter: str, topic: str, expected: bool
) -> None:
    assert filter_matches_topic(topic_filter, topic) is expected


# --- the reachy/# exclusion proof (the load-bearing acceptance criterion) -------


def test_reachy_owned_topics_are_never_captured_by_a_contract_lane_filter() -> None:
    """The mandatory events/ prefix is what keeps this contract lane out of
    reachy-mini-cli's producer-owned tree (docs/contract.md): `reachy/events/{source}/{type}`
    and retained `reachy/state/{key}`. Proven with real MQTT wildcard-matching
    semantics (`filter_matches_topic`), not a bare string comparison.
    """
    compiled = [
        type_to_topic("task.requested"),
        pattern_to_filter("task.*"),
        pattern_to_filter("*"),
        pattern_to_filter("*.*"),
        # Even a producer choosing an event type that *looks* like it wants the
        # reachy tree stays inside events/ — the prefix cannot be spelled away.
        type_to_topic("reachy.state.updated"),
        pattern_to_filter("reachy.events.battery_low"),
    ]
    for value in compiled:
        assert value.startswith(f"{TOPIC_PREFIX}/")
        assert not filter_matches_topic(value, REACHY_EVENT_TOPIC)
        assert not filter_matches_topic(value, REACHY_STATE_TOPIC)


def test_reachy_wildcard_filters_never_match_a_contract_lane_topic_or_filter() -> None:
    """The reverse direction: a reachy `#` subscription must not see contract-lane traffic,
    and the literal strings named in the acceptance criterion ('reachy/#',
    'reachy/state/#') never match anything this module compiles."""
    compiled = [
        type_to_topic("task.requested"),
        pattern_to_filter("task.*"),
        pattern_to_filter("*"),
    ]
    for value in compiled:
        assert not filter_matches_topic(REACHY_WILDCARD_FILTER, value)
        assert not filter_matches_topic(REACHY_STATE_WILDCARD_FILTER, value)
        # Named in the acceptance criterion verbatim, treated as topic strings:
        assert not filter_matches_topic(value, "reachy/#")
        assert not filter_matches_topic(value, "reachy/state/#")


def test_no_random_dotted_type_or_pattern_can_escape_the_events_prefix() -> None:
    """Property-based exclusion proof: hundreds of random dotted names, including
    ones that could plausibly try to name the reachy tree, never escape events/.
    """
    rng = random.Random(424242)  # nosec B311 - deterministic test data, not crypto
    for _ in range(300):
        event_type = random_event_type(rng)
        topic = type_to_topic(event_type)
        assert topic.split("/", 1)[0] == TOPIC_PREFIX
        assert not filter_matches_topic(REACHY_WILDCARD_FILTER, topic)
        assert not filter_matches_topic(REACHY_STATE_WILDCARD_FILTER, topic)
        assert not filter_matches_topic(topic, REACHY_EVENT_TOPIC)
        assert not filter_matches_topic(topic, REACHY_STATE_TOPIC)


# --- the error object itself -----------------------------------------------------


def test_topic_validation_error_reports_field_message_and_json_shape() -> None:
    with pytest.raises(TopicValidationError) as exc:
        pattern_to_filter("task/*")
    err = exc.value
    assert err.fields == ("pattern",)
    assert "pattern" in str(err)
    payload = err.to_dict()
    assert payload["error"] == "topic_validation"
    assert payload["message"]
    assert payload["errors"] == [
        {"field": "pattern", "code": "reserved_mqtt_char", "message": err.errors[0].message}
    ]
    assert json.loads(json.dumps(payload)) == payload


def test_reserved_mqtt_char_code_is_declared_in_the_shared_error_codes_tuple() -> None:
    assert "reserved_mqtt_char" in ERROR_CODES
    assert len(set(ERROR_CODES)) == len(ERROR_CODES)

"""Contract tests for the pure event-envelope core.

These tests are the executable form of the ``events-cli`` event contract: the
envelope is the stable public surface (topic names are transport-level routing
details), so anything asserted here is a promise to consumers.

Every test in this file runs with **no docker and no broker** — that is
acceptance criterion 1, and it is enforced mechanically by
``test_core_imports_only_the_standard_library``.
"""

from __future__ import annotations

import ast
import dataclasses
import json
import math
import random
import sys
import tokenize
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import MappingProxyType

import pytest

from events_cli.core import (
    ERROR_CODES,
    EVENT_ID_PREFIX,
    REQUIRED_FIELDS,
    SCHEMA_VERSION,
    WIRE_FIELD_NAMES,
    Envelope,
    EnvelopeValidationError,
    EventsError,
    FieldError,
    new_event_id,
    new_id,
    now_rfc3339,
    parse_rfc3339,
)

# --- fixtures / helpers ----------------------------------------------------

# The reference envelope from issue #1, verbatim apart from the elided ids.
REFERENCE_WIRE: dict[str, object] = {
    "id": "evt_01JZ8QK3W6X7Y8Z9A0B1C2D3E4",
    "type": "implementation.completed",
    "source": "agent://builder",
    "time": "2026-07-23T15:00:00Z",
    "schemaVersion": "1",
    "correlationId": "task_42",
    "causationId": "evt_00JZ8QK3W6X7Y8Z9A0B1C2D3E4",
    "runId": "run_01JZ8QK3W6X7Y8Z9A0B1C2D3E4",
    "data": {"repository": "agentculture/example", "commit": "abc123"},
}

CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def valid_envelope(**overrides: object) -> Envelope:
    """A minimal valid envelope, with overrides applied."""
    kwargs: dict[str, object] = {
        "id": "evt_01JZ8QK3W6X7Y8Z9A0B1C2D3E4",
        "type": "task.requested",
        "source": "agent://builder",
        "time": "2026-07-23T15:00:00Z",
        "schema_version": "1",
        "data": {"task": 42},
    }
    kwargs.update(overrides)
    return Envelope(**kwargs)  # type: ignore[arg-type]


def decode_crockford(text: str) -> int:
    """Independent Crockford base32 decoder, so the test does not reuse prod code."""
    value = 0
    for char in text:
        value = value * 32 + CROCKFORD.index(char)
    return value


def core_source_files() -> list[Path]:
    import events_cli.core

    core_dir = Path(events_cli.core.__file__).resolve().parent
    files = sorted(core_dir.glob("*.py"))
    assert files, "no source files found in events_cli/core"
    return files


# --- acceptance criterion 1: pure, stdlib-only, dockerless -----------------


def test_core_imports_only_the_standard_library() -> None:
    """The core must import nothing but stdlib (and its own submodules).

    A third-party import here would drag the whole event contract — and the
    unit suite that gates the SonarCloud quality check — behind an install of
    paho-mqtt, docker or anything else. Static (AST) rather than dynamic, so it
    also catches imports on branches this suite never executes.
    """
    for path in core_source_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                assert node.level == 0, f"{path.name}: use absolute imports"
                names = [node.module or ""]
            else:
                continue
            for name in names:
                root = name.split(".")[0]
                if root == "events_cli":
                    assert name.startswith(
                        "events_cli.core"
                    ), f"{path.name}: core may not import {name} (it is the bottom layer)"
                    continue
                assert (
                    root in sys.stdlib_module_names
                ), f"{path.name}: {name} is not in the standard library"


def code_without_comments_or_strings(path: Path) -> str:
    """The file's executable tokens only — prose in docstrings does not count."""
    skip = {tokenize.COMMENT, tokenize.STRING, tokenize.NL, tokenize.NEWLINE}
    fstring_middle = getattr(tokenize, "FSTRING_MIDDLE", None)
    if fstring_middle is not None:
        skip.add(fstring_middle)
    with path.open("rb") as handle:
        tokens = [tok.string for tok in tokenize.tokenize(handle.readline) if tok.type not in skip]
    return " ".join(tokens).lower()


def test_core_code_names_no_transport_or_container_machinery() -> None:
    """No paho/docker/subprocess/socket in core *code* (docstrings may discuss them)."""
    for path in core_source_files():
        code = code_without_comments_or_strings(path)
        for forbidden in ("paho", "docker", "subprocess", "socket"):
            assert forbidden not in code, f"{path.name} uses {forbidden}"


def test_core_error_is_not_a_cli_error() -> None:
    """Core errors are domain errors; the CLI layer translates them at its edge."""
    from events_cli.cli._errors import CliError

    assert issubclass(EnvelopeValidationError, EventsError)
    assert not issubclass(EnvelopeValidationError, CliError)
    assert not issubclass(EventsError, CliError)


# --- acceptance criterion 2: round-trip ------------------------------------

CONTRACT_FIELDS = (
    "id",
    "type",
    "source",
    "time",
    "schema_version",
    "correlation_id",
    "causation_id",
    "run_id",
)


def random_envelope(rng: random.Random) -> Envelope:
    """Build a valid envelope from a seeded RNG (deterministic, never flaky)."""
    stamp = datetime(2020, 1, 1, tzinfo=timezone.utc) + timedelta(
        seconds=rng.randrange(0, 200_000_000)
    )
    suffix = rng.choice(["Z", "z", "+00:00", "-00:00"])
    fraction = rng.choice(["", ".5", ".123", ".123456"])
    time_text = stamp.strftime("%Y-%m-%dT%H:%M:%S") + fraction + suffix
    optional = [None, "task_42", new_id("run_"), new_event_id()]
    return Envelope(
        id=new_event_id(),
        type=rng.choice(["task.requested", "scope.completed", "heartbeat", "reachy.emotion"]),
        source=rng.choice(["agent://builder", "app://reachy-mini-cli", "urn:agent:daria"]),
        time=time_text,
        schema_version=rng.choice(["1", "2", "1.3"]),
        correlation_id=rng.choice(optional),
        causation_id=rng.choice(optional),
        run_id=rng.choice(optional),
        traceparent=rng.choice([None, "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"]),
        producer=rng.choice([None, "events-cli@spark-f8a9"]),
        delivery_attempt=rng.choice([None, 1, 2, 17]),
        data=rng.choice(
            [
                {},
                {"commit": "abc123"},
                {"nested": {"a": [1, 2.5, None, True, "x"]}},
                {"repository": "agentculture/example", "count": 3},
            ]
        ),
    )


def test_valid_envelopes_round_trip_unchanged() -> None:
    """serialize -> parse preserves every contract field (property-style, 300 cases)."""
    rng = random.Random(20260724)  # nosec B311 - deterministic test data, not crypto
    for _ in range(300):
        env = random_envelope(rng)
        env.validate()  # the generator must only produce valid envelopes
        parsed = Envelope.from_dict(env.to_dict())
        assert parsed == env
        for name in CONTRACT_FIELDS:
            assert getattr(parsed, name) == getattr(env, name), name
        assert parsed.data == env.data
        assert parsed.traceparent == env.traceparent
        assert parsed.producer == env.producer
        assert parsed.delivery_attempt == env.delivery_attempt


def test_valid_envelopes_round_trip_through_json() -> None:
    """The wire form is JSON text; to_json -> from_json is the real round-trip."""
    rng = random.Random(4242)  # nosec B311 - deterministic test data, not crypto
    for _ in range(200):
        env = random_envelope(rng)
        assert Envelope.from_json(env.to_json()) == env
        assert Envelope.from_json(env.to_json().encode("utf-8")) == env


def test_time_string_is_preserved_verbatim_not_normalised() -> None:
    """Round-trip means *unchanged* — parsing must not rewrite +00:00 into Z."""
    for text in ("2026-07-23T15:00:00Z", "2026-07-23T15:00:00+00:00", "2026-07-23T15:00:00.5Z"):
        env = valid_envelope(time=text)
        env.validate()
        assert Envelope.from_dict(env.to_dict()).time == text


def test_to_dict_matches_the_issue_1_reference_example() -> None:
    """The example in issue #1 is the contract; pin it exactly, both directions."""
    env = Envelope(
        id=REFERENCE_WIRE["id"],  # type: ignore[arg-type]
        type=REFERENCE_WIRE["type"],  # type: ignore[arg-type]
        source=REFERENCE_WIRE["source"],  # type: ignore[arg-type]
        time=REFERENCE_WIRE["time"],  # type: ignore[arg-type]
        schema_version=REFERENCE_WIRE["schemaVersion"],  # type: ignore[arg-type]
        correlation_id=REFERENCE_WIRE["correlationId"],  # type: ignore[arg-type]
        causation_id=REFERENCE_WIRE["causationId"],  # type: ignore[arg-type]
        run_id=REFERENCE_WIRE["runId"],  # type: ignore[arg-type]
        data=REFERENCE_WIRE["data"],  # type: ignore[arg-type]
    )
    env.validate()
    assert env.to_dict() == REFERENCE_WIRE
    assert list(env.to_dict()) == list(REFERENCE_WIRE), "wire key order must match the spec"
    assert Envelope.from_dict(REFERENCE_WIRE) == env


def test_absent_optional_fields_are_omitted_never_null() -> None:
    """A consumer must never have to special-case a materialised null."""
    env = valid_envelope()
    wire_form = env.to_dict()
    assert set(wire_form) == {"id", "type", "source", "time", "schemaVersion", "data"}
    assert None not in wire_form.values()
    assert "null" not in env.to_json()
    parsed = Envelope.from_dict(wire_form)
    for name in ("correlation_id", "causation_id", "run_id", "traceparent", "producer"):
        assert getattr(parsed, name) is None
    assert parsed.delivery_attempt is None


def test_empty_data_is_still_emitted() -> None:
    """``data`` is a required wire field, so it is emitted even when empty."""
    env = valid_envelope(data={})
    assert env.to_dict()["data"] == {}
    assert Envelope.from_dict(env.to_dict()) == env


# --- the snake_case <-> camelCase bridge -----------------------------------


def test_wire_field_names_cover_every_python_field_and_are_immutable() -> None:
    """The mapping is the bridge; a new field without a wire name must fail here."""
    python_names = {f.name for f in dataclasses.fields(Envelope)}
    assert set(WIRE_FIELD_NAMES) == python_names
    assert len(set(WIRE_FIELD_NAMES.values())) == len(WIRE_FIELD_NAMES), "duplicate wire names"
    assert isinstance(WIRE_FIELD_NAMES, MappingProxyType)
    with pytest.raises(TypeError):
        WIRE_FIELD_NAMES["nope"] = "nope"  # type: ignore[index]


def test_wire_field_names_are_pinned() -> None:
    """CloudEvents-compatible camelCase on the wire, idiomatic snake_case in Python."""
    assert dict(WIRE_FIELD_NAMES) == {
        "id": "id",
        "type": "type",
        "source": "source",
        "time": "time",
        "schema_version": "schemaVersion",
        "correlation_id": "correlationId",
        "causation_id": "causationId",
        "run_id": "runId",
        # W3C trace context spells its header lowercase; it is deliberately not
        # camelCased into "traceParent".
        "traceparent": "traceparent",
        "producer": "producer",
        "delivery_attempt": "deliveryAttempt",
        "data": "data",
    }


def test_required_fields_are_the_six_from_issue_1() -> None:
    assert REQUIRED_FIELDS == ("id", "type", "source", "time", "schema_version", "data")
    assert SCHEMA_VERSION == "1"


# --- acceptance criterion 2: field-level rejection -------------------------


def wire(**overrides: object) -> dict[str, object]:
    """The reference wire form with overrides; ``...`` removes a key."""
    payload = dict(REFERENCE_WIRE)
    for key, value in overrides.items():
        if value is ...:
            payload.pop(key, None)
        else:
            payload[key] = value
    return payload


def errors_for(payload: object) -> dict[str, str]:
    """Parse ``payload`` and return {field: code} for the reported problems."""
    with pytest.raises(EnvelopeValidationError) as exc:
        Envelope.from_dict(payload)  # type: ignore[arg-type]
    assert exc.value.errors, "an EnvelopeValidationError must carry field errors"
    for err in exc.value.errors:
        assert err.code in ERROR_CODES, f"undeclared error code {err.code!r}"
        assert err.message
    return {err.field: err.code for err in exc.value.errors}


def test_missing_required_fields_are_all_reported_by_wire_name() -> None:
    assert errors_for({}) == {
        "id": "missing",
        "type": "missing",
        "source": "missing",
        "time": "missing",
        "schemaVersion": "missing",
        "data": "missing",
    }


def test_several_broken_fields_are_reported_in_one_pass() -> None:
    """One parse, every reason — an agent should not fix errors one at a time."""
    found = errors_for(
        wire(id="", type="Task.Requested", source="builder", time="yesterday", data=[1, 2])
    )
    assert found == {
        "id": "empty",
        "type": "malformed",
        "source": "malformed",
        "time": "malformed",
        "data": "not_an_object",
    }


def test_missing_and_invalid_fields_are_reported_together() -> None:
    """A missing field must not mask the invalid ones (single-pass validation)."""
    assert errors_for(wire(id=..., type=42)) == {"id": "missing", "type": "not_a_string"}


def test_unknown_wire_field_is_rejected_with_a_did_you_mean_hint() -> None:
    # `wire()` is built outside the block so only `from_dict` can raise inside it.
    payload = wire(schema_version="1")
    with pytest.raises(EnvelopeValidationError) as exc:
        Envelope.from_dict(payload)
    (err,) = [e for e in exc.value.errors if e.field == "schema_version"]
    assert err.code == "unknown_field"
    assert "schemaVersion" in err.message


def test_unknown_wire_field_without_a_near_match_is_still_rejected() -> None:
    assert errors_for(wire(colour="red")) == {"colour": "unknown_field"}


def test_from_dict_rejects_a_non_mapping_payload() -> None:
    for payload in ([], "not a dict", 7, None):
        assert errors_for(payload) == {"envelope": "not_an_object"}


def test_from_json_rejects_malformed_text_as_a_domain_error() -> None:
    """A broken MQTT payload must surface as our error, not json.JSONDecodeError."""
    with pytest.raises(EnvelopeValidationError) as exc:
        Envelope.from_json("{not json")
    assert [e.code for e in exc.value.errors] == ["not_json"]
    assert exc.value.fields == ("envelope",)


@pytest.mark.parametrize(
    "value,code",
    [
        (42, "not_a_string"),
        ("", "empty"),
        ("2026-07-23", "malformed"),
        ("2026-07-23T15:00:00", "malformed"),
        ("2026-07-23 15:00:00Z", "malformed"),
        ("2026-13-01T00:00:00Z", "malformed"),
        ("2026-07-23T25:00:00Z", "malformed"),
        ("2026-02-29T00:00:00Z", "malformed"),
        ("2026-07-23T15:00:00+02:00", "not_utc"),
        ("2026-07-23T15:00:00-05:00", "not_utc"),
    ],
)
def test_invalid_times_are_rejected(value: object, code: str) -> None:
    assert errors_for(wire(time=value)) == {"time": code}


@pytest.mark.parametrize(
    "value",
    [
        "2026-07-23T15:00:00Z",
        "2026-07-23t15:00:00z",
        "2026-07-23T15:00:00.123456Z",
        "2026-07-23T15:00:00+00:00",
        "2026-07-23T15:00:00-00:00",
        "2024-02-29T00:00:00Z",
    ],
)
def test_valid_utc_times_are_accepted(value: str) -> None:
    valid_envelope(time=value).validate()


@pytest.mark.parametrize(
    "value,code",
    [
        (42, "not_a_string"),
        ("", "empty"),
        ("Task.Requested", "malformed"),
        ("task requested", "malformed"),
        ("task..requested", "malformed"),
        (".task", "malformed"),
        ("task.", "malformed"),
        ("task/requested", "malformed"),
        ("x" * 200, "too_long"),
    ],
)
def test_invalid_event_types_are_rejected(value: object, code: str) -> None:
    assert errors_for(wire(type=value)) == {"type": code}


@pytest.mark.parametrize(
    "value", ["task.requested", "heartbeat", "reachy.state.updated", "scope_completed", "v1.a-b"]
)
def test_valid_event_types_are_accepted(value: str) -> None:
    valid_envelope(type=value).validate()


@pytest.mark.parametrize(
    "value,code",
    [
        (42, "not_a_string"),
        ("", "empty"),
        ("builder", "malformed"),
        ("/agents/builder", "malformed"),
        ("agent://build er", "malformed"),
        ("a" * 600 + "://x", "too_long"),
    ],
)
def test_invalid_sources_are_rejected(value: object, code: str) -> None:
    assert errors_for(wire(source=value)) == {"source": code}


@pytest.mark.parametrize(
    "value", ["agent://builder", "app://reachy-mini-cli", "urn:agent:daria", "https://example/x"]
)
def test_valid_sources_are_accepted(value: str) -> None:
    valid_envelope(source=value).validate()


@pytest.mark.parametrize("value,code", [(1, "not_a_string"), ("", "empty"), ("v1", "malformed")])
def test_invalid_schema_versions_are_rejected(value: object, code: str) -> None:
    assert errors_for(wire(schemaVersion=value)) == {"schemaVersion": code}


@pytest.mark.parametrize("value", ["٣", "1.٣", "੩"])
def test_non_ascii_digits_are_not_a_schema_version(value: str) -> None:
    """The version is ASCII digits only — Unicode decimal digits are rejected.

    This pins a deliberate choice against a static-analysis suggestion to write
    the validator as ``\\d``. In Python ``\\d`` matches every Unicode decimal
    digit, so ``\\d`` would accept U+0663 ARABIC-INDIC DIGIT THREE — and
    ``int()`` parses it to 3, so such a value would validate *and* parse here
    while being something no other consumer's JSON parser would accept as a
    number. Wire formats are ASCII; keep this test if the pattern is ever
    rewritten.
    """
    assert int(value.replace(".", "") or "0") >= 0  # the trap: Python does parse these
    assert errors_for(wire(schemaVersion=value)) == {"schemaVersion": "malformed"}


@pytest.mark.parametrize("field_name", ["correlationId", "causationId", "runId", "producer"])
@pytest.mark.parametrize("value,code", [(7, "not_a_string"), ("", "empty"), ("a b", "malformed")])
def test_invalid_optional_identifiers_are_rejected(
    field_name: str, value: object, code: str
) -> None:
    assert errors_for(wire(**{field_name: value})) == {field_name: code}


@pytest.mark.parametrize("field_name", ["correlationId", "causationId", "runId", "producer"])
def test_optional_identifiers_may_be_omitted(field_name: str) -> None:
    Envelope.from_dict(wire(**{field_name: ...})).validate()


@pytest.mark.parametrize(
    "value,code",
    [
        ("00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7", "malformed"),
        ("00-4BF92F3577B34DA6A3CE929D0E0E4736-00f067aa0ba902b7-01", "malformed"),
        ("00-00000000000000000000000000000000-00f067aa0ba902b7-01", "malformed"),
        ("00-4bf92f3577b34da6a3ce929d0e0e4736-0000000000000000-01", "malformed"),
        ("ff-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01", "malformed"),
        (3, "not_a_string"),
    ],
)
def test_invalid_traceparents_are_rejected(value: object, code: str) -> None:
    assert errors_for(wire(traceparent=value)) == {"traceparent": code}


def test_valid_traceparent_is_accepted() -> None:
    valid_envelope(traceparent="00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01").validate()


@pytest.mark.parametrize(
    "value,code",
    [
        (0, "out_of_range"),
        (-1, "out_of_range"),
        ("1", "not_an_integer"),
        (1.0, "not_an_integer"),
        (True, "not_an_integer"),
    ],
)
def test_invalid_delivery_attempts_are_rejected(value: object, code: str) -> None:
    assert errors_for(wire(deliveryAttempt=value)) == {"deliveryAttempt": code}


def test_valid_delivery_attempt_is_accepted() -> None:
    valid_envelope(delivery_attempt=3).validate()


# --- data payload validation ----------------------------------------------


@pytest.mark.parametrize("value", [[1, 2], "text", 7, None, True])
def test_data_must_be_a_json_object(value: object) -> None:
    assert errors_for(wire(data=value)) == {"data": "not_an_object"}


def test_data_values_that_json_cannot_represent_are_rejected_by_path() -> None:
    env = valid_envelope(data={"when": datetime(2026, 7, 23, tzinfo=timezone.utc), "ok": 1})
    with pytest.raises(EnvelopeValidationError) as exc:
        env.validate()
    (err,) = exc.value.errors
    assert err.field == "data.when"
    assert err.code == "unsupported_type"
    assert "datetime" in err.message


def test_nested_and_list_payload_paths_are_reported() -> None:
    env = valid_envelope(data={"outer": {"items": [1, {"bad": {1, 2}}]}})
    with pytest.raises(EnvelopeValidationError) as exc:
        env.validate()
    (err,) = exc.value.errors
    assert err.field == "data.outer.items[1].bad"
    assert err.code == "unsupported_type"


def test_non_string_object_keys_are_rejected() -> None:
    env = valid_envelope(data={1: "one"})
    with pytest.raises(EnvelopeValidationError) as exc:
        env.validate()
    (err,) = exc.value.errors
    assert err.field == "data"
    assert err.code == "unsupported_type"
    assert "key" in err.message


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_numbers_are_rejected(value: float) -> None:
    """json.dumps happily emits NaN/Infinity, which no other JSON parser accepts."""
    assert math.isnan(value) or math.isinf(value)
    env = valid_envelope(data={"ratio": value})
    with pytest.raises(EnvelopeValidationError) as exc:
        env.validate()
    assert [(e.field, e.code) for e in exc.value.errors] == [("data.ratio", "malformed")]


def test_absurdly_nested_payloads_are_rejected() -> None:
    payload: dict[str, object] = {"leaf": 1}
    for _ in range(80):
        payload = {"nest": payload}
    env = valid_envelope(data=payload)
    with pytest.raises(EnvelopeValidationError) as exc:
        env.validate()
    assert exc.value.errors[0].code == "too_deep"


def test_deep_but_reasonable_payloads_are_accepted() -> None:
    payload: dict[str, object] = {"leaf": 1}
    for _ in range(8):
        payload = {"nest": payload}
    valid_envelope(data=payload).validate()


# --- the error objects themselves ------------------------------------------


def test_validation_error_reports_fields_message_and_json_shape() -> None:
    payload = wire(id="", type="")
    with pytest.raises(EnvelopeValidationError) as exc:
        Envelope.from_dict(payload)
    err = exc.value
    assert err.fields == ("id", "type")
    assert "id" in str(err) and "type" in str(err)
    payload = err.to_dict()
    assert payload["error"] == "envelope_validation"
    assert payload["message"]
    assert payload["errors"] == [
        {"field": "id", "code": "empty", "message": err.errors[0].message},
        {"field": "type", "code": "empty", "message": err.errors[1].message},
    ]
    assert json.loads(json.dumps(payload)) == payload


def test_field_error_is_frozen_and_stringifies_as_field_colon_reason() -> None:
    err = FieldError(field="id", code="empty", message="must not be empty")
    assert str(err) == "id: must not be empty"
    assert err.to_dict() == {"field": "id", "code": "empty", "message": "must not be empty"}
    with pytest.raises(dataclasses.FrozenInstanceError):
        err.field = "type"  # type: ignore[misc]


def test_validate_returns_none_and_validation_errors_is_empty_when_valid() -> None:
    env = valid_envelope()
    assert env.validate() is None
    assert env.validation_errors() == ()


def test_error_codes_are_declared_once_and_unique() -> None:
    assert len(set(ERROR_CODES)) == len(ERROR_CODES)
    assert "missing" in ERROR_CODES


# --- acceptance criterion 3: generated ids ---------------------------------


def test_new_event_id_is_prefixed_and_collision_resistant() -> None:
    ids = {new_event_id() for _ in range(20_000)}
    assert len(ids) == 20_000, "generated event ids collided"
    for value in ids:
        assert value.startswith(EVENT_ID_PREFIX)
        suffix = value[len(EVENT_ID_PREFIX) :]
        assert len(suffix) == 26
        assert set(suffix) <= set(CROCKFORD)


def test_event_id_prefix_is_evt() -> None:
    assert EVENT_ID_PREFIX == "evt_"


def test_generated_event_ids_validate_as_envelope_ids() -> None:
    """The generator and the validator must agree, or emit breaks on its own output."""
    valid_envelope(id=new_event_id(), causation_id=new_event_id()).validate()


def test_event_ids_embed_their_generation_time_and_sort_by_it() -> None:
    """Sortable ids make history and cursors cheap; decode the timestamp to prove it."""
    from events_cli.core.envelope import _encode_ulid  # internal: pinned by this test

    before = int(datetime.now(timezone.utc).timestamp() * 1000)
    value = new_event_id()[len(EVENT_ID_PREFIX) :]
    after = int(datetime.now(timezone.utc).timestamp() * 1000)
    assert before - 1000 <= (decode_crockford(value) >> 80) <= after + 1000

    # Lexicographic order follows timestamp order (same entropy, later millisecond).
    assert _encode_ulid(1_000, 1) < _encode_ulid(1_001, 1)
    assert _encode_ulid(1_000, 1) < _encode_ulid(1_000, 2)
    assert len(_encode_ulid(0, 0)) == 26


def test_new_id_applies_any_prefix() -> None:
    """Runs and correlations use the same generator with their own prefix."""
    run = new_id("run_")
    assert run.startswith("run_")
    assert len(run) == len("run_") + 26
    # Two separate calls must not collide. Bound to names rather than written as
    # `new_id("") != new_id("")`: the two expressions are textually identical, so
    # that form reads as a self-comparison to both a human and an analyser, even
    # though the point is that the two *calls* differ.
    first, second = new_id(""), new_id("")
    assert first != second


# --- immutability ----------------------------------------------------------


def test_envelope_is_a_frozen_dataclass() -> None:
    env = valid_envelope()
    assert dataclasses.is_dataclass(env)
    with pytest.raises(dataclasses.FrozenInstanceError):
        env.id = "evt_other"  # type: ignore[misc]
    with pytest.raises(AttributeError):
        env.extra = 1  # type: ignore[attr-defined]


def test_payload_is_defensively_copied_in_both_directions() -> None:
    """Mutating the caller's dict, or the dict to_dict hands back, must not alias."""
    payload = {"commit": "abc123"}
    env = Envelope(
        id="evt_01JZ8QK3W6X7Y8Z9A0B1C2D3E4",
        type="task.requested",
        source="agent://builder",
        time="2026-07-23T15:00:00Z",
        data=payload,
    )
    payload["commit"] = "tampered"
    assert env.data == {"commit": "abc123"}

    handed_back = env.to_dict()["data"]
    handed_back["commit"] = "tampered"  # type: ignore[index]
    assert env.data == {"commit": "abc123"}


def test_envelopes_are_hashable_and_compare_by_value() -> None:
    """Consumers dedupe on `id` (QoS 1 is at-least-once), so envelopes must fit a set."""
    a = valid_envelope()
    b = valid_envelope()
    assert a == b and hash(a) == hash(b)
    assert len({a, b}) == 1
    assert a != valid_envelope(id=new_event_id())

    seen: set[str] = set()
    delivered = [a, b, valid_envelope(id="evt_01JZ8QK3W6X7Y8Z9A0B1C2D3E5")]
    unique = [e for e in delivered if not (e.id in seen or seen.add(e.id))]
    assert len(unique) == 2


def test_replace_produces_a_new_valid_envelope() -> None:
    """`dataclasses.replace` is the supported way to derive a changed copy."""
    env = valid_envelope()
    other = dataclasses.replace(env, delivery_attempt=2)
    other.validate()
    assert other.delivery_attempt == 2
    assert env.delivery_attempt is None


# --- constructors and time helpers -----------------------------------------


def test_new_fills_id_and_time_and_validates() -> None:
    env = Envelope.new("task.requested", "agent://builder", data={"task": 42})
    env.validate()
    assert env.id.startswith(EVENT_ID_PREFIX)
    assert env.schema_version == SCHEMA_VERSION
    assert env.data == {"task": 42}
    assert parse_rfc3339(env.time).tzinfo is not None
    assert Envelope.new("task.requested", "agent://builder").id != env.id
    assert Envelope.new("task.requested", "agent://builder").data == {}


def test_new_rejects_an_invalid_event_immediately() -> None:
    """Generation-time validation stops a bad envelope reaching the transport."""
    with pytest.raises(EnvelopeValidationError) as exc:
        Envelope.new("Task.Requested", "builder")
    assert {e.field for e in exc.value.errors} == {"type", "source"}


def test_new_accepts_explicit_tracing_fields() -> None:
    parent = Envelope.new("task.requested", "agent://builder")
    child = Envelope.new(
        "scope.completed",
        "agent://scoper",
        correlation_id="task_42",
        causation_id=parent.id,
        run_id=new_id("run_"),
        producer="events-cli",
        delivery_attempt=1,
        traceparent="00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
    )
    child.validate()
    assert child.causation_id == parent.id
    assert Envelope.from_dict(child.to_dict()) == child


def test_now_rfc3339_is_utc_and_validates() -> None:
    text = now_rfc3339()
    assert text.endswith("Z")
    valid_envelope(time=text).validate()
    parsed = parse_rfc3339(text)
    assert parsed.utcoffset() == timedelta(0)
    assert abs((datetime.now(timezone.utc) - parsed).total_seconds()) < 60


def test_parse_rfc3339_raises_value_error_on_bad_input() -> None:
    """The public parse helper stays idiomatic: ValueError, not a domain error."""
    with pytest.raises(ValueError):
        parse_rfc3339("yesterday")
    with pytest.raises(ValueError):
        parse_rfc3339("2026-07-23T15:00:00+02:00")
    assert parse_rfc3339("2026-07-23T15:00:00Z") == datetime(
        2026, 7, 23, 15, 0, tzinfo=timezone.utc
    )


# --- edges the happy paths do not reach ------------------------------------


def test_over_long_identifiers_and_versions_are_rejected() -> None:
    """Routing fields carry length limits, so one producer cannot bloat a topic."""
    assert errors_for(wire(correlationId="c" * 200)) == {"correlationId": "too_long"}
    assert errors_for(wire(schemaVersion="1" * 64)) == {"schemaVersion": "too_long"}


def test_deep_nesting_through_lists_is_also_rejected() -> None:
    """The depth guard must not be escapable by alternating objects and arrays."""
    payload: object = "leaf"
    for _ in range(60):
        payload = [payload]
    env = valid_envelope(data={"stack": payload})
    with pytest.raises(EnvelopeValidationError) as exc:
        env.validate()
    assert exc.value.errors[0].code == "too_deep"


def test_non_string_payload_keys_are_reported_as_unknown_fields() -> None:
    """A hand-built dict can carry non-string keys; JSON cannot, so reject them."""
    assert errors_for({1: "one"})[str(1)] == "unknown_field"


def test_a_case_only_misspelling_still_gets_a_suggestion() -> None:
    payload = wire(schemaversion="1")
    with pytest.raises(EnvelopeValidationError) as exc:
        Envelope.from_dict(payload)
    (err,) = [e for e in exc.value.errors if e.field == "schemaversion"]
    assert "schemaVersion" in err.message


def test_parse_rfc3339_rejects_a_non_string() -> None:
    with pytest.raises(ValueError):
        parse_rfc3339(20260723)  # type: ignore[arg-type]

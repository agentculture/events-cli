"""Behaviour tests for ``events emit`` — the CLI translation layer.

**No test here ever constructs a real EventClient.** ``events_cli.client.EventClient``
is always replaced (as imported into ``events_cli.cli._commands.emit``) with a
hand-written fake before ``main()`` runs — this machine runs a **production
broker for a live robot** on ``127.0.0.1:1883``, and a real ``EventClient()``
would connect to exactly that port. Every fake only ever records what it was
called with and returns a canned :class:`~events_cli.client.PublishResult`;
nothing here opens a socket.

The design under test
----------------------
``events emit <type> --data <file|-> [--source ...] [--correlation-id ...]``:

1. ``--data`` supplies only the envelope's ``data`` payload (a file or stdin),
   never a whole envelope — ``id``/``time`` are always generated, and ``type``
   is the positional.
2. The assembled wire dict is validated through
   :meth:`~events_cli.core.envelope.Envelope.from_dict` **before** anything is
   published — an invalid envelope must never reach ``EventClient`` at all,
   which every "poison" fake below proves by raising if it is ever
   constructed.
3. A successful publish always uses ``qos=1`` — never a flag.
4. A failed publish is exit 2 (an environment fault), and the event/topic/
   publish-result payload is still printed — never swallowed by the error
   path.
"""

from __future__ import annotations

import argparse
import io
import json
from pathlib import Path

import pytest

from events_cli.cli import _build_parser, main
from events_cli.cli._commands import emit as emit_module
from events_cli.client import PublishResult
from events_cli.explain import known_paths

# --- fakes: never touch a socket --------------------------------------------


class _FakeEventClient:
    """Records every construction and ``publish_event`` call. Never a socket."""

    instances: list["_FakeEventClient"] = []

    def __init__(self, *args: object, **kwargs: object) -> None:
        self.init_args = args
        self.init_kwargs = kwargs
        self.calls: list[dict[str, object]] = []
        self.closed = False
        self.result = PublishResult(ok=True, connected=True, reason="ok", mid=1)
        _FakeEventClient.instances.append(self)

    def publish_event(self, envelope, topic, *, qos=0, retain=False):
        self.calls.append({"envelope": envelope, "topic": topic, "qos": qos, "retain": retain})
        return self.result

    def close(self) -> None:
        self.closed = True


def _poison_client(*args: object, **kwargs: object):
    raise AssertionError(
        f"EventClient must not be constructed; got args={args!r} kwargs={kwargs!r}"
    )


@pytest.fixture(autouse=True)
def _reset_fake_instances():
    _FakeEventClient.instances = []
    yield
    _FakeEventClient.instances = []


def _choices(parser) -> dict:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action.choices
    return {}


def _subparser(parser, *path):
    node = parser
    for name in path:
        node = _choices(node)[name]
    return node


# --- structure: registration, catalog, --json -------------------------------


def test_emit_is_registered_with_json() -> None:
    parser = _build_parser()
    emit = _subparser(parser, "emit")
    assert "--json" in emit._option_string_actions


def test_emit_catalog_entry_exists() -> None:
    assert ("emit",) in known_paths()


# --- the --data shape: a file, stdin, or absent -----------------------------


def test_emit_reads_data_from_a_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    data_file = tmp_path / "task.json"
    data_file.write_text(json.dumps({"job": "build"}), encoding="utf-8")
    monkeypatch.setattr(emit_module, "EventClient", _FakeEventClient)

    rc = main(["emit", "task.requested", "--data", str(data_file), "--json"])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["event"]["data"] == {"job": "build"}
    assert payload["event"]["type"] == "task.requested"


def test_emit_reads_data_from_stdin(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(emit_module, "EventClient", _FakeEventClient)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"job": "from-stdin"})))

    rc = main(["emit", "task.requested", "--data", "-", "--json"])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["event"]["data"] == {"job": "from-stdin"}


def test_emit_defaults_data_to_empty_object(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(emit_module, "EventClient", _FakeEventClient)

    rc = main(["emit", "heartbeat", "--json"])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["event"]["data"] == {}


def test_emit_data_file_not_found_is_a_user_error_and_never_constructs_a_client(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(emit_module, "EventClient", _poison_client)

    rc = main(["emit", "task.requested", "--data", "/does/not/exist.json"])

    assert rc == 1
    err = capsys.readouterr().err
    assert err.startswith("error:")
    assert "could not be read" in err
    assert "Traceback" not in err


def test_emit_data_malformed_json_is_a_user_error_and_never_constructs_a_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    data_file = tmp_path / "bad.json"
    data_file.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(emit_module, "EventClient", _poison_client)

    rc = main(["emit", "task.requested", "--data", str(data_file)])

    assert rc == 1
    err = capsys.readouterr().err
    assert "not valid JSON" in err
    assert "Traceback" not in err


def test_emit_data_not_utf8_is_a_user_error_and_never_constructs_a_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    data_file = tmp_path / "binary.json"
    data_file.write_bytes(b"\xff\xfe\x00\x01")
    monkeypatch.setattr(emit_module, "EventClient", _poison_client)

    rc = main(["emit", "task.requested", "--data", str(data_file)])

    assert rc == 1
    err = capsys.readouterr().err
    assert "could not be read" in err
    assert "Traceback" not in err


# --- validation happens before any publish -----------------------------------


def test_emit_rejects_an_invalid_type_before_any_publish(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(emit_module, "EventClient", _poison_client)

    rc = main(["emit", "Not A Valid Type"])

    assert rc == 1
    err = capsys.readouterr().err
    assert err.startswith("error:")
    assert "type" in err
    assert "hint:" in err
    assert "Traceback" not in err


def test_emit_rejects_a_non_object_data_payload_before_any_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    data_file = tmp_path / "list.json"
    data_file.write_text("[1, 2, 3]", encoding="utf-8")
    monkeypatch.setattr(emit_module, "EventClient", _poison_client)

    rc = main(["emit", "task.requested", "--data", str(data_file)])

    assert rc == 1
    err = capsys.readouterr().err
    assert "data" in err


def test_emit_reports_every_field_error_in_one_pass(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(emit_module, "EventClient", _poison_client)
    monkeypatch.setattr(emit_module, "_default_source", lambda: "not-a-uri")

    rc = main(["emit", "Bad Type"])

    assert rc == 1
    err = capsys.readouterr().err
    # Both the bad type and the bad source are reported together, not one at a time.
    assert "type" in err
    assert "source" in err


# --- topic derivation, qos=1 always, source/tracing flags -------------------


def test_emit_publishes_to_the_canonical_topic_at_qos_1(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(emit_module, "EventClient", _FakeEventClient)

    rc = main(["emit", "task.requested", "--json"])

    assert rc == 0
    assert len(_FakeEventClient.instances) == 1
    call = _FakeEventClient.instances[0].calls[0]
    assert call["topic"] == "events/task/requested"
    assert call["qos"] == 1


def test_emit_default_source_is_this_agents_nick(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(emit_module, "EventClient", _FakeEventClient)
    monkeypatch.setattr(emit_module, "read_agent_fields", lambda: {"nick": "test-agent"})

    rc = main(["emit", "heartbeat", "--json"])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["event"]["source"] == "agent://test-agent"


def test_emit_source_flag_overrides_the_default(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(emit_module, "EventClient", _FakeEventClient)

    rc = main(["emit", "heartbeat", "--source", "app://reachy-mini-cli", "--json"])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["event"]["source"] == "app://reachy-mini-cli"


def test_emit_tracing_flags_populate_the_envelope(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(emit_module, "EventClient", _FakeEventClient)

    rc = main(
        [
            "emit",
            "task.requested",
            "--correlation-id",
            "run-42",
            "--causation-id",
            "evt_prev",
            "--run-id",
            "run-42",
            "--json",
        ]
    )

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    event = payload["event"]
    assert event["correlationId"] == "run-42"
    assert event["causationId"] == "evt_prev"
    assert event["runId"] == "run-42"


def test_emit_closes_the_client_after_publishing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(emit_module, "EventClient", _FakeEventClient)

    rc = main(["emit", "heartbeat", "--json"])

    assert rc == 0
    assert _FakeEventClient.instances[0].closed is True


# --- a failed publish is an environment fault, and still prints the result --


def test_emit_failed_publish_exits_2_and_still_prints_json(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class _DownClient(_FakeEventClient):
        def __init__(self, *args: object, **kwargs: object) -> None:
            super().__init__(*args, **kwargs)
            self.result = PublishResult(ok=False, connected=False, reason="no_conn", mid=None)

    monkeypatch.setattr(emit_module, "EventClient", _DownClient)

    rc = main(["emit", "task.requested", "--json"])

    assert rc == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["publish"]["ok"] is False
    assert payload["publish"]["reason"] == "no_conn"
    assert payload["event"]["type"] == "task.requested"  # the envelope is still reported


def test_emit_failed_publish_text_mode_shows_reason_and_no_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class _DownClient(_FakeEventClient):
        def __init__(self, *args: object, **kwargs: object) -> None:
            super().__init__(*args, **kwargs)
            self.result = PublishResult(ok=False, connected=False, reason="no_conn", mid=None)

    monkeypatch.setattr(emit_module, "EventClient", _DownClient)

    rc = main(["emit", "task.requested"])

    assert rc == 2
    out = capsys.readouterr().out
    assert "reason:" in out
    assert "no_conn" in out
    assert "Traceback" not in out

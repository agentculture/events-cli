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
        # Connected from the outset, so `emit`'s bounded connection wait returns
        # on its first check and no unit test pays a wall-clock delay. The
        # never-connects case has its own test below, which pins the bound.
        self.connected = True
        self.result = PublishResult(ok=True, connected=True, reason="ok", mid=1)
        _FakeEventClient.instances.append(self)

    @property
    def is_connected(self) -> bool:
        return self.connected

    def publish_event(self, envelope, topic, *, qos=0, retain=False, wait=0.0):
        self.calls.append(
            {"envelope": envelope, "topic": topic, "qos": qos, "retain": retain, "wait": wait}
        )
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


# --- the one-shot publish waits: for the connection, and for the broker -----
#
# `EventClient` connects asynchronously, so a publish issued in the same breath
# as the constructor is always issued while disconnected. Before this was fixed,
# `events emit` reported `connected=false` / `no_conn` and exited 2 against a
# perfectly healthy broker, EVERY time — and the message queued inside paho was
# then discarded by the immediate `close()`. Nothing here could see it, because
# every test above replaces `EventClient` with a fake that is born connected;
# only the docker-backed round-trip in tests/test_subs_integration.py could.
# These three tests are what stop it regressing without a broker in the room.


class _LateClient(_FakeEventClient):
    """Connects only after ``connect_after`` reads of ``is_connected``."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self.connected = False
        self.reads = 0
        self.connect_after = 3
        self.connected_at_publish: bool | None = None

    @property
    def is_connected(self) -> bool:
        self.reads += 1
        if self.reads >= self.connect_after:
            self.connected = True
        return self.connected

    def publish_event(self, envelope, topic, *, qos=0, retain=False, wait=0.0):
        self.connected_at_publish = self.connected
        return super().publish_event(envelope, topic, qos=qos, retain=retain, wait=wait)


def test_emit_waits_for_the_connection_before_publishing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The publish happens only once the client reports connected."""
    monkeypatch.setattr(emit_module, "EventClient", _LateClient)

    rc = main(["emit", "task.requested", "--json"])
    capsys.readouterr()

    assert rc == 0
    client = _FakeEventClient.instances[-1]
    assert client.connected_at_publish is True, (
        "emit published while still disconnected — the message would be queued "
        "inside paho and then dropped by close()"
    )
    assert client.reads >= client.connect_after
    assert client.closed is True


def test_emit_asks_the_broker_to_confirm_the_publish(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A positive ``wait`` is passed, so the process cannot exit before the bytes leave.

    paho's own ``loop_stop`` docstring says stopping the loop does not guarantee
    a queued PUBLISH was sent. A one-shot CLI therefore has to wait for the
    confirmation; the hot lane (``publish``) still defaults to ``wait=0``.
    """
    monkeypatch.setattr(emit_module, "EventClient", _FakeEventClient)

    rc = main(["emit", "task.requested", "--json"])
    capsys.readouterr()

    assert rc == 0
    call = _FakeEventClient.instances[-1].calls[0]
    assert call["qos"] == 1
    assert isinstance(call["wait"], float) and call["wait"] > 0


def test_emit_gives_up_waiting_for_a_connection_that_never_comes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The connection wait is bounded — a dead broker must not hang the caller.

    The bound is shrunk to milliseconds here rather than waited out: what is
    under test is that ``_await_connection`` returns at its deadline and lets the
    publish proceed to a normal exit-2 report, not how long the shipped bound is.
    """

    class _NeverConnects(_FakeEventClient):
        def __init__(self, *args: object, **kwargs: object) -> None:
            super().__init__(*args, **kwargs)
            self.connected = False
            self.result = PublishResult(ok=False, connected=False, reason="no_conn", mid=None)

    monkeypatch.setattr(emit_module, "EventClient", _NeverConnects)
    monkeypatch.setattr(emit_module, "_CONNECT_WAIT", 0.05)

    rc = main(["emit", "task.requested", "--json"])

    assert rc == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["publish"]["ok"] is False
    assert payload["publish"]["reason"] == "no_conn"
    assert _FakeEventClient.instances[-1].closed is True


# --- content marks (#15): flags, --marks, and write-side strictness ---------


def _run_marks(
    argv: list[str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    *,
    poison: bool = False,
):
    monkeypatch.setattr(emit_module, "EventClient", _poison_client if poison else _FakeEventClient)
    rc = main(["emit", "task.requested", *argv])
    captured = capsys.readouterr()
    return rc, captured.out, captured.err


def _marks_file(tmp_path: Path, content: object) -> str:
    path = tmp_path / "marks.json"
    path.write_text(content if isinstance(content, str) else json.dumps(content), encoding="utf-8")
    return str(path)


def _assert_clean_refusal(rc: int, out: str, err: str) -> None:
    assert rc == 1
    assert out == ""
    assert err.startswith("error:")
    assert "hint:" in err
    assert "Traceback" not in err
    assert _FakeEventClient.instances == []


def test_emit_mark_flags_land_on_the_envelope(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    rc, out, _ = _run_marks(
        ["--visibility", "private", "--origin", "ai", "--fidelity", "generated", "--json"],
        monkeypatch,
        capsys,
    )
    assert rc == 0
    event = json.loads(out)["event"]
    assert event["visibility"] == "private"
    assert event["origin"] == "ai"
    assert event["fidelity"] == "generated"
    sent = _FakeEventClient.instances[0].calls[0]["envelope"]
    assert sent.visibility == "private"


def test_emit_unmarked_envelope_carries_no_mark_keys(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    rc, out, _ = _run_marks(["--json"], monkeypatch, capsys)
    assert rc == 0
    event = json.loads(out)["event"]
    for key in ("visibility", "origin", "fidelity", "timestamps"):
        assert key not in event


def test_emit_marks_file_carries_timestamps_and_scalars(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    marks = {
        "visibility": "public",
        "origin": "human",
        "fidelity": "verbatim",
        "timestamps": [
            {"type": "created", "at": "2026-01-01T00:00:00Z"},
            {"type": "witnessed", "at": "2026-01-01T00:00:05Z", "by": "client"},
        ],
    }
    rc, out, _ = _run_marks(
        ["--marks", _marks_file(tmp_path, marks), "--json"], monkeypatch, capsys
    )
    assert rc == 0
    event = json.loads(out)["event"]
    assert event["visibility"] == "public"
    assert event["origin"] == "human"
    assert event["fidelity"] == "verbatim"
    assert event["timestamps"] == marks["timestamps"]


def test_emit_flag_and_marks_may_supply_different_marks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _marks_file(tmp_path, {"origin": "code"})
    rc, out, _ = _run_marks(
        ["--visibility", "private", "--marks", path, "--json"], monkeypatch, capsys
    )
    assert rc == 0
    event = json.loads(out)["event"]
    assert event["visibility"] == "private"
    assert event["origin"] == "code"


@pytest.mark.parametrize("mark", ["visibility", "origin", "fidelity"])
def test_emit_mark_given_by_flag_and_marks_is_refused(
    mark: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    value = {"visibility": "public", "origin": "ai", "fidelity": "verbatim"}[mark]
    path = _marks_file(tmp_path, {mark: value})
    rc, out, err = _run_marks(
        [f"--{mark}", value, "--marks", path], monkeypatch, capsys, poison=True
    )
    _assert_clean_refusal(rc, out, err)
    assert mark in err


def test_emit_marks_file_missing_is_refused(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    rc, out, err = _run_marks(["--marks", "/does/not/exist.json"], monkeypatch, capsys, poison=True)
    _assert_clean_refusal(rc, out, err)
    assert "--marks" in err


def test_emit_marks_file_not_utf8_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "bin.json"
    path.write_bytes(b"\xff\xfe\x00")
    rc, out, err = _run_marks(["--marks", str(path)], monkeypatch, capsys, poison=True)
    _assert_clean_refusal(rc, out, err)


def test_emit_marks_invalid_json_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    rc, out, err = _run_marks(
        ["--marks", _marks_file(tmp_path, "{nope")], monkeypatch, capsys, poison=True
    )
    _assert_clean_refusal(rc, out, err)
    assert "not valid JSON" in err


@pytest.mark.parametrize("content", [["origin", "ai"], "ai", 3, None])
def test_emit_marks_non_object_is_refused(
    content: object,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = _marks_file(tmp_path, json.dumps(content))
    rc, out, err = _run_marks(["--marks", path], monkeypatch, capsys, poison=True)
    _assert_clean_refusal(rc, out, err)
    assert "object" in err


def test_emit_marks_unknown_key_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _marks_file(tmp_path, {"origin": "ai", "mood": "happy"})
    rc, out, err = _run_marks(["--marks", path], monkeypatch, capsys, poison=True)
    _assert_clean_refusal(rc, out, err)
    assert "mood" in err


def test_emit_marks_dash_is_refused_not_read_from_stdin(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"origin": "ai"})))
    rc, out, err = _run_marks(["--marks", "-"], monkeypatch, capsys, poison=True)
    _assert_clean_refusal(rc, out, err)
    assert "stdin" in err


@pytest.mark.parametrize("flag", ["--visibility", "--origin", "--fidelity"])
def test_emit_unknown_flag_word_is_refused(
    flag: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    rc, out, err = _run_marks([flag, "mystery"], monkeypatch, capsys, poison=True)
    _assert_clean_refusal(rc, out, err)
    assert "mystery" in err


@pytest.mark.parametrize("mark", ["visibility", "origin", "fidelity"])
def test_emit_unknown_marks_file_word_is_refused(
    mark: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = _marks_file(tmp_path, {mark: "mystery"})
    rc, out, err = _run_marks(["--marks", path], monkeypatch, capsys, poison=True)
    _assert_clean_refusal(rc, out, err)
    assert "mystery" in err


def test_emit_unknown_timestamp_type_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _marks_file(
        tmp_path, {"timestamps": [{"type": "sighted", "at": "2026-01-01T00:00:00Z"}]}
    )
    rc, out, err = _run_marks(["--marks", path], monkeypatch, capsys, poison=True)
    _assert_clean_refusal(rc, out, err)
    assert "sighted" in err


def test_emit_published_timestamp_is_refused_and_points_at_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _marks_file(
        tmp_path, {"timestamps": [{"type": "published", "at": "2026-01-01T00:00:00Z"}]}
    )
    rc, out, err = _run_marks(["--marks", path], monkeypatch, capsys, poison=True)
    _assert_clean_refusal(rc, out, err)
    assert "time" in err

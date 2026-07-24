"""Behaviour tests for ``events get`` — the CLI translation layer.

``EVENTS_HISTORY_DIR`` is pinned to a fresh ``tmp_path`` for every test in this
module (the ``isolated_history`` fixture, autouse), so nothing here can ever
read the real per-host history store — the same isolation
``tests/test_cli_watch.py`` uses. ``get`` never opens a broker connection, so
unlike the ``sub``/``watch`` test files there is no client or drain to fake.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from events_cli.cli import _build_parser, main
from events_cli.cli._commands import get as get_module
from events_cli.core import Envelope
from events_cli.explain import known_paths
from events_cli.history import HISTORY_DIR_ENV, HistoryCorruptError, HistoryStore


@pytest.fixture(autouse=True)
def isolated_history(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    root = tmp_path / "history"
    monkeypatch.setenv(HISTORY_DIR_ENV, str(root))
    return root


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


def test_get_is_registered_with_json() -> None:
    parser = _build_parser()
    get = _subparser(parser, "get")
    assert "--json" in get._option_string_actions


def test_get_catalog_entry_exists() -> None:
    assert ("get",) in known_paths()


# --- found / not found -------------------------------------------------------


def test_get_returns_the_captured_event(
    isolated_history: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store = HistoryStore(isolated_history)
    envelope = Envelope.new("task.requested", "agent://builder", data={"job": "build"})
    store.append(envelope, "robot")

    rc = main(["get", envelope.id, "--json"])

    assert rc == 0
    import json

    payload = json.loads(capsys.readouterr().out)
    assert payload["event"]["id"] == envelope.id
    assert payload["subscription"] == "robot"
    assert payload["seq"] == 1


def test_get_text_mode_shows_the_key_fields(
    isolated_history: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store = HistoryStore(isolated_history)
    envelope = Envelope.new("task.requested", "agent://builder", data={})
    store.append(envelope, "robot")

    rc = main(["get", envelope.id])

    assert rc == 0
    out = capsys.readouterr().out
    assert envelope.id in out
    assert "task.requested" in out
    assert "robot" in out


def test_get_unknown_id_is_exit_1_with_a_hint_naming_list(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = main(["get", "evt_does_not_exist"])

    assert rc == 1
    err = capsys.readouterr().err
    assert err.startswith("error:")
    assert "hint:" in err
    assert "list" in err
    assert "Traceback" not in err


def test_get_unknown_id_json_mode_is_still_a_structured_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = main(["get", "evt_does_not_exist", "--json"])

    assert rc == 1
    import json

    payload = json.loads(capsys.readouterr().err)
    assert payload["code"] == 1
    assert "remediation" in payload


# --- a damaged store is an environment fault ---------------------------------


def test_get_damaged_store_is_exit_2(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class _BrokenStore:
        def get(self, id):
            raise HistoryCorruptError("the log is damaged", remediation="inspect the store")

    monkeypatch.setattr(get_module, "open_store", lambda: _BrokenStore())

    rc = main(["get", "evt_whatever"])

    assert rc == 2
    err = capsys.readouterr().err
    assert err.startswith("error:")
    assert "damaged" in err
    assert "Traceback" not in err

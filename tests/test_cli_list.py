"""Behaviour tests for ``events list`` — the CLI translation layer.

``EVENTS_HISTORY_DIR`` is pinned to a fresh ``tmp_path`` for every test in this
module (the ``isolated_history`` fixture, autouse), matching
``tests/test_cli_watch.py`` and ``tests/test_cli_get.py``. ``list`` never opens
a broker connection.

This file also carries the proof that ``events get`` / ``events list`` need no
MQTT client installed at all: :func:`test_get_and_list_run_with_paho_absent`
spawns a subprocess with ``paho`` blocked from importing (mirroring
``tests/test_client.py``'s ``_ABSENT_SNIPPET``) and runs both verbs in it.
Neither module imports :mod:`events_cli.client`, so both must succeed.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from events_cli.cli import _build_parser, main
from events_cli.cli._commands import list as list_module
from events_cli.core import Envelope
from events_cli.explain import known_paths
from events_cli.history import DEFAULT_MAX, HISTORY_DIR_ENV, HistoryCorruptError, HistoryStore

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


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


def _seed(root: Path, sub: str, *, event_type: str = "task.requested", count: int = 1) -> None:
    store = HistoryStore(root)
    for i in range(count):
        store.append(Envelope.new(event_type, "agent://builder", data={"n": i}), sub)


# --- structure: registration, catalog, --json, defaults ---------------------


def test_list_is_registered_with_json() -> None:
    parser = _build_parser()
    lst = _subparser(parser, "list")
    assert "--json" in lst._option_string_actions
    assert "--type" in lst._option_string_actions
    assert "--max" in lst._option_string_actions


def test_list_catalog_entry_exists() -> None:
    assert ("list",) in known_paths()


def test_list_default_max_matches_the_store_default() -> None:
    parser = _build_parser()
    args = parser.parse_args(["list"])
    assert args.max == DEFAULT_MAX


# --- behaviour ----------------------------------------------------------------


def test_list_is_empty_on_a_fresh_store(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["list", "--json"])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["events"] == []


def test_list_text_mode_on_a_fresh_store_says_so(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["list"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "no captured events" in out


def test_list_returns_captured_events(
    isolated_history: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed(isolated_history, "robot", count=3)

    rc = main(["list", "--json"])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert len(payload["events"]) == 3


def test_list_filters_by_type(isolated_history: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _seed(isolated_history, "robot", event_type="task.requested", count=2)
    _seed(isolated_history, "robot", event_type="task.completed", count=1)

    rc = main(["list", "--type", "task.completed", "--json"])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert len(payload["events"]) == 1
    assert payload["events"][0]["event"]["type"] == "task.completed"


def test_list_max_bounds_the_result(
    isolated_history: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed(isolated_history, "robot", count=5)

    rc = main(["list", "--max", "2", "--json"])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert len(payload["events"]) == 2


def test_list_rejects_a_non_positive_max_before_reading_the_store(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = main(["list", "--max", "0"])

    assert rc == 1
    err = capsys.readouterr().err
    assert err.startswith("error:")
    assert "--max" in err
    assert "Traceback" not in err


def test_list_damaged_store_is_exit_2(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class _BrokenStore:
        def list(self, type, max):
            raise HistoryCorruptError("the log is damaged", remediation="inspect the store")

    monkeypatch.setattr(list_module, "open_store", lambda: _BrokenStore())

    rc = main(["list"])

    assert rc == 2
    err = capsys.readouterr().err
    assert err.startswith("error:")
    assert "damaged" in err
    assert "Traceback" not in err


# --- get / list need no paho -------------------------------------------------

_ABSENT_SNIPPET = """
import sys
for name in ("paho", "paho.mqtt", "paho.mqtt.client"):
    sys.modules[name] = None
try:
    import paho  # noqa: F401
except ImportError:
    pass
else:
    raise SystemExit("paho importable despite the block")
import events_cli
from events_cli.cli import main
assert main(["list", "--json"]) == 0
assert main(["get", "evt_does_not_exist", "--json"]) == 1
print("GET_LIST_NO_PAHO_OK")
"""


def test_get_and_list_run_with_paho_absent(tmp_path: Path) -> None:
    """``events get``/``events list`` must run on a machine with no MQTT client.

    Both only read the history store; neither module imports
    ``events_cli.client``. Proven the same way
    ``tests/test_client.py::test_introspection_verbs_run_with_paho_absent``
    proves it for the introspection verbs: block every ``paho`` module from
    importing, then run the verb in a fresh subprocess.
    """
    env = dict(os.environ)
    env["PYTHONPATH"] = _REPO_ROOT + os.pathsep + env.get("PYTHONPATH", "")
    env[HISTORY_DIR_ENV] = str(tmp_path / "history")
    proc = subprocess.run(
        [sys.executable, "-c", _ABSENT_SNIPPET],
        capture_output=True,
        text=True,
        timeout=90,
        env=env,
    )
    assert proc.returncode == 0, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    assert "GET_LIST_NO_PAHO_OK" in proc.stdout

"""Smoke tests for the events-cli CLI entry point and its verbs."""

from __future__ import annotations

import json

import pytest

from events import __version__
from events.cli import main
from events.explain import known_paths


def test_version_flag(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_no_args_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main([])
    assert rc == 0
    assert "usage: events [-h]" in capsys.readouterr().out


def test_unknown_command_errors(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["bogus"])
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert err.startswith("error:")
    assert "hint:" in err


# --- whoami ---------------------------------------------------------------


def test_whoami_text(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["whoami"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "nick: events-cli" in out
    assert "backend: colleague" in out
    assert "model:" in out


def test_whoami_json(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["whoami", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["nick"] == "events-cli"
    assert payload["version"] == __version__
    assert payload["backend"] == "colleague"


# --- learn ----------------------------------------------------------------


def test_learn_text(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["learn"])
    assert rc == 0
    out = capsys.readouterr().out
    assert len(out) >= 200
    assert "events whoami" in out
    assert "Exit-code policy" in out
    assert "--json" in out
    assert "explain" in out


def test_learn_json(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["learn", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["tool"] == "events"
    assert payload["version"] == __version__
    assert payload["json_support"] is True


# --- explain --------------------------------------------------------------


def test_explain_root(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["explain"])
    assert rc == 0
    assert capsys.readouterr().out.startswith("# events\n")


def test_explain_self(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["explain", "events-cli"])
    assert rc == 0
    assert capsys.readouterr().out.startswith("#")


def test_explain_json(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["explain", "whoami", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["path"] == ["whoami"]
    assert "events whoami" in payload["markdown"]


def test_explain_unknown_path_errors(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["explain", "nonexistent"])
    assert rc == 1
    captured = capsys.readouterr()
    assert captured.err.startswith("error:")
    assert "hint:" in captured.err


def test_every_catalog_path_resolves(capsys: pytest.CaptureFixture[str]) -> None:
    for path in known_paths():
        rc = main(["explain", *path])
        assert rc == 0, f"explain {' '.join(path)} failed"
        capsys.readouterr()


def test_explain_root_keys_both_resolve_to_same_entry(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`explain events` and `explain events-cli` must both reach the root entry.

    The agent-first rubric's ``explain_self`` check invokes ``events explain
    events``; the dist-name spelling stays valid for callers that know the repo
    by ``events-cli``. Dropping either key breaks a caller, so pin both.
    """
    assert main(["explain", "events"]) == 0
    by_command = capsys.readouterr().out
    assert main(["explain", "events-cli"]) == 0
    by_distribution = capsys.readouterr().out
    assert by_command == by_distribution
    assert by_command.startswith("# events\n")


# --- command-name contract -------------------------------------------------


def test_prog_matches_installed_console_script() -> None:
    """argparse's ``prog`` must be the command a user actually types.

    Regression guard: the scaffold shipped ``prog="events-cli"`` while
    ``[project.scripts]`` installed ``events``, so ``--help`` and every argparse
    error named a command that does not exist. Read the console-script name from
    ``pyproject.toml`` rather than hard-coding it, so a future rename of either
    side fails here instead of shipping.
    """
    import tomllib
    from pathlib import Path

    from events.cli import _build_parser

    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    if not pyproject.is_file():  # pragma: no cover - wheel install, no source tree
        pytest.skip("no pyproject.toml alongside the package")

    scripts = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["scripts"]
    assert list(scripts) == ["events"], "expected exactly one console script named 'events'"
    assert _build_parser().prog == "events"

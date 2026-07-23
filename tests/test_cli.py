"""Smoke tests for the events-cli CLI entry point and its verbs."""

from __future__ import annotations

import json
import sys

import pytest

from events_cli import __version__
from events_cli.cli import main
from events_cli.explain import known_paths


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


def _package_main() -> str:
    """Path `python -m events_cli` puts in argv[0] — this package's __main__.py.

    Not any __main__.py: `python -m pytest` has one too, which is exactly the
    false positive the resolver must not trip on.
    """
    from pathlib import Path

    import events_cli

    return str(Path(events_cli.__file__).resolve().parent / "__main__.py")


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

    from events_cli.cli import _build_parser

    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    if not pyproject.is_file():  # pragma: no cover - wheel install, no source tree
        pytest.skip("no pyproject.toml alongside the package")

    scripts = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["scripts"]
    assert list(scripts) == ["events"], "expected exactly one console script named 'events'"
    assert _build_parser().prog == "events"


def test_prog_names_module_invocation_when_run_as_module(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Under ``python -m events_cli`` the hints must name *that*, not the script.

    The documented no-install fallback runs from a checkout, where the ``events``
    console script is typically absent — so a hint saying ``run 'events --help'``
    would name a command the caller cannot run.
    """
    import events_cli
    from events_cli.cli import _build_parser
    from events_cli.cli._prog import prog_name

    monkeypatch.setattr(sys, "argv", [_package_main(), "bogus"])
    assert prog_name() == "python -m events_cli"
    assert _build_parser().prog == "python -m events_cli"
    assert events_cli  # the package under test is importable by that path

    # A *different* module's __main__.py must not trip it — `python -m pytest`
    # is itself one, so a basename check would report module mode for every
    # test run (and for any host process that happens to use -m).
    monkeypatch.setattr(sys, "argv", ["/usr/lib/python3/pytest/__main__.py"])
    assert prog_name() == "events"


def test_remediation_hints_name_the_active_invocation(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Both hint sources track the invocation mode, not a hard-coded name."""
    monkeypatch.setattr(sys, "argv", [_package_main()])

    # argparse parse error (routed through _CliArgumentParser.error).
    with pytest.raises(SystemExit):
        main(["cli", "overview", "--bogus"])
    assert "hint: run 'python -m events_cli --help'" in capsys.readouterr().err

    # CliError raised from the explain catalog.
    assert main(["explain", "nonexistent"]) == 1
    assert "hint: list entries with: python -m events_cli explain" in capsys.readouterr().err


# --- packaging contract ----------------------------------------------------
#
# Three names, deliberately different, each pinned below:
#   distribution  events-cli   (PyPI — `pip install events-cli`)
#   command       events       ([project.scripts] — what a user types)
#   import        events_cli   (the top-level module)
# The import name is NOT `events`: PyPI distribution `Events` 0.5 already owns
# that top-level module, so shipping it too would silently clobber one of the
# two in any environment that holds both.


def _pyproject_data() -> dict:
    import tomllib
    from pathlib import Path

    path = Path(__file__).resolve().parent.parent / "pyproject.toml"
    if not path.is_file():  # pragma: no cover - wheel install, no source tree
        pytest.skip("no pyproject.toml alongside the package")
    return tomllib.loads(path.read_text(encoding="utf-8"))


def test_import_package_is_events_cli() -> None:
    """The importable top-level module is ``events_cli``."""
    import events_cli

    assert events_cli.__name__ == "events_cli"
    assert events_cli.__version__ == __version__


def test_packaging_config_points_at_events_cli() -> None:
    """Every packaging knob names ``events_cli``; the console script stays ``events``."""
    data = _pyproject_data()
    assert data["project"]["name"] == "events-cli"
    assert data["project"]["scripts"] == {"events": "events_cli.cli:main"}
    assert data["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == ["events_cli"]
    assert data["tool"]["coverage"]["run"]["source"] == ["events_cli"]
    assert data["tool"]["isort"]["known_first_party"] == ["events_cli"]


def test_no_top_level_events_package_in_the_source_tree() -> None:
    """Nothing may reintroduce a top-level ``events`` package beside the source.

    The wheel ships exactly the packages named in
    ``[tool.hatch.build.targets.wheel]``, so the collision can only come back by
    someone re-adding the directory.
    """
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    if not (root / "pyproject.toml").is_file():  # pragma: no cover - wheel install
        pytest.skip("no source tree")
    assert not (root / "events").exists(), "top-level `events` package collides with PyPI Events"

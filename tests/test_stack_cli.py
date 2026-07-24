"""Behaviour tests for the stack verbs.

**Nothing here runs docker.** Every invocation goes through
``events_cli.stack._docker.run``, and these tests replace that one function with
a recorder. That is deliberate and load-bearing: CI collects coverage on a
runner with no daemon, and a suite that needed a live broker to reach the
quality gate would be flaky exactly where it matters most.

What is asserted, then, is the argv we construct and the decisions we make from
docker's output — not that docker does what docker does. Live-broker behaviour
is a separate, marked suite.
"""

from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path
from typing import Sequence

import pytest

from events_cli.cli import main
from events_cli.stack import COMPOSE_FILENAME, MOSQUITTO_CONF_FILENAME, MOSQUITTO_IMAGE
from events_cli.stack._docker import CommandResult


class FakeDocker:
    """Records argv lists and replays canned results, keyed by subcommand."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.kwargs: list[dict] = []
        self._responses: dict[str, CommandResult] = {}

    def respond(
        self, marker: str, *, returncode: int = 0, stdout: str = "", stderr: str = ""
    ) -> None:
        self._responses[marker] = CommandResult((), returncode, stdout, stderr)

    def __call__(self, argv: Sequence[str], **kwargs) -> CommandResult:
        argv = list(argv)
        self.calls.append(argv)
        self.kwargs.append(kwargs)
        # Longest marker first, so a specific one ("--filter", the preflight
        # lookup) beats a generic one ("ps", which both calls contain).
        for marker in sorted(self._responses, key=len, reverse=True):
            if marker in argv:
                canned = self._responses[marker]
                return CommandResult(tuple(argv), canned.returncode, canned.stdout, canned.stderr)
        return CommandResult(tuple(argv), 0, "", "")

    def argv_for(self, marker: str) -> list[str]:
        for argv in self.calls:
            if marker in argv:
                return argv
        raise AssertionError(f"no docker call containing {marker!r}; got {self.calls}")


def _ps_row(**overrides) -> str:
    row = {
        "Name": "events-mosquitto",
        "Service": "broker",
        "Image": MOSQUITTO_IMAGE,
        "State": "running",
        "Health": "healthy",
        "Status": "Up 2 minutes (healthy)",
        "ExitCode": 0,
        "Publishers": [
            {"URL": "127.0.0.1", "TargetPort": 1883, "PublishedPort": 1883, "Protocol": "tcp"}
        ],
    }
    row.update(overrides)
    return json.dumps(row)


@pytest.fixture
def docker(monkeypatch: pytest.MonkeyPatch) -> FakeDocker:
    """Replace the single docker seam, and pretend the binary exists."""
    fake = FakeDocker()
    fake.respond("ps", stdout=_ps_row())
    monkeypatch.setattr("events_cli.stack._docker.run", fake)
    monkeypatch.setattr("events_cli.stack._docker.shutil.which", lambda _: "/usr/bin/docker")
    # Default: nothing is listening, so preflight passes.
    monkeypatch.setattr("events_cli.stack._preflight.port_in_use", lambda *_: False)
    return fake


@pytest.fixture
def stack(tmp_path: Path) -> Path:
    """An initialised stack directory."""
    directory = tmp_path / "stack"
    assert main(["init", "--dir", str(directory)]) == 0
    return directory


def _hold_port(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("events_cli.stack._preflight.port_in_use", lambda *_: True)


# --- init ------------------------------------------------------------------


def test_init_writes_both_templates(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    directory = tmp_path / "stack"
    assert main(["init", "--dir", str(directory)]) == 0
    assert (directory / COMPOSE_FILENAME).is_file()
    assert (directory / MOSQUITTO_CONF_FILENAME).is_file()
    assert capsys.readouterr().err == ""


def test_init_writes_a_loopback_only_compose(tmp_path: Path) -> None:
    """End-to-end of the headline criterion: what lands on disk is loopback-bound."""
    directory = tmp_path / "stack"
    assert main(["init", "--dir", str(directory)]) == 0
    written = (directory / COMPOSE_FILENAME).read_text(encoding="utf-8")
    assert "127.0.0.1:1883:1883" in written
    assert MOSQUITTO_IMAGE in written
    # 0.0.0.0 may appear in a comment explaining the hazard, never in config.
    config = [line for line in written.splitlines() if not line.strip().startswith("#")]
    assert not any("0.0.0.0" in line for line in config)

    conf = (directory / MOSQUITTO_CONF_FILENAME).read_text(encoding="utf-8")
    assert "persistence true" in conf


def test_init_json_payload_shape(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    directory = tmp_path / "stack"
    assert main(["init", "--dir", str(directory), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["stack_dir"] == str(directory)
    assert len(payload["written"]) == 2
    assert payload["image"] == MOSQUITTO_IMAGE
    assert payload["broker"]["published"] == "127.0.0.1:1883:1883"
    assert payload["broker"]["loopback_only"] is True
    assert payload["broker"]["websockets"] is False


def test_init_refuses_to_overwrite_without_force(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A stack directory can hold a deliberate divergence; do not silently eat it."""
    directory = tmp_path / "stack"
    assert main(["init", "--dir", str(directory)]) == 0
    (directory / COMPOSE_FILENAME).write_text("# operator edit\n", encoding="utf-8")
    capsys.readouterr()

    assert main(["init", "--dir", str(directory)]) == 1
    captured = capsys.readouterr()
    assert captured.out == "", "errors must not reach stdout"
    assert captured.err.startswith("error:")
    assert "--force" in captured.err
    assert (directory / COMPOSE_FILENAME).read_text(encoding="utf-8") == "# operator edit\n"


def test_init_force_overwrites(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    directory = tmp_path / "stack"
    assert main(["init", "--dir", str(directory)]) == 0
    (directory / COMPOSE_FILENAME).write_text("# operator edit\n", encoding="utf-8")
    capsys.readouterr()

    assert main(["init", "--dir", str(directory), "--force"]) == 0
    assert "127.0.0.1:1883:1883" in (directory / COMPOSE_FILENAME).read_text(encoding="utf-8")


def test_init_honours_the_stack_dir_environment_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "from-env"
    monkeypatch.setenv("EVENTS_STACK_DIR", str(directory))
    assert main(["init"]) == 0
    assert (directory / COMPOSE_FILENAME).is_file()


# --- up: the preflight refusal ---------------------------------------------

# The foreign row keeps the floating `:2` deliberately — that is the tag the
# real nova-mosquitto on this box runs, and the point is that it is not ours.
_FOREIGN_PS = "bdc998b5f4e4|||nova-mosquitto|||eclipse-mosquitto:2|||0.0.0.0:1883->1883/tcp|||"
_OURS_PS = (
    f"aaaa11112222|||events-mosquitto|||{MOSQUITTO_IMAGE}"
    "|||127.0.0.1:1883->1883/tcp|||events-cli"
)


def test_up_refuses_when_a_foreign_broker_holds_the_port(
    stack: Path,
    docker: FakeDocker,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Exit 2, and the hint is the exact command that frees the port.

    Modelled on the real machine this ships to, which runs a `nova-mosquitto`
    container published on 0.0.0.0:1883 — the LAN-exposed anti-pattern.
    """
    _hold_port(monkeypatch)
    docker.respond("--filter", stdout=_FOREIGN_PS + "\n")
    capsys.readouterr()

    assert main(["up", "--dir", str(stack)]) == 2
    captured = capsys.readouterr()
    assert captured.out == "", "a refusal must not write to stdout"
    assert captured.err.startswith("error:")
    assert "nova-mosquitto" in captured.err
    assert "docker stop nova-mosquitto" in captured.err
    assert "reachable from the LAN" in captured.err
    assert "Traceback" not in captured.err

    assert not any("up" in argv for argv in docker.calls), "refused, so nothing was started"


def test_up_refusal_is_structured_in_json_mode(
    stack: Path,
    docker: FakeDocker,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _hold_port(monkeypatch)
    docker.respond("--filter", stdout=_FOREIGN_PS + "\n")
    capsys.readouterr()

    assert main(["up", "--dir", str(stack), "--json"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    payload = json.loads(captured.err)
    assert payload["code"] == 2
    assert "docker stop nova-mosquitto" in payload["remediation"]


def test_up_refusal_names_a_discovery_command_when_docker_cannot_identify_the_owner(
    stack: Path,
    docker: FakeDocker,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A host process holds 1883: still refuse, and say how to find it."""
    _hold_port(monkeypatch)
    docker.respond("--filter", stdout="")
    capsys.readouterr()

    assert main(["up", "--dir", str(stack)]) == 2
    err = capsys.readouterr().err
    assert "sport = :1883" in err


def test_up_proceeds_when_our_own_broker_already_holds_the_port(
    stack: Path,
    docker: FakeDocker,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`up` is idempotent against its own stack — that is not a foreign broker."""
    _hold_port(monkeypatch)
    docker.respond("--filter", stdout=_OURS_PS + "\n")
    capsys.readouterr()

    assert main(["up", "--dir", str(stack)]) == 0
    assert docker.argv_for("up")


# --- up: the argv we hand to docker ----------------------------------------


def test_up_drives_compose_with_a_fixed_argv_list(
    stack: Path, docker: FakeDocker, capsys: pytest.CaptureFixture[str]
) -> None:
    capsys.readouterr()
    assert main(["up", "--dir", str(stack)]) == 0

    argv = docker.argv_for("up")
    assert argv == [
        "docker",
        "compose",
        "--project-name",
        "events-cli",
        "--file",
        str(stack / COMPOSE_FILENAME),
        "up",
        "--detach",
        "--wait",
        "--wait-timeout",
        "60",
    ]
    # Every element is a separate argument; nothing was string-joined into a
    # shell word.
    assert all(isinstance(part, str) and " " not in part for part in argv)


def test_every_docker_invocation_is_bounded_by_a_timeout(
    stack: Path, docker: FakeDocker, capsys: pytest.CaptureFixture[str]
) -> None:
    """No agent-facing verb may block forever waiting on docker."""
    capsys.readouterr()
    main(["up", "--dir", str(stack)])
    main(["status", "--dir", str(stack)])
    main(["logs", "--dir", str(stack)])
    main(["down", "--dir", str(stack)])
    assert docker.kwargs, "no docker calls recorded"
    for kwargs in docker.kwargs:
        assert isinstance(kwargs.get("timeout"), int)
        assert 0 < kwargs["timeout"] <= 300


def test_up_timeout_flag_reaches_compose(
    stack: Path, docker: FakeDocker, capsys: pytest.CaptureFixture[str]
) -> None:
    capsys.readouterr()
    assert main(["up", "--dir", str(stack), "--timeout", "5"]) == 0
    argv = docker.argv_for("up")
    assert argv[argv.index("--wait-timeout") + 1] == "5"


def test_up_reports_a_compose_failure_as_an_environment_error(
    stack: Path, docker: FakeDocker, capsys: pytest.CaptureFixture[str]
) -> None:
    docker.respond("up", returncode=1, stderr="Error response from daemon: port is allocated")
    capsys.readouterr()

    assert main(["up", "--dir", str(stack)]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "port is allocated" in captured.err
    assert "hint:" in captured.err


# --- status: truthful health ------------------------------------------------


def test_status_reports_healthy(
    stack: Path, docker: FakeDocker, capsys: pytest.CaptureFixture[str]
) -> None:
    capsys.readouterr()
    assert main(["status", "--dir", str(stack), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["running"] is True
    assert payload["healthy"] is True
    assert payload["loopback_only"] is True


def test_status_does_not_call_a_starting_container_healthy(
    stack: Path, docker: FakeDocker, capsys: pytest.CaptureFixture[str]
) -> None:
    docker.respond("ps", stdout=_ps_row(Health="starting", Status="Up 2s (health: starting)"))
    capsys.readouterr()

    assert main(["status", "--dir", str(stack), "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["running"] is True
    assert payload["healthy"] is False
    assert "not healthy" in payload["summary"]


def test_status_does_not_infer_health_from_running(
    stack: Path, docker: FakeDocker, capsys: pytest.CaptureFixture[str]
) -> None:
    """No healthcheck means unknown, and unknown is not a pass."""
    docker.respond("ps", stdout=_ps_row(Health=""))
    capsys.readouterr()

    assert main(["status", "--dir", str(stack), "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["running"] is True
    assert payload["healthy"] is False
    assert payload["services"][0]["health"] == "none"


def test_status_reports_a_stopped_container(
    stack: Path, docker: FakeDocker, capsys: pytest.CaptureFixture[str]
) -> None:
    docker.respond("ps", stdout=_ps_row(State="exited", Health="", ExitCode=137))
    capsys.readouterr()

    assert main(["status", "--dir", str(stack), "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["running"] is False
    assert "137" in payload["summary"]


def test_status_reports_no_containers(
    stack: Path, docker: FakeDocker, capsys: pytest.CaptureFixture[str]
) -> None:
    docker.respond("ps", stdout="")
    capsys.readouterr()

    assert main(["status", "--dir", str(stack), "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["services"] == []
    assert payload["running"] is False


def test_status_flags_a_broker_published_off_loopback(
    stack: Path, docker: FakeDocker, capsys: pytest.CaptureFixture[str]
) -> None:
    """Someone edited compose onto 0.0.0.0 — report what is bound, not the template."""
    docker.respond(
        "ps",
        stdout=_ps_row(
            Publishers=[
                {"URL": "0.0.0.0", "TargetPort": 1883, "PublishedPort": 1883, "Protocol": "tcp"}
            ]
        ),
    )
    capsys.readouterr()

    assert main(["status", "--dir", str(stack), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["loopback_only"] is False
    assert payload["services"][0]["lan_exposed"] is True


def test_status_accepts_ndjson_and_array_ps_output(
    stack: Path, docker: FakeDocker, capsys: pytest.CaptureFixture[str]
) -> None:
    """Compose has shipped both shapes across its v2 series."""
    docker.respond("ps", stdout=f"[{_ps_row()}]")
    capsys.readouterr()
    assert main(["status", "--dir", str(stack), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["healthy"] is True

    docker.respond("ps", stdout=f"{_ps_row()}\n")
    assert main(["status", "--dir", str(stack), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["healthy"] is True


# --- the real subprocess seam ----------------------------------------------


def test_undecodable_output_does_not_raise_out_of_the_docker_seam() -> None:
    """Docker output that is not valid UTF-8 must not blow up the seam.

    Bare ``text=True`` decodes strictly with the locale's encoding, so one bad
    byte would raise ``UnicodeDecodeError`` from inside ``run()`` — escaping the
    StackError translation and reaching the user as an "unexpected" error with
    the wrong exit code. ``events logs`` pipes the broker's container log
    through this path, so the bytes really are arbitrary.

    Runs a tiny Python program instead of docker: the seam's argv is not
    docker-specific, and this keeps the test in the dockerless default suite.
    """
    from events_cli.stack._docker import run

    result = run(
        [
            sys.executable,
            "-c",
            # A lone 0xFF is invalid UTF-8 in any position.
            "import sys; sys.stdout.buffer.write(b'ok \\xff done'); sys.exit(3)",
        ],
        timeout=30,
    )
    assert result.returncode == 3, "the exit code must survive the bad byte"
    assert "ok " in result.stdout and "done" in result.stdout
    assert "�" in result.stdout, "the undecodable byte becomes U+FFFD, not an exception"


# --- the reproduce-it-yourself remediation string ---------------------------


def test_failure_remediation_is_a_command_you_can_actually_paste() -> None:
    """`CommandResult.display` must survive an argv element needing quoting.

    It is handed to the operator as copy-paste remediation (stack.py's
    "reproduce it directly with:"), so a stack directory containing a space
    must not render a command that silently means something else.
    """
    result = CommandResult(
        argv=("docker", "compose", "-f", "/home/My Stack/compose.yaml", "up"),
        returncode=1,
        stdout="",
        stderr="boom",
    )
    assert "'/home/My Stack/compose.yaml'" in result.display
    # The naive " ".join would produce a 6-word command; quoting keeps it 5.
    assert shlex.split(result.display) == list(result.argv)


# --- logs and down ----------------------------------------------------------


def test_logs_is_bounded_by_default(
    stack: Path, docker: FakeDocker, capsys: pytest.CaptureFixture[str]
) -> None:
    """No --follow, and a finite --tail: an unbounded stream hangs an agent turn."""
    capsys.readouterr()
    assert main(["logs", "--dir", str(stack)]) == 0
    argv = docker.argv_for("logs")
    assert argv[-3:] == ["--no-color", "--tail", "100"]
    assert "--follow" not in argv
    assert "-f" not in argv


def test_logs_tail_flag_reaches_compose(
    stack: Path, docker: FakeDocker, capsys: pytest.CaptureFixture[str]
) -> None:
    capsys.readouterr()
    assert main(["logs", "--dir", str(stack), "--tail", "5"]) == 0
    assert docker.argv_for("logs")[-1] == "5"


def test_logs_bounds_duration_as_well_as_output(
    stack: Path, docker: FakeDocker, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--tail` caps how much comes back; `--timeout` caps how long we wait.

    Bounding output alone is not enough: on a loaded host `docker compose logs`
    can be slow to answer even a small tail, and an agent turn hangs on the
    wait, not on the byte count.
    """
    capsys.readouterr()
    assert main(["logs", "--dir", str(stack)]) == 0
    assert docker.kwargs[-1]["timeout"] == 30, "default must be finite"

    assert main(["logs", "--dir", str(stack), "--timeout", "5"]) == 0
    assert docker.kwargs[-1]["timeout"] == 5, "--timeout must reach the runner"


def test_logs_json_returns_lines(
    stack: Path, docker: FakeDocker, capsys: pytest.CaptureFixture[str]
) -> None:
    docker.respond("logs", stdout="broker | mosquitto version 2.1.2 running\nbroker | ok\n")
    capsys.readouterr()

    assert main(["logs", "--dir", str(stack), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["tail"] == 100
    assert len(payload["lines"]) == 2


def test_down_keeps_the_data_volume_by_default(
    stack: Path, docker: FakeDocker, capsys: pytest.CaptureFixture[str]
) -> None:
    capsys.readouterr()
    assert main(["down", "--dir", str(stack), "--json"]) == 0
    assert "--volumes" not in docker.argv_for("down")
    payload = json.loads(capsys.readouterr().out)
    assert payload["data_destroyed"] is False


def test_down_volumes_is_explicit_and_says_what_it_destroys(
    stack: Path, docker: FakeDocker, capsys: pytest.CaptureFixture[str]
) -> None:
    capsys.readouterr()
    assert main(["down", "--dir", str(stack), "--volumes", "--json"]) == 0
    assert docker.argv_for("down")[-1] == "--volumes"
    payload = json.loads(capsys.readouterr().out)
    assert payload["data_destroyed"] is True


# --- environment errors -----------------------------------------------------


@pytest.mark.parametrize("verb", ["up", "status", "logs", "down"])
def test_verbs_require_an_initialised_stack(
    verb: str, tmp_path: Path, docker: FakeDocker, capsys: pytest.CaptureFixture[str]
) -> None:
    capsys.readouterr()
    assert main([verb, "--dir", str(tmp_path / "missing")]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("error:")
    assert "init" in captured.err


@pytest.mark.parametrize("verb", ["up", "status", "logs", "down"])
def test_missing_docker_is_an_environment_error(
    verb: str,
    stack: Path,
    docker: FakeDocker,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("events_cli.stack._docker.shutil.which", lambda _: None)
    capsys.readouterr()

    assert main([verb, "--dir", str(stack)]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "docker" in captured.err
    assert "hint:" in captured.err


def test_a_docker_binary_that_vanishes_mid_run_is_still_not_a_traceback(
    stack: Path,
    docker: FakeDocker,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``shutil.which`` can pass and the exec still fail; both end as exit 2."""
    from events_cli.stack import DockerUnavailable

    def explode(argv, **kwargs):
        raise DockerUnavailable("docker not found on PATH")

    monkeypatch.setattr("events_cli.stack._docker.run", explode)
    capsys.readouterr()

    assert main(["status", "--dir", str(stack)]) == 2
    captured = capsys.readouterr()
    assert "Traceback" not in captured.err
    assert captured.err.startswith("error:")


def test_a_docker_timeout_is_an_environment_error(
    stack: Path,
    docker: FakeDocker,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from events_cli.stack import DockerTimeout

    def stall(argv, **kwargs):
        raise DockerTimeout("timed out after 30s: docker compose ps")

    monkeypatch.setattr("events_cli.stack._docker.run", stall)
    capsys.readouterr()

    assert main(["status", "--dir", str(stack)]) == 2
    err = capsys.readouterr().err
    assert "timed out" in err
    assert "--timeout" in err


# --- criterion 4: the agent-first contract ---------------------------------

STACK_VERBS = ["init", "up", "status", "logs", "down"]


@pytest.mark.parametrize("verb", STACK_VERBS)
def test_every_stack_verb_has_an_explain_entry(
    verb: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["explain", verb]) == 0
    out = capsys.readouterr().out
    assert out.startswith(f"# events {verb}")
    assert len(out) > 200


@pytest.mark.parametrize("verb", STACK_VERBS)
def test_every_stack_verb_accepts_json(
    verb: str, stack: Path, docker: FakeDocker, capsys: pytest.CaptureFixture[str]
) -> None:
    capsys.readouterr()
    args = [verb, "--dir", str(stack), "--json"]
    if verb == "init":
        args.append("--force")
    rc = main(args)
    assert rc in (0, 1)
    json.loads(capsys.readouterr().out)


@pytest.mark.parametrize("verb", STACK_VERBS)
def test_every_stack_verb_routes_parse_errors_through_the_error_contract(
    verb: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exc:
        main([verb, "--bogus-flag"])
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert err.startswith("error:")
    assert "hint:" in err
    assert "Traceback" not in err


def test_stack_verbs_appear_in_learn_and_overview(capsys: pytest.CaptureFixture[str]) -> None:
    """The self-teaching surfaces must not still say the broker is unimplemented."""
    assert main(["learn", "--json"]) == 0
    paths = {tuple(c["path"]) for c in json.loads(capsys.readouterr().out)["commands"]}
    for verb in STACK_VERBS:
        assert (verb,) in paths

    assert main(["overview"]) == 0
    overview = capsys.readouterr().out
    for verb in STACK_VERBS:
        assert f"{verb} —" in overview


# --- the subprocess seam itself --------------------------------------------
#
# The tests above replace `run`; these are the only ones that exercise it, and
# they still spawn nothing — they replace `subprocess.run` one layer lower.


def test_run_never_uses_a_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fixed argv list, ``shell=False``, bounded timeout — the bandit skip's premise.

    B603 is skipped repo-wide because this call site is safe. That skip is only
    honest while these three properties hold, so pin them.
    """
    import subprocess

    from events_cli.stack._docker import run

    seen: dict = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        seen["kwargs"] = kwargs
        return subprocess.CompletedProcess(argv, 0, "out", "err")

    monkeypatch.setattr("events_cli.stack._docker.subprocess.run", fake_run)
    result = run(["docker", "compose", "ps"], timeout=17)

    assert seen["argv"] == ["docker", "compose", "ps"]
    assert isinstance(seen["argv"], list), "a string argv would be shell-parsed"
    assert seen["kwargs"].get("shell") in (None, False)
    assert seen["kwargs"]["timeout"] == 17
    assert result.ok and result.stdout == "out"


def test_run_translates_a_missing_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    from events_cli.stack import DockerUnavailable
    from events_cli.stack._docker import run

    def missing(argv, **kwargs):
        raise FileNotFoundError(2, "No such file or directory", "docker")

    monkeypatch.setattr("events_cli.stack._docker.subprocess.run", missing)
    with pytest.raises(DockerUnavailable):
        run(["docker", "ps"])


def test_run_translates_a_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess

    from events_cli.stack import DockerTimeout
    from events_cli.stack._docker import run

    def stall(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, 5)

    monkeypatch.setattr("events_cli.stack._docker.subprocess.run", stall)
    with pytest.raises(DockerTimeout):
        run(["docker", "ps"], timeout=5)


def test_parse_ps_json_handles_both_compose_output_shapes() -> None:
    from events_cli.stack._docker import parse_ps_json

    assert parse_ps_json("") == []
    assert len(parse_ps_json(f"[{_ps_row()}, {_ps_row()}]")) == 2
    # True NDJSON: several objects, one per line, which is not valid JSON as a
    # whole document.
    assert len(parse_ps_json(f"{_ps_row()}\n{_ps_row()}\n")) == 2
    # Garbage yields nothing rather than raising; callers gate on returncode.
    assert parse_ps_json("not json at all") == []


# --- the port probe ---------------------------------------------------------


def test_port_probe_detects_a_real_listener() -> None:
    """A genuine socket test — no docker, no fixed port, nothing left behind."""
    import socket

    from events_cli.stack._preflight import port_in_use

    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        assert port_in_use("127.0.0.1", port) is True

    # The socket is closed now, so the same port must read as free.
    assert port_in_use("127.0.0.1", port) is False

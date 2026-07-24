"""Stack verbs — ``init``, ``up``, ``status``, ``logs``, ``down``.

These are the verbs that make ``events`` actually operate a broker. They are
registered at the top level rather than under a ``stack`` noun because that is
the surface the contract names: ``events up``, not ``events stack up``.

Layering
--------
Everything about the deployment itself lives in :mod:`events_cli.stack` — the
templates, the argv construction, the port preflight, the status parsing. This
module is only the translation layer: it turns a
:class:`~events_cli.stack.StackError` into a
:class:`~events_cli.cli._errors.CliError` with an exit code and a hint, and
turns a report dataclass into stdout. It never prints an error itself, and no
handler here lets an exception escape untranslated.

Exit codes used here
--------------------
``2`` (environment error) covers everything that is wrong with the machine
rather than the invocation: docker missing, the stack not initialised, a docker
command failing, and — deliberately — the foreign-broker refusal. That refusal
is not a mistyped flag; it is a fact about the host the caller has to go and
change. ``1`` is reserved for genuine user error, of which there is exactly one
here: asking ``init`` to overwrite without ``--force``.

``status`` follows ``doctor``'s precedent and exits ``1`` when the thing it
inspected is not healthy, so ``events status && ...`` means what it looks like.

The test seam
-------------
Every docker invocation funnels through :func:`events_cli.stack._docker.run`,
resolved at call time. Substituting that one function is enough to exercise
every path in this module with no docker on the machine, which is why the unit
suite has no container dependency.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from events_cli.cli._errors import EXIT_ENV_ERROR, EXIT_USER_ERROR, CliError
from events_cli.cli._output import emit_result
from events_cli.cli._prog import prog_name
from events_cli.stack import (
    BROKER_PORT,
    COMPOSE_FILENAME,
    LOOPBACK_ADDR,
    MOSQUITTO_CONF_FILENAME,
    MOSQUITTO_IMAGE,
    MOSQUITTO_VERSION,
    PROJECT_NAME,
    PUBLISHED_MAPPING,
    STACK_DIR_ENV,
    DockerTimeout,
    DockerUnavailable,
    StackError,
    default_stack_dir,
    is_initialised,
    write_stack,
)
from events_cli.stack._docker import (
    CommandResult,
    compose_argv,
    docker_available,
    parse_ps_json,
    resolve_runner,
)
from events_cli.stack._preflight import preflight
from events_cli.stack._status import StackStatus, status_from_rows

#: Extra seconds allowed on top of a verb's ``--timeout`` before the subprocess
#: itself is killed. Compose needs a moment to tear down after it gives up, and
#: killing it mid-teardown is how you get an orphaned container.
_RUN_MARGIN = 30

#: Bound for the short, read-only invocations (``ps``, ``logs``). Non-infinite
#: by policy: an agent-facing verb that can block forever hangs a turn.
_QUERY_TIMEOUT = 30


# --- shared plumbing -------------------------------------------------------


def _stack_dir(args: argparse.Namespace) -> Path:
    explicit = getattr(args, "dir", None)
    return Path(explicit).expanduser() if explicit else default_stack_dir()


def _require_initialised(directory: Path) -> Path:
    if not is_initialised(directory):
        raise CliError(
            code=EXIT_ENV_ERROR,
            message=(
                f"no broker stack at {directory} "
                f"(expected {COMPOSE_FILENAME} and {MOSQUITTO_CONF_FILENAME})"
            ),
            remediation=f"generate it with: {prog_name()} init",
        )
    return directory


def _require_docker() -> None:
    if not docker_available():
        raise CliError(
            code=EXIT_ENV_ERROR,
            message="docker is not on PATH",
            remediation="install Docker Engine with the Compose v2 plugin, then re-run",
        )


def _execute(argv: list[str], *, timeout: int) -> CommandResult:
    """Run one docker command, translating environment failures to CliError."""
    try:
        return resolve_runner(None)(argv, timeout=timeout)
    except DockerUnavailable as exc:
        raise CliError(
            code=EXIT_ENV_ERROR,
            message=str(exc),
            remediation="install Docker Engine with the Compose v2 plugin, then re-run",
        ) from exc
    except DockerTimeout as exc:
        raise CliError(
            code=EXIT_ENV_ERROR,
            message=str(exc),
            remediation="raise --timeout, or check the daemon with: docker info",
        ) from exc
    except StackError as exc:  # pragma: no cover - defensive; keeps tracebacks in
        raise CliError(
            code=EXIT_ENV_ERROR,
            message=str(exc),
            remediation="check the docker daemon is running: docker info",
        ) from exc


def _failure(result: CommandResult, what: str) -> CliError:
    """Build the CliError for a docker command that ran and failed."""
    detail = (result.stderr or result.stdout or "").strip().splitlines()
    tail = detail[-1] if detail else "no output"
    return CliError(
        code=EXIT_ENV_ERROR,
        message=f"{what} failed (exit {result.returncode}): {tail}",
        remediation=f"reproduce it directly with: {result.display}",
    )


def _query_status(directory: Path) -> StackStatus:
    """Ask compose for the current state of the stack's containers."""
    argv = compose_argv(directory, "ps", "--all", "--format", "json")
    result = _execute(argv, timeout=_QUERY_TIMEOUT)
    if not result.ok:
        raise _failure(result, "docker compose ps")
    return status_from_rows(str(directory), parse_ps_json(result.stdout))


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--dir",
        metavar="PATH",
        help="Stack directory holding compose.yaml and mosquitto.conf. Defaults to "
        f"the per-user path under $XDG_CONFIG_HOME; override with ${STACK_DIR_ENV}.",
    )
    parser.add_argument("--json", action="store_true", help="Emit structured JSON.")


def _emit(payload: dict[str, object], lines: list[str], *, json_mode: bool) -> None:
    if json_mode:
        emit_result(payload, json_mode=True)
    else:
        emit_result("\n".join(lines), json_mode=False)


# --- init ------------------------------------------------------------------


def cmd_init(args: argparse.Namespace) -> int:
    target = _stack_dir(args)
    try:
        written = write_stack(target, force=bool(args.force))
    except FileExistsError as exc:
        raise CliError(
            code=EXIT_USER_ERROR,
            message=f"stack files already exist: {exc}",
            remediation="re-run with --force to regenerate them; that discards any "
            "local edits, including a deliberate remote-access opt-in",
        ) from exc
    except OSError as exc:
        raise CliError(
            code=EXIT_ENV_ERROR,
            message=f"could not write the stack to {target}: {exc}",
            remediation="check the directory is writable, or choose another with --dir",
        ) from exc

    payload: dict[str, object] = {
        "stack_dir": str(target),
        "written": [str(path) for path in written],
        "image": MOSQUITTO_IMAGE,
        "mosquitto_version": MOSQUITTO_VERSION,
        "compose_project": PROJECT_NAME,
        "broker": {
            "address": LOOPBACK_ADDR,
            "port": BROKER_PORT,
            "published": PUBLISHED_MAPPING,
            "loopback_only": True,
            "anonymous": True,
            "websockets": False,
        },
        "next": f"{prog_name()} up",
    }
    lines = [
        f"wrote broker stack to {target}",
        *(f"  {path.name}" for path in written),
        "",
        f"image:     {MOSQUITTO_IMAGE}  (exact tag, not a floating major)",
        f"published: {PUBLISHED_MAPPING}  (loopback only - not reachable from the LAN)",
        "listener:  1883 MQTT, anonymous; no websocket and no http_api listener",
        "storage:   persistence on, named volume events-data, autosave every 60s",
        "",
        f"next: {prog_name()} up",
    ]
    _emit(payload, lines, json_mode=bool(args.json))
    return 0


# --- up --------------------------------------------------------------------


def cmd_up(args: argparse.Namespace) -> int:
    directory = _require_initialised(_stack_dir(args))
    _require_docker()

    check = preflight(BROKER_PORT)
    if check.blocked:
        # Environment error, not user error: the caller typed a correct command
        # and it is the host that has to change.
        raise CliError(
            code=EXIT_ENV_ERROR,
            message=check.message(),
            remediation=check.remediation(prog_name()),
        )

    timeout = int(args.timeout)
    argv = compose_argv(
        directory,
        "up",
        "--detach",
        "--wait",
        "--wait-timeout",
        str(timeout),
    )
    result = _execute(argv, timeout=timeout + _RUN_MARGIN)
    if not result.ok:
        raise _failure(result, "docker compose up")

    status = _query_status(directory)
    payload: dict[str, object] = {
        "stack_dir": str(directory),
        "preflight": check.to_dict(),
        "command": list(result.argv),
        "status": status.to_dict(),
    }
    lines = [
        f"broker stack up ({directory})",
        f"preflight: port {BROKER_PORT} was {check.verdict}",
        f"status:    {status.summary()}",
        f"endpoint:  mqtt://{LOOPBACK_ADDR}:{BROKER_PORT}"
        + ("" if status.loopback_only else "  WARNING: also published off loopback"),
    ]
    _emit(payload, lines, json_mode=bool(args.json))
    # `up --wait` already blocked until healthy, so an unhealthy broker here is
    # a real result and deserves a non-zero code rather than a cheerful zero.
    return 0 if status.healthy else 1


# --- status ----------------------------------------------------------------


def cmd_status(args: argparse.Namespace) -> int:
    directory = _require_initialised(_stack_dir(args))
    _require_docker()
    status = _query_status(directory)

    payload = status.to_dict()
    payload["image"] = MOSQUITTO_IMAGE
    payload["endpoint"] = f"mqtt://{LOOPBACK_ADDR}:{BROKER_PORT}"

    lines = [f"broker stack: {status.summary()}", f"stack_dir:    {directory}"]
    for svc in status.services:
        lines.append(
            f"  {svc.service or svc.name}: state={svc.state} health={svc.health} "
            f"ports={svc.ports or 'none'}"
        )
    if not status.services:
        lines.append(f"  (no containers - start the broker with '{prog_name()} up')")
    if not status.loopback_only:
        lines.append(
            "  WARNING: the broker port is published on a non-loopback address; "
            "it is reachable from the LAN"
        )
    _emit(payload, lines, json_mode=bool(args.json))
    return 0 if status.healthy else 1


# --- logs ------------------------------------------------------------------


def cmd_logs(args: argparse.Namespace) -> int:
    directory = _require_initialised(_stack_dir(args))
    _require_docker()

    tail = int(args.tail)
    argv = compose_argv(directory, "logs", "--no-color", "--tail", str(tail))
    result = _execute(argv, timeout=_QUERY_TIMEOUT)
    if not result.ok:
        raise _failure(result, "docker compose logs")

    lines = result.stdout.splitlines()
    if bool(args.json):
        emit_result({"stack_dir": str(directory), "tail": tail, "lines": lines}, json_mode=True)
    else:
        emit_result("\n".join(lines) if lines else "(no log output)", json_mode=False)
    return 0


# --- down ------------------------------------------------------------------


def cmd_down(args: argparse.Namespace) -> int:
    directory = _require_initialised(_stack_dir(args))
    _require_docker()

    destroy = bool(args.volumes)
    argv = compose_argv(directory, "down")
    if destroy:
        argv.append("--volumes")
    result = _execute(argv, timeout=int(args.timeout) + _RUN_MARGIN)
    if not result.ok:
        raise _failure(result, "docker compose down")

    payload: dict[str, object] = {
        "stack_dir": str(directory),
        "command": list(result.argv),
        "volumes_removed": destroy,
        "data_destroyed": destroy,
    }
    lines = [
        f"broker stack down ({directory})",
        (
            "removed the events-data volume: retained messages and queued sessions are gone"
            if destroy
            else "kept the events-data volume: retained messages survive the next 'up'"
        ),
    ]
    _emit(payload, lines, json_mode=bool(args.json))
    return 0


# --- registration ----------------------------------------------------------


def register(sub: argparse._SubParsersAction) -> None:
    init = sub.add_parser(
        "init",
        help="Generate the loopback-only broker stack (compose.yaml + mosquitto.conf).",
    )
    _add_common(init)
    init.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing stack files. Discards local edits.",
    )
    init.set_defaults(func=cmd_init)

    up = sub.add_parser("up", help="Start the broker, refusing if another one holds the port.")
    _add_common(up)
    up.add_argument(
        "--timeout",
        type=int,
        default=60,
        metavar="SECONDS",
        help="How long to wait for the broker to report healthy (default: 60).",
    )
    up.set_defaults(func=cmd_up)

    status = sub.add_parser(
        "status",
        help="Report broker state and health. Exits 1 when it is not healthy.",
    )
    _add_common(status)
    status.set_defaults(func=cmd_status)

    logs = sub.add_parser("logs", help="Print the last N lines of the broker log.")
    _add_common(logs)
    logs.add_argument(
        "--tail",
        type=int,
        default=100,
        metavar="N",
        help="Number of trailing log lines (default: 100). There is no --follow: "
        "an unbounded stream hangs an agent turn.",
    )
    logs.set_defaults(func=cmd_logs)

    down = sub.add_parser("down", help="Stop and remove the broker containers.")
    _add_common(down)
    down.add_argument(
        "--volumes",
        action="store_true",
        help="Also delete the events-data volume. Destroys retained messages and "
        "queued sessions.",
    )
    down.add_argument(
        "--timeout",
        type=int,
        default=30,
        metavar="SECONDS",
        help="Seconds to allow for the stack to stop (default: 30).",
    )
    down.set_defaults(func=cmd_down)

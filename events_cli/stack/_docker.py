"""The `docker` seam: fixed-argv command construction and one place that runs it.

Every invocation is a **list**, built here, passed to :func:`subprocess.run`
with ``shell=False`` (the default) and a bounded ``timeout``. No user-supplied
string is ever concatenated into a command; the only caller-controlled values
that reach an argv are a filesystem path and integers the parser has already
validated as ints, and both arrive as their own list elements where a shell
would never see them. bandit's B404/B603 are skipped repo-wide for exactly this
pattern — the skip is only honest while the rule above holds.

:func:`run` is the single seam. Tests substitute a fake runner rather than
monkeypatching :mod:`subprocess`, which is what keeps the unit suite free of any
docker dependency: nothing in ``tests/`` executes a container.
"""

from __future__ import annotations

import json
import shlex
import shutil
import subprocess  # nosec B404 - fixed argv lists, shell=False; see module docstring
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from events_cli.stack import (
    COMPOSE_FILENAME,
    PROJECT_NAME,
    DockerTimeout,
    DockerUnavailable,
)

DOCKER_BIN = "docker"

#: Default wall-clock bound for a docker invocation. Non-infinite by policy:
#: an agent-facing verb that can block forever eventually hangs a turn.
DEFAULT_TIMEOUT = 120


@dataclass(frozen=True)
class CommandResult:
    """The outcome of one docker invocation. Never raises on non-zero."""

    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def display(self) -> str:
        """The command as a human would retype it. Diagnostics only.

        ``shlex.join``, not ``" ".join``: this string is handed to the operator
        as copy-paste remediation, and a stack directory containing a space
        would otherwise render a command that silently means something else.
        """
        return shlex.join(self.argv)


#: A runner takes an argv list and a timeout and returns a :class:`CommandResult`.
Runner = Callable[..., CommandResult]


def docker_available() -> bool:
    """True when a ``docker`` executable is on PATH.

    Cheap and side-effect free; it says nothing about whether the daemon is
    reachable. That is deliberate — probing the daemon costs a round trip, and
    the real invocation reports a dead daemon perfectly well through stderr.
    """
    return shutil.which(DOCKER_BIN) is not None


def compose_argv(stack_dir: Path | str, *args: str, project: str = PROJECT_NAME) -> list[str]:
    """Build a ``docker compose`` argv for the generated stack.

    ``--project-name`` is passed explicitly rather than inherited from the
    directory name, so the project identity is the same no matter where the
    operator put the stack — preflight matches containers on that label.
    """
    return [
        DOCKER_BIN,
        "compose",
        "--project-name",
        project,
        "--file",
        str(Path(stack_dir) / COMPOSE_FILENAME),
        *args,
    ]


def docker_argv(*args: str) -> list[str]:
    """Build a plain ``docker`` argv (used by preflight's container lookup)."""
    return [DOCKER_BIN, *args]


def run(argv: Sequence[str], *, timeout: int = DEFAULT_TIMEOUT) -> CommandResult:
    """Execute ``argv`` and capture its output. The only process spawn here.

    A non-zero exit is data, not an exception — callers decide what a failure
    means. A missing binary or a blown timeout are environment failures and do
    raise, as :class:`DockerUnavailable` / :class:`DockerTimeout`, so no
    ``FileNotFoundError`` traceback can escape to stderr.
    """
    argv = list(argv)
    try:
        proc = subprocess.run(  # nosec B603 - fixed argv, shell=False, bounded timeout
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise DockerUnavailable(f"{DOCKER_BIN} not found on PATH") from exc
    except PermissionError as exc:
        raise DockerUnavailable(f"{DOCKER_BIN} is not executable: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise DockerTimeout(f"timed out after {timeout}s: {' '.join(argv)}") from exc
    return CommandResult(tuple(argv), proc.returncode, proc.stdout or "", proc.stderr or "")


def resolve_runner(runner: Runner | None) -> Runner:
    """Return the caller's runner, or the real one."""
    return runner if runner is not None else run


def parse_ps_json(text: str) -> list[dict]:
    """Parse ``docker compose ps --format json`` output.

    Compose changed this format mid-v2: older builds emit a single JSON array,
    newer ones emit newline-delimited objects. Both appear in the wild on the
    machines this ships to, so both are accepted. Unparseable output yields an
    empty list rather than an exception — ``status`` reporting "no containers"
    on garbage is wrong, so callers check :attr:`CommandResult.ok` first and
    only parse output from a successful invocation.
    """
    text = text.strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        rows: list[dict] = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                rows.append(obj)
        return rows
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    if isinstance(data, dict):
        return [data]
    return []

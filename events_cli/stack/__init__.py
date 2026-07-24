"""Broker stack: the Dockerised Mosquitto deployment `events` operates.

This package owns everything about *how the broker is deployed* — the Compose
and `mosquitto.conf` templates, the `docker compose` argv construction, and the
preflight that refuses to start on top of somebody else's broker. It is
deliberately free of CLI concerns: nothing here imports
:mod:`events_cli.cli`, nothing here prints, and failures raise
:class:`StackError` (or a plain :class:`OSError`) rather than ``CliError``.

The CLI layer in :mod:`events_cli.cli._commands.stack` is the only thing that
translates those into exit codes and ``error:``/``hint:`` output. Keeping the
split means the same core can back the HTTP and MCP surfaces later without an
adapter having to unpick argparse-shaped errors.

Templates are shipped verbatim
------------------------------
``compose.yaml`` and ``mosquitto.conf`` under ``templates/`` are complete,
final files — there is no substitution step. ``events init`` copies them byte
for byte. That is what makes "the compose file contains the literal
``127.0.0.1:1883:1883``" a property of a file a reviewer can read, rather than
of a render path they have to simulate. The constants below mirror the
templates; ``tests/test_stack_templates.py`` pins them to each other so the two
cannot drift apart silently.
"""

from __future__ import annotations

import os
from importlib import resources
from pathlib import Path

# --- the deployment's fixed facts -----------------------------------------
#
# None of these are configurable. The port is not configurable because
# loopback-only is the security property this whole module exists to hold, and
# a `--port` flag is the first step toward an operator "temporarily" binding
# 0.0.0.0. Remote access is a documented opt-in that edits compose.yaml, so the
# edit is visible in a diff.

#: Exact patch tag. Never the floating ``eclipse-mosquitto:2``.
#:
#: The ``-alpine`` suffix is not a style choice — it is the only way to name
#: this version. Upstream publishes the 2.1 line *exclusively* as
#: ``2.1.x-alpine``; a bare ``eclipse-mosquitto:2.1.2`` has never existed and
#: pulls fail with ``no such manifest`` (deviation d2). ``eclipse-mosquitto:2``
#: and ``eclipse-mosquitto:2.1.2-alpine`` resolve to the same manifest digest
#: today, so this pin names exactly what the floating tag serves — it just
#: cannot move underneath us.
MOSQUITTO_IMAGE = "eclipse-mosquitto:2.1.2-alpine"
#: The broker version the generated ``mosquitto.conf`` documents defaults for.
#: The *software* version, which is why it is not simply the tag's suffix-free
#: form: the tag must be published, this must match ``mosquitto -h``.
MOSQUITTO_VERSION = "2.1.2"

#: Compose project name (``--project-name``), and the label preflight matches on.
PROJECT_NAME = "events-cli"
#: The service key inside compose.yaml.
SERVICE_NAME = "broker"
#: ``container_name`` in compose.yaml — how preflight recognises its own broker.
CONTAINER_NAME = "events-mosquitto"

BROKER_PORT = 1883
LOOPBACK_ADDR = "127.0.0.1"
#: The literal string that must appear in compose.yaml's ``ports:`` list.
PUBLISHED_MAPPING = f"{LOOPBACK_ADDR}:{BROKER_PORT}:{BROKER_PORT}"

COMPOSE_FILENAME = "compose.yaml"
MOSQUITTO_CONF_FILENAME = "mosquitto.conf"
TEMPLATE_FILENAMES = (COMPOSE_FILENAME, MOSQUITTO_CONF_FILENAME)

#: Environment override for the stack directory, mainly so tests and multiple
#: checkouts do not fight over one path.
STACK_DIR_ENV = "EVENTS_STACK_DIR"


class StackError(Exception):
    """A stack-layer failure. The CLI translates these into ``CliError``."""


class DockerUnavailable(StackError):
    """The ``docker`` executable is missing or not runnable."""


class DockerTimeout(StackError):
    """A ``docker`` invocation exceeded its bounded timeout."""


class StackNotInitialised(StackError):
    """No generated stack was found at the requested directory."""


def default_stack_dir() -> Path:
    """Where ``events init`` writes when ``--dir`` is not given.

    Under ``$XDG_CONFIG_HOME`` (or ``~/.config``) rather than the current
    working directory: the generated stack is machine state, not project state,
    and one broker per host is the deployment model. ``EVENTS_STACK_DIR``
    overrides it.
    """
    override = os.environ.get(STACK_DIR_ENV)
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return base / "events-cli" / "stack"


def template_text(name: str) -> str:
    """Read a shipped template by filename.

    Uses ``importlib.resources`` so this works identically from a source
    checkout and from an installed wheel.
    """
    if name not in TEMPLATE_FILENAMES:
        raise StackError(f"unknown stack template: {name}")
    resource = resources.files("events_cli.stack") / "templates" / name
    return resource.read_text(encoding="utf-8")


def compose_path(stack_dir: Path | str) -> Path:
    return Path(stack_dir) / COMPOSE_FILENAME


def is_initialised(stack_dir: Path | str) -> bool:
    """True when every template has been written to ``stack_dir``."""
    directory = Path(stack_dir)
    return all((directory / name).is_file() for name in TEMPLATE_FILENAMES)


def require_initialised(stack_dir: Path | str) -> Path:
    """Return ``stack_dir`` as a Path, or raise :class:`StackNotInitialised`."""
    directory = Path(stack_dir)
    if not is_initialised(directory):
        raise StackNotInitialised(str(directory))
    return directory


def write_stack(stack_dir: Path | str, *, force: bool = False) -> list[Path]:
    """Write both templates into ``stack_dir`` and return the paths written.

    Refuses to clobber an existing file unless ``force`` — a stack directory
    can carry an operator's deliberate divergence (the documented remote-access
    opt-in, for one), and silently overwriting it would move the broker off
    whatever address they had chosen.
    """
    directory = Path(stack_dir).expanduser()
    existing = [directory / name for name in TEMPLATE_FILENAMES if (directory / name).exists()]
    if existing and not force:
        raise FileExistsError(", ".join(str(p) for p in existing))

    directory.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for name in TEMPLATE_FILENAMES:
        target = directory / name
        target.write_text(template_text(name), encoding="utf-8")
        written.append(target)
    return written

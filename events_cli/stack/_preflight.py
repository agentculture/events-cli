"""Refuse to start on top of somebody else's broker.

``events up`` must never quietly attach to, race with, or fail cryptically
against a broker it did not start. There is a specific failure this guards:
a pre-existing MQTT container published on ``0.0.0.0:1883`` — the LAN-exposed
anti-pattern this stack exists to replace. Starting beside it is impossible
(the port is taken) and, worse, an operator who sees "already running" could
reasonably conclude the loopback-only stack is the one serving traffic.

Two independent questions, answered separately:

1. **Is the address we intend to publish on already taken?** Answered by an
   ordinary TCP connect to ``127.0.0.1:1883``. This is the authoritative
   question — it is exactly the bind Docker is about to attempt — and it needs
   no docker at all. A listener on ``0.0.0.0`` answers on loopback too, so one
   probe covers both the wildcard and the loopback binding. A listener bound
   only to a routable address does not conflict with our loopback publish and
   correctly does not trip this.

2. **Who is holding it?** Best effort, via ``docker ps --filter publish=1883``.
   This exists only to turn a refusal into an actionable one: an exact
   ``docker stop <name>``. When it comes up empty — a host process, a rootless
   daemon, a broker in another namespace — the refusal still stands and names
   the command that finds the owner instead. The refusal never depends on being
   able to answer this.
"""

from __future__ import annotations

import socket
from dataclasses import dataclass, field
from typing import Callable

from events_cli.stack import (
    BROKER_PORT,
    CONTAINER_NAME,
    LOOPBACK_ADDR,
    PROJECT_NAME,
    StackError,
)
from events_cli.stack._docker import Runner, docker_argv, resolve_runner

#: Seconds to wait for the TCP connect. Short by design: on loopback a live
#: listener answers immediately, and anything slower is not a broker we would
#: attach to anyway.
PROBE_TIMEOUT = 0.5

#: Field separator in the ``docker ps --format`` template. Chosen because it
#: cannot occur in a container name, image reference, port map or label value.
_SEP = "|||"

_PS_TEMPLATE = _SEP.join(
    (
        "{{.ID}}",
        "{{.Names}}",
        "{{.Image}}",
        "{{.Ports}}",
        '{{.Label "com.docker.compose.project"}}',
    )
)

#: A probe answers "is something listening on this address:port?".
Probe = Callable[[str, int], bool]

VERDICT_FREE = "free"
VERDICT_OURS = "ours"
VERDICT_FOREIGN = "foreign"


def port_in_use(host: str = LOOPBACK_ADDR, port: int = BROKER_PORT) -> bool:
    """True when a TCP connect to ``host:port`` succeeds."""
    try:
        with socket.create_connection((host, port), timeout=PROBE_TIMEOUT):
            return True
    except OSError:
        # Refused, unreachable, or timed out — nothing we could collide with.
        return False


def _address_is_loopback(address: str) -> bool:
    address = address.strip().strip("[]")
    return address == "::1" or address.startswith("127.")


def publish_is_lan_exposed(ports: str, port: int = BROKER_PORT) -> bool:
    """True when ``ports`` publishes ``port`` on anything but a loopback address.

    ``ports`` is docker's own rendering, e.g.
    ``0.0.0.0:1883->1883/tcp, [::]:1883->1883/tcp``. Entries without ``->`` are
    merely exposed, not published, and cannot be reached from off-host.
    """
    for entry in ports.split(","):
        entry = entry.strip()
        if "->" not in entry:
            continue
        published, _, _ = entry.partition("->")
        host_address, _, host_port = published.rpartition(":")
        if not host_port.isdigit() or int(host_port) != port:
            continue
        if not _address_is_loopback(host_address):
            return True
    return False


@dataclass(frozen=True)
class PortOwner:
    """A container docker says is publishing the port."""

    container_id: str
    name: str
    image: str
    ports: str
    compose_project: str

    @property
    def is_ours(self) -> bool:
        return self.name == CONTAINER_NAME or self.compose_project == PROJECT_NAME

    @property
    def lan_exposed(self) -> bool:
        return publish_is_lan_exposed(self.ports)

    @property
    def stop_command(self) -> str:
        return f"docker stop {self.name or self.container_id}"

    def to_dict(self) -> dict[str, object]:
        return {
            "container_id": self.container_id,
            "name": self.name,
            "image": self.image,
            "ports": self.ports,
            "compose_project": self.compose_project,
            "lan_exposed": self.lan_exposed,
        }


@dataclass(frozen=True)
class PreflightResult:
    """What ``events up`` learned before touching the stack."""

    port: int
    in_use: bool
    verdict: str
    owner: PortOwner | None = None
    owners: list[PortOwner] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return self.verdict == VERDICT_FOREIGN

    @property
    def stop_command(self) -> str | None:
        """The exact command that frees the port, when we can name one."""
        return self.owner.stop_command if self.owner is not None else None

    def message(self) -> str:
        """One line naming what is in the way. Used verbatim in the refusal."""
        if self.owner is None:
            return (
                f"something is already listening on {LOOPBACK_ADDR}:{self.port}, "
                "and it is not a container this stack manages"
            )
        exposure = (
            " and is published on a non-loopback address, reachable from the LAN"
            if self.owner.lan_exposed
            else ""
        )
        project = (
            f" (compose project '{self.owner.compose_project}')"
            if self.owner.compose_project
            else ""
        )
        return (
            f"container '{self.owner.name}' ({self.owner.image}){project} already holds "
            f"port {self.port}{exposure}"
        )

    @property
    def discovery_command(self) -> str:
        """How to identify a listener docker could not account for."""
        return f"sudo ss -ltnp 'sport = :{self.port}'"

    def remediation(self, prog: str = "events") -> str:
        """The exact next command. Never a suggestion to retry blindly.

        ``prog`` is threaded in by the CLI so the hint names the invocation the
        caller is actually using (``events`` when installed, ``python -m
        events_cli`` from a checkout). The stack layer stays CLI-free; it just
        does not hard-code a command name that may not be on the caller's PATH.
        """
        if self.owner is not None:
            return (
                f"stop it with: {self.owner.stop_command} — then re-run "
                f"'{prog} up'. Exactly one broker may own this port"
            )
        return f"find the owner with: {self.discovery_command} — stop it, then re-run '{prog} up'"

    def to_dict(self) -> dict[str, object]:
        return {
            "port": self.port,
            "in_use": self.in_use,
            "verdict": self.verdict,
            "owner": self.owner.to_dict() if self.owner is not None else None,
            "stop_command": self.stop_command,
        }


def _parse_ps_line(line: str) -> PortOwner | None:
    fields = line.split(_SEP)
    if len(fields) < 5:
        return None
    container_id, name, image, ports, project = (f.strip() for f in fields[:5])
    if not container_id and not name:
        return None
    return PortOwner(
        container_id=container_id,
        name=name,
        image=image,
        ports=ports,
        compose_project=project,
    )


def containers_publishing(
    port: int = BROKER_PORT, *, runner: Runner | None = None
) -> list[PortOwner]:
    """Ask docker which running containers publish ``port``. Best effort.

    Any docker failure returns an empty list: this is an identification aid, not
    a gate. :func:`preflight` refuses on the TCP probe alone.
    """
    execute = resolve_runner(runner)
    argv = docker_argv(
        "ps",
        "--filter",
        f"publish={port}",
        "--format",
        _PS_TEMPLATE,
    )
    try:
        result = execute(argv, timeout=15)
    except StackError:
        return []
    if not result.ok:
        return []
    owners = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        owner = _parse_ps_line(line)
        if owner is not None:
            owners.append(owner)
    return owners


def preflight(
    port: int = BROKER_PORT,
    *,
    runner: Runner | None = None,
    probe: Probe | None = None,
) -> PreflightResult:
    """Decide whether ``events up`` may proceed.

    Three outcomes: the port is free; the port is held by our own broker (``up``
    is idempotent, proceed); or the port is held by something else (refuse).
    """
    check = probe if probe is not None else port_in_use
    if not check(LOOPBACK_ADDR, port):
        return PreflightResult(port=port, in_use=False, verdict=VERDICT_FREE)

    owners = containers_publishing(port, runner=runner)
    ours = [owner for owner in owners if owner.is_ours]
    if ours:
        return PreflightResult(
            port=port,
            in_use=True,
            verdict=VERDICT_OURS,
            owner=ours[0],
            owners=owners,
        )

    return PreflightResult(
        port=port,
        in_use=True,
        verdict=VERDICT_FOREIGN,
        owner=owners[0] if owners else None,
        owners=owners,
    )

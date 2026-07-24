"""Turn ``docker compose ps`` output into a report that does not flatter itself.

Two rules govern everything here:

**Running is not healthy.** A container can be up, listening, and refusing every
connection. ``healthy`` is true only when docker's own healthcheck says
``healthy`` — never inferred from ``State == "running"``, and never from the
absence of a healthcheck. A broker with no healthcheck configured reports
health ``none`` and ``healthy: false``, because "we did not check" is not a
pass.

**Report the address actually published, not the one we generated.** The stack
directory is a file an operator can edit, so ``loopback_only`` is derived from
what docker says is bound right now. That is what catches the case this whole
module exists for: a broker quietly moved onto ``0.0.0.0``.
"""

from __future__ import annotations

from dataclasses import dataclass

from events_cli.stack import BROKER_PORT, SERVICE_NAME
from events_cli.stack._preflight import publish_is_lan_exposed

#: Docker's health states. Anything else (including "") means no healthcheck ran.
HEALTH_HEALTHY = "healthy"
HEALTH_NONE = "none"


def _publishers_to_ports(publishers: list) -> str:
    """Render compose's structured ``Publishers`` the way ``docker ps`` renders ports.

    Normalising to the single string form means
    :func:`~events_cli.stack._preflight.publish_is_lan_exposed` is the only
    place that knows how to judge an address, rather than having a second
    near-copy of that rule for a second input shape.
    """
    parts = []
    for pub in publishers:
        if not isinstance(pub, dict):
            continue
        published = pub.get("PublishedPort")
        target = pub.get("TargetPort")
        if not published:
            # Not published to the host at all; exposed only inside the network.
            continue
        url = str(pub.get("URL") or "")
        protocol = str(pub.get("Protocol") or "tcp")
        host = f"{url}:{published}" if url else str(published)
        parts.append(f"{host}->{target}/{protocol}")
    return ", ".join(parts)


@dataclass(frozen=True)
class ServiceState:
    """One container's state, as docker reports it."""

    name: str
    service: str
    image: str
    state: str
    health: str
    ports: str
    status: str
    exit_code: int | None

    @property
    def running(self) -> bool:
        return self.state.lower() == "running"

    @property
    def healthy(self) -> bool:
        # Deliberately strict: only docker's own "healthy" counts.
        return self.health == HEALTH_HEALTHY

    @property
    def lan_exposed(self) -> bool:
        return publish_is_lan_exposed(self.ports, BROKER_PORT)

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "service": self.service,
            "image": self.image,
            "state": self.state,
            "health": self.health,
            "healthy": self.healthy,
            "running": self.running,
            "ports": self.ports,
            "status": self.status,
            "exit_code": self.exit_code,
            "lan_exposed": self.lan_exposed,
        }


def service_from_row(row: dict) -> ServiceState:
    """Build a :class:`ServiceState` from one ``compose ps --format json`` row.

    Compose has shipped both a ``Ports`` string and a structured ``Publishers``
    list across its v2 series; accept either.
    """
    ports = str(row.get("Ports") or "")
    if not ports:
        publishers = row.get("Publishers") or []
        if isinstance(publishers, list):
            ports = _publishers_to_ports(publishers)

    health = str(row.get("Health") or "").strip().lower() or HEALTH_NONE
    exit_code = row.get("ExitCode")
    return ServiceState(
        name=str(row.get("Name") or ""),
        service=str(row.get("Service") or ""),
        image=str(row.get("Image") or ""),
        state=str(row.get("State") or ""),
        health=health,
        ports=ports,
        status=str(row.get("Status") or ""),
        exit_code=exit_code if isinstance(exit_code, int) else None,
    )


@dataclass(frozen=True)
class StackStatus:
    """The whole stack's state. ``healthy`` is the one an agent should branch on."""

    stack_dir: str
    services: list[ServiceState]

    @property
    def broker(self) -> ServiceState | None:
        for svc in self.services:
            if svc.service == SERVICE_NAME:
                return svc
        return self.services[0] if self.services else None

    @property
    def running(self) -> bool:
        broker = self.broker
        return broker is not None and broker.running

    @property
    def healthy(self) -> bool:
        broker = self.broker
        return broker is not None and broker.running and broker.healthy

    @property
    def loopback_only(self) -> bool:
        """False if any service publishes the broker port off loopback."""
        return not any(svc.lan_exposed for svc in self.services)

    def summary(self) -> str:
        """One line an operator can read without expanding anything."""
        broker = self.broker
        if broker is None:
            return "not running (no containers for this stack)"
        if not broker.running:
            code = "" if broker.exit_code is None else f", exit code {broker.exit_code}"
            return f"not running (state '{broker.state}'{code})"
        if broker.health == HEALTH_NONE:
            return "running, but no healthcheck reported — health unknown"
        if not broker.healthy:
            return f"running but not healthy (health '{broker.health}')"
        return "running and healthy"

    def to_dict(self) -> dict[str, object]:
        return {
            "stack_dir": self.stack_dir,
            "running": self.running,
            "healthy": self.healthy,
            "loopback_only": self.loopback_only,
            "summary": self.summary(),
            "services": [svc.to_dict() for svc in self.services],
        }


def status_from_rows(stack_dir: str, rows: list[dict]) -> StackStatus:
    return StackStatus(stack_dir=stack_dir, services=[service_from_row(r) for r in rows])

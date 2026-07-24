"""Who *this* agent is, read from its own ``culture.yaml``.

Originally this scanner lived inside ``events whoami``, which was its only
caller. It moved here when the subscription registry needed the same answer:
every durable subscription records an **owner**, and the sensible default owner
is the agent that created it — the same nick ``events whoami`` prints. Two
scanners would have been two identities that could disagree, so there is one,
and it sits in :mod:`events_cli.core` where every surface may import it and
nothing has to reach into :mod:`events_cli.cli` to ask a question about
identity.

Parsed with a hand-rolled line scanner rather than PyYAML, deliberately: the
introspection lane imports nothing third-party, so ``events whoami`` and
``events doctor`` run from a bare checkout with nothing installed. The shape
read is exactly the documented one::

    agents:
    - suffix: events-cli
      backend: colleague
      model: …

Anything fancier than that falls back to the literal defaults rather than
guessing.

The file is located by walking up from ``__file__``, never from the working
directory: identity must always be *this* agent's, not whatever
``culture.yaml`` happens to sit beside the caller. A wheel install ships no
``culture.yaml`` alongside the package, so :func:`find_culture_yaml` returns
``None`` there and callers fall back — which is why
:func:`agent_nick` reports ``None`` rather than a plausible-looking lie.
"""

from __future__ import annotations

from pathlib import Path

__all__ = [
    "FALLBACK_NICK",
    "agent_nick",
    "find_culture_yaml",
    "read_agent_fields",
]

#: What ``whoami`` reports when no ``culture.yaml`` can be found. It is the
#: repo/mesh name, so the literal default is also the true answer in the only
#: case that reaches it (a wheel install of this very distribution).
FALLBACK_NICK = "events-cli"

_UNKNOWN = "unknown"


def find_culture_yaml() -> Path | None:
    """Locate this agent's own ``culture.yaml`` by walking up from this module.

    The identity must be the agent's own, not whatever ``culture.yaml`` happens
    to sit in the caller's current working directory. In an editable / source
    install, walking up from ``__file__`` finds the repo root; in a wheel
    install no ``culture.yaml`` ships alongside the package and the caller falls
    back to the literal defaults.
    """
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "culture.yaml"
        if candidate.is_file():
            return candidate
    return None


def read_agent_fields() -> dict[str, str]:
    """Return ``suffix``/``backend``/``model`` from the first agent block.

    Parsed without a YAML dependency to keep the introspection lane free of
    third-party imports. Reads top-level ``key: value`` lines within the first
    agent entry; anything fancier than the documented shape falls back to the
    defaults below.
    """
    fields = {"nick": FALLBACK_NICK, "backend": _UNKNOWN, "model": _UNKNOWN}
    cfg = find_culture_yaml()
    if cfg is None:
        return fields
    try:
        text = cfg.read_text(encoding="utf-8")
    except OSError:
        return fields
    seen_agent = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(("- suffix:", "suffix:")):
            if seen_agent:  # second agent block — stop at the first
                break
            seen_agent = True
            fields["nick"] = _scalar(stripped, "suffix")
        elif seen_agent and stripped.startswith("backend:"):
            fields["backend"] = _scalar(stripped, "backend")
        elif seen_agent and stripped.startswith("model:"):
            fields["model"] = _scalar(stripped, "model")
    return fields


def agent_nick() -> str | None:
    """This agent's declared nick, or ``None`` when there is no ``culture.yaml``.

    Distinct from ``read_agent_fields()["nick"]``, which always answers with
    :data:`FALLBACK_NICK` because ``events whoami`` must print *something*. A
    caller recording an owner identity needs to know the difference between "the
    agent declares itself ``events-cli``" and "there is no declaration here", so
    it can fall back to something that is actually unique to the caller — see
    :func:`events_cli.subs.record.resolve_owner`.
    """
    if find_culture_yaml() is None:
        return None
    nick = read_agent_fields()["nick"]
    return nick or None


def _scalar(line: str, key: str) -> str:
    """Extract the scalar after ``key:`` from a ``culture.yaml`` line."""
    _, _, value = line.partition(f"{key}:")
    return value.strip().strip("'\"") or _UNKNOWN

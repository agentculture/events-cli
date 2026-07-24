"""``events whoami`` — the smallest identity probe.

Reports the agent's identity as declared in ``culture.yaml``: its nick
(``suffix``), the backend it runs on, and the served model (if any) — plus the
package version. Read-only; touches nothing but its own ``culture.yaml``.

The nick is the mesh suffix (``events-cli``), deliberately distinct from the
console command (``events``). Identity is read from ``culture.yaml``, so it
tracks that file with no code change.

The hand-rolled ``culture.yaml`` line scanner this verb is built on lives in
:mod:`events_cli.core.identity` and is re-exported here, unchanged, under the
names it has always had. It moved when the subscription registry needed the
same answer for a subscription's default *owner*: one scanner, so the identity
``events whoami`` prints and the identity a subscription records can never
disagree. It is still hand-rolled rather than PyYAML, and
:mod:`events_cli.core` is still stdlib-only, so the introspection lane keeps
running from a bare checkout.
"""

from __future__ import annotations

import argparse

from events_cli import __version__
from events_cli.cli._output import emit_result
from events_cli.core.identity import find_culture_yaml, read_agent_fields

__all__ = ["cmd_whoami", "find_culture_yaml", "read_agent_fields", "register", "report"]


def report() -> dict[str, object]:
    fields = read_agent_fields()
    return {
        "nick": fields["nick"],
        "version": __version__,
        "backend": fields["backend"],
        "model": fields["model"],
    }


def cmd_whoami(args: argparse.Namespace) -> None:
    identity = report()
    json_mode = bool(getattr(args, "json", False))
    if json_mode:
        emit_result(identity, json_mode=True)
        return
    text = (
        f"nick: {identity['nick']}\n"
        f"version: {identity['version']}\n"
        f"backend: {identity['backend']}\n"
        f"model: {identity['model']}"
    )
    emit_result(text, json_mode=False)


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "whoami",
        help="Report this agent's nick, version, backend, and served model.",
    )
    p.add_argument("--json", action="store_true", help="Emit structured JSON.")
    p.set_defaults(func=cmd_whoami)

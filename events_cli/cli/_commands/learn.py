"""``events learn`` — the learnability affordance.

Prints a structured self-teaching prompt. Must satisfy the agent-first rubric:
>=200 chars and mention purpose, command map, exit codes, --json, and explain.
"""

from __future__ import annotations

import argparse

from events_cli import __version__
from events_cli.cli._output import emit_result

_TEXT = """\
events — the AgentCulture event fabric.

Purpose
-------
Runs and maintains a Dockerised Eclipse Mosquitto MQTT broker and fronts it as a
CLI, an HTTP API and an MCP surface, so any app can import it, any service can
call the API, and an agent or a human can publish and subscribe to events the
same way. Mosquitto transports events; this tool defines what they mean — typed
immutable envelopes, correlation and causation, durable history, pipeline runs.

Status: the event and broker surface is NOT implemented yet. What ships today is
the agent-first CLI below — identity and introspection. See the repository
issues for the specification being built against.

Commands
--------
  events whoami             Identity from culture.yaml.
  events learn              This self-teaching prompt.
  events explain <path>...  Markdown docs for any noun/verb path.
  events overview           Descriptive snapshot of the agent.
  events doctor             Check the agent-identity invariants.
  events cli overview       Describe the CLI surface itself.

Machine-readable output
-----------------------
Every command supports --json. Errors in JSON mode emit
{"code", "message", "remediation"} to stderr. Stdout and stderr never mix.

Exit-code policy
----------------
  0 success
  1 user-input error (bad flag, bad path, missing arg)
  2 environment / setup error
  3+ reserved

More detail
-----------
  events explain events
"""


def _as_json_payload() -> dict[str, object]:
    return {
        # Three distinct names, all machine-discoverable: the command an agent
        # invokes, the distribution it installs, and the module it imports.
        "tool": "events",
        "distribution": "events-cli",
        "import_package": "events_cli",
        "version": __version__,
        "purpose": (
            "The AgentCulture event fabric: a Dockerised Mosquitto MQTT broker fronted "
            "as a CLI, an HTTP API and an MCP surface. The event and broker surface is "
            "not implemented yet; today's verbs are identity and introspection only."
        ),
        "commands": [
            {"path": ["whoami"], "summary": "Identity probe from culture.yaml."},
            {"path": ["learn"], "summary": "Self-teaching prompt."},
            {"path": ["explain"], "summary": "Markdown docs by path."},
            {"path": ["overview"], "summary": "Descriptive snapshot of the agent."},
            {"path": ["doctor"], "summary": "Check the agent-identity invariants."},
            {"path": ["cli", "overview"], "summary": "Describe the CLI surface."},
        ],
        "exit_codes": {
            "0": "success",
            "1": "user-input error",
            "2": "environment/setup error",
        },
        "json_support": True,
        "explain_pointer": "events explain <path>",
    }


def cmd_learn(args: argparse.Namespace) -> int:
    if getattr(args, "json", False):
        emit_result(_as_json_payload(), json_mode=True)
    else:
        emit_result(_TEXT, json_mode=False)
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "learn",
        help="Print a structured self-teaching prompt for agent consumers.",
    )
    p.add_argument("--json", action="store_true", help="Emit structured JSON.")
    p.set_defaults(func=cmd_learn)

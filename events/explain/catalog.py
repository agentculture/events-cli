"""Markdown catalog for ``events explain <path>``.

Each entry is verbatim markdown. Keys are command-path tuples. The empty tuple,
``("events",)`` and ``("events-cli",)`` all resolve to the root entry — the
installed console script is ``events``, ``events-cli`` is the PyPI distribution
name, and the agent-first rubric's ``explain_self`` check invokes
``events explain events``. Keep every one of those keys.

Keep bodies self-contained: an agent reading one entry should get enough
context without chaining reads.
"""

from __future__ import annotations

_ROOT = """\
# events

The AgentCulture event fabric. Runs and maintains a Dockerised Eclipse Mosquitto
MQTT broker and fronts it as a CLI, an HTTP API and an MCP surface — so any app
can `import events`, any service can call the API, and an agent or a human can
publish and subscribe to events the same way.

Mosquitto transports events; `events` defines what they mean. Consumers depend
on the `events` contract — typed immutable envelopes, correlation and causation,
durable history, pipeline runs — not on Mosquitto-specific topic conventions.

The installed console command is `events`; `events-cli` is the distribution name
on PyPI and the repository name.

## Status

The event and broker surface is **not implemented yet**. What ships today is the
agent-first CLI below: identity and introspection. The specification being built
against lives in the repository's open issues.

## Verbs

- `events whoami` — identity probe from `culture.yaml`.
- `events learn` — structured self-teaching prompt.
- `events explain <path>` — markdown docs for any noun/verb.
- `events overview` — descriptive snapshot of the agent.
- `events doctor` — check the agent-identity invariants.
- `events cli overview` — describe the CLI surface.

## Exit-code policy

- `0` success
- `1` user-input error
- `2` environment / setup error
- `3+` reserved

## See also

- `events explain whoami`
- `events explain doctor`
"""

_WHOAMI = """\
# events whoami

Reports the agent's identity from `culture.yaml`: nick (`suffix`), backend,
served model, and the package version. Read-only.

The nick is the mesh suffix (`events-cli`), which is deliberately not the same
string as the console command (`events`).

## Usage

    events whoami
    events whoami --json
"""

_LEARN = """\
# events learn

Prints a structured self-teaching prompt covering purpose, command map,
exit-code policy, `--json` support, and the `explain` pointer.

## Usage

    events learn
    events learn --json
"""

_EXPLAIN = """\
# events explain <path>

Prints markdown documentation for any noun/verb path. Unlike `--help` (terse,
positional), `explain` is global and addressable by path.

## Usage

    events explain events
    events explain whoami
    events explain --json <path>
"""

_OVERVIEW = """\
# events overview

Read-only descriptive snapshot of the agent: identity (from `culture.yaml`), the
verb surface, and the sibling-pattern artifacts this repo carries. Accepts an
ignored `target` so a stray path never hard-fails.

## Usage

    events overview
    events overview --json
"""

_DOCTOR = """\
# events doctor

Checks the agent-identity invariants `steward doctor` verifies:
prompt-file-present and backend-consistency (`colleague` → `AGENTS.colleague.md`), plus a
skills-present check. Exits 1 when unhealthy.

## Usage

    events doctor
    events doctor --json
"""

_CLI = """\
# events cli

Noun group for CLI-surface introspection. `cli overview` describes the CLI
itself (distinct from the global `overview`, which describes the agent).

## Usage

    events cli overview
    events cli overview --json
"""


ENTRIES: dict[tuple[str, ...], str] = {
    (): _ROOT,
    # Both spellings are load-bearing: the rubric gate calls `explain events`,
    # and `explain events-cli` stays valid for callers using the dist name.
    ("events",): _ROOT,
    ("events-cli",): _ROOT,
    ("whoami",): _WHOAMI,
    ("learn",): _LEARN,
    ("explain",): _EXPLAIN,
    ("overview",): _OVERVIEW,
    ("doctor",): _DOCTOR,
    ("cli",): _CLI,
    ("cli", "overview"): _CLI,
}

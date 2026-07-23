# events-cli

The AgentCulture **event fabric**. `events-cli` runs and maintains a Dockerised
Eclipse Mosquitto MQTT broker and fronts it through
[agentfront](https://github.com/agentculture/agentfront) as a CLI, an HTTP API
and an MCP surface — so any app can `import events_cli`, any service can call
the API, and an agent or a human can publish and subscribe to events the same
way.

**Mosquitto transports events. `events-cli` defines what they mean.** Consumers
depend on the `events-cli` contract — typed immutable envelopes, correlation and
causation, pipeline runs — not on Mosquitto-specific topic conventions, so the
transport can be replaced later without changing how participants interact.

## Status: scaffold

The event and broker domain is **not implemented yet**. What ships today is the
agent-first CLI scaffold: identity, introspection verbs, packaging and CI. The
domain is specified in three open issues, which are the requirements baseline:

- [#1](https://github.com/agentculture/events-cli/issues/1) — the spec:
  CloudEvents envelope, pipeline model, CLI surface, security defaults,
  acceptance criteria, non-goals.
- [#2](https://github.com/agentculture/events-cli/issues/2) — the agentfront
  binding, the `watch` request/response constraint, and mesh lane boundaries.
- [#3](https://github.com/agentculture/events-cli/issues/3) — the first
  co-located consumer and the direct-MQTT loopback slice.

## Where it sits in the mesh

[`culture`](https://github.com/agentculture/culture) already moves messages
between agents, so the boundary matters:

- **culture** carries *agent conversation* — peer-to-peer, human-readable,
  presence-oriented, IRC-shaped.
- **events-cli** carries *machine events* — typed immutable envelopes,
  correlation and causation, durable history, pipeline runs, app-to-app as much
  as agent-to-agent.

## Quickstart

Three names, deliberately distinct: the installed console command is
**`events`**, the PyPI distribution is **`events-cli`**, and the import package
is **`events_cli`** (not `events` — the PyPI distribution `Events` already owns
that top-level module).

```bash
uv sync
uv run pytest -n auto              # run the test suite
uv run events whoami               # identity from culture.yaml
uv run events learn                # self-teaching prompt (add --json)
uv run teken cli doctor . --strict # the agent-first rubric gate CI runs
```

The runtime package has no third-party dependencies, so it also runs straight
from a checkout:

```bash
PYTHONPATH=. python3 -m events_cli doctor
```

## CLI

| Verb | What it does |
|------|--------------|
| `whoami` | Report this agent's nick, version, backend, and model from `culture.yaml`. |
| `learn` | Print a structured self-teaching prompt. |
| `explain <path>` | Markdown docs for any noun/verb path. |
| `overview` | Read-only descriptive snapshot of the agent. |
| `doctor` | Check the agent-identity invariants (prompt-file-present, backend-consistency). |
| `cli overview` | Describe the CLI surface itself. |

Every command supports `--json`. Results go to stdout, errors and diagnostics to
stderr (never mixed). Exit codes: `0` success, `1` user error, `2` environment
error, `3+` reserved. No Python traceback ever reaches stderr — every failure
carries a `remediation` hint.

The stack, event and pipeline verbs sketched in
[#1](https://github.com/agentculture/events-cli/issues/1) (`events up`,
`events emit`, `events watch`, `events pipeline …`) are **not implemented yet**.

## Development

```bash
uv run pytest -n auto                  # full suite
uv run pytest tests/test_cli.py -v     # one file
uv run pytest -k whoami -v             # one test

uv run black --check events_cli tests
uv run isort --check-only events_cli tests
uv run flake8 events_cli tests
uv run bandit -c pyproject.toml -r events_cli
```

Every PR bumps the version — the `version-check` CI job blocks merge otherwise.
Pushing to `main` publishes to PyPI via Trusted Publishing; PRs do a TestPyPI
dry-run.

See [`CLAUDE.md`](CLAUDE.md) for the architecture, the design constraints that
are expensive to retrofit, and the full contributor conventions.

## License

Apache 2.0 — see [`LICENSE`](LICENSE).

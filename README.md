# events-cli

The AgentCulture **event fabric**. `events-cli` runs and maintains a Dockerised
Eclipse Mosquitto MQTT broker and defines the contract carried over it — so any
app can `import events_cli`, a co-located producer can publish straight to the
broker port, and an agent or a human can drive the stack from the CLI.

**Mosquitto transports events. `events-cli` defines what they mean.** Consumers
depend on the `events-cli` contract — typed immutable envelopes, correlation and
causation, pipeline runs — not on Mosquitto-specific topic conventions, so the
transport can be replaced later without changing how participants interact.

## Status: first slice

Shipping today: the envelope core, the importable publish client, the loopback
Mosquitto stack verbs, and the agent-first CLI scaffold (identity, introspection,
packaging, CI). **Not built yet**, each tracked as its own arc — the
[agentfront](https://github.com/agentculture/agentfront)-derived MCP and HTTP
surfaces ([#6](https://github.com/agentculture/events-cli/issues/6)), durable
subscriptions with history and replay
([#7](https://github.com/agentculture/events-cli/issues/7)), pipelines
([#8](https://github.com/agentculture/events-cli/issues/8)), `shell-cli`
confinement for `events up`
([#9](https://github.com/agentculture/events-cli/issues/9)), and dynsec
identities with a documented remote opt-in
([#10](https://github.com/agentculture/events-cli/issues/10)).

The domain is specified in three issues, which are the requirements baseline:

- [#1](https://github.com/agentculture/events-cli/issues/1) — the spec:
  CloudEvents envelope, pipeline model, CLI surface, security defaults,
  acceptance criteria, non-goals.
- [#2](https://github.com/agentculture/events-cli/issues/2) — the agentfront
  binding, the `watch` request/response constraint, and mesh lane boundaries.
- [#3](https://github.com/agentculture/events-cli/issues/3) — the first
  co-located consumer and the direct-MQTT loopback slice.

## Where it sits in the mesh

[`culture`](https://github.com/agentculture/culture) carries **agent
conversation** — peer-to-peer, human-readable, presence-oriented, IRC-shaped —
and `events-cli` carries **machine events** — typed immutable envelopes,
correlation and causation, durable history, pipeline runs, app-to-app as much as
agent-to-agent ([#2](https://github.com/agentculture/events-cli/issues/2)).

A second messaging substrate in the same ecosystem only works if the boundary is
stated, so it is: no events-cli surface accepts or relays agent conversation. If
a payload's audience is a human reading it as prose, it belongs on the mesh.

## Contract

The full contract lives in [`docs/contract.md`](docs/contract.md). Three parts of
it are obligations on *consumers*, and are cheaper to read here than to discover
in production:

- **The raw MQTT port is first-class.** A co-located, latency-sensitive producer
  connects straight to `127.0.0.1:1883` — `reachy-mini-cli` publishes from a
  50 Hz control loop where a publish must be an O(1) in-process enqueue and
  cannot cross a CLI, HTTP or MCP boundary. Direct broker access is a supported,
  documented surface; the contract and pipeline layer is an optional value-add,
  **not a mandatory gateway**, and producer-owned topic trees stay legal.
- **Delivery is at-least-once, so consumers must dedupe on the event `id`.**
  QoS 1 redelivers, exactly-once is an explicit non-goal, and duplicates are
  normal operation rather than an error. Idempotency keyed on the envelope `id`
  is a **requirement** the contract places on every consumer, not an
  optimisation.
- **Retained messages are the last value, not history.** A retained message
  hands a new subscriber the current value of one topic. It is not a replayable
  log — no window, no ordering across topics, no evidence that intermediate
  values existed. Durable history and replay are a separate, not-yet-built lane
  ([#7](https://github.com/agentculture/events-cli/issues/7)).

The first consumer is real, not aspirational:
[`reachy-mini-cli`](https://github.com/agentculture/reachy-mini-cli)'s converged
spec ships no broker and no MQTT library of its own, and its broker-dependent
acceptance items wait on this slice — recorded in that repo's
`docs/specs/2026-07-23-reachy-nervous-system.md` and `docs/export-schema.md`, and
from this side on [#3](https://github.com/agentculture/events-cli/issues/3).

## Quickstart

Three names, deliberately distinct: the installed console command is
**`events`**, the PyPI distribution is **`events-cli`**, and the import package
is **`events_cli`** (not `events` — the PyPI distribution `Events` already owns
that top-level module).

```bash
uv sync
uv run pytest -n auto                   # run the test suite
uv run events whoami                    # identity from culture.yaml
uv run events learn                     # self-teaching prompt (add --json)
uv run agentfront cli doctor . --strict # the agent-first rubric gate CI runs
```

The MQTT client is imported lazily, so the introspection verbs still run from a
bare checkout with nothing installed:

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
| `init` / `up` / `status` / `logs` / `down` | Manage the loopback-only Mosquitto stack. |

Every command supports `--json`. Results go to stdout, errors and diagnostics to
stderr (never mixed). Exit codes: `0` success, `1` user error, `2` environment
error, `3+` reserved. No Python traceback ever reaches stderr — every failure
carries a `remediation` hint.

The event and pipeline verbs sketched in
[#1](https://github.com/agentculture/events-cli/issues/1) (`events emit`,
`events watch`, `events pipeline …`) are **not implemented yet** — see
[`docs/contract.md`](docs/contract.md) for what is built and what each deferred
lane is tracked as.

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

See [`docs/contract.md`](docs/contract.md) for the consumer-facing contract, and
[`CLAUDE.md`](CLAUDE.md) for the architecture, the design constraints that are
expensive to retrofit, and the full contributor conventions.

## License

Apache 2.0 — see [`LICENSE`](LICENSE).

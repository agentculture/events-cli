# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

`events-cli` is the AgentCulture **event fabric**: an agent and CLI that runs and
maintains a Dockerised Eclipse Mosquitto MQTT broker, so an app can
`import events_cli` and an agent or a human can publish and subscribe to events
the same way.

**Today that means two surfaces — the CLI and the import package.** Fronting
the broker through [`agentfront`](https://github.com/agentculture/agentfront) to
*also* derive an HTTP API and an MCP surface is the project's intent and the
shape the contract is designed for, but it is **not built** — it is a deferred
arc, tracked as [#6](https://github.com/agentculture/events-cli/issues/6). Read
the intended end state under [Roadmap](#roadmap) and
[Design constraints](#design-constraints-decide-these-before-the-data-plane);
it is deliberately not described here as something that exists.

**Design principle (from [#1](https://github.com/agentculture/events-cli/issues/1)):**
*Mosquitto transports events. `events-cli` defines what they mean.* Consumers
depend on the `events-cli` contract — typed envelopes, correlation/causation,
pipeline runs — not on Mosquitto-specific topic conventions, so the transport
can be replaced later without changing how participants interact.

### Read this before you build anything

**The first vertical slice is built; the data plane's later arcs are not.** What
began as the agent-first CLI scaffold inherited from `culture-agent-template`
(identity, introspection verbs, CI, packaging) now also carries the event
fabric's first slice:

- a pure **CloudEvents envelope core** (`events_cli/core/`) — stdlib only, no
  broker and no Docker;
- an **importable publish client** (`events_cli/client.py`, `EventClient`) on
  `paho-mqtt`, now a base dependency imported lazily; and
- the **stack verbs** (`events init/up/status/logs/down`) that generate and run a
  loopback-only Dockerised Mosquitto.

What is *not* built: the agentfront MCP/HTTP binding, durable subscriptions +
history, and pipelines. Those are deferred arcs tracked in
[#6–#10](https://github.com/agentculture/events-cli/issues) — see the
[Roadmap](#roadmap). **Issue #1's pipeline acceptance criteria are explicitly not
met by this slice.**

The domain is specified across three open issues, and they are the requirements
baseline. Read them before starting work; they are not summarised anywhere else
in the repo:

| Issue | What it settles |
|-------|-----------------|
| [#1](https://github.com/agentculture/events-cli/issues/1) | The spec: CloudEvents envelope, pipeline model, CLI verb surface, security defaults, acceptance criteria, non-goals. |
| [#2](https://github.com/agentculture/events-cli/issues/2) | The agentfront binding, the `watch` request/response constraint, mesh lane boundaries, Mosquitto specifics, repo-shape decisions. |
| [#3](https://github.com/agentculture/events-cli/issues/3) | First co-located consumer (`reachy-mini-cli`): direct-MQTT loopback slice on `spark-f8a9`, and the nova-stack migration. |

Keep this file describing the repository **as it exists on disk today**. When a
section describes something not yet built, it lives under
[Design constraints](#design-constraints-decide-these-before-the-data-plane) or
[Roadmap](#roadmap) and says so — do not let prose drift ahead of code.

## Identity

Declared in `culture.yaml`:

```yaml
agents:
- suffix: events-cli
  backend: colleague
  model: sakamakismile/Qwen3.6-27B-Text-NVFP4-MTP
```

`backend: colleague` fixes the resident prompt file to **`AGENTS.colleague.md`**
— the mesh runtime reads that file, while `CLAUDE.md` (this file) is the Claude
Code guidance file and is *not* the backend's prompt. Together they satisfy the
two invariants `steward doctor` verifies: **prompt-file-present** and
**backend-consistency** (`colleague` ↔ `AGENTS.colleague.md`). `events doctor`
checks the same invariants locally, plus a skills-present check.

(The pre-`/init` seed of this file claimed `backend: claude`. That was template
drift; `colleague` is correct and is what `culture.yaml`, `doctor`, and the test
suite assert.)

## The CLI

**Three names, deliberately distinct — do not collapse them:**

| Name | What it is |
|------|------------|
| `events` | the installed console command (`[project.scripts]` in `pyproject.toml`) |
| `events-cli` | the PyPI distribution, the repo name, and the mesh nick |
| `events_cli` | the import package (`import events_cli`) |

The import package is **not** `events`: the PyPI distribution
[`Events`](https://pypi.org/project/Events/) already owns that top-level module,
so shipping it here would silently clobber one of the two in any environment
holding both. The collision was vacated in 0.8.0, before the first consumer
bound to the import name.

```bash
# introspection verbs (no third-party imports — run from a bare checkout)
events whoami            # identity from culture.yaml
events learn             # structured self-teaching prompt
events explain <path>    # markdown docs for any noun/verb path
events overview          # descriptive snapshot of the agent
events doctor            # check the agent-identity invariants
events cli overview      # describe the CLI surface itself

# stack verbs (operate the Dockerised Mosquitto broker)
events init              # generate the loopback-only broker stack
events up                # start the broker (refuses a foreign broker on the port)
events status            # broker state + health (exits 1 when unhealthy)
events logs              # tail the broker log (bounded --tail, no --follow)
events down              # stop and remove the broker containers
```

Conventions, enforced by the agent-first rubric:

- Every command supports `--json`.
- **Results to stdout, errors and diagnostics to stderr — never mixed**
  (`events_cli/cli/_output.py`).
- Exit codes: `0` success, `1` user error, `2` environment error, `3+` reserved
  (`events_cli/cli/_errors.py`).
- No Python traceback ever reaches stderr; every failure is a `CliError` with a
  `remediation` hint, and even argparse parse errors route through the same
  `error:` / `hint:` format (`_CliArgumentParser` in
  `events_cli/cli/__init__.py`).

CI enforces the rubric with `uv run agentfront cli doctor . --strict`.

### How the CLI fits together

Reading these four files gives you the whole contract; the rest is content.

- `events_cli/cli/__init__.py` — the only parser. `_build_parser()` imports each
  command module lazily and calls its `register(sub)`. `_dispatch()` is the
  single place exceptions become exit codes. **Register a new noun group by
  adding one `register()` call here** — there is a marked comment at the
  insertion point.
- `events_cli/cli/_commands/<verb>.py` — one module per verb/noun. Each exposes
  `register(sub)` (adds the subparser, `--json`, and `set_defaults(func=...)`)
  and a `cmd_*` handler returning `None`/`int`. Handlers raise `CliError`; they
  never print errors themselves.
- `events_cli/cli/_output.py` / `_errors.py` — the stdout/stderr split and the
  exit-code policy. Both are marked *stable-contract*: changing them changes the
  agent-facing contract, so treat edits as breaking.
- `events_cli/explain/catalog.py` — markdown keyed by command-path tuple. Every
  noun/verb you register needs an entry, and `tests/test_cli.py`'s
  `test_every_catalog_path_resolves` walks all of them.

**Load-bearing detail:** the catalog maps *both* `("events",)` and
`("events-cli",)` to the root entry. The rubric gate's `explain_self` check
invokes `events explain events`; the dist-name key keeps `events explain
events-cli` working for callers that know the repo by that name. **Do not remove
either key** — dropping `("events",)` fails CI.
`test_explain_root_keys_both_resolve_to_same_entry` pins both. These keys are
*command-path* names, not module names: the import package rename to
`events_cli` deliberately did not touch them, because nobody types `events_cli`.

**Command name:** hints tell an agent what to run *next*, so they must name a
command that exists in the mode the caller is already using.
`events_cli/cli/_prog.py` resolves it — `events` when installed, `python -m
events_cli` under the no-install fallback — and feeds both argparse's `prog` and
the `explain` unknown-path remediation. It compares `sys.argv[0]` to *this*
package's `__main__.py` by full path; a basename check matches every `python -m`
host, `python -m pytest` included. `test_prog_matches_installed_console_script`
reads `[project.scripts]` from `pyproject.toml` to keep the console-script name
and `prog` in lockstep, so renaming either side fails there rather than shipping.
The three-way name split itself is pinned by
`test_packaging_config_points_at_events_cli` and
`test_no_top_level_events_package_in_the_source_tree`.

`whoami` parses `culture.yaml` with a hand-rolled line scanner
(`events_cli/cli/_commands/whoami.py`) rather than PyYAML, deliberately: the
**introspection lane imports nothing third-party**, so it runs from a bare
checkout even though `paho-mqtt` is now a base dependency (that import is
confined to the client — see
[The event core and the client](#the-event-core-and-the-client)). It locates the
file by walking up from `__file__`, so identity is always *this agent's*, never
whatever `culture.yaml` sits in the caller's CWD; a wheel install finds none and
falls back to literal defaults (which is why `doctor` reports a single info check
and exits 0 there).

## The event core and the client

The first slice added two runtime layers below the CLI. Both are pure or lazy
enough that the introspection lane still imports nothing third-party.

**The envelope core** (`events_cli/core/`) is the bottom layer and the stable
public contract: an immutable, CloudEvents-compatible `Envelope`
(`id`/`type`/`source`/`time`/`schemaVersion`/`data` plus the tracing fields
`correlationId`, `causationId`, `runId`, `traceparent`, `producer`,
`deliveryAttempt`). It is **standard library only** — no transport client, no
Docker, no I/O — so the unit suite that gates code quality runs on a machine
with nothing installed. `Envelope.from_dict` / `from_json` is the trust boundary
and validates in one pass, reporting **every** problem as a field-level
`FieldError` (field, code, message). It is deliberately **stricter than
CloudEvents**: unknown top-level keys are rejected (put producer values inside
`data`), `schemaVersion` must be numeric on the wire, `type` must be
lowercase-dotted (`task.requested` — wrong case is rejected, not normalised), and
`source` must be an absolute URI (`agent://builder`). Events are facts, so the
dataclass is frozen; delivery is at-least-once (QoS 1), so consumers **must**
dedupe on `id`.

**The importable client** (`events_cli/client.py`, `EventClient`) is the
producer lane the `reachy-mini-cli` 50 Hz control loop binds to (#3).
`publish()` is an **O(1) enqueue on the caller's thread** — paho's background
loop owns every socket operation — and it **never raises into the caller**: a
broker-down publish returns a `PublishResult(ok=False)`, connection state is
observable via `state` / `is_connected`, and retained messages, Last Will and
QoS 0 are all supported. A per-process-unique default client id avoids MQTT
session-takeover between co-located producers.

**The lazy-import boundary.** `paho-mqtt` is a **base dependency** (`pip install
events-cli` pulls it), but it is imported **only** inside `client.py`, and only
when a client is actually constructed (`_load_paho`) — never from
`events_cli/__init__.py` or anywhere under `events_cli/cli/`. That is what keeps
the introspection verbs (`whoami`, `doctor`, `learn`, `explain`) working from a
checkout with nothing installed, preserving the `PYTHONPATH=. python3 -m
events_cli` fallback. The old "runtime package has zero third-party
dependencies" claim is therefore **retired**: the runtime now carries paho, but
the introspection lane stays dependency-free by design. A missing paho surfaces
as a named `MqttDependencyError`, not an opaque `ImportError`.

## The stack verbs

`events init/up/status/logs/down` (`events_cli/cli/_commands/stack.py`, backed by
`events_cli/stack/`) generate and operate the broker. They are registered at the
top level — `events up`, not `events stack up` — because that is the surface the
contract names. The CLI module is only a translation layer: `events_cli/stack/`
owns the templates, the `docker compose` argv, the preflight and the status
parsing, raises `StackError`, and imports nothing from `events_cli.cli`; the
command module turns that into `CliError` exit codes. Every docker invocation
funnels through one `events_cli.stack._docker.run` seam, so the unit suite drives
every path with **no docker on the machine**.

`events init` writes two **verbatim** templates (no substitution step, so a
reviewer reads the literal file): `compose.yaml` and `mosquitto.conf`. What they
guarantee:

- **Loopback-only by construction**: the published mapping is the literal
  `127.0.0.1:1883:1883`, never a bare `1883:1883`. Docker's DNAT bypasses the
  host firewall, so the host-address prefix is the only thing keeping the broker
  off the LAN; remote access is a documented opt-in that edits `compose.yaml`.
- **An exact image pin**: `eclipse-mosquitto:2.1.2-alpine`, never the floating
  `eclipse-mosquitto:2`. This matters more than it looks (**deviation d1**): the
  `:2` tag now resolves to **2.1.2, not 2.0.x**, and 2.1.2 opens an
  **unauthenticated dashboard/http-api listener on 9883** by default — the
  generated `mosquitto.conf` declares an explicit `listener 1883`, which replaces
  the default listener set wholesale, so no 9883 (and no 9001 websocket) socket
  is opened and none is published. A unit test fails if the template ever carries
  a bare floating major tag.
  **The `-alpine` suffix is load-bearing** (**deviation d2**): upstream publishes
  the 2.1 line *only* in that form, so the suffix-free `eclipse-mosquitto:2.1.2`
  — which d1 originally pinned, reading the version off the local image's OCI
  label — is a tag that has never existed. It pulls with `no such manifest`,
  which no dockerless test and no cache-warm host can catch; `2.1.2-alpine` and
  the floating `:2` share a manifest digest today, so the pin names exactly what
  `:2` serves, frozen. `test_the_pinned_image_tag_is_actually_pullable` (stack
  marker) asks the *registry*, not the local image store, and is the standing
  guard.
- **Persistence that actually persists**: `persistence true` **plus** the mounted
  `events-data` named volume (the setting alone writes into the container layer
  and vanishes on `down`), with `autosave_interval` set explicitly so the
  unclean-stop loss bound is a documented number.
- **A foreign-broker preflight**: `events up` refuses to double-bind port 1883 if
  another broker (e.g. `nova-mosquitto`) already holds it, printing the exact stop
  command rather than fighting it.

`up`/`logs`/`down` carry **non-infinite `--timeout` / `--tail` bounds** by policy;
there is deliberately no `--follow` on `logs`, because an unbounded stream hangs
an agent turn.

## Design constraints (decide these before the data plane)

These come out of #1, #2 and #3. Several are now realised by the first slice —
the raw MQTT port by [the client](#the-event-core-and-the-client), loopback
binding and the Mosquitto facts by [the stack verbs](#the-stack-verbs), and the
dependency-posture question by the paho base-dep decision below. The rest (one
registry / four surfaces, `watch`) are still decisions, not code, and are the
expensive-to-retrofit constraints the deferred arcs (#6, #7) must honour.

**One registry, four surfaces.** agentfront is an importable runtime, not a
scaffolder: declare docs and tools once on an `App`, and CLI, MCP and HTTP are
*derived* from that registry, so they cannot drift apart. agentfront derives
three surfaces; the request names four. The fourth — `import events_cli` — is not
agentfront's and is yours to keep honest. *The core module now exists*
(`events_cli/core/`), so the remaining work in **#6** is to populate the registry
**from that core**: if it is filled by separate adapter functions, the import
lane becomes a fourth thing that drifts, which is exactly what agentfront
otherwise removes. *Decided (q2):* the hand-rolled argparse CLI **stays** the CLI
surface; agentfront derives **MCP + HTTP only**, with CLI/registry parity pinned
by our own tests.

**`watch` does not fit request/response.** A long-lived MQTT subscription blocks
an MCP tool call and an HTTP request; the CLI is the only surface that can stream
forever. So `watch` cannot be exposed verbatim everywhere. The shape that works
identically on all surfaces: **durable server-side subscriptions** (named,
persistent, owned by an identity) that the control service drains from — `events
watch --since <cursor> --max N --timeout 30s` returning a bounded batch plus a
cursor. **Every agent-facing tool needs `--max` and `--timeout` with non-infinite
defaults**; an unbounded default eventually hangs an agent turn. Streaming stays
HTTP-only (SSE, or pass through Mosquitto's native WebSocket listener). This is
the same machinery as #1's "history survives a stack restart" — design them
together, not a cursor bolted onto a streaming-only `watch` later.

**The raw MQTT port stays first-class.** `reachy-mini-cli` publishes from inside
a 50 Hz robot control loop where a publish must be an O(1) in-process enqueue; it
connects **directly to TCP 1883** and cannot go through the CLI/HTTP/MCP front.
*Now served by* [`EventClient`](#the-event-core-and-the-client), which wraps
`paho-mqtt >=2,<3` so that consumer binds one importable seam instead of paho
itself. Direct broker access is a supported, documented surface
for co-located latency-sensitive producers, with the contract/pipeline layer as
an **optional value-add, not a mandatory gateway**. If a validation layer later
polices topic shapes, leave room for producer-owned topic trees
(`reachy/events/{source}/{type}`, retained `reachy/state/{key}`) that don't
participate in pipelines.

**Loopback binding is explicit, not implied.** The Compose port mapping must be
`127.0.0.1:1883:1883`. Docker publishes on `0.0.0.0` by default and its NAT
bypasses host firewalls, so a bare `1883:1883` is LAN-exposed regardless of
`ufw`. Remote access is an explicit, documented opt-in.

**Mosquitto facts worth knowing before writing `events init`:**

- Mosquitto 2.0 already defaults the way #1 wants — no listener means
  localhost-only, and `allow_anonymous` defaults to `false`. Several security
  requirements are "don't override the upstream default," not a hardening layer.
  Verify against the version you pin and say so in the generated
  `mosquitto.conf`. **The pin is now 2.1.2, and 2.1 is not 2.0 here**: it opens
  an unauthenticated 9883 dashboard by default, which the generated config
  suppresses — see [The stack verbs](#the-stack-verbs) (deviation d1). Re-verify
  every "upstream default" claim before moving the pin.
- The **dynamic security plugin** (`mosquitto_ctrl dynsec`, 2.0+) is the right
  answer for distinct identities + topic-level ACLs — runtime JSON config, no
  broker reload per change, which is what an agent-facing CLI actually needs.
- **MQTT 5 shared subscriptions** (`$share/<group>/<topic>`) fan one event type
  across competing consumers — relevant to pipeline stages with several workers.
- **QoS 1 is at-least-once**, matching #1's explicit non-goal of exactly-once.
  Consumer-side idempotency keyed on event ID is therefore a *requirement* of
  the contract, not an optimisation — state it so consumers know they must dedupe.
- **Retained messages are not history.** A retained message gives a new
  subscriber the last value on a topic; it is not a replayable log. History and
  survive-a-restart need the control service's own store.
- **Persistence needs `persistence true` *plus* a mounted volume** — easy to
  omit, and only discovered on the first restart test.

**Docker must not be a unit-test dependency.** CI runs pytest with SonarCloud
coverage on every PR (`sonar.qualitygate.wait=true` blocks the merge). Keep
envelope validation and pipeline-transition logic pure and dockerless — they are
the parts worth high coverage anyway. *Now enforced:* two pytest markers, both
**excluded from the default selection** via `addopts = "-ra -m 'not perf and not
stack'"` — `stack` (needs a live broker or docker) and `perf` (the only
wall-clock assertion, the enqueue-latency bound). CI runs the default selection,
so neither a missing broker nor a loaded runner can make the quality gate flaky;
run them explicitly with `pytest -m stack` / `pytest -m perf`.

**Dependency posture.** *Decided (q1):* `paho-mqtt >=2,<3` is a **base
dependency** — `pip install events-cli` pulls it. The `dependencies = []` posture
is deliberately given up; what preserves the no-install introspection lane is the
lazy-import boundary, not an empty dependency list (see
[The event core and the client](#the-event-core-and-the-client)). agentfront's
own convention (CLI + HTTP dependency-free, MCP behind `agentfront[mcp]`) still
applies to the deferred binding (#6): the MCP surface sits behind an extra even
though the transport client does not.

**Do not build a store from scratch.** `eidetic-cli` (memory/recall) and
`data-refinery-cli` (data quality) already own that lane; evaluate them before a
bespoke history backend — still open, and a precondition of #7. *Resolved for
now:* `events up` shells out to `docker` via stdlib `subprocess`, because
`shell-cli` (which owns shell confinement and the approval policy) is
scaffold-only today — no operation, policy or runner has been built. Routing
through it is **deferred and tracked as #9**, not silently skipped; this repo
should still not grow a second shell-confinement posture.

### Mesh lane boundary

[`culture`](https://github.com/agentculture/culture) is the IRC-based agent mesh
and already moves messages between agents. `events-cli` is a **second messaging
substrate in the same ecosystem**, which is defensible but must be stated or
contributors will guess:

- **culture** carries *agent conversation* — peer-to-peer, human-readable,
  presence-oriented, IRC-shaped.
- **events-cli** carries *machine events* — typed immutable envelopes,
  correlation and causation, durable history, pipeline runs, app-to-app as much
  as agent-to-agent.

[`devague`](https://github.com/agentculture/devague) is the likely first real
consumer: #1's motivating flow (`task.requested → scope.completed →
specification.ready → implementation.completed → validation.completed`) is almost
exactly the devague lifecycle, and `/deviate` exists precisely because mid-run
divergence has no transport today. Test the design against it — if events-cli is
the transport under devague, its stage names are the first real event-type
vocabulary and a far better validation than a synthetic example. If not, say why,
because the resemblance will otherwise be assumed.

## Development

```bash
uv sync
uv run pytest -n auto                  # default selection (260 tests today)
uv run pytest tests/test_cli.py -v     # one file
uv run pytest -k whoami -v             # one test / pattern
uv run pytest -n auto --cov=events_cli --cov-report=term   # with coverage
uv run pytest -m stack                 # broker/docker integration (needs a stack)
uv run pytest -m perf                  # the enqueue-latency bound
```

The default selection **excludes the `perf` and `stack` markers** (`addopts` in
`pyproject.toml`), so it passes on a machine with no docker and no broker — that
is exactly what keeps the coverage/Sonar gate independent of a live stack and
free of wall-clock flake. Select those suites explicitly with `-m`.

Lint — all six run in CI and must pass:

```bash
uv run black --check events_cli tests      # line length 100
uv run isort --check-only events_cli tests # profile=black
uv run flake8 events_cli tests
uv run bandit -c pyproject.toml -r events_cli
markdownlint-cli2 "**/*.md" "#node_modules" "#.local" "#.claude/skills" "#.agentfront"
uv run agentfront cli doctor . --strict    # the agent-first rubric gate
```

Coverage floor is 60% (`[tool.coverage.report] fail_under`), and SonarCloud
gates the PR on top of that (`sonar-project.properties`,
`sonar.qualitygate.wait=true`). The Sonar step is skipped when `SONAR_TOKEN` is
absent, so fork PRs stay green.

The **introspection verbs** import nothing third-party — `paho-mqtt` is a base
dependency but is imported lazily, only inside the client — so they still run
straight from a checkout without `uv sync`, useful when the network is
unavailable:

```bash
PYTHONPATH=. python3 -m events_cli doctor
PYTHONPATH=. python3 -m pytest tests -q
```

A CI test exercises those verbs from a tree where paho is **not** installed, so
the no-install lane is proven per PR rather than assumed. Anything that actually
constructs an `EventClient` does need paho present.

## Conventions

- **Every PR bumps the version** — even docs/config/CI-only PRs. Use the
  `version-bump` skill; the `version-check` CI job comments on and blocks the PR
  otherwise. This repo published to production PyPI on its genesis push, so the
  Trusted Publisher is already registered and a push to `main` touching
  `pyproject.toml` or `events_cli/**` publishes for real.
- **PRs go through the `cicd` skill** (`devex pr` + SonarCloud gating). Sign
  online posts as `- events-cli (Claude)`; the `cicd` / `communicate` scripts
  resolve the nick from `culture.yaml` automatically, so don't hand-sign in
  bodies they author.
- **Reach for `ask-colleague` reflexively.** Treat it as the teammate at the next
  desk, not a last resort — its value is a *second, independent mind* (a
  different backend/model), not a stronger one. Before presenting or opening a PR
  on a non-trivial committed diff, run `review`; for a fresh read of an
  unfamiliar area, run `explore`. Both are read-only (throwaway worktree, zero
  side effects), so the reflex is always safe. The side-effecting `write
  --apply` / `write --pr` still needs the user's go-ahead. Its output is a second
  opinion to verify and own, never authority.
- **The vendored `.claude/skills/` are cited verbatim** — do not reformat or edit
  their scripts. Re-sync from guildmaster (or the tracked direct-from-origin
  exceptions) per `docs/skill-sources.md`.
- **Deploy**: pushing to `main` publishes to PyPI via Trusted Publishing
  (`.github/workflows/publish.yml`); PRs from this repo do a TestPyPI dry-run
  with a `.devN` suffix.

### Worktrees

**Every worktree you create by hand lives in
`../.worktrees.events-cli/<name>/`** — one repo-named directory beside the
checkout, one subfolder per worktree:

```bash
git worktree add ../.worktrees.events-cli/<name> -b <branch>
```

Not a shared `../worktrees/`. This workspace holds many sibling projects, and a
generic shared folder accumulates orphaned trees from several repos with nothing
indicating who owns which — a stale-tree sweep cannot tell a live lane from junk.
Use a branch prefix scoped to the work (`broker/t2`, not `agent/t2`): plain
`agent/*` collides with leftovers from earlier fan-outs and `git worktree add -b`
fails on an existing branch.

The vendored `assign-to-workforce` skill's fan-out example uses *both* the shared
path and `agent/<task-id>` branches. It is cited verbatim and must not be edited,
so override both when following it.

Remove a worktree with `git worktree remove <path>`, which deletes the directory
and its bookkeeping together; `git worktree prune` only clears metadata for
directories that are already gone. Never `rm -rf` a worktree you did not create.
Exception: `ask-colleague`'s read-only verbs create their own detached worktree
under `${TMPDIR:-/tmp}` and reap it on an EXIT trap — tool-managed throwaways
never persist, so they are outside this rule.

### Memory discipline — recall before, remember after

This repo keeps its eidetic memory **in-repo and public**: a plain `/remember`
lands in `<repo-root>/.eidetic/memory` — committed, and shared with the team and
mesh peers (the `claude` and `colleague` backends both resolve the `events-cli`
scope), so memory travels with the repo rather than a private home-dir store.
The vendored `remember.sh` applies this as a deliberate policy override of
eidetic's upstream private default.

- **`/recall` before you start** a non-trivial task — prior decisions, gotchas,
  "have we done this before?" — so you build on what's known instead of
  re-deriving it. Not just when asked.
- **`/remember` when something worth keeping surfaces** — a non-obvious decision
  and its rationale, a constraint, a fix and *why*, a gotcha that cost time.
  Capture it as it happens, not at the end.

Keep something out of the committed store with `--visibility private` (routes to
`$HOME/.eidetic/memory`); `/recall` reads both and merges. Don't store what the
repo already records — code structure, git history, this file, `CHANGELOG.md`.
Store what you'd otherwise re-derive.

## Skills

`.claude/skills/` vendors the canonical **guildmaster** skill kit
(cite-don't-import); provenance and the re-sync procedure live in
`docs/skill-sources.md`. Seven skills originate in `devague` and one
(`ask-colleague`) in `colleague`; four devague skills and `ask-colleague` are
vendored **directly from their origin** as tracked divergences, because
guildmaster's re-broadcast copies differ. Every vendored `SKILL.md` must carry
`type: command` — `core.skill_loader` silently skips any that lacks it, even
where guildmaster's upstream copy omits the field.

Tooling prerequisites: **`devex`** (>=0.21) and **`agtag`** (>=0.1) on PATH;
**`colleague`** on PATH is optional and only needed when `ask-colleague` is
invoked. Copy `.claude/skills.local.yaml.example` to `skills.local.yaml`
(git-ignored) for per-machine sibling paths.

## Layout

```text
events_cli/               agent-first CLI (cited from agentfront's python-cli reference)
  cli/__init__.py         the single parser + dispatch/exit-code translation
  cli/_output.py          stdout/stderr split          (stable-contract)
  cli/_errors.py          CliError + exit-code policy  (stable-contract)
  cli/_commands/          one module per verb; each exposes register(sub)
    stack.py              init/up/status/logs/down -> StackError to CliError
  explain/catalog.py      markdown keyed by command-path tuple
  core/                   the envelope contract — stdlib only, no broker, no docker
    envelope.py           immutable CloudEvents-compatible Envelope + validation
    errors.py             EventsError / EnvelopeValidationError / FieldError
  client.py               EventClient — importable publisher (lazy paho import)
  stack/                  the Dockerised Mosquitto deployment
    templates/            compose.yaml + mosquitto.conf, shipped verbatim
    _docker.py            the single `docker compose` invocation seam
    _preflight.py         foreign-broker refusal on port 1883
    _status.py            `compose ps` parsing -> health / loopback verdict
tests/                    pytest suite; `perf` and `stack` markers excluded by default
.claude/skills/           vendored guildmaster skill kit (cite-don't-import)
docs/contract.md          the consumer-facing event contract
docs/skill-sources.md     skill provenance ledger
docs/specs/, docs/plans/  the devague spec and plan for the first slice
culture.yaml              mesh identity (suffix + backend)
.github/workflows/        tests.yml (test/lint/version-check), publish.yml (PyPI)
```

## Roadmap

**Shipped in the first slice** (the #3 unblock):

1. **Core event semantics** — envelope + validation, pure and dockerless
   (`events_cli/core/`).
2. **The importable client** — `EventClient`, O(1) enqueue, retained / LWT /
   QoS 0, on `paho-mqtt` as a base dependency (`events_cli/client.py`).
3. **Stack management** — `events init/up/status/logs/down`, Compose pinned to
   `eclipse-mosquitto:2.1.2-alpine` with `127.0.0.1:1883:1883`, `persistence true` +
   volume, and a foreign-broker preflight (`events_cli/stack/`).

**Deferred, each tracked as its own arc.** These carry a confirmed spec contract
recorded on the issue itself, so the requirement outlives the frame state:

| Issue | Arc |
|-------|-----|
| [#6](https://github.com/agentculture/events-cli/issues/6) | **agentfront binding** — one App registry fed from the core, deriving MCP (behind `[mcp]`) + HTTP, with `assert_surfaces_agree` and CLI parity tests. |
| [#7](https://github.com/agentculture/events-cli/issues/7) | **Durable subscriptions + cursor drain**, designed together with history; non-infinite `--max`/`--timeout` everywhere. |
| [#8](https://github.com/agentculture/events-cli/issues/8) | **Pipelines** — apply/list/show/run/inspect over the event graph. |
| [#9](https://github.com/agentculture/events-cli/issues/9) | **`shell-cli` routing** for `events up`, once shell-cli is more than scaffold. |
| [#10](https://github.com/agentculture/events-cli/issues/10) | **dynsec identities + topic ACLs**, and the documented remote-access opt-in. |

**Issue #1's pipeline acceptance criteria are explicitly *not* met by this
slice** — that is a deliberate, recorded scope decision (q4), not an oversight.

## Known drift

Real, checked-in inconsistency. Fixing it is a normal PR (bump the version).

**None outstanding today.** The one entry this section used to carry —
**`teken` → `agentfront`** — is **resolved**: the dev dependency is now
`agentfront>=0.20`, `uv.lock` resolves it, CI runs
`uv run agentfront cli doctor . --strict`, and the stale references in
`docs/skill-sources.md`, `.claude/skills.local.yaml.example` and
`.markdownlint-cli2.yaml` (now `.agentfront/**`) were swept with it. No live
config, CI step or dependency names the old package any more; the only surviving
mentions are historical — `CHANGELOG.md`, the archived spec/plan under `docs/`,
and this paragraph.

Add an entry here the moment prose and code diverge again — an empty section is a
claim, so do not leave a stale warning standing in place of one.

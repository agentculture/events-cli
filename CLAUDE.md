# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

`events-cli` is the AgentCulture **event fabric**: an agent and CLI that runs and
maintains a Dockerised Eclipse Mosquitto MQTT broker and fronts it through
[`agentfront`](https://github.com/agentculture/agentfront) as a CLI, an HTTP API
and an MCP surface — so any app can `import events_cli`, any service can call
the API, and an agent or a human can publish and subscribe to events the same
way.

**Design principle (from [#1](https://github.com/agentculture/events-cli/issues/1)):**
*Mosquitto transports events. `events-cli` defines what they mean.* Consumers
depend on the `events-cli` contract — typed envelopes, correlation/causation,
pipeline runs — not on Mosquitto-specific topic conventions, so the transport
can be replaced later without changing how participants interact.

### Read this before you build anything

**The event/broker domain is not implemented yet.** What is on disk today is the
agent-first CLI scaffold inherited from `culture-agent-template` — identity,
introspection verbs, CI, packaging. Nothing MQTT, Docker, envelope- or
pipeline-related exists in `events_cli/`.

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
events whoami            # identity from culture.yaml
events learn             # structured self-teaching prompt
events explain <path>    # markdown docs for any noun/verb path
events overview          # descriptive snapshot of the agent
events doctor            # check the agent-identity invariants
events cli overview      # describe the CLI surface itself
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

CI enforces the rubric with `uv run teken cli doctor . --strict`.

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
runtime package has **zero third-party dependencies**. It locates the file by
walking up from `__file__`, so identity is always *this agent's*, never whatever
`culture.yaml` sits in the caller's CWD; a wheel install finds none and falls
back to literal defaults (which is why `doctor` reports a single info check and
exits 0 there).

## Design constraints (decide these before the data plane)

These come out of #2 and #3 and are expensive to retrofit. They are decisions,
not code — nothing below exists yet.

**One registry, four surfaces.** agentfront is an importable runtime, not a
scaffolder: declare docs and tools once on an `App`, and CLI, MCP and HTTP are
*derived* from that registry, so they cannot drift apart. agentfront derives
three surfaces; the request names four. The fourth — `import events_cli` — is not
agentfront's and is yours to keep honest. Build a core module that owns event
semantics and populate the agentfront registry *from that core*. If the registry
is filled by separate adapter functions, the import lane becomes a fourth thing
that drifts, which is exactly what agentfront otherwise removes.

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
connects **directly to TCP 1883** with `paho-mqtt >=2,<3` and cannot go through
the CLI/HTTP/MCP front. Direct broker access is a supported, documented surface
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
  `mosquitto.conf`.
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
the parts worth high coverage anyway — and mark stack integration tests so they
can be selected separately. A suite that needs a live broker to collect coverage
makes the quality gate flaky.

**Dependency posture.** Runtime `dependencies = []` today, and agentfront keeps
CLI + HTTP dependency-free with MCP behind an extra (`agentfront[mcp]`). An MQTT
client is a real third-party dependency: decide whether `pip install events-cli`
pulls it or whether the data plane sits behind an extra. Matching agentfront's
convention is the path of least surprise.

**Do not build a store from scratch.** `eidetic-cli` (memory/recall) and
`data-refinery-cli` (data quality) already own that lane; evaluate them before a
bespoke history backend. Likewise, if `events up` shells out to `docker`, check
whether that belongs behind `shell-cli` (which owns shell confinement and the
approval policy) rather than a bare `subprocess` call — this repo should not grow
a second shell-confinement posture.

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
uv run pytest -n auto                  # full suite (29 tests today)
uv run pytest tests/test_cli.py -v     # one file
uv run pytest -k whoami -v             # one test / pattern
uv run pytest -n auto --cov=events_cli --cov-report=term   # with coverage
```

Lint — all four run in CI and must pass:

```bash
uv run black --check events_cli tests      # line length 100
uv run isort --check-only events_cli tests # profile=black
uv run flake8 events_cli tests
uv run bandit -c pyproject.toml -r events_cli
markdownlint-cli2 "**/*.md" "#node_modules" "#.local" "#.claude/skills" "#.teken"
uv run teken cli doctor . --strict     # the agent-first rubric gate
```

Coverage floor is 60% (`[tool.coverage.report] fail_under`), and SonarCloud
gates the PR on top of that (`sonar-project.properties`,
`sonar.qualitygate.wait=true`). The Sonar step is skipped when `SONAR_TOKEN` is
absent, so fork PRs stay green.

The CLI has no runtime dependencies, so it also runs straight from a checkout
without `uv sync` — useful when the network is unavailable:

```bash
PYTHONPATH=. python3 -m events_cli doctor
PYTHONPATH=. python3 -m pytest tests -q
```

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
events_cli/               agent-first CLI (cited from teken's python-cli reference)
  cli/__init__.py         the single parser + dispatch/exit-code translation
  cli/_output.py          stdout/stderr split          (stable-contract)
  cli/_errors.py          CliError + exit-code policy  (stable-contract)
  cli/_commands/          one module per verb; each exposes register(sub)
  explain/catalog.py      markdown keyed by command-path tuple
tests/                    pytest smoke + introspection tests
.claude/skills/           vendored guildmaster skill kit (cite-don't-import)
docs/skill-sources.md     skill provenance ledger
culture.yaml              mesh identity (suffix + backend)
.github/workflows/        tests.yml (test/lint/version-check), publish.yml (PyPI)
```

## Roadmap

Not built; tracked in the issues above. The rough order the constraints imply:

1. **Core event semantics** — envelope + validation, pure and dockerless, as the
   module the other surfaces are built from.
2. **agentfront binding** — one registry deriving CLI/MCP/HTTP, plus the
   `import events_cli` lane fed from the same core. Requires migrating the dev
   dependency (below).
3. **Stack management** — `events init` / `up` / `status` / `logs` / `down`,
   Compose with `127.0.0.1:1883:1883`, `persistence true` + volume, dynsec.
   This is what unblocks [#3](https://github.com/agentculture/events-cli/issues/3)
   on `spark-f8a9`, including stopping the `nova-mosquitto` /
   `nova-nervous-system` stack so exactly one broker runs on the box.
4. **Durable subscriptions + cursor drain**, designed together with history.
5. **Pipelines** — apply/list/show/run/inspect over the event graph.

## Known drift

Real, checked-in inconsistency. Fixing it is a normal PR (bump the version):

- **`teken` → `agentfront`.** The dev dependency is `teken>=0.8` and CI runs
  `uv run teken cli doctor . --strict`, but teken was renamed: `agentfront`
  0.20.0 is the live package and `teken` survives only as a deprecated console
  alias that prints a stderr note and forwards. Migrate the dev dep, the CI step,
  and `uv.lock` together — and note that `docs/skill-sources.md`,
  `.claude/skills.local.yaml.example` (`sibling_projects`), and
  `.markdownlint-cli2.yaml` (`.teken/**` ignore) still reference the old name.
  The rename is behaviourally inert today, so this is housekeeping, not a bug.

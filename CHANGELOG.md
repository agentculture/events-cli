# Changelog

All notable changes to this project will be documented in this file.

Format follows [Keep a Changelog](https://keepachangelog.com/). This project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.10.0] - 2026-07-24

### Added

- **`events emit <type> --data <file|->`** — validates an envelope through the core (generating `id`/`time`, with `type` as the positional and `--data` supplying only the event's `data` payload — a file path or `-` for stdin, never a whole envelope), then publishes it QoS 1 to its canonical topic (`events_cli/core/topics.py`) via `EventClient`. An invalid envelope is rejected with every field-level reason in one pass and **nothing is published**; a valid envelope is always published at QoS 1, never a flag, so a CLI-emitted event is always eligible for durable capture. `--correlation-id`/`--causation-id`/`--run-id` set the envelope's tracing fields. A broker-down publish is an environment fault (exit 2), and the event/topic/`PublishResult` are still printed either way.
- **`events get <event-id>`** and **`events list [--type T] [--max N]`** — read captured history straight from the store (`events_cli/history/`), read-only and dockerless: neither imports `events_cli.client`, so both run with no MQTT client installed. `get` on an unknown id and `list --max 0` are user errors (exit 1); a damaged store record is an environment fault (exit 2). Both carry `--json` and an `events explain` catalog entry.

### Changed

- `[project] description` no longer claims an HTTP API or an MCP surface. Both are the deferred agentfront binding ([#6](https://github.com/agentculture/events-cli/issues/6)), not shipped code, so the package's own PyPI-facing summary was prose ahead of the repository — the exact drift `CLAUDE.md`'s Known-drift rule exists to catch. It now names what is built: the envelope core, the importable client, and the stack verbs.
- **`EventClient.publish_event` now defaults to `qos=1`, not `qos=0` — a behaviour change on an already-published wheel.** An envelope published at QoS 0 is never queued for an offline persistent session at all, so it silently bypassed durable capture (`events_cli/history`) regardless of whether a subscription existed — exactly the trap this arc's consume side exists to close. Only the envelope-publishing lane changes: `EventClient.publish()` (the raw lane `reachy-mini-cli`'s 50 Hz control loop binds to with an explicit `qos=0`) keeps its `qos=0` default and is untouched. Callers relying on the old `publish_event` default now get QoS 1 by default; pass `qos=0` explicitly to keep the previous behaviour. `docs/contract.md` and the `client.py` docstrings both state that a QoS 0 publish bypasses durable capture.

## [0.9.0] - 2026-07-24

### Added

- **Envelope core** (`events_cli/core/`) — an immutable, CloudEvents-compatible `Envelope` with `id`/`type`/`source`/`time`/`schemaVersion`/`data` plus tracing fields (`correlationId`, `causationId`, `runId`, `traceparent`, `producer`, `deliveryAttempt`). Standard library only: no transport, no Docker, no I/O. `from_dict`/`from_json` is the trust boundary and reports **every** problem as a field-level `FieldError` in one pass. Deliberately stricter than CloudEvents — unknown top-level keys rejected, numeric `schemaVersion`, lowercase-dotted `type`, absolute-URI `source`.
- **Importable publish client** (`events_cli.EventClient`) — the producer lane for co-located latency-sensitive publishers. `publish()` is an O(1) enqueue on the caller's thread and never raises into the caller; a broker-down publish returns `PublishResult(ok=False)`. Supports retained messages, Last Will and Testament, QoS 0/1, and a per-process-unique client id that avoids MQTT session takeover between co-located producers.
- **Stack verbs** `events init/up/status/logs/down` — generate and operate a Dockerised Mosquitto broker. Loopback-only by construction (`127.0.0.1:1883:1883`), an exact image pin, `persistence true` plus a mounted `events-data` volume, and a preflight that refuses to double-bind port 1883 when a foreign broker holds it.
- **Consumer contract** (`docs/contract.md`) — the event contract, the mesh lane boundary against `culture`, and the at-least-once/dedupe-on-id requirement.
- **Docker-backed integration suite** behind a `stack` marker plus an `EVENTS_STACK_IT` opt-in, including a measured unclean-kill persistence bound and a registry probe that fails if the pinned image tag is not published.
- **Runnable issue-#3 acceptance checklist** (`scripts/acceptance-issue-3.sh`) covering all five criteria against a live broker, and the executed live-run record in `docs/acceptance/`.
- **Nova migration runbook** (`docs/migrations/`) with an executed forward/rollback rehearsal.

### Changed

- `paho-mqtt >=2,<3` is now a **base dependency** — `pip install events-cli` pulls it. The previous `dependencies = []` posture is given up deliberately; what preserves the no-install introspection lane is a lazy-import boundary (paho is imported only inside `client.py`, only when a client is constructed), not an empty dependency list.
- Dev dependency and CI rubric gate migrated from `teken` to `agentfront>=0.20`, resolving the repo's only tracked drift entry (closes #5).
- `CLAUDE.md` trued up to the repo as built — it previously stated that nothing MQTT, Docker or envelope-related existed.
- Default pytest selection now excludes the `perf` and `stack` markers, so the coverage/Sonar gate never depends on a live broker or wall-clock timing.

### Fixed

- **Broker image pin corrected to `eclipse-mosquitto:2.1.2-alpine`.** The previously pinned `eclipse-mosquitto:2.1.2` is a tag upstream has never published — a pull fails with `no such manifest`, so `events up` would have failed on any clean host while passing every dockerless test and every run on a cache-warm machine. The version had been read from the local image's `org.opencontainers.image.version` label, which reports the software version rather than a tag name. Verified that `eclipse-mosquitto:2` and `eclipse-mosquitto:2.1.2-alpine` resolve to an identical manifest digest, so the corrected pin freezes exactly what the floating tag serves today (deviation d2).

## [0.8.0] - 2026-07-24

### Added

- **Three packaging-contract tests** pinning the three-way name split.
  `test_import_package_is_events_cli` asserts the top-level module;
  `test_packaging_config_points_at_events_cli` reads `pyproject.toml` and
  asserts the script entry, hatch packages, coverage source and isort
  first-party all name `events_cli` while the distribution stays `events-cli`;
  `test_no_top_level_events_package_in_the_source_tree` fails if anyone re-adds
  an `events/` directory beside the source, which is the only way the PyPI
  collision can come back.

### Changed

- **The import package is now `events_cli`, not `events`** — breaking for
  importers, though no consumer binds to it yet. PyPI's
  [`Events`](https://pypi.org/project/Events/) distribution already owns the
  top-level module `events`, so any environment installing both `events-cli`
  and `Events` silently clobbered one; the collision is vacated now, before the
  first consumer (`reachy-mini-cli`, issue #3) binds to the import name. Renamed
  with `git mv`, so file history is preserved. **The console command stays
  `events` and the distribution stays `events-cli`** — only the import name
  moved. `[project.scripts]` is now `events = "events_cli.cli:main"`, and
  `[tool.hatch.build.targets.wheel]` packages, `[tool.coverage.run]` source,
  `[tool.isort]` `known_first_party`, `sonar.sources`, the CI `--cov=` / lint /
  bandit paths, and `publish.yml`'s path filter all follow.
- **The documented no-install fallback is `PYTHONPATH=. python3 -m events_cli`.**
  `_prog.py` still resolves the invocation by comparing `sys.argv[0]` against
  *this* package's `__main__.py` by full path, so hints name
  `python -m events_cli` in module mode and `events` when installed. The explain
  catalog's root keys are deliberately unchanged: `("events",)` and
  `("events-cli",)` are command-path and distribution names, not module names,
  and both still resolve to the root entry.
- **`learn --json` gained an `import_package` field.** All three names — the
  command (`tool`), the distribution (`distribution`) and the module
  (`import_package`) — are now machine-discoverable, so an agent consuming the
  CLI never has to guess which string goes in an `import` statement.

## [0.7.0] - 2026-07-23

### Added

- **`CLAUDE.md` initialized into a real runtime prompt** (`/init`), replacing the
  `guild create` seed placeholder. It now states the thing no single file in the
  repo revealed: the event/broker domain is **not implemented** — everything in
  `events/` is the `culture-agent-template` scaffold, renamed — and points at
  issues #1/#2/#3 as the requirements baseline. Documents the CLI architecture
  (single registration point, the `_dispatch` exit-code translation, the
  stable-contract modules), the design constraints that are expensive to
  retrofit (one-registry/four-surfaces, `watch` not fitting request/response,
  keeping the raw MQTT port first-class, explicit `127.0.0.1:1883:1883`, the
  Mosquitto facts, Docker-free unit tests), the culture-vs-events-cli lane
  boundary, and the contributor conventions carried over from the template
  (version-bump-every-PR, `cicd`, `ask-colleague`, worktree location, memory
  discipline). Also reconciles the seed's stale `backend: claude` claim against
  `culture.yaml`'s actual `backend: colleague`.
- **`events/cli/_prog.py`** — resolves the command name to name back at the
  user, so remediation hints reference an invocation that actually exists. The
  installed console script gets `events`; the documented no-install fallback
  (`python -m events` from a checkout, where the console script is typically
  absent) gets `python -m events`. Detection compares `sys.argv[0]` against
  *this* package's `__main__.py` by full path — a basename check would match
  every other `python -m` host, `python -m pytest` included. Consumed by
  argparse's `prog` and by the `explain` catalog's unknown-path remediation.
- **Two regression tests for the command-name contract** —
  `test_prog_matches_installed_console_script` reads `[project.scripts]` from
  `pyproject.toml` and asserts argparse's `prog` matches it, so the two can
  never drift apart again; `test_explain_root_keys_both_resolve_to_same_entry`
  pins that `explain events` and `explain events-cli` reach the same root entry
  (the rubric gate's `explain_self` check calls the former).

### Changed

- **The CLI now calls itself `events`, the command that is actually installed.**
  argparse's `prog` was `events-cli` — the PyPI distribution name — so `--help`
  and every argparse error printed a command name that does not exist on PATH.
  `prog`, the `learn` command map, the `explain` catalog, `doctor`'s status
  line, and the `overview` / `cli overview` subjects all now say `events`. The
  mesh nick stays `events-cli` (it is the `culture.yaml` suffix, deliberately
  distinct from the console command), as does the distribution name.
- **User-facing strings describe the event fabric instead of the template.** The
  parser description, `learn` prose and JSON payload, and every `explain` entry
  described this as "a clonable template for AgentCulture mesh agents" — the
  first thing an agent consuming this CLI reads. They now describe the broker /
  contract purpose and state plainly that the event surface is not built yet.
  `learn --json` gains a `distribution` field so the dist name is still
  machine-discoverable alongside the new `tool: "events"`.
- **`README.md` rewritten** off the template's "make it your own" text, with the
  scaffold status, the mesh lane boundary, and a working quickstart. Its
  quickstart was broken: it invoked `uv run events-cli whoami`, which never
  existed as a console script.

### Fixed

- Tests that pinned the old command spelling (`"usage: events-cli"`,
  `"events-cli doctor"`, `payload["tool"] == "events-cli"`, the `overview`
  subjects) updated to the real command name, and several loose `in` assertions
  tightened to anchored `startswith` checks so `# events` can no longer pass by
  matching `# events cli`.

## [0.6.1] - 2026-07-20

### Added

- **Worktree location convention** in `CLAUDE.md` — every worktree you create
  by hand (workforce fan-out lanes, scratch checkouts) lives in
  `../.worktrees.events-cli/<name>/`, one
  repo-named directory beside the checkout, replacing a shared `../worktrees/`
  folder. This workspace holds many sibling projects, so a generic shared
  folder accumulates orphaned trees from several repos at once with nothing
  indicating ownership — a stale-tree sweep can't tell a live lane from junk.
  Matches the convention already documented in sibling repo `reachy-mini-cli`.
  Adds branch-prefix guidance (scope the prefix to the work; plain `agent/*`
  collides with leftovers from earlier fan-outs and fails `git worktree add
  -b`), and notes that the vendored `assign-to-workforce` skill uses both the
  shared path *and* `agent/<task-id>` branches in its fan-out example — it is
  cited verbatim and must not be edited, so both are overridden when following
  it. Teardown guidance names `git worktree remove <path>` as the verb that
  actually deletes a worktree; `git worktree prune` only clears metadata for
  directories that are already gone. Tool-managed throwaways are explicitly
  out of scope: `ask-colleague`'s read-only verbs create a detached worktree
  under `${TMPDIR:-/tmp}` and reap it on an EXIT trap, so they never persist
  to need an owner.

## [0.6.0] - 2026-07-18

### Added

- **Four devague-origin skills re-vendored into `.claude/skills/`**
  (cite-don't-import), synced to the fixed devague source
  (devague#74/#75/#76):
  - `challenge` — a risk-scaled blind-spot discovery pass that runs between
    `/think` and `/spec-to-plan`, routing findings back through the existing
    deterministic moves as human-adjudicated proposals.
  - `scope` — the idea→scope leg that surveys the surfaces an idea touches
    before framing, seeding the Announcement Frame with provenance-backed
    boundary/non-goal/assumption claims.
  - `deviate` — stops an in-flight `assign-to-workforce` run when execution
    must diverge from the confirmed plan and records the divergence as a
    first-class, append-only deviation record.
  - `summarize-delivery` — closes the loop after an `assign-to-workforce`
    run with a planned-vs-actual accountability artifact.

  These four originate in `devague` and are re-broadcast via guildmaster; see
  `docs/skill-sources.md` for provenance.

## [0.5.0] - 2026-06-24

### Added

- **Memory-discipline "Conventions and workflow" section in `CLAUDE.md`** — a
  per-task *recall-before / remember-after* convention (scope localized to this
  repo's nick) so the vendored `remember` / `recall` skills are actually used,
  not just present: `/recall` before non-trivial work to build on prior
  decisions instead of re-deriving them, and `/remember` when a non-obvious
  decision, constraint, fix-and-why, or hard-won gotcha surfaces. The section
  documents this repo's memory as **in-repo and public** — records resolve to
  `<repo-root>/.eidetic/memory` (committed, team- and mesh-shared). Inserted
  idempotently (skipped if already present), slotted under an existing
  "Conventions and workflow" heading when one exists, else appended.

### Changed

- **Refreshed the `remember` + `recall` wrappers from eidetic-cli 0.10.0**
  (cite-don't-import) — picks up eidetic's **project-local store default**: the
  files backend now resolves per record by visibility — PUBLIC records inside a
  git repo go to `<repo-root>/.eidetic/memory` (committed, team-shared), PRIVATE
  records (or any record outside a repo) go to `$HOME/.eidetic/memory` (never
  committed), an explicit `EIDETIC_DATA_DIR` still wins, and recall reads both
  stores and merges. Also carries the 0.9.3 hardening (interactive-stdin guard,
  `help` as a search term, SIGPIPE-safe suffix parsing). **Recipe policy
  override (the wrappers here are NOT byte-verbatim):** the injected default
  visibility is flipped from eidetic's `private` to **`public`**, so a plain
  `/remember` lands the note in `./.eidetic/memory` in this repo, kept as part
  of the repo — pass `--visibility private` to route a record to `$HOME`
  instead. `remember` drives `eidetic remember` (idempotent upsert of one JSON
  record or an NDJSON batch on stdin); `recall` drives `eidetic recall` with
  four search modes (exact / approximate / keyword / hybrid). Each `SKILL.md` is
  localized only in the illustrative `--scope <nick>` examples (Provenance keeps
  "First-party to eidetic-cli"). Runtime dep: the `eidetic` CLI on PATH (else a
  local eidetic-cli checkout with `uv`) — **`eidetic >= 0.10.0`** for the
  in-repo routing; on an older CLI the public records still work but are stored
  in `$HOME/.eidetic/memory` instead of in-repo. Propagated by rollout-cli's
  `eidetic-memory` recipe.

## [0.4.0] - 2026-06-23

### Added

- **Vendored the `remember` + `recall` memory skills from eidetic-cli**
  (cite-don't-import) — the write/read halves of eidetic's shared
  `$HOME/.eidetic/memory` surface, so this agent (Claude and its colleague
  backend) can persist facts across sessions and recall them later, sharing
  one store.
  `remember` drives `eidetic remember` (idempotent upsert of one JSON record or
  an NDJSON batch on stdin, dedup by id + content hash); `recall` drives
  `eidetic recall` with four search modes — exact / approximate / keyword /
  hybrid — each hit carrying text, full provenance metadata, a relevance score,
  and a freshness signal. The `.sh` wrappers are byte-verbatim from eidetic-cli
  (their first-party origin); each `SKILL.md` is localized only in the
  illustrative `--scope <nick>` examples (Provenance keeps "First-party to
  eidetic-cli"). Both default to this agent's PRIVATE scope, reading the suffix
  from `culture.yaml`. Runtime dep: the `eidetic` CLI on PATH (else a local
  eidetic-cli checkout with `uv`). Propagated by rollout-cli's `eidetic-memory`
  recipe.

## [0.3.4] - 2026-06-20

### Fixed

- Identity docs and self-description strings still claimed `backend: claude`
  (prompt file `CLAUDE.md`), but this template was promoted to a colleague
  resident in #14/#15: `culture.yaml` declares `backend: colleague` (Qwen) with
  `AGENTS.colleague.md` as the resident prompt. Corrected the stale claim in
  `CLAUDE.md` (Identity section), `README.md`, `docs/skill-sources.md`, and the
  two CLI description strings (`overview` artifacts and `explain doctor`). The
  `doctor` backend→prompt-file mapping and the tests were already on
  `colleague`; this aligns the prose and self-description with them.

## [0.3.3] - 2026-06-20

### Fixed

- pyproject.toml: correct the `license` field and PyPI classifier from MIT to
  Apache-2.0 to match the `LICENSE` file. The README License section was already
  corrected in 0.3.2, but the package metadata was missed; the built wheel now
  reports `License-Expression: Apache-2.0`.

## [0.3.2] - 2026-06-18

### Added

- ask-colleague skill: `monitor`/`guide`/`stop` pilot verbs plus a `--watch`
  flag to dispatch, watch the live feed of, send mid-flight guidance to, and
  cooperatively stop a running colleague flight (re-vendored from colleague).

### Changed

- README: correct the License section from MIT to Apache 2.0 to match the
  `LICENSE` file.

## [0.3.1] - 2026-06-13

### Changed

- CLAUDE.md: add a convention to reach for the `ask-colleague` skill reflexively
  for explore/review/write/grade — read-only `review`/`explore` are always safe;
  side-effecting `write` needs the user's go-ahead.

## [0.3.0] - 2026-06-13

### Added

- AGENTS.colleague.md resident prompt file (backend colleague <-> AGENTS.colleague.md)

### Changed

- Promote agent identity to a colleague resident: culture.yaml backend
  claude -> colleague with a pinned model. The `doctor` backend-consistency
  map gains `colleague` -> AGENTS.colleague.md.

## [0.2.1] - 2026-06-12

### Changed

- **Re-vendored the `ask-colleague` skill from colleague (now 1.7.0, up from the
  0.39.2 sync)** — the wrapper had drifted multiple releases behind origin. Picks
  up the `clean` verb (reap stale/corrupt `colleague/*` branches + orphaned
  `.colleague/` artifacts a crashed run left behind), the `--json` flag on every
  verb (result JSON on stdout, diagnostics/digest on stderr), the
  `_colleague_via_uv` local-dev resolution that honors `--repo`, and the
  tri-state (0/1/2) exit-code contract. `scripts/ask-colleague.sh` + `prompts/`
  are byte-identical to the origin; `SKILL.md` diverges only in the one
  consumer-identifying Provenance clause (`events-cli vendors from
  guildmaster`). `docs/skill-sources.md` sync row updated to
  `2026-06-12 (colleague 1.7.0, direct)`. Refs: colleague#183, #186.

## [0.2.0] - 2026-06-06

### Added

- **`ask-colleague` skill** (`.claude/skills/ask-colleague/`) — the first-party front door to the `colleague` CLI (the renamed `convertible`). On top of `explore` / `review` / `write` it adds a `feedback` verb (grade a finished work item — the ROI loop), and `write` now **previews by default** in a throwaway worktree (no side effects) unless `--apply` / `--pr` is given. Reach for it reflexively — `review` for a diverse second opinion on a committed diff before opening a PR, `explore` for a fresh read of an unfamiliar area.

### Changed

- **Replaced the `outsource` skill with `ask-colleague`.** `outsource` was renamed to `ask-colleague` upstream ([colleague#148](https://github.com/agentculture/colleague/pull/148)). Because guildmaster has not re-broadcast the rename yet (its kit still ships the old `outsource`), `ask-colleague` is vendored **directly from the sibling `colleague` checkout** rather than from guildmaster — a tracked local divergence recorded in `docs/skill-sources.md`, parallel to the `agex` → `devex` one. Vendored verbatim except one consumer-identifying clause in the Provenance paragraph.
- **Ledger + CLAUDE.md + `.gitignore`:** point `docs/skill-sources.md` and the CLAUDE.md Skills section at `colleague` / `ask-colleague`, swap the *optional* runtime prerequisite `convertible` → `colleague` (env prefix `CONVERTIBLE_*` → `COLLEAGUE_*`, with the legacy names kept as a deprecated fallback), and gitignore the `.colleague/` run-artifact dir the skill writes (plus the stale `.agex/`).

## [0.1.4] - 2026-05-31

### Added

- **Vendor the `outsource` skill** (`.claude/skills/outsource/`) from
  guildmaster's canonical copy (origin
  [`agentculture/convertible`](https://github.com/agentculture/convertible),
  re-broadcast via guildmaster — guildmaster
  [#51](https://github.com/agentculture/guildmaster/pull/51)). Every agent
  cloned from this template now inherits the ability to hand a scoped task to a
  *different* engine/mind: `explore` (read-only investigation), `review` (a
  diverse second opinion on the committed diff), and `write` (delegate a small
  implementation). `explore`/`review` run isolated in a throwaway `git worktree`;
  `write` refuses a dirty tree. Fulfils
  [#8](https://github.com/agentculture/events-cli/issues/8).
- **Ledger + CLAUDE.md:** record `outsource` in `docs/skill-sources.md`
  (origin = convertible, re-broadcast via guildmaster; vendored verbatim — it
  already carries `type: command`) and document its *optional* runtime
  dependency on the `convertible` CLI (the skill exits with an install hint if
  absent, so a clone that never uses it is unaffected).

### Changed

### Fixed

## [0.1.3] - 2026-05-31

### Changed

- Expanded the clone-and-rename instructions in `CLAUDE.md`: added `README.md` to
  the rename targets and a portable `git grep` discovery command so a cloner can
  find every occurrence of the template name (hard-coded in ~100 places across the
  package, including the CLI command files and `_ISSUES_URL` in
  `events/cli/__init__.py`) rather than renaming by hand.
- Synced `README.md`'s "Make it your own" checklist with `CLAUDE.md`: it now lists
  `README.md` itself as a rename target and points to `CLAUDE.md`'s discovery
  command as the authoritative procedure, so the two onboarding checklists no
  longer drift.

## [0.1.2] - 2026-05-30

### Changed

- Renamed the PR-lifecycle CLI references `agex` / `agex-cli` to `devex` (same
  tool, new name) across `CLAUDE.md`, `docs/skill-sources.md`, `.gitignore`, and
  the vendored `cicd`, `assign-to-workforce`, and `communicate` skills — the
  `cicd` scripts now invoke `devex pr`.
- Logged the vendored-skill in-place patch as a local divergence in
  `docs/skill-sources.md`; the matching canonical rename is tracked upstream for
  guildmaster in
  [agentculture/guildmaster#48](https://github.com/agentculture/guildmaster/issues/48)
  so a future re-sync reconciles cleanly.
- Aligned the documented `devex` version floor to `>=0.21` across the vendored
  `cicd` `SKILL.md` and `workflow.sh` install hint (were `>=0.1`), matching
  `docs/skill-sources.md` and the `await`-era feature set; flagged upstream on
  guildmaster#48.

### Fixed

- SonarCloud now reports code coverage — added `relative_files = true` to
  `[tool.coverage.run]` so `coverage.xml` emits repo-relative paths that map to
  `sonar.sources=events` (absolute / `.venv` paths were dropped
  as unmappable). Mirrors the sibling `convertible` setup.

## [0.1.1] - 2026-05-26

### Changed

- **CI gates on the SonarCloud quality gate**
  ([issue #3](https://github.com/agentculture/events-cli/issues/3)) —
  added `sonar.qualitygate.wait=true` to `sonar-project.properties` so a failing
  gate fails the `test` job when `SONAR_TOKEN` is set. Token-less repos and fork
  PRs remain green (the scan step is guarded by `if: env.SONAR_TOKEN != ''`).

## [0.1.0] - 2026-05-26

### Added

- **Onboarded into the AgentCulture mesh** ([issue #1](https://github.com/agentculture/events-cli/issues/1)).
- **Agent-first CLI** cited from teken's (`afi-cli`) `python-cli` reference
  (`teken cli cite`) — verbs `whoami`, `learn`, `explain`, `overview`, `doctor`,
  and the `cli` noun group. Runtime is self-contained (`dependencies = []`);
  `teken>=0.8` is a dev dependency only. Passes the seven-bundle agent-first
  rubric (`teken cli doctor . --strict`). `doctor` checks the agent-identity
  invariants (prompt-file-present, backend-consistency, skills-present).
- **Mesh identity**: `culture.yaml` (`suffix: events-cli`,
  `backend: claude`) and the matching `CLAUDE.md` prompt file.
- **Canonical guildmaster skill kit** (11 skills) vendored under
  `.claude/skills/` (cite-don't-import): `agent-config`, `assign-to-workforce`,
  `cicd`, `communicate`, `doc-test-alignment`, `pypi-maintainer`, `run-tests`,
  `sonarclaude`, `spec-to-plan`, `think`, `version-bump`. Every `SKILL.md`
  carries `type: command` (load-bearing for the culture/claude backend);
  `cicd` / `communicate` consumer-identifying prose adapted, all script bodies
  verbatim. Provenance in `docs/skill-sources.md`. Three skills (`think`,
  `spec-to-plan`, `assign-to-workforce`) originate in `devague`, re-broadcast
  via guildmaster.
- **Build + deploy baseline**: `pyproject.toml` (hatchling), `tests/` (pytest,
  xdist, coverage), `.github/workflows/{tests,publish}.yml` (CI rubric/lint gate,
  PyPI Trusted Publishing), `.flake8`, `.markdownlint-cli2.yaml`,
  `sonar-project.properties`, and `.claude/skills.local.yaml.example`.

### Changed

### Fixed

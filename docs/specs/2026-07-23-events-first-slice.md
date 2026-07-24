# events first slice

> events-cli ships its first vertical slice: a loopback-only Mosquitto stack on spark-f8a9 replacing the nova brokers, a CloudEvents envelope core, an importable O(1)-enqueue client on PyPI that unblocks reachy-mini-cli, and agentfront-derived CLI/MCP/HTTP surfaces — issues #1 #2 #3
> instruction: Verify on spark-f8a9 against issue #3's acceptance checklist: fresh install of the released wheel, events init && events up, ss -ltn shows 1883 on 127.0.0.1 only, pub/sub round-trip with retained+LWT+QoS0, nova pair absent, sub-millisecond client enqueue measured by test

## Audience

- Co-located producers and consumers on spark-f8a9 first: reachy-mini-cli 50 Hz publisher and its reTerminal-bridge consumer; then any Python app importing events, any service on the HTTP API, agents over MCP, and humans on the events CLI

## Before → After

- Before: Today the box runs nova-mosquitto LAN-exposed on 0.0.0.0:1883/9001 with allow_anonymous true, coupled to nova-nervous-system; reachy-mini-cli publishes against a fake client because no owned broker exists; events/ contains only the introspection scaffold with zero event, broker or client code
- After: Exactly one loopback-only Mosquitto broker runs on spark-f8a9, created and managed by events init/up/status/logs/down; the nova stack is gone; reachy-mini-cli binds its injected client seam to the released events-cli wheel in one composition line, its events flow on reachy/events/# and retained state on reachy/state/# survives broker restarts; the envelope core validates events with zero broker or docker dependency

## Why it matters

- Agents and applications in the mesh need a shared machine-event substrate without service-to-service coupling — culture carries conversation, nothing carries typed correlated events; the reachy nervous-system arc is blocked on it today, and every future consumer (reTerminal, tau, daria, devague) inherits whatever contract ships first

## Requirements

- Core envelope module: CloudEvents-compatible immutable envelope (id, type, source, time, schemaVersion, data) plus tracing fields (correlationId, causationId, runId, trace context, producer identity, delivery attempt) with pure, dockerless validation — built first, as the module every other surface derives from (issue #1 contract, CLAUDE.md roadmap step 1)
  - instruction: Implement as the core module (e.g. events/core/): envelope type + validate() with field-level named errors, stdlib-only; property test parse(serialize(e)) == e; generated evt_ ids; correlation/causation/runId optional tracing fields per issue #1; zero docker in these tests
  - honesty: Envelope validation runs pure: pytest passes with no docker or broker present; malformed envelopes are rejected with named field-level reasons; a valid envelope round-trips id/type/source/time/schemaVersion/correlation/causation/runId unchanged through serialize and parse
- Importable client surface per the #3 operator amendment: events-cli the package exposes a publish client — O(1) enqueue from the caller thread with network I/O on background machinery, retained publishes, Last Will, QoS 0, never raises into the caller on broker-unreachable, observable connection state — shipped as a pure-Python wheel (py>=3.12) on PyPI; the first release carrying this API is reachy-mini-cli composition unblock
  - instruction: Implement as the client module wrapping paho-mqtt >=2,<3: connect_async + reconnect on the client's own loop thread, queue-backed publish returning immediately, LWT configured before connect, observable connection state; a test measures enqueue latency with a monotonic clock from a non-owner thread; broker-unreachable paths covered by test
  - honesty: A publish from a non-owner thread measures as an O(1) sub-millisecond enqueue (asserted by a test, not claimed); with the broker unreachable, constructing the client and publishing raises nothing into the caller and connection state is observable; the wheel installs on py3.12 pulling only paho-mqtt
- Stack verbs events init/up/status/logs/down generate a Compose deployment pinned to eclipse-mosquitto:2 with the port mapping explicitly 127.0.0.1:1883:1883, persistence true plus a mounted volume, and a healthcheck; the generated mosquitto.conf states which security properties are upstream 2.0 defaults rather than a hardening layer
  - instruction: Register a stack noun group in events/cli: init writes compose + mosquitto.conf (pinned eclipse-mosquitto:2, literal 127.0.0.1:1883:1883, persistence true + named volume, mosquitto_sub healthcheck, conf comments naming which properties are upstream defaults); up/down/logs/status drive docker compose via stdlib subprocess (issue #9 tracks the shell-cli revisit); acceptance = the docker-restart retained-state test
  - honesty: events init writes a compose file containing the literal mapping 127.0.0.1:1883:1883 and persistence true with a mounted volume; events up reaches a passing healthcheck; retained state survives a docker restart of the broker (the first-restart test #2 warns is easy to omit)
- Migration on spark-f8a9: the events-cli deploy stops the nova stack (nova-mosquitto, nova-nervous-system) as a documented deliberate step so exactly one broker runs on the box — verified live today: bare 1883:1883 mapping, 0.0.0.0:1883 and 0.0.0.0:9001 bound, allow_anonymous true
  - instruction: Ship as an events up preflight plus a documented migration: detect a foreign broker on 1883 (nova-mosquitto), refuse to double-bind, and print the exact stop command; the operator-run migration stops nova-mosquitto and nova-nervous-system; verify with ss -ltn and docker ps; record the step in docs
  - honesty: After the deploy on spark-f8a9, docker ps shows exactly one broker container; nova-mosquitto and nova-nervous-system are stopped with the migration recorded in docs; ss -ltn shows no 0.0.0.0 binding on 1883 or 9001; the untouched neighbours (eidetic, model-gear, qq) are still running
- agentfront binding: one App registry populated from the core events module, deriving CLI, MCP (behind the [mcp] extra) and HTTP; the import-events lane is fed from the same core so the fourth surface cannot drift; agentfront.testing.assert_surfaces_agree runs in the test suite
  - instruction: Tracked as #6, its own arc: build the agentfront App registry from the core module, derive http_app() and mcp_server() ([mcp] extra), add assert_surfaces_agree plus CLI/registry parity tests; every tool bounded with --max/--timeout defaults
  - honesty: When the binding lands (#6): the registry is populated from the core module, agentfront.testing.assert_surfaces_agree passes in CI, every MCP/HTTP tool has non-infinite --max/--timeout defaults, and the CLI parity tests fail if a registry tool lacks a CLI verb equivalent
- Migrate the dev dependency and CI gate from teken>=0.8 to agentfront (0.20.0 live): pyproject dev group, uv.lock, the CI step uv run teken cli doctor . --strict, plus stale references in docs/skill-sources.md, .claude/skills.local.yaml.example and .markdownlint-cli2.yaml — behaviourally inert housekeeping, but it must ride this arc since the binding depends on agentfront
  - instruction: One housekeeping PR riding this arc: dev dep teken>=0.8 -> agentfront, uv.lock regenerated, CI step becomes uv run agentfront cli doctor . --strict, sweep docs/skill-sources.md + .claude/skills.local.yaml.example + .markdownlint-cli2.yaml; version bump per convention
  - honesty: After the migration, uv.lock resolves agentfront (not teken), CI runs uv run agentfront cli doctor . --strict and passes, and no repo file outside CHANGELOG references teken (docs/skill-sources.md, .claude/skills.local.yaml.example, .markdownlint-cli2.yaml all updated)
- watch is durable server-side subscriptions plus cursor drain: named persistent subscriptions owned by an identity, drained via `--since <cursor>` --max N --timeout, with non-infinite defaults on every agent-facing tool; unbounded streaming stays CLI/HTTP-SSE only; designed together with history since replay-from-cursor and survive-a-restart are the same machinery (issue #2 hard constraint)
  - instruction: Tracked as #7, its own arc designed with history: named identity-owned subscriptions, control-service store (evaluate eidetic-cli / data-refinery-cli first per #2), drain verb --since/--max/--timeout with non-infinite defaults, streaming only on CLI/HTTP-SSE
  - honesty: When durable subscriptions land (#7): a drain call returns within its timeout with at most --max events plus a cursor; resuming from that cursor loses nothing acknowledged; a broker restart preserves both subscription registrations and undrained history
- The compose pins an exact Mosquitto version tag, not the floating eclipse-mosquitto:2 — the upstream-default annotations in the generated mosquitto.conf are only true for a specific version, and an unpinned tag can change security behavior under the deploy silently [challenge pass / operations lens: c4 vs CLAUDE.md verify-against-the-version-you-pin constraint]
  - honesty: The generated compose contains an exact mosquitto version tag and a unit test fails if the template ever carries a bare floating major tag
- The migration is reversible and says so: the migration doc records the exact rollback command (the nova compose file remains in reachy_nova) so a failed events-cli stack can be rolled back to the nova broker in one step during the cutover window [challenge pass / reversibility lens: docker-compose.nervous-system.yml still on disk]
  - honesty: The migration doc names the exact one-command rollback, and the cutover runbook is exercised once on spark-f8a9 (forward, back, forward) before the migration is called done
- The client generates a unique per-process client id by default (caller-overridable): MQTT brokers disconnect the existing session on client-id takeover, so two co-located producers or CLI sessions sharing a default id would kick each other into reconnect loops — the robot publisher, the reTerminal bridge and ad-hoc CLI probes all connect to the same loopback broker [challenge pass / concurrency lens: MQTT session-takeover semantics + issue #3 consumer set]
  - honesty: An integration test connects two default-constructed clients concurrently and both stay connected — default client ids observed unique across processes

## Honesty conditions

- The slice is real end-to-end: a fresh machine-local install of the released wheel plus events init/up yields a broker one process publishes to and another subscribes from, with nothing listening off-loopback and no manual steps outside the documented verbs
- The rubric gate (cli doctor --strict) and the pinned contract tests — dual catalog root keys, prog resolution, stdout/stderr split, exit codes — pass unchanged on every PR of this arc
- The README states the two-lane boundary in one sentence, and no events-cli surface accepts or relays agent conversation — machine envelopes only
- The unit suite passes on a machine with no docker and no broker (CI proves this on every PR); stack integration tests carry a pytest marker excluded by default and are selected explicitly
- The named first consumers are real, not aspirational: reachy-mini-cli's spec codes against the injected client seam today, and the reTerminal bridge is its declared first subscriber — both verified in that repo's committed spec
- Every element of the after-state is mechanically checkable on spark-f8a9: docker ps shows one broker, the stack verbs manage it, the reachy composition line imports the released wheel, and the restart test shows retained state surviving
- The before-state is evidence, not narrative: ss -ltn and docker ps captured 0.0.0.0:1883/9001 and the nova pair on 2026-07-23, and the events/ package contains no MQTT, docker or envelope code at that date's HEAD
- The blocked-consumer claim is traceable: reachy-mini-cli's converged spec and events-cli#3 both record that its broker-dependent acceptance items wait on this slice; no other machine-event substrate exists in the mesh today
- The success signal is exactly issue #3's acceptance checklist plus its amendment's sub-millisecond enqueue criterion — verified on the deploy box, each item a command with observable output, none a judgment call
- A CI-run test exercises the introspection verbs from a tree where paho-mqtt is not installed and they pass — the no-install lane is proven per PR, not assumed
- An integration test (stack-marked) kills the broker container uncleanly after a retained publish and the docs state exactly what survived — the durability claim is measured, not assumed
- The default pytest selection (the one CI and Sonar see) contains no wall-clock timing assertion; the enqueue measurement runs behind a marker with a documented generous bound

## Success signals

- Issue #3 acceptance passes end-to-end on spark-f8a9: ss -ltn shows 1883 bound on 127.0.0.1 only; a mosquitto_pub/sub round-trip demonstrates retained messages, LWT and QoS 0; a loopback client connects without credentials; the nova pair is stopped; and a co-located process importing the released PyPI wheel publishes one retained message and one QoS 0 event with the publish call measuring as a sub-millisecond enqueue

## Scope / boundaries

- The existing CLI contract survives the agentfront binding: stdout/stderr split (`_output.py`), CliError exit-code policy (`_errors.py`), no tracebacks, and the explain catalog keeping BOTH root keys (events,) and (events-cli,) — the rubric gate and pinned tests must stay green throughout
  - instruction: Treat `_output.py` and `_errors.py` as frozen; every new verb goes through register(sub) plus a catalog entry; the existing pinned tests and the rubric gate are the enforcement — no new formatter or error path
- Mesh lane boundary stated in the README: culture carries agent conversation (peer-to-peer, human-readable, IRC-shaped); events-cli carries machine events (typed immutable envelopes, correlation/causation, durable history, pipeline runs) — one sentence that stops contributors guessing
  - instruction: Add the one-sentence lane statement to the README (culture = agent conversation, events-cli = machine events), citing issue #2's adjudication
- Docker is never a unit-test dependency: envelope validation and pipeline-transition logic stay pure with high coverage; broker/stack integration tests are marked and separately selectable so the SonarCloud quality gate never depends on a live broker
  - instruction: Add a pytest marker (e.g. stack) for broker/docker integration tests, excluded by default in addopts; unit coverage of envelope + client logic stays dockerless; Sonar coverage comes from the default selection only
- The paho-mqtt import is lazy: introspection verbs (whoami, doctor, learn, explain) keep working from a bare checkout with no dependencies installed, preserving the documented PYTHONPATH=. python3 -m events fallback — the base dep (q1) must not break the no-install lane [challenge pass / hidden-dependencies lens: CLAUDE.md no-install fallback + q1 decision]
- The sub-millisecond enqueue assertion runs as a marked perf test with generous headroom or outside the default CI selection: a wall-clock timing assertion on shared CI runners will eventually flake, and c16 forbids flaky inputs to the Sonar gate [challenge pass / counter-evidence lens: h3 vs c16]

## Non-goals

- Issue #1 non-goals hold for this arc: no reimplementing MQTT, no multiple broker technologies, no Kubernetes, no GUI, no arbitrary user code inside the Events service, no general-purpose workflow engine, no exactly-once guarantees
- No bespoke history store is built from scratch: eidetic-cli and data-refinery-cli already own the memory/storage lane and are evaluated first before any control-service persistence beyond mosquitto state lands
- No WebSocket listener in the first slice: nova exposed 9001 for debugging; the new stack does not, and websockets return (if ever) via the #7 streaming lane as an explicit opt-in [challenge pass / adjacent-systems lens: nova mosquitto.conf 9001 + traffic probe]

## Assumptions

- The raw MQTT port stays a first-class documented surface for co-located latency-sensitive producers; producer-owned topic trees (reachy/events/{source}/{type}, retained reachy/state/{key}) are never forced through envelope validation or pipelines; anonymous auth on loopback is acceptable for this slice (issue #3)
- The contract states consumer-side idempotency keyed on event id as a requirement, because QoS 1 is at-least-once and exactly-once is a non-goal; retained messages are documented as last-value, not history — history lives in the control-service store
- events up shells out to docker via stdlib subprocess for this slice: shell-cli, which owns the confinement lane, is scaffold-only today (no operation, policy or runner built), so routing through it is deferred and documented rather than silently skipped
- Retained-state durability has a bound: clean broker shutdown (SIGTERM via docker stop/restart) persists retained state, but an uncleanly killed broker may lose writes since the last autosave — the generated mosquitto.conf sets autosave_interval explicitly and the docs state the bound instead of implying retained state is unconditionally durable [challenge pass / failure-modes lens: persistence semantics, to be verified by the kill-broker integration test]

## Scope exploration

- `s1` — `issue #1 (events-cli scaffold spec)`: the requirements baseline: CloudEvents envelope with tracing fields, declarative pipeline model, stack verbs, security defaults, 14 acceptance criteria and 7 explicit non-goals; design principle 'Mosquitto transports events, events-cli defines what they mean'
  - seeds: `c2`, `c4`, `c14`
- `s2` — `issue #2 (build brief: agentfront binding, watch constraint, mesh lanes)`: agentfront is a named requirement #1 omits — one App registry, three derived surfaces, the import lane fed from the same core; watch cannot fit MCP/HTTP request-response, so durable subscriptions + bounded cursor drain must be designed with history; lane boundary vs culture must be stated; eidetic-cli/data-refinery-cli own the store lane; Docker must not become a unit-test dependency
  - seeds: `c6`, `c8`, `c10`, `c13`, `c15`, `c16`
- `s3` — `issue #3 + operator amendment comment (2026-07-23)`: reachy-mini-cli imports events-cli's client surface instead of paho — O(1) enqueue, retained, LWT, QoS 0, never-raises, pure wheel py>=3.12 on PyPI as their base dep; raw 1883 stays first-class for co-located producers; loopback-only compose mapping mandated; nova stack replacement is the operator's decided migration
  - seeds: `c3`, `c5`, `c9`
- `s4` — `spark-f8a9 live state (docker ps, ss -ltn, hostname)`: this box IS spark-f8a9; nova-mosquitto (eclipse-mosquitto:2) and nova-nervous-system run now with 1883 and 9001 bound on 0.0.0.0 — the exact exposure #3 forbids; eidetic-mongo/neo4j and the model-gear stack share the box, so the migration must touch only the nova pair
  - seeds: `c5`
- `s5` — `reachy_nova docker-compose.nervous-system.yml + config/mosquitto/mosquitto.conf`: the stack being replaced uses a bare 1883:1883 mapping, allow_anonymous true, a websockets listener on 9001, persistence true with named volumes, and a mosquitto_sub healthcheck — the persistence+healthcheck patterns are worth carrying into the generated compose, the port mapping and exposure are not
  - seeds: `c4`, `c5`
- `s6` — `pyproject.toml + .github/workflows/tests.yml`: runtime dependencies = [] today; dev group pins teken>=0.8 and CI runs 'uv run teken cli doctor . --strict' plus pytest+coverage+SonarCloud on every PR and a version-check job that blocks unbumped PRs — the teken->agentfront migration touches dev dep, lockfile and CI step together
  - seeds: `c7`, `c16`
- `s7` — `agentfront checkout (README.md, agentfront/app.py, agentfront/testing)`: importable runtime at 0.20.0 (installed tool is 0.18.0): App.tool/add_docs_dir feed one registry; app.cli()/http_app()/mcp_server() derive the surfaces; MCP behind the [mcp] extra raising a named ModuleNotFoundError without it; agentfront.testing.assert_surfaces_agree makes surface-agreement testable
  - seeds: `c6`
- `s8` — `events/cli package + explain catalog (per CLAUDE.md)`: the scaffold's CLI contract is marked stable: single parser with lazy register(), `_output.py` stdout/stderr split, `_errors.py` exit codes, catalog mapping BOTH ('events',) and ('events-cli',) to root with tests pinning them — the binding must preserve all of it or the rubric gate fails
  - seeds: `c12`
- `s9` — `shell-cli README`: shell-cli is the confinement lane owner but is scaffold-only: 'no operation, environment, policy, or runner has been built yet' — events up cannot route docker invocations through it today, so stdlib subprocess with a documented revisit note is the honest posture
  - seeds: `c11`
- `s10` — `reachy-mini-cli spec (docs/specs/2026-07-23-reachy-nervous-system.md) + docs/export-schema.md`: the consumer codes against an injected client seam, binds the real import in one composition line once the first events-cli wheel ships (base dep + uv lock same change), reads REACHY_MQTT_URL defaulting localhost:1883, ships no compose of its own, and never carries media payloads in events
  - seeds: `c3`, `c9`
- `s11` — `eidetic recall (--scope events-cli)`: no prior events-cli implementation decisions in memory — only eidetic's own roadmap surfaced; the three issues genuinely are the whole requirements baseline
- `s12` — `challenge pass / adjacent-systems lens: live nova-mosquitto traffic (wildcard + $SYS probe)`: 2 connected clients (this probe + nova-nervous-system), zero live event traffic in an 8s window, only two retained nova/state/* values — nothing else on the box consumes the broker, so the migration orphans only the nova pair's own state
  - seeds: `c5`
- `s13` — `challenge pass / hidden-dependencies lens: PyPI namespace (pypi.org/pypi/events/json)`: PyPI 'Events' 0.5 owns import events; events-cli 0.7.0 ships the same top-level module — collision confirmed by probe, routed as q5 (user decision: rename vs document+guard)
- `s14` — `challenge pass / operations lens: docker compose + host tooling probes`: Docker Compose v5.0.1 plugin present on spark-f8a9; host has no mosquitto_pub/mosquitto_sub, so issue #3's pub/sub acceptance round-trip runs via docker exec into the broker container (as this pass's probe did)
  - seeds: `c4`
- `s15` — `challenge pass / failure-modes lens: localhost resolution (getent ahosts)`: localhost resolves to 127.0.0.1 only on spark-f8a9, so the v4-only compose mapping cannot silently no-op a localhost-dialing client on this box; on dual-stack boxes a v6-first localhost could — client docs should prefer explicit 127.0.0.1
  - seeds: `c9`
- `s16` — `challenge pass / concurrency lens: co-located client set`: robot publisher + reTerminal bridge + ad-hoc CLI sessions share one loopback broker; MQTT client-id takeover would make same-id clients kick each other — seeded the unique-client-id requirement
  - seeds: `c29`
- `s17` — `challenge pass / reversibility lens: nova rollback path`: reachy_nova's docker-compose.nervous-system.yml remains on disk after the stack is stopped, so a one-command rollback exists during the cutover window — seeded the documented-rollback requirement
  - seeds: `c28`
- `s18` — `challenge pass / security lens: compose mapping + upstream defaults`: c4/c9 examined against the live anti-pattern and Mosquitto 2.0 defaults; clean beyond the exact-version-pin finding — the loopback mapping, anonymous-on-loopback posture and no-listener defaults hold as specified
  - seeds: `c27`

## Decisions

- paho-mqtt >=2,<3 is a BASE dependency: pip install events-cli carries the publish client; the deps=[] posture is deliberately given up (user decision 2026-07-23, q1)
- The hand-rolled argparse CLI stays the CLI surface; the agentfront App registry is fed from the same core and derives MCP + HTTP only, with CLI/registry parity pinned by our own tests (user decision 2026-07-23, q2)
- Pipelines are deferred: this arc ships producer-owned reachy topics only; whether devague is the first pipeline consumer is decided when the pipeline layer is built (user decision 2026-07-23, q3)
- First arc = the #3 unblock slice — envelope core, importable client, stack verbs, nova migration, PyPI release; deferred and tracked on this repo: agentfront MCP/HTTP binding (#6), durable subscriptions + history (#7), pipelines (#8), shell-cli routing for events up (#9), dynsec identities / remote opt-in (#10); issue #1 pipeline acceptance is explicitly NOT met by this slice (user decision 2026-07-23, q4)
- The import package is renamed events -> events_cli before the first consumer binds (user decision 2026-07-23, resolves q5): PyPI 'Events' 0.5 owns import events, and the cheapest moment to vacate the collision is while zero consumers exist; the console command stays events, the dist stays events-cli
  - instruction: Rename the events/ package dir to events_cli/, update [project.scripts] (events = events_cli.cli:main), hatch packages, coverage source, isort known_first_party, and the no-install fallback docs (python -m events_cli); the explain-catalog keys are command-path names tied to the console command and stay ('events',)/('events-cli',); update the repo description and note the change on issue #2's provisioning record

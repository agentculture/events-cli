# Build Plan — events first slice

slug: `events-first-slice` · status: `exported` · from frame: `events-first-slice`

> events-cli ships its first vertical slice: a loopback-only Mosquitto stack on spark-f8a9 replacing the nova brokers, a CloudEvents envelope core, an importable O(1)-enqueue client on PyPI that unblocks reachy-mini-cli, and agentfront-derived CLI/MCP/HTTP surfaces — issues #1 #2 #3

## Tasks

### t1 — Rename the import package events -> events_cli (user decision c33) with the CLI contract held invariant

- instruction: Mechanical rename, contract-preserving: git mv events/ events_cli/; update pyproject [project.scripts] (events = events_cli.cli:main), [tool.hatch.build.targets.wheel] packages, [tool.coverage.run] source, [tool.isort] known_first_party; update tests/ imports and CLAUDE.md/README no-install fallback to "python -m events_cli". The explain-catalog root keys are COMMAND-path names, not module names — ("events",) and ("events-cli",) both STAY. `_prog.py` compares sys.argv[0] to this package `__main__.py` by full path; verify test_prog_matches_installed_console_script still passes. Do NOT touch `_output.py` or `_errors.py`.
- covers: c12, h9
- acceptance:
  - Console command 'events' and no-install fallback 'python -m events_cli' both pass every introspection verb
  - The full existing test suite, the dual explain-catalog root keys, prog resolution and the rubric gate (cli doctor --strict) pass unchanged
  - pyproject [project.scripts], hatch packages, coverage source and isort known_first_party all reference events_cli; no module named 'events' ships in the wheel

### t2 — Migrate dev dependency and CI gate from teken to agentfront

- instruction: pyproject dev group: replace teken>=0.8 with agentfront>=0.20; regenerate uv.lock (uv sync); .github/workflows/tests.yml step 'afi rubric gate' becomes 'uv run agentfront cli doctor . --strict'; sweep docs/skill-sources.md, .claude/skills.local.yaml.example (sibling_projects) and .markdownlint-cli2.yaml (.teken/** ignore). Do not add agentfront as a RUNTIME dep — the binding itself is deferred to issue #6.
- depends on: t1
- covers: c7, h7
- acceptance:
  - uv.lock resolves agentfront (>=0.20), not teken; CI step is 'uv run agentfront cli doctor . --strict' and passes
  - No repo file outside CHANGELOG references teken: docs/skill-sources.md, .claude/skills.local.yaml.example and .markdownlint-cli2.yaml updated

### t3 — Build the envelope core module (pure, dockerless)

- instruction: New package events_cli/core/ (envelope.py + errors.py): stdlib only, no paho import anywhere in this module. Envelope is immutable (frozen dataclass); validate() raises a typed error listing field-level reasons; to_dict/from_dict round-trip; new_event_id() returns 'evt_' + a collision-resistant suffix. CloudEvents field names per issue #1: id, type, source, time, schemaVersion, data + correlationId, causationId, runId, traceparent, producer, deliveryAttempt. Tests live in tests/test_envelope.py and must run with no docker and no broker.
- depends on: t1
- covers: c2, h2
- acceptance:
  - pytest passes with no docker and no broker installed; the core imports only stdlib
  - Malformed envelopes are rejected with field-level named reasons; a valid envelope round-trips id/type/source/time/schemaVersion/correlationId/causationId/runId unchanged through serialize->parse (property test)
  - Generated event ids are unique and prefixed (evt_)

### t4 — Build the importable publish client on paho-mqtt >=2,<3 (base dep per c22)

- instruction: New module events_cli/client.py wrapping paho-mqtt: add 'paho-mqtt>=2,<3' to [project] dependencies (BASE, per decision c22). LAZY IMPORT — import paho inside the client module only, never from events_cli/__init__.py or the cli package, so the introspection verbs still run with paho absent (prove it with a test that stubs the import out). Use paho CallbackAPIVersion.VERSION2, connect_async + loop_start, will_set BEFORE connect, publish() returns immediately (paho queues internally). Default client_id must be unique per process. Never let a paho exception escape publish()/connect(); expose a readable connection state. Mark the enqueue-latency test with @pytest.mark.perf and give it a generous bound; register the markers in pyproject and exclude perf+stack from the default addopts.
- depends on: t1, t2
- covers: c3, h3, c26, h18, c29, h21, c31, h22
- acceptance:
  - publish() from a non-owner thread is a queue-backed enqueue returning immediately; a marker-gated perf test with a documented generous bound measures it; the default CI selection contains no wall-clock timing assertion
  - With no broker reachable, constructing the client and publishing raises nothing into the caller; connection state is observable; LWT, retained publishes and QoS 0 are exposed
  - Default client ids are unique per process (two default-constructed clients connect concurrently without kicking each other - integration-marked test)
  - A CI-run test exercises the introspection verbs with paho-mqtt uninstalled and they pass (lazy import)

### t5 — Build the stack verbs: init/up/status/logs/down with loopback-only compose

- instruction: New events_cli/cli/_commands/stack.py registering the stack verbs (init/up/status/logs/down) via register(sub) in events_cli/cli/__init__.py at the marked insertion point, plus explain-catalog entries for every new path. Templates live in events_cli/stack/templates/. Compose MUST contain the literal '127.0.0.1:1883:1883', an exact image tag (verify the tag exists), persistence true + named volume, a healthcheck, and NO 9001 websocket listener. mosquitto.conf comments name which properties are upstream defaults for that exact version. 'up' preflight: if something already listens on 1883 that we did not start, refuse with exit code 2 and print the exact stop command. docker compose is invoked via stdlib subprocess with a fixed argv list (no shell=True, no user interpolation).
- depends on: t1
- covers: c4, h4, c27, h19
- acceptance:
  - events init writes compose with the literal mapping 127.0.0.1:1883:1883, persistence true plus a named volume, a mosquitto_sub healthcheck, an exact mosquitto version tag, and no websocket listener; a unit test fails if the tag is a bare floating major
  - The generated mosquitto.conf comments state which security properties are upstream defaults for the pinned version
  - events up preflight detects a foreign broker on 1883 and refuses with the exact stop command; up/down/logs/status drive docker compose via stdlib subprocess and report health truthfully
  - Every new verb has an explain-catalog entry and --json support; the rubric gate stays green

### t6 — Write the contract docs and README lane statement

- instruction: README: add the one-sentence culture-vs-events-cli lane statement citing issue #2, and a Contract section covering the raw-MQTT-port-is-first-class surface, consumer-side idempotency keyed on event id (QoS 1 is at-least-once), and retained-as-last-value-not-history. New docs/contract.md carries the long form. Cite reachy-mini-cli's spec and events-cli#3 for the blocked-consumer chain. markdownlint-cli2 must pass.
- depends on: t1
- covers: c13, h10, c20, h15
- acceptance:
  - README states the two-lane boundary in one sentence (culture = agent conversation, events-cli = machine events) citing issue #2
  - Docs state the raw MQTT port as a first-class co-located producer surface, consumer-side idempotency keyed on event id as a contract requirement, and retained-as-last-value-not-history
  - The why/traceability chain is recorded: reachy-mini-cli's broker-dependent acceptance waits on this slice, cited to their spec and events-cli#3

### t7 — Write and execute the nova migration runbook with rollback on spark-f8a9

- instruction: docs/migrations/2026-07-nova-to-events-cli.md: record the captured before-state evidence verbatim (ss -ltn showing 0.0.0.0:1883 and 0.0.0.0:9001, docker ps showing nova-mosquitto + nova-nervous-system, allow_anonymous true in reachy_nova/config/mosquitto/mosquitto.conf), the forward cutover, the exact one-command rollback (docker compose -f /home/spark/git/reachy_nova/docker-compose.nervous-system.yml up -d), and the SERVICE WINDOW warning: between stopping nova-nervous-system and reachy-mini-cli shipping its new publisher, no nervous system runs on the robot (plan risk r3). AUTHOR the runbook only — the live cutover on spark-f8a9 is executed by the main agent, not a task agent.
- depends on: t4, t5
- covers: c5, h5, c28, h20, c19, h14
- acceptance:
  - The runbook records the captured before-state evidence (0.0.0.0:1883/9001, allow_anonymous true, the nova pair) and the exact one-command rollback via reachy_nova's compose file
  - The cutover is exercised forward-back-forward on spark-f8a9 before being called done
  - After cutover: docker ps shows exactly one broker, nova-mosquitto and nova-nervous-system are stopped, ss -ltn shows no 0.0.0.0 binding on 1883/9001, and the eidetic/model-gear/qq neighbours are untouched

### t8 — Build the stack-marked integration suite (docker kept out of the unit gate)

- instruction: tests/test_stack_integration.py under @pytest.mark.stack (excluded from the default addopts selection, so Sonar coverage never depends on a broker). Cover: retained state across docker restart, retained-state bound after docker kill (unclean — this MEASURES the h17/park-v2 assumption; if it contradicts the autosave assumption, report it rather than editing the claim), two default-constructed clients connected concurrently, and a pub/sub round-trip run via docker exec mosquitto_pub/mosquitto_sub because the HOST has no mosquitto client tools. Also add a test proving the default selection passes with no docker present.
- depends on: t3, t4, t5
- covers: c16, h11
- acceptance:
  - The default pytest selection passes on a machine with no docker and no broker; CI proves it on every PR; Sonar coverage comes from the default selection only
  - Integration tests carry a 'stack' marker excluded by default and cover: retained state across docker restart, retained-state bound after an unclean broker kill (h17), the two-default-clients concurrency case, and a docker-exec pub/sub round-trip (host has no mosquitto tools)

### t9 — Release to PyPI and run issue #3 acceptance end-to-end on spark-f8a9

- instruction: SPLIT BY THE PR GATE: everything except the actual release is authored pre-merge — scripts/acceptance-issue-3.sh (the runnable #3 checklist) and the draft notification for issue #3. The PyPI release happens when the human merges the PR (push to main publishes via Trusted Publishing), so the fresh-wheel install, the end-to-end acceptance run on spark-f8a9 and the issue-#3 comment naming the released version are POST-MERGE steps executed by the main agent. Record this split as a deviation if it changes anything material.
- depends on: t2, t3, t4, t5, t6, t7, t8
- covers: c1, h1, c17, h12, c18, h13, c21, h16
- acceptance:
  - A version-bumped release is live on PyPI; a fresh install of the released wheel plus events init/up yields the working loopback broker with no manual steps outside the documented verbs
  - Issue #3's checklist passes: 127.0.0.1-only binding, retained+LWT+QoS0 round-trip, credential-less loopback connect, nova pair absent, and a co-located process importing the released wheel publishes one retained message and one QoS 0 event with a sub-millisecond measured enqueue
  - reachy-mini-cli is notified on issue #3 that the composition unblock (import events_cli) is live, naming the release version

### t10 — Record the deferred-arc contracts and true up CLAUDE.md

- instruction: Post comments on issues #6 and #7 carrying their confirmed spec contract (claims c6/h6 and c8/h8 from the exported spec) so the deferred requirements are durable outside frame state; sign as events-cli (Claude). Then true up CLAUDE.md to the repo as it exists after this arc: events_cli import package, paho base dep + the lazy-import boundary, the stack verbs, the perf/stack markers, and a Roadmap pointing at issues 6-10. Update the Known drift section (the teken drift is fixed by t2).
- depends on: t3, t4, t5
- covers: c6, h6, c8, h8
- acceptance:
  - Issues #6 and #7 each carry a comment with the confirmed spec contract for their arc (registry-fed-from-core + parity + bounded tools for #6; identity-owned subscriptions + cursor drain + non-infinite defaults + history designed together for #7) so the deferred requirements are durable outside frame state
  - CLAUDE.md describes the repo as it now exists: events_cli import package, paho base dep with the lazy-import boundary, stack verbs, integration markers, and the arc roadmap pointing at #6-#10

## Risks

- [unknown_nonblocking] Every merge to main publishes to PyPI, so intermediate versions may ship a partial slice; the client-API release that unblocks reachy-mini-cli must be explicitly named on issue #3 (t9) so they pin >= it
- [unknown_nonblocking] Mosquitto autosave/persistence semantics were asserted from general knowledge (frame park v2); t8's unclean-kill test is the bound - if it contradicts the assumption, the generated conf and docs change, not the claim silently
- [unknown_nonblocking] Between stopping nova-nervous-system (t7) and reachy-mini-cli shipping its new publisher, no nervous system runs on the robot; the operator times the cutover deliberately - the runbook must say this window exists

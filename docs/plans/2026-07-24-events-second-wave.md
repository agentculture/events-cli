# Build Plan — events second wave

slug: `events-second-wave` · status: `exported` · from frame: `events-second-wave`

> events-cli ships its second wave: the deferred arcs land on the first slice — the agentfront MCP+HTTP binding from the core registry (#6), durable identity-owned subscriptions with history and bounded cursor drain (#7), pipelines over the event graph (#8), shell-cli-routed stack verbs (#9), and dynsec identities with a documented remote-access opt-in (#10) — with issue #3 answered and closed

## Tasks

### t1 — Reply to issue #3 naming the client symbol and subscribe story, then close it

- covers: c2, h7
- acceptance:
  - The comment on #3 states the import path 'from events_cli import EventClient', the constructor signature with defaults (host='127.0.0.1', port=1883, client_id auto-unique per process, connect=True so connect() runs in the constructor), and that the reTerminal bridge subscribes over raw loopback MQTT until #7's drain ships; signed '- events-cli (Claude)'
  - Issue #3 is closed after the reply, or left open with the operator's stated reason in the same thread

### t2 — Fix the pyproject description drift and carry the arc's version bump

- covers: c8, h11
- acceptance:
  - The [project] description no longer claims HTTP or MCP surfaces exist; the reword rides the arc's first PR with a version-bump-skill bump and CHANGELOG entry; agentfront cli doctor --strict still passes

### t3 — Run the #9 shell-cli evaluation and record the verdict on the issue

- covers: c3, h8
- acceptance:
  - Issue #9 carries a comment recording the verdict (migrate or stay on stdlib subprocess) with concrete reasons referencing shell-cli 0.13.x's shipped surface (process.exec/process.shell, policy gate, evidence records); if 'stay', the revisit conditions are stated

### t4 — Canonical topic mapping module: type-to-topic and pattern-to-filter, pure

- covers: c20, h5
- acceptance:
  - events_cli/core/topics.py maps task.requested -> events/task/requested and compiles task.* -> events/task/+ with documented wildcard semantics; topic-to-type round-trips; a test proves reachy/# and reachy/state/# are never matched by contract-lane filters; stdlib-only, tests dockerless

### t5 — Store evaluation spike: data_refinery.store vs eidetic-cli vs minimal fallback

- acceptance:
  - A written verdict (docs/ note + issue #7 comment) evaluates both siblings against: ordered append with store-assigned sequence, read-since-cursor, get-by-id, list-by-type, restart survival, dependency posture; it names the chosen backend and the rejection reasons; a bespoke-minimal JSONL log is chosen only if both fail the criteria with reasons recorded

### t6 — History store seam with store-assigned per-subscription cursor

- depends on: t5
- covers: c5, h9, c28, h24
- acceptance:
  - events_cli/history/ exposes append(envelope, sub)->seq, read(sub, since, max), get(id), list(type, max) behind a backend seam implementing t5's verdict; the sequence is store-assigned and monotonic per subscription; append dedupes on envelope id
  - A test mints two envelope ids in the same millisecond and proves read order equals append order and cursor resume is exact — no ordering derived from ULID comparison; all tests dockerless
  - The store lives beside the per-host stack state (never CWD-relative; env-overridable) and every record carries a store-format version field; 'events list' returns identical results from two different CWDs against the same host stack (test)

### t7 — Subscription registry and MQTT persistent-session lifecycle

- depends on: t4, t6
- covers: c17, c21, h6, c29, h25
- acceptance:
  - Subscription records carry name, pattern, owner (default culture.yaml nick, fallback client id), created; 'events sub list --json' shows owner; the record schema needs no migration when #10 adds dynsec (identity name == owner name)
  - sub add establishes an MQTT persistent session (clean_start=false, QoS 1 subscribe on the compiled filter, session expiry set to the documented value) and disconnects leaving the session live in the broker; sub remove destroys the session; paho stays lazily imported; lifecycle unit-tested against a fake client, dockerless
  - Subscription names must be slugs and patterns must match the dotted-type grammar ('*' only): '#', '+', '/', '..' and empty are rejected with field-level errors (dockerless tests); compiled filters are always events/-prefixed

### t8 — Drain engine: resume, consume bounded, persist-then-ack, return cursor

- depends on: t7
- covers: c18, h3
- acceptance:
  - The drain resumes the session with manual acknowledgement, persists each event to the history store BEFORE acking it, stops at --max or --timeout, and returns the batch plus the next cursor; broker-unreachable surfaces as a named error mappable to CliError exit 2
  - Drain/cursor logic is tested pure and dockerless with a fake client; the live-broker path is deferred to the stack-marked suite

### t9 — CLI verbs: events sub add/list/show/remove and events watch

- depends on: t8
- covers: c18
- acceptance:
  - All verbs registered via register(sub) with catalog entries and --json; watch defaults are --max 100 --timeout 30 and no --follow flag exists; errors are CliError with remediation; the rubric gate and pinned contract tests pass unchanged

### t10 — CLI verbs: events emit, events get, events list

- depends on: t9
- covers: c19, c27
- acceptance:
  - emit <type> --data <file|-> validates via the core envelope (generated id/time) and publishes QoS 1 to the canonical topic via EventClient, rejecting an invalid envelope with field-level errors before any publish; get <event-id> and list --type --max read drained history from the store; all three carry catalog entries and --json
  - EventClient.publish_event defaults to qos=1 (behaviour change per resolved q3: CHANGELOG-noted, regression-tested; reachy's publish() raw lane unaffected); 'events emit' asserts qos=1; client docstrings state QoS 0 bypasses durable capture

### t11 — Template and contract docs: backlog bound and the consume side

- depends on: t4
- covers: c10, h2, c24, h20, c25, h23
- acceptance:
  - The generated mosquitto.conf sets max_queued_messages explicitly with a comment stating the undrained-backlog bound; template tests updated; events init --force regenerates cleanly
  - docs/contract.md documents the consume side: the canonical topic mapping, the backlog bound, cursor-drain semantics, and the built/not-built table rows flip for sub/watch/emit/get/list in the same PR that adds them
  - docs/contract.md gains the capture-boundary section (registered-subscription QoS-1 traffic only; no global log until #8), documents drop-newest overflow at the explicit max_queued_messages bound with the 'events logs' notice pointer, and single-drainer/takeover semantics

### t12 — Stack-marked integration suite for the persistent-session architecture

- depends on: t9, t10, t11
- covers: h2, h3, h4, c14, h15, c23, h21, c26, h22
- acceptance:
  - Stack-marked tests prove: publish N envelopes with no drainer, docker restart of the broker, then drain returns all N in order; a drain returns within --timeout with at most --max events plus a cursor and resuming re-delivers nothing acknowledged; two CLI processes complete an emit-to-watch round-trip; a reachy/* publish is not captured by a contract-lane subscription
  - The default pytest selection still passes on a machine with no docker and no broker
  - The suite asserts its own isolation (own compose project/port/volume/stack dir; events-mosquitto uptime unbroken by a full run); an overflow test proves exactly-bound oldest-first delivery; a takeover test proves the store holds every event exactly once after concurrent drains; a capture-boundary test proves pre-registration events are absent from drains

### t13 — Acceptance gate script and live run on spark-f8a9

- depends on: t12
- covers: c1, h1, c13, h14, c15, h16, c16, h17, h19
- acceptance:
  - scripts/acceptance-second-wave.sh exits non-zero on any failing check and covers every success-signal item; the live run on spark-f8a9 is recorded in docs/acceptance/ with observable output per item
  - The record captures the before-state evidence (no consume verbs or store code at pre-arc HEAD) and marks #6/#8/#9/#10 deferred with issue links — the wave claim stays arc-by-arc honest
  - The live template rollout on spark-f8a9 is a recorded service window with a rehearsed rollback in docs/acceptance/, following the first slice's cutover discipline

### t14 — Deferred-arc hygiene ledger at arc close

- depends on: t13
- covers: c6, h10, c9, h12, h13, c22, h18
- acceptance:
  - Issues #6/#8/#10 remain open with their contracts intact and the arc's diff contains no agentfront-binding, pipeline, dynsec or SSE code; #9 carries its recorded verdict
  - Every PR of the arc passed the rubric gate and the pinned contract tests, and touched docs/contract.md whenever it added a surface; the delivery record notes the dynsec deferred condition (the template comment still binds allow_anonymous removal to #10)

## Risks

- [unknown_nonblocking] mosquitto 2.1.2 session-expiry semantics: paho must send a session-expiry-interval on CONNECT and the broker may cap it (max_session_expiry_interval); if effectively-infinite sessions are capped, sub add re-arms expiry on each drain and the real bound is documented — verify live in t12 (task t7)
- [unknown_nonblocking] paho v2 manual-ack behaviour for QoS 1 persist-then-ack: verify the client exposes it as the drain design assumes; the fallback is a documented store-then-ack discipline inside on_message (task t8)
- [unknown_nonblocking] Closing #3 may wait on reachy-mini-cli acknowledging the reply; the arc does not gate on their response (task t1)
- [follow_up] --follow streaming and the SSE lane return at #6-time (parked on the frame as v2/v3)

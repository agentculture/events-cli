# Build Plan — events second wave

slug: `events-second-wave` · status: `exported` · from frame: `events-second-wave`

> events-cli ships its second wave: the deferred arcs land on the first slice — the agentfront MCP+HTTP binding from the core registry (#6), durable identity-owned subscriptions with history and bounded cursor drain (#7), pipelines over the event graph (#8), shell-cli-routed stack verbs (#9), and dynsec identities with a documented remote-access opt-in (#10) — with issue #3 answered and closed

## Tasks

### t1 — Reply to issue #3 naming the client symbol and subscribe story, then close it

- instruction: Post one comment on #3 answering both asks verbatim from code on main: import path 'from events_cli import EventClient' (PEP 562 lazy re-export in events_cli/__init__.py; events_cli.client defines it), constructor EventClient(host='127.0.0.1', port=1883, *, client_id=None->auto-unique, connect=True so connect() runs in the constructor), will/availability_topic kwargs for LWT, and: the reTerminal bridge subscribes over raw loopback MQTT until #7's drain ships. Let the communicate-skill script sign it; then close the issue (or record the operator's leave-open reason in-thread).
- covers: c2, h7
- acceptance:
  - The comment on #3 states the import path 'from events_cli import EventClient', the constructor signature with defaults (host='127.0.0.1', port=1883, client_id auto-unique per process, connect=True so connect() runs in the constructor), and that the reTerminal bridge subscribes over raw loopback MQTT until #7's drain ships; signed '- events-cli (Claude)'
  - Issue #3 is closed after the reply, or left open with the operator's stated reason in the same thread

### t2 — Fix the pyproject description drift and carry the arc's version bump

- instruction: Use the version-bump skill (minor — the arc adds verbs); reword [project] description in pyproject.toml to today's truth (Dockerised Mosquitto stack + agent-first CLI + importable client; agentfront MCP/HTTP tracked as #6), keeping the three-name split note intact; CHANGELOG entry under Changed; agentfront cli doctor --strict must still pass
- covers: c8, h11
- acceptance:
  - The [project] description no longer claims HTTP or MCP surfaces exist; the reword rides the arc's first PR with a version-bump-skill bump and CHANGELOG entry; agentfront cli doctor --strict still passes

### t3 — Run the #9 shell-cli evaluation and record the verdict on the issue

- instruction: Read shell-cli's CHANGELOG 0.13.x and shell/cli/_commands/{operation,policy}.py; weigh plan/authorize/execute/observe/record + evidence records against stack.py's fixed, non-user-interpolated argv via events_cli/stack/_docker.run; the likely verdict is 'stay, revisit when a container runner lands' — but write whichever the evidence supports on #9, with the revisit conditions, via the communicate skill
- covers: c3, h8
- acceptance:
  - Issue #9 carries a comment recording the verdict (migrate or stay on stdlib subprocess) with concrete reasons referencing shell-cli 0.13.x's shipped surface (process.exec/process.shell, policy gate, evidence records); if 'stay', the revisit conditions are stated

### t4 — Canonical topic mapping module: type-to-topic and pattern-to-filter, pure

- instruction: New stdlib-only module events_cli/core/topics.py: type_to_topic (task.requested -> events/task/requested), topic_to_type (inverse), pattern_to_filter (task.* -> events/task/+; '*' matches exactly one dotted segment); reuse core.envelope's type validation for segments; raw MQTT filter chars in a pattern raise the core FieldError shape; tests/test_topics.py dockerless including the reachy/# exclusion proof
- covers: c20, h5
- acceptance:
  - events_cli/core/topics.py maps task.requested -> events/task/requested and compiles task.* -> events/task/+ with documented wildcard semantics; topic-to-type round-trips; a test proves reachy/# and reachy/state/# are never matched by contract-lane filters; stdlib-only, tests dockerless

### t5 — Store evaluation spike: data_refinery.store vs eidetic-cli vs minimal fallback

- instruction: Read data_refinery.store source (put/get/list/migrate, files backend) and eidetic-cli's remember/recall surface; score each against: ordered append + store-assigned per-sub sequence, read-since-cursor, get-by-id, list-by-type, restart survival, base-dependency posture; write the verdict doc (docs/ + a comment on #7); write NO store code in this task
- acceptance:
  - A written verdict (docs/ note + issue #7 comment) evaluates both siblings against: ordered append with store-assigned sequence, read-since-cursor, get-by-id, list-by-type, restart survival, dependency posture; it names the chosen backend and the rejection reasons; a bespoke-minimal JSONL log is chosen only if both fail the criteria with reasons recorded

### t6 — History store seam with store-assigned per-subscription cursor

- instruction: New package events_cli/history/ implementing t5's verdict behind one seam module; store root defaults beside the per-host stack state (follow default_stack_dir's XDG pattern with an env override), never CWD; records carry storeFormatVersion from the first write; append(envelope, sub) assigns the monotonic per-sub seq and dedupes on envelope id; include the same-millisecond ULID ordering test (h9)
- depends on: t5
- covers: c5, h9, c28, h24
- acceptance:
  - events_cli/history/ exposes append(envelope, sub)->seq, read(sub, since, max), get(id), list(type, max) behind a backend seam implementing t5's verdict; the sequence is store-assigned and monotonic per subscription; append dedupes on envelope id
  - A test mints two envelope ids in the same millisecond and proves read order equals append order and cursor resume is exact — no ordering derived from ULID comparison; all tests dockerless
  - The store lives beside the per-host stack state (never CWD-relative; env-overridable) and every record carries a store-format version field; 'events list' returns identical results from two different CWDs against the same host stack (test)

### t7 — Subscription registry and MQTT persistent-session lifecycle

- instruction: New package events_cli/subs/: registry records live in the history store dir (from t6); lifecycle over lazy paho import — MQTT5, clean_start=False, CONNECT SessionExpiryInterval=0xFFFFFFFF, QoS 1 subscribe on the compiled filter, then graceful disconnect leaving the session live; sub remove connects clean_start=True (or expiry 0) to destroy it; owner default = culture.yaml nick via the whoami line scanner, fallback client id; name/pattern validation per c29 rejecting #,+,/ with field-level errors; fake-client unit tests only
- depends on: t4, t6
- covers: c17, c21, h6, c29, h25
- acceptance:
  - Subscription records carry name, pattern, owner (default culture.yaml nick, fallback client id), created; 'events sub list --json' shows owner; the record schema needs no migration when #10 adds dynsec (identity name == owner name)
  - sub add establishes an MQTT persistent session (clean_start=false, QoS 1 subscribe on the compiled filter, session expiry set to the documented value) and disconnects leaving the session live in the broker; sub remove destroys the session; paho stays lazily imported; lifecycle unit-tested against a fake client, dockerless
  - Subscription names must be slugs and patterns must match the dotted-type grammar ('*' only): '#', '+', '/', '..' and empty are rejected with field-level errors (dockerless tests); compiled filters are always events/-prefixed

### t8 — Drain engine: resume, consume bounded, persist-then-ack, return cursor

- instruction: Drain module under events_cli/subs/: paho Client(manual_ack=True) (probe-verified in paho 2.x) with manual_ack_set; persist each message to the history store BEFORE client.ack(); stop at --max or a monotonic-clock --timeout deadline; return batch + next cursor from the store's seq; broker-unreachable raises a named SubsError the CLI maps to exit 2; fake-client tests dockerless — live-broker paths belong to t12
- depends on: t7
- covers: c18, h3
- acceptance:
  - The drain resumes the session with manual acknowledgement, persists each event to the history store BEFORE acking it, stops at --max or --timeout, and returns the batch plus the next cursor; broker-unreachable surfaces as a named error mappable to CliError exit 2
  - Drain/cursor logic is tested pure and dockerless with a fake client; the live-broker path is deferred to the stack-marked suite

### t9 — CLI verbs: events sub add/list/show/remove and events watch

- instruction: New modules events_cli/cli/_commands/sub.py and watch.py; register both at the marked insertion point in cli/__init__.py; every path gets an explain-catalog entry (test_every_catalog_path_resolves enforces); watch defaults --max 100 --timeout 30, no --follow; SubsError -> CliError with remediation hints naming the exact next command via _prog; run agentfront cli doctor . --strict before calling it done
- depends on: t8
- covers: c18
- acceptance:
  - All verbs registered via register(sub) with catalog entries and --json; watch defaults are --max 100 --timeout 30 and no --follow flag exists; errors are CliError with remediation; the rubric gate and pinned contract tests pass unchanged

### t10 — CLI verbs: events emit, events get, events list

- instruction: New modules emit.py + get.py + list.py under _commands with catalog entries; emit validates via Envelope.from_dict FIRST (field-level errors, no publish on failure) then publishes qos=1 via EventClient and prints the PublishResult; get/list read the history store; ALSO in this task: flip client.py publish_event default to qos=1 (resolved q3) with a regression test and a CHANGELOG behaviour note; state in client docstrings that QoS 0 bypasses durable capture
- depends on: t9
- covers: c19, c27
- acceptance:
  - emit <type> --data <file|-> validates via the core envelope (generated id/time) and publishes QoS 1 to the canonical topic via EventClient, rejecting an invalid envelope with field-level errors before any publish; get <event-id> and list --type --max read drained history from the store; all three carry catalog entries and --json
  - EventClient.publish_event defaults to qos=1 (behaviour change per resolved q3: CHANGELOG-noted, regression-tested; reachy's publish() raw lane unaffected); 'events emit' asserts qos=1; client docstrings state QoS 0 bypasses durable capture

### t11 — Template and contract docs: backlog bound and the consume side

- instruction: Edit stack/templates/mosquitto.conf: add max_queued_messages (explicit value; comment states drop-NEWEST overflow citing the 2026-07-24 probe) next to the persistence block; update tests/test_stack_templates.py; docs/contract.md: canonical topic mapping, the capture boundary (registered QoS-1 traffic only, no global log until #8), overflow + 'events logs' notice pointer, single-drainer/takeover semantics, and flip the built/not-built rows for sub/watch/emit/get/list
- depends on: t4
- covers: c10, h2, c24, h20, c25, h23
- acceptance:
  - The generated mosquitto.conf sets max_queued_messages explicitly with a comment stating the undrained-backlog bound; template tests updated; events init --force regenerates cleanly
  - docs/contract.md documents the consume side: the canonical topic mapping, the backlog bound, cursor-drain semantics, and the built/not-built table rows flip for sub/watch/emit/get/list in the same PR that adds them
  - docs/contract.md gains the capture-boundary section (registered-subscription QoS-1 traffic only; no global log until #8), documents drop-newest overflow at the explicit max_queued_messages bound with the 'events logs' notice pointer, and single-drainer/takeover semantics

### t12 — Stack-marked integration suite for the persistent-session architecture

- instruction: Extend tests/test_stack_integration.py behind the stack marker: the suite builds its own stack in a temp EVENTS_STACK_DIR with its own compose project name, a port other than 1883, and its own volume, and asserts events-mosquitto's container uptime is unchanged around the run; add the overflow test (publish bound+K offline, drain exactly bound oldest-first), the takeover test (two concurrent drains, store holds every event exactly once), and the capture-boundary test (pre-registration event absent from a drain); default selection must stay green with no docker
- depends on: t9, t10, t11
- covers: h2, h3, h4, c14, h15, c23, h21, c26, h22
- acceptance:
  - Stack-marked tests prove: publish N envelopes with no drainer, docker restart of the broker, then drain returns all N in order; a drain returns within --timeout with at most --max events plus a cursor and resuming re-delivers nothing acknowledged; two CLI processes complete an emit-to-watch round-trip; a reachy/* publish is not captured by a contract-lane subscription
  - The default pytest selection still passes on a machine with no docker and no broker
  - The suite asserts its own isolation (own compose project/port/volume/stack dir; events-mosquitto uptime unbroken by a full run); an overflow test proves exactly-bound oldest-first delivery; a takeover test proves the store holds every event exactly once after concurrent drains; a capture-boundary test proves pre-registration events are absent from drains

### t13 — Acceptance gate script and live run on spark-f8a9

- instruction: Model scripts/acceptance-second-wave.sh on acceptance-issue-3.sh: per-check observable output, non-zero exit on any failure, covering every success-signal item including cursor-resume and restart-survival; the live run needs an operator-scheduled service window (robot downtime) with the template rollout and a rehearsed rollback; record in docs/acceptance/ with before-state evidence (no consume verbs at pre-arc HEAD) and deferred-arc links
- depends on: t12
- covers: c1, h1, c13, h14, c15, h16, c16, h17, h19
- acceptance:
  - scripts/acceptance-second-wave.sh exits non-zero on any failing check and covers every success-signal item; the live run on spark-f8a9 is recorded in docs/acceptance/ with observable output per item
  - The record captures the before-state evidence (no consume verbs or store code at pre-arc HEAD) and marks #6/#8/#9/#10 deferred with issue links — the wave claim stays arc-by-arc honest
  - The live template rollout on spark-f8a9 is a recorded service window with a rehearsed rollback in docs/acceptance/, following the first slice's cutover discipline

### t14 — Deferred-arc hygiene ledger at arc close

- instruction: Close-of-arc ledger: verify #6/#8/#10 open with contracts intact and grep the arc's full diff for absence of agentfront/pipeline/dynsec/SSE surface; verify #9 carries its verdict and #3 is closed; link the green CI runs proving the rubric gate + pinned tests per PR; confirm docs/contract.md rows flipped in the surface-adding PRs; update CLAUDE.md's as-built sections (consume side now exists) and note the dynsec deferred condition in the delivery record
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

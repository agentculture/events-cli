# Delivery Summary — events second wave

plan: `events-second-wave` · run: `complete` · date: `2026-07-24`
baseline: `devague summary skeleton`

## Intent

Ship the consume half of the event fabric — durable subscriptions, a history
store with a real cursor, and the verbs that drain it — as the arc after the
first slice's publish-only surface. The announcement frame covered all five
deferred arcs (#6–#10); the confirmed build scope was **#7 only**, with the #3
reply, the `pyproject` description fix, and the #9 evaluation riding it.
Fourteen tasks in nine dependency waves, fanned out to isolated worktrees with
TDD-gated merges.

## Planned Work

Quoted verbatim from the `devague summary` skeleton:

- `t1` — Reply to issue #3 naming the client symbol and subscribe story, then close it
- `t2` — Fix the pyproject description drift and carry the arc's version bump
- `t3` — Run the #9 shell-cli evaluation and record the verdict on the issue
- `t4` — Canonical topic mapping module: type-to-topic and pattern-to-filter, pure
- `t5` — Store evaluation spike: data_refinery.store vs eidetic-cli vs minimal fallback
- `t6` — History store seam with store-assigned per-subscription cursor
- `t7` — Subscription registry and MQTT persistent-session lifecycle
- `t8` — Drain engine: resume, consume bounded, persist-then-ack, return cursor
- `t9` — CLI verbs: events sub add/list/show/remove and events watch
- `t10` — CLI verbs: events emit, events get, events list
- `t11` — Template and contract docs: backlog bound and the consume side
- `t12` — Stack-marked integration suite for the persistent-session architecture
- `t13` — Acceptance gate script and live run on spark-f8a9
- `t14` — Deferred-arc hygiene ledger at arc close

## Actual Delivery

All 14 tasks accounted for; all delivered.

| Plan task | Status | What actually landed |
|-----------|--------|----------------------|
| `t1` | delivered | Reply posted on [#3](https://github.com/agentculture/events-cli/issues/3) naming `EventClient`, its import path, constructor and connect-in-constructor semantics; issue **closed**. Went beyond the brief with a Protocol-mismatch table (see Mid-work Decisions). |
| `t2` | delivered | `pyproject` description no longer claims HTTP/MCP surfaces; version 0.9.0 → 0.10.0 with a CHANGELOG entry — commit `0c1e8e1` |
| `t3` | delivered | Evaluation posted on [#9](https://github.com/agentculture/events-cli/issues/9): **stay on `subprocess`**, with four named reopen triggers. Issue left open by design. |
| `t4` | delivered | `events_cli/core/topics.py` — type⇄topic, pattern→filter, 72 tests — commit `4da8a8d` |
| `t5` | delivered | `docs/decisions/2026-07-24-history-store-evaluation.md` — both siblings rejected on measured evidence — commit `9bd11f8` |
| `t6` | delivered | `events_cli/history/` — append-only JSONL, index-as-commit-marker, store-assigned cursor, 92 tests — commit `7fca75d` |
| `t7` | delivered | `events_cli/subs/` registry + `PersistentSession`, 125 tests — commit `4964782` |
| `t8` | delivered | `events_cli/subs/drain.py`, persist→ack, 57 tests — commit `858c1eb`; ordering corrected in `4c78cd3` (see Drift) |
| `t9` | delivered | `events sub add/list/show/remove` + `events watch`, 63 tests — commit `6df270d` |
| `t10` | delivered | `events emit/get/list` + `publish_event` qos flip, 38 tests — commit `9a25d18` |
| `t11` | delivered | `max_queued_messages 1000` in the template; consume side documented in `docs/contract.md` — commit `b89a2e2` |
| `t12` | delivered | 8 stack-marked integration tests + `events_cli/address.py`; **found a real bug** (see Drift) — commit `7c7eb62` |
| `t13` | delivered | `scripts/acceptance-second-wave.sh` + executed live run, **12/12** — commit `8fbe724` |
| `t14` | delivered | `CLAUDE.md` / `CHANGELOG.md` / `docs/contract.md` arc-close pass — commit `3250189` |

## Mid-work Decisions

No `/deviate` records were created during this run (`devague deviate --list` →
*no deviations recorded yet*), so every decision below is captured directly.

- **The history store is bespoke, overriding a standing constraint.** #2 says
  *do not build a store from scratch*. `t5` evaluated both siblings and both
  failed on measured evidence: `data_refinery.store.Envelope` has no sequence
  field and `list()` accepts no `since`/`offset`/`limit`, so the cursor — which
  **is** a store-assigned sequence — was unsatisfiable. Verified independently
  before accepting. The plan's acceptance criterion explicitly reserved this
  both-fail path.
- **`publish_event` flips to `qos=1`** (operator decision q3, resolved during
  `/challenge`). A QoS 0 envelope is never queued for an offline session, so it
  silently bypassed durable capture. `publish()` keeps `qos=0` — that is
  reachy-mini-cli's 50 Hz raw lane.
- **The `culture.yaml` scanner moved to `core/identity.py`** (`t7`). `subs/`
  needs owner resolution, and domain packages import nothing from
  `events_cli.cli`. Re-exported unchanged; `whoami` behaviour identical.
- **`events watch --since` replays history before draining the broker** (`t9`,
  a design decision the plan deliberately left open). If history fills `--max`,
  no broker session is opened at all; the broker drain is floored at the history
  page's cursor so a redelivery cannot repeat a replayed event.
- **A broker-address override was added** (`t12`, prerequisite). `BrokerAddress`
  hardcoded `127.0.0.1:1883` with no override, so the planned CLI round-trip
  test would have pointed at the robot's production broker. `events_cli/address.py`
  is now the single definition; a malformed `EVENTS_BROKER_PORT` is fatal, never
  a silent fallback.
- **`t1` reported a Protocol mismatch the brief did not ask for.** Introspecting
  the shipped client showed reachy-mini-cli's declared Protocol (`connected`,
  `disconnect`, `will_set`, `set_on_connect`) does not match what we ship
  (`is_connected`, `close`, will-at-construction, no `set_on_connect`). Their
  import is total, so each mismatch would have degraded to a silent no-op. Two
  options were offered on the issue; **the choice was left to them**.
- **The independent review was re-run with a larger step budget.** The first
  `ask-colleague review` died at step 12/20; re-running with `--max-steps 45`
  completed and found a real bug (see Drift). Graded 4/5 via the skill's
  feedback loop.

## Drift From Plan

Every divergence from the confirmed contract. No `/deviate` record covers any of
these — they are recorded here exhaustively instead.

| Plan item | Reason for divergence | Classification |
|-----------|-----------------------|----------------|
| `t8` | Shipped `persist → ack → read_back`. An independent `ask-colleague` review found that a read-back failure then leaves the event stored **and** acked but absent from every batch — the store's cursor advancing past an event the broker will never redeliver. Corrected to `persist → read_back → ack` in `4c78cd3`, mutation-tested both ways. t8's own test had pinned the old behaviour as acceptable ("only the batch is lost"), which understated it. | acceptable *(found and fixed within the arc)* |
| `t12` | Expanded beyond an integration suite: fixed a **production bug it uncovered** — `events emit` had never worked against a real broker, because `EventClient` connects asynchronously and the queued message was discarded by the immediate `close()`. No unit test could catch it (all fake the client). Fix adds an opt-in `wait` argument defaulting to `0.0`, so the 50 Hz lane is untouched. | acceptable *(arguably t10's bug; may deserve splitting out)* |
| `t12` | Also added `events_cli/address.py` (broker-address override), which was not in the task contract — but without it the task's own CLI round-trip criterion could only be met by pointing tests at the production broker. | acceptable |
| `t14` | Fixed markdownlint failures in **this arc's own** exported spec and plan (bare `<placeholders>` parsed as inline HTML). The first slice's exports lint clean, so this was new breakage introduced by the `/think` and `/spec-to-plan` legs that would have failed CI. | acceptable |
| `t9`, `t10` | Both flipped their own `docs/contract.md` built/not-built rows, which the plan assigned conceptually to `t11`. Honesty condition h13 requires the doc change to ride the same PR as the surface, so this is the contract being honoured rather than broken. | acceptable |

Scope **not** delivered, and never in this arc's contract: the announcement
frame named #6, #8 and #10, but the confirmed build scope was #7 plus riders.
Those three remain open with their contracts intact — verified at close.

## Evidence

- tests: `uv run pytest -n auto` — **744 passed** (267 at arc start)
- tests: `uv run pytest -m perf` — **1 passed** (the O(1) enqueue bound, proving
  the `wait` argument did not regress the raw lane)
- tests: `EVENTS_STACK_IT=1 uv run pytest -m stack` — **13 passed, 1 skipped**
  (reported by `t12`; the skip is a pre-existing unrelated opt-in)
- lint: `uv run flake8 events_cli tests` — clean; `black`, `isort`, `bandit` clean
- lint: `markdownlint-cli2 "**/*.md" …` — 0 errors
- rubric: `uv run agentfront cli doctor . --strict` — 26/26 PASS
- no-install lane: `PYTHONPATH=. python3 -m events_cli doctor` — exit 0
- commits: `db45e4c..3250189` (27 commits, 55 files, +14105/−134)
- live run: `docs/acceptance/2026-07-24-second-wave-live-run.md` — **12/12**,
  1m46s window on spark-f8a9
- issues: [#3](https://github.com/agentculture/events-cli/issues/3) closed ·
  [#9](https://github.com/agentculture/events-cli/issues/9) verdict recorded ·
  [#6](https://github.com/agentculture/events-cli/issues/6),
  [#8](https://github.com/agentculture/events-cli/issues/8),
  [#10](https://github.com/agentculture/events-cli/issues/10) open, untouched

## Delivery Claims

| Claim | Confidence | Evidence |
|-------|------------|----------|
| A durable subscription's queued backlog survives a broker restart, delivered in order | high | live run 12/12 check 8 · `docs/acceptance/2026-07-24-second-wave-live-run.md` · stack test `…survive_a_broker_restart_in_order` |
| Resuming from a returned cursor repeats nothing already acknowledged | high | live run check 9 · stack test `…resumes_from_its_cursor_without_redelivering` |
| An event is durably stored before it is acknowledged, and read back before that | high | commit `4c78cd3` · mutation-verified: `test_each_event_is_persisted_before_it_is_acknowledged`, `test_a_store_whose_read_path_is_broken_is_a_named_error_not_a_traceback` |
| Ordering never derives from ULID comparison | high | `test_read_order_is_append_order_even_when_the_ulid_order_is_reversed` |
| Producer-owned `reachy/*` trees are never captured by the contract lane | high | live run check 11 · stack test `…producer_owned_topic_trees_are_never_captured` |
| One CLI process sees what another emitted | high | live run check 10 · stack test `…emit_to_watch_round_trip` |
| Overflow drops the **newest** message at the configured bound | high | probe 2026-07-24 (1200 published, oldest 1000 delivered) · stack test `…drops_the_newest_at_the_configured_bound` |
| The 50 Hz raw publish lane is unregressed by the arc | high | `uv run pytest -m perf` passes · `publish()` still defaults `qos=0`, `wait=0.0` |
| The introspection lane still runs with no dependencies installed | high | `PYTHONPATH=. python3 -m events_cli doctor` exit 0 · 5 CI tests with paho blocked |
| `events emit` works against a real broker | high | fixed in `7c7eb62`; live run checks 6 and 10 |
| Concurrent drains leave the store exactly-once | medium | stack test `…store_holding_every_event_exactly_once` — verified on a throwaway broker, not under sustained production load |
| The deployed bound is exactly 1000 in production | medium | live run check 4 confirms the value in the deployed config; the *behaviour* at 1000 is probe-measured, while the stack test asserts shape at a smaller bound |
| A kicked drainer reports its takeover distinguishably | unverified | no named error is raised — it returns a partial batch and burns its timeout (see Remaining Work) |

## Remaining Work / Follow-up

No planned task is incomplete. The items below are gaps and follow-ups this arc
either discovered or deliberately left.

- **Mid-drain takeover is not detected.** A kicked drainer returns a *partial*
  batch and burns its full `--timeout` with `stopped='timeout'` and no named
  error; `t12` observed the broker logging three `session taken over` lines as
  paho auto-reconnects and the two drainers ping-pong. Nothing is lost. Fixing
  it needs an `on_disconnect` seam in `subs/session.py`. — next: own issue.
- **reachy-mini-cli's Protocol does not bind as written.** Two options offered
  on #3; awaiting their choice. If they want compatibility aliases
  (`connected`, `disconnect`, `will_set`, `set_on_connect`), that is a small
  additive change. — next: their call, then a follow-up PR.
- **A malformed payload is permanently discarded** (acked, counted, logged; no
  dead-letter). Documented in `docs/contract.md` at arc close. Revisit if a
  producer needs the bytes back. — next: none unless asked.
- **`has_more` under-reports** (it is `stopped == "max"` only), so a caller
  polling on it alone idles one cycle longer than necessary. — next: minor.
- **A batch may repeat an event under takeover** even though the *store* holds
  it exactly once. Callers dedupe on id. — next: none; documented.
- **#9's verdict has four named reopen triggers**, the likeliest being #10 —
  `mosquitto_ctrl dynsec` takes user-supplied identity names, the first
  genuinely non-fixed argv in this repo. — next: evaluate shell-cli *for that
  surface* when #10 starts.
- **Ten `broker/t*` branches** from the first slice remain, unreachable from
  `main` (PR #11 was squash-merged). Safe to delete. — next: operator's call.
- **#6, #8, #10 remain open** with contracts intact; #1's pipeline acceptance
  criteria are still explicitly unmet. — next: separate arcs.

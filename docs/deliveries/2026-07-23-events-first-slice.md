# Delivery Summary — events first slice

plan: `events-first-slice` · run: `complete` · date: `2026-07-23`
baseline: `devague summary skeleton`

## Intent

events-cli ships its first vertical slice: a loopback-only Mosquitto stack on
`spark-f8a9` replacing the nova brokers, a CloudEvents envelope core, and an
importable O(1)-enqueue client that unblocks `reachy-mini-cli` — issues
[#1](https://github.com/agentculture/events-cli/issues/1),
[#2](https://github.com/agentculture/events-cli/issues/2),
[#3](https://github.com/agentculture/events-cli/issues/3). The plan was fanned
out to parallel agents in isolated worktrees across five waves, each merge
gated by tests passing before **and** after.

**One planned outcome did not ship as written**: the agentfront-derived
MCP/HTTP surfaces named in the frame's after-state are *not* in this slice.
That was a recorded scope decision taken during `/think` (q4), deferred to
[#6](https://github.com/agentculture/events-cli/issues/6) before any task was
assigned — not a mid-run cut. Issue #1's pipeline acceptance criteria are
likewise explicitly unmet.

## Planned Work

Quoted verbatim from the `devague summary` skeleton:

- `t1` — Rename the import package events -> events_cli (user decision c33) with the CLI contract held invariant
- `t2` — Migrate dev dependency and CI gate from teken to agentfront
- `t3` — Build the envelope core module (pure, dockerless)
- `t4` — Build the importable publish client on paho-mqtt >=2,<3 (base dep per c22)
- `t5` — Build the stack verbs: init/up/status/logs/down with loopback-only compose
- `t6` — Write the contract docs and README lane statement
- `t7` — Write and execute the nova migration runbook with rollback on spark-f8a9
- `t8` — Build the stack-marked integration suite (docker kept out of the unit gate)
- `t9` — Release to PyPI and run issue #3 acceptance end-to-end on spark-f8a9
- `t10` — Record the deferred-arc contracts and true up CLAUDE.md

## Actual Delivery

| Plan task | Status | What actually landed |
|-----------|--------|----------------------|
| `t1` | delivered | Import package renamed to `events_cli`; console command stays `events`, PyPI dist stays `events-cli`. Both `("events",)` and `("events-cli",)` explain-catalog root keys retained. Merged `0fe288f`. |
| `t2` | delivered | Dev dep `teken>=0.8` → `agentfront>=0.20`, `uv.lock` regenerated, CI gate is `uv run agentfront cli doctor . --strict`. Merged `f253607`. Closes [#5](https://github.com/agentculture/events-cli/issues/5). |
| `t3` | delivered | `events_cli/core/` — frozen `Envelope`, one-pass field-level validation, stdlib only. Merged `445072b`. |
| `t4` | delivered | `events_cli/client.py` — `EventClient` with O(1) enqueue, never-raises, retained/LWT/QoS 0, lazy paho import. Merged `966c303`. |
| `t5` | delivered | `events init/up/status/logs/down` + `events_cli/stack/`; loopback-only compose, exact pin, persistence volume, foreign-broker preflight. Merged `4d8f176`. |
| `t6` | delivered | `docs/contract.md` + README mesh-lane statement. Merged `737d346`. |
| `t7` | delivered | Runbook authored (`docs/migrations/2026-07-nova-to-events-cli.md`, merged `008d6f6`) **and executed** by the operator — forward, rollback, forward again. |
| `t8` | delivered | `tests/test_stack_integration.py`, `stack` marker + `EVENTS_STACK_IT` opt-in; h17 measured. Merged `4c574ae`. |
| `t9` | partial | Acceptance checklist authored and **executed live: 10/10 passed** (`ef05f30`, `2da7ea2`). The **PyPI release is not done** — it happens on merge to main, by design (see Drift). |
| `t10` | delivered | `CLAUDE.md` trued up; #6 and #7 carry their confirmed spec contract as comments. Merged `e4db0cc`. |

## Mid-work Decisions

Both deviation records below are **recorded but still `proposed`** — confirming
a deviation is the user's decision and has not yet been given. They are quoted
here as recorded ground truth for what happened, not as approved rulings.

- `d1` (task `t5`, classification `acceptable`, **proposed**) — Pinned a 2.1
  Mosquitto rather than a 2.0.x. The floating `eclipse-mosquitto:2` tag now
  resolves to 2.1.2, and **2.1 ships an unauthenticated http_api dashboard on
  9883 by default**. The generated `mosquitto.conf` therefore replaces the
  image's bundled config wholesale to suppress 9883, instead of relying on the
  2.0 upstream defaults the spec assumed. `allow_anonymous=false` and
  localhost-only-with-no-listener still hold in 2.1.2, so the security posture
  is unchanged; only the "rely on defaults" premise became "actively suppress".
- `d2` (task `t8`, classification `acceptable`, **proposed**) — Corrected the
  pin to `eclipse-mosquitto:2.1.2-alpine`. The tag `d1` chose has **never been
  published**; `docker manifest inspect` returns `no such manifest`, so
  `events up` would have failed to pull on any clean host. The version had been
  read from the local image's `org.opencontainers.image.version` label, which
  reports the *software* version rather than a tag name. Verified that
  `eclipse-mosquitto:2` and `eclipse-mosquitto:2.1.2-alpine` resolve to an
  identical manifest digest, so the corrected pin freezes exactly what the
  floating tag serves and `d1`'s reasoning is untouched.

Decisions no deviation record covers:

- **The h17 persistence assumption was measured, not assumed.** `t8`'s
  unclean-kill test (`docker kill -s KILL`, `autosave_interval` at 3600 s so no
  periodic flush could confound it) found retained state **lost** — which
  *matches* the autosave-bound assumption in frame park v2. No deviation was
  needed. Had it contradicted the assumption, that would have been a third
  record rather than a quiet edit to the claim.
- **Two service windows instead of one.** The operator ran the cutover as a
  rehearsal ending in rollback (4 min 11 s), then the real cutover (19 s), so
  reversibility was proven on the live box before it was relied on.
- **Acceptance artifacts were cleared from the production broker.** The
  emit/consume run leaves retained values on `reachy/state/…`; a lingering
  `reachy/state/online=false` would hand `reachy-mini-cli` stale state on its
  first connect, so the topics were explicitly cleared.
- **`t9` was split at the PR gate.** Authoring the checklist is pre-merge; the
  PyPI release is post-merge because a push to `main` publishes via Trusted
  Publishing. This split was in the plan's own `t9` instruction, not invented
  mid-run.

## Drift From Plan

| Plan item | Reason for divergence | Classification |
|-----------|-----------------------|----------------|
| `t5` (`d1`) | Pinned 2.1.2 rather than a 2.0.x; `mosquitto.conf` actively suppresses the 9883 dashboard instead of relying on 2.0 defaults | acceptable |
| `t8` (`d2`) | The `d1` pin named an unpublished tag; corrected to `-alpine`, which shares a manifest digest with the floating tag | acceptable |
| `t9` | The PyPI release and the fresh-wheel install are **not delivered** — merging the PR publishes them. The live acceptance ran from the working tree, not the released wheel | needs-follow-up |

The frame's after-state also named agentfront-derived MCP/HTTP surfaces, which
this slice does not build. That is **not** drift: it was decided during
`/think` (q4) and deferred to #6 before the plan was written, so the plan the
user confirmed never contained it.

## Evidence

- tests (default selection): `uv run pytest -q` — **261 passed, 7 deselected**
- tests (docker-backed): `EVENTS_STACK_IT=1 pytest -m stack` — **5 passed**,
  including `test_the_pinned_image_tag_is_actually_pullable`,
  `test_retained_state_bound_after_unclean_kill`,
  `test_retained_state_survives_clean_docker_restart`
- coverage: **94.75 %** against a 60 % floor
- lint: `black --check`, `isort --check-only`, `flake8`, `bandit`,
  `markdownlint-cli2` — all clean
- rubric gate: `agentfront cli doctor . --strict` — exit 0
- live acceptance: `scripts/acceptance-issue-3.sh` — **10 passed, 0 failed** on
  `spark-f8a9`; and **3 passed / 7 failed** against the pre-cutover nova stack,
  which is what makes the pass meaningful
- commits: `0448148..7c6d4cf` (27 commits)
- issues: #1, #2, #3 advanced; #5 closed; #6–#10 opened and contracted
- evidence record: `docs/acceptance/2026-07-24-issue-3-live-run.md`

## Delivery Claims

| Claim | Confidence | Evidence |
|-------|------------|----------|
| The envelope core validates events with no broker or docker dependency | high | `events_cli/core/envelope.py` · default suite runs with no docker · commit `445072b` |
| `EventClient.publish()` is an O(1) enqueue inside a 50 Hz budget | high | measured median **0.0042 ms**, p99 0.0222 ms, n=2000, on the caller's thread — `docs/acceptance/2026-07-24-issue-3-live-run.md` |
| A broker-down publish never raises into the caller | high | live check: `ok=False reason='no_conn'` · `tests/test_client.py` |
| Exactly one loopback-only broker runs on `spark-f8a9`; the nova stack is gone | high | `ss -ltn` shows only `127.0.0.1:1883`; LAN address refused; `docker ps` shows only `events-mosquitto` |
| Retained messages, LWT and QoS 0 work on the deployed broker | high | acceptance run: LWT flipped `true`→`false` on an ungraceful socket kill, not a clean disconnect |
| Retained state survives a broker restart | high | live restart of the real stack; three retained topics returned · `test_retained_state_survives_clean_docker_restart` |
| An `Envelope` survives a real broker round-trip byte-for-byte | high | acceptance run, `evt_01KYA78X88ZBWDA084G7VYXCQQ`, `equal=True` |
| The pinned image tag is actually published | high | `test_the_pinned_image_tag_is_actually_pullable` — verified it **fails** on the old pin and passes on the new one |
| No 9883 dashboard or 9001 websocket is exposed | high | acceptance run, criterion 2 |
| The rollback path works | high | executed: nova fully restored, nervous system logged `Nervous System is running` with 29 rules |
| The introspection lane runs with paho absent | high | `tests/test_client.py::test_introspection_verbs_run_with_paho_absent` |
| Issue #3's acceptance criteria are met on the deploy box | high | `scripts/acceptance-issue-3.sh` — 10/10 |
| A fresh install of the **released wheel** yields a working broker | unverified | the release has not happened; this ran from the working tree — **not claimed done** |
| `reachy-mini-cli` can bind the client in one composition line | unverified | requires the published wheel and their publisher — **not claimed done** |
| Issue #1's pipeline acceptance criteria | unverified | explicitly out of scope for this slice (q4, deferred to #8) |

## Remaining Work / Follow-up

- **`t9` (partial)** — merge the PR to publish `0.9.0` to PyPI via Trusted
  Publishing, then verify the fresh-wheel install path on a host **without** a
  cached image. That last part matters specifically because `d2` is the class of
  bug a warm cache hides.
- **Notify `reachy-mini-cli` on #3** naming the released version, so they pin
  `>=` it. Drafted but not posted — it should name a version that exists.
- **Confirm or reject `d1` and `d2`** (`devague deviate --confirm d1 d2`). Both
  are recorded and implemented; neither is approved.
- **The robot has no nervous system** until `reachy-mini-cli` ships its
  `events_cli` publisher. This is the tracked consequence of deprecating nova,
  not a defect — it is what #3 exists to unblock.
- **Deferred arcs**: #6 (agentfront MCP/HTTP binding), #7 (durable
  subscriptions + cursor drain), #8 (pipelines), #9 (shell-cli routing), #10
  (dynsec + remote-access opt-in). #6 and #7 carry their confirmed spec
  contract as issue comments.
- **`docs/plans/`'s risk r1 still stands**: every merge to `main` publishes, so
  intermediate versions may ship a partial slice. `0.9.0` is the version to
  name on #3.

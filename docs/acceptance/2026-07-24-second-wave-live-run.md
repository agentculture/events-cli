# Live acceptance run — the second wave, spark-f8a9, 2026-07-24

The record of an **executed** service window and acceptance run for the
durable-subscription arc ([#7](https://github.com/agentculture/events-cli/issues/7)),
against the live broker on this box. Re-runnable with
`scripts/acceptance-second-wave.sh`, which exits non-zero on any failure — a
gate, not a demo.

- **Box**: `spark-f8a9`.
- **Broker under test**: `eclipse-mosquitto:2.1.2-alpine`, published
  `127.0.0.1:1883:1883`, the same broker that serves the robot's nervous system.
- **Outcome**: **12/12 checks passed**, including restart survival and exact
  cursor resume.
- **Operator authorisation**: the robot could be taken down as needed
  (2026-07-24).

## The window

| Event | UTC |
|-------|-----|
| window opens — baseline captured (`Up 4 hours (healthy)`) | 19:48:43 |
| `events init --force` — config regenerated from the new template | 19:48:5x |
| broker restarted to load the new config | 19:48:5x |
| acceptance gate run (12/12) — including its own broker restart | 19:49:09–19:50:2x |
| window closes — healthy, one client connected, registry clean | 19:50:29 |

**Total window: 1 minute 46 seconds.** The broker container was recreated
**twice** (once to load the config, once by the gate to prove restart survival).
Data survived both because `events down` does not remove volumes —
`volumes_removed: false, data_destroyed: false` in its own JSON output — so the
`events-data` named volume carried the persistence database across each
recreation. That pairing of `persistence true` *with* a mounted volume is the
thing the first slice called easy to omit and only discoverable on a restart
test; this run is that test, on the real deployment.

## What the window actually changed

Exactly one line of deployed configuration. The stack was generated at 0.9.0 and
its `mosquitto.conf` carried no `max_queued_messages`, so the undrained-backlog
bound was mosquitto's inherited default rather than a documented number. After
regeneration:

```text
111:max_queued_messages 1000
```

Every other change in the arc is new code that needs no broker change.

## The checklist

Each item is a command with observable output, not a judgement call. Full JSON
evidence was written alongside the run.

| # | Check | Result |
|---|-------|--------|
| 1 | broker healthy | PASS — `events status --json` reports healthy |
| 2 | loopback only | PASS — 1883 bound on `127.0.0.1` only |
| 3 | exactly one broker | PASS — one mosquitto container running |
| 4 | backlog bound deployed | PASS — `max_queued_messages 1000` in the live config |
| 5 | `sub add` | PASS — registered, owner resolved to `events-cli` from `culture.yaml` |
| 6 | emit while offline | PASS — 5/5 published with **no drainer connected** |
| 7 | broker restart | PASS — `events down` + `events up` |
| 8 | **restart survival** | PASS — **all 5 events survived, in order**, cursor 5 |
| 9 | **cursor resume** | PASS — resuming from cursor 5 returned **nothing already acknowledged** |
| 10 | cross-process round trip | PASS — a second process saw what the first emitted |
| 11 | producer trees excluded | PASS — `reachy/events/…` and retained `reachy/state/…` never reached the contract lane |
| 12 | `sub remove` | PASS — record gone; registry left clean |

Checks 8 and 9 are the arc's load-bearing claims. Check 11 is the boundary
[#3](https://github.com/agentculture/events-cli/issues/3) asked for: the robot's
producer-owned topic trees stay outside the contract lane even while a
contract-lane subscription is live on the same broker.

## Before-state evidence

Mechanical, not narrative. At pre-arc `HEAD` (`main` = `db45e4c`, the first
slice's PR), `git ls-tree -r --name-only main -- events_cli` returns **no**
`subs/`, **no** `history/`, and none of `topics.py`, `identity.py`, `watch.py`,
`emit.py`, `get.py` or `list.py`. The consume side did not exist; there was no
`events watch`, and a contract-lane consumer that restarted lost everything
published while it was away.

## Reversibility

The window was reversible throughout. The live `mosquitto.conf` and
`compose.yaml` were snapshotted before any change, and the stronger path — the
one the first slice established — is that the config is **regenerable from the
template**: checking out the previous revision and running `events init --force`
reproduces the old deployment exactly. The template is the source of truth, so
rollback is a checkout plus a restart rather than a restored copy.

## What this run does **not** claim

- **Pipelines are untouched.** [#1](https://github.com/agentculture/events-cli/issues/1)'s
  pipeline acceptance criteria remain unmet, deferred to
  [#8](https://github.com/agentculture/events-cli/issues/8).
- **No MCP or HTTP surface exists** —
  [#6](https://github.com/agentculture/events-cli/issues/6).
- **The broker is still anonymous on loopback.** dynsec identities and topic
  ACLs remain [#10](https://github.com/agentculture/events-cli/issues/10); the
  generated config still carries `allow_anonymous true`, and its comment still
  binds removing that line to #10 landing.
- **`events up` still shells out via stdlib `subprocess`.**
  [#9](https://github.com/agentculture/events-cli/issues/9) now carries a
  recorded verdict — stay on `subprocess`, with named reopen triggers — rather
  than being merely open.
- **The queue bound was verified as deployed and as *shape*, not at 1000 live.**
  The drop-newest behaviour was measured at the real default (1200 published,
  exactly the oldest 1000 delivered) by probe on 2026-07-24, and is asserted
  live at a smaller bound in the stack-marked suite; the number itself is a text
  assertion against the template.

## Related

- [`scripts/acceptance-second-wave.sh`](../../scripts/acceptance-second-wave.sh) — this gate.
- [`docs/acceptance/2026-07-24-issue-3-live-run.md`](2026-07-24-issue-3-live-run.md) — the first slice's run.
- [`docs/decisions/2026-07-24-history-store-evaluation.md`](../decisions/2026-07-24-history-store-evaluation.md) — why the history store is bespoke.

# The events-cli contract

**Mosquitto transports events. `events-cli` defines what they mean.**

This is the long form of that sentence. Consumers depend on what is written
here — typed envelopes, correlation and causation, delivery semantics, the
surfaces they may connect to — not on Mosquitto-specific topic conventions, so
the transport can be replaced later without changing how participants interact.
[Issue #1](https://github.com/agentculture/events-cli/issues/1) is the
requirements baseline this document renders; where the two disagree, #1 wins and
this file is the bug.

Three obligations fall on *consumers* rather than on this repo, and each has its
own section below because discovering them in production is expensive:

1. The [raw MQTT port is a first-class surface](#the-raw-mqtt-port-is-first-class)
   — the contract layer is an optional value-add, not a mandatory gateway.
2. [Delivery is at-least-once, so consumers must dedupe](#delivery-is-at-least-once-so-consumers-must-dedupe)
   on the envelope `id`. This is a requirement, not an optimisation.
3. [Retained messages are the last value, not history](#retained-messages-are-the-last-value-not-history).
   Replay is a different mechanism — durable subscriptions and the bounded
   cursor drain (`events sub` / `events watch`) — not a property of a
   retained message.

## What is built, and what is not

This repo's rule is that prose does not drift ahead of code. The table is the
status of each lane as of the first slice; everything marked *not built* is a
decided design constraint with an issue, not a shipped behaviour.

| Lane | Status |
|------|--------|
| Envelope core — CloudEvents-shaped, immutable, pure and dockerless | built (first slice) |
| Canonical topic mapping — dotted `type` ⇄ MQTT topic (`events_cli/core/topics.py`) | built (second wave) |
| Importable publish client — `import events_cli` | built (first slice) |
| Raw MQTT port — TCP 1883 on loopback | supported surface (first slice) |
| Stack verbs — `events init` / `up` / `status` / `logs` / `down` | built (first slice) |
| agentfront-derived MCP and HTTP surfaces | not built — [#6](https://github.com/agentculture/events-cli/issues/6) |
| Durable subscriptions + cursor drain — registry, MQTT persistent sessions, history store, `events sub add/list/show/remove`, `events watch` | built (second wave) |
| Event history reads — `events emit` / `get` / `list` | built (second wave) |
| Pipelines — apply / list / show / run / inspect | not built — [#8](https://github.com/agentculture/events-cli/issues/8) |
| `events up` routed through `shell-cli` confinement | not built — [#9](https://github.com/agentculture/events-cli/issues/9) |
| dynsec identities, topic ACLs, documented remote opt-in | not built — [#10](https://github.com/agentculture/events-cli/issues/10) |

`events emit`, `events get` and `events list` shipped alongside `events sub`
and `events watch` in the second wave: `emit` validates an envelope through
the core and publishes it QoS 1 to its canonical topic, and `get`/`list` read
back whatever a registered subscription's drain actually captured. There is
still no `events pipeline` verb, and #1's pipeline acceptance criteria remain
explicitly **not** met.

## Lane boundary: culture carries conversation, events-cli carries events

[`culture`](https://github.com/agentculture/culture) carries **agent
conversation** — peer-to-peer, human-readable, presence-oriented, IRC-shaped —
and `events-cli` carries **machine events** — typed immutable envelopes,
correlation and causation, durable history, pipeline runs, app-to-app as much as
agent-to-agent ([#2](https://github.com/agentculture/events-cli/issues/2)).

A second messaging substrate in one ecosystem is defensible, but only if the
boundary is stated; otherwise contributors guess, and the guess is usually
"whichever one I already have wired up". The operational consequence is a hard
one: **no events-cli surface accepts or relays agent conversation.** Chat
belongs on the mesh. If a payload's audience is a human reading it as prose, it
is a culture message, not an event.

## Surfaces

One core owns event semantics; every surface is derived from it, so the four
lanes cannot say different things about the same event.

| Surface | Who uses it | Status |
|---------|-------------|--------|
| Raw MQTT on TCP 1883 | co-located latency-sensitive producers | supported |
| `import events_cli` | Python applications in the same process | built (first slice) |
| `events …` CLI | humans, agents, scripts, CI | built (introspection + stack verbs) |
| HTTP API and MCP tools | services and agents over a socket | not built — [#6](https://github.com/agentculture/events-cli/issues/6) |

Three names, deliberately distinct: the console command is **`events`**, the
PyPI distribution is **`events-cli`**, and the import package is
**`events_cli`** — not `events`, because the PyPI distribution `Events` already
owns that top-level module name.

When the agentfront binding lands (#6), CLI, MCP and HTTP are *derived* from one
registry that is itself populated from the core module — including the import
lane, which is not agentfront's and would otherwise become a fourth thing that
drifts. Two constraints are already fixed for that arc: a long-lived
subscription cannot block an MCP tool call or an HTTP request, so `watch` is
exposed everywhere as a **bounded drain** over a durable server-side
subscription rather than an endless stream; and **every agent-facing tool
carries `--max` and `--timeout` with non-infinite defaults**, because an
unbounded default eventually hangs an agent turn.

## The raw MQTT port is first-class

The broker's TCP port is a **supported, documented surface** for co-located
producers, not a private implementation detail that consumers are supposed to
route around.

This is not a concession; it is a requirement that fell out of the first
consumer. `reachy-mini-cli` publishes from inside a 50 Hz robot control loop
where a publish must be an **O(1) in-process enqueue** — the caller returns
immediately and network I/O happens on the client's own background machinery. A
publish that crossed a CLI process boundary, an HTTP request or an MCP tool call
would put a syscall-and-round-trip on the tick thread and blow the loop's
budget. There is no version of "just go through the front door" that survives
contact with a real-time control loop.

Three consequences, all binding:

- **Direct broker access is documented and supported.** A co-located producer
  connecting straight to `127.0.0.1:1883` is using the system correctly.
- **The contract layer is an optional value-add, not a mandatory gateway.**
  Envelope validation, correlation, pipelines and history are worth opting into;
  nothing in this repo may make them a precondition for getting a message onto
  the broker.
- **Producer-owned topic trees stay legal.** `reachy-mini-cli` owns
  `reachy/events/{source}/{type}` (not retained) and retained
  `reachy/state/{key}`, with the payload of each event byte-identical to the
  line its existing stdout feed already emits. Those topics do not participate
  in pipelines and are never forced through envelope validation.

If a validation layer later polices topic shapes, it must leave room for
producer-owned trees that opt out. Anything else breaks the first consumer that
ever used this broker in anger.

## Delivery is at-least-once, so consumers must dedupe

MQTT's quality-of-service ladder is the whole delivery story here; `events-cli`
adds no delivery guarantee of its own.

| QoS | Guarantee | What a consumer sees |
|-----|-----------|----------------------|
| 0 | at-most-once | a message may be lost; it is never duplicated by the broker |
| 1 | at-least-once | every message arrives; **some arrive more than once** |
| 2 | exactly-once | not used — see below |

**Exactly-once delivery is an explicit non-goal** of this project
([#1](https://github.com/agentculture/events-cli/issues/1)). QoS 2's four-way
handshake buys end-to-end exactly-once only in the narrowest sense — it does not
survive a consumer that crashes after acting and before acknowledging — so it is
not a substitute for idempotent consumers, only a slower way to still need them.

Therefore, as a **requirement of this contract, not an optimisation**:

> Every consumer that acts on an event must be idempotent, keyed on the
> envelope's `id`. Receiving the same `id` twice must produce exactly the effect
> of receiving it once.

The `id` field exists to make this cheap: it is generated once by the producer,
travels unchanged through every hop and every redelivery, and is the only field
guaranteed stable for that purpose. `time`, `deliveryAttempt` and broker-level
metadata all legitimately differ between two deliveries of the same event —
`deliveryAttempt` exists precisely so a consumer can *observe* the redelivery,
not so it can avoid handling it.

Duplicates are normal operation, not an error condition. They arrive when a
subscriber reconnects with an unacknowledged in-flight message, when a session
is resumed after a network partition, and when a producer retries after an
ambiguous failure it could not tell from a success. A consumer that treats a
duplicate as corruption will page someone during the first flaky link.

Producers publishing at QoS 0 — which is what the first slice's co-located
producer does on its own topic trees — trade duplicate delivery for dropped
delivery. That does not exempt their consumers: any consumer that also reads a
QoS 1 tree, or that resubscribes and re-reads retained state, still needs the
same `id`-keyed dedupe.

## Retained messages are the last value, not history

A retained message is the broker holding **one** message per topic and handing
it to each new subscriber at subscribe time. It answers "what is the current
value of this thing?" for a client that just arrived.

It is not a log. Specifically, a retained message gives you **none** of:

- **replay** — the previous values are gone, overwritten by the current one;
- **a window** — there is no "last N" or "since timestamp", only "latest";
- **ordering across topics** — retained values arrive per topic with no relation
  between them;
- **evidence that intermediate values existed** — a topic that went `a → b → c`
  between two subscribes is indistinguishable from one that was always `c`.

Two further properties matter to anyone building on retained state:

**Durability has a bound, and the bound is stated rather than implied.** Retained
state survives a *clean* broker restart when the deployment sets
`persistence true` **and** mounts a volume — both, which is why the generated
Compose file carries the volume and the first restart test is the acceptance for
it. An *uncleanly* killed broker may lose writes since the last autosave, so the
generated `mosquitto.conf` sets `autosave_interval` explicitly and the
integration suite measures what actually survives instead of assuming.

**A retained value outlives its publisher.** Once a producer dies, its retained
topics keep serving their last value forever, and a subscriber cannot tell that
from a live system. The fix is publisher-owned availability: a retained
`…/online` topic set `true` on connect, with an MQTT **Last Will** configured
*before* connect that flips it to `false` on ungraceful death, and republished on
every reconnect. `reachy-mini-cli` does exactly this with `reachy/state/online`;
any producer of retained state should carry an equivalent, because without one
"retained" quietly means "stale forever".

Durable history — replay from a cursor, surviving a stack restart — is a
different mechanism living in the control service's own store, deliberately
built together with durable subscriptions rather than bolted on afterwards.
The store, the registry and the bounded drain are built, `events sub` /
`events watch` are the CLI surface over them (see
[below](#the-consume-side-cursor-drain-single-drainer-and-the-capture-boundary)),
and `events get` / `events list` now read captured history directly. Nothing
in this repo should be read as implying retained == history.

## The canonical topic mapping

Every contract-lane topic is derived mechanically from an event's dotted
`type`, never invented by hand: `events_cli/core/topics.py` is the one place
that mapping is defined, and every other layer — the importable client, the
subscription registry, the drain engine, the CLI verbs — is required to go
through it rather than construct topic strings itself. A dot in a dotted
`type` becomes an MQTT topic level, and the literal segment `events` is
prepended to everything the module produces:

| Dotted type / pattern | MQTT topic / filter |
|------------------------|----------------------|
| `task.requested` | `events/task/requested` |
| `heartbeat` | `events/heartbeat` |
| `task.*` (pattern) | `events/task/+` (filter) |

Two rules keep the mapping unambiguous:

- **`*` matches exactly one dotted segment**, and always compiles to MQTT's
  single-level wildcard `+` — **never** the multi-level `#`. `task.*` selects
  `task.requested` and `task.completed` but not a grandchild such as
  `task.sub.completed`; compiling it to `#` would let a pattern's reach widen
  silently every time a producer added a deeper segment.
- **A pattern must never contain a raw MQTT filter character** — `#`, `+`, or
  the level separator `/` itself. Those carry MQTT structural meaning a
  hand-typed dotted pattern must never smuggle in; most importantly, they are
  exactly what would let a pattern escape the `events/` prefix. Rejecting them
  outright, rather than passing them through, is a confirmed finding from this
  arc's challenge pass, not a defensive guess.

The `events/` prefix is what separates this contract lane from a
[producer-owned topic tree](#the-raw-mqtt-port-is-first-class) such as
`reachy-mini-cli`'s `reachy/events/{source}/{type}` and retained
`reachy/state/{key}`. No dotted event type can spell its way out of the
prefix — an event literally typed `reachy.state.updated` still maps to
`events/reachy/state/updated`, never to `reachy/state/updated` — and no
contract-lane filter can reach into a producer's own tree either.

## The backlog bound: queued messages for an offline session

A persistent MQTT session's queue for a subscriber that is currently offline
is not unbounded, and the bound is not something `events-cli` invents — it is
mosquitto's own `max_queued_messages` setting, which the generated
`mosquitto.conf` now states explicitly, next to the `persistence` block, so it
is a documented number rather than an inherited default.

**Measured, not assumed.** On 2026-07-24 a scratch `eclipse-mosquitto:2.1.2-alpine`
broker was run with this repo's exact template config and probed directly:

- An MQTT5 persistent session (`clean_start=False`, CONNECT
  `SessionExpiryInterval=0xFFFFFFFF`) reported `session_present=True` after a
  **broker restart**, with no extra broker configuration — subscription
  registration and queued backlog both survive.
- **1200** QoS-1 messages were published while the subscriber was offline. On
  resume, exactly **1000** arrived, **in order**, and they were the
  **OLDEST** ones (`m00000` … `m00999`). The 200 newest were **dropped**.

That makes mosquitto 2.1.2's overflow behaviour concrete: once the queue is
full, the broker **keeps the backlog it already has and refuses new
arrivals** — it does not evict the head to make room for the tail. A session
that overflows loses its most recent events, not its oldest ones.

**The only in-band signal is a broker log line** —
`Outgoing messages are being dropped for client <id>.` — reachable via
`events logs`; there is no `$SYS` topic and no distinct disconnect reason for
it. A design that must detect overflow has to watch that log line, or,
better, drain often enough that reaching the bound stays unrealistic — see
cursor-drain semantics below.

## The consume side: cursor drain, single-drainer, and the capture boundary

The subscription registry, the MQTT persistent-session lifecycle, the history
store and the bounded drain engine are **built** (`events_cli/subs/`,
`events_cli/history/`), and the CLI surface over them — `events sub
add/list/show/remove`, `events watch`, and the direct history-read surface
`events emit` / `events get` / `events list` — all shipped in the second wave.
`events emit <type> --data <file|->` validates an envelope through the core
(generating `id`/`time`), publishes it QoS 1 to its canonical topic via
`EventClient`, and prints the resulting `PublishResult`; `events get
<event-id>` and `events list --type <t> --max N` read back whatever a
registered subscription's drain actually captured. Everything below is the
confirmed contract this arc satisfies, not a proposal.

**Cursor-drain semantics.** A long-lived MQTT subscription blocks a request in
every request/response surface — an MCP tool call, an HTTP request — and the
CLI is the only surface that can stream forever, so the consume verb cannot be
an endless `watch` everywhere. The shape that works identically on every
surface is a **bounded drain over a durable, server-side, named
subscription**: `events watch <sub> --since <cursor> --max N --timeout S`
replays whatever the history store already holds past `<cursor>` first — a
pure, dockerless read, no broker connection required — then resumes the
subscription's persistent session only for whatever budget is left, consuming
up to `--max` events or until the `--timeout` deadline, persisting each event
to the history store **before** acknowledging it, and returning the batch plus
the next cursor. Every agent-facing verb in this arc carries `--max` and
`--timeout` with **non-infinite defaults** — an unbounded default eventually
hangs an agent turn — and there is deliberately no `--follow`; unbounded
streaming stays an HTTP-only concern for a later arc.

**Single-drainer semantics.** A named subscription is drained by **one**
process at a time. MQTT itself enforces this: a persistent session is
identified by its client id, and a second process connecting with the same id
causes the broker to **take over** the session and disconnect the first — the
2026-07-24 probe observed exactly this log line: `Client <id> [(null):0]
disconnected: session taken over.` A concurrent second drainer is therefore
not a silent race; it is an observable disconnect of whichever drainer was
already connected. Persisting each event to the history store **before**
acknowledging it — the same ordering the backlog-bound section above assumes —
is what makes a takeover **lossless** rather than merely loud: the outgoing
drainer may have delivered a batch it never got to acknowledge, but nothing it
already persisted is lost, and the incoming drainer resumes the same session
with the same queued backlog. Consumer-side dedupe on the envelope `id` —
already a contract requirement,
[above](#delivery-is-at-least-once-so-consumers-must-dedupe) — is what makes a
takeover safe to retry rather than merely lossless.

**The capture boundary.** History captures only what a **registered
subscription's persistent session actually queued** — nothing more. Three
concrete consequences:

- An event published **before** a subscription is registered is transported by
  the broker to whichever other subscriber wants it, but was never queued for
  this one, so a later drain of that subscription will never return it. There
  is no retroactive capture.
- An event published at **QoS 0** is never queued for an offline session at
  all — MQTT's own delivery model, not an `events-cli` choice — so it is never
  captured regardless of whether a subscription exists. See the QoS trap
  below.
- There is **no global capture** of contract-lane traffic — only what a
  registered subscription's session actually queued is ever captured; nothing
  published to `events/…` is captured for a topic nobody has subscribed to
  yet. `events list` reads only what registered subscriptions actually
  drained — a view over what was captured, never a claim that every event
  ever published is in it. Consumers must not read it as a complete log.

## The QoS trap: `publish()` still defaults to QoS 0 — `publish_event()` no longer does

`EventClient.publish()` — the raw lane `reachy-mini-cli`'s 50 Hz control loop
binds to — still defaults to `qos=0`, correct for the co-located,
drop-don't-block lane [described above](#the-raw-mqtt-port-is-first-class),
where a real-time loop would rather lose a message than block on an
acknowledgement. **`publish_event()` no longer shares that default**: since
`0.10.0` it defaults to `qos=1` — a deliberate behaviour change on an
already-published wheel (see `CHANGELOG.md`) — because an envelope published
at QoS 0 is never queued for an offline persistent session at all, exactly the
trap this section is named for, and `publish_event()` is the
envelope-publishing call, not the raw one. `events emit` also passes `qos=1`
explicitly, so a CLI-emitted event is always eligible for durable capture
regardless of the client's own default.

That default has a consequence a consumer must not miss whichever call they
use: **QoS 0 messages are never queued for an offline session**, at all,
regardless of whether a subscription exists for them. A publish at QoS 0 is
transported to whoever is listening *right now* and nowhere else — it bypasses
durable capture entirely, by construction, not by a bug. A caller that needs a
published event to survive being captured by a drain must publish it at QoS
1 — `publish_event()`'s new default, and always what `events emit` does.

## Network posture

The Compose port mapping is the literal `127.0.0.1:1883:1883`, never a bare
`1883:1883`. Docker publishes on `0.0.0.0` by default and its NAT bypasses host
firewall rules, so an unqualified mapping is LAN-exposed regardless of what
`ufw` says. The literal loopback prefix is the control, and a test asserts it.

Mosquitto 2.0 already defaults the way this project wants — no listener means
localhost-only, and `allow_anonymous` defaults to `false` — so several of the
security properties here are *upstream defaults not overridden* rather than a
hardening layer. The generated `mosquitto.conf` says which is which, for the
exact version it pins; an unpinned floating tag could change that behaviour
underneath a deployment silently, so the image tag is exact and a test fails if
it ever becomes a bare major.

Anonymous access on loopback is accepted for this slice
([#3](https://github.com/agentculture/events-cli/issues/3)): the broker is
reachable only from the box it runs on, and the co-located producers are the
box's own processes. Distinct identities with topic-level ACLs via Mosquitto's
dynamic security plugin, and remote access as an explicit documented opt-in, are
[#10](https://github.com/agentculture/events-cli/issues/10). The first slice
ships **no** WebSocket listener; if one returns it arrives through the streaming
lane as an opt-in, not as a debugging leftover.

One portability note for clients: prefer an explicit `127.0.0.1` over
`localhost`. On a dual-stack host `localhost` can resolve v6-first, which would
silently miss a v4-only published port.

## Who depends on this: the blocked-consumer chain

The first consumer is real and its dependency is recorded on both sides, so this
is a traceable chain rather than a motivating anecdote.

[`reachy-mini-cli`](https://github.com/agentculture/reachy-mini-cli)'s converged
spec (`docs/specs/2026-07-23-reachy-nervous-system.md`) makes its nervous-system
leg a publisher into a broker **it does not own and does not ship**: that repo
ships no Compose file and no MQTT library at all. Its wire contract — the topic
map, the retention and QoS per topic, the `online` availability topic and its
Last Will — is written down in `docs/export-schema.md` under *Nervous System
Bus (MQTT)*, which names this project as the owner of both the broker and the
client and cites
[events-cli#3](https://github.com/agentculture/events-cli/issues/3) as the
tracking issue.

That spec codes against an *injected client seam* today: its publisher is
written and tested against a fake, and one composition line binds the real
import once the first `events-cli` wheel carrying the client ships. Its
broker-dependent acceptance items — a subscriber receiving live events on
`reachy/events/#`, retained state on `reachy/state/#` a late subscriber sees
immediately, `kill -9` flipping `reachy/state/online` to `false` while other
retained state persists, and a publish from the tick thread measured as an O(1)
enqueue — cannot be checked until this broker and this client exist. The same
dependency is recorded from this side on
[#3](https://github.com/agentculture/events-cli/issues/3), including the
operator amendment that made the importable client (rather than `paho-mqtt` in
their tree) the thing they wait on.

Their declared first subscriber is the reTerminal panel, which today reads a
single stdout pipe that exactly one process can hold. So the chain is: this
slice unblocks a publisher, which unblocks a consumer that has no other way to
get the data.

## Non-goals

Carried from [#1](https://github.com/agentculture/events-cli/issues/1) and
unchanged by this slice:

- No reimplementing MQTT, and no second broker technology.
- No Kubernetes, and no GUI.
- No arbitrary user code executing inside the events service.
- No general-purpose workflow engine.
- **No exactly-once delivery** — see
  [above](#delivery-is-at-least-once-so-consumers-must-dedupe).

And for this slice specifically:

- No bespoke history store written from scratch: `eidetic-cli` and
  `data-refinery-cli` already own the memory and data-quality lanes, and are
  evaluated before any control-service persistence beyond Mosquitto's own state.
- No pipelines, and therefore no decision yet on whether `devague`'s lifecycle
  is the first pipeline vocabulary.

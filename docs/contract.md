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
   Replay is a different mechanism that does not exist yet.

## What is built, and what is not

This repo's rule is that prose does not drift ahead of code. The table is the
status of each lane as of the first slice; everything marked *not built* is a
decided design constraint with an issue, not a shipped behaviour.

| Lane | Status |
|------|--------|
| Envelope core — CloudEvents-shaped, immutable, pure and dockerless | built (first slice) |
| Importable publish client — `import events_cli` | built (first slice) |
| Raw MQTT port — TCP 1883 on loopback | supported surface (first slice) |
| Stack verbs — `events init` / `up` / `status` / `logs` / `down` | built (first slice) |
| agentfront-derived MCP and HTTP surfaces | not built — [#6](https://github.com/agentculture/events-cli/issues/6) |
| Durable subscriptions, cursor drain, history and replay | not built — [#7](https://github.com/agentculture/events-cli/issues/7) |
| Pipelines — apply / list / show / run / inspect | not built — [#8](https://github.com/agentculture/events-cli/issues/8) |
| `events up` routed through `shell-cli` confinement | not built — [#9](https://github.com/agentculture/events-cli/issues/9) |
| dynsec identities, topic ACLs, documented remote opt-in | not built — [#10](https://github.com/agentculture/events-cli/issues/10) |

There are no `events emit`, `events watch` or `events pipeline` verbs in the
first slice. Publishing in this slice happens through the importable client or
straight over MQTT; #1's pipeline acceptance criteria are explicitly **not** met
by it.

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
different mechanism living in the control service's own store, and it is
deliberately designed together with durable subscriptions rather than bolted on
afterwards ([#7](https://github.com/agentculture/events-cli/issues/7)). It does
not exist yet. Nothing in this repo should be read as implying retained ==
history.

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

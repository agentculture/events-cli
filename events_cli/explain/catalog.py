"""Markdown catalog for ``events explain <path>``.

Each entry is verbatim markdown. Keys are command-path tuples. The empty tuple,
``("events",)`` and ``("events-cli",)`` all resolve to the root entry — the
installed console script is ``events``, ``events-cli`` is the PyPI distribution
name, and the agent-first rubric's ``explain_self`` check invokes
``events explain events``. Keep every one of those keys.

These keys are **command-path names, not module names**: the import package was
renamed to ``events_cli`` in 0.8.0, and these keys deliberately did not follow —
they spell what a caller types, and no caller types ``events_cli``.

Keep bodies self-contained: an agent reading one entry should get enough
context without chaining reads.
"""

from __future__ import annotations

_ROOT = """\
# events

The AgentCulture event fabric. Runs and maintains a Dockerised Eclipse Mosquitto
MQTT broker and fronts it as a CLI, an HTTP API and an MCP surface — so any app
can `import events_cli`, any service can call the API, and an agent or a human
can publish and subscribe to events the same way.

Mosquitto transports events; `events` defines what they mean. Consumers depend
on the `events` contract — typed immutable envelopes, correlation and causation,
durable history, pipeline runs — not on Mosquitto-specific topic conventions.

Three names, deliberately distinct: the installed console command is `events`,
the PyPI distribution (and repository) is `events-cli`, and the import package
is `events_cli`.

## Status

The **broker stack** is implemented: `init`/`up`/`status`/`logs`/`down` generate
and operate a Dockerised Mosquitto deployment. **Durable subscriptions and the
bounded cursor drain** are also implemented: `sub add/list/show/remove` and
`watch`. Publishing an event through the CLI (`events emit`) and reading
captured history directly (`events get` / `events list`) are not implemented
yet, and neither are pipelines. The specification being built against lives in
the repository's open issues.

## Verbs

- `events whoami` — identity probe from `culture.yaml`.
- `events learn` — structured self-teaching prompt.
- `events explain <path>` — markdown docs for any noun/verb.
- `events overview` — descriptive snapshot of the agent.
- `events doctor` — check the agent-identity invariants.
- `events cli overview` — describe the CLI surface.
- `events init` — generate the loopback-only broker stack.
- `events up` — start the broker (refuses if another one holds the port).
- `events status` — broker state and health.
- `events logs` — the last N lines of the broker log.
- `events down` — stop and remove the broker.
- `events sub add/list/show/remove` — manage durable subscriptions.
- `events watch <name>` — bounded cursor drain over a durable subscription.

## The broker

One Eclipse Mosquitto container, published on `127.0.0.1:1883` and nowhere
else. Anonymous on loopback, no websocket listener, persistence on a named
volume. See `events explain init`.

## Durable subscriptions and the consume side

`events sub` registers a named subscription (an MQTT persistent session plus a
registry record); `events watch` bounded-drains it, replaying already-persisted
history before touching the broker for anything newer. See `events explain sub`
and `events explain watch`. Publishing an event (`events emit`) and reading
captured history directly (`events get` / `events list`) are not implemented
yet.

## Exit-code policy

- `0` success
- `1` user-input error
- `2` environment / setup error
- `3+` reserved

## See also

- `events explain whoami`
- `events explain doctor`
"""

_WHOAMI = """\
# events whoami

Reports the agent's identity from `culture.yaml`: nick (`suffix`), backend,
served model, and the package version. Read-only.

The nick is the mesh suffix (`events-cli`), which is deliberately not the same
string as the console command (`events`).

## Usage

    events whoami
    events whoami --json
"""

_LEARN = """\
# events learn

Prints a structured self-teaching prompt covering purpose, command map,
exit-code policy, `--json` support, and the `explain` pointer.

## Usage

    events learn
    events learn --json
"""

_EXPLAIN = """\
# events explain <path>

Prints markdown documentation for any noun/verb path. Unlike `--help` (terse,
positional), `explain` is global and addressable by path.

## Usage

    events explain events
    events explain whoami
    events explain --json <path>
"""

_OVERVIEW = """\
# events overview

Read-only descriptive snapshot of the agent: identity (from `culture.yaml`), the
verb surface, and the sibling-pattern artifacts this repo carries. Accepts an
ignored `target` so a stray path never hard-fails.

## Usage

    events overview
    events overview --json
"""

_DOCTOR = """\
# events doctor

Checks the agent-identity invariants `steward doctor` verifies:
prompt-file-present and backend-consistency (`colleague` → `AGENTS.colleague.md`), plus a
skills-present check. Exits 1 when unhealthy.

## Usage

    events doctor
    events doctor --json
"""

_CLI = """\
# events cli

Noun group for CLI-surface introspection. `cli overview` describes the CLI
itself (distinct from the global `overview`, which describes the agent).

## Usage

    events cli overview
    events cli overview --json
"""


_INIT = """\
# events init

Generates the broker stack — `compose.yaml` and `mosquitto.conf` — into the
stack directory. Writes nothing else and starts nothing. Both files are copied
verbatim from the shipped templates, so what `init` produces is exactly what a
reviewer can read in the repository.

## Usage

    events init
    events init --dir ./my-stack
    events init --force        # regenerate, discarding local edits
    events init --json

## What it generates

- **`eclipse-mosquitto:2.1.2-alpine`** — an exact patch tag, never the floating
  `eclipse-mosquitto:2`. A floating tag swaps the broker and its defaults under
  a running deployment, and those defaults are what `mosquitto.conf` documents.
  The `-alpine` suffix is required, not stylistic: upstream publishes the 2.1
  line only in that form, so a bare `:2.1.2` fails to pull.
- **`127.0.0.1:1883:1883`** — the published port mapping. Not a bare
  `1883:1883`. Docker publishes on `0.0.0.0` by default and its NAT rules are
  traversed before the host firewall, so a bare mapping is LAN-reachable no
  matter what `ufw` says. This mapping is the only thing keeping the broker off
  the network; remote access is an explicit opt-in that edits `compose.yaml`.
- **`persistence true` plus a named `events-data` volume.** The setting alone
  writes the database into the container's writable layer, where it dies with
  the container. `autosave_interval` is set to 60 seconds, which bounds what an
  unclean stop can lose.
- **A `mosquitto_sub` healthcheck** that connects and subscribes, so `status`
  reports something stronger than "the process is up".
- **No websocket listener and no `http_api` listener.** Mosquitto 2.1 opens an
  unauthenticated dashboard on 9883 by default; declaring an explicit listener
  suppresses it, and nothing publishes 9001 or 9883.

Anonymous access is enabled, which overrides the upstream default of
`allow_anonymous false`. That is acceptable only because the port is
loopback-bound. The generated `mosquitto.conf` names every such override.

## Where it writes

`$XDG_CONFIG_HOME/events-cli/stack` (usually `~/.config/events-cli/stack`).
Override per-invocation with `--dir`, or globally with `$EVENTS_STACK_DIR`.

## Exit codes

- `0` written.
- `1` files already exist and `--force` was not given.
- `2` the directory could not be written.

## See also

- `events explain up`
"""


_UP = """\
# events up

Starts the broker, in detached mode, waiting until it reports healthy. Requires
`events init` first, and `docker` with the Compose v2 plugin.

## Usage

    events up
    events up --timeout 120
    events up --json

## Preflight

Before touching the stack, `up` checks whether anything already listens on
`127.0.0.1:1883` — a plain TCP connect, needing no docker. Three outcomes:

- **free** — proceed.
- **ours** — our own broker already holds it; `up` is idempotent and proceeds.
- **foreign** — refuse, with exit code `2`.

A refusal names the container holding the port and gives the exact command that
frees it, for example `docker stop nova-mosquitto`. When docker cannot account
for the listener (a host process, another namespace) the refusal still stands
and names `ss` to find the owner instead. It also says so when the incumbent is
published on a non-loopback address, because that is the LAN-exposed
anti-pattern this stack replaces.

Exactly one broker may own this port. `up` never attaches to a broker it did
not start.

## Exit codes

- `0` running and healthy.
- `1` started, but not healthy.
- `2` docker missing, stack not initialised, preflight refused, or compose
  failed.

## See also

- `events explain status`
"""


_STATUS = """\
# events status

Reports what the broker is actually doing, from `docker compose ps`.

## Usage

    events status
    events status --json

## What "healthy" means here

`healthy` is true only when docker's own healthcheck says `healthy`. It is
never inferred from a container being up: a broker can be running, listening
and refusing every connection. A container with no healthcheck reports health
`none` and `healthy: false`, because "not checked" is not a pass.

`loopback_only` is derived from the address docker reports as published right
now, not from the template that was generated. That is what catches a stack
someone has edited onto `0.0.0.0`.

## Exit codes

- `0` running and healthy.
- `1` not running, or running but not healthy.
- `2` docker missing, stack not initialised, or the query failed.
"""


_LOGS = """\
# events logs

Prints the last N lines of the broker's container log.

## Usage

    events logs
    events logs --tail 500
    events logs --timeout 60
    events logs --json

Two bounds, and both matter. `--tail` caps how much comes back; `--timeout`
caps how long you wait for it, because on a loaded host docker can be slow to
answer even a small tail. Bounding output alone still lets a turn hang.

There is deliberately no `--follow`. An unbounded stream blocks whatever is
reading it, which for an agent means a hung turn; every agent-facing verb here
takes a bounded amount of time. Poll `logs` instead.

The broker logs to stdout, so the container log is the whole log surface —
`compose.yaml` caps its size at 3 x 10 MB.

## Exit codes

- `0` printed.
- `2` docker missing, stack not initialised, or the query failed.
"""

_DOWN = """\
# events down

Stops and removes the broker's containers.

## Usage

    events down
    events down --volumes     # also delete the data volume
    events down --json

By default the `events-data` volume is kept, so retained messages and queued
QoS>0 sessions survive the next `up`. `--volumes` deletes it, which destroys
them. Note that retained messages are the last value published on a topic
handed to new subscribers — they are not a replayable log and not event
history.

## Exit codes

- `0` stopped.
- `2` docker missing, stack not initialised, or compose failed.
"""


_SUB = """\
# events sub

Manage durable subscriptions: a registry record (name, pattern, owner, client
id) plus an MQTT persistent session in the broker, kept in step by
`events_cli/subs/`. `events sub` alone lists what is registered (same as
`events sub list`).

## Verbs

- `events sub add <name> <pattern>` — create the broker session, then the
  record. `--owner NAME` overrides the default (this agent's `culture.yaml`
  nick).
- `events sub list` — every registered subscription.
- `events sub show <name>` — one record.
- `events sub remove <name>` — end the broker session, then drop the record.
  `--force` drops the record even if the session could not be destroyed (the
  broker is down) — the escape hatch for a broker that is gone for good, at
  the cost of possibly leaving a live session orphaned in it.

## Usage

    events sub add robot 'task.*'
    events sub list --json
    events sub show robot
    events sub remove robot --force

## Exit codes

- `0` success.
- `1` a bad name/pattern, a name already registered, or an unknown name.
- `2` a damaged registry record, or the broker refused/never answered
  (`sub add` and `sub remove` touch the broker; `sub list`/`sub show` do not).

## See also

- `events explain watch`
"""

_SUB_ADD = """\
# events sub add <name> <pattern>

Registers a durable subscription: opens an MQTT persistent session
(`clean_start=False`, an effectively infinite session expiry), subscribes the
pattern at QoS 1, disconnects gracefully leaving the session live in the
broker, and only then writes the registry record. A record naming a session
that was never created would be a subscription that silently captures
nothing, so the session is created first.

## Usage

    events sub add robot 'task.*'
    events sub add robot 'task.*' --owner reachy-mini-cli
    events sub add robot 'task.*' --json

## Exit codes

- `0` registered.
- `1` an invalid name/pattern, or that name is already registered.
- `2` the broker refused the session or never answered (start it with
  `events up`).
"""

_SUB_LIST = """\
# events sub list

Lists every registered subscription, sorted by name. Read-only: no broker
connection, registry only.

## Usage

    events sub list
    events sub list --json
"""

_SUB_SHOW = """\
# events sub show <name>

Shows one subscription's record. Read-only: no broker connection, registry
only.

## Usage

    events sub show robot
    events sub show robot --json

## Exit codes

- `0` found.
- `1` no subscription by that name.
"""

_SUB_REMOVE = """\
# events sub remove <name>

Destroys a subscription: connects with `clean_start=True` and a session
expiry of 0 (which ends the broker session on disconnect), then drops the
registry record. A broker that cannot be reached is an error, not a silent
success — dropping the record would orphan a live queue with nothing on disk
pointing at it — unless `--force` is given.

## Usage

    events sub remove robot
    events sub remove robot --force
    events sub remove robot --json

## `--force`

Drops the registry record even when the broker session could not be
destroyed. Only the record goes; if the broker is actually still there, the
session it describes is left running, queuing events forever with nothing in
the registry pointing at it any more. Use it only when the broker is gone for
good.

## Exit codes

- `0` removed.
- `1` no subscription by that name.
- `2` the broker refused or never answered, and `--force` was not given.
"""

_WATCH = """\
# events watch <name>

The bounded cursor drain over a durable subscription. Replays already-
persisted history first (a pure store read — no broker connection), then
drains the broker for anything newer, up to `--max` events or the `--timeout`
deadline. Returns the batch plus the cursor to pass back as `--since` next
time.

## Usage

    events watch robot
    events watch robot --since 42
    events watch robot --max 20 --timeout 10
    events watch robot --json

## Defaults

`--max 100 --timeout 30`. Both are finite by policy, and there is deliberately
no `--follow`: an unbounded stream would hang whatever is reading it, which
for an agent means a hung turn. Poll `watch` again with the returned cursor
instead.

## How `--since` composes history and the broker

1. `HistoryStore.read(name, since, max)` — bounded, exact, no broker
   connection. If this alone fills `max`, the broker is never touched.
2. Whatever budget is left is drained from the broker
   (`drain_subscription`), floored at the cursor the read ended on, so a
   drain can never return an event already replayed from history.
3. The two batches concatenate, oldest first. `servedFrom` in the result
   says which happened: `history`, `broker`, or `history+broker`.

## Exit codes

- `0` success (an empty batch at the deadline is still success — the
  subscription is just idle).
- `1` a bad name/bound, or no subscription by that name.
- `2` the store is damaged, or the broker refused/never answered.

## See also

- `events explain sub`
"""


ENTRIES: dict[tuple[str, ...], str] = {
    (): _ROOT,
    # Both spellings are load-bearing: the rubric gate calls `explain events`,
    # and `explain events-cli` stays valid for callers using the dist name.
    ("events",): _ROOT,
    ("events-cli",): _ROOT,
    ("whoami",): _WHOAMI,
    ("learn",): _LEARN,
    ("explain",): _EXPLAIN,
    ("overview",): _OVERVIEW,
    ("doctor",): _DOCTOR,
    ("cli",): _CLI,
    ("cli", "overview"): _CLI,
    # Broker stack verbs. Registered at the top level, so their catalog keys are
    # single-token too — there is no ("stack", ...) prefix to mirror.
    ("init",): _INIT,
    ("up",): _UP,
    ("status",): _STATUS,
    ("logs",): _LOGS,
    ("down",): _DOWN,
    # Durable subscriptions and the consume side. `sub` is a noun with
    # sub-verbs, so it gets both its own key and one per sub-verb.
    ("sub",): _SUB,
    ("sub", "add"): _SUB_ADD,
    ("sub", "list"): _SUB_LIST,
    ("sub", "show"): _SUB_SHOW,
    ("sub", "remove"): _SUB_REMOVE,
    ("watch",): _WATCH,
}

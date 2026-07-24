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
and operate a Dockerised Mosquitto deployment. The **event contract** on top of
it — typed envelopes, correlation and causation, durable history, pipelines — is
not implemented yet. The specification being built against lives in the
repository's open issues.

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

## The broker

One Eclipse Mosquitto container, published on `127.0.0.1:1883` and nowhere
else. Anonymous on loopback, no websocket listener, persistence on a named
volume. See `events explain init`.

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
    events logs --json

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
}

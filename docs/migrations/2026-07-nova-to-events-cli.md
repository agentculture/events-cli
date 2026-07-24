# Runbook — migrate the spark-f8a9 broker from the nova stack to events-cli

- **Box**: `spark-f8a9`
- **Direction**: `nova-mosquitto` + `nova-nervous-system` (LAN-exposed
  `eclipse-mosquitto:2`) -> the events-cli loopback-only broker
  (`eclipse-mosquitto:2.1.2`, `127.0.0.1:1883:1883`).
- **Tracking issue**: [events-cli#3](https://github.com/agentculture/events-cli/issues/3).
- **Plan**: `docs/plans/2026-07-23-events-first-slice.md`, task **t7**, risk
  **r3**.
- **Document status**: **AUTHORED, NOT YET EXECUTED.** This file lays out the
  commands. The live cutover — which takes down a running robot's nervous
  system — is run by the operating (main) agent, deliberately and at a chosen
  time. Authoring the runbook is a separate task from running it; see
  [Execution status](#execution-status) at the foot of this file.

## Purpose and the operator decision

events-cli now owns the MQTT broker on this box. Exactly one broker may run on
spark-f8a9, and it must be the loopback-only events-cli stack rather than the
nova stack, because:

- The nova broker publishes `1883` and `9001` on `0.0.0.0` — reachable from the
  whole LAN, the exact anti-pattern events-cli exists to replace. Docker's DNAT
  is traversed before the host firewall, so `ufw` does not save it.
- The events-cli stack publishes **only** `127.0.0.1:1883:1883`, ships no `9001`
  websocket listener, pins an exact image tag, and refuses to start on top of a
  foreign broker (`events_cli/stack/_preflight.py`).
- events-cli#3's acceptance requires the nova pair to be **gone** and a single
  loopback broker serving traffic.

The two brokers cannot coexist: they both want host port `1883`. So the cutover
is a replace, not an add — stop nova, then bring up events-cli.

## Service window warning (plan risk r3)

> **Stopping `nova-nervous-system` leaves the robot with no nervous system.**
>
> `nova-nervous-system` is the process that consumes the sensory event stream
> off the broker and turns it into the robot's reactive awareness — every rule
> in `reachy_nova/config/nervous-system/rules.yaml`: snap/person/face/speech
> detection, vision descriptions, Slack mentions and DMs, memory recalls,
> emotion and heartbeat events, each mapped to a priority/urgency and (for many)
> an LLM-evaluated injection into the robot's attention. With it stopped, none
> of that reaches the robot: it stops reacting to what it sees, hears, and is
> told.
>
> **What is lost, and for how long.** From the moment `nova-nervous-system`
> stops until **reachy-mini-cli ships its new events-cli-client publisher** (the
> composition change that binds the importable `events_cli` client — see
> events-cli#3 and reachy-mini-cli's `docs/specs/2026-07-23-reachy-nervous-system.md`),
> **no nervous system runs on the robot at all.** Bringing up the events-cli
> broker does **not** close this window: the broker is transport; it does not
> produce or process nervous-system events. The window is open for as long as
> the robot has a live broker but no publisher/consumer wired to it.
>
> **This is why the operator times the cutover deliberately.** Do not run this
> during a session where the robot is expected to be responsive. If reachy-mini-cli's
> publisher is not ready and the robot must stay reactive, **do not cut over
> yet** — or cut over, verify, and immediately [roll back](#rollback) to nova
> until the publisher ships.

## Before-state evidence

Captured read-only on **2026-07-24 08:47 UTC** on spark-f8a9, before any change.
This is the state the [rollback](#rollback) restores.

### docker ps — the nova pair is up, published on 0.0.0.0

```text
$ docker ps --filter name=nova- --format '{{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}'
nova-nervous-system  reachy_nova-nervous-system  Up 2 days
nova-mosquitto       eclipse-mosquitto:2         Up 2 days (healthy)  0.0.0.0:1883->1883/tcp, [::]:1883->1883/tcp, 0.0.0.0:9001->9001/tcp, [::]:9001->9001/tcp
```

The neighbours below share this box and **must not be touched** by the cutover
(they are unrelated stacks; the events-cli preflight only ever names port 1883):

```text
$ docker ps --format '{{.Names}}\t{{.Image}}\t{{.Ports}}'
model-gear-realtime     lobes-realtime        8080/tcp
model-gear-stt          lobes-stt             127.0.0.1:9002->9002/tcp
model-gear-chatterbox   lobes-chatterbox      9000/tcp
model-gear-gateway      lobes-gateway         0.0.0.0:8001->8000/tcp, [::]:8001->8000/tcp
model-gear-vllm-embed-deep  vllm/vllm-openai  8000/tcp
model-gear-vllm-primary     vllm/vllm-openai  8000/tcp
model-gear-vllm-rerank      vllm/vllm-openai  8000/tcp
model-gear-vllm-embed       vllm/vllm-openai  8000/tcp
eidetic-mongo   mongo:8.0            0.0.0.0:27018->27017/tcp, [::]:27018->27017/tcp
eidetic-neo4j   neo4j:5-community    0.0.0.0:7474->7474/tcp, [::]:7474->7474/tcp, 7473/tcp, 0.0.0.0:7687->7687/tcp, [::]:7687->7687/tcp
nova-nervous-system  reachy_nova-nervous-system
nova-mosquitto       eclipse-mosquitto:2   0.0.0.0:1883->1883/tcp, [::]:1883->1883/tcp, 0.0.0.0:9001->9001/tcp, [::]:9001->9001/tcp
qq-mongodb      mongo:8.0            0.0.0.0:27017->27017/tcp, [::]:27017->27017/tcp
```

### ss -ltn — 1883 and 9001 bound on the wildcard address

```text
$ ss -ltn | grep -E ':1883|:9001'
LISTEN 0  4096   0.0.0.0:1883   0.0.0.0:*
LISTEN 0  4096   0.0.0.0:9001   0.0.0.0:*
LISTEN 0  4096      [::]:1883      [::]:*
LISTEN 0  4096      [::]:9001      [::]:*
```

Both the IPv4 wildcard (`0.0.0.0`) and the IPv6 wildcard (`[::]`) are bound on
each port. After cutover, **none** of these four lines may remain — only a
single `127.0.0.1:1883` line.

### mosquitto.conf — anonymous, LAN-exposed

`reachy_nova/config/mosquitto/mosquitto.conf`, verbatim:

```text
listener 1883
allow_anonymous true
persistence true
persistence_location /mosquitto/data/
log_dest stdout
log_type all

# WebSocket listener for debugging
listener 9001
protocol websockets
```

`allow_anonymous true` with an `0.0.0.0`-published `1883` means any host on the
LAN can connect anonymously. The events-cli broker also runs `allow_anonymous
true`, but that is acceptable there **only because** its port is loopback-bound
(`events_cli/stack/templates/mosquitto.conf` documents exactly this).

## Forward cutover

Order is mandatory: **stop nova first, then start events-cli.** `events up` runs
a preflight (`events_cli/stack/_preflight.py`) that does a TCP connect to
`127.0.0.1:1883`; while `nova-mosquitto` holds the port the preflight sees it as
a foreign broker and **refuses with exit code 2**, printing `docker stop
nova-mosquitto` as the remediation. That refusal is by design — it is the guard
against two brokers on one port — so it must be satisfied by stopping nova, not
worked around.

### Step 1 — stop the nova pair

Pick one. Both free port `1883`; they differ in what they leave behind.

**Option A (recommended) — compose down, symmetric with the rollback:**

```bash
docker compose -f /home/spark/git/reachy_nova/docker-compose.nervous-system.yml down
```

Stops **and removes** both containers and the compose network. The named
volumes `mosquitto-data` and `mosquitto-log` are **kept** (no `--volumes`), so
nova's retained MQTT state survives for a rollback. This is the exact inverse of
the rollback command (`... up -d`), which is why it is preferred: `docker ps -a`
shows no nova containers afterward, and rollback recreates them cleanly.

**Option B — docker stop, most surgical:**

```bash
docker stop nova-nervous-system nova-mosquitto
```

Stops the nervous system first (it depends on the broker), then the broker.
Leaves both containers present in `Exited` state so they can be brought back
with `docker start` or the rollback `up -d`. Both services carry `restart:
unless-stopped`, and a manual `docker stop` is honoured across a daemon restart
— they stay down until explicitly started. Use this if you want to preserve the
exact container instances rather than recreate them.

### Step 2 — bring up the events-cli loopback broker

Use the installed console command if present, otherwise the no-install fallback
from an events-cli checkout (`PYTHONPATH=. python3 -m events_cli <verb>`). The
stack is written to the per-user default dir (`$XDG_CONFIG_HOME/events-cli/stack`,
i.e. `~/.config/events-cli/stack`); override with `--dir` or `$EVENTS_STACK_DIR`
only if you have a reason to.

```bash
events init      # writes compose.yaml + mosquitto.conf (idempotent; --force to regenerate)
events up        # preflight, then `docker compose up -d --wait`; blocks until healthy
events status    # reports state + health; exits 1 if not healthy
```

`events init` refuses to overwrite an existing stack without `--force`, so
re-running it on a box that already has the stack is safe (it errors with exit
1 rather than clobbering a possible remote-access opt-in). `events up` is
idempotent against its own broker — if events-cli's broker is already up it
reports `ours` and proceeds.

### Step 3 — verify the forward cutover

```bash
ss -ltn | grep -E ':1883|:9001'
# Expect exactly ONE line:  LISTEN ... 127.0.0.1:1883  0.0.0.0:*
# NO 0.0.0.0:1883, NO [::]:1883, and NOTHING on 9001 at all.

docker ps --filter publish=1883 --format '{{.Names}}\t{{.Image}}\t{{.Ports}}'
# Expect exactly ONE broker:  events-mosquitto  eclipse-mosquitto:2.1.2  127.0.0.1:1883->1883/tcp
```

## Post-cutover verification checklist

Acceptance criterion 3 of task t7, as a checklist to tick after the forward
cutover:

- [ ] `docker ps` shows **exactly one** broker container, `events-mosquitto`
      (`eclipse-mosquitto:2.1.2`), and it is healthy (`events status` exits 0).
- [ ] `nova-mosquitto` and `nova-nervous-system` are **stopped** — absent from
      `docker ps` (Option A removed them; Option B leaves them `Exited`, still
      absent from a plain `docker ps`).
- [ ] `ss -ltn` shows **no `0.0.0.0` or `[::]` binding on 1883 or 9001** — only
      `127.0.0.1:1883`.
- [ ] The published mapping is the literal `127.0.0.1:1883->1883/tcp`, not a
      bare `0.0.0.0:1883->1883/tcp`.
- [ ] The neighbour stacks are **untouched and still running**: `eidetic-mongo`,
      `eidetic-neo4j`, the `model-gear-*` stack (realtime, stt, chatterbox,
      gateway, and the four `vllm` containers), and `qq-mongodb`. Confirm with
      `docker ps` that each is still `Up`.

## Rollback

Restores the before-state exactly. Roll back if the events-cli broker will not
come up healthy, or if the robot must stay reactive and reachy-mini-cli's
publisher is not yet shipped (the [service window](#service-window-warning-plan-risk-r3)).

Free port `1883` first (the events-cli broker holds it), then re-apply nova's
compose file. The **single canonical rollback command** is the `up -d`:

```bash
events down       # stop the events-cli broker so 1883 is free (keeps its events-data volume)
docker compose -f /home/spark/git/reachy_nova/docker-compose.nervous-system.yml up -d
```

Notes:

- **The env vars resolve on their own — the `up -d` really is one command, from
  any working directory.** `nova-nervous-system` reads `AWS_ACCESS_KEY_ID`,
  `AWS_SECRET_ACCESS_KEY`, `AWS_DEFAULT_REGION`, `HEARTBEAT_INTERVAL` and
  `LLM_ENABLED` (the compose file's `environment:` block), and
  `/home/spark/git/reachy_nova/.env` exists and supplies them. Passing `-f` with
  an absolute path makes that file's directory the Compose *project directory*,
  and Compose auto-loads `.env` from there — so no `cd` and no exported vars are
  needed. Do not replace the absolute `-f` path with a relative one; that is
  what would move the project directory and lose the `.env`.
- **State preserved**: nova's `mosquitto-data` / `mosquitto-log` volumes survive
  both stop options above, so nova's retained MQTT state returns. The events-cli
  `events-data` volume is separate and is kept by `events down` (it does **not**
  pass `--volumes`), so no events-cli state is destroyed either.
- **State lost**: any events published to the events-cli broker during the
  cutover window are not migrated to nova — the two brokers share no storage.
- After rollback, re-run the [before-state evidence](#before-state-evidence)
  commands: `ss -ltn` should again show `0.0.0.0:1883` and `0.0.0.0:9001`, and
  `docker ps` should again show the nova pair `Up`.

## Forward-back-forward rehearsal

The main agent runs this full sequence to prove **both** the cutover and the
rollback work before declaring t7 done (acceptance criterion 2). Do not skip the
middle leg — an untested rollback is not a rollback.

1. **Forward** — [stop nova](#step-1--stop-the-nova-pair), then
   [bring up events-cli](#step-2--bring-up-the-events-cli-loopback-broker), then
   run the [post-cutover checklist](#post-cutover-verification-checklist). All
   boxes must tick.
2. **Back** — run the [rollback](#rollback), then confirm nova is fully
   restored: `ss -ltn` shows `0.0.0.0:1883` **and** `0.0.0.0:9001` again,
   `docker ps` shows `nova-mosquitto` (healthy) and `nova-nervous-system` (Up),
   and `events status` reports the events-cli broker is down / not running.
3. **Forward again** — repeat step 1 to leave the box in the target state:
   stop nova, `events up`, `events status`, and re-run the post-cutover
   checklist. Only now is the cutover complete.

Only after all three legs verify clean is t7's cutover done. Then close out
events-cli#3's acceptance (t9) and, before the service window closes, confirm
reachy-mini-cli's publisher is ready.

## Execution status

**This runbook is authored only.** As of writing, no `docker stop`, no `docker
compose down`, and no `docker compose up` from this runbook has been run. The
before-state evidence above was gathered with read-only commands (`docker ps`,
`ss -ltn`, reading config files) — the nova stack is untouched and still `Up 2
days` as captured. The live forward-back-forward cutover on spark-f8a9 is the
main agent's step to execute deliberately, per plan task t7 and the service-window
warning above.

#!/usr/bin/env bash
# Runnable acceptance gate for the second wave — the durable-subscription arc.
#
#   https://github.com/agentculture/events-cli/issues/7
#
# Checks the arc's success signal against the LIVE stack on this box: a
# subscription survives a broker restart with its queued backlog intact, a
# bounded drain returns a cursor, resuming from that cursor loses nothing and
# repeats nothing, one CLI process sees what another emitted, and a
# producer-owned reachy topic is never captured by the contract lane.
#
# It is a GATE, not a demo: every check prints PASS/FAIL and the script exits
# non-zero if any fail, so it can run unattended.
#
# NOT read-only, unlike scripts/acceptance-issue-3.sh. Check 4 RESTARTS THE
# BROKER, because restart-survival is the property the arc rests on and it
# cannot be observed without one. On this box that broker serves a live robot,
# so run this only inside an agreed service window. It always removes the
# subscription it created, including on failure.
#
# Usage:
#   scripts/acceptance-second-wave.sh                  # run the gate
#   scripts/acceptance-second-wave.sh --json out.json  # also write JSON evidence

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT" || exit 2

JSON_OUT=""
if [ "${1:-}" = "--json" ]; then
    JSON_OUT="${2:?--json needs an output path}"
fi

if command -v events >/dev/null 2>&1; then
    EVENTS="events"
elif [ -x "$REPO_ROOT/.venv/bin/events" ]; then
    EVENTS="$REPO_ROOT/.venv/bin/events"
else
    EVENTS="python3 -m events_cli"
fi

SUB="acceptance-w2-$$"
STACK_DIR="${EVENTS_STACK_DIR:-$HOME/.config/events-cli/stack}"
CONF="$STACK_DIR/mosquitto.conf"
N_EVENTS=5
PASSES=0
FAILURES=0
RESULTS=()

pass() { PASSES=$((PASSES + 1)); RESULTS+=("PASS|$1|$2"); printf 'PASS  %s\n      %s\n' "$1" "$2"; }
fail() { FAILURES=$((FAILURES + 1)); RESULTS+=("FAIL|$1|$2"); printf 'FAIL  %s\n      %s\n' "$1" "$2"; }

cleanup() {
    # Always remove the subscription this run created, even on failure. --force
    # so a dead broker cannot strand the registry record.
    $EVENTS sub remove "$SUB" --json >/dev/null 2>&1 \
        || $EVENTS sub remove "$SUB" --force --json >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "=== events-cli second wave — live acceptance gate ==="
echo "subscription under test: $SUB"
echo

# --- 1. the broker is healthy, loopback-only, and alone --------------------
status_json="$($EVENTS status --json 2>/dev/null)"
if echo "$status_json" | python3 -c 'import json,sys; sys.exit(0 if json.load(sys.stdin).get("healthy") else 1)' 2>/dev/null; then
    pass "broker healthy" "events status --json reports healthy"
else
    fail "broker healthy" "events status --json did not report healthy: $status_json"
fi

listeners="$(ss -ltn 2>/dev/null | awk '$4 ~ /:1883$/ {print $4}')"
off_loopback="$(echo "$listeners" | grep -v '^127\.0\.0\.1:' | grep -v '^$' || true)"
if [ -n "$listeners" ] && [ -z "$off_loopback" ]; then
    pass "loopback only" "1883 bound on 127.0.0.1 only ($listeners)"
else
    fail "loopback only" "unexpected 1883 binding: [$listeners]"
fi

brokers="$(docker ps --filter ancestor=eclipse-mosquitto:2.1.2-alpine --format '{{.Names}}' 2>/dev/null | wc -l)"
if [ "$brokers" = "1" ]; then
    pass "exactly one broker" "one mosquitto container running"
else
    fail "exactly one broker" "expected 1 mosquitto container, found $brokers"
fi

# --- 2. the deployed config carries the explicit backlog bound -------------
bound="$(grep -E '^max_queued_messages[[:space:]]+[0-9]+' "$CONF" 2>/dev/null | awk '{print $2}')"
if [ -n "$bound" ]; then
    pass "backlog bound deployed" "max_queued_messages $bound in $CONF"
else
    fail "backlog bound deployed" "no explicit max_queued_messages in $CONF"
fi

# --- 3. a subscription registers and leaves a live session ----------------
if $EVENTS sub add "$SUB" 'acceptance.*' --json >/dev/null 2>&1; then
    owner="$($EVENTS sub show "$SUB" --json 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin).get("owner",""))' 2>/dev/null)"
    pass "sub add" "registered $SUB (owner: ${owner:-unknown})"
else
    fail "sub add" "events sub add failed"
fi

# --- 4. RESTART SURVIVAL — the property the arc rests on -------------------
# Publish with no drainer connected, restart the broker, then drain.
emitted=0
for i in $(seq 1 $N_EVENTS); do
    $EVENTS emit acceptance.probe --data <(printf '{"n": %d}' "$i") \
        --correlation-id "$SUB" --json >/dev/null 2>&1 && emitted=$((emitted + 1))
done
if [ "$emitted" = "$N_EVENTS" ]; then
    pass "emit while offline" "$emitted/$N_EVENTS events published with no drainer connected"
else
    fail "emit while offline" "only $emitted/$N_EVENTS published"
fi

echo "  ... restarting the broker (the service-window step) ..."
if $EVENTS down --json >/dev/null 2>&1 && $EVENTS up --json >/dev/null 2>&1; then
    pass "broker restart" "events down + up completed"
else
    fail "broker restart" "restart failed"
fi

drain1="$($EVENTS watch "$SUB" --max 50 --timeout 30 --json 2>/dev/null)"
got="$(echo "$drain1" | python3 -c 'import json,sys; print(len(json.load(sys.stdin).get("records",[])))' 2>/dev/null || echo 0)"
ordered="$(echo "$drain1" | python3 -c '
import json,sys
recs = json.load(sys.stdin).get("records", [])
seqs = [r.get("seq") for r in recs]
print("yes" if seqs == sorted(seqs) else "no")' 2>/dev/null || echo no)"
cursor="$(echo "$drain1" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("cursor",0))' 2>/dev/null || echo 0)"
if [ "$got" = "$N_EVENTS" ] && [ "$ordered" = "yes" ]; then
    pass "restart survival" "all $got events survived the restart, in order (cursor $cursor)"
else
    fail "restart survival" "got $got/$N_EVENTS after restart, in order: $ordered"
fi

# --- 5. resuming from the cursor repeats nothing --------------------------
drain2="$($EVENTS watch "$SUB" --since "$cursor" --max 50 --timeout 5 --json 2>/dev/null)"
again="$(echo "$drain2" | python3 -c 'import json,sys; print(len(json.load(sys.stdin).get("records",[])))' 2>/dev/null || echo -1)"
if [ "$again" = "0" ]; then
    pass "cursor resume" "resuming from cursor $cursor returned nothing already acknowledged"
else
    fail "cursor resume" "expected 0 repeats from cursor $cursor, got $again"
fi

# --- 6. one CLI process sees what another emitted -------------------------
marker="roundtrip-$$"
$EVENTS emit acceptance.roundtrip --data <(printf '{"marker": "%s"}' "$marker") --json >/dev/null 2>&1
seen="$($EVENTS watch "$SUB" --since "$cursor" --max 10 --timeout 20 --json 2>/dev/null | grep -c "$marker" || true)"
if [ "$seen" -ge 1 ]; then
    pass "cross-process round trip" "a second process saw the event the first emitted"
else
    fail "cross-process round trip" "the emitted marker never arrived"
fi

# --- 7. producer-owned trees are never captured ---------------------------
before="$($EVENTS watch "$SUB" --max 50 --timeout 3 --json 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin).get("cursor",0))' 2>/dev/null || echo 0)"
docker exec events-mosquitto mosquitto_pub -h 127.0.0.1 -t 'reachy/events/probe/acceptance' -m '{"not":"ours"}' -q 1 >/dev/null 2>&1
docker exec events-mosquitto mosquitto_pub -h 127.0.0.1 -t 'reachy/state/probe' -m 'standing' -q 1 -r >/dev/null 2>&1
leaked="$($EVENTS watch "$SUB" --since "$before" --max 10 --timeout 5 --json 2>/dev/null | grep -c 'reachy' || true)"
if [ "$leaked" = "0" ]; then
    pass "producer trees excluded" "reachy/events and retained reachy/state never reached the contract lane"
else
    fail "producer trees excluded" "$leaked reachy payload(s) leaked into the drain"
fi

# --- 8. removal cleans up -------------------------------------------------
if $EVENTS sub remove "$SUB" --json >/dev/null 2>&1; then
    if $EVENTS sub show "$SUB" --json >/dev/null 2>&1; then
        fail "sub remove" "$SUB still resolves after removal"
    else
        pass "sub remove" "$SUB removed; the record is gone"
    fi
else
    fail "sub remove" "events sub remove failed"
fi

# --- report ---------------------------------------------------------------
echo
echo "=== $PASSES passed, $FAILURES failed ==="

if [ -n "$JSON_OUT" ]; then
    {
        printf '{\n  "passed": %d,\n  "failed": %d,\n  "checks": [\n' "$PASSES" "$FAILURES"
        first=1
        for row in "${RESULTS[@]}"; do
            IFS='|' read -r st name detail <<<"$row"
            [ $first = 1 ] || printf ',\n'
            first=0
            printf '    {"status": "%s", "check": "%s", "detail": "%s"}' \
                "$st" "$name" "$(echo "$detail" | sed 's/"/\\"/g')"
        done
        printf '\n  ]\n}\n'
    } >"$JSON_OUT"
    echo "evidence written to $JSON_OUT"
fi

[ "$FAILURES" = "0" ] || exit 1

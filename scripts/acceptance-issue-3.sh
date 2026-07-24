#!/usr/bin/env bash
# Runnable acceptance checklist for issue #3 — the reachy-mini-cli unblock.
#
#   https://github.com/agentculture/events-cli/issues/3
#
# Checks the five criteria the issue actually lists, in its own words, against a
# live broker on this box. Shell-probe criteria (1, 2, 5) are here; the ones
# needing a live MQTT client (3, 4) are in the Python half beside this file.
#
# It is a GATE, not a demo: every criterion prints PASS/FAIL and the script exits
# non-zero if any fail, so it can run unattended.
#
# Read-only with respect to the stack — it never starts, stops or migrates
# anything. Bring the broker up first (see docs/migrations/) and then run this.
#
# Usage:
#   scripts/acceptance-issue-3.sh              # run the checklist
#   scripts/acceptance-issue-3.sh --json out.json   # also write JSON evidence

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT" || exit 2

JSON_OUT=""
if [ "${1:-}" = "--json" ]; then
    JSON_OUT="${2:?--json needs an output path}"
fi

# Prefer the venv interpreter so the check runs against this checkout's paho.
PY="$REPO_ROOT/.venv/bin/python"
[ -x "$PY" ] || PY="$(command -v python3)"

# The console script if installed, else the documented no-install fallback.
if command -v events >/dev/null 2>&1; then
    EVENTS=(events)
else
    EVENTS=("$PY" -m events_cli)
fi

PASS_COUNT=0
FAIL_COUNT=0

pass() { printf '  PASS  %s\n' "$1"; PASS_COUNT=$((PASS_COUNT + 1)); }
fail() { printf '  FAIL  %s\n' "$1"; FAIL_COUNT=$((FAIL_COUNT + 1)); }

printf '=== issue #3 acceptance on %s (%s) ===\n\n' "$(hostname)" "$(date -u '+%Y-%m-%d %H:%M:%SZ')"

# --- criterion 1: mosquitto answers on localhost:1883 -----------------------
printf 'criterion 1 — events up yields mosquitto answering on localhost:1883\n'

if "${EVENTS[@]}" status --json >/tmp/events-status.$$ 2>/dev/null; then
    SUMMARY=$("$PY" -c "
import json
s = json.load(open('/tmp/events-status.$$'))
print(f\"{s.get('summary','?')}; endpoint={s.get('endpoint','?')}; loopback_only={s.get('loopback_only','?')}\")
" 2>/dev/null)
    pass "events status exits 0 — $SUMMARY"
else
    fail "events status exited non-zero — the broker is not healthy"
fi

if "$PY" -c "
import socket, sys
s = socket.socket(); s.settimeout(5)
sys.exit(0 if s.connect_ex(('127.0.0.1', 1883)) == 0 else 1)
"; then
    pass "TCP connect to 127.0.0.1:1883 succeeds"
else
    fail "nothing accepting on 127.0.0.1:1883"
fi

# --- criterion 2: loopback-only binding -------------------------------------
printf '\ncriterion 2 — 1883 bound on 127.0.0.1 only; non-loopback refused\n'

BINDINGS=$(ss -ltn 2>/dev/null | awk '$4 ~ /:1883$/ {print $4}')
printf '    ss -ltn shows: %s\n' "${BINDINGS:-<nothing>}"

if [ -z "$BINDINGS" ]; then
    fail "nothing is listening on 1883 at all"
elif printf '%s\n' "$BINDINGS" | grep -qE '^(0\.0\.0\.0|\*|\[?::\]?):1883$'; then
    fail "1883 is bound on a WILDCARD address — LAN-exposed"
elif printf '%s\n' "$BINDINGS" | grep -qv '^127\.0\.0\.1:1883$'; then
    fail "1883 is bound on a non-loopback address: $BINDINGS"
else
    pass "1883 is bound on 127.0.0.1 only"
fi

# The issue asks that a non-loopback connection be refused. Probe this host's own
# LAN address: the broker must not answer there even though it is the same box.
LAN_IP=$(ip -4 -o addr show scope global 2>/dev/null | awk '{split($4,a,"/"); print a[1]; exit}')
if [ -n "$LAN_IP" ]; then
    if "$PY" -c "
import socket, sys
s = socket.socket(); s.settimeout(5)
sys.exit(0 if s.connect_ex(('$LAN_IP', 1883)) == 0 else 1)
"; then
        fail "the broker ANSWERS on the LAN address $LAN_IP:1883 — it is exposed"
    else
        pass "connection to the LAN address $LAN_IP:1883 is refused"
    fi
else
    printf '    SKIP  no global-scope IPv4 address to probe\n'
fi

# 9001 (nova's websocket) and 9883 (mosquitto 2.1's dashboard) must both be absent.
for PORT in 9001 9883; do
    if ss -ltn 2>/dev/null | awk '{print $4}' | grep -qE ":${PORT}$"; then
        fail "port $PORT is listening — it must not be published"
    else
        pass "port $PORT is not listening"
    fi
done

# --- criterion 5: exactly one broker, nova absent ---------------------------
printf '\ncriterion 5 — the nova stack is absent; exactly one broker runs\n'

NOVA=$(docker ps --filter name=nova- --format '{{.Names}}' 2>/dev/null)
if [ -z "$NOVA" ]; then
    pass "nova-mosquitto and nova-nervous-system are not running"
else
    fail "nova containers still running: $(printf '%s' "$NOVA" | tr '\n' ' ')"
fi

BROKERS=$(docker ps --filter publish=1883 --format '{{.Names}}' 2>/dev/null)
BROKER_COUNT=$(printf '%s' "$BROKERS" | grep -c . || true)
if [ "$BROKER_COUNT" = "1" ]; then
    pass "exactly one broker publishes 1883: $BROKERS"
else
    fail "expected exactly 1 broker on 1883, found $BROKER_COUNT: $(printf '%s' "$BROKERS" | tr '\n' ' ')"
fi

IMAGE=$(docker ps --filter publish=1883 --format '{{.Image}}' 2>/dev/null | head -1)
if printf '%s' "$IMAGE" | grep -qE '^eclipse-mosquitto:[0-9]+\.[0-9]+\.[0-9]+(-[a-z0-9.]+)?$'; then
    pass "the running broker uses an exact pinned tag: $IMAGE"
else
    fail "the running broker's image is not an exact pin: ${IMAGE:-<none>}"
fi

# --- criteria 3 + 4: the live MQTT round-trip -------------------------------
printf '\ncriteria 3 + 4 — retained / LWT / QoS 0, and a credential-less paho client\n'

MQTT_ARGS=()
[ -n "$JSON_OUT" ] && MQTT_ARGS=(--json)

if [ -n "$JSON_OUT" ]; then
    "$PY" "$REPO_ROOT/scripts/acceptance_issue_3_mqtt.py" "${MQTT_ARGS[@]}" | tee /tmp/events-mqtt.$$
    MQTT_RC=${PIPESTATUS[0]}
    # Keep only the JSON object the Python half appended after its PASS/FAIL lines.
    "$PY" -c "
import json, sys
text = open('/tmp/events-mqtt.$$').read()
start = text.find('{')
json.dump(json.loads(text[start:]), open('$JSON_OUT', 'w'), indent=2)
" 2>/dev/null || true
else
    "$PY" "$REPO_ROOT/scripts/acceptance_issue_3_mqtt.py"
    MQTT_RC=$?
fi

if [ "$MQTT_RC" -eq 0 ]; then
    pass "the MQTT emit/consume checks all passed"
else
    fail "one or more MQTT emit/consume checks failed (see above)"
fi

rm -f "/tmp/events-status.$$" "/tmp/events-mqtt.$$"

# --- verdict ----------------------------------------------------------------
printf '\n=== %d passed, %d failed ===\n' "$PASS_COUNT" "$FAIL_COUNT"
[ "$FAIL_COUNT" -eq 0 ] || exit 1
printf 'issue #3 acceptance: PASSED\n'

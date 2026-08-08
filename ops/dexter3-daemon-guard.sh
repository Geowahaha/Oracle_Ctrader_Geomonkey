#!/usr/bin/env bash
# Dexter3 daemon guard (2026-08-08 fd-leak incident follow-up).
#
# WHAT: probe the openapi daemon's local HTTP endpoint. If the HTTP layer is
# UNRESPONSIVE (connect timeout / refused — the local-wedge signature) for 3
# consecutive runs (~15 min at the timer's 5-min cadence), restart the daemon
# and log why. A daemon that RESPONDS — even with ok:false (e.g. Spotware-side
# "Cannot route request") — is NEVER restarted: a restart cannot fix a
# server-side condition and churn would only reset spot subscriptions.
#
# WHY: on 2026-08-08 the daemon wedged on fd exhaustion for ~2h40m; every
# lane's OM tick failed silently (broker SL/TP were the only exits). This
# guard bounds any future local wedge to ~15 minutes.
#
# STATE: /run/dexter3-daemon-guard.fails (tmpfs — resets on boot, correct
# since a fresh boot starts a fresh daemon).
set -u
STATE=/run/dexter3-daemon-guard.fails
LOG=/var/log/dexter3-daemon-guard.log
ts() { date -u +%Y-%m-%dT%H:%M:%SZ; }

resp=$(curl -s -m 10 -X POST http://127.0.0.1:9877/call \
    -H 'Content-Type: application/json' -d '{"mode":"health"}' 2>/dev/null)
rc=$?

if [ $rc -eq 0 ] && [ -n "$resp" ]; then
    # HTTP layer alive -> not a local wedge; clear the strike counter.
    if [ -s "$STATE" ]; then
        echo "$(ts) recovered (http responsive again); clearing strikes" >> "$LOG"
    fi
    : > "$STATE"
    exit 0
fi

fails=$(( $(cat "$STATE" 2>/dev/null || echo 0) + 1 ))
echo "$fails" > "$STATE"
echo "$(ts) http unresponsive (curl rc=$rc) strike $fails/3" >> "$LOG"
if [ "$fails" -ge 3 ]; then
    echo "$(ts) RESTARTING dexter3-openapi-daemon (3 consecutive unresponsive probes)" >> "$LOG"
    : > "$STATE"
    systemctl restart dexter3-openapi-daemon
fi

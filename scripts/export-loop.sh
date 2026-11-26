#!/usr/bin/env bash
# Reruns one course export until it finishes, resuming each time from the course state.
# Usage: export-loop.sh <log file> <export args...>   (the same args as `python3 -m course_exporter export`)
# Exit 1 (incomplete) retries after RETRY_SECONDS; it gives up after MAX_IDLE attempts in a row publish nothing.
# Exit 0 (archived), 5 (archived with gaps: gated lessons, unsupported media), 2 (usage), 3 (busy) end the loop.
set -uo pipefail

log=$1
shift
retry_seconds=${RETRY_SECONDS:-600}
max_idle=${MAX_IDLE:-6}
cd "$(dirname "$0")/.." || exit 2

published() { jq -r '.lessons.published // 0' <<<"$1" 2>/dev/null || echo 0; }

idle=0
last=-1
attempt=0
while :; do
    attempt=$((attempt + 1))
    printf '=== attempt %d at %s\n' "$attempt" "$(date -u +%FT%TZ)" >>"$log"
    python3 -m course_exporter export --rediscover "$@" >>"$log" 2>&1
    code=$?
    summary=$(grep -E '^\{"lessons"' "$log" | tail -1)
    printf '=== exit %d\n' "$code" >>"$log"
    [[ $code -eq 1 ]] || exit "$code"
    now=$(published "$summary")
    if ((now > last)); then idle=0; else idle=$((idle + 1)); fi
    last=$now
    if ((idle >= max_idle)); then
        printf '=== giving up: %d attempts in a row published nothing\n' "$idle" >>"$log"
        exit 1
    fi
    sleep "$retry_seconds"
done

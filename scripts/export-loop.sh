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
# A systemd user service starts with a minimal PATH; yt-dlp and openclaw live in the user's own bin directories.
export PATH="$HOME/.local/bin:$HOME/.npm-global/bin:/usr/local/bin:/usr/bin:/bin"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=$XDG_RUNTIME_DIR/bus}"

cd "$(dirname "$0")/.." || exit 2
exec 3>>"$log" || exit 2
attempt_out="$log.attempt"
: >"$attempt_out" || exit 2
for tool in python3 yt-dlp ffmpeg ffprobe rclone jq openclaw; do
    command -v "$tool" >/dev/null || { printf '=== missing %s on PATH %s\n' "$tool" "$PATH" >&3; exit 2; }
done

idle=0
last=-1
attempt=0
while :; do
    attempt=$((attempt + 1))
    printf '=== attempt %d at %s\n' "$attempt" "$(date -u +%FT%TZ)" >&3
    python3 -m course_exporter export --rediscover "$@" 2>&1 | tee "$attempt_out" >&3
    code=${PIPESTATUS[0]}
    printf '=== exit %d\n' "$code" >&3
    [[ $code -eq 1 ]] || exit "$code"
    summary=$(grep -E '^\{"lessons"' "$attempt_out" | tail -1)
    now=$(jq -r '.lessons.published // 0' <<<"${summary:-{\}}" 2>/dev/null || echo 0)
    if ((now > last)); then idle=0; else idle=$((idle + 1)); fi
    last=$now
    if ((idle >= max_idle)); then
        printf '=== giving up: %d attempts in a row published nothing\n' "$idle" >&3
        exit 1
    fi
    sleep "$retry_seconds"
done

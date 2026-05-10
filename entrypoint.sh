#!/bin/bash
set -euo pipefail

# run provided command verbatim (don't touch users)
if [ "$#" -gt 0 ]; then
  exec "$@"
fi

# create runtime user to match Unraid defaults if needed
if id -u abc >/dev/null 2>&1; then :
else
  addgroup -g "${PGID:-100}" abc >/dev/null 2>&1 || true
  adduser  -D -H -u "${PUID:-99}" -G abc abc >/dev/null 2>&1 || true
fi
chown -R ${PUID:-99}:${PGID:-100} /config /logs 2>/dev/null || true
touch "$DECKMOVER_LOG" || true

# Source persistent config written by the WebUI (overrides env vars)
if [ -f /config/deckmover.env ]; then
  echo "[deckmover] Loading settings from /config/deckmover.env"
  set -a
  # shellcheck disable=SC1091
  . /config/deckmover.env
  set +a
fi

# ---- helper functions ----

to_bool() {
  case "${1:-}" in
    1|true|TRUE|yes|YES|on|ON)  echo 1 ;;
    0|false|FALSE|no|NO|off|OFF|'') echo 0 ;;
    *) echo 0 ;;
  esac
}

get_next_run_time() {
  if [ -n "${DECKMOVER_CRON:-}" ]; then
    python3 -c "
from croniter import croniter
from datetime import datetime
import sys
try:
    cron = croniter('${DECKMOVER_CRON}', datetime.now())
    next_time = cron.get_next(datetime)
    print(next_time.strftime('%Y-%m-%d %H:%M:%S'))
except Exception as e:
    print('unknown', file=sys.stderr)
" 2>/dev/null || echo "unknown"
  fi
}

# ---- unified run execution ----
execute_deckmover_run () {
  # Re-source config so WebUI edits take effect on scheduled runs too
  if [ -f /config/deckmover.env ]; then
    set -a
    # shellcheck disable=SC1091
    . /config/deckmover.env
    set +a
  fi
  echo "[deckmover] DeckMover run started: $(date)"
  echo "[deckmover] Log level: ${DECKMOVER_LOG_LEVEL:-info}"
  echo "[deckmover] Detailed logs: ${DECKMOVER_LOG:-/logs/deckmover.log}"
  echo "[deckmover] Dry run: ${RSYNC_DRY_RUN:-0} | Move warm: ${DECKMOVER_WARM_MOVE:-1} | Move back: ${DECKMOVER_MOVE_WATCHED_BACK:-0}"
  echo "[deckmover] Array: ${DECKMOVER_ARRAY_ROOT:-/mnt/user0} | Cache: ${DECKMOVER_CACHE_ROOT:-/mnt/cache}"

  /usr/local/bin/run_once.sh >> "$DECKMOVER_LOG" 2>&1

  if [ -f "$DECKMOVER_LOG" ]; then
    echo "[deckmover] -------------------------------------------------"
    copied_count=$(tail -20 "$DECKMOVER_LOG" | grep "Warm/copy phase complete" | tail -1 | sed 's/.*: \([0-9]*\) copied.*/\1/' || echo "0")
    back_count=$(tail -20 "$DECKMOVER_LOG" | grep "Move-back phase complete" | tail -1 | sed 's/.*: \([0-9]*\) items moved.*/\1/' || echo "0")
    moved_count=$(tail -20 "$DECKMOVER_LOG" | grep "moved - source deleted" | tail -1 | sed 's/.*(\([0-9]*\) moved.*/\1/' || echo "0")

    copied_count=${copied_count:-0}
    back_count=${back_count:-0}
    moved_count=${moved_count:-0}

    MOVE_BACK_ENABLED="$(to_bool "${DECKMOVER_MOVE_WATCHED_BACK:-false}")"
    if [ "$moved_count" -gt 0 ]; then
      echo "[deckmover] Warm/copy phase complete: $copied_count copied ($moved_count moved - source deleted after verify)"
    else
      echo "[deckmover] Warm/copy phase complete: $copied_count copied"
    fi
    if [ "$MOVE_BACK_ENABLED" = "1" ]; then
      echo "[deckmover] Move-back phase complete: $back_count items moved back to array"
    fi
    echo "[deckmover] -------------------------------------------------"
    echo "[deckmover] DeckMover run ended: $(date)"
    if [ -n "${DECKMOVER_CRON:-}" ]; then
      next_run=$(get_next_run_time)
      if [ "$next_run" != "unknown" ] && [ -n "$next_run" ]; then
        echo "[deckmover] next run: $next_run"
      fi
    fi
    echo "[deckmover] ==============================================="
  fi
}

# ---- pretty schedule logging ----
say_when () {
  if [ "${DECKMOVER_RUN_IMMEDIATELY:-false}" = "true" ]; then
    echo "[deckmover] scheduler: run immediately (one-shot mode)"
  elif [ -n "${DECKMOVER_CRON:-}" ]; then
    echo "[deckmover] scheduler: cron '${DECKMOVER_CRON}' (busybox crond)"
    next_run=$(get_next_run_time)
    if [ "$next_run" != "unknown" ]; then
      echo "[deckmover] next run: $next_run"
    else
      echo "[deckmover] note: next-run calculation not available for this cron pattern; run 'docker logs -f' to watch executions."
    fi
  elif [ -n "${DECKMOVER_TIME:-}" ]; then
    TARGET="${DECKMOVER_TIME:-03:15}"
    now=$(date +%s)
    next=$(date -d "$(date -d @$now +%F) $TARGET" +%s 2>/dev/null || echo $((now+86400)))
    [ "$next" -le "$now" ] && next=$(date -d "tomorrow $TARGET" +%s 2>/dev/null || echo $((now+86400)))
    echo "[deckmover] scheduler: daily at ${TARGET}"
    echo "[deckmover] next run: $(date -d @$next +'%Y-%m-%d %H:%M:%S %Z')"
  else
    echo "[deckmover] scheduler: none (one-shot only)"
  fi
}

# Start WebUI in background (non-fatal if unavailable)
python3 /opt/deckmover/webui.py &

say_when

# run immediately mode - execute once, wait for input, exit
if [ "${DECKMOVER_RUN_IMMEDIATELY:-false}" = "true" ]; then
  echo "[deckmover] Running immediately (one-shot mode)..."
  execute_deckmover_run
  echo ""
  echo "[deckmover] Run complete. Press any key to exit..."
  read -n 1 -s -r
  echo "[deckmover] Exiting."
  exit 0
fi

# cron mode if DECKMOVER_CRON is set
if [ -n "${DECKMOVER_CRON:-}" ]; then
  # Export environment variables to /etc/environment
  env | grep -E '^(DECKMOVER|PLEX|TZ|PUID|PGID|RSYNC)=' > /etc/environment

  # Create a functions-only file for cron to source
  cat > /usr/local/bin/deckmover_functions.sh <<'EOF'
# DeckMover functions (safe to source from cron)

to_bool() {
  case "${1:-}" in
    1|true|TRUE|yes|YES|on|ON)  echo 1 ;;
    0|false|FALSE|no|NO|off|OFF|'') echo 0 ;;
    *) echo 0 ;;
  esac
}

get_next_run_time() {
  if [ -n "${DECKMOVER_CRON:-}" ]; then
    python3 -c "
from croniter import croniter
from datetime import datetime
import sys
try:
    cron = croniter('${DECKMOVER_CRON}', datetime.now())
    next_time = cron.get_next(datetime)
    print(next_time.strftime('%Y-%m-%d %H:%M:%S'))
except Exception as e:
    print('unknown', file=sys.stderr)
" 2>/dev/null || echo "unknown"
  fi
}

execute_deckmover_run () {
  echo "[deckmover] DeckMover run started: $(date)"
  echo "[deckmover] Log level: ${DECKMOVER_LOG_LEVEL:-info}"
  echo "[deckmover] Detailed logs: ${DECKMOVER_LOG:-/logs/deckmover.log}"
  echo "[deckmover] Dry run: ${RSYNC_DRY_RUN:-0} | Move warm: ${DECKMOVER_WARM_MOVE:-1} | Move back: ${DECKMOVER_MOVE_WATCHED_BACK:-0}"
  echo "[deckmover] Array: ${DECKMOVER_ARRAY_ROOT:-/mnt/user0} | Cache: ${DECKMOVER_CACHE_ROOT:-/mnt/cache}"

  /usr/local/bin/run_once.sh >> "$DECKMOVER_LOG" 2>&1

  if [ -f "$DECKMOVER_LOG" ]; then
    echo "[deckmover] -------------------------------------------------"
    copied_count=$(tail -20 "$DECKMOVER_LOG" | grep "Warm/copy phase complete" | tail -1 | sed 's/.*: \([0-9]*\) copied.*/\1/' || echo "0")
    back_count=$(tail -20 "$DECKMOVER_LOG" | grep "Move-back phase complete" | tail -1 | sed 's/.*: \([0-9]*\) items moved.*/\1/' || echo "0")
    moved_count=$(tail -20 "$DECKMOVER_LOG" | grep "moved - source deleted" | tail -1 | sed 's/.*(\([0-9]*\) moved.*/\1/' || echo "0")

    copied_count=${copied_count:-0}
    back_count=${back_count:-0}
    moved_count=${moved_count:-0}

    MOVE_BACK_ENABLED="$(to_bool "${DECKMOVER_MOVE_WATCHED_BACK:-false}")"
    if [ "$moved_count" -gt 0 ]; then
      echo "[deckmover] Warm/copy phase complete: $copied_count copied ($moved_count moved - source deleted after verify)"
    else
      echo "[deckmover] Warm/copy phase complete: $copied_count copied"
    fi
    if [ "$MOVE_BACK_ENABLED" = "1" ]; then
      echo "[deckmover] Move-back phase complete: $back_count items moved back to array"
    fi
    echo "[deckmover] -------------------------------------------------"
    echo "[deckmover] DeckMover run ended: $(date)"
    if [ -n "${DECKMOVER_CRON:-}" ]; then
      next_run=$(get_next_run_time)
      if [ "$next_run" != "unknown" ] && [ -n "$next_run" ]; then
        echo "[deckmover] next run: $next_run"
      fi
    fi
    echo "[deckmover] ==============================================="
  fi
}

EOF

  # Create wrapper script that sources environment and runs main script
  cat > /usr/local/bin/cron_wrapper.sh <<'EOF'
#!/bin/sh
LOCK_FILE="/tmp/deckmover_cron.lock"

if [ -f "$LOCK_FILE" ]; then
    echo "[CRON] Another DeckMover run is already in progress, skipping this execution"
    exit 0
fi

touch "$LOCK_FILE"

set -a
. /etc/environment 2>/dev/null || true
set +a

. /usr/local/bin/deckmover_functions.sh

execute_deckmover_run

rm -f "$LOCK_FILE"
EOF
  chmod +x /usr/local/bin/cron_wrapper.sh

  mkdir -p "$(dirname "${DECKMOVER_LOG}")"
  touch "${DECKMOVER_LOG}"

  mkdir -p /var/spool/cron/crontabs
  echo "${DECKMOVER_CRON} /usr/local/bin/cron_wrapper.sh" > /var/spool/cron/crontabs/root
  chmod 0600 /var/spool/cron/crontabs/root

  exec busybox crond -f -l 0 -c /var/spool/cron/crontabs
fi

# daily time mode
TARGET="${DECKMOVER_TIME:-03:15}"
while true; do
  now=$(date +%s)
  next=$(date -d "$(date +%F) $TARGET" +%s 2>/dev/null || echo $((now+60)))
  if [ "$next" -le "$now" ]; then
    next=$(date -d "tomorrow $TARGET" +%s 2>/dev/null || echo $((now+86400)))
  fi
  echo "[deckmover] sleeping until $(date -d @$next +'%Y-%m-%d %H:%M:%S %Z')"
  sleep $(( next - now ))
  execute_deckmover_run
done

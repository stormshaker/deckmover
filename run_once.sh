#!/bin/bash
set -Eeuo pipefail

ARRAY_ROOT="${DECKMOVER_ARRAY_ROOT:-/mnt/user0}"
CACHE_ROOT="${DECKMOVER_CACHE_ROOT:-/mnt/cache}"
LOG="${DECKMOVER_LOG:-/logs/deckmover.log}"
PUID="${PUID:-99}"
PGID="${PGID:-100}"
umask 0002

# setup logging with rotation
mkdir -p "$(dirname "$LOG")"
# Rotate log on startup with timestamp
if [ -f "$LOG" ]; then
    TIMESTAMP=$(date +%Y%m%d-%H%M%S)
    # Move to timestamped backup (preserves original timestamps)
    # Change /logs/deckmover.log to /logs/deckmover.20251014-031000.log
    LOG_BASE="${LOG%.*}"  # Remove .log extension
    mv "$LOG" "${LOG_BASE}.${TIMESTAMP}.log"
    touch "$LOG"  # create new empty log file
    
    # Prune old log files - keep only the 12 newest
    LOG_DIR="$(dirname "$LOG")"
    find "$LOG_DIR" -name "$(basename "$LOG_BASE").*.log" -type f | sort -r | tail -n +13 | xargs -r rm -f
fi

to_bool() {
  case "${1:-}" in
    1|true|TRUE|yes|YES|on|ON)  echo 1 ;;
    0|false|FALSE|no|NO|off|OFF|'') echo 0 ;;
    *) echo 0 ;;
  esac
}

# Logging levels: error=0, warn=1, info=2, debug=3
LOG_LEVEL="${DECKMOVER_LOG_LEVEL:-info}"
case "${LOG_LEVEL,,}" in
  error) LOG_LEVEL_NUM=0 ;;
  warn|warning) LOG_LEVEL_NUM=1 ;;
  info) LOG_LEVEL_NUM=2 ;;
  debug) LOG_LEVEL_NUM=3 ;;
  *) LOG_LEVEL_NUM=2 ;;  # default to info
esac

log_error() { echo "[ERROR] $*" >> "$LOG"; }
log_warn()  { [ "$LOG_LEVEL_NUM" -ge 1 ] && echo "[WARN] $*" >> "$LOG" || true; }
log_info()  { [ "$LOG_LEVEL_NUM" -ge 2 ] && echo "[INFO] $*" >> "$LOG" || true; }
log_debug() { [ "$LOG_LEVEL_NUM" -ge 3 ] && echo "[DEBUG] $*" >> "$LOG" || true; }

# Note: startup_summary and completion_summary functions moved to entrypoint.sh

DRY="$(to_bool "${RSYNC_DRY_RUN:-}")"
WARM_MOVE="$(to_bool "${DECKMOVER_WARM_MOVE:-true}")"
WARM_SIDECARS="$(to_bool "${DECKMOVER_WARM_SIDECARS:-true}")"
SKIP_IF_PLAYING_WARM="$(to_bool "${DECKMOVER_SKIP_IF_PLAYING_WARM:-true}")"
MOVE_BACK="$(to_bool "${DECKMOVER_MOVE_WATCHED_BACK:-false}")"

# Note: startup summary now handled by entrypoint.sh

# base rsync cmd
RSYNC_CMD=(rsync -ahW --inplace --partial --numeric-ids --xattrs --acls)
# Add verbose flag only for debug level
[ "$LOG_LEVEL_NUM" -ge 3 ] && RSYNC_CMD+=(-v)
RSYNC_CMD+=(--chown=${PUID}:${PGID})
[ "$DRY" = "1" ] && RSYNC_CMD+=(--dry-run)


# -------------------------------
# Build the selection list (paths)
# -------------------------------
LIST_CMD='python3 /opt/deckmover/selector_sqlite.py'
if id -u abc >/dev/null 2>&1; then
  LIST_OUT="$(su abc -s /bin/sh -c "$LIST_CMD" 2>> "$LOG" || true)"
else
  LIST_OUT="$(bash -lc "$LIST_CMD" 2>> "$LOG" || true)"
fi

# Active session detection requires the Plex API (not installed); session data is
# not stored in the SQLite database. SKIP_IF_PLAYING_WARM is kept as a config
# knob for future API integration, but for now we leave the set empty so all
# candidate files are evaluated.
PLAYING_SET=""

# -----------------------------------------
# Free-space guard: size, trim, then copy
# -----------------------------------------
RESERVE_GB="${DECKMOVER_RESERVE_GB:-10}"
KEEP_FREE_GB="${DECKMOVER_MIN_FREE_GB:-20}"
TRIM_PLAN="$(to_bool "${DECKMOVER_TRIM_PLAN:-true}")"

# Collect existing sources with sizes first
log_info "Building file list from selector..."
declare -a ALL=()
TOTAL_NEED=0
while IFS= read -r src; do
  [ -z "$src" ] && continue
  if [[ "$src" != "$ARRAY_ROOT"/* ]]; then
    log_debug "Skip (not under array root): $src"
    continue
  fi
  if [[ "$src" == "$CACHE_ROOT"/* ]]; then
    log_debug "Skip (already on cache): $src"
    continue
  fi
  if [ -n "$PLAYING_SET" ] && grep -Fqx "$src" <(printf "%s\n" $PLAYING_SET); then
    log_debug "Skip (currently playing): $src"
    continue
  fi

  if [ ! -f "$src" ]; then
    # if the same file already exists on cache, it's already warm
    cache_equiv="${src/$ARRAY_ROOT/$CACHE_ROOT}"
    if [ -f "$cache_equiv" ]; then
      log_debug "Skip (already on cache): $cache_equiv"
      continue
    fi
    log_debug "Skip (missing on array): $src"
    continue
  fi

  size=$(stat -c%s "$src" 2>/dev/null || stat -f%z "$src" 2>/dev/null || echo 0)
  log_debug "Add to plan: $src ($(numfmt --to=iec $size))"
  ALL+=("$src|$size")
  TOTAL_NEED=$((TOTAL_NEED + size))
done < <(printf "%s\n" "$LIST_OUT")

FREE=$(df -PB1 "$CACHE_ROOT" | awk 'NR==2{print $4}')
RESERVE=$(( (RESERVE_GB + KEEP_FREE_GB) * 1024 * 1024 * 1024 ))
ALLOW=$(( FREE - RESERVE ))

log_info "Space analysis:"
log_info "  Cache free: $(numfmt --to=iec $FREE)"
log_info "  Total needed: $(numfmt --to=iec $TOTAL_NEED)"
log_info "  Available after reserve: $(numfmt --to=iec $ALLOW)"

if [ "$ALLOW" -le 0 ]; then
  log_warn "Not enough free space even before copying. Aborting."
  log_info "DeckMover run ended: $(date)"
  exit 0
fi

# Trim plan to what fits (or abort if trimming disabled)
log_info "Planning copy operations..."
declare -a PICKED=()
ACC=0
SKIPPED_COUNT=0
for rec in "${ALL[@]}"; do
  src="${rec%%|*}"; size="${rec##*|}"
  if [ $((ACC + size)) -le "$ALLOW" ]; then
    PICKED+=("$rec"); ACC=$((ACC + size))
  else
    if [ "$TRIM_PLAN" = "1" ]; then
      log_debug "Skip (out of space): $src"
      SKIPPED_COUNT=$((SKIPPED_COUNT + 1))
      continue
    else
      log_warn "Plan exceeds cache allowance. Aborting."
      log_info "DeckMover run ended: $(date)"
      exit 0
    fi
  fi
done
log_info "Plan ready: ${#PICKED[@]} items will be copied ($(numfmt --to=iec $ACC))"
[ "$SKIPPED_COUNT" -gt 0 ] && log_info "  ($SKIPPED_COUNT items skipped due to space constraints)"

# ----------------
# Copy loop (warm)
# ----------------
log_info "Starting warm/copy phase..."
IFS=$'\n'
COPIED_COUNT=0
MOVED_COUNT=0
for rec in "${PICKED[@]}"; do
  src="${rec%%|*}"
  dst="${src/$ARRAY_ROOT/$CACHE_ROOT}"

  # if the array source is gone but the cache copy exists, skip quietly
  if [ ! -f "$src" ] && [ -f "$dst" ]; then
    log_debug "Skip (already on cache): $dst"
    continue
  fi

  # create dest dir tree with correct ownership
  dst_dir="$(dirname "$dst")"
  mkdir -p "$dst_dir"
  chown -R ${PUID}:${PGID} "$dst_dir"
  chmod -R 775 "$dst_dir"

  log_info "Copy: $(basename "$src")"
  log_debug "  $(echo "${RSYNC_CMD[*]}" | tr '\n' ' ') '$src' '$dst'"
  "${RSYNC_CMD[@]}" "$src" "$dst"
  COPIED_COUNT=$((COPIED_COUNT + 1))

  # sidecars during warm
  if [ "$WARM_SIDECARS" = "1" ]; then
    base="${src%.*}"
    db="$(dirname "$dst")"
    for ext in srt ass sub nfo jpg png; do
      sc_src="${base}.${ext}"
      [ -f "$sc_src" ] || continue
      sc_dst="${db}/$(basename "$sc_src")"
      log_debug "  Copy sidecar: $(basename "$sc_src")"
      "${RSYNC_CMD[@]}" "$sc_src" "$sc_dst"
    done
  fi

  # verify + optional delete (turn copy into move) when NOT dry
  if [ "$DRY" != "1" ] && [ "$WARM_MOVE" = "1" ]; then
    src_sz=$(stat -c%s "$src" 2>/dev/null || stat -f%z "$src")
    dst_sz=$(stat -c%s "$dst" 2>/dev/null || stat -f%z "$dst" || echo 0)
    if [ "$src_sz" = "$dst_sz" ]; then
      log_debug "  Verify OK, removing source"
      rm -f -- "$src"
      MOVED_COUNT=$((MOVED_COUNT + 1))
      # try to tidy empty dir on array (non-fatal)
      rdir="$(dirname "$src")"
      rmdir --ignore-fail-on-non-empty "$rdir" 2>/dev/null || true
    else
      # fall back to checksum pass once, then re-check
      log_debug "  Size mismatch, verifying with checksum"
      if [ "$LOG_LEVEL_NUM" -ge 3 ]; then
        rsync -ahvW --checksum "$src" "$dst"
      else
        rsync -ahW --checksum "$src" "$dst"
      fi
      dst_sz2=$(stat -c%s "$dst" 2>/dev/null || stat -f%z "$dst" || echo 0)
      if [ "$src_sz" = "$dst_sz2" ]; then
        log_debug "  Checksum verify OK, removing source"
        rm -f -- "$src"
        MOVED_COUNT=$((MOVED_COUNT + 1))
      else
        log_warn "  Checksum verify failed, keeping source: $src"
      fi
    fi
  fi
done
log_info "Warm/copy phase complete: $COPIED_COUNT copied"
[ "$MOVED_COUNT" -gt 0 ] && log_info "  ($MOVED_COUNT moved - source deleted after verify)"

# -------------------------------------------
# Optional: Move watched items back (separate)
# -------------------------------------------
BACK_COUNT=0
if [ "$MOVE_BACK" = "1" ]; then
  log_info "Starting move-back phase (watched items)..."
  BACK_LIST="$(python3 /opt/deckmover/selector_watched_back_sqlite.py 2>> "$LOG" || true)"
  for cache_src in $BACK_LIST; do
    [ -z "$cache_src" ] && continue
    array_dst="${cache_src/$CACHE_ROOT/$ARRAY_ROOT}"
    # create dest dir tree with correct ownership
    array_dir="$(dirname "$array_dst")"
    mkdir -p "$array_dir"
    chown -R ${PUID}:${PGID} "$array_dir"
    chmod -R 775 "$array_dir"

    log_info "Move back: $(basename "$cache_src")"
    log_debug "  $(echo "${RSYNC_CMD[*]}" | tr '\n' ' ') '$cache_src' '$array_dst'"
    "${RSYNC_CMD[@]}" "$cache_src" "$array_dst"
    BACK_COUNT=$((BACK_COUNT + 1))

    if [ "$(to_bool "${DECKMOVER_MOVE_BACK_SIDECARS:-true}")" = "1" ]; then
      base="${cache_src%.*}"
      for ext in srt ass sub nfo jpg png; do
        sc="${base}.${ext}"
        if [ -f "$sc" ]; then
          log_debug "  Move back sidecar: $(basename "$sc")"
          "${RSYNC_CMD[@]}" "$sc" "${array_dst%/*}/"
        fi
      done
    fi

    if [ "$DRY" != "1" ]; then
      s=$(stat -c%s "$cache_src" 2>/dev/null || stat -f%z "$cache_src")
      d=$(stat -c%s "$array_dst" 2>/dev/null || stat -f%z "$array_dst" || echo 0)
      if [ "$s" = "$d" ]; then
        log_debug "  Verify OK, removing cache copy"
        rm -f -- "$cache_src"
        # Also remove sidecars if they were moved back
        if [ "$(to_bool "${DECKMOVER_MOVE_BACK_SIDECARS:-true}")" = "1" ]; then
          base="${cache_src%.*}"
          for ext in srt ass sub nfo jpg png; do
            sc="${base}.${ext}"
            if [ -f "$sc" ]; then
              log_debug "  Removing sidecar from cache: $(basename "$sc")"
              rm -f -- "$sc"
            fi
          done
        fi
      else
        log_warn "  Verify failed, keeping cache copy: $cache_src"
      fi
    fi
  done
  log_info "Move-back phase complete: $BACK_COUNT items moved back to array"
  
  # Clean up orphaned sidecar files (main video already moved back but sidecars left behind)
  if [ "$(to_bool "${DECKMOVER_MOVE_BACK_SIDECARS:-true}")" = "1" ]; then
    log_info "Checking for orphaned sidecar files on cache..."
    ORPHAN_COUNT=0
    
    # Query Plex database for actual library root paths, then map to cache
    SEARCH_PATHS=()
    LIBRARY_PATHS=$(python3 -c "
import os
import sys
import sqlite3

plexdb_root = os.environ.get('DECKMOVER_PLEXDB_PATH', '/plexdb')
db_path = os.path.join(plexdb_root, 'Library/Application Support/Plex Media Server/Plug-in Support/Databases/com.plexapp.plugins.library.db')

if not os.path.exists(db_path):
    sys.exit(0)

conn = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)
cursor = conn.cursor()

# Get library root paths for movie and TV libraries
cursor.execute('''
    SELECT DISTINCT sl.root_path
    FROM section_locations sl
    JOIN library_sections ls ON sl.library_section_id = ls.id
    WHERE ls.section_type IN (1, 2)
''')

paths = [row[0] for row in cursor.fetchall()]
conn.close()

# Apply path mapping (PLEX_PATH_MAP)
path_map = os.environ.get('PLEX_PATH_MAP', '')
for path in paths:
    mapped_path = path
    for pair in path_map.split(','):
        pair = pair.strip()
        if not pair or '=' not in pair:
            continue
        src, dst = pair.split('=', 1)
        src = src.rstrip('/')
        dst = dst.rstrip('/')
        if src and mapped_path.startswith(src + '/'):
            mapped_path = mapped_path.replace(src, dst, 1)
            break
    
    # Convert array path to cache path
    array_root = os.environ.get('DECKMOVER_ARRAY_ROOT', '/mnt/user0')
    cache_root = os.environ.get('DECKMOVER_CACHE_ROOT', '/mnt/cache')
    cache_path = mapped_path.replace(array_root, cache_root, 1)
    
    print(cache_path)
" 2>> "$LOG" || true)
    
    # Build array of search paths
    while IFS= read -r cache_path; do
      if [ -n "$cache_path" ] && [ -d "$cache_path" ]; then
        SEARCH_PATHS+=("$cache_path")
      fi
    done <<< "$LIBRARY_PATHS"
    
    # If no paths found, skip orphan cleanup
    if [ ${#SEARCH_PATHS[@]} -eq 0 ]; then
      log_debug "No video library paths found on cache, skipping orphan sidecar cleanup"
    else
      for search_path in "${SEARCH_PATHS[@]}"; do
        log_debug "Searching for orphaned sidecars in: $search_path"
        for ext in srt ass sub nfo jpg png; do
          while IFS= read -r -d '' sidecar; do
            # Check if corresponding video file exists on cache
            base="${sidecar%.*}"
            video_exists=0
            for vext in mkv mp4 avi m4v; do
              if [ -f "${base}.${vext}" ]; then
                video_exists=1
                break
              fi
            done
            
            # If no video file, this sidecar is orphaned - move it back
            if [ "$video_exists" = "0" ]; then
              array_sidecar="${sidecar/$CACHE_ROOT/$ARRAY_ROOT}"
              if [ "$DRY" = "1" ]; then
                log_info "Would move orphaned sidecar: $(basename "$sidecar")"
              else
                log_debug "Moving orphaned sidecar: $(basename "$sidecar")"
                array_dir="$(dirname "$array_sidecar")"
                mkdir -p "$array_dir"
                chown -R ${PUID}:${PGID} "$array_dir"
                chmod -R 775 "$array_dir"
                "${RSYNC_CMD[@]}" "$sidecar" "$array_sidecar" && rm -f -- "$sidecar"
                ORPHAN_COUNT=$((ORPHAN_COUNT + 1))
              fi
            fi
          done < <(find "$search_path" -type f -name "*.${ext}" -print0 2>/dev/null || true)
        done
      done
      [ "$ORPHAN_COUNT" -gt 0 ] && log_info "Moved $ORPHAN_COUNT orphaned sidecar files back to array"
    fi
  fi
fi

# Note: completion summary now handled by entrypoint.sh

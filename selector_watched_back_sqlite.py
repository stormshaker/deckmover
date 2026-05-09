#!/usr/bin/env python3
"""
selector_watched_back_sqlite.py - Query Plex SQLite database for watched items on cache.
Supports all users without requiring per-user tokens.
"""
import os
import sys
import sqlite3
import datetime as dt
from typing import List, Set

# ---------- helpers ----------

def should_debug():
    log_level = os.environ.get("DECKMOVER_LOG_LEVEL", "info").lower()
    return log_level == "debug"

def debug_print(*args, **kwargs):
    if should_debug():
        print(*args, **kwargs)

def env_bool(key: str, default=False) -> bool:
    v = os.environ.get(key)
    if v is None:
        return default
    return str(v).lower() in ("1", "true", "yes", "on")

def env_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, str(default)))
    except Exception:
        return default

def env_list(key: str):
    v = os.environ.get(key, "").strip()
    if not v:
        return []
    return [x.strip() for x in v.split(",") if x.strip()]

def apply_path_map(path: str) -> str:
    """
    Translate container-internal paths (e.g. /data/...) to host paths.
    PLEX_PATH_MAP example: "/data=/mnt/user,/media=/mnt/user"
    """
    maps = os.environ.get("PLEX_PATH_MAP", "")
    for pair in maps.split(","):
        pair = pair.strip()
        if not pair or "=" not in pair:
            continue
        src, dst = pair.split("=", 1)
        src = src.rstrip("/")
        dst = dst.rstrip("/")
        if src and path.startswith(src + "/"):
            return path.replace(src, dst, 1)
    return path

# ---------- database queries ----------

def get_watched_items(db_path: str, include_libs: Set[str], only_libs: Set[str], 
                     min_age_days: int, cache_root: str, array_root: str) -> List[str]:
    """
    Query items that should be moved back from cache to array.
    
    Items are moved back if they are:
    1. Currently on cache
    2. NOT in anyone's Continue Watching (not in progress, not next episode)
    
    Strategy: Find all items on cache, exclude items that are in Continue Watching.
    """
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    results = []
    
    try:
        # Calculate cutoff timestamp if age filter is enabled
        cutoff_timestamp = None
        if min_age_days > 0:
            cutoff_time = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=min_age_days)
            cutoff_timestamp = int(cutoff_time.timestamp())
        
        # Find items that are NOT in anyone's Continue Watching
        # Items in Continue Watching are: partial episodes/movies OR next unwatched episodes
        # Move back items that are fully watched and not "up next"
        # IMPORTANT: For multi-episode files, exclude if ANY episode in the file is in Continue Watching
        query = """
        SELECT DISTINCT
            mi.id as metadata_id,
            mi.title,
            ls.name as library_name,
            mp.file as file_path,
            (SELECT MAX(miv_age.viewed_at) FROM metadata_item_views miv_age WHERE miv_age.guid = mi.guid) as last_viewed_at
        FROM metadata_items mi
        JOIN library_sections ls ON mi.library_section_id = ls.id
        JOIN media_items med ON mi.id = med.metadata_item_id
        JOIN media_parts mp ON med.id = mp.media_item_id
        WHERE mp.file IS NOT NULL
            AND mi.metadata_type IN (1, 4)
            -- Exclude if ANY user has this item in progress (view_offset > 0)
            AND mi.id NOT IN (
                SELECT DISTINCT mi2.id
                FROM metadata_items mi2
                LEFT JOIN metadata_item_settings mis ON mi2.guid = mis.guid
                WHERE COALESCE(mis.view_offset, 0) > 0
            )
            -- Exclude if this is a next unwatched episode in a started season for ANY user
            AND mi.id NOT IN (
                SELECT DISTINCT unwatched_ep.id
                FROM metadata_items unwatched_ep
                WHERE unwatched_ep.metadata_type = 4
                    AND unwatched_ep.parent_id IN (
                        -- Find seasons where ANY user has watched at least one episode
                        SELECT DISTINCT watched_ep.parent_id
                        FROM metadata_items watched_ep
                        JOIN metadata_item_views watched_view ON watched_ep.guid = watched_view.guid
                        WHERE watched_ep.metadata_type = 4
                            AND watched_view.viewed_at IS NOT NULL
                    )
                    -- And this episode hasn't been watched by ALL users who have watched other episodes in this season
                    AND EXISTS (
                        SELECT 1
                        FROM metadata_items sibling_ep
                        JOIN metadata_item_views sibling_view ON sibling_ep.guid = sibling_view.guid
                        WHERE sibling_ep.parent_id = unwatched_ep.parent_id
                            AND sibling_ep.metadata_type = 4
                            AND sibling_view.viewed_at IS NOT NULL
                            AND NOT EXISTS (
                                SELECT 1
                                FROM metadata_item_views unwatched_view
                                WHERE unwatched_view.guid = unwatched_ep.guid
                                    AND unwatched_view.account_id = sibling_view.account_id
                                    AND unwatched_view.viewed_at IS NOT NULL
                            )
                    )
            )
            -- CRITICAL: Exclude files that contain OTHER episodes in Continue Watching (multi-episode files)
            AND mp.file NOT IN (
                SELECT DISTINCT mp_cw.file
                FROM metadata_items mi_cw
                JOIN media_items med_cw ON mi_cw.id = med_cw.metadata_item_id
                JOIN media_parts mp_cw ON med_cw.id = mp_cw.media_item_id
                LEFT JOIN metadata_item_settings mis_cw ON mi_cw.guid = mis_cw.guid
                WHERE mi_cw.metadata_type = 4
                    AND (
                        -- Episode is in progress
                        COALESCE(mis_cw.view_offset, 0) > 0
                        -- OR episode is next unwatched in a started season
                        OR mi_cw.id IN (
                            SELECT DISTINCT unwatched_ep2.id
                            FROM metadata_items unwatched_ep2
                            WHERE unwatched_ep2.metadata_type = 4
                                AND unwatched_ep2.parent_id IN (
                                    SELECT DISTINCT watched_ep2.parent_id
                                    FROM metadata_items watched_ep2
                                    JOIN metadata_item_views watched_view2 ON watched_ep2.guid = watched_view2.guid
                                    WHERE watched_ep2.metadata_type = 4
                                        AND watched_view2.viewed_at IS NOT NULL
                                )
                                AND EXISTS (
                                    SELECT 1
                                    FROM metadata_items sibling_ep2
                                    JOIN metadata_item_views sibling_view2 ON sibling_ep2.guid = sibling_view2.guid
                                    WHERE sibling_ep2.parent_id = unwatched_ep2.parent_id
                                        AND sibling_ep2.metadata_type = 4
                                        AND sibling_view2.viewed_at IS NOT NULL
                                        AND NOT EXISTS (
                                            SELECT 1
                                            FROM metadata_item_views unwatched_view2
                                            WHERE unwatched_view2.guid = unwatched_ep2.guid
                                                AND unwatched_view2.account_id = sibling_view2.account_id
                                                AND unwatched_view2.viewed_at IS NOT NULL
                                        )
                                )
                        )
                    )
            )
            -- Exclude newly added movies (within last 30 days) from move-back
            AND NOT (
                mi.metadata_type = 1 
                AND mi.added_at > (strftime('%s', 'now') - (30 * 24 * 60 * 60))
            )
        """
        
        cursor.execute(query)
        
        for row in cursor.fetchall():
            library_name = row['library_name']

            # Apply library filters
            if include_libs and library_name not in include_libs:
                continue
            if only_libs and library_name not in only_libs:
                continue

            # Apply age filter: skip items viewed more recently than the cutoff
            if cutoff_timestamp is not None:
                last_viewed = row['last_viewed_at']
                if last_viewed is not None and last_viewed > cutoff_timestamp:
                    continue

            file_path = row['file_path']
            
            # Map to host path
            host_path = apply_path_map(file_path)
            
            # Only process items that are actually on cache
            # The file path from Plex database points to the array location
            # We need to check if there's a corresponding file on cache
            cache_root_clean = cache_root.rstrip("/")
            array_root_clean = array_root.rstrip("/")
            
            # Convert array path to cache path
            cache_path = None
            if host_path.startswith(array_root_clean + "/"):
                cache_path = host_path.replace(array_root_clean, cache_root_clean, 1)
            elif host_path.startswith("/mnt/user/"):
                cache_path = host_path.replace("/mnt/user", cache_root_clean, 1)
            elif host_path.startswith(cache_root_clean + "/"):
                # Already on cache
                cache_path = host_path
            
            # Only add if the cache file actually exists
            if cache_path and os.path.isfile(cache_path):
                results.append(cache_path)
        
    except Exception as e:
        print(f"[ERROR] Watched items query error: {e}", file=sys.stderr)
    finally:
        conn.close()
    
    return results

# ---------- main ----------

def main():
    # Database path
    plexdb_root = os.environ.get("DECKMOVER_PLEXDB_PATH", "/plexdb")
    db_path = os.path.join(plexdb_root, "Library/Application Support/Plex Media Server/Plug-in Support/Databases/com.plexapp.plugins.library.db")
    
    if not os.path.exists(db_path):
        print(f"[ERROR] Plex database not found at: {db_path}", file=sys.stderr)
        print("[ERROR] Ensure DECKMOVER_PLEXDB_PATH is set correctly and mounted", file=sys.stderr)
        sys.exit(2)
    
    # Configuration
    array_root = os.environ.get("DECKMOVER_ARRAY_ROOT", "/mnt/user0").rstrip("/")
    cache_root = os.environ.get("DECKMOVER_CACHE_ROOT", "/mnt/cache").rstrip("/")

    min_age_days = env_int("DECKMOVER_MOVE_BACK_MIN_AGE_DAYS", 0)
    include_libs = set(env_list("PLEX_LIBRARIES"))
    only_libs = set(env_list("DECKMOVER_LIBRARIES_ONLY"))

    watched_items = get_watched_items(db_path, include_libs, only_libs,
                                     min_age_days, cache_root, array_root)

    seen = set()
    for cache_path in watched_items:
        if cache_path in seen:
            continue
        seen.add(cache_path)
        print(cache_path)

if __name__ == "__main__":
    main()



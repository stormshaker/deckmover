#!/usr/bin/env python3
"""
selector_sqlite.py - Query Plex SQLite database directly for On Deck items.
Supports all users without requiring per-user tokens.
"""
import os
import sys
import sqlite3
from typing import List, Set, Tuple

# ---------- helpers ----------

def should_debug():
    log_level = os.environ.get("DECKMOVER_LOG_LEVEL", "info").lower()
    return log_level == "debug"

def debug_print(*args, **kwargs):
    if should_debug():
        print(*args, **kwargs)

def env_bool(key: str, default: bool = False) -> bool:
    v = os.environ.get(key, "")
    if v == "" and default is not None:
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
    Translate container-internal paths to host paths.
    Example env: PLEX_PATH_MAP="/data=/mnt/user,/media=/mnt/user"
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

def normalize_to_array_root(p: str, array_root: str) -> str:
    """
    Force anything under /mnt/user to the array-only root (/mnt/user0 by default).
    """
    array_root = array_root.rstrip("/")
    if p.startswith("/mnt/user/"):
        return p.replace("/mnt/user", array_root, 1)
    return p

# ---------- database queries ----------

def get_ondeck_items(db_path: str, max_per_user: int, include_libs: Set[str], only_libs: Set[str]) -> List[Tuple[str, int]]:
    """
    Query On Deck items for all users.
    Returns list of (file_path, metadata_id) tuples.
    
    On Deck logic:
    - Partially watched episodes (TV shows) - next unwatched episode in a started show
    - Unwatched movies in libraries
    - Recent additions
    """
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        results = []
        # Test basic connection
        cursor.execute("SELECT COUNT(*) FROM metadata_items")
        item_count = cursor.fetchone()[0]
        debug_print(f"[DEBUG] Connected to database, found {item_count} metadata items", file=sys.stderr)
        
        # Get all user accounts, ordered by most recent activity (most active users first)
        cursor.execute("""
            SELECT DISTINCT a.id, a.name, MAX(miv.viewed_at) as last_view
            FROM accounts a
            LEFT JOIN metadata_item_views miv ON a.id = miv.account_id
            WHERE a.id > 0
            GROUP BY a.id
            ORDER BY last_view DESC NULLS LAST
        """)
        account_rows = cursor.fetchall()
        account_ids = [row[0] for row in account_rows]
        account_names = {row[0]: row[1] for row in account_rows}
        debug_print(f"[DEBUG] Found {len(account_ids)} accounts, ordered by most recent activity", file=sys.stderr)

        ignore_accounts = {
            a.strip().lower()
            for a in os.environ.get("DECKMOVER_IGNORE_ACCOUNTS", "").split(",")
            if a.strip()
        }
        if ignore_accounts:
            account_ids = [
                aid for aid in account_ids
                if account_names.get(aid, "").lower() not in ignore_accounts
            ]
            debug_print(f"[DEBUG] After ignore filter: {len(account_ids)} accounts remain", file=sys.stderr)

        if not account_ids:
            # Fallback to admin account (id=1) if no accounts found
            account_ids = [1]
        
        for account_id in account_ids:
            # Stop if we've already collected enough items
            if len(results) >= max_per_user * 20:  # Reasonable upper limit to avoid over-querying
                debug_print(f"[DEBUG] Reached item limit, stopping at {len(results)} items from On Deck", file=sys.stderr)
                break
            
            # Query for On Deck items per user - match Plex's "Continue Watching" logic
            # Strategy: Find next unwatched episodes + partially watched movies
            # For episodes: sort by the season's most recent viewing activity (not the episode's add date)
            # IMPORTANT: Only select ONE episode per show/season (the next unwatched one)
            query = """
            SELECT 
                mp.file as file_path,
                mi.id as metadata_id,
                mi.title,
                ls.name as library_name,
                mi.parent_id,
                mi."index",
                parent_mi."index" as season_number,
                miv.viewed_at,
                mis.view_offset,
                mis.view_count,
                mi.added_at,
                CASE 
                    WHEN mi.metadata_type = 4 AND mis.view_offset > 0 THEN 'partial_episode'
                    WHEN mi.metadata_type = 4 AND miv.viewed_at IS NULL AND mi.parent_id IN (
                        SELECT DISTINCT mi2.parent_id 
                        FROM metadata_items mi2
                        LEFT JOIN metadata_item_views miv2 ON mi2.guid = miv2.guid AND miv2.account_id = ?
                        WHERE mi2.metadata_type = 4 AND miv2.viewed_at IS NOT NULL
                    ) THEN 'next_episode'
                    WHEN mi.metadata_type = 1 AND mis.view_offset > 0 THEN 'partial_movie'
                    ELSE 'new_content'
                END as item_type,
                -- For episodes, use the season's most recent viewing activity
                -- For movies, use the movie's own viewing activity
                CASE 
                    WHEN mi.metadata_type = 4 THEN (
                        SELECT MAX(COALESCE(mis2.last_viewed_at, miv2.viewed_at))
                        FROM metadata_items mi2
                        LEFT JOIN metadata_item_views miv2 ON mi2.guid = miv2.guid AND miv2.account_id = ?
                        LEFT JOIN metadata_item_settings mis2 ON mi2.guid = mis2.guid AND mis2.account_id = ?
                        WHERE mi2.parent_id = mi.parent_id
                    )
                    ELSE COALESCE(mis.last_viewed_at, miv.viewed_at, mi.added_at)
                END as sort_time
            FROM metadata_items mi
            JOIN library_sections ls ON mi.library_section_id = ls.id
            JOIN media_items med ON mi.id = med.metadata_item_id
            JOIN media_parts mp ON med.id = mp.media_item_id
            LEFT JOIN metadata_item_views miv ON mi.guid = miv.guid AND miv.account_id = ?
            LEFT JOIN metadata_item_settings mis ON mi.guid = mis.guid AND mis.account_id = ?
            LEFT JOIN metadata_items parent_mi ON mi.parent_id = parent_mi.id
            WHERE mp.file IS NOT NULL
                AND mi.metadata_type IN (1, 4)  -- Movies and Episodes
                AND ls.section_type IN (1, 2)   -- Movie and TV libraries
                AND (
                    -- Episodes: in-progress episodes (started but not finished)
                    (mi.metadata_type = 4 AND mis.view_offset > 0)
                    OR
                    -- Episodes: next unwatched episode of shows that have been started
                    -- Only select the FIRST (lowest index) unwatched episode per season
                    (mi.metadata_type = 4 AND miv.viewed_at IS NULL AND mi.parent_id IN (
                        SELECT DISTINCT mi2.parent_id 
                        FROM metadata_items mi2
                        LEFT JOIN metadata_item_views miv2 ON mi2.guid = miv2.guid AND miv2.account_id = ?
                        WHERE mi2.metadata_type = 4 
                        AND miv2.viewed_at IS NOT NULL
                        AND mi2.parent_id = mi.parent_id
                    ) AND mi."index" = (
                        SELECT MIN(mi3."index")
                        FROM metadata_items mi3
                        LEFT JOIN metadata_item_views miv3 ON mi3.guid = miv3.guid AND miv3.account_id = ?
                        WHERE mi3.parent_id = mi.parent_id
                        AND mi3.metadata_type = 4
                        AND miv3.viewed_at IS NULL
                    ))
                    OR
                    -- Movies: in progress (started, regardless of completion status)
                    (mi.metadata_type = 1 AND mis.view_offset > 0)
                )
            GROUP BY mp.file
            ORDER BY 
                -- Sort by most recent viewing activity (for episodes, use season's activity)
                sort_time DESC
            LIMIT ?
            """
            
            cursor.execute(query, (account_id, account_id, account_id, account_id, account_id, account_id, account_id, max_per_user))
            rows = cursor.fetchall()
            account_name = account_names.get(account_id, f"ID:{account_id}")
            if rows:
                debug_print(f"[DEBUG] Adding {len(rows)} On Deck items for user: {account_name}", file=sys.stderr)
            
            for row in rows:
                library_name = row['library_name']
                title = row['title']
                episode_index = row['index']
                season_number = row['season_number']
                item_type = row['item_type']
                viewed_at = row['viewed_at']
                
                # Apply library filters
                if include_libs and library_name not in include_libs:
                    continue
                if only_libs and library_name not in only_libs:
                    continue
                
                file_path = row['file_path']
                metadata_id = row['metadata_id']
                results.append((file_path, metadata_id))
                
                # Log individual files with context
                if item_type == 'partial_episode':
                    season_str = f"S{season_number}" if season_number else "S?"
                    debug_print(f"[DEBUG]   - {title} ({season_str}E{episode_index}, partial) - {file_path}", file=sys.stderr)
                elif item_type == 'next_episode':
                    season_str = f"S{season_number}" if season_number else "S?"
                    debug_print(f"[DEBUG]   - {title} ({season_str}E{episode_index}) - {file_path}", file=sys.stderr)
                elif item_type == 'partial_movie':
                    debug_print(f"[DEBUG]   - {title} (partial movie) - {file_path}", file=sys.stderr)
                else:
                    debug_print(f"[DEBUG]   - {title} (new movie) - {file_path}", file=sys.stderr)
        
    except Exception as e:
        print(f"[ERROR] On Deck error: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc(file=sys.stderr)
    finally:
        if 'conn' in locals():
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
    ondeck_enabled = env_bool("DECKMOVER_ONDECK", True)
    ondeck_count = env_int("DECKMOVER_ONDECK_COUNT", 10)
    max_items = env_int("DECKMOVER_MAX_ITEMS", 100)
    
    include_libs = set(env_list("PLEX_LIBRARIES"))
    only_libs = set(env_list("DECKMOVER_LIBRARIES_ONLY"))
    
    array_root = os.environ.get("DECKMOVER_ARRAY_ROOT", "/mnt/user0")
    
    # Collect items
    items = []
    
    if ondeck_enabled:
        ondeck_items = get_ondeck_items(db_path, ondeck_count, include_libs, only_libs)
        items.extend(ondeck_items)
    
    # Deduplicate while preserving order
    seen_paths = set()
    seen_ids = set()
    final_paths = []
    
    for file_path, metadata_id in items:
        # Skip if we've already processed this file or metadata item
        if file_path in seen_paths or metadata_id in seen_ids:
            continue
        
        seen_paths.add(file_path)
        seen_ids.add(metadata_id)
        
        # Apply path mapping and normalization
        mapped = apply_path_map(file_path)
        normalized = normalize_to_array_root(mapped, array_root)
        final_paths.append(normalized)
        
        if len(final_paths) >= max_items:
            break
    
    # Output file paths
    for path in final_paths:
        print(path)

if __name__ == "__main__":
    main()
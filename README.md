# DeckMover for Unraid

Warm your Unraid cache with Plex "Continue Watching" items. Optionally move watched items back to the array. Safe, Unraid-specific paths. No symlink tricks. Minimal knobs, sensible defaults.

This is a lightweight Docker container designed for **Unraid servers** that:

* Reads Plex's SQLite database directly to identify "On Deck" items from **all users**
* Copies those items from **/mnt/user0** (array only) to **/mnt/cache**
* Optionally moves watched items back from cache to array
* Honors Unraid permissions via `PUID=99` `PGID=100`
* Schedules daily runs or uses cron expressions
* Provides clean logging with startup/completion summaries

> Credit: Inspired by **[bexem/PlexCache](https://github.com/bexem/PlexCache)** — original cache warming concept. DeckMover is a new codebase with direct SQLite database access, multi-user support, and Unraid-native copy/move mechanics.

---

## Contents

* [What this does](#what-this-does)
* [Architecture](#architecture)
* [Requirements](#requirements)
* [Quick start](#quick-start)
* [Unraid template](#unraid-template)
* [Environment variables](#environment-variables)
* [Scheduling](#scheduling)
* [Permissions and ownership](#permissions-and-ownership)
* [Logging](#logging)
* [How On Deck selection works](#how-on-deck-selection-works)
* [Build script](#build-script)
* [Update workflow](#update-workflow)
* [Troubleshooting](#troubleshooting)

---

## What this does

* Reads Plex's SQLite database directly to gather **"Continue Watching"** items for **all users**
* Identifies:
  * Episodes you've started watching but haven't finished
  * Next unwatched episode of shows you're currently watching
  * Movies you've started but haven't finished
* Translates Plex media paths to Unraid host paths via `PLEX_PATH_MAP` (e.g. `/data=/mnt/user`)
* For each selected media file:
  * Plans copies that **fit** the cache free-space budget
  * **Copies** from `/mnt/user0/...` to `/mnt/cache/...`
  * Verifies sizes (optionally checksum on mismatch)
  * If `DECKMOVER_WARM_MOVE=true` and not dry-run, **deletes the array source** after successful verify
  * Copies common sidecars (`srt`, `ass`, `sub`, `nfo`, `jpg`, `png`) if enabled
* Optional **move-back** phase: when `DECKMOVER_MOVE_WATCHED_BACK=true`, watched items found on cache are copied back to the array and the cache copy is removed after verify
* Skips items already on cache on subsequent runs

---

## Architecture

```
Plex SQLite Database
   │
   ├─ selector_sqlite.py                   # Queries "Continue Watching" for all users
   ├─ selector_watched_back_sqlite.py      # Queries watched items for move-back
   └─ run_once.sh                          # Free-space planning → rsync warm/move → optional watched-back
        ├─ Safeguards: /mnt/user0 → /mnt/cache only
        ├─ PUID/PGID ownership
        ├─ Sidecars support
        └─ Clean logging with summaries

entrypoint.sh                              # Sets up scheduling (daily time or cron) + startup/completion summaries
Dockerfile                                 # Installs Python, rsync, and copies selector scripts
```

**Direct SQLite access:**
* Reads Plex's `com.plexapp.plugins.library.db` database
* No API authentication required
* Covers **all users** automatically
* Faster and more reliable than API-based selection

---

## Requirements

* Unraid with Docker
* Access to your Plex Media Server's database directory (typically `/mnt/user/appdata/plex/`)
* Your Plex container's library path mapping to build `PLEX_PATH_MAP` (e.g. Plex uses `/data` → host `/mnt/user`)

---

## Quick start

1. **Create directory structure:**

   ```bash
   mkdir -p /mnt/user/appdata/deckmover/config
   mkdir -p /mnt/user/appdata/deckmover/logs
   ```

2. **Pull from GHCR:**

   ```bash
   docker pull ghcr.io/stormshaker/deckmover:latest
   ```

   Or build locally (see [Build script](#build-script)).

3. **Add the container via Unraid GUI → Docker → Add Container**, set:

   * **Repository:** `ghcr.io/stormshaker/deckmover:latest`
   * **Network:** Bridge (Host not required)
   * **Paths:**
     * `/mnt/user0` → `/mnt/user0` (Read/Write)
     * `/mnt/cache` → `/mnt/cache` (Read/Write)
     * `/config` → `/mnt/user/appdata/deckmover/config` (Read/Write)
     * `/logs` → `/mnt/user/appdata/deckmover/logs` (Read/Write)
     * **Required:** `/plexdb` → `/mnt/user/appdata/plex` (Read Only)
   * **Variables (minimum):**
     * `PLEX_PATH_MAP=/data=/mnt/user`
     * `PUID=99`
     * `PGID=100`
     * `DECKMOVER_ONDECK=true`
     * `DECKMOVER_WARM_MOVE=true`
     * `DECKMOVER_TIME=03:15`

4. **Apply**, then check logs at `/mnt/user/appdata/deckmover/logs/deckmover.log`

---

## Unraid template

Save as `/boot/config/plugins/dockerMan/templates-user/my-DeckMover.xml`

Key template structure:

* `<Container version="2">`
* Each `<Config …/>` sits directly under `<Container>`
* Add an **Icon** path if you want a logo (e.g. `https://raw.githubusercontent.com/stormshaker/deckmover/master/icon.png`)

Example minimal template:

```xml
<?xml version="1.0"?>
<Container version="2">
  <Name>DeckMover</Name>
  <Repository>ghcr.io/stormshaker/deckmover:latest</Repository>
  <Registry>https://ghcr.io</Registry>
  <Network>bridge</Network>
  <Privileged>false</Privileged>
  <Support>https://github.com/stormshaker/deckmover</Support>
  <Project>https://github.com/stormshaker/deckmover</Project>
  <Overview>Warm Unraid cache with Plex Continue Watching items using direct SQLite database access.</Overview>
  <Category>MediaApp:Other MediaServer:Other</Category>

  <Config Name="Array Root" Target="/mnt/user0" Default="/mnt/user0" Mode="rw" Description="Source (array only)" Type="Path" Display="always" Required="true" Mask="false">/mnt/user0</Config>
  <Config Name="Cache Root" Target="/mnt/cache" Default="/mnt/cache" Mode="rw" Description="Destination (cache)" Type="Path" Display="always" Required="true" Mask="false">/mnt/cache</Config>
  <Config Name="Config" Target="/config" Default="/mnt/user/appdata/deckmover/config" Mode="rw" Description="Config directory" Type="Path" Display="advanced" Required="true" Mask="false">/mnt/user/appdata/deckmover/config</Config>
  <Config Name="Logs" Target="/logs" Default="/mnt/user/appdata/deckmover/logs" Mode="rw" Description="Log directory" Type="Path" Display="advanced" Required="true" Mask="false">/mnt/user/appdata/deckmover/logs</Config>
  <Config Name="Plex Database" Target="/plexdb" Default="/mnt/user/appdata/plex" Mode="ro" Description="Plex appdata root (for SQLite access)" Type="Path" Display="always" Required="true" Mask="false">/mnt/user/appdata/plex</Config>

  <Config Name="PLEX_PATH_MAP" Target="PLEX_PATH_MAP" Default="/data=/mnt/user" Mode="" Description="Path mapping from Plex to Unraid (e.g. /data=/mnt/user)" Type="Variable" Display="always" Required="true" Mask="false">/data=/mnt/user</Config>
  <Config Name="PUID" Target="PUID" Default="99" Mode="" Description="User ID (99=nobody)" Type="Variable" Display="advanced" Required="true" Mask="false">99</Config>
  <Config Name="PGID" Target="PGID" Default="100" Mode="" Description="Group ID (100=users)" Type="Variable" Display="advanced" Required="true" Mask="false">100</Config>

  <Config Name="DECKMOVER_ONDECK" Target="DECKMOVER_ONDECK" Default="true" Mode="" Description="Include On Deck items" Type="Variable" Display="always" Required="false" Mask="false">true</Config>
  <Config Name="DECKMOVER_ONDECK_COUNT" Target="DECKMOVER_ONDECK_COUNT" Default="10" Mode="" Description="Max On Deck items per user" Type="Variable" Display="always" Required="false" Mask="false">10</Config>
  <Config Name="DECKMOVER_WARM_MOVE" Target="DECKMOVER_WARM_MOVE" Default="true" Mode="" Description="Delete array source after copy (prevents duplicates)" Type="Variable" Display="always" Required="false" Mask="false">true</Config>
  <Config Name="DECKMOVER_TIME" Target="DECKMOVER_TIME" Default="03:15" Mode="" Description="Daily run time (HH:MM)" Type="Variable" Display="always" Required="false" Mask="false">03:15</Config>
  <Config Name="DECKMOVER_LOG_LEVEL" Target="DECKMOVER_LOG_LEVEL" Default="info" Mode="" Description="Log level (error, warn, info, debug)" Type="Variable" Display="advanced" Required="false" Mask="false">info</Config>
</Container>
```

---

## Environment variables

| Variable                            | Default               | Purpose                                                                                             |
| ----------------------------------- | --------------------- | --------------------------------------------------------------------------------------------------- |
| `PLEX_PATH_MAP`                     | `/data=/mnt/user`     | Translate Plex paths to Unraid host paths. Format: `plex_path=host_path`. Comma-separated for multiple mappings. |
| `PLEX_LIBRARIES`                    | *(blank)*             | Comma list of library names to include (e.g. `Movies,TV Shows`). Blank = all libraries.            |
| `DECKMOVER_PLEXDB_PATH`             | `/plexdb`             | Container path to Plex database root. Bind mount your Plex appdata here.                           |
| `DECKMOVER_ONDECK`                  | `true`                | Include "Continue Watching" items (in-progress and next episodes).                                  |
| `DECKMOVER_ONDECK_COUNT`            | `10`                  | Max "Continue Watching" items per user.                                                             |
| `DECKMOVER_MAX_ITEMS`               | `100`                 | Hard cap for total items (across all users and sources).                                            |
| `DECKMOVER_ARRAY_ROOT`              | `/mnt/user0`          | Source root (array only). Mirrors Unraid Mover behavior.                                            |
| `DECKMOVER_CACHE_ROOT`              | `/mnt/cache`          | Destination cache root.                                                                             |
| `DECKMOVER_WARM_MOVE`               | `true`                | Copy to cache then delete array source after verify. Prevents duplicates in user shares.           |
| `DECKMOVER_WARM_SIDECARS`           | `true`                | Copy subtitle and metadata sidecars with the media during warm.                                     |
| `DECKMOVER_MOVE_WATCHED_BACK`       | `false`               | Move fully watched items found on cache back to array and remove cache copy.                        |
| `DECKMOVER_MOVE_BACK_MIN_AGE_DAYS`  | `0`                   | Only move back if last viewed older than N days. 0 = any age.                                       |
| `DECKMOVER_MOVE_BACK_SIDECARS`      | `true`                | Move sidecars together with the media on move-back.                                                 |
| `DECKMOVER_MIN_FREE_GB`             | `20`                  | Leave at least this much free space on cache after the run.                                         |
| `DECKMOVER_RESERVE_GB`              | `10`                  | Extra buffer on top of MIN_FREE that's never touched.                                               |
| `DECKMOVER_TRIM_PLAN`               | `true`                | If the plan doesn't fit, trim it to what fits; if `false`, abort the entire run instead.           |
| `DECKMOVER_TIME`                    | `03:15`               | Daily time scheduler (HH:MM format). Leave blank if using CRON or RUN_IMMEDIATELY.                 |
| `DECKMOVER_CRON`                    | *(blank)*             | Cron expression alternative (e.g. `0 */2 * * *` for every 2 hours).                                |
| `DECKMOVER_RUN_IMMEDIATELY`         | `false`               | If `true`, run once immediately, wait for key press, then exit. Perfect for testing.               |
| `DECKMOVER_LOG_LEVEL`               | `info`                | Log verbosity: `error`, `warn`, `info`, or `debug`. Use `debug` for troubleshooting rsync issues.  |
| `DECKMOVER_LOG`                     | `/logs/deckmover.log` | Log file path.                                                                                      |
| `PUID`                              | `99`                  | Owner UID (Unraid `nobody`).                                                                        |
| `PGID`                              | `100`                 | Group GID (Unraid `users`).                                                                         |
| `RSYNC_DRY_RUN`                     | `false`               | If `true`, add `--dry-run` to rsync and don't modify files. Perfect for testing plans.             |

---

## Scheduling

**Three scheduling modes (use only one):**

1. **Daily time (recommended):** Set `DECKMOVER_TIME=HH:MM` (e.g. `03:15`). The container logs "next run at..." and sleeps until that time daily.

2. **Cron expression:** Set `DECKMOVER_CRON` to a standard expression (e.g. `0 */2 * * *` for every 2 hours). BusyBox `crond` is used.

3. **Run immediately (testing):** Set `DECKMOVER_RUN_IMMEDIATELY=true`. Executes once, shows results in the console, waits for key press, then exits. Great for testing changes with `RSYNC_DRY_RUN=true`.

The container logs show:
* Startup summary with configuration
* Progress during execution (detail depends on `DECKMOVER_LOG_LEVEL`)
* Completion summary with file counts

---

## Permissions and ownership

We honor `PUID`/`PGID` for Unraid compatibility:

* Directories created with `install -d -m 0775 -o $PUID -g $PGID`
* `rsync` uses `--chown=$PUID:$PGID`
* Default matches Unraid standards: `nobody:users` → `99:100`

All files copied to cache will have correct Unraid ownership.

---

## Logging

* **Detailed logs:** Written to `/logs/deckmover.log` (rotated on each run, previous log saved with timestamp)
* **Container logs:** Show startup and completion summaries only (not duplicated in detail log)

**Log levels** (set via `DECKMOVER_LOG_LEVEL`):
* `error` - Only errors
* `warn` - Warnings and errors
* `info` (default) - High-level progress: start/end, summaries, file counts
* `debug` - Verbose details: every file, rsync commands, skip reasons, SQL queries

**Container log format:**
```
[deckmover] ===============================================
[deckmover] DeckMover run started: Sun Oct 13 03:15:00 UTC 2025
[deckmover] Log level: info
[deckmover] Detailed logs: /logs/deckmover.log
[deckmover] Dry run: 0 | Move warm: 1 | Move back: 0
[deckmover] Array: /mnt/user0 | Cache: /mnt/cache
[deckmover] ===============================================
...
[deckmover] Warm/copy phase complete: 15 copied
[deckmover] ===============================================
[deckmover] DeckMover run ended: Sun Oct 13 03:45:23 UTC 2025
[deckmover] ===============================================
```

---

## How On Deck selection works

The SQLite selector queries Plex's database (`com.plexapp.plugins.library.db`) to replicate Plex's "Continue Watching" algorithm:

1. **For each user account** in the Plex database:
   * Find episodes with `view_offset > 0` (in-progress episodes)
   * Find the **next unwatched episode** for each show currently being watched
   * Find movies with `view_offset > 0` (in-progress movies)

2. **Sorting:**
   * Items are sorted by most recent viewing activity
   * For shows, we use the **most recent viewing activity of the entire show** (not just that episode) to keep "currently watching" shows at the top

3. **Limits:**
   * Up to `DECKMOVER_ONDECK_COUNT` items per user (default: 10)
   * Hard cap of `DECKMOVER_MAX_ITEMS` total across all users (default: 100)

4. **Library filters:**
   * If `PLEX_LIBRARIES` is set (e.g. `Movies,TV Shows`), only those libraries are included
   * If blank, all Movie and TV libraries are included

5. **Path translation:**
   * Plex database stores paths like `/data/media/Movies/...`
   * We translate using `PLEX_PATH_MAP` to `/mnt/user/media/Movies/...`
   * We then check if the file exists on array (`/mnt/user0/...`) and copy to cache (`/mnt/cache/...`)

**Result:** The selector identifies exactly what Plex would show in each user's "Continue Watching" row, ensuring the most relevant content is warmed to cache.

---

## Build script

The included `build.sh` script builds a local image directly from source:

```bash
cd /path/to/deckmover
./build.sh
```

It tags as both `deckmover:<version>` and `deckmover:local`, using the git tag as the version. The CI/CD pipeline (`.github/workflows/docker-publish.yml`) publishes automatically to `ghcr.io/stormshaker/deckmover` on every push to `master` and on semver tags.

---

## Update workflow

**Via GHCR (recommended):**

1. Pull the latest image in Unraid GUI → Docker → check for updates
2. Restart the container

**From source:**

1. Edit scripts or selectors
2. `git add -A && git commit -m "description of changes"`
3. `./build.sh`
4. In Unraid GUI, update the **Repository** to `deckmover:local` and click **Apply**

---

## Troubleshooting

**"Plex database not found"**

* Ensure `/plexdb` mount is correct and points to your Plex appdata root (e.g. `/mnt/user/appdata/plex/`)
* The selector looks for `Library/Application Support/Plex Media Server/Plug-in Support/Databases/com.plexapp.plugins.library.db` under that mount
* Check: `docker exec <container_name> ls -la "/plexdb/Library/Application Support/Plex Media Server/Plug-in Support/Databases/"`

**"No items found"**

* Verify users have "Continue Watching" items in Plex UI
* Check `PLEX_LIBRARIES` filter — if set, only those libraries are included
* Verify `PLEX_PATH_MAP` is correct — paths must translate from Plex's paths to your Unraid paths
* Set `DECKMOVER_LOG_LEVEL=debug` to see SQL queries and results

**"Missing on array" for warmed files**

* This is expected behavior after successful warm with `DECKMOVER_WARM_MOVE=true`
* The file now exists only on cache (`/mnt/cache/...`), not array (`/mnt/user0/...`)
* User share (`/mnt/user/...`) will show the file from cache location

**Permissions show as root (orange in Unraid)**

* Ensure `PUID=99` and `PGID=100` are set in template
* For existing files with wrong ownership, run **Docker Safe New Perms** in Unraid

**Files copied multiple times / duplicates**

* Check that `DECKMOVER_WARM_MOVE=true` (default)
* This deletes array source after successful copy, preventing user share duplicates
* If `false`, file exists in both array and cache, appearing twice in user shares

**Testing changes without modifying files**

* Set `DECKMOVER_RUN_IMMEDIATELY=true` and `RSYNC_DRY_RUN=true`
* Container will run once, show what it would do, wait for key press, then exit
* No files are modified in dry-run mode

**Log file too large**

* Adjust `DECKMOVER_LOG_LEVEL` to `warn` or `error` for less verbosity
* Debug level logs every file and rsync command, which can be very verbose for large libraries

---

## Implementation notes

This project started as a learning exercise in Docker container development, initially built with AI assistance. The breakthrough during development was providing a copy of the Plex SQLite database directly to the AI, so it could query the schema and find the right tables and columns for the "Continue Watching" logic.

The codebase is intentionally small and readable so it can be maintained by hand.

---

## Credits

* **Inspiration:** [bexem/PlexCache](https://github.com/bexem/PlexCache) — original cache warming concept for Unraid
* **This implementation:** New codebase with direct SQLite access, multi-user support, and Unraid-native copy/move mechanics

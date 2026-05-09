# DeckMover — Agent Context

## What this is

DeckMover is a Docker container for **Unraid servers** that warms the Unraid cache drive with Plex "Continue Watching" items. It reads Plex's SQLite database directly (no Plex API/token needed), copies selected media from `/mnt/user0` (array) to `/mnt/cache`, and optionally moves fully-watched items back to the array.

Originally named **PlexCache** (inspired by [bexem/PlexCache](https://github.com/bexem/PlexCache)), renamed to DeckMover in May 2026. Any old `PLEXCACHE_` env var references are bugs — the correct prefix is `DECKMOVER_`.

## Repo / deployment

- **GitHub:** `https://github.com/stormshaker/deckmover`
- **Image:** `ghcr.io/stormshaker/deckmover:latest` (public)
- **CI/CD:** GitHub Actions (`.github/workflows/docker-publish.yml`) builds and pushes to GHCR on every push to `main` and on semver tags (`v*.*.*`)
- **Tags produced:** `latest` (main branch), `vX.Y.Z`, `X.Y`, `sha-<hash>`
- **Local builds:** `./build.sh` → tags `deckmover:<version>` and `deckmover:local`

## Architecture

```
entrypoint.sh         # Scheduling (daily time / cron / immediate) + run summaries
run_once.sh           # Core logic: space planning → rsync warm → optional move-back
selector_sqlite.py              # Queries Plex SQLite for On Deck / Continue Watching items
selector_watched_back_sqlite.py # Queries Plex SQLite for fully-watched items to move back
```

- Selectors live in `/opt/deckmover/` inside the container
- Scripts are in `/usr/local/bin/`
- Base image: `python:3.11-slim` (Debian-based, NOT Alpine — `groupadd`/`useradd` syntax, not `addgroup`/`adduser`)
- `busybox` is installed for `crond` in cron scheduling mode
- Always use `python3`, never `python`

## Key conventions

- **Env var prefix:** `DECKMOVER_` everywhere (selectors, shell scripts, Dockerfile)
- **Log prefix:** `[deckmover]` in container stdout
- **Run function:** `execute_deckmover_run` (defined at top level of `entrypoint.sh` so it's available in all three scheduling modes — daily, cron, and immediate)
- **Cron support file:** `/usr/local/bin/deckmover_functions.sh` (generated at runtime, only in cron mode)
- **Lock file:** `/tmp/deckmover_cron.lock`
- **Default log:** `/logs/deckmover.log` (rotated on each run, last 12 kept)

## Scheduling modes

Only one should be set:
1. `DECKMOVER_TIME=HH:MM` — daily at that time (default `03:15`)
2. `DECKMOVER_CRON=<expr>` — busybox crond expression
3. `DECKMOVER_RUN_IMMEDIATELY=true` — run once, wait for keypress, exit (for testing)

## Unraid specifics

- `PUID=99` / `PGID=100` = Unraid's `nobody:users` — all copied files must use this ownership
- `/mnt/user0` = array-only root (not the user share `/mnt/user`)
- `/mnt/cache` = cache drive root
- `/plexdb` = bind mount of Plex appdata (read-only); selector looks for the SQLite DB at `Library/Application Support/Plex Media Server/Plug-in Support/Databases/com.plexapp.plugins.library.db` under this mount
- Template file: `my-DeckMover.xml` — drop into `/boot/config/plugins/dockerMan/templates-user/` on Unraid

## Known limitations / gotchas

- Active playback session detection (`DECKMOVER_SKIP_IF_PLAYING_WARM`) is a no-op — Plex doesn't store active sessions in SQLite (they're in memory/redis). The guard exists as a future hook.
- The `plexapi` library is NOT installed. Any code referencing it will silently fail.
- `selector_watched_back_sqlite.py` applies the `DECKMOVER_MOVE_BACK_MIN_AGE_DAYS` filter in Python after the query (not in SQL), using `last_viewed_at` from a subquery added to the SELECT.

## CI/CD notes

- `latest` tag is only produced on pushes to `main` (uses `{{is_default_branch}}` in metadata-action). Re-running an old workflow job will NOT produce `latest` if the default branch was different when it originally ran — push a new commit instead.
- The `org.opencontainers.image.source` label in the Dockerfile links the GHCR package to the GitHub repo (required for the package to appear under the repo on GitHub).

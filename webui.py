#!/usr/bin/env python3
"""DeckMover WebUI — Flask-based admin interface (no auth, LAN use)."""

import json
import os
import re
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

from flask import Flask, Response, jsonify, render_template_string, request, send_file

# ── Config ────────────────────────────────────────────────────────────────────
LOG_PATH   = Path(os.environ.get('DECKMOVER_LOG', '/logs/deckmover.log'))
LOG_DIR    = LOG_PATH.parent
CONFIG_FILE = Path('/config/deckmover.env')
LOCK_FILE  = Path('/tmp/deckmover_cron.lock')
PORT       = int(os.environ.get('DECKMOVER_WEBUI_PORT', '7575'))

app = Flask(__name__)

# ── Settings schema ───────────────────────────────────────────────────────────
SETTINGS_GROUPS = [
    {
        'title': 'Scheduling',
        'note': 'Changes to scheduling settings require a container restart to take effect.',
        'settings': [
            {
                'key': 'DECKMOVER_TIME',
                'label': 'Daily Run Time',
                'default': '03:15',
                'type': 'text',
                'help': 'Time to run each day in HH:MM (24-hour). Used when DECKMOVER_CRON is not set.',
            },
            {
                'key': 'DECKMOVER_CRON',
                'label': 'Cron Expression',
                'default': '',
                'type': 'text',
                'help': 'Busybox cron expression (e.g. "0 3 * * *"). Overrides DECKMOVER_TIME when set.',
            },
            {
                'key': 'DECKMOVER_RUN_IMMEDIATELY',
                'label': 'Run Immediately on Start',
                'default': 'false',
                'type': 'bool',
                'help': 'Run once as soon as the container starts then stop. Useful for one-shot testing.',
            },
        ],
    },
    {
        'title': 'Plex Connection',
        'settings': [
            {
                'key': 'PLEX_LIBRARIES',
                'label': 'Plex Libraries',
                'default': 'Movies,TV Shows',
                'type': 'text',
                'help': 'Comma-separated names of Plex libraries to scan for Continue Watching items. Names must match exactly.',
            },
            {
                'key': 'PLEX_PATH_MAP',
                'label': 'Plex Container Path Mapping',
                'default': '/data=/mnt/user',
                'type': 'text',
                'placeholder': '/data=/mnt/user',
                'help': 'Only needed if Plex runs in a Docker container. Plex records paths as it sees them inside its own container (e.g. /data/Movies/...), but DeckMover needs to find those files on Unraid. Format: /container-path=/unraid-path. Most Unraid Plex installs use /data=/mnt/user. Leave blank if Plex runs natively.',
            },
            {
                'key': 'DECKMOVER_PLEXDB_PATH',
                'label': 'Plex DB Mount',
                'default': '/plexdb',
                'type': 'text',
                'help': 'Container path of the Plex appdata bind mount. The SQLite database must exist at Library/Application Support/Plex Media Server/Plug-in Support/Databases/ under this path.',
            },
        ],
    },
    {
        'title': 'On Deck Warming',
        'settings': [
            {
                'key': 'DECKMOVER_ONDECK',
                'label': 'Enable On Deck Warming',
                'default': 'true',
                'type': 'bool',
                'help': 'Warm Continue Watching / On Deck items from the array to cache. Disable to only use the move-back phase.',
            },
            {
                'key': 'DECKMOVER_ONDECK_COUNT',
                'label': 'On Deck Count',
                'default': '10',
                'type': 'number',
                'help': 'Number of Continue Watching items to warm per user per library. Higher values pre-warm more episodes/movies.',
            },
            {
                'key': 'DECKMOVER_MAX_ITEMS',
                'label': 'Max Items',
                'default': '100',
                'type': 'number',
                'help': 'Hard cap on total items processed per run across all users and libraries.',
            },
            {
                'key': 'DECKMOVER_WARM_MOVE',
                'label': 'Move After Warm (Delete Original)',
                'default': 'true',
                'type': 'bool',
                'help': 'Delete the array copy after it is verified on cache. Prevents duplicates appearing in user shares. Recommended.',
            },
            {
                'key': 'DECKMOVER_WARM_SIDECARS',
                'label': 'Copy Sidecar Files',
                'default': 'true',
                'type': 'bool',
                'help': 'Also copy subtitle (.srt, .ass, .sub) and metadata (.nfo, .jpg, .png) files alongside each video.',
            },
        ],
    },
    {
        'title': 'Space Management',
        'settings': [
            {
                'key': 'DECKMOVER_MIN_FREE_GB',
                'label': 'Minimum Free Space (GB)',
                'default': '20',
                'type': 'number',
                'help': 'DeckMover will not fill the cache beyond this floor. Files that would exceed it are skipped.',
            },
            {
                'key': 'DECKMOVER_RESERVE_GB',
                'label': 'Reserve Buffer (GB)',
                'default': '10',
                'type': 'number',
                'help': 'Additional headroom on top of Min Free Space. Total protected space = MIN_FREE_GB + RESERVE_GB.',
            },
            {
                'key': 'DECKMOVER_TRIM_PLAN',
                'label': 'Trim Plan to Fit',
                'default': 'true',
                'type': 'bool',
                'help': "Skip items that don't fit rather than aborting the whole run. When disabled, the run aborts entirely if the full plan exceeds available space.",
            },
        ],
    },
    {
        'title': 'Move Watched Back',
        'settings': [
            {
                'key': 'DECKMOVER_MOVE_WATCHED_BACK',
                'label': 'Enable Move-Back',
                'default': 'false',
                'type': 'bool',
                'help': 'Move fully-watched items from cache back to the array to reclaim cache space.',
            },
            {
                'key': 'DECKMOVER_MOVE_BACK_MIN_AGE_DAYS',
                'label': 'Min Age Before Move Back (days)',
                'default': '0',
                'type': 'number',
                'help': 'Only move items back if they were last watched at least this many days ago. Use 0 to move back as soon as the item is fully watched.',
            },
            {
                'key': 'DECKMOVER_MOVE_BACK_SIDECARS',
                'label': 'Move Back Sidecar Files',
                'default': 'true',
                'type': 'bool',
                'help': 'Also move subtitle and metadata files back to the array alongside the video.',
            },
        ],
    },
    {
        'title': 'Logging & Debug',
        'settings': [
            {
                'key': 'DECKMOVER_LOG_LEVEL',
                'label': 'Log Level',
                'default': 'info',
                'type': 'select',
                'options': ['error', 'warn', 'info', 'debug'],
                'help': 'Verbosity of the run log. Use "debug" to see per-file rsync commands; "info" is normal. Higher verbosity = larger log files.',
            },
            {
                'key': 'RSYNC_DRY_RUN',
                'label': 'Dry Run Mode',
                'default': 'false',
                'type': 'bool',
                'help': 'Simulate a run without copying or deleting any files. Useful to preview what would happen before enabling for real.',
            },
        ],
    },
]

ALL_KEYS = {s['key'] for g in SETTINGS_GROUPS for s in g['settings']}


# ── Config file helpers ───────────────────────────────────────────────────────

def read_config() -> dict:
    overrides: dict = {}
    if not CONFIG_FILE.exists():
        return overrides
    with open(CONFIG_FILE) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            if '=' in line:
                key, _, val = line.partition('=')
                val = val.strip('"').strip("'")
                overrides[key.strip()] = val
    return overrides


def write_config(data: dict) -> None:
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f'# DeckMover config — saved by WebUI on {datetime.now():%Y-%m-%d %H:%M:%S}\n',
        '# Values here override container environment variables for each run.\n',
        '# Scheduling settings (TIME, CRON, RUN_IMMEDIATELY) need a container restart.\n',
        '\n',
    ]
    for key, val in sorted(data.items()):
        lines.append(f'{key}="{val}"\n')
    with open(CONFIG_FILE, 'w') as f:
        f.writelines(lines)


# ── Status helpers ────────────────────────────────────────────────────────────

def get_status() -> dict:
    running = LOCK_FILE.exists()

    if os.environ.get('DECKMOVER_RUN_IMMEDIATELY', '').lower() == 'true':
        schedule_mode, schedule_value = 'immediate', ''
    elif os.environ.get('DECKMOVER_CRON', ''):
        schedule_mode = 'cron'
        schedule_value = os.environ['DECKMOVER_CRON']
    else:
        schedule_mode = 'daily'
        schedule_value = os.environ.get('DECKMOVER_TIME', '03:15')

    last_run = None
    if LOG_PATH.exists():
        try:
            content = LOG_PATH.read_text(errors='replace')
            starts = re.findall(r'\[deckmover\] DeckMover run (?:started|ended): (.+)', content)
            if starts:
                last_run = starts[-1].strip()
        except Exception:
            pass

    return {
        'running': running,
        'schedule_mode': schedule_mode,
        'schedule_value': schedule_value,
        'last_run': last_run,
        'log_path': str(LOG_PATH),
    }


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE)


@app.route('/icon.png')
def icon():
    return send_file('/opt/deckmover/icon.png', mimetype='image/png')


@app.route('/api/status')
def api_status():
    return jsonify(get_status())


@app.route('/api/settings', methods=['GET'])
def api_settings_get():
    config_overrides = read_config()
    values = {}
    for key in ALL_KEYS:
        if key in config_overrides:
            values[key] = config_overrides[key]
        else:
            values[key] = os.environ.get(key, '')
    return jsonify({
        'groups': SETTINGS_GROUPS,
        'values': values,
        'config_overrides': config_overrides,
    })


@app.route('/api/settings', methods=['POST'])
def api_settings_post():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({'error': 'No data'}), 400
    filtered = {k: str(v) for k, v in data.items() if k in ALL_KEYS}
    write_config(filtered)
    for key, val in filtered.items():
        os.environ[key] = val
    return jsonify({'status': 'saved'})


@app.route('/api/run', methods=['POST'])
def api_run():
    if LOCK_FILE.exists():
        return jsonify({'error': 'A run is already in progress'}), 409

    def _run():
        LOCK_FILE.touch()
        try:
            env = {**os.environ, **read_config()}
            log_target = env.get('DECKMOVER_LOG', str(LOG_PATH))
            with open(log_target, 'a') as lf:
                lf.write(f'\n[deckmover] DeckMover run started: {datetime.now()}\n')
                lf.flush()
                result = subprocess.run(
                    ['/usr/local/bin/run_once.sh'],
                    env=env, stdout=lf, stderr=lf,
                )
                lf.write(f'[deckmover] DeckMover run ended: {datetime.now()} (exit {result.returncode})\n')
        except Exception as exc:
            try:
                with open(str(LOG_PATH), 'a') as lf:
                    lf.write(f'[ERROR] WebUI-triggered run failed: {exc}\n')
            except Exception:
                pass
        finally:
            LOCK_FILE.unlink(missing_ok=True)

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({'status': 'started'})


@app.route('/api/logs')
def api_logs():
    n = min(int(request.args.get('lines', 300)), 2000)
    if not LOG_PATH.exists():
        return jsonify({'lines': [], 'path': str(LOG_PATH)})
    try:
        lines = LOG_PATH.read_text(errors='replace').splitlines()[-n:]
    except Exception as exc:
        return jsonify({'error': str(exc)}), 500
    return jsonify({'lines': lines, 'path': str(LOG_PATH)})


@app.route('/api/logs/stream')
def api_logs_stream():
    def generate():
        # Send last 80 lines as backfill
        try:
            if LOG_PATH.exists():
                for line in LOG_PATH.read_text(errors='replace').splitlines()[-80:]:
                    yield f'data: {json.dumps(line)}\n\n'
        except Exception:
            pass

        # Tail for new lines, handle rotation
        inode    = LOG_PATH.stat().st_ino if LOG_PATH.exists() else None
        position = LOG_PATH.stat().st_size if LOG_PATH.exists() else 0

        try:
            while True:
                time.sleep(0.5)
                if not LOG_PATH.exists():
                    inode, position = None, 0
                    continue
                cur_inode = LOG_PATH.stat().st_ino
                if cur_inode != inode:
                    inode, position = cur_inode, 0
                cur_size = LOG_PATH.stat().st_size
                if cur_size > position:
                    with open(LOG_PATH, errors='replace') as f:
                        f.seek(position)
                        chunk = f.read(cur_size - position)
                    position = cur_size
                    for line in chunk.splitlines():
                        if line:
                            yield f'data: {json.dumps(line)}\n\n'
        except GeneratorExit:
            pass
        except Exception as exc:
            yield f'data: {json.dumps(f"[stream error] {exc}")}\n\n'

    return Response(
        generate(),
        mimetype='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'},
    )


@app.route('/api/logs/files')
def api_logs_files():
    files = []
    if LOG_DIR.exists():
        for f in sorted(LOG_DIR.glob('deckmover*.log'), reverse=True):
            try:
                st = f.stat()
                files.append({
                    'name': f.name,
                    'size': st.st_size,
                    'modified': datetime.fromtimestamp(st.st_mtime).isoformat(),
                    'current': f.resolve() == LOG_PATH.resolve(),
                })
            except Exception:
                pass
    return jsonify({'files': files})


@app.route('/api/logs/file/<filename>')
def api_log_file(filename):
    safe = re.sub(r'[^a-zA-Z0-9._-]', '', filename)
    path = (LOG_DIR / safe).resolve()
    if not str(path).startswith(str(LOG_DIR.resolve())):
        return jsonify({'error': 'Access denied'}), 403
    if not path.exists() or not path.is_file():
        return jsonify({'error': 'Not found'}), 404
    try:
        lines = path.read_text(errors='replace').splitlines()
    except Exception as exc:
        return jsonify({'error': str(exc)}), 500
    return jsonify({'lines': lines, 'name': safe})


# ── HTML template ─────────────────────────────────────────────────────────────

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>DeckMover</title>
<link rel="icon" href="/icon.png">
<style>
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

:root {
  --bg:          #111113;
  --bg-card:     #1c1c1f;
  --bg-input:    #252528;
  --bg-hover:    #2a2a2e;
  --border:      #2e2e33;
  --accent:      #5b8dee;
  --accent-h:    #7aaaf2;
  --text:        #e2e2e6;
  --text-m:      #72727a;
  --text-l:      #b0b0ba;
  --success:     #3dd68c;
  --warning:     #f5c842;
  --danger:      #f2555a;
  --info:        #3e8ed0;
  --font: -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif;
  --mono: 'JetBrains Mono', 'Fira Code', ui-monospace, monospace;
  --r:  6px;
  --rl: 10px;
}

body { font-family: var(--font); background: var(--bg); color: var(--text); min-height: 100vh; font-size: 14px; line-height: 1.5; }

/* ── Layout ── */
header {
  display: flex; align-items: center; gap: 10px;
  padding: 13px 24px;
  background: var(--bg-card); border-bottom: 1px solid var(--border);
  position: sticky; top: 0; z-index: 200;
}
header h1 { font-size: 17px; font-weight: 700; letter-spacing: -.3px; }
.header-right { margin-left: auto; display: flex; align-items: center; gap: 14px; }

.status-pill {
  display: flex; align-items: center; gap: 7px;
  padding: 4px 10px; border-radius: 20px;
  background: var(--bg-input); border: 1px solid var(--border);
  font-size: 12px; color: var(--text-l);
}
.dot { width: 7px; height: 7px; border-radius: 50%; background: var(--text-m); flex-shrink: 0; }
.dot.running { background: var(--success); box-shadow: 0 0 0 3px rgba(61,214,140,.25); animation: blink 1.4s infinite; }
@keyframes blink { 0%,100%{opacity:1} 50%{opacity:.45} }

nav.tabs { display: flex; background: var(--bg-card); border-bottom: 1px solid var(--border); padding: 0 20px; }
.tab {
  display: flex; align-items: center; gap: 6px;
  padding: 11px 16px; cursor: pointer;
  font-size: 13px; font-weight: 500; color: var(--text-m);
  border: none; border-bottom: 2px solid transparent;
  background: none; transition: color .15s, border-color .15s;
}
.tab:hover { color: var(--text); }
.tab.active { color: var(--accent); border-bottom-color: var(--accent); }

main { padding: 24px; max-width: 1060px; }
.tab-pane { display: none; }
.tab-pane.active { display: block; }

/* ── Cards & stats ── */
.card { background: var(--bg-card); border: 1px solid var(--border); border-radius: var(--rl); padding: 20px; margin-bottom: 18px; }
.card-title { font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: .7px; color: var(--text-m); margin-bottom: 16px; }

.stat-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(190px, 1fr)); gap: 14px; margin-bottom: 18px; }
.stat { background: var(--bg-card); border: 1px solid var(--border); border-radius: var(--rl); padding: 16px 18px; }
.stat-lbl { font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: .5px; color: var(--text-m); margin-bottom: 6px; }
.stat-val { font-size: 18px; font-weight: 700; }
.stat-val.sm { font-size: 13px; font-weight: 500; }

/* ── Buttons ── */
.btn {
  display: inline-flex; align-items: center; gap: 6px;
  padding: 7px 15px; border-radius: var(--r);
  font-size: 13px; font-weight: 600; cursor: pointer;
  border: 1px solid transparent; transition: all .14s;
  white-space: nowrap; text-decoration: none;
}
.btn:disabled { opacity: .4; cursor: not-allowed; }
.btn-primary { background: var(--accent); color: #fff; border-color: var(--accent); }
.btn-primary:hover:not(:disabled) { background: var(--accent-h); border-color: var(--accent-h); }
.btn-success { background: var(--success); color: #000; border-color: var(--success); }
.btn-success:hover:not(:disabled) { opacity: .85; }
.btn-ghost { background: transparent; color: var(--text-l); border-color: var(--border); }
.btn-ghost:hover:not(:disabled) { background: var(--bg-hover); color: var(--text); }
.row { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }

/* ── Logs ── */
.log-bar { display: flex; align-items: center; gap: 10px; margin-bottom: 12px; flex-wrap: wrap; }
.log-bar select {
  background: var(--bg-input); border: 1px solid var(--border); color: var(--text);
  border-radius: var(--r); padding: 6px 10px; font-size: 13px; cursor: pointer;
}
.log-box {
  background: #0b0b0d; border: 1px solid var(--border); border-radius: var(--r);
  height: 560px; overflow-y: auto; padding: 12px 14px;
  font-family: var(--mono); font-size: 12.5px; line-height: 1.65;
}
.ll { white-space: pre-wrap; word-break: break-all; }
.ll.E { color: #f2555a; }
.ll.W { color: #f5c842; }
.ll.D { color: #444; }
.ll:hover { background: rgba(255,255,255,.025); }
.log-status { margin-left: auto; font-size: 12px; color: var(--text-m); }
.live-dot { display: inline-block; width: 6px; height: 6px; border-radius: 50%; background: var(--success); margin-right: 4px; animation: blink 1.4s infinite; }

/* ── Settings ── */
.sg { background: var(--bg-card); border: 1px solid var(--border); border-radius: var(--rl); margin-bottom: 22px; overflow: hidden; }
.sg-head { padding: 13px 20px; border-bottom: 1px solid var(--border); }
.sg-title { font-size: 14px; font-weight: 700; }
.sg-note { font-size: 12px; color: var(--warning); margin-top: 2px; }
.sr {
  display: grid; grid-template-columns: 270px 1fr; gap: 18px; align-items: start;
  padding: 15px 20px; border-bottom: 1px solid var(--border); transition: background .12s;
}
.sr:last-child { border-bottom: none; }
.sr:hover { background: rgba(255,255,255,.018); }
.s-label { font-size: 14px; font-weight: 500; margin-bottom: 3px; }
.s-key { font-family: var(--mono); font-size: 11px; color: var(--text-m); background: var(--bg-input); padding: 1px 5px; border-radius: 3px; display: inline-block; margin-bottom: 5px; }
.s-help { font-size: 12px; color: var(--text-m); line-height: 1.45; }
.s-inp { display: flex; flex-direction: column; gap: 5px; }
.src { font-size: 11px; font-weight: 600; }
.src.cfg { color: var(--accent); }
.src.env { color: var(--text-m); }

input[type=text], input[type=number], select.ss {
  background: var(--bg-input); border: 1px solid var(--border); color: var(--text);
  border-radius: var(--r); padding: 7px 11px; font-size: 13px; font-family: var(--font); width: 100%;
}
input[type=text]:focus, input[type=number]:focus, select.ss:focus { outline: none; border-color: var(--accent); }
input.changed, select.changed { border-color: var(--warning); }

.tog { position: relative; width: 40px; height: 22px; flex-shrink: 0; }
.tog input { opacity: 0; width: 0; height: 0; }
.ts {
  position: absolute; inset: 0;
  background: var(--bg-input); border: 1px solid var(--border); border-radius: 22px; cursor: pointer;
  transition: all .18s;
}
.ts::before {
  content: ''; position: absolute;
  width: 14px; height: 14px; left: 3px; top: 3px;
  background: var(--text-m); border-radius: 50%; transition: all .18s;
}
.tog input:checked + .ts { background: var(--accent); border-color: var(--accent); }
.tog input:checked + .ts::before { transform: translateX(18px); background: #fff; }
.tog.changed .ts { border-color: var(--warning); }
.tog-row { display: flex; align-items: center; gap: 9px; }
.tog-label { font-size: 13px; color: var(--text-l); }

/* ── Save bar ── */
.save-bar {
  position: sticky; bottom: 0;
  background: var(--bg-card); border-top: 1px solid var(--border);
  padding: 13px 24px; margin: 0 -24px -24px;
  display: flex; align-items: center; gap: 12px; z-index: 100;
}
.unsaved { font-size: 13px; color: var(--warning); display: none; }
.unsaved.on { display: block; }

/* ── Toast ── */
.toast {
  position: fixed; bottom: 22px; right: 22px;
  padding: 10px 18px; border-radius: var(--r);
  font-size: 13px; font-weight: 500; z-index: 999;
  animation: fadein .25s ease; max-width: 340px;
}
.toast.success { background: var(--success); color: #000; }
.toast.error   { background: var(--danger);  color: #fff; }
.toast.info    { background: var(--info);    color: #fff; }
@keyframes fadein { from{transform:translateY(10px);opacity:0} to{transform:none;opacity:1} }

/* ── Badge ── */
.badge { display:inline-flex; align-items:center; padding:2px 9px; border-radius:20px; font-size:12px; font-weight:600; }
.b-ok  { background:rgba(61,214,140,.12); color:var(--success); }
.b-dim { background:var(--bg-input); color:var(--text-m); }

@media(max-width:680px){
  .sr { grid-template-columns: 1fr; }
  main { padding: 14px; }
}
</style>
</head>
<body>

<header>
  <img src="/icon.png" width="28" height="28" style="border-radius:6px;flex-shrink:0" alt="DeckMover">
  <h1>DeckMover</h1>
  <div class="header-right">
    <div class="status-pill">
      <span class="dot" id="hDot"></span>
      <span id="hTxt">Loading…</span>
    </div>
  </div>
</header>

<nav class="tabs">
  <button class="tab active" id="t-dashboard" onclick="showTab('dashboard')">
    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/><rect x="3" y="14" width="7" height="7"/><rect x="14" y="14" width="7" height="7"/></svg>
    Dashboard
  </button>
  <button class="tab" id="t-logs" onclick="showTab('logs')">
    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/></svg>
    Logs
  </button>
  <button class="tab" id="t-settings" onclick="showTab('settings')">
    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>
    Settings
  </button>
</nav>

<main>
  <!-- Dashboard -->
  <div class="tab-pane active" id="p-dashboard">
    <div class="stat-grid">
      <div class="stat">
        <div class="stat-lbl">Status</div>
        <div class="stat-val" id="dStatus">—</div>
      </div>
      <div class="stat">
        <div class="stat-lbl">Schedule</div>
        <div class="stat-val sm" id="dSchedule">—</div>
      </div>
      <div class="stat">
        <div class="stat-lbl">Last Activity</div>
        <div class="stat-val sm" id="dLast">—</div>
      </div>
    </div>
    <div class="card">
      <div class="card-title">Actions</div>
      <div class="row">
        <button class="btn btn-primary" id="btnRun" onclick="runNow()">
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polygon points="5 3 19 12 5 21 5 3"/></svg>
          Run Now
        </button>
        <span id="runMsg" style="font-size:13px;color:var(--text-m)"></span>
      </div>
    </div>
  </div>

  <!-- Logs -->
  <div class="tab-pane" id="p-logs">
    <div class="log-bar">
      <select id="logPicker" onchange="pickLog(this.value)">
        <option value="">Current log (live)</option>
      </select>
      <button class="btn btn-ghost" onclick="toggleScroll()">
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="7 13 12 18 17 13"/><polyline points="7 6 12 11 17 6"/></svg>
        <span id="scrollLbl">Auto-scroll: On</span>
      </button>
      <button class="btn btn-ghost" onclick="clearLogs()">Clear view</button>
      <span class="log-status" id="logSt"></span>
    </div>
    <div class="log-box" id="logBox"></div>
  </div>

  <!-- Settings -->
  <div class="tab-pane" id="p-settings">
    <div id="settingsBody">Loading…</div>
    <div class="save-bar">
      <span class="unsaved" id="unsaved">Unsaved changes</span>
      <div style="margin-left:auto;display:flex;gap:8px">
        <button class="btn btn-ghost" onclick="discardSettings()">Discard</button>
        <button class="btn btn-success" onclick="saveSettings()">
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/><polyline points="17 21 17 13 7 13 7 21"/><polyline points="7 3 7 8 15 8"/></svg>
          Save Settings
        </button>
      </div>
    </div>
  </div>
</main>

<script>
// ── State ──────────────────────────────────────────────────────────────────────
let autoScroll   = true;
let evtSrc       = null;
let schema       = null;   // groups array from API
let sValues      = {};     // effective values (env/config)
let cfgOverrides = {};     // what's in the config file
let pending      = {};     // unsaved edits

// ── Tabs ───────────────────────────────────────────────────────────────────────
function showTab(name) {
  ['dashboard','logs','settings'].forEach(n => {
    document.getElementById('p-'+n).classList.toggle('active', n===name);
    document.getElementById('t-'+n).classList.toggle('active', n===name);
  });
  if (name==='logs')     initStream();
  if (name==='settings') initSettings();
}

// ── Status ─────────────────────────────────────────────────────────────────────
async function pollStatus() {
  try {
    const d = await fetch('/api/status').then(r=>r.json());
    const running = d.running;
    document.getElementById('hDot').className = 'dot'+(running?' running':'');
    document.getElementById('hTxt').textContent = running ? 'Running' : 'Idle';
    document.getElementById('dStatus').innerHTML = running
      ? '<span class="badge b-ok">Running</span>'
      : '<span class="badge b-dim">Idle</span>';
    let sched = d.schedule_mode;
    if (d.schedule_value) sched += ' · ' + d.schedule_value;
    document.getElementById('dSchedule').textContent = sched || '—';
    document.getElementById('dLast').textContent = d.last_run || 'No runs yet';
    document.getElementById('btnRun').disabled = running;
  } catch {}
}
pollStatus();
setInterval(pollStatus, 5000);

// ── Run Now ────────────────────────────────────────────────────────────────────
async function runNow() {
  const btn = document.getElementById('btnRun');
  const msg = document.getElementById('runMsg');
  btn.disabled = true;
  msg.textContent = 'Starting…';
  try {
    const d = await fetch('/api/run',{method:'POST'}).then(r=>r.json());
    if (d.error) { toast(d.error,'error'); msg.textContent=d.error; btn.disabled=false; }
    else { toast('Run started','success'); msg.textContent='Running — watch Logs for progress'; }
  } catch { toast('Failed to start run','error'); btn.disabled=false; msg.textContent=''; }
}

// ── Logs ───────────────────────────────────────────────────────────────────────
function addLine(text) {
  const box = document.getElementById('logBox');
  const d = document.createElement('div');
  d.className = 'll';
  if (text.includes('[ERROR]')) d.classList.add('E');
  else if (text.includes('[WARN]'))  d.classList.add('W');
  else if (text.includes('[DEBUG]')) d.classList.add('D');
  d.textContent = text;
  box.appendChild(d);
  if (autoScroll) box.scrollTop = box.scrollHeight;
}
function clearLogs() { document.getElementById('logBox').innerHTML=''; }
function toggleScroll() {
  autoScroll=!autoScroll;
  document.getElementById('scrollLbl').textContent='Auto-scroll: '+(autoScroll?'On':'Off');
}

function initStream() {
  if (evtSrc) return;
  const st = document.getElementById('logSt');
  st.innerHTML = 'Connecting…';
  evtSrc = new EventSource('/api/logs/stream');
  evtSrc.onopen = () => { st.innerHTML = '<span class="live-dot"></span>Live'; };
  evtSrc.onmessage = e => addLine(JSON.parse(e.data));
  evtSrc.onerror = () => {
    st.textContent = 'Reconnecting…';
    evtSrc.close(); evtSrc = null;
    setTimeout(()=>{ if(document.getElementById('p-logs').classList.contains('active')) initStream(); }, 3000);
  };
  loadLogFiles();
}

async function loadLogFiles() {
  const d = await fetch('/api/logs/files').then(r=>r.json());
  const sel = document.getElementById('logPicker');
  sel.innerHTML = '<option value="">Current log (live)</option>';
  for (const f of d.files) {
    if (!f.current) {
      const o = document.createElement('option');
      o.value = f.name;
      o.textContent = f.name + ' (' + fmtBytes(f.size) + ')';
      sel.appendChild(o);
    }
  }
}

async function pickLog(name) {
  if (!name) { clearLogs(); if(!evtSrc) initStream(); return; }
  if (evtSrc) { evtSrc.close(); evtSrc=null; }
  clearLogs();
  document.getElementById('logSt').textContent = 'Loading…';
  const d = await fetch('/api/logs/file/'+encodeURIComponent(name)).then(r=>r.json());
  document.getElementById('logSt').textContent = d.name || '';
  for (const l of d.lines||[]) addLine(l);
}

function fmtBytes(b) {
  if (b<1024) return b+' B';
  if (b<1048576) return (b/1024).toFixed(1)+' KB';
  return (b/1048576).toFixed(1)+' MB';
}

// ── Settings ───────────────────────────────────────────────────────────────────
async function initSettings() {
  if (schema) return;
  document.getElementById('settingsBody').textContent = 'Loading…';
  const d = await fetch('/api/settings').then(r=>r.json());
  schema = d.groups;
  sValues = d.values;
  cfgOverrides = d.config_overrides;
  pending = {};
  renderSettings();
}

function renderSettings() {
  let html = '';
  for (const g of schema) {
    html += `<div class="sg">
      <div class="sg-head">
        <div class="sg-title">${esc(g.title)}</div>
        ${g.note ? `<div class="sg-note">&#9888; ${esc(g.note)}</div>` : ''}
      </div>`;
    for (const s of g.settings) {
      const val = pending[s.key] !== undefined ? pending[s.key] : (sValues[s.key] ?? s.default);
      html += `<div class="sr">
        <div>
          <div class="s-label">${esc(s.label)}</div>
          <code class="s-key">${esc(s.key)}</code>
          <div class="s-help">${esc(s.help)}</div>
        </div>
        <div class="s-inp">
          ${renderInput(s, val)}
        </div>
      </div>`;
    }
    html += '</div>';
  }
  document.getElementById('settingsBody').innerHTML = html;
}

function renderInput(s, val) {
  const ch = pending[s.key] !== undefined ? ' changed' : '';
  const k  = esc(s.key);
  if (s.type === 'bool') {
    const on = truthy(val);
    const tch = pending[s.key] !== undefined ? ' changed' : '';
    return `<div class="tog-row">
      <label class="tog${tch}">
        <input type="checkbox" ${on?'checked':''} onchange="onChange('${k}',this.checked?'true':'false')">
        <span class="ts"></span>
      </label>
      <span class="tog-label" id="bl_${k}">${on?'Enabled':'Disabled'}</span>
    </div>`;
  }
  if (s.type === 'select') {
    const opts = (s.options||[]).map(o=>`<option ${o===val?'selected':''} value="${esc(o)}">${esc(o)}</option>`).join('');
    return `<select class="ss${ch}" onchange="onChange('${k}',this.value)">${opts}</select>`;
  }
  if (s.type === 'number') {
    return `<input type="number" class="${ch}" value="${esc(String(val))}" oninput="onChange('${k}',this.value)">`;
  }
  const ph = s.placeholder ? ` placeholder="${esc(s.placeholder)}"` : '';
  return `<input type="text" class="${ch}" value="${esc(String(val))}"${ph} oninput="onChange('${k}',this.value)">`;
}

function onChange(key, value) {
  pending[key] = value;
  document.getElementById('unsaved').classList.add('on');
  const lbl = document.getElementById('bl_'+key);
  if (lbl) lbl.textContent = truthy(value) ? 'Enabled' : 'Disabled';
}

function truthy(v) { return ['1','true','yes','on','TRUE','YES','ON'].includes(String(v)); }

function discardSettings() {
  schema = null; pending = {};
  document.getElementById('unsaved').classList.remove('on');
  initSettings();
}

async function saveSettings() {
  if (!Object.keys(pending).length) { toast('No changes to save','info'); return; }
  const toSave = { ...sValues, ...pending };
  try {
    const d = await fetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(toSave)}).then(r=>r.json());
    if (d.error) { toast('Save failed: '+d.error,'error'); return; }
    toast('Settings saved','success');
    sValues = toSave;
    cfgOverrides = { ...cfgOverrides, ...pending };
    pending = {};
    schema = null;
    document.getElementById('unsaved').classList.remove('on');
    initSettings();
  } catch { toast('Save failed','error'); }
}

// ── Utilities ──────────────────────────────────────────────────────────────────
function esc(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}
function toast(msg, type='info') {
  const el = document.createElement('div');
  el.className = 'toast '+type;
  el.textContent = msg;
  document.body.appendChild(el);
  setTimeout(()=>el.remove(), 3800);
}
</script>
</body>
</html>"""

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=PORT, threaded=True)

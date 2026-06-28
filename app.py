import io
import os
import re
import zipfile
import json
import logging
import random
import time
import threading
from datetime import datetime
from PIL import Image, ImageOps
import numpy as np
from flask import Flask, request, jsonify, render_template_string, send_from_directory, redirect, url_for, session, Response
from zeroconf import IPVersion, Zeroconf, ServiceInfo
import socket
import sqlite3

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'picframes_secret_session_key_12345')
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', 'admin')
SERVER_VERSION = "0.7.0"

# ---------------------------------------------------------------------------
# Authentication Gate
# ---------------------------------------------------------------------------

LOGIN_HTML = """<!DOCTYPE html>
<html>
<head>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>PicTracks Controller Login</title>
  <style>
    @import url('https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;600;700&display=swap');
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: 'Outfit', sans-serif;
      background: radial-gradient(circle at center, hsl(220, 30%, 12%), hsl(220, 35%, 6%));
      color: hsl(220, 20%, 94%);
      min-height: 100vh;
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 24px;
    }
    .card {
      background: rgba(30, 41, 59, 0.75);
      backdrop-filter: blur(20px);
      border: 1px solid rgba(255, 255, 255, 0.09);
      padding: 40px;
      border-radius: 22px;
      width: 100%;
      max-width: 400px;
      box-shadow: 0 24px 48px rgba(0,0,0,0.55);
    }
    h2 {
      font-weight: 700;
      font-size: 2rem;
      margin-bottom: 8px;
      background: linear-gradient(135deg, hsl(190, 100%, 55%), hsl(260, 90%, 65%));
      -webkit-background-clip: text;
      -webkit-text-fill-color: transparent;
      text-align: center;
    }
    .sub {
      text-align: center;
      font-size: 0.9rem;
      color: hsl(220, 15%, 58%);
      margin-bottom: 24px;
    }
    .error {
      color: hsl(0, 85%, 65%);
      background: rgba(239, 68, 68, 0.12);
      border: 1px solid rgba(239, 68, 68, 0.2);
      padding: 12px;
      border-radius: 10px;
      margin-bottom: 20px;
      font-size: 0.85rem;
      text-align: center;
    }
    label {
      display: block;
      font-size: 0.85rem;
      color: hsl(220, 12%, 68%);
      margin-bottom: 6px;
      font-weight: 500;
    }
    .input-group {
      margin-bottom: 20px;
    }
    input[type=password] {
      width: 100%;
      padding: 12px 14px;
      background: rgba(15, 23, 42, 0.6);
      border: 1px solid rgba(255, 255, 255, 0.11);
      border-radius: 10px;
      color: white;
      font-size: 0.95rem;
      transition: all 0.2s;
    }
    input[type=password]:focus {
      outline: none;
      border-color: hsl(190, 100%, 55%);
      box-shadow: 0 0 0 3px rgba(56, 189, 248, 0.14);
    }
    input[type=submit] {
      width: 100%;
      padding: 14px;
      border: none;
      border-radius: 10px;
      background: linear-gradient(135deg, hsl(190, 100%, 45%), hsl(260, 90%, 55%));
      color: white;
      font-size: 1rem;
      font-weight: 600;
      cursor: pointer;
      transition: all 0.2s;
      box-shadow: 0 4px 12px rgba(56, 189, 248, 0.2);
      margin-top: 8px;
    }
    input[type=submit]:hover {
      transform: translateY(-1px);
      box-shadow: 0 6px 18px rgba(56, 189, 248, 0.3);
    }
  </style>
</head>
<body>
  <div class="card">
    <h2>PicFrames</h2>
    <div class="sub">Sign in to manage your frames</div>
    {% if error %}
      <div class="error">{{ error }}</div>
    {% endif %}
    <form method="POST">
      <div class="input-group">
        <label>Admin Password</label>
        <input type="password" name="password" required placeholder="Enter password" autofocus>
      </div>
      <input type="submit" value="Sign In">
    </form>
  </div>
</body>
</html>"""

@app.before_request
def auth_gate():
    path = request.path
    if path.startswith('/api/') or path == '/device_orientation' or path == '/login' or path.startswith('/static/'):
        return None
    if not session.get('authenticated'):
        return redirect(url_for('login'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if session.get('authenticated'):
        return redirect(url_for('index'))
    error = None
    if request.method == 'POST':
        pwd = request.form.get('password')
        if pwd == ADMIN_PASSWORD:
            session['authenticated'] = True
            session.permanent = True
            return redirect(url_for('index'))
        else:
            error = "Invalid Password"
    return render_template_string(LOGIN_HTML, error=error)

@app.route('/logout')
def logout():
    session.pop('authenticated', None)
    return redirect(url_for('login'))

SHARE_DIR     = os.environ.get('SHARE_DIR', '/share')
CONFIG_DIR    = os.environ.get('CONFIG_DIR', '/config')
ORIGINALS_DIR = os.path.join(SHARE_DIR, 'originals')
IMAGES_DIR    = os.path.join(SHARE_DIR, 'images')

for d in (SHARE_DIR, ORIGINALS_DIR, IMAGES_DIR, CONFIG_DIR):
    os.makedirs(d, exist_ok=True)

_github_latest_release_cache = {
    'tag': None,
    'assets': {}, 
    'last_updated': 0
}

def _update_github_release_cache():
    import requests as py_requests
    now = time.time()
    if now - _github_latest_release_cache['last_updated'] < 60:
        return
        
    repo = "finn-e/picframe-waveshare-ESP32-S3-PhotoPainter"
    url = f"https://api.github.com/repos/{repo}/releases/latest"
    try:
        headers = {"Accept": "application/vnd.github+json"}
        token = os.environ.get("GITHUB_TOKEN")
        if token:
            headers["Authorization"] = f"token {token}"
            
        r = py_requests.get(url, headers=headers, timeout=5)
        if r.status_code == 200:
            data = r.json()
            tag = data.get("tag_name", "").strip().lstrip('v')
            assets = {}
            for asset in data.get("assets", []):
                name = asset.get("name", "")
                if name.endswith(".zip"):
                    hw_profile = name[:-4]
                    assets[hw_profile] = asset.get("browser_download_url")
                    if '-' in hw_profile:
                        base_profile = hw_profile.split('-', 1)[0]
                        assets[base_profile] = asset.get("browser_download_url")
            
            _github_latest_release_cache['tag'] = tag
            _github_latest_release_cache['assets'] = assets
            _github_latest_release_cache['last_updated'] = now
            logger.info(f"GitHub Releases Cache updated: latest tag is {tag}")
    except Exception as e:
        logger.error(f"Failed to query GitHub Releases API: {e}")

def get_latest_firmware_version():
    try:
        _update_github_release_cache()
        if _github_latest_release_cache['tag']:
            return _github_latest_release_cache['tag']
    except Exception:
        pass

    for path in [
        os.path.join(os.path.dirname(os.path.abspath(__file__)), '../picframe-devices/ESP32-S3-PhotoPainter/main.py'),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), 'firmware/main.py')
    ]:
        if os.path.exists(path):
            try:
                with open(path, 'r') as f:
                    content = f.read()
                m = re.search(r'FILE VERSION:\s*([0-9\.]+)', content)
                if m:
                    return m.group(1).strip()
            except Exception:
                pass
    return '2.0.0'

PALETTE = np.array([
    [0,   0,   0  ],
    [255, 255, 255],
    [0,   255, 0  ],
    [0,   0,   255],
    [255, 0,   0  ],
    [255, 255, 0  ],
], dtype=np.float32)

ALLOWED_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.webp', '.bmp'}
LANDSCAPE_SUFFIX   = '_l.bmp'
PORTRAIT_SUFFIX    = '_p.bmp'

def orient_suffix(orientation):
    return LANDSCAPE_SUFFIX if orientation == 'landscape' else PORTRAIT_SUFFIX

_state_lock = threading.Lock()

def start_mdns_broadcast(port=8000):
    try:
        zeroconf = Zeroconf(ip_version=IPVersion.V4Only)
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(('10.255.255.255', 1))
            local_ip = s.getsockname()[0]
        except Exception:
            local_ip = '127.0.0.1'
        finally:
            s.close()

        info = ServiceInfo(
            "_picframes._tcp.local.",
            "PicFrames Server._picframes._tcp.local.",
            addresses=[socket.inet_aton(local_ip)],
            port=port,
            properties={"path": "/"},
        )
        zeroconf.register_service(info)
        logger.info(f"Broadcasting mDNS service at {local_ip}:{port}")
        return zeroconf
    except Exception as e:
        logger.error(f"Failed to start mDNS broadcast: {e}")
        return None

DB_PATH = os.path.join(CONFIG_DIR, 'picframes.db')

legacy_db_path = os.path.join(SHARE_DIR, 'picframes.db')
if not os.path.exists(DB_PATH) and os.path.exists(legacy_db_path):
    try:
        import shutil
        shutil.copy2(legacy_db_path, DB_PATH)
        logger.info(f"Copied legacy database from {legacy_db_path} to {DB_PATH}")
    except Exception as e:
        logger.error(f"Failed to copy legacy database from NFS: {e}")

def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=10.0)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS global_settings (
        key TEXT PRIMARY KEY,
        value TEXT
    )
    """)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS devices (
        mac TEXT PRIMARY KEY,
        name TEXT,
        orientation TEXT,
        debug INTEGER,
        mode TEXT,
        shuffle INTEGER DEFAULT 0,
        flip_l INTEGER DEFAULT 0,
        flip_p INTEGER DEFAULT 0,
        images_json TEXT
    )
    """)
    for col in [('shuffle', 'INTEGER DEFAULT 0'), ('flip_l', 'INTEGER DEFAULT 0'), ('flip_p', 'INTEGER DEFAULT 0')]:
        try:
            cursor.execute(f"ALTER TABLE devices ADD COLUMN {col[0]} {col[1]}")
        except sqlite3.OperationalError:
            pass
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS crops (
        base TEXT PRIMARY KEY,
        l REAL,
        p REAL
    )
    """)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS enabled (
        base TEXT PRIMARY KEY,
        l INTEGER,
        p INTEGER
    )
    """)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS image_order (
        base TEXT PRIMARY KEY,
        sort_order INTEGER
    )
    """)
    try:
        cursor.execute("ALTER TABLE enabled ADD COLUMN title INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    try:
        cursor.execute("ALTER TABLE enabled ADD COLUMN caption_mode TEXT DEFAULT 'none'")
    except sqlite3.OperationalError:
        pass
    try:
        cursor.execute("ALTER TABLE enabled ADD COLUMN description TEXT DEFAULT ''")
    except sqlite3.OperationalError:
        pass
    conn.commit()

    cursor.execute("SELECT COUNT(*) FROM global_settings")
    if cursor.fetchone()[0] == 0:
        logger.info("Migrating legacy JSON config/state files to SQLite...")
        cfg_path = os.path.join(SHARE_DIR, 'config.json')
        if os.path.exists(cfg_path):
            try:
                with open(cfg_path) as f:
                    cfg = json.load(f)
                cursor.execute("INSERT OR REPLACE INTO global_settings (key, value) VALUES (?, ?)", ('timer', str(cfg.get('timer', 900))))
                cursor.execute("INSERT OR REPLACE INTO global_settings (key, value) VALUES (?, ?)", ('wake_timeout', str(cfg.get('wake_timeout', 45))))
                cursor.execute("INSERT OR REPLACE INTO global_settings (key, value) VALUES (?, ?)", ('shuffle', '1' if cfg.get('shuffle', False) else '0'))
                cursor.execute("INSERT OR REPLACE INTO global_settings (key, value) VALUES (?, ?)", ('sync_images', '1' if cfg.get('sync_images', False) else '0'))
                for dev in cfg.get('devices', []):
                    cursor.execute("""
                    INSERT OR REPLACE INTO devices (mac, name, orientation, debug, mode, images_json)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """, (
                        dev.get('mac', '').lower(),
                        dev.get('name', ''),
                        dev.get('orientation', 'landscape'),
                        1 if dev.get('debug', False) else 0,
                        dev.get('mode', 'group'),
                        json.dumps(dev.get('images', []))
                    ))
            except Exception as e:
                logger.error(f"Migration of config.json failed: {e}")

        crops_path = os.path.join(SHARE_DIR, 'image_crops.json')
        if os.path.exists(crops_path):
            try:
                with open(crops_path) as f:
                    crops = json.load(f)
                for base, val in crops.items():
                    cursor.execute("INSERT OR REPLACE INTO crops (base, l, p) VALUES (?, ?, ?)", (base, val.get('l', 0.5), val.get('p', 0.5)))
            except Exception as e:
                logger.error(f"Migration of image_crops.json failed: {e}")

        enabled_path = os.path.join(SHARE_DIR, 'image_enabled.json')
        if os.path.exists(enabled_path):
            try:
                with open(enabled_path) as f:
                    enabled = json.load(f)
                for base, val in enabled.items():
                    l_val = val if isinstance(val, bool) else val.get('l', True)
                    p_val = val if isinstance(val, bool) else val.get('p', True)
                    cursor.execute("INSERT OR REPLACE INTO enabled (base, l, p) VALUES (?, ?, ?)", (base, 1 if l_val else 0, 1 if p_val else 0))
            except Exception as e:
                logger.error(f"Migration of image_enabled.json failed: {e}")

        order_path = os.path.join(SHARE_DIR, 'image_order.json')
        if os.path.exists(order_path):
            try:
                with open(order_path) as f:
                    order = json.load(f)
                for idx, base in enumerate(order):
                    cursor.execute("INSERT OR REPLACE INTO image_order (base, sort_order) VALUES (?, ?)", (base, idx))
                cursor.execute("INSERT OR REPLACE INTO global_settings (key, value) VALUES ('image_order_initialized', '1')")
            except Exception as e:
                logger.error(f"Migration of image_order.json failed: {e}")

        state_path = os.path.join(SHARE_DIR, 'server_state.json')
        if os.path.exists(state_path):
            try:
                with open(state_path) as f:
                    state = json.load(f)
                for k, v in state.items():
                    val_str = json.dumps(v) if isinstance(v, (dict, list)) else str(v)
                    cursor.execute("INSERT OR REPLACE INTO global_settings (key, value) VALUES (?, ?)", (k, val_str))
            except Exception as e:
                logger.error(f"Migration of server_state.json failed: {e}")

        conn.commit()
    conn.close()

init_db()

def load_config():
    defaults = {
        "timer": 900, "wake_timeout": 45,
        "devices": [], "shuffle": False, "sync_images": False,
    }
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT key, value FROM global_settings WHERE key IN ('timer', 'wake_timeout', 'shuffle', 'sync_images')")
        for row in cursor.fetchall():
            key, val = row['key'], row['value']
            if key == 'timer': defaults['timer'] = int(val)
            elif key == 'wake_timeout': defaults['wake_timeout'] = int(val)
            elif key == 'shuffle': defaults['shuffle'] = (val == '1')
            elif key == 'sync_images': defaults['sync_images'] = (val == '1')
        
        cursor.execute("SELECT mac, name, orientation, debug, mode, shuffle, flip_l, flip_p, images_json FROM devices")
        devices = []
        for row in cursor.fetchall():
            devices.append({
                "mac": row['mac'].lower(),
                "name": row['name'],
                "orientation": row['orientation'],
                "debug": bool(row['debug']),
                "mode": row['mode'],
                "shuffle": bool(row['shuffle']),
                "flip_l": bool(row['flip_l']),
                "flip_p": bool(row['flip_p']),
                "images": json.loads(row['images_json'] or '[]')
            })
        defaults['devices'] = devices
        conn.close()
    except Exception as e:
        logger.error(f"SQL load_config failed: {e}")
    return defaults

def save_config(cfg):
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("INSERT OR REPLACE INTO global_settings (key, value) VALUES (?, ?)", ('timer', str(cfg.get('timer', 900))))
        cursor.execute("INSERT OR REPLACE INTO global_settings (key, value) VALUES (?, ?)", ('wake_timeout', str(cfg.get('wake_timeout', 45))))
        cursor.execute("INSERT OR REPLACE INTO global_settings (key, value) VALUES (?, ?)", ('shuffle', '1' if cfg.get('shuffle', False) else '0'))
        cursor.execute("INSERT OR REPLACE INTO global_settings (key, value) VALUES (?, ?)", ('sync_images', '1' if cfg.get('sync_images', False) else '0'))
        
        cursor.execute("DELETE FROM devices")
        for dev in cfg.get('devices', []):
            cursor.execute("""
            INSERT OR REPLACE INTO devices (mac, name, orientation, debug, mode, shuffle, flip_l, flip_p, images_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                dev.get('mac', '').lower(),
                dev.get('name', ''),
                dev.get('orientation', 'landscape'),
                1 if dev.get('debug', False) else 0,
                dev.get('mode', 'group'),
                1 if dev.get('shuffle', False) else 0,
                1 if dev.get('flip_l', False) else 0,
                1 if dev.get('flip_p', False) else 0,
                json.dumps(dev.get('images', []))
            ))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        logger.error(f"SQL save_config failed: {e}")
        return False

def load_crops():
    crops = {}
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT base, l, p FROM crops")
        for row in cursor.fetchall():
            crops[row['base']] = {"l": row['l'], "p": row['p']}
        conn.close()
    except Exception as e:
        logger.error(f"SQL load_crops failed: {e}")
    return crops

def save_crops(crops):
    try:
        conn = get_db()
        cursor = conn.cursor()
        for base, val in crops.items():
            cursor.execute("INSERT OR REPLACE INTO crops (base, l, p) VALUES (?, ?, ?)", (base, val.get('l', 0.5), val.get('p', 0.5)))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"SQL save_crops failed: {e}")

PHASE_GATHERING = 'GATHERING'
PHASE_READY     = 'READY'
PHASE_CHANGE    = 'CHANGE'

def load_state():
    defaults = {
        "current_index": 0, "last_sync_ts": 0, "last_change_ts": 0,
        "phase": PHASE_GATHERING,
        "phase_checkins":  {},
        "phase_ready_ack": {},
        "phase_change_ack": {},
        "round_assignments": {},
        "last_seen": {},
        "device_ips": {},
        "device_images": {},
        "redownload": {},
        "queued_image": None,
    }
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT key, value FROM global_settings")
        for row in cursor.fetchall():
            key, val = row['key'], row['value']
            if key in ('current_index', 'last_sync_ts', 'last_change_ts'):
                defaults[key] = int(val)
            elif key == 'phase':
                defaults[key] = val
            elif key in ('phase_checkins', 'phase_ready_ack', 'phase_change_ack', 'round_assignments', 'last_seen', 'device_ips', 'device_images', 'redownload', 'queued_image'):
                try: defaults[key] = json.loads(val)
                except Exception: pass
        conn.close()
    except Exception as e:
        logger.error(f"SQL load_state failed: {e}")
    return defaults

def save_state(state):
    try:
        conn = get_db()
        cursor = conn.cursor()
        for k, v in state.items():
            val_str = json.dumps(v) if isinstance(v, (dict, list)) else str(v)
            cursor.execute("INSERT OR REPLACE INTO global_settings (key, value) VALUES (?, ?)", (k, val_str))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"SQL save_state failed: {e}")

def trigger_redownload(mac=None):
    state = load_state()
    state.setdefault('redownload', {})
    if mac:
        state['redownload'][mac.lower()] = True
    else:
        cfg = load_config()
        for dev in cfg.get('devices', []):
            if dev.get('mac'):
                state['redownload'][dev['mac'].lower()] = True
    save_state(state)

def load_image_order():
    order = []
    initialized = False
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT base FROM image_order ORDER BY sort_order ASC")
        rows = cursor.fetchall()
        order = [row['base'] for row in rows]
        
        cursor.execute("SELECT value FROM global_settings WHERE key = 'image_order_initialized'")
        row = cursor.fetchone()
        if row and row['value'] == '1': initialized = True
            
        if not order and not initialized:
            order = sorted([
                os.path.splitext(f)[0] for f in os.listdir(ORIGINALS_DIR)
                if os.path.splitext(f)[1].lower() in ALLOWED_EXTENSIONS
            ])
            cursor.execute("DELETE FROM image_order")
            for idx, base in enumerate(order):
                cursor.execute("INSERT INTO image_order (base, sort_order) VALUES (?, ?)", (base, idx))
            cursor.execute("INSERT OR REPLACE INTO global_settings (key, value) VALUES ('image_order_initialized', '1')")
            conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"SQL load_image_order failed: {e}")
    return order

def save_image_order(order):
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM image_order")
        for idx, base in enumerate(order):
            cursor.execute("INSERT INTO image_order (base, sort_order) VALUES (?, ?)", (base, idx))
        cursor.execute("INSERT OR REPLACE INTO global_settings (key, value) VALUES ('image_order_initialized', '1')")
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"SQL save_image_order failed: {e}")

def load_enabled():
    enabled = {}
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM enabled")
        for row in cursor.fetchall():
            cols = row.keys()
            enabled[row['base']] = {
                "l": bool(row['l']),
                "p": bool(row['p']),
                "title": bool(row['title']) if 'title' in cols else False,
                "caption_mode": row['caption_mode'] if 'caption_mode' in cols else 'none',
                "description": row['description'] if 'description' in cols else ''
            }
        conn.close()
    except Exception as e:
        logger.error(f"SQL load_enabled failed: {e}")
    return enabled

def save_enabled(enabled):
    try:
        conn = get_db()
        cursor = conn.cursor()
        for base, val in enabled.items():
            l_val = val.get('l', True) if isinstance(val, dict) else val
            p_val = val.get('p', True) if isinstance(val, dict) else val
            title_val = val.get('title', False) if isinstance(val, dict) else False
            caption_mode = val.get('caption_mode', 'none') if isinstance(val, dict) else 'none'
            desc_val = val.get('description', '') if isinstance(val, dict) else ''
            try:
                cursor.execute("INSERT OR REPLACE INTO enabled (base, l, p, title, caption_mode, description) VALUES (?, ?, ?, ?, ?, ?)",
                               (base, 1 if l_val else 0, 1 if p_val else 0, 1 if title_val else 0, caption_mode, desc_val))
            except sqlite3.OperationalError:
                try:
                    cursor.execute("INSERT OR REPLACE INTO enabled (base, l, p, title) VALUES (?, ?, ?, ?)",
                                   (base, 1 if l_val else 0, 1 if p_val else 0, 1 if title_val else 0))
                except sqlite3.OperationalError:
                    cursor.execute("INSERT OR REPLACE INTO enabled (base, l, p) VALUES (?, ?, ?)",
                                   (base, 1 if l_val else 0, 1 if p_val else 0))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"SQL save_enabled failed: {e}")

def _flags(enabled, base):
    v = enabled.get(base, {"l": True, "p": True, "title": False, "caption_mode": 'none', "description": ''})
    if isinstance(v, bool): return {"l": v, "p": v, "title": False, "caption_mode": 'none', "description": ''}
    return {
        "l": v.get("l", True), "p": v.get("p", True), "title": v.get("title", False),
        "caption_mode": v.get("caption_mode", 'none'), "description": v.get("description", '')
    }

def landscape_file(base): return base + LANDSCAPE_SUFFIX
def portrait_file(base):  return base + PORTRAIT_SUFFIX

def get_active_bases(orientation=None):
    order   = load_image_order()
    enabled = load_enabled()
    result  = []
    for base in order:
        flags = _flags(enabled, base)
        has_l = os.path.exists(os.path.join(IMAGES_DIR, landscape_file(base)))
        has_p = os.path.exists(os.path.join(IMAGES_DIR, portrait_file(base)))
        if orientation == 'landscape' and has_l and flags["l"]: result.append(base)
        elif orientation == 'portrait' and has_p and flags["p"]: result.append(base)
        elif orientation is None and ((has_l and flags["l"]) or (has_p and flags["p"])): result.append(base)
    return result

def get_unified_index():
    order   = load_image_order()
    enabled = load_enabled()
    result  = []
    for base in order:
        flags = _flags(enabled, base)
        if flags["l"] and os.path.exists(os.path.join(IMAGES_DIR, landscape_file(base))): result.append(landscape_file(base))
        if flags["p"] and os.path.exists(os.path.join(IMAGES_DIR, portrait_file(base))): result.append(portrait_file(base))
    return result

def dither_floyd_steinberg(img_array, palette):
    h, w, _ = img_array.shape
    padded = np.pad(img_array, ((0, 1), (1, 1), (0, 0)), mode='edge').astype(np.float32)
    for y in range(h):
        for x in range(1, w + 1):
            old_val = padded[y, x].copy()
            l1_dist_black = np.sum(np.abs(old_val - [0, 0, 0]))
            l1_dist_white = np.sum(np.abs(old_val - [255, 255, 255]))
            
            if l1_dist_black < 15:
                padded[y, x] = [0, 0, 0]; continue
            elif l1_dist_white < 15:
                padded[y, x] = [255, 255, 255]; continue
                
            diff = palette - old_val; dist = np.sum(diff ** 2, axis=1); idx = np.argmin(dist)
            new_val = palette[idx]; padded[y, x] = new_val; err = old_val - new_val
            padded[y,     x + 1] += err * (7.0 / 16.0)
            padded[y + 1, x - 1] += err * (3.0 / 16.0)
            padded[y + 1, x    ] += err * (5.0 / 16.0)
            padded[y + 1, x + 1] += err * (1.0 / 16.0)
    return padded[0:h, 1:w+1].astype(np.uint8)

def ensure_dithered_original(base):
    dith_name = base + '_dithered.png'
    dith_path = os.path.join(IMAGES_DIR, dith_name)
    if os.path.exists(dith_path): return dith_path
    original_name = None
    for f in os.listdir(ORIGINALS_DIR):
        if os.path.splitext(f)[0] == base: original_name = f; break
    if not original_name: return None
    try:
        src_path = os.path.join(ORIGINALS_DIR, original_name)
        img = Image.open(src_path)
        img = ImageOps.exif_transpose(img).convert('RGB')
        img.thumbnail((800, 800), Image.Resampling.LANCZOS)
        img_dith = Image.fromarray(dither_floyd_steinberg(np.array(img, dtype=np.float32), PALETTE))
        img_dith.save(dith_path, format='PNG')
        return dith_path
    except Exception as e:
        logger.error(f"Error producing dither overlay: {e}")
        return None

def rgb_array_to_spectra6_bitstream_fast(img_array):
    h, w, _ = img_array.shape
    hardware_map = np.array([0, 1, 6, 5, 3, 2], dtype=np.uint8)
    pixels = img_array.reshape(-1, 3)
    dists = np.sum((pixels[:, None, :] - PALETTE[None, :, :])**2, axis=2)
    palette_indices = np.argmin(dists, axis=1)
    hw_indices = hardware_map[palette_indices].reshape(h, w)
    packed = (hw_indices[:, 0::2] << 4) | hw_indices[:, 1::2]
    return packed.tobytes()

def ensure_bin_file(base, orientation):
    bin_name = base + ('_l.bin' if orientation == 'landscape' else '_p.bin')
    bin_path = os.path.join(IMAGES_DIR, bin_name)
    if os.path.exists(bin_path): return bin_path
    bmp_path = os.path.join(IMAGES_DIR, base + ('_l.bmp' if orientation == 'landscape' else '_p.bmp'))
    if not os.path.exists(bmp_path): return None
    try:
        img = Image.open(bmp_path).convert('RGB')
        bitstream = rgb_array_to_spectra6_bitstream_fast(np.array(img, dtype=np.uint8))
        with open(bin_path, 'wb') as f: f.write(bitstream)
        return bin_path
    except Exception as e:
        logger.error(f"Error generating bin map {bin_name}: {e}")
        return None

def convert_image(src_path, base):
    try:
        img = Image.open(src_path)
        img = ImageOps.exif_transpose(img).convert('RGB')
        w, h = img.size
        crops = load_crops(); offsets = crops.get(base, {"l": 0.5, "p": 0.5})
        offset_l, offset_p = offsets.get("l", 0.5), offsets.get("p", 0.5)

        target_ratio_l = 5.0/3.0
        if w / h <= target_ratio_l:
            w_crop, h_crop = w, int(w / target_ratio_l)
            x_crop, y_crop = 0, int(offset_l * (h - h_crop))
        else:
            h_crop, w_crop = h, int(h * target_ratio_l)
            y_crop, x_crop = 0, int(0.5 * (w - w_crop))
        land = img.crop((x_crop, y_crop, x_crop + w_crop, y_crop + h_crop)).resize((800, 480), Image.Resampling.LANCZOS)
        Image.fromarray(dither_floyd_steinberg(np.array(land, dtype=np.float32), PALETTE)).save(os.path.join(IMAGES_DIR, base + LANDSCAPE_SUFFIX), format='BMP')

        target_ratio_p = 3.0/5.0
        if w / h >= target_ratio_p:
            h_crop, w_crop = h, int(h * target_ratio_p)
            y_crop, x_crop = 0, int(offset_p * (w - w_crop))
        else:
            w_crop, h_crop = w, int(w / target_ratio_p)
            x_crop, y_crop = 0, int(0.5 * (h - h_crop))
        port = img.crop((x_crop, y_crop, x_crop + w_crop, y_crop + h_crop)).resize((480, 800), Image.Resampling.LANCZOS)
        port_img = Image.fromarray(dither_floyd_steinberg(np.array(port, dtype=np.float32), PALETTE))
        port_img.rotate(270, expand=True).save(os.path.join(IMAGES_DIR, base + PORTRAIT_SUFFIX), format='BMP')

        order = load_image_order()
        if base not in order: order.append(base); save_image_order(order)
        
        for suffix in ('_l.bin', '_p.bin'):
            p = os.path.join(IMAGES_DIR, base + suffix)
            if os.path.exists(p): os.remove(p)
        ensure_bin_file(base, 'landscape')
        ensure_bin_file(base, 'portrait')
        return True
    except Exception as e:
        logger.error(f"Conversion error: {e}"); return False

def _build_shuffle_assignments(cfg, state):
    devices = cfg.get('devices', []); sync_images = cfg.get('sync_images', False)
    assignments = {}
    if sync_images:
        all_bases = get_active_bases(None)
        if all_bases: assignments['__sync__'] = random.choice(all_bases)
    else:
        used = set()
        for dev in devices:
            mac = dev['mac'].lower(); orientation = dev.get('orientation', 'landscape')
            if dev.get('mode', 'group') == 'individual':
                dev_imgs = dev.get('images', [])
                pool = [item['base'] for item in dev_imgs if item.get(orientation[0], True) and os.path.exists(os.path.join(IMAGES_DIR, item['base'] + orient_suffix(orientation)))]
            else:
                pool = [b for b in get_active_bases(orientation) if b not in used]
                if not pool: pool = get_active_bases(orientation)
            if pool:
                chosen = random.choice(pool); used.add(chosen); assignments[mac] = chosen
    state['round_assignments'] = assignments

def _target_for_device(cfg, state, device_mac, device_idx, num_devices):
    devices = cfg.get('devices', []); sync_images = cfg.get('sync_images', False)
    dev_cfg = next((d for d in devices if d['mac'].lower() == device_mac.lower()), {})
    orientation = dev_cfg.get('orientation', 'landscape')
    suffix = orient_suffix(orientation)
    
    queued = state.get('queued_image')
    if queued:
        q_base = queued.get('base'); q_source = queued.get('source', '')
        if (q_source == 'general' and dev_cfg.get('mode', 'group') == 'group') or \
           (q_source.lower() == device_mac.lower() and dev_cfg.get('mode', 'group') == 'individual'):
            return q_base + suffix
    
    shuffle = dev_cfg.get('shuffle', False) if dev_cfg.get('mode', 'group') == 'individual' else cfg.get('shuffle', False)
    if shuffle:
        base = state.get('round_assignments', {}).get('__sync__' if sync_images else device_mac.lower())
        return (base + suffix) if base else None

    active = dev_cfg.get('images', []) if dev_cfg.get('mode', 'group') == 'individual' else get_active_bases(orientation)
    if dev_cfg.get('mode', 'group') == 'individual':
        orient_char = orientation[0]
        active = [item['base'] for item in active if item.get(orient_char, True) and os.path.exists(os.path.join(IMAGES_DIR, item['base'] + suffix))]
    else:
        active = get_active_bases(orientation)
        
    if not active: return None
    n = len(active); idx = state.get('current_index', 0)
    return active[idx % n if sync_images else (idx + device_idx) % n] + suffix

HTML_TEMPLATE = r"""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <script>
        (function() {
            const savedTheme = localStorage.getItem('picframes-theme') || 'default';
            document.documentElement.setAttribute('data-theme', savedTheme);
        })();
    </script>
    <title>PicFrames Dashboard</title>
    <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;500;600;700&display=swap" rel="stylesheet">

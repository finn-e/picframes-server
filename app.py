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
from flask import Flask, request, jsonify, render_template_string, redirect, url_for, session, Response, send_from_directory
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
  <title>PicFrames Controller Login</title>
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

HTML_TEMPLATE = r"""<!DOCTYPE html>
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
    <style>
        :root {
            --bg: #080d1a; --bg-gradient: radial-gradient(ellipse at 20% 0%, #1a2545 0%, var(--bg) 60%);
            --card: rgba(16,24,48,0.65); --border: rgba(255,255,255,0.07);
            --text: #f1f3f9; --muted: #8b95ae; --accent: #4f8ef7; --accent-h: #3371e0;
            --danger: #ef4444; --danger-h: #dc2626; --success: #22c55e; --warning: #f59e0b;
            --input-bg: rgba(0,0,0,0.4);
        }
        [data-theme="light"] {
            --bg: #f4f6fa; --bg-gradient: linear-gradient(135deg, #eef2f7 0%, #f4f6fa 100%);
            --card: #ffffff; --border: rgba(0, 0, 0, 0.08); --text: #1e293b; --muted: #64748b;
            --accent: #4f46e5; --accent-h: #4338ca; --input-bg: #ffffff;
        }
        [data-theme="really-dark"] {
            --bg: #000000; --bg-gradient: #000000; --card: #0d0d0d; --border: #262626;
            --text: #ffffff; --muted: #a3a3a3; --accent: #a855f7; --accent-h: #9333ea; --input-bg: #000000;
        }
        *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
        body { font-family: 'Outfit', sans-serif; background: var(--bg-gradient); color: var(--text); min-height: 100vh; padding: 2rem 1.5rem; }
        .container { width: 100%; max-width: 1400px; margin: 0 auto; }
        header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 2rem; padding-bottom: 1.5rem; border-bottom: 1px solid var(--border); }
        h1 { font-size: 2rem; font-weight: 700; background: linear-gradient(135deg, #60a5fa, #a78bfa); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }
        .version-tag { font-size: 0.85rem; font-weight: 600; background: rgba(255, 255, 255, 0.08); border: 1px solid var(--border); color: var(--muted); padding: 0.15rem 0.5rem; border-radius: 6px; display: inline-block; margin-left: 0.6rem; }
        .subtitle { color: var(--muted); font-size: 0.9rem; margin-top: 0.2rem; }
        .grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 1.5rem; margin-bottom: 1.5rem; }
        @media (max-width: 900px) { .grid-2 { grid-template-columns: 1fr; } }
        .card { background: var(--card); border: 1px solid var(--border); border-radius: 16px; padding: 1.5rem; backdrop-filter: blur(14px); }
        .card-title { font-size: 0.85rem; font-weight: 600; color: var(--muted); text-transform: uppercase; letter-spacing: 0.06em; margin-bottom: 1.2rem; display: flex; align-items: center; gap: 0.5rem; }
        label { font-weight: 500; color: var(--muted); font-size: 0.9rem; }
        input[type="number"], input[type="text"] { background: rgba(0,0,0,0.35); border: 1px solid var(--border); border-radius: 8px; padding: 0.5rem 0.75rem; color: var(--text); font-size: 0.95rem; outline: none; font-family: inherit; }
        input[type="number"] { width: 100px; }
        .form-row { display: flex; align-items: center; gap: 0.75rem; flex-wrap: wrap; margin-bottom: 0.75rem; }
        .toggle-wrap { display: flex; align-items: center; gap: 0.5rem; }
        .toggle { appearance: none; width: 38px; height: 22px; background: rgba(255,255,255,0.12); border-radius: 11px; position: relative; cursor: pointer; border: 1px solid var(--border); flex-shrink: 0; }
        .toggle::after { content: ''; position: absolute; width: 16px; height: 16px; border-radius: 50%; background: white; top: 2px; left: 2px; transition: transform 0.25s; }
        .toggle:checked { background: var(--accent); border-color: var(--accent); }
        .toggle:checked::after { transform: translateX(16px); }
        .btn { background: var(--accent); color: white; border: none; border-radius: 8px; padding: 0.55rem 1.1rem; font-weight: 600; cursor: pointer; display: inline-flex; align-items: center; gap: 0.4rem; font-size: 0.9rem; font-family: inherit; white-space: nowrap; }
        .btn:hover { background: var(--accent-h); transform: translateY(-1px); }
        .btn-danger { background: var(--danger); }
        .btn-ghost { background: rgba(255,255,255,0.07); color: var(--text); border: 1px solid var(--border); }
        .btn-ghost:hover { background: rgba(255,255,255,0.13); }
        .btn-sm { padding: 0.35rem 0.7rem; font-size: 0.82rem; }
        .btn-warning { background: rgba(245,158,11,0.2); color: var(--warning); border: 1px solid rgba(245,158,11,0.3); }
        .orient-pill { display: inline-flex; border-radius: 8px; overflow: hidden; border: 1px solid var(--border); flex-shrink: 0; }
        .orient-pill button { background: rgba(0,0,0,0.25); border: none; cursor: pointer; padding: 0.3rem 0.6rem; color: var(--muted); font-size: 0.8rem; font-weight: 600; font-family: inherit; }
        .orient-pill button.active-l { background: rgba(79,142,247,0.3); color: var(--accent); }
        .orient-pill button.active-p { background: rgba(167,139,250,0.3); color: #a78bfa; }
        .mode-pill { display: inline-flex; border-radius: 8px; overflow: hidden; border: 1px solid var(--border); flex-shrink: 0; }
        .mode-pill button { background: rgba(0,0,0,0.25); border: none; cursor: pointer; padding: 0.3rem 0.6rem; color: var(--muted); font-size: 0.8rem; font-weight: 600; font-family: inherit; }
        .mode-pill button.active-g { background: rgba(34,197,94,0.3); color: var(--success); }
        .mode-pill button.active-i { background: rgba(79,142,247,0.3); color: var(--accent); }
        .device-row { display: flex; align-items: center; gap: 0.75rem; padding: 0.65rem 0; border-bottom: 1px solid var(--border); flex-wrap: wrap; }
        .device-row:last-child { border-bottom: none; }
        .device-mac { font-family: monospace; font-size: 0.85rem; color: var(--muted); }
        .device-idx { color: var(--muted); font-size: 0.8rem; white-space: nowrap; }
        .theme-selector-wrap { display: flex; align-items: center; background: rgba(255,255,255,0.03); border: 1px solid var(--border); border-radius: 12px; padding: 0.4rem 0.8rem; }
        .theme-selector-wrap select { background: transparent; border: none; color: var(--text); font-weight: 600; cursor: pointer; font-family: inherit; outline: none; }
        #drop-zone { border: 2px dashed rgba(255,255,255,0.13); border-radius: 16px; padding: 2.5rem 2rem; text-align: center; cursor: pointer; background: var(--card); margin-bottom: 2rem; }
        #drop-zone:hover { border-color: var(--accent); background: rgba(79, 142, 247, 0.05); }
        #upload-progress { display: none; background: var(--card); border: 1px solid var(--border); border-radius: 12px; padding: 1rem 1.5rem; margin-bottom: 1.5rem; }
        .progress-bar-wrap { background: rgba(255,255,255,0.08); border-radius: 4px; height: 6px; margin-top: 0.5rem; overflow: hidden; }
        .progress-bar { height: 100%; background: var(--accent); width: 0%; transition: width 0.2s; }
        .section-header { margin: 2rem 0 1rem; font-size: 1.3rem; font-weight: 600; color: var(--muted); display: flex; align-items: center; gap: 0.75rem; }
        .badge { padding: 0.25rem 0.65rem; border-radius: 20px; font-size: 0.78rem; font-weight: 600; }
        .badge-blue { background: rgba(79,142,247,0.15); color: var(--accent); border: 1px solid rgba(79,142,247,0.25); }
        .tabs-bar { display: flex; gap: 0.5rem; border-bottom: 2px solid var(--border); margin: 2rem 0 1.5rem; overflow-x: auto; }
        .tab-btn { padding: 0.75rem 1.25rem; border-radius: 12px 12px 0 0; background: rgba(255,255,255,0.03); border: 1px solid var(--border); border-bottom: none; color: var(--muted); font-weight: 600; cursor: pointer; position: relative; top: 2px; }
        .tab-btn.active { background: var(--card); border-bottom: 2px solid var(--accent); color: var(--text); }
        .image-list-container { margin-bottom: 3rem; }
        .image-card { background: var(--card); border: 1px solid var(--border); border-radius: 16px; overflow: hidden; margin-bottom: 1.25rem; }
        .card-header { padding: 0.9rem 1.4rem; border-bottom: 1px solid var(--border); display: flex; align-items: center; gap: 0.75rem; }
        .btn-queue { background: rgba(255, 255, 255, 0.05); border: 1px solid var(--border); color: var(--muted); border-radius: 8px; padding: 0.35rem 0.75rem; font-size: 0.8rem; font-weight: 600; cursor: pointer; margin-left: auto; }
        .btn-queue.active { background: var(--accent); color: #fff; }
        .img-name { font-weight: 600; font-size: 1rem; }
        .triple-preview { display: grid; grid-template-columns: 1fr 1fr 1fr; border-bottom: 1px solid var(--border); }
        @media (max-width: 768px) { .triple-preview { grid-template-columns: 1fr; } }
        .preview-cell { padding: 1.2rem; display: flex; flex-direction: column; align-items: center; background: rgba(0,0,0,0.18); border-right: 1px solid var(--border); }
        .preview-cell:last-child { border-right: none; }
        .preview-label { font-size: 0.75rem; text-transform: uppercase; color: var(--muted); margin-bottom: 0.5rem; font-weight: 600; width: 100%; display: flex; justify-content: space-between; align-items: center; }
        .preview-img { max-width: 100%; max-height: 200px; border-radius: 6px; object-fit: contain; box-shadow: 0 4px 12px rgba(0,0,0,0.4); }
        .crop-container { position: relative; width: 100%; max-height: 200px; display: flex; align-items: center; justify-content: center; overflow: hidden; border-radius: 8px; background: #0b0f19; border: 1px solid var(--border); }
        .crop-bg-img { display: block; max-width: 100%; max-height: 200px; object-fit: contain; pointer-events: none; }
        .crop-overlay-box { position: absolute; border: 2px dashed var(--accent); background: rgba(79, 142, 247, 0.22); cursor: grab; }
        .status-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 0.75rem; margin-bottom: 1.5rem; }
        .status-node { background: rgba(0,0,0,0.25); border: 1px solid var(--border); border-radius: 12px; padding: 1rem; transition: transform 0.2s; }
        .status-node:hover { transform: translateY(-2px); background: rgba(0,0,0,0.35); }
        .status-node.online { border-color: rgba(34,197,94,0.4); }
        .node-mac { font-family: monospace; font-size: 0.8rem; color: var(--muted); }
        .node-status { font-size: 0.8rem; color: var(--muted); margin-top: 0.25rem; }
        .status-node.online .node-status { color: var(--success); }
        .status-node-img-wrap { margin-top: 0.5rem; border-radius: 6px; overflow: hidden; height: 110px; border: 1px solid var(--border); display: flex; align-items: center; justify-content: center; background: #111; }
        .status-node-img { width: 150px; height: 90px; object-fit: cover; }
        .status-node-img.portrait-rot { width: 90px; height: 54px; transform: rotate(-90deg); }
        .card-actions { padding: 0.8rem 1.4rem; display: flex; justify-content: flex-end; gap: 0.75rem; background: rgba(0,0,0,0.08); }
        #toast { position: fixed; bottom: 2rem; right: 2rem; background: var(--card); border: 1px solid var(--border); border-radius: 12px; padding: 0.75rem 1.25rem; opacity: 0; transition: opacity 0.3s; z-index: 999; pointer-events: none; backdrop-filter: blur(10px); }
        #toast.show { opacity: 1; }
        
        .tabs-bar { display: flex; gap: 0.5rem; border-bottom: 2px solid var(--border); margin: 2rem 0 1.5rem; overflow-x: auto; }
        .tab-btn { padding: 0.75rem 1.25rem; border-radius: 12px 12px 0 0; background: rgba(255,255,255,0.03); border: 1px solid var(--border); border-bottom: none; color: var(--muted); font-weight: 600; cursor: pointer; position: relative; top: 2px; }
        .tab-btn.active { background: var(--card); border-bottom: 2px solid var(--accent); color: var(--text); }
        .image-card { background: var(--card); border: 1px solid var(--border); border-radius: 16px; overflow: hidden; margin-bottom: 1.25rem; }
        .card-header { padding: 0.9rem 1.4rem; border-bottom: 1px solid var(--border); display: flex; align-items: center; gap: 0.75rem; }
        .img-name-wrap { flex: 1; display: flex; align-items: center; gap: 0.5rem; min-width: 0; }
        .img-name { font-weight: 600; font-size: 1rem; cursor: pointer; border-bottom: 1px dashed transparent; }
        .rename-hint { font-size: 0.75rem; color: var(--muted); }
        .rename-input { font-weight: 600; font-size: 1rem; width: 100%; max-width: 300px; background: var(--input-bg); border: 1px solid var(--accent); border-radius: 6px; padding: 0.2rem 0.5rem; color: var(--text); }
    </style>
</head>
<body>
<div class="container">
    <header>
        <div>
            <h1>PicFrames Controller <span class="version-tag">v{{ version }}</span></h1>
            <div class="subtitle">E-Paper Frame Fleet Manager &nbsp;·&nbsp; {{ images|length }} image(s)</div>
        </div>
        <div class="theme-selector-wrap">
            <select id="theme-select" onchange="changeTheme(this.value)">
                <option value="default">Default Dark</option>
                <option value="light">Light</option>
                <option value="really-dark">Really Dark</option>
            </select>
        </div>
    </header>

    <div style="margin-bottom: 1.5rem;">
        <div class="card-title" style="font-size: 0.9rem; color: var(--muted); margin-bottom: 0.75rem;">📶 Node Status (Phase: <strong>{{ phase }}</strong>)</div>
        <div class="status-grid">
        {% for dev in config.devices %}
            {% set ts = node_status.get(dev.mac.lower(), 0) %}
            {% set active_img = device_images.get(dev.mac.lower()) %}
            {% set ip_addr = device_ips.get(dev.mac.lower(), "DHCP") %}
            <div class="status-node {% if ts > now_ts - 120 %}online{% endif %}" style="cursor: pointer;" onclick="switchToDeviceTab('{{ dev.mac }}')">
                <div style="font-weight: 600;">{{ dev.name }}</div>
                <div class="node-mac">{{ dev.mac }}</div>
                <div style="font-size: 0.75rem; color: var(--muted);">{{ ip_addr }}</div>
                <div class="node-status">
                    {% if ts > 0 %}Active {{ ((now_ts - ts)|int) }}s ago{% else %}Offline{% endif %}
                </div>
                {% if active_img %}
                <div class="status-node-img-wrap">
                    <img class="status-node-img {% if active_img.endswith('_p.bin') %}portrait-rot{% endif %}" src="{{ url_for('serve_image', filename=active_img[:-4] + '.bmp') }}?t={{ now_ts }}">
                </div>
                {% endif %}
            </div>
        {% endfor %}
        </div>
    </div>

    <div class="grid-2">
        <div class="card">
            <div class="card-title">⚙️ Slideshow Settings</div>
            <form action="{{ url_for('update_config') }}" method="POST">
                <div class="form-row">
                    <label for="timer">Sleep interval (s):</label>
                    <input type="number" id="timer" name="timer" value="{{ config.timer }}" min="10">
                </div>
                <div class="form-row">
                    <span class="toggle-wrap">
                        <input type="checkbox" class="toggle" id="sync_images" name="sync_images" {% if config.sync_images %}checked{% endif %} onchange="this.form.submit()">
                        <label for="sync_images">Same image on all grouped devices</label>
                    </span>
                </div>
                <div class="form-row">
                    <span class="toggle-wrap">
                        <input type="checkbox" class="toggle" id="shuffle" name="shuffle" {% if config.shuffle %}checked{% endif %} onchange="this.form.submit()">
                        <label for="shuffle">Shuffle images in general pool</label>
                    </span>
                </div>
                <button type="submit" class="btn">💾 Save Settings</button>
                {% for dev in config.devices %}
                <input type="hidden" name="device_mac_{{ loop.index0 }}" value="{{ dev.mac }}">
                <input type="hidden" name="device_orient_{{ loop.index0 }}" value="{{ dev.orientation }}">
                <input type="hidden" name="device_debug_{{ loop.index0 }}" value="{{ '1' if dev.debug else '0' }}">
                <input type="hidden" name="device_shuffle_{{ loop.index0 }}" value="{{ '1' if dev.shuffle else '0' }}">
                <input type="hidden" name="device_flip_l_{{ loop.index0 }}" value="{{ '1' if dev.flip_l else '0' }}">
                <input type="hidden" name="device_flip_p_{{ loop.index0 }}" value="{{ '1' if dev.flip_p else '0' }}">
                {% endfor %}
                <input type="hidden" name="device_count" value="{{ config.devices|length }}">
            </form>
        </div>

        <div class="card">
            <div class="card-title">📡 Registered Hardware Nodes</div>
            <form action="{{ url_for('update_devices') }}" method="POST">
                <div id="device-list">
                {% for dev in config.devices %}
                <div class="device-row" data-idx="{{ loop.index0 }}">
                    <span class="device-idx">#{{ loop.index0 }}</span>
                    <div class="device-name-wrap" style="flex:1;">
                        <span style="font-weight:600; cursor:pointer;" onclick="startDeviceRename(this, '{{ dev.mac }}')">{{ dev.name }}</span> &nbsp;
                        <div class="orient-pill">
                            <button type="button" class="{% if dev.orientation == 'landscape' %}active-l{% endif %}" onclick="setDeviceOrient('{{ dev.mac }}', 'landscape')">🌅 L</button>
                            <button type="button" class="rm-or-btn {% if dev.orientation == 'portrait' %}active-p{% endif %}" onclick="setDeviceOrient('{{ dev.mac }}', 'portrait')">🤳 P</button>
                        </div>
                        <div class="mode-pill" style="margin-left: 0.25rem;">
                            <button type="button" class="{% if dev.mode == 'group' %}active-g{% endif %}" onclick="setDeviceMode('{{ dev.mac }}', 'group')">👥 Group</button>
                            <button type="button" class="{% if dev.mode == 'individual' %}active-i{% endif %}" onclick="setDeviceMode('{{ dev.mac }}', 'individual')">🖼️ Indiv</button>
                        </div>
                        <br><span class="device-mac">{{ dev.mac }}</span>
                    </div>
                    <input type="hidden" name="mac_{{ loop.index0 }}" value="{{ dev.mac }}">
                    <input type="hidden" name="orient_{{ loop.index0 }}" value="{{ dev.orientation }}" id="hidden_orient_{{ dev.mac }}">
                    <button type="button" class="btn btn-danger btn-sm" onclick="removeDevice({{ loop.index0 }})">✕</button>
                </div>
                {% endfor %}
                </div>
                <input type="hidden" name="device_count" id="device_count" value="{{ config.devices|length }}">
                <button type="submit" class="btn" style="margin-top:0.75rem;">💾 Save Changes</button>
            </form>
        </div>
    </div>

    <div class="section-header">
        🖼️ System Media Asset Pool <span class="badge badge-blue">{{ images|length }}</span>
    </div>

    <div id="drop-zone">
        <strong>Drag &amp; drop photos here, or click to browse</strong>
        <p style="font-size: 0.85rem; color: var(--muted); margin-top: 0.25rem;">Supports JPG, PNG, WebP, BMP assets. Native EPD bitstreams are packed automatically on upload.</p>
        <form id="upload-form" action="{{ url_for('upload_file') }}" method="POST" enctype="multipart/form-data">
            <input type="file" id="file-input" name="files" multiple style="display:none;">
        </form>
    </div>
    
    <div id="upload-progress">
        <div id="progress-label">Uploading assets…</div>
        <div class="progress-bar-wrap"><div class="progress-bar" id="progress-bar"></div></div>
    </div>

    <div class="tabs-bar">
        <div class="tab-btn active" data-tab="general">General Pool</div>
        {% for dev in config.devices %}
        <div class="tab-btn" data-tab="device-{{ dev.mac }}">{{ dev.name }}</div>
        {% endfor %}
    </div>

    <div class="tab-content" id="tab-content-general">
        <div id="image-list-general" class="image-list-container">
        {% for img in images %}
        <div class="image-card" data-base="{{ img.base }}">
            <div class="card-header">
                <span class="img-name" onclick="startRename(this, '{{ img.base }}')">{{ img.base.replace('_', ' ') }}</span>
                {% set is_queued = (state.queued_image and state.queued_image.base == img.base) %}
                <button type="button" class="btn-queue {% if is_queued %}active{% endif %}" onclick="queueImage('{{ img.base }}')">
                    ⚡ {% if is_queued %}Queued Next{% else %}Queue Push{% endif %}
                </button>
            </div>
            <div class="triple-preview">
                <div class="preview-cell">
                    <div class="preview-label">Original Matrix Source</div>
                    <img class="preview-img" src="{{ url_for('serve_original', filename=img.original_name) }}">
                </div>
                <div class="preview-cell">
                    <div class="preview-label">
                        <span>Landscape EPD Channel</span>
                        <span class="toggle-wrap">
                            <input type="checkbox" class="toggle" id="tog_l_{{ img.base }}" {% if img.l_on %}checked{% endif %} onchange="toggleOrient('{{ img.base }}', 'l', this.checked)">
                        </span>
                    </div>
                    <div class="crop-container" data-base="{{ img.base }}" data-orient="l" data-offset="{{ img.offset_l }}" data-w="{{ img.orig_w }}" data-h="{{ img.orig_h }}">
                        <img class="crop-bg-img" src="{{ url_for('serve_image', filename=img.base + '_dithered.png') }}" onerror="this.style.display='none'">
                        <div class="crop-overlay-box"></div>
                    </div>
                </div>
                <div class="preview-cell">
                    <div class="preview-label">
                        <span>Portrait EPD Channel</span>
                        <span class="toggle-wrap">
                            <input type="checkbox" class="toggle" id="tog_p_{{ img.base }}" {% if img.p_on %}checked{% endif %} onchange="toggleOrient('{{ img.base }}', 'p', this.checked)">
                        </span>
                    </div>
                    <div class="crop-container" data-base="{{ img.base }}" data-orient="p" data-offset="{{ img.offset_p }}" data-w="{{ img.orig_w }}" data-h="{{ img.orig_h }}">
                        <img class="crop-bg-img" src="{{ url_for('serve_image', filename=img.base + '_dithered.png') }}" onerror="this.style.display='none'">
                        <div class="crop-overlay-box"></div>
                    </div>
                </div>
            </div>
            <div class="card-actions">
                <form action="{{ url_for('delete_file', filename=img.original_name) }}" method="POST" onsubmit="return confirm('Purge this asset from cluster cache?');">
                    <button type="submit" class="btn btn-danger btn-sm">🗑 Remove Asset</button>
                </form>
            </div>
        </div>
        {% endfor %}
        </div>
    </div>

    {% for dev in config.devices %}
    <div class="tab-content" id="tab-content-device-{{ dev.mac }}" style="display:none;">
        <div class="image-list-container">
            <h3>Individual Lists for Device: {{ dev.name }}</h3>
            <p style="color:var(--muted); font-size:0.9rem; margin-bottom:1rem;">To configure individual playlists, drop media into the dynamic matrix array pool.</p>
        </div>
    </div>
    {% endfor %}
</div>

<div id="toast"></div>

<script>
function changeTheme(t) { document.documentElement.setAttribute('data-theme', t); localStorage.setItem('picframes-theme', t); }
function toast(m) { const e = document.getElementById('toast'); e.textContent = m; e.classList.add('show'); setTimeout(() => e.classList.remove('show'), 2500); }

const dz = document.getElementById('drop-zone');
const fi = document.getElementById('file-input');
dz.addEventListener('click', () => fi.click());
dz.addEventListener('dragover', e => { e.preventDefault(); });
dz.addEventListener('drop', e => { e.preventDefault(); if(e.dataTransfer.files.length) handleUpload(e.dataTransfer.files); });
fi.addEventListener('change', () => { if(fi.files.length) handleUpload(fi.files); });

function handleUpload(files) {
    const fd = new FormData();
    for(let f of files) fd.append('files', f);
    document.getElementById('upload-progress').style.display = 'block';
    const xhr = new XMLHttpRequest();
    xhr.open('POST', '/upload');
    xhr.upload.addEventListener('progress', e => {
        if(e.lengthComputable) {
            const pct = Math.round(e.loaded / e.total * 100);
            document.getElementById('progress-bar').style.width = pct + '%';
        }
    });
    xhr.onload = () => window.location.reload();
    xhr.send(fd);
}

function toggleOrient(base, orient, enabled) {
    fetch('/toggle_orient', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({base, orient, enabled})
    }).then(r => r.json()).then(d => toast(d.ok ? 'State Updated' : 'Error'));
}

function setDeviceOrient(mac, orientation) {
    fetch('/device_orientation', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({mac, orientation})
    }).then(r => r.json()).then(d => {
        if(d.ok) {
            const h = document.getElementById('hidden_orient_' + mac);
            if(h) h.value = orientation;
            toast('Orientation changed to ' + orientation);
            setTimeout(() => window.location.reload(), 500);
        }
    });
}

function setDeviceMode(mac, mode) {
    fetch('/device_mode', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({mac, mode})
    }).then(r => r.json()).then(d => {
        if(d.ok) { toast('Device mode altered'); setTimeout(() => window.location.reload(), 500); }
    });
}

function queueImage(base) {
    fetch('/api/queue', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({base, source: 'general'})
    }).then(r => r.json()).then(d => {
        if(d.ok) {
            toast(d.action === 'queued' ? 'Asset pushed to front of execution queue' : 'Cleared queue line');
            setTimeout(() => window.location.reload(), 500);
        }
    });
}

function startRename(span, base) {
    const wrap = span.closest('.img-name-wrap') || span.parentElement;
    const input = document.createElement('input');
    let finished = false;
    input.type = 'text'; input.className = 'rename-input'; input.value = base.replaceAll('_', ' ');
    span.style.display = 'none';
    wrap.insertBefore(input, span);
    input.focus(); input.select();
    function commit() {
        if (finished) return; finished = true;
        const newBase = input.value.trim();
        if (!newBase || newBase.replaceAll(' ', '_') === base) { cancel(); return; }
        fetch('/rename', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({old_base: base, new_base: newBase})
        }).then(r => r.json()).then(d => {
            if (d.ok) { toast(`Renamed to ${newBase}`); window.location.reload(); }
            else { toast(`Error: ${d.error}`); cancel(); }
        });
    }
    function cancel() { if (finished) return; finished = true; input.remove(); span.style.display = ''; }
    input.addEventListener('keydown', ev => { if (ev.key === 'Enter') commit(); if (ev.key === 'Escape') cancel(); });
    input.addEventListener('blur', commit);
}

function startDeviceRename(span, mac) {
    const wrap = span.parentElement;
    const input = document.createElement('input');
    let finished = false;
    input.type = 'text'; input.className = 'rename-input'; input.value = span.textContent;
    span.style.display = 'none';
    wrap.insertBefore(input, span);
    input.focus(); input.select();
    function commit() {
        if (finished) return; finished = true;
        const newName = input.value.trim();
        if (!newName || newName === span.textContent) { cancel(); return; }
        fetch('/device_rename', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({mac, name: newName})
        }).then(r => r.json()).then(d => {
            if (d.ok) { window.location.reload(); }
            else { cancel(); }
        });
    }
    function cancel() { if (finished) return; finished = true; input.remove(); span.style.display = ''; }
    input.addEventListener('keydown', ev => { if (ev.key === 'Enter') commit(); if (ev.key === 'Escape') cancel(); });
    input.addEventListener('blur', commit);
}

function removeDevice(idx) {
    document.querySelector(`.device-row[data-idx="${idx}"]`)?.remove();
    document.getElementById('device_count').value = document.querySelectorAll('.device-row').length;
}

function switchTab(tabId) {
    document.querySelectorAll('.tab-btn').forEach(btn => {
        if (btn.dataset.tab === tabId) btn.classList.add('active');
        else btn.classList.remove('active');
    });
    document.querySelectorAll('.tab-content').forEach(content => {
        if (content.id === `tab-content-${tabId}`) content.style.display = 'block';
        else content.style.display = 'none';
    });
}

document.querySelectorAll('.tab-btn').forEach(btn => {
    btn.addEventListener('click', () => { switchTab(btn.dataset.tab); });
});

function switchToDeviceTab(mac) { switchTab(`device-${mac}`); }

function updateCropOverlay(container) {
    const orient = container.dataset.orient;
    const offset = parseFloat(container.dataset.offset);
    const w = parseFloat(container.dataset.w);
    const h = parseFloat(container.dataset.h);
    const r = w / h;
    const box = container.querySelector('.crop-overlay-box');
    let widthPct, heightPct, leftPct, topPct;
    if (orient === 'l') {
        const targetR = 5 / 3;
        if (r <= targetR) { widthPct = 100; heightPct = (r / targetR) * 100; leftPct = 0; topPct = offset * (100 - heightPct); }
        else { heightPct = 100; widthPct = (targetR / r) * 100; topPct = 0; leftPct = 50 - (widthPct / 2); }
    } else {
        const targetR = 3 / 5;
        if (r >= targetR) { heightPct = 100; widthPct = (targetR / r) * 100; topPct = 0; leftPct = offset * (100 - widthPct); }
        else { widthPct = 100; heightPct = (r / targetR) * 100; leftPct = 0; topPct = 50 - (heightPct / 2); }
    }
    box.style.width = widthPct + '%'; box.style.height = heightPct + '%'; box.style.left = leftPct + '%'; box.style.top = topPct + '%';
}

document.querySelectorAll('.crop-container').forEach(container => {
    updateCropOverlay(container);
    const box = container.querySelector('.crop-overlay-box');
    let isDragging = false; let startY = 0; let startX = 0; let startOffset = 0;
    box.addEventListener('mousedown', e => {
        e.preventDefault(); e.stopPropagation(); isDragging = true;
        startY = e.clientY; startX = e.clientX; startOffset = parseFloat(container.dataset.offset);
    });
    window.addEventListener('mousemove', e => {
        if (!isDragging) return;
        const containerRect = container.getBoundingClientRect();
        const orient = container.dataset.orient;
        const w = parseFloat(container.dataset.w); const h = parseFloat(container.dataset.h); const r = w / h;
        let deltaOffset = 0;
        if (orient === 'l') {
            const targetR = 5 / 3;
            if (r <= targetR) {
                const heightPct = (r / targetR) * 100;
                const maxDragPx = containerRect.height * (1 - heightPct / 100);
                if (maxDragPx > 0) { deltaOffset = (e.clientY - startY) / maxDragPx; }
            }
        } else {
            const targetR = 3 / 5;
            if (r >= targetR) {
                const widthPct = (targetR / r) * 100;
                const maxDragPx = containerRect.width * (1 - widthPct / 100);
                if (maxDragPx > 0) { deltaOffset = (e.clientX - startX) / maxDragPx; }
            }
        }
        let newOffset = Math.max(0, Math.min(1, startOffset + deltaOffset));
        container.dataset.offset = newOffset; updateCropOverlay(container);
    });
    window.addEventListener('mouseup', () => {
        if (!isDragging) return; isDragging = false;
        fetch('/recrop', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({base: container.dataset.base, orient: container.dataset.orient, offset: parseFloat(container.dataset.offset)})
        }).then(r => r.json()).then(d => { if(d.ok) toast('Asset re-cropped successfully'); });
    });
});

window.addEventListener('DOMContentLoaded', () => { applyTheme(localStorage.getItem('picframes-theme') || 'default'); });
function applyTheme(t) { document.documentElement.setAttribute('data-theme', t); const s = document.getElementById('theme-select'); if(s) s.value = t; }
</script>
</body>
</html>
"""

# ---------------------------------------------------------------------------
# Web routes
# ---------------------------------------------------------------------------

@app.route('/')
def index():
    config  = load_config()
    state   = load_state()
    order   = load_image_order()
    enabled = load_enabled()
    crops   = load_crops()

    images = []
    for base in order:
        original_name = None
        for f in os.listdir(ORIGINALS_DIR):
            if os.path.splitext(f)[0] == base: original_name = f; break
        if original_name is None: continue

        has_l = os.path.exists(os.path.join(IMAGES_DIR, base + LANDSCAPE_SUFFIX))
        has_p = os.path.exists(os.path.join(IMAGES_DIR, base + PORTRAIT_SUFFIX))
        flags = _flags(enabled, base)
        ensure_dithered_original(base)

        try:
            with Image.open(os.path.join(ORIGINALS_DIR, original_name)) as img_obj:
                orig_w, orig_h = img_obj.size
        except Exception:
            orig_w, orig_h = 800, 480

        crop_offsets = crops.get(base, {"l": 0.5, "p": 0.5})
        images.append({
            'base': base, 'original_name': original_name,
            'has_l': has_l, 'has_p': has_p,
            'l_on': flags["l"], 'p_on': flags["p"], 'title_on': flags.get("title", False),
            'caption_mode': flags.get("caption_mode", 'none'),
            'description': flags.get("description", ''),
            'orig_w': orig_w, 'orig_h': orig_h,
            'offset_l': crop_offsets.get("l", 0.5),
            'offset_p': crop_offsets.get("p", 0.5),
        })

    image_by_base = {img['base']: img for img in images}
    now_ts      = int(time.time())
    node_status = state.get('last_seen', {})
    device_ips = state.get('device_ips', {})
    device_images = state.get('device_images', {})
    phase       = state.get('phase', PHASE_GATHERING)

    return render_template_string(HTML_TEMPLATE, images=images, config=config, state=state,
                                  node_status=node_status, device_ips=device_ips,
                                  device_images=device_images, now_ts=now_ts, phase=phase,
                                  image_by_base=image_by_base, version=SERVER_VERSION)


@app.route('/upload', methods=['POST'])
def upload_file():
    for file in request.files.getlist('files'):
        if not file.filename: continue
        _, ext = os.path.splitext(file.filename)
        if ext.lower() not in ALLOWED_EXTENSIONS: continue
        original_path = os.path.join(ORIGINALS_DIR, file.filename)
        file.save(original_path)
        base, _ = os.path.splitext(file.filename)
        convert_image(original_path, base)
    return redirect(url_for('index'))


@app.route('/convert/<filename>', methods=['POST'])
def convert_file(filename):
    original_path = os.path.join(ORIGINALS_DIR, filename)
    if not os.path.exists(original_path): return "File not found", 404
    base, _ = os.path.splitext(filename)
    return redirect(url_for('index')) if convert_image(original_path, base) else ("Conversion failed", 500)


@app.route('/convert_all', methods=['POST'])
def convert_all():
    count = 0
    for f in sorted(os.listdir(ORIGINALS_DIR)):
        if os.path.splitext(f)[1].lower() in ALLOWED_EXTENSIONS:
            base = os.path.splitext(f)[0]
            convert_image(os.path.join(ORIGINALS_DIR, f), base)
            count += 1
    return redirect(url_for('index'))


@app.route('/delete/<filename>', methods=['POST'])
def delete_file(filename):
    base, _ = os.path.splitext(filename)
    cfg = load_config(); assigned_to_any_device = False
    for dev in cfg.get('devices', []):
        for img in dev.get('images', []):
            if img.get('base') == base: assigned_to_any_device = True; break
            
    if assigned_to_any_device:
        order = load_image_order()
        if base in order: order.remove(base); save_image_order(order)
    else:
        for path in [
            os.path.join(ORIGINALS_DIR, filename),
            os.path.join(IMAGES_DIR, base + LANDSCAPE_SUFFIX),
            os.path.join(IMAGES_DIR, base + PORTRAIT_SUFFIX),
            os.path.join(IMAGES_DIR, base + '_dithered.png'),
            os.path.join(IMAGES_DIR, base + '_l.bin'),
            os.path.join(IMAGES_DIR, base + '_p.bin'),
        ]:
            if os.path.exists(path): os.remove(path)

        order = load_image_order()
        if base in order: order.remove(base); save_image_order(order)
        enabled = load_enabled(); enabled.pop(base, None); save_enabled(enabled)
        crops = load_crops(); crops.pop(base, None); save_crops(crops)

        changed = False
        for dev in cfg.get('devices', []):
            old_len = len(dev.get('images', []))
            dev['images'] = [img for img in dev.get('images', []) if img['base'] != base]
            if len(dev['images']) != old_len:
                changed = True
                if not dev['images']: dev['mode'] = 'group'
        if changed: save_config(cfg)

    trigger_redownload()
    return redirect(url_for('index'))


@app.route('/rename', methods=['POST'])
def rename_image():
    data = request.get_json(); old_base = data.get('old_base', '').strip(); new_base = data.get('new_base', '').strip()
    if not old_base or not new_base: return jsonify({'ok': False, 'error': 'Missing name'}), 400
    new_base = new_base.replace(' ', '_')
    if len(new_base) > 78: return jsonify({'ok': False, 'error': 'Title too long'}), 400

    for f in os.listdir(ORIGINALS_DIR):
        base, ext = os.path.splitext(f)
        if base == old_base:
            os.rename(os.path.join(ORIGINALS_DIR, f), os.path.join(ORIGINALS_DIR, new_base + ext)); break

    for old_f, new_f in [
        (old_base + LANDSCAPE_SUFFIX, new_base + LANDSCAPE_SUFFIX),
        (old_base + PORTRAIT_SUFFIX,  new_base + PORTRAIT_SUFFIX),
        (old_base + '_l.bin',         new_base + '_l.bin'),
        (old_base + '_p.bin',         new_base + '_p.bin'),
        (old_base + '_dithered.png',  new_base + '_dithered.png'),
    ]:
        old_p, new_p = os.path.join(IMAGES_DIR, old_f), os.path.join(IMAGES_DIR, new_f)
        if os.path.exists(old_p): os.rename(old_p, new_p)

    order = load_image_order()
    if old_base in order: order[order.index(old_base)] = new_base; save_image_order(order)
    enabled = load_enabled()
    if old_base in enabled: enabled[new_base] = enabled.pop(old_base); save_enabled(enabled)
    crops = load_crops()
    if old_base in crops: crops[new_base] = crops.pop(old_base); save_crops(crops)

    cfg = load_config(); changed = False
    for dev in cfg.get('devices', []):
        for img in dev.get('images', []):
            if img['base'] == old_base: img['base'] = new_base; changed = True
    if changed: save_config(cfg)

    trigger_redownload()
    return jsonify({'ok': True, 'new_base': new_base})


@app.route('/toggle_orient', methods=['POST'])
def toggle_orient():
    data = request.get_json(); base = data.get('base'); orient = data.get('orient'); val = bool(data.get('enabled', True))
    if not base or orient not in ('l', 'p', 'title'): return jsonify({'ok': False}), 400
    enabled = load_enabled(); flags = _flags(enabled, base); flags[orient] = val
    enabled[base] = flags; save_enabled(enabled); trigger_redownload()
    return jsonify({'ok': True})


@app.route('/update_caption', methods=['POST'])
def update_caption():
    data = request.get_json() or {}; base = data.get('base'); mode = data.get('caption_mode'); desc = data.get('description')
    if not base: return jsonify({'ok': False, 'error': 'Missing base'}), 400
    enabled = load_enabled(); flags = _flags(enabled, base)
    if mode is not None: flags['caption_mode'] = mode
    if desc is not None: flags['description'] = desc
    enabled[base] = flags; save_enabled(enabled); trigger_redownload()
    return jsonify({'ok': True})


@app.route('/reorder', methods=['POST'])
def reorder():
    order = request.get_json().get('order', [])
    save_image_order(order); trigger_redownload()
    return jsonify({'ok': True})


@app.route('/recrop', methods=['POST'])
def recrop():
    data = request.get_json(); base = data.get('base'); orient = data.get('orient'); offset = data.get('offset')
    if not base or orient not in ('l', 'p') or offset is None: return jsonify({'ok': False, 'error': 'Invalid parameters'}), 400
    crops = load_crops()
    if base not in crops: crops[base] = {"l": 0.5, "p": 0.5}
    crops[base][orient] = max(0.0, min(1.0, float(offset))); save_crops(crops)

    original_name = None
    for f in os.listdir(ORIGINALS_DIR):
        if os.path.splitext(f)[0] == base: original_name = f; break
    if not original_name: return jsonify({'ok': False, 'error': 'Original not found'}), 404

    if convert_image(os.path.join(ORIGINALS_DIR, original_name), base):
        trigger_redownload(); return jsonify({'ok': True})
    return jsonify({'ok': False, 'error': 'Re-conversion failed'}), 500


@app.route('/api/daily-config', methods=['GET'])
@app.route('/api/config', methods=['GET'])
def api_config():
    cfg = load_config(); now = datetime.now()
    cfg['current_date'] = now.strftime('%Y-%m-%d'); cfg['timestamp'] = int(now.timestamp())
    return jsonify(cfg)


@app.route('/api/images', methods=['GET'])
def api_images():
    cfg = load_config(); caller_ip = _caller_ip(); all_files = get_unified_index()
    mac = request.args.get('mac', '').strip().lower() or request.headers.get('X-Device-Mac', '').strip().lower()
    dev_cfg = None
    if mac: dev_cfg = next((d for d in cfg.get('devices', []) if d['mac'].lower() == mac), None)
    if not dev_cfg: dev_cfg = next((d for d in cfg.get('devices', []) if d.get('ip') == caller_ip), None)

    if dev_cfg:
        suffix = orient_suffix(dev_cfg.get('orientation', 'landscape'))
        if dev_cfg.get('mode', 'group') == 'individual':
            dev_imgs = dev_cfg.get('images', [])
            orient_char = 'l' if dev_cfg.get('orientation') == 'landscape' else 'p'
            files = [item['base'] + suffix for item in dev_imgs if item.get(orient_char, True) and os.path.exists(os.path.join(IMAGES_DIR, item['base'] + suffix))]
        else:
            files = [f for f in all_files if f.endswith(suffix)]
    else:
        files = all_files
    return jsonify(files)

def get_latest_github_release_url(hw_profile, current_version):
    try:
        _update_github_release_cache(); tag = _github_latest_release_cache['tag']
        def parse_ver(v):
            try: return tuple(int(x) for x in v.split('.')[:3])
            except Exception: return (0, 0, 0)
        if tag and parse_ver(tag) > parse_ver(current_version): return _github_latest_release_cache['assets'].get(hw_profile)
    except Exception as e: logger.error(f"Error checking GitHub releases: {e}")
    return None

@app.route('/update', methods=['GET'])
@app.route('/api/update', methods=['GET'])
def api_update():
    hw = request.args.get('hw', '').strip(); version = request.args.get('version', '').strip()
    if not hw: return "Missing hw profile parameter", 400
    update_url = get_latest_github_release_url(hw, version)
    return (update_url, 200) if update_url else ("", 204)

@app.route('/api/daily-zip', methods=['GET'])
def api_daily_zip():
    cfg = load_config(); caller_ip = _caller_ip(); devices = cfg.get('devices', [])
    mac = request.args.get('mac', '').strip().lower() or request.headers.get('X-Device-Mac', '').strip().lower() or caller_ip.lower()
    
    dev_cfg = next((d for d in devices if d['mac'].lower() == mac), None)
    if not dev_cfg:
        dev_cfg = {'mac': mac, 'name': mac, 'orientation': 'portrait', 'debug': False, 'mode': 'group', 'images': []}
        cfg.setdefault('devices', []).append(dev_cfg); save_config(cfg)

    mac = dev_cfg.get('mac').lower()
    with _state_lock:
        state = load_state(); state.setdefault('redownload', {})
        state['redownload'][mac] = False; save_state(state)

    orientation = dev_cfg.get('orientation', 'portrait')
    if dev_cfg.get('mode', 'group') == 'individual':
        orient_char = 'l' if orientation == 'landscape' else 'p'
        active_bases = [item['base'] for item in dev_cfg.get('images', []) if item.get(orient_char, True)]
    else:
        active_bases = get_active_bases(orientation)
        
    bin_suffix = '_l.bin' if orientation == 'landscape' else '_p.bin'
    candidates = [b + bin_suffix for b in active_bases]
    
    queued = state.get('queued_image')
    if queued and ((queued.get('source') == 'general' and dev_cfg.get('mode', 'group') == 'group') or (queued.get('source', '').lower() == mac)):
        q_filename = queued.get('base') + bin_suffix
        if q_filename not in candidates: candidates.append(q_filename)

    serializable_cfg = {
        "timer": int(cfg.get("timer", 900)),
        "wake_timeout": int(cfg.get("wake_timeout", 45)),
        "shuffle": bool(cfg.get("shuffle", False)),
        "sync_images": bool(cfg.get("sync_images", False)),
        "enabled": {str(k): dict(v) for k, v in load_enabled().items()}
    }

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode='w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        zf.writestr('config.json', json.dumps(serializable_cfg, indent=2))
        manifest_data = json.dumps(candidates, indent=2)
        zf.writestr('index.json', manifest_data)
        zf.writestr('list.json', manifest_data)
        
        for bin_filename in candidates:
            bin_path = ensure_bin_file(bin_filename[:-6], orientation)
            if bin_path and os.path.exists(bin_path):
                if (orientation == 'landscape' and dev_cfg.get('flip_l', False)) or (orientation != 'landscape' and dev_cfg.get('flip_p', False)):
                    try:
                        with open(bin_path, 'rb') as f: data = f.read()
                        flipped_data = bytes([ ((b & 0x0F) << 4) | ((b & 0xF0) >> 4) for b in reversed(data) ])
                        zf.writestr(bin_filename, flipped_data)
                    except Exception: zf.write(bin_path, arcname=bin_filename)
                else: zf.write(bin_path, arcname=bin_filename)

    size = buf.tell(); buf.seek(0)
    return Response(buf, mimetype='application/zip',
                    headers={'Content-Disposition': 'attachment; filename="daily.zip"', 'Content-Length': str(size)})

# ---------------------------------------------------------------------------
# Wakeup — 3-phase protocol
# ---------------------------------------------------------------------------

def _caller_ip():
    if request.headers.get('X-Forwarded-For'): return request.headers['X-Forwarded-For'].split(',')[0].strip()
    return request.remote_addr

def _advance_index(cfg, state, num_devices):
    state['last_change_ts'] = int(time.time())
    any_sequential = any(not (d.get('shuffle', False) if d.get('mode', 'group') == 'individual' else cfg.get('shuffle', False)) for d in cfg.get('devices', []))
    if any_sequential or not cfg.get('devices'):
        if cfg.get('sync_images', False):
            pool = get_active_bases(None)
            if pool: state['current_index'] = (state['current_index'] + 1) % len(pool)
        else: state['current_index'] = state['current_index'] + num_devices

def _reset_round(state):
    state['phase'] = PHASE_GATHERING
    state['phase_checkins'] = {}; state['phase_ready_ack'] = {}; state['phase_change_ack'] = {}
    state['round_assignments'] = {}; state['queued_image'] = None

@app.route('/api/wakeup', methods=['POST'])
def api_wakeup():
    cfg = load_config(); devices = cfg.get('devices', [])
    data = request.get_json() or {}
    mac = data.get('mac', '').strip().lower() or request.headers.get('X-Device-Mac', '').strip().lower() or _caller_ip().lower()
    known_macs = [d['mac'].lower() for d in devices if d.get('mac')]

    if mac not in known_macs:
        ip = _caller_ip(); ip_device = next((d for d in devices if d.get('name') == ip and not d.get('mac')), None)
        if ip_device: ip_device['mac'] = mac; ip_device['name'] = mac
        else: devices.append({'mac': mac, 'name': mac, 'orientation': 'portrait', 'debug': False, 'mode': 'group', 'images': []})
        cfg['devices'] = devices; save_config(cfg); known_macs = [d['mac'].lower() for d in devices]

    dev_cfg = next((d for d in devices if d['mac'].lower() == mac), None)
    if not dev_cfg: return Response("WAIT - None", mimetype='text/plain'), 200
    device_idx = devices.index(dev_cfg); num_devices = len(devices)

    with _state_lock:
        state = load_state(); now_ts = int(time.time())
        state.setdefault('last_seen', {})[mac] = now_ts
        state.setdefault('device_ips', {})[mac] = _caller_ip()

        if data.get('version') or request.headers.get('User-Agent', '').lower().startswith('micropython'):
            version = data.get('version', '').strip() or request.args.get('version', '').strip()
            latest_version = get_latest_firmware_version()
            def parse_version(v_str):
                try: return [int(x) for x in v_str.split('.')]
                except Exception: return [0, 0, 0]
            if not version or parse_version(version) < parse_version(latest_version):
                save_state(state); return Response("UPDATE", mimetype='text/plain'), 200

        if dev_cfg.get('debug', False):
            save_state(state); return Response("DEBUG", mimetype='text/plain'), 200

        phase = state.get('phase', PHASE_GATHERING)
        if any((d.get('shuffle', False) if d.get('mode', 'group') == 'individual' else cfg.get('shuffle', False)) for d in devices) and not state.get('round_assignments'):
            _build_shuffle_assignments(cfg, state)

        target_file = _target_for_device(cfg, state, mac, device_idx, num_devices)
        if target_file and target_file.endswith('.bmp'): target_file = target_file[:-4] + '.bin'
        if target_file: ensure_bin_file(target_file[:-6], dev_cfg.get('orientation', 'landscape'))
        redownload_suffix = " - REDOWNLOAD" if state.get('redownload', {}).get(mac, False) else ""

        if phase == PHASE_GATHERING:
            state.setdefault('phase_checkins', {})[mac] = now_ts
            if set(known_macs) <= set(state['phase_checkins'].keys()):
                state['phase'] = PHASE_READY; state['phase_ready_ack'] = {}; phase = PHASE_READY
            else:
                remaining = (state.setdefault('last_change_ts', now_ts) + cfg.get('timer', 900)) - now_ts
                save_state(state)
                msg_body = f"WAIT - {target_file}"
                if remaining > 10:
                    msg_body += f" - {remaining}"
                if redownload_suffix:
                    msg_body += redownload_suffix
                return Response(msg_body, mimetype='text/plain'), 200

        if phase == PHASE_READY:
            state.setdefault('phase_ready_ack', {})[mac] = now_ts
            if set(known_macs) <= set(state['phase_ready_ack'].keys()):
                state['phase'] = PHASE_CHANGE; state['phase_change_ack'] = {}; phase = PHASE_CHANGE
            else:
                save_state(state); return Response(f"READY - {target_file}{redownload_suffix}", mimetype='text/plain'), 200

        if phase == PHASE_CHANGE:
            state.setdefault('phase_change_ack', {})[mac] = now_ts
            state.setdefault('device_images', {})[mac] = target_file
            if set(known_macs) <= set(state['phase_change_ack'].keys()):
                _advance_index(cfg, state, num_devices)
                if (now_ts - state.get('last_sync_ts', 0)) >= 86400: state['last_sync_ts'] = now_ts
                _reset_round(state)
            save_state(state); return Response(f"CHANGE - {target_file}{redownload_suffix}", mimetype='text/plain'), 200

        save_state(state); return Response("WAIT - None", mimetype='text/plain'), 200


@app.route('/api/wakeup/reset', methods=['POST'])
def api_reset_state():
    state = load_state(); _reset_round(state); save_state(state)
    return jsonify({"ok": True, "phase": state['phase']})


@app.route('/api/wakeup/next', methods=['POST'])
def api_next_image():
    with _state_lock:
        state = load_state(); _reset_round(state); save_state(state)
    return jsonify({"ok": True, "current_index": state.get('current_index', 0), "phase": state['phase']})

# ---------------------------------------------------------------------------
# Safe Global Post-Initialization Execution
# ---------------------------------------------------------------------------

try:
    for directory_path in (SHARE_DIR, ORIGINALS_DIR, IMAGES_DIR, CONFIG_DIR):
        os.makedirs(directory_path, exist_ok=True)
    init_db()
except Exception as global_err:
    logger.error(f"Global thread initial runtime initialization failed: {global_err}")

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8000))
    zeroconf_instance = start_mdns_broadcast(port)
    try: app.run(host='0.0.0.0', port=port, debug=False)
    finally:
        if zeroconf_instance: zeroconf_instance.close()

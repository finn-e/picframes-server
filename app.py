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
from flask import Flask, request, jsonify, render_template_string, send_from_directory, redirect, url_for
from zeroconf import IPVersion, Zeroconf, ServiceInfo
import socket
import sqlite3

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)
SERVER_VERSION = "0.6.7"

SHARE_DIR     = os.environ.get('SHARE_DIR', '/share')
CONFIG_DIR    = os.environ.get('CONFIG_DIR', '/config')
ORIGINALS_DIR = os.path.join(SHARE_DIR, 'originals')
IMAGES_DIR    = os.path.join(SHARE_DIR, 'images')   # flat: base_l.bmp / base_p.bmp

for d in (SHARE_DIR, ORIGINALS_DIR, IMAGES_DIR, CONFIG_DIR):
    os.makedirs(d, exist_ok=True)

FIRMWARE_DIR = os.environ.get('FIRMWARE_DIR', '/app/picframe-ESP32S3')
if not os.path.exists(FIRMWARE_DIR):
    FIRMWARE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'picframe-ESP32S3')

def get_latest_firmware_version():
    main_py_path = os.path.join(FIRMWARE_DIR, 'main.py')
    if os.path.exists(main_py_path):
        try:
            with open(main_py_path, 'r') as f:
                content = f.read()
            m = re.search(r'FIRMWARE_VERSION\s*=\s*["\']([^"\']+)["\']', content)
            if m:
                return m.group(1)
        except Exception as e:
            logger.warning(f"Could not extract firmware version from main.py: {e}")
    return '0.1.1'

# ---------------------------------------------------------------------------
# E6 Spectra 7.3" 6-color palette
# Driver indices: 0=Black, 1=White, 2=Green, 3=Blue, 4=Red, 5=Yellow
# ---------------------------------------------------------------------------
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

# ---------------------------------------------------------------------------
# Zeroconf mDNS Service Advertisement
# ---------------------------------------------------------------------------

def start_mdns_broadcast(port=8000):
    try:
        zeroconf = Zeroconf(ip_version=IPVersion.V4Only)
        
        # Get active local IP address
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
        logger.info(f"Broadcasting mDNS service '_picframes._tcp.local.' at {local_ip}:{port}")
        return zeroconf
    except Exception as e:
        logger.error(f"Failed to start mDNS broadcast: {e}")
        return None

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DB_PATH = os.path.join(CONFIG_DIR, 'picframes.db')

# Copy legacy DB from NFS share to local config volume if local DB doesn't exist
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
    # 1. Global settings table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS global_settings (
        key TEXT PRIMARY KEY,
        value TEXT
    )
    """)
    # 2. Devices table
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
    # 3. Crops table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS crops (
        base TEXT PRIMARY KEY,
        l REAL,
        p REAL
    )
    """)
    # 4. Enabled table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS enabled (
        base TEXT PRIMARY KEY,
        l INTEGER,
        p INTEGER
    )
    """)
    # 5. Image order table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS image_order (
        base TEXT PRIMARY KEY,
        sort_order INTEGER
    )
    """)
    conn.commit()

    # Migrate legacy JSON files if database is empty / not migrated yet
    cursor.execute("SELECT COUNT(*) FROM global_settings")
    if cursor.fetchone()[0] == 0:
        logger.info("Migrating legacy JSON config/state files to SQLite...")
        
        # Migrate config
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

        # Migrate crops
        crops_path = os.path.join(SHARE_DIR, 'image_crops.json')
        if os.path.exists(crops_path):
            try:
                with open(crops_path) as f:
                    crops = json.load(f)
                for base, val in crops.items():
                    cursor.execute("INSERT OR REPLACE INTO crops (base, l, p) VALUES (?, ?, ?)", (base, val.get('l', 0.5), val.get('p', 0.5)))
            except Exception as e:
                logger.error(f"Migration of image_crops.json failed: {e}")

        # Migrate enabled
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

        # Migrate image order
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

        # Migrate server state
        state_path = os.path.join(SHARE_DIR, 'server_state.json')
        if os.path.exists(state_path):
            try:
                with open(state_path) as f:
                    state = json.load(f)
                for k, v in state.items():
                    if isinstance(v, (dict, list)):
                        val_str = json.dumps(v)
                    else:
                        val_str = str(v)
                    cursor.execute("INSERT OR REPLACE INTO global_settings (key, value) VALUES (?, ?)", (k, val_str))
            except Exception as e:
                logger.error(f"Migration of server_state.json failed: {e}")

        conn.commit()
    conn.close()

# Initialize DB on startup
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
            if key == 'timer':
                defaults['timer'] = int(val)
            elif key == 'wake_timeout':
                defaults['wake_timeout'] = int(val)
            elif key == 'shuffle':
                defaults['shuffle'] = (val == '1')
            elif key == 'sync_images':
                defaults['sync_images'] = (val == '1')
        
        cursor.execute("SELECT mac, name, orientation, debug, mode, shuffle, flip_l, flip_p, images_json FROM devices")
        devices = []
        for row in cursor.fetchall():
            # Get shuffle, flip_l, flip_p with default values if they are None/empty
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
                try:
                    defaults[key] = json.loads(val)
                except Exception:
                    pass
        conn.close()
    except Exception as e:
        logger.error(f"SQL load_state failed: {e}")
    return defaults

def save_state(state):
    try:
        conn = get_db()
        cursor = conn.cursor()
        for k, v in state.items():
            if isinstance(v, (dict, list)):
                val_str = json.dumps(v)
            else:
                val_str = str(v)
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
        logger.info(f"Flagged device {mac} for redownload")
    else:
        cfg = load_config()
        for dev in cfg.get('devices', []):
            if dev.get('mac'):
                state['redownload'][dev['mac'].lower()] = True
        logger.info("Flagged all devices for redownload")
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
        if row and row['value'] == '1':
            initialized = True
            
        if not order and not initialized:
            order = sorted([
                os.path.splitext(f)[0]
                for f in os.listdir(ORIGINALS_DIR)
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
        cursor.execute("SELECT base, l, p FROM enabled")
        for row in cursor.fetchall():
            enabled[row['base']] = {"l": bool(row['l']), "p": bool(row['p'])}
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
            cursor.execute("INSERT OR REPLACE INTO enabled (base, l, p) VALUES (?, ?, ?)", (base, 1 if l_val else 0, 1 if p_val else 0))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"SQL save_enabled failed: {e}")

def _flags(enabled, base):
    v = enabled.get(base, {"l": True, "p": True})
    if isinstance(v, bool): return {"l": v, "p": v}
    return {"l": v.get("l", True), "p": v.get("p", True)}

# ---------------------------------------------------------------------------
# Active image helpers
# ---------------------------------------------------------------------------

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
        if orientation == 'landscape' and has_l and flags["l"]:
            result.append(base)
        elif orientation == 'portrait' and has_p and flags["p"]:
            result.append(base)
        elif orientation is None and ((has_l and flags["l"]) or (has_p and flags["p"])):
            result.append(base)
    return result

def get_unified_index():
    order   = load_image_order()
    enabled = load_enabled()
    result  = []
    for base in order:
        flags = _flags(enabled, base)
        lf = landscape_file(base)
        pf = portrait_file(base)
        if flags["l"] and os.path.exists(os.path.join(IMAGES_DIR, lf)):
            result.append(lf)
        if flags["p"] and os.path.exists(os.path.join(IMAGES_DIR, pf)):
            result.append(pf)
    return result

# ---------------------------------------------------------------------------
# Image conversion
# ---------------------------------------------------------------------------

def dither_floyd_steinberg(img_array, palette):
    h, w, _ = img_array.shape
    padded = np.pad(img_array, ((0, 1), (1, 1), (0, 0)), mode='edge').astype(np.float32)
    for y in range(h):
        for x in range(1, w + 1):
            old_val = padded[y, x].copy()
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
    if os.path.exists(dith_path):
        return dith_path

    # Find original image
    original_name = None
    for f in os.listdir(ORIGINALS_DIR):
        b, _ = os.path.splitext(f)
        if b == base: original_name = f; break
    if not original_name:
        return None

    try:
        src_path = os.path.join(ORIGINALS_DIR, original_name)
        img = Image.open(src_path)
        img = ImageOps.exif_transpose(img).convert('RGB')
        img.thumbnail((800, 800), Image.Resampling.LANCZOS)
        img_dith = Image.fromarray(dither_floyd_steinberg(np.array(img, dtype=np.float32), PALETTE))
        img_dith.save(dith_path, format='PNG')
        logger.info(f"Generated dithered original for {base}")
        return dith_path
    except Exception as e:
        logger.error(f"Error generating dithered original for {base}: {e}", exc_info=True)
        return None

def rgb_array_to_spectra6_bitstream_fast(img_array):
    h, w, _ = img_array.shape
    hardware_map = np.array([0, 1, 6, 5, 3, 2], dtype=np.uint8)
    pixels = img_array.reshape(-1, 3)
    dists = np.sum((pixels[:, None, :] - PALETTE[None, :, :])**2, axis=2)
    palette_indices = np.argmin(dists, axis=1)
    hw_indices = hardware_map[palette_indices]
    hw_indices = hw_indices.reshape(h, w)
    packed = (hw_indices[:, 0::2] << 4) | hw_indices[:, 1::2]
    return packed.tobytes()

def ensure_bin_file(base, orientation):
    bin_name = base + ('_l.bin' if orientation == 'landscape' else '_p.bin')
    bin_path = os.path.join(IMAGES_DIR, bin_name)
    if os.path.exists(bin_path):
        return bin_path
    
    bmp_name = base + ('_l.bmp' if orientation == 'landscape' else '_p.bmp')
    bmp_path = os.path.join(IMAGES_DIR, bmp_name)
    if not os.path.exists(bmp_path):
        return None
        
    try:
        img = Image.open(bmp_path).convert('RGB')
        img_array = np.array(img, dtype=np.uint8)
        bitstream = rgb_array_to_spectra6_bitstream_fast(img_array)
        with open(bin_path, 'wb') as f:
            f.write(bitstream)
        logger.info(f"Generated {bin_name} from {bmp_name}")
        return bin_path
    except Exception as e:
        logger.error(f"Error generating bin file {bin_name}: {e}", exc_info=True)
        return None

def convert_image(src_path, base):
    """Generates {base}_l.bmp (800×480) and {base}_p.bmp (rotated 800×480) in IMAGES_DIR, using custom crop offsets."""
    try:
        logger.info(f"Converting {src_path} → {base}_l.bmp / {base}_p.bmp")
        img = Image.open(src_path)
        img = ImageOps.exif_transpose(img).convert('RGB')
        w, h = img.size

        crops = load_crops()
        offsets = crops.get(base, {"l": 0.5, "p": 0.5})
        offset_l = offsets.get("l", 0.5)
        offset_p = offsets.get("p", 0.5)

        # Landscape: 5:3 → 800×480
        target_ratio_l = 5.0/3.0
        if w / h <= target_ratio_l:
            w_crop = w
            h_crop = int(w / target_ratio_l)
            x_crop = 0
            y_crop = int(offset_l * (h - h_crop))
        else:
            h_crop = h
            w_crop = int(h * target_ratio_l)
            y_crop = 0
            x_crop = int(0.5 * (w - w_crop))
        land = img.crop((x_crop, y_crop, x_crop + w_crop, y_crop + h_crop)).resize((800, 480), Image.Resampling.LANCZOS)
        Image.fromarray(dither_floyd_steinberg(np.array(land, dtype=np.float32), PALETTE))\
             .save(os.path.join(IMAGES_DIR, base + LANDSCAPE_SUFFIX), format='BMP')

        # Portrait: 3:5 → 480×800 → rotate 90°CW → stored as 800×480
        target_ratio_p = 3.0/5.0
        if w / h >= target_ratio_p:
            h_crop = h
            w_crop = int(h * target_ratio_p)
            y_crop = 0
            x_crop = int(offset_p * (w - w_crop))
        else:
            w_crop = w
            h_crop = int(w / target_ratio_p)
            x_crop = 0
            y_crop = int(0.5 * (h - h_crop))
        port = img.crop((x_crop, y_crop, x_crop + w_crop, y_crop + h_crop)).resize((480, 800), Image.Resampling.LANCZOS)
        port_img = Image.fromarray(dither_floyd_steinberg(np.array(port, dtype=np.float32), PALETTE))
        port_img.rotate(270, expand=True)\
                .save(os.path.join(IMAGES_DIR, base + PORTRAIT_SUFFIX), format='BMP')

        order = load_image_order()
        if base not in order: order.append(base); save_image_order(order)
        logger.info(f"  Saved: {base}_l.bmp, {base}_p.bmp")
        
        # Pre-generate bin files
        for suffix in ('_l.bin', '_p.bin'):
            bin_path = os.path.join(IMAGES_DIR, base + suffix)
            if os.path.exists(bin_path): os.remove(bin_path)
            
        ensure_bin_file(base, 'landscape')
        ensure_bin_file(base, 'portrait')
        
        return True
    except Exception as e:
        logger.error(f"Conversion error {src_path}: {e}", exc_info=True); return False

# ---------------------------------------------------------------------------
# FLIP scheduling
# ---------------------------------------------------------------------------

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
                chosen = random.choice(pool)
                used.add(chosen)
                assignments[mac] = chosen
    state['round_assignments'] = assignments

def _target_for_device(cfg, state, device_mac, device_idx, num_devices):
    devices = cfg.get('devices', []); sync_images = cfg.get('sync_images', False)
    dev_cfg = next((d for d in devices if d['mac'].lower() == device_mac.lower()), {})
    orientation = dev_cfg.get('orientation', 'landscape')
    suffix = orient_suffix(orientation)
    
    queued = state.get('queued_image')
    if queued:
        q_base = queued.get('base')
        q_source = queued.get('source', '')
        if (q_source == 'general' and dev_cfg.get('mode', 'group') == 'group') or \
           (q_source.lower() == device_mac.lower() and dev_cfg.get('mode', 'group') == 'individual'):
            return q_base + suffix
    
    if dev_cfg.get('mode', 'group') == 'individual':
        shuffle = dev_cfg.get('shuffle', False)
    else:
        shuffle = cfg.get('shuffle', False)
    
    if shuffle:
        if sync_images:
            base = state.get('round_assignments', {}).get('__sync__')
            return (base + suffix) if base else None
        else:
            base = state.get('round_assignments', {}).get(device_mac.lower())
            return (base + suffix) if base else None

    # Sequential:
    if dev_cfg.get('mode', 'group') == 'individual':
        dev_imgs = dev_cfg.get('images', [])
        active = [item['base'] for item in dev_imgs if item.get(orientation[0], True) and os.path.exists(os.path.join(IMAGES_DIR, item['base'] + suffix))]
    else:
        active = get_active_bases(orientation)
        
    if not active: return None
    n = len(active)
    idx = state.get('current_index', 0)
    if sync_images:
        return active[idx % n] + suffix
    return active[(idx + device_idx) % n] + suffix

# ---------------------------------------------------------------------------
# HTML Template
# ---------------------------------------------------------------------------

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
    <style>
        :root {
            --bg: #080d1a; 
            --bg-gradient: radial-gradient(ellipse at 20% 0%, #1a2545 0%, var(--bg) 60%);
            --card: rgba(16,24,48,0.65); 
            --border: rgba(255,255,255,0.07);
            --text: #f1f3f9; --muted: #8b95ae; --accent: #4f8ef7; --accent-h: #3371e0;
            --danger: #ef4444; --danger-h: #dc2626; --success: #22c55e; --warning: #f59e0b;
            --input-bg: rgba(0,0,0,0.4);
        }

        /* Light Theme */
        [data-theme="light"] {
            --bg: #f4f6fa;
            --bg-gradient: linear-gradient(135deg, #eef2f7 0%, #f4f6fa 100%);
            --card: #ffffff;
            --border: rgba(0, 0, 0, 0.08);
            --text: #1e293b;
            --muted: #64748b;
            --accent: #4f46e5;
            --accent-h: #4338ca;
            --input-bg: #ffffff;
        }

        /* Really Dark Theme */
        [data-theme="really-dark"] {
            --bg: #000000;
            --bg-gradient: #000000;
            --card: #0d0d0d;
            --border: #262626;
            --text: #ffffff;
            --muted: #a3a3a3;
            --accent: #a855f7;
            --accent-h: #9333ea;
            --input-bg: #000000;
        }

        *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
        body { font-family: 'Outfit', sans-serif;
            background: var(--bg-gradient);
            color: var(--text); min-height: 100vh; padding: 2rem 1.5rem; }
        .container { width: 100%; max-width: 1400px; margin: 0 auto; }
        header { display: flex; justify-content: space-between; align-items: center;
            margin-bottom: 2rem; padding-bottom: 1.5rem; border-bottom: 1px solid var(--border); }
        h1 { font-size: 2rem; font-weight: 700;
            background: linear-gradient(135deg, #60a5fa, #a78bfa);
            -webkit-background-clip: text; -webkit-text-fill-color: transparent; }
        .version-tag {
            font-size: 0.85rem;
            font-weight: 600;
            background: rgba(255, 255, 255, 0.08);
            border: 1px solid var(--border);
            color: var(--muted);
            padding: 0.15rem 0.5rem;
            border-radius: 6px;
            vertical-align: middle;
            margin-left: 0.6rem;
            -webkit-background-clip: initial;
            -webkit-text-fill-color: initial;
            display: inline-block;
        }
        .subtitle { color: var(--muted); font-size: 0.9rem; margin-top: 0.2rem; }
        .grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 1.5rem; margin-bottom: 1.5rem; }
        @media (max-width: 900px) { .grid-2 { grid-template-columns: 1fr; } }
        .card { background: var(--card); border: 1px solid var(--border);
            border-radius: 16px; padding: 1.5rem; backdrop-filter: blur(14px); }
        .card-title { font-size: 0.85rem; font-weight: 600; color: var(--muted);
            text-transform: uppercase; letter-spacing: 0.06em;
            margin-bottom: 1.2rem; display: flex; align-items: center; gap: 0.5rem; }
        label { font-weight: 500; color: var(--muted); font-size: 0.9rem; }
        input[type="number"], input[type="text"] {
            background: rgba(0,0,0,0.35); border: 1px solid var(--border);
            border-radius: 8px; padding: 0.5rem 0.75rem;
            color: var(--text); font-size: 0.95rem; outline: none;
            transition: border-color 0.2s; font-family: inherit; }
        input[type="number"]:focus, input[type="text"]:focus { border-color: var(--accent); }
        input[type="number"] { width: 100px; }
        .form-row { display: flex; align-items: center; gap: 0.75rem; flex-wrap: wrap; margin-bottom: 0.75rem; }
        /* Toggle */
        .toggle-wrap { display: flex; align-items: center; gap: 0.5rem; }
        .toggle { appearance: none; width: 38px; height: 22px;
            background: rgba(255,255,255,0.12); border-radius: 11px;
            position: relative; cursor: pointer; transition: background 0.25s;
            border: 1px solid var(--border); flex-shrink: 0; }
        .toggle::after { content: ''; position: absolute; width: 16px; height: 16px;
            border-radius: 50%; background: white; top: 2px; left: 2px; transition: transform 0.25s; }
        .toggle:checked { background: var(--accent); border-color: var(--accent); }
        .toggle:checked::after { transform: translateX(16px); }
        /* Buttons */
        .btn { background: var(--accent); color: white; border: none; border-radius: 8px;
            padding: 0.55rem 1.1rem; font-weight: 600; cursor: pointer; transition: all 0.2s;
            display: inline-flex; align-items: center; gap: 0.4rem;
            font-size: 0.9rem; font-family: inherit; white-space: nowrap; }
        .btn:hover { background: var(--accent-h); transform: translateY(-1px); }
        .btn-danger { background: var(--danger); }
        .btn-danger:hover { background: var(--danger-h); }
        .btn-ghost { background: rgba(255,255,255,0.07); color: var(--text); border: 1px solid var(--border); }
        .btn-ghost:hover { background: rgba(255,255,255,0.13); transform: translateY(-1px); }
        .btn-sm { padding: 0.35rem 0.7rem; font-size: 0.82rem; }
        .btn-warning { background: rgba(245,158,11,0.2); color: var(--warning); border: 1px solid rgba(245,158,11,0.3); }
        .btn-warning:hover { background: rgba(245,158,11,0.35); }
        /* Orient toggle pill */
        .orient-pill { display: inline-flex; border-radius: 8px; overflow: hidden;
            border: 1px solid var(--border); flex-shrink: 0; }
        .orient-pill button { background: rgba(0,0,0,0.25); border: none; cursor: pointer;
            padding: 0.3rem 0.6rem; color: var(--muted); font-size: 0.8rem; font-weight: 600;
            font-family: inherit; transition: all 0.2s; }
        .orient-pill button.active-l { background: rgba(79,142,247,0.3); color: var(--accent); }
        .orient-pill button.active-p { background: rgba(167,139,250,0.3); color: #a78bfa; }
        /* Mode Pill */
        .mode-pill { display: inline-flex; border-radius: 8px; overflow: hidden; border: 1px solid var(--border); flex-shrink: 0; }
        .mode-pill button { background: rgba(0,0,0,0.25); border: none; cursor: pointer; padding: 0.3rem 0.6rem; color: var(--muted); font-size: 0.8rem; font-weight: 600; font-family: inherit; transition: all 0.2s; }
        .mode-pill button:disabled { opacity: 0.4; cursor: not-allowed; }
        .mode-pill button.active-g { background: rgba(34,197,94,0.3); color: var(--success); }
        .mode-pill button.active-i { background: rgba(79,142,247,0.3); color: var(--accent); }
        /* Device list */
        .device-row { display: flex; align-items: center; gap: 0.75rem; padding: 0.65rem 0;
            border-bottom: 1px solid var(--border); flex-wrap: wrap; }
        .device-row:last-child { border-bottom: none; }
        .device-mac { font-family: monospace; font-size: 0.85rem; color: var(--muted); }
        .device-idx { color: var(--muted); font-size: 0.8rem; white-space: nowrap; }
        /* Drop zone */
        #drop-zone { border: 2px dashed rgba(255,255,255,0.13); border-radius: 16px;
            padding: 2.5rem 2rem; text-align: center; cursor: pointer;
            transition: all 0.3s; background: var(--card); backdrop-filter: blur(14px);
            margin-bottom: 2rem; }
        #drop-zone.drag-over { border-color: var(--accent); background: rgba(79,142,247,0.08); }
        #drop-zone .icon { font-size: 2.5rem; margin-bottom: 0.75rem; }
        #drop-zone p { color: var(--muted); font-size: 0.9rem; margin-top: 0.4rem; }
        #file-input { display: none; }
        /* Progress */
        #upload-progress { display: none; background: var(--card); border: 1px solid var(--border);
            border-radius: 12px; padding: 1rem 1.5rem; margin-bottom: 1.5rem; }
        .progress-bar-wrap { background: rgba(255,255,255,0.08); border-radius: 4px; height: 6px;
            margin-top: 0.5rem; overflow: hidden; }
        .progress-bar { height: 100%; background: var(--accent); border-radius: 4px; transition: width 0.3s; }
        /* Section header */
        .section-header { margin: 2rem 0 1rem; font-size: 1.3rem; font-weight: 600; color: var(--muted);
            display: flex; align-items: center; gap: 0.75rem; flex-wrap: wrap; }
        .badge { padding: 0.25rem 0.65rem; border-radius: 20px; font-size: 0.78rem; font-weight: 600; }
        .badge-blue { background: rgba(79,142,247,0.15); color: var(--accent); border: 1px solid rgba(79,142,247,0.25); }
        
        /* Tabs Bar */
        .tabs-bar {
            display: flex;
            gap: 0.5rem;
            border-bottom: 2px solid var(--border);
            margin: 2rem 0 1.5rem;
            overflow-x: auto;
            padding-bottom: 0.25rem;
        }
        .tab-btn {
            padding: 0.75rem 1.25rem;
            border-radius: 12px 12px 0 0;
            background: rgba(255,255,255,0.03);
            border: 1px solid var(--border);
            border-bottom: none;
            color: var(--muted);
            font-weight: 600;
            cursor: pointer;
            transition: all 0.25s cubic-bezier(0.4, 0, 0.2, 1);
            white-space: nowrap;
            position: relative;
            top: 2px;
        }
        .tab-btn:hover {
            background: rgba(255,255,255,0.08);
            color: var(--text);
        }
        .tab-btn.active {
            background: var(--card);
            border-color: var(--border);
            border-bottom: 2px solid var(--accent);
            color: var(--text);
            box-shadow: 0 -4px 12px rgba(0,0,0,0.2);
        }
        .tab-btn.drag-over {
            background: rgba(79,142,247,0.2);
            border-color: var(--accent);
        }

        /* Image cards */
        .image-list-container { margin-bottom: 3rem; }
        .image-card { background: var(--card); border: 1px solid var(--border); border-radius: 16px;
            overflow: hidden; backdrop-filter: blur(14px); margin-bottom: 1.25rem; }
        .image-card.dragging { opacity: 0.5; outline: 2px dashed var(--accent); }
        .image-card.drag-target { outline: 2px solid var(--accent); }
        .card-header { padding: 0.9rem 1.4rem; border-bottom: 1px solid var(--border);
            display: flex; align-items: center; gap: 0.75rem; flex-wrap: wrap; }
        .drag-handle { cursor: grab; color: var(--muted); font-size: 1.2rem; user-select: none; }
        .drag-handle:active { cursor: grabbing; }
        .btn-queue {
            background: rgba(255, 255, 255, 0.05);
            border: 1px solid var(--border);
            color: var(--muted);
            border-radius: 8px;
            padding: 0.35rem 0.75rem;
            font-size: 0.8rem;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.2s ease;
            display: inline-flex;
            align-items: center;
            gap: 0.25rem;
            user-select: none;
            margin-left: auto;
        }
        .btn-queue:hover {
            background: rgba(255, 255, 255, 0.1);
            color: var(--text);
            border-color: rgba(255,255,255,0.2);
        }
        .btn-queue.active {
            background: var(--accent);
            color: #fff;
            border-color: var(--accent);
            box-shadow: 0 0 10px rgba(79, 142, 247, 0.4);
        }
        /* Inline rename */
        .img-name-wrap { flex: 1; display: flex; align-items: center; gap: 0.5rem; min-width: 0; }
        .img-name { font-weight: 600; font-size: 1rem; word-break: break-all;
            cursor: pointer; border-bottom: 1px dashed transparent; transition: border-color 0.2s; }
        .img-name:hover { border-color: var(--muted); }
        .rename-input { font-weight: 600; font-size: 1rem; width: 100%; max-width: 300px;
            background: var(--input-bg); border: 1px solid var(--accent);
            border-radius: 6px; padding: 0.2rem 0.5rem; color: var(--text); font-family: inherit; }
        .rename-hint { font-size: 0.75rem; color: var(--muted); white-space: nowrap; }
        /* Theme Selector */
        .theme-selector-wrap { display: flex; align-items: center; background: rgba(255,255,255,0.03);
            border: 1px solid var(--border); border-radius: 12px; padding: 0.4rem 0.8rem; backdrop-filter: blur(10px); }
        .theme-selector-wrap select { background: transparent; border: none; color: var(--text);
            font-family: inherit; font-size: 0.85rem; font-weight: 600; outline: none; cursor: pointer; }
        .theme-selector-wrap select option { background: var(--bg); color: var(--text); }
        /* Triple preview */
        .triple-preview { display: grid; grid-template-columns: 1fr 1fr 1fr;
            border-bottom: 1px solid var(--border); }
        @media (max-width: 768px) { .triple-preview { grid-template-columns: 1fr; } }
        .preview-cell { padding: 1.2rem; display: flex; flex-direction: column;
            align-items: center; justify-content: flex-start;
            background: rgba(0,0,0,0.18); border-right: 1px solid var(--border); }
        .preview-cell:last-child { border-right: none; }
        .preview-label { font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.05em;
            color: var(--muted); margin-bottom: 0.5rem; font-weight: 600;
            display: flex; align-items: center; gap: 0.5rem; width: 100%; }
        .preview-img { max-width: 100%; max-height: 200px; border-radius: 6px;
            box-shadow: 0 8px 20px rgba(0,0,0,0.5); object-fit: contain; background: #111;
            margin-top: auto; }
        .no-preview { width: 100%; min-height: 80px; display: flex; align-items: center;
            justify-content: center; background: rgba(0,0,0,0.3); border-radius: 6px;
            color: var(--muted); font-size: 0.85rem; }
        .card-actions { padding: 0.8rem 1.4rem; display: flex; justify-content: flex-end;
            gap: 0.75rem; flex-wrap: wrap; background: rgba(0,0,0,0.08); }
            
        /* Crop Container & Overlay */
        .crop-container {
            position: relative;
            width: 100%;
            max-height: 200px;
            display: flex;
            align-items: center;
            justify-content: center;
            overflow: hidden;
            border-radius: 8px;
            box-shadow: 0 8px 24px rgba(0,0,0,0.5);
            background: #0b0f19;
            border: 1px solid var(--border);
            user-select: none;
        }
        .crop-bg-img {
            display: block;
            max-width: 100%;
            max-height: 200px;
            object-fit: contain;
            pointer-events: none;
            opacity: 0.85;
        }
        .crop-overlay-box {
            position: absolute;
            border: 2px dashed var(--accent);
            background: rgba(79, 142, 247, 0.22);
            box-sizing: border-box;
            cursor: grab;
            transition: box-shadow 0.2s, background-color 0.2s;
            box-shadow: 0 0 15px rgba(79, 142, 247, 0.4);
        }
        .crop-overlay-box:active {
            cursor: grabbing;
            background-color: rgba(79, 142, 247, 0.35);
            box-shadow: 0 0 25px rgba(79, 142, 247, 0.6);
        }
        .crop-overlay-box::before {
            content: '↕';
            position: absolute;
            top: 50%;
            left: 50%;
            transform: translate(-50%, -50%);
            font-size: 1.2rem;
            font-weight: bold;
            color: white;
            text-shadow: 0 0 4px black;
            pointer-events: none;
            opacity: 0.6;
        }
        .crop-container[data-orient="l"] .crop-overlay-box::before {
            content: '↕';
        }
        .crop-container[data-orient="p"] .crop-overlay-box::before {
            content: '↔';
        }

        /* Drag Over visual effects */
        .status-node.drag-over {
            border-color: var(--accent);
            background: rgba(79, 142, 247, 0.15);
            transform: scale(1.02);
            transition: all 0.2s;
        }

        /* Node status */
        .status-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr)); gap: 0.75rem; }
        .status-node { background: rgba(0,0,0,0.25); border: 1px solid var(--border);
            border-radius: 10px; padding: 0.75rem 1rem; transition: all 0.2s; }
        .status-node:hover { background: rgba(0,0,0,0.35); transform: translateY(-1px); }
        .status-node .node-mac { font-family: monospace; font-size: 0.8rem; color: var(--muted); }
        .status-node .node-status { font-size: 0.8rem; color: var(--muted); margin-top: 0.3rem; }
        .status-node.online { border-color: rgba(34,197,94,0.4); }
        .status-node.online .node-status { color: var(--success); }
        .status-node.debug-active { border-color: rgba(245,158,11,0.5); }
        .status-node.debug-active .node-status { color: var(--warning); }
        .status-node-img-wrap { margin-top: 0.5rem; border-radius: 6px; overflow: hidden; background: #0b0f19; display: flex; align-items: center; justify-content: center; height: 110px; border: 1px solid var(--border); }
        .status-node-img { width: 150px; height: 90px; object-fit: cover; border-radius: 4px; }
        .status-node-img.portrait-rot { width: 90px; height: 54px; transform: rotate(-90deg); object-fit: cover; }
        /* Toast */
        #toast { position: fixed; bottom: 2rem; right: 2rem; background: var(--card);
            border: 1px solid var(--border); border-radius: 12px; padding: 0.75rem 1.25rem;
            backdrop-filter: blur(14px); font-size: 0.9rem; opacity: 0;
            transition: opacity 0.3s; pointer-events: none; z-index: 999; }
        #toast.show { opacity: 1; }
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
            <span style="font-size:0.85rem; font-weight:600; color:var(--muted); margin-right:0.5rem; display:flex; align-items:center; gap:0.25rem;">🎨 Theme:</span>
            <select id="theme-select" onchange="changeTheme(this.value)">
                <option value="default">Default Dark</option>
                <option value="light">Light</option>
                <option value="really-dark">Really Dark</option>
            </select>
        </div>
    </header>

    <div class="grid-2">
        <div class="card">
            <div class="card-title">⚙️ Slideshow Settings</div>
            <form action="{{ url_for('update_config') }}" method="POST">
                <div class="form-row">
                    <label for="timer">Sleep interval (s):</label>
                    <input type="number" id="timer" name="timer" value="{{ config.timer }}" min="10">
                </div>
                <div class="form-row" style="margin-top:0.8rem;">
                    <span class="toggle-wrap">
                        <input type="checkbox" class="toggle" id="sync_images" name="sync_images"
                               {% if config.sync_images %}checked{% endif %} onchange="this.form.submit()">
                        <label for="sync_images">Same image on all grouped devices</label>
                    </span>
                </div>
                <div class="form-row" style="margin-top:0.8rem;">
                    <span class="toggle-wrap">
                        <input type="checkbox" class="toggle" id="shuffle" name="shuffle"
                               {% if config.shuffle %}checked{% endif %} onchange="this.form.submit()">
                        <label for="shuffle">Shuffle images in general pool</label>
                    </span>
                </div>
                <div style="margin-top:1rem;">
                    <button type="submit" class="btn">💾 Save Settings</button>
                </div>
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
            <div class="card-title">📡 Picframe Devices</div>
            <form action="{{ url_for('update_devices') }}" method="POST" id="device-form">
                <div id="device-list">
                {% for dev in config.devices %}
                <div class="device-row" data-idx="{{ loop.index0 }}">
                    <span class="device-idx">#{{ loop.index0 }}</span>
                    
                    <div class="device-name-wrap" style="flex: 1; min-width: 150px; display: flex; flex-direction: column;">
                        <span class="device-name" style="font-weight:600; cursor:pointer; border-bottom: 1px dashed transparent;" onclick="startDeviceRename(this, '{{ dev.mac }}')">{{ dev.name }}</span>
                        <span class="device-mac">{{ dev.mac }}</span>
                    </div>
                    
                    <div class="orient-pill" title="Toggle orientation">
                        <button type="button"
                                class="{% if dev.orientation == 'landscape' %}active-l{% endif %}"
                                onclick="setDeviceOrient('{{ dev.mac }}', 'landscape', this)">🌅 L</button>
                        <button type="button"
                                class="{% if dev.orientation == 'portrait' %}active-p{% endif %}"
                                onclick="setDeviceOrient('{{ dev.mac }}', 'portrait', this)">🤳 P</button>
                    </div>

                    <div class="mode-pill" title="Slideshow Mode: Group vs Individual" style="margin-left: 0.25rem;">
                        <button type="button" class="{% if dev.mode == 'group' %}active-g{% endif %}" onclick="setDeviceMode('{{ dev.mac }}', 'group', this)">👥 Group</button>
                        <button type="button" class="{% if dev.mode == 'individual' %}active-i{% endif %}" {% if not dev.images %}disabled title="No individual images assigned"{% endif %} onclick="setDeviceMode('{{ dev.mac }}', 'individual', this)">🖼️ Indiv</button>
                    </div>

                    <div class="toggle-wrap" style="margin-left: 0.5rem; margin-right: 0.5rem;" title="Enable safe REPL debug mode">
                        <input type="checkbox" class="toggle" id="dbg_dev_{{ loop.index0 }}"
                               {% if dev.debug %}checked{% endif %}
                               onchange="setDeviceDebug('{{ dev.mac }}', this.checked)">
                        <label for="dbg_dev_{{ loop.index0 }}" style="font-size:0.8rem; font-weight:600; color:var(--warning);">DEBUG</label>
                    </div>

                    <input type="hidden" name="mac_{{ loop.index0 }}" value="{{ dev.mac }}">
                    <input type="hidden" name="orient_{{ loop.index0 }}" value="{{ dev.orientation }}" class="orient-hidden">
                    <input type="hidden" name="debug_{{ loop.index0 }}" value="{{ '1' if dev.debug else '0' }}" class="debug-hidden">
                    <input type="hidden" name="shuffle_{{ loop.index0 }}" value="{{ '1' if dev.shuffle else '0' }}" class="shuffle-hidden">
                    <input type="hidden" name="flip_l_{{ loop.index0 }}" value="{{ '1' if dev.flip_l else '0' }}" class="flip-l-hidden">
                    <input type="hidden" name="flip_p_{{ loop.index0 }}" value="{{ '1' if dev.flip_p else '0' }}" class="flip-p-hidden">

                    <button type="button" class="btn btn-danger btn-sm"
                            onclick="removeDevice({{ loop.index0 }})">✕</button>
                </div>
                {% else %}
                <p style="color:var(--muted);font-size:0.9rem;">No devices configured yet. Connect a frame on network to auto-register it.</p>
                {% endfor %}
                </div>
                <input type="hidden" name="device_count" id="device_count" value="{{ config.devices|length }}">
                <div class="form-row" style="margin-top:1rem;">
                    <input type="text" id="new-mac" placeholder="aa:bb:cc:dd:ee:ff" style="flex:1;min-width:130px;">
                    <div class="orient-pill">
                        <button type="button" id="new-orient-l" class="active-l"
                                onclick="setNewOrient('landscape')">🌅 L</button>
                        <button type="button" id="new-orient-p"
                                onclick="setNewOrient('portrait')">🤳 P</button>
                    </div>
                    <input type="hidden" id="new-orient-val" value="landscape">
                    
                    <span class="toggle-wrap" style="margin-left:0.25rem;" title="Default Debug Mode for New Device">
                        <input type="checkbox" class="toggle" id="new-debug-val">
                        <label for="new-debug-val" style="font-size:0.8rem; font-weight:600; color:var(--warning);">DEBUG</label>
                    </span>

                    <button type="button" class="btn btn-ghost btn-sm" onclick="addDevice()">+ Add</button>
                </div>
                <button type="submit" class="btn" style="margin-top:0.75rem;">💾 Save Devices</button>
            </form>
        </div>
    </div>

    <div style="margin-bottom:1.5rem;">
        <div class="card-title" style="font-size:0.9rem;color:var(--muted);margin-bottom:0.75rem;">
            📶 Node Status &nbsp;
            <span style="font-size:0.8rem;font-weight:400;">Phase: <strong>{{ phase }}</strong></span>
        </div>
        <div class="status-grid">
        {% for dev in config.devices %}
            {% set ts = node_status.get(dev.mac.lower(), 0) %}
            {% set active_img = device_images.get(dev.mac.lower()) %}
            {% set ip_addr = device_ips.get(dev.mac.lower(), "DHCP") %}
            <div class="status-node {% if dev.debug %}debug-active{% elif ts > now_ts - 120 %}online{% endif %}"
                 style="cursor: pointer;" onclick="switchToDeviceTab('{{ dev.mac }}')">
                <div class="node-ip" style="font-weight:600;">{{ dev.name }}</div>
                <div class="node-mac">{{ dev.mac }}</div>
                <div style="font-size:0.75rem; color:var(--muted); font-family:monospace;">{{ ip_addr }}</div>
                <div class="node-status">
                    {% if dev.debug %}<span style="color:var(--warning); font-weight:600;">⚠️ REPL DEBUG LOCK</span>
                    {% elif ts > 0 %}Last seen {{ ((now_ts - ts)|int) }}s ago
                    {% else %}Never seen{% endif %}
                </div>
                {% if active_img %}
                {% set bmp_filename = active_img[:-4] + '.bmp' %}
                {% set is_portrait = active_img.endswith('_p.bin') %}
                <div class="status-node-img-wrap">
                    <img class="status-node-img {% if is_portrait %}portrait-rot{% endif %}"
                         src="{{ url_for('serve_image', filename=bmp_filename) }}?t={{ now_ts }}"
                         alt="On Frame">
                </div>
                {% endif %}
            </div>
        {% else %}
            <p style="color:var(--muted);font-size:0.85rem;">Add or connect devices above.</p>
        {% endfor %}
        </div>
    </div>

    <div id="drop-zone">
        <div class="icon">📥</div>
        <strong style="font-size:1.15rem;">Drag &amp; drop photos here, or click to browse</strong>
        <p>JPG, PNG, WebP, BMP · Multiple files OK ·
           Generates <code>name_l.bmp</code> and <code>name_p.bmp</code> automatically.</p>
        <form id="upload-form" action="{{ url_for('upload_file') }}" method="POST" enctype="multipart/form-data">
            <input type="file" id="file-input" name="files" accept=".png,.jpg,.jpeg,.webp,.bmp" multiple>
        </form>
    </div>
    <div id="upload-progress">
        <div id="progress-label">Uploading…</div>
        <div class="progress-bar-wrap"><div class="progress-bar" id="progress-bar" style="width:0%"></div></div>
    </div>

    <div class="section-header">
        🖼️ Images
        <span class="badge badge-blue">{{ images|length }}</span>
        <form action="{{ url_for('convert_all') }}" method="POST" style="margin-left:auto;">
            <button type="submit" class="btn btn-warning btn-sm">🔄 Reconvert All</button>
        </form>
        <span style="font-size:0.8rem;color:var(--muted);">Drag to reorder/assign · Click name to rename</span>
    </div>

    <!-- Tabs Bar -->
    <div class="tabs-bar">
        <div class="tab-btn active" data-tab="general">General Pool</div>
        {% for dev in config.devices %}
        <div class="tab-btn" data-tab="device-{{ dev.mac }}" data-mac="{{ dev.mac }}">{{ dev.name }}</div>
        {% endfor %}
    </div>

    <!-- General Tab Content -->
    <div class="tab-content" id="tab-content-general">
        <div id="image-list-general" class="image-list-container" data-source-tab="general">
        {% for img in images %}
        {% set is_queued = (state.queued_image and state.queued_image.base == img.base and state.queued_image.source == 'general') %}
        <div class="image-card" data-base="{{ img.base }}" draggable="true" data-source-tab="general">
            <div class="card-header">
                <span class="drag-handle" title="Drag to reorder">⣿</span>

                <div class="img-name-wrap">
                    <span class="img-name" title="Click to rename"
                          onclick="startRename(this, '{{ img.base }}')">{{ img.base }}</span>
                    <span class="rename-hint" style="display:none;">Enter to save · Esc to cancel</span>
                </div>

                <button type="button" class="btn-queue{% if is_queued %} active{% endif %}"
                        onclick="queueImage('{{ img.base }}', 'general')">
                    ⚡ {% if is_queued %}Queued{% else %}Queue{% endif %}
                </button>
            </div>

            <div class="triple-preview">
                <div class="preview-cell">
                    <div class="preview-label">Original</div>
                    <img class="preview-img"
                         src="{{ url_for('serve_original', filename=img.original_name) }}"
                         alt="Original">
                </div>

                <div class="preview-cell">
                    <div class="preview-label">
                        <span style="flex:1;">{{ img.base }}_l.bmp</span>
                        <span class="toggle-wrap">
                            <input type="checkbox" class="toggle" id="tog_l_{{ img.base }}"
                                   {% if img.l_on %}checked{% endif %}
                                   onchange="toggleOrient('{{ img.base }}', 'l', this.checked)">
                            <label for="tog_l_{{ img.base }}" style="font-size:0.75rem;">On</label>
                        </span>
                    </div>
                    <div class="crop-container" data-base="{{ img.base }}" data-orient="l" data-offset="{{ img.offset_l }}" data-w="{{ img.orig_w }}" data-h="{{ img.orig_h }}">
                        <img class="crop-bg-img" src="{{ url_for('serve_image', filename=img.base + '_dithered.png') }}">
                        <div class="crop-overlay-box"></div>
                    </div>
                </div>

                <div class="preview-cell">
                    <div class="preview-label">
                        <span style="flex:1;">{{ img.base }}_p.bmp</span>
                        <span class="toggle-wrap">
                            <input type="checkbox" class="toggle" id="tog_p_{{ img.base }}"
                                   {% if img.p_on %}checked{% endif %}
                                   onchange="toggleOrient('{{ img.base }}', 'p', this.checked)">
                            <label for="tog_p_{{ img.base }}" style="font-size:0.75rem;">On</label>
                        </span>
                    </div>
                    <div class="crop-container" data-base="{{ img.base }}" data-orient="p" data-offset="{{ img.offset_p }}" data-w="{{ img.orig_w }}" data-h="{{ img.orig_h }}">
                        <img class="crop-bg-img" src="{{ url_for('serve_image', filename=img.base + '_dithered.png') }}">
                        <div class="crop-overlay-box"></div>
                    </div>
                </div>
            </div>

            <div class="card-actions">
                <form action="{{ url_for('convert_file', filename=img.original_name) }}" method="POST" style="display:inline;">
                    <button type="submit" class="btn btn-ghost btn-sm">🔄 Re-Convert</button>
                </form>
                <form action="{{ url_for('delete_file', filename=img.original_name) }}" method="POST"
                      style="display:inline;" onsubmit="return confirm('Delete {{ img.base }}?');">
                    <button type="submit" class="btn btn-danger btn-sm">🗑 Delete</button>
                </form>
            </div>
        </div>
        {% else %}
        <div style="text-align:center;padding:3rem;color:var(--muted);background:var(--card);border-radius:16px;border:1px solid var(--border);">
            <div style="font-size:2rem;margin-bottom:1rem;">🖼️</div>
            <p>No images yet. Drop some photos above to get started.</p>
        </div>
        {% endfor %}
        </div>
    </div>

    <!-- Device Tabs Contents -->
    {% for dev in config.devices %}
    <div class="tab-content" id="tab-content-device-{{ dev.mac }}" style="display:none;">
        <!-- Device settings bar -->
        <div class="device-settings-bar" style="display: flex; gap: 1.25rem; align-items: center; padding: 1rem; background: var(--card); border: 1px solid var(--border); border-radius: 12px; margin-bottom: 1.5rem; flex-wrap: wrap;">
            <div style="font-weight: 600; color: var(--text);">Settings for {{ dev.name }}:</div>
            
            <!-- Mode selection -->
            <div class="mode-pill" title="Slideshow Mode: Group vs Individual">
                <button type="button" class="{% if dev.mode == 'group' %}active-g{% endif %}" onclick="setDeviceMode('{{ dev.mac }}', 'group', this)">👥 Group</button>
                <button type="button" class="{% if dev.mode == 'individual' %}active-i{% endif %}" {% if not dev.images %}disabled title="No individual images assigned"{% endif %} onclick="setDeviceMode('{{ dev.mac }}', 'individual', this)">🖼️ Indiv</button>
            </div>
            
            <!-- L/P Orientation toggle -->
            <div class="orient-pill" title="Device orientation">
                <button type="button"
                        class="{% if dev.orientation == 'landscape' %}active-l{% endif %}"
                        onclick="setDeviceOrient('{{ dev.mac }}', 'landscape', this)">🌅 L</button>
                <button type="button"
                        class="{% if dev.orientation == 'portrait' %}active-p{% endif %}"
                        onclick="setDeviceOrient('{{ dev.mac }}', 'portrait', this)">🤳 P</button>
            </div>

            <!-- Debug toggle -->
            <span class="toggle-wrap" title="Enable safe REPL debug mode">
                <input type="checkbox" class="toggle" id="dbg_tab_dev_{{ dev.mac }}"
                       {% if dev.debug %}checked{% endif %}
                       onchange="setDeviceDebug('{{ dev.mac }}', this.checked)">
                <label for="dbg_tab_dev_{{ dev.mac }}" style="font-size:0.85rem; font-weight:600; color:var(--warning);">DEBUG</label>
            </span>

            <!-- Shuffle images toggle -->
            <span class="toggle-wrap" title="Shuffle images for this device">
                <input type="checkbox" class="toggle" id="shuffle_tab_dev_{{ dev.mac }}"
                       {% if dev.shuffle %}checked{% endif %}
                       onchange="setDeviceShuffle('{{ dev.mac }}', this.checked)">
                <label for="shuffle_tab_dev_{{ dev.mac }}" style="font-size:0.85rem; font-weight:600;">🔀 Shuffle</label>
            </span>

            <!-- Flip Landscape toggle -->
            <span class="toggle-wrap" title="Flip Landscape image 180 degrees (upside down correction)">
                <input type="checkbox" class="toggle" id="flip_l_tab_dev_{{ dev.mac }}"
                       {% if dev.flip_l %}checked{% endif %}
                       onchange="setDeviceFlip('{{ dev.mac }}', 'l', this.checked)">
                <label for="flip_l_tab_dev_{{ dev.mac }}" style="font-size:0.85rem; font-weight:600;">🔄 Flip L</label>
            </span>

            <!-- Flip Portrait toggle -->
            <span class="toggle-wrap" title="Flip Portrait image 180 degrees (upside down correction)">
                <input type="checkbox" class="toggle" id="flip_p_tab_dev_{{ dev.mac }}"
                       {% if dev.flip_p %}checked{% endif %}
                       onchange="setDeviceFlip('{{ dev.mac }}', 'p', this.checked)">
                <label for="flip_p_tab_dev_{{ dev.mac }}" style="font-size:0.85rem; font-weight:600;">🔄 Flip P</label>
            </span>
        </div>

        <div id="image-list-device-{{ dev.mac }}" class="image-list-container" data-mac="{{ dev.mac }}" data-source-tab="device-{{ dev.mac }}">
        {% for dev_img in dev.images %}
            {% set img = image_by_base.get(dev_img.base) %}
            {% if img %}
            {% set is_queued = (state.queued_image and state.queued_image.base == img.base and state.queued_image.source == dev.mac) %}
            <div class="image-card" data-base="{{ img.base }}" draggable="true" data-source-tab="device-{{ dev.mac }}">
                <div class="card-header">
                    <span class="drag-handle" title="Drag to reorder">⣿</span>
                    <div class="img-name-wrap">
                        <span class="img-name">{{ img.base }}</span>
                    </div>
                    <button type="button" class="btn-queue{% if is_queued %} active{% endif %}"
                            onclick="queueImage('{{ img.base }}', '{{ dev.mac }}')">
                        ⚡ {% if is_queued %}Queued{% else %}Queue{% endif %}
                    </button>
                </div>

                <div class="triple-preview">
                    <div class="preview-cell">
                        <div class="preview-label">Original</div>
                        <img class="preview-img"
                             src="{{ url_for('serve_original', filename=img.original_name) }}"
                             alt="Original">
                    </div>

                    <div class="preview-cell">
                        <div class="preview-label">
                            <span style="flex:1;">Landscape</span>
                            <span class="toggle-wrap">
                                <input type="checkbox" class="toggle" id="tog_dev_l_{{ dev.mac }}_{{ img.base }}"
                                       {% if dev_img.l %}checked{% endif %}
                                       onchange="toggleDeviceImageOrient('{{ dev.mac }}', '{{ img.base }}', 'l', this.checked)">
                                <label for="tog_dev_l_{{ dev.mac }}_{{ img.base }}" style="font-size:0.75rem;">On</label>
                            </span>
                        </div>
                        <div class="crop-container" data-base="{{ img.base }}" data-orient="l" data-offset="{{ img.offset_l }}" data-w="{{ img.orig_w }}" data-h="{{ img.orig_h }}">
                            <img class="crop-bg-img" src="{{ url_for('serve_image', filename=img.base + '_dithered.png') }}">
                            <div class="crop-overlay-box"></div>
                        </div>
                    </div>

                    <div class="preview-cell">
                        <div class="preview-label">
                            <span style="flex:1;">Portrait</span>
                            <span class="toggle-wrap">
                                <input type="checkbox" class="toggle" id="tog_dev_p_{{ dev.mac }}_{{ img.base }}"
                                       {% if dev_img.p %}checked{% endif %}
                                       onchange="toggleDeviceImageOrient('{{ dev.mac }}', '{{ img.base }}', 'p', this.checked)">
                                <label for="tog_dev_p_{{ dev.mac }}_{{ img.base }}" style="font-size:0.75rem;">On</label>
                            </span>
                        </div>
                        <div class="crop-container" data-base="{{ img.base }}" data-orient="p" data-offset="{{ img.offset_p }}" data-w="{{ img.orig_w }}" data-h="{{ img.orig_h }}">
                            <img class="crop-bg-img" src="{{ url_for('serve_image', filename=img.base + '_dithered.png') }}">
                            <div class="crop-overlay-box"></div>
                        </div>
                    </div>
                </div>

                <div class="card-actions">
                    <button type="button" class="btn btn-danger btn-sm" onclick="removeDeviceImage('{{ dev.mac }}', '{{ img.base }}')">✕ Remove</button>
                </div>
            </div>
            {% endif %}
        {% else %}
        <div style="text-align:center;padding:3rem;color:var(--muted);background:var(--card);border-radius:16px;border:1px solid var(--border);">
            <div style="font-size:2rem;margin-bottom:1rem;">📥</div>
            <p>No individual images assigned to this device yet. Drag and drop images onto this tab or its node card to assign them.</p>
        </div>
        {% endfor %}
        </div>
    </div>
    {% endfor %}

</div>

<div id="toast"></div>

<script>
// ---- Toast ----
function toast(msg, ms=2500) {
    const t = document.getElementById('toast');
    t.textContent = msg; t.classList.add('show');
    setTimeout(() => t.classList.remove('show'), ms);
}

// ---- Upload ----
const dropZone    = document.getElementById('drop-zone');
const fileInput   = document.getElementById('file-input');
const progressEl  = document.getElementById('upload-progress');
const progressBar = document.getElementById('progress-bar');
const progressLbl = document.getElementById('progress-label');
dropZone.addEventListener('click', () => fileInput.click());
['dragenter','dragover','dragleave','drop'].forEach(e =>
    dropZone.addEventListener(e, ev => { ev.preventDefault(); ev.stopPropagation(); }));
['dragenter','dragover'].forEach(e => dropZone.addEventListener(e, () => dropZone.classList.add('drag-over')));
['dragleave','drop'].forEach(e => dropZone.addEventListener(e, () => dropZone.classList.remove('drag-over')));
dropZone.addEventListener('drop', ev => { if (ev.dataTransfer.files.length) uploadFiles(ev.dataTransfer.files); });
fileInput.addEventListener('change', () => { if (fileInput.files.length) uploadFiles(fileInput.files); });
function uploadFiles(files) {
    const fd = new FormData();
    for (const f of files) fd.append('files', f);
    progressEl.style.display = 'block';
    progressBar.style.width = '0%';
    progressLbl.textContent = `Uploading ${files.length} file(s)…`;
    const xhr = new XMLHttpRequest();
    xhr.open('POST', document.getElementById('upload-form').action);
    xhr.upload.addEventListener('progress', ev => {
        if (ev.lengthComputable) {
            const pct = Math.round(ev.loaded / ev.total * 100);
            progressBar.style.width = pct + '%';
            progressLbl.textContent = `Uploading… ${pct}%`;
        }
    });
    xhr.addEventListener('load', () => {
        progressLbl.textContent = 'Converting… (may take a moment)';
        progressBar.style.width = '100%';
        window.location.reload();
    });
    xhr.addEventListener('error', () => { progressLbl.textContent = 'Upload failed.'; });
    xhr.send(fd);
}

// ---- Per-orientation toggles ----
function toggleOrient(base, orient, enabled) {
    fetch('/toggle_orient', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({base, orient, enabled})
    }).then(r => r.json()).then(d => toast(d.ok ? `${base}_${orient}.bmp ${enabled?'on':'off'}` : 'Error'));
}

// ---- Device specific image L/P toggles ----
function toggleDeviceImageOrient(mac, base, orient, enabled) {
    fetch('/device_image_toggle_orient', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({mac, base, orient, enabled})
    }).then(r => r.json()).then(d => {
        if (d.ok) {
            toast(`${base}_${orient}.bmp ${enabled ? 'on' : 'off'} for ${mac}`);
        } else {
            toast('Error updating device image state');
        }
    });
}

// ---- Device specific image removal ----
function removeDeviceImage(mac, base) {
    if (!confirm(`Remove ${base} from device ${mac}?`)) return;
    fetch('/device_remove_image', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({mac, base})
    }).then(r => r.json()).then(d => {
        if (d.ok) {
            toast(`Removed ${base} from ${mac}`);
            window.location.reload();
        } else {
            toast('Error removing image');
        }
    });
}

// ---- Queue Image ----
function queueImage(base, source) {
    fetch('/api/queue', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({base, source})
    })
    .then(r => r.json())
    .then(d => {
        if (d.ok) {
            toast(d.action === 'queued' ? `Queued ${base} to show next` : `Removed ${base} from queue`);
            setTimeout(() => location.reload(), 500);
        } else {
            toast(`Failed to queue image: ${d.error}`);
        }
    });
}

// ---- Inline rename ----
function startRename(span, base) {
    const wrap = span.closest('.img-name-wrap');
    const hint = wrap.querySelector('.rename-hint');
    const input = document.createElement('input');
    let finished = false;
    input.type = 'text'; input.className = 'rename-input'; input.value = base;
    span.style.display = 'none'; hint.style.display = 'inline';
    wrap.insertBefore(input, hint);
    input.focus(); input.select();

    function commit() {
        if (finished) return;
        finished = true;
        const newBase = input.value.trim();
        if (!newBase || newBase === base) { cancel(); return; }
        fetch('/rename', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({old_base: base, new_base: newBase})
        }).then(r => r.json()).then(d => {
            if (d.ok) { toast(`Renamed to ${newBase}`); window.location.reload(); }
            else { toast(`Error: ${d.error || 'rename failed'}`); cancel(); }
        });
    }
    function cancel() {
        if (finished) return;
        finished = true;
        input.remove(); span.style.display = ''; hint.style.display = 'none';
    }
    input.addEventListener('keydown', ev => {
        if (ev.key === 'Enter') commit();
        if (ev.key === 'Escape') cancel();
    });
    input.addEventListener('blur', commit);
    input.addEventListener('mousedown', ev => ev.stopPropagation());
}

// ---- Device Inline Rename ----
function startDeviceRename(span, mac) {
    const wrap = span.closest('.device-name-wrap');
    const originalName = span.textContent;
    const input = document.createElement('input');
    let finished = false;
    input.type = 'text';
    input.className = 'rename-input';
    input.value = originalName;
    input.style.fontSize = '0.9rem';
    input.style.padding = '0.1rem 0.3rem';
    span.style.display = 'none';
    wrap.insertBefore(input, span);
    input.focus();
    input.select();
    
    function commit() {
        if (finished) return;
        finished = true;
        const newName = input.value.trim();
        if (!newName || newName === originalName) { cancel(); return; }
        fetch('/device_rename', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({mac, name: newName})
        }).then(r => r.json()).then(d => {
            if (d.ok) {
                toast(`Renamed device to ${newName}`);
                span.textContent = newName;
                cancel();
                window.location.reload();
            } else {
                toast(`Error: ${d.error || 'rename failed'}`);
                cancel();
            }
        });
    }
    function cancel() {
        if (finished) return;
        finished = true;
        input.remove();
        span.style.display = '';
    }
    input.addEventListener('keydown', ev => {
        if (ev.key === 'Enter') commit();
        if (ev.key === 'Escape') cancel();
    });
    input.addEventListener('blur', commit);
    input.addEventListener('mousedown', ev => ev.stopPropagation());
}

// ---- Theme Switcher ----
function applyTheme(theme) {
    document.documentElement.setAttribute('data-theme', theme);
    const select = document.getElementById('theme-select');
    if (select) select.value = theme;
}

function changeTheme(theme) {
    localStorage.setItem('picframes-theme', theme);
    applyTheme(theme);
}

// Apply on DOM load
window.addEventListener('DOMContentLoaded', () => {
    const savedTheme = localStorage.getItem('picframes-theme') || 'default';
    applyTheme(savedTheme);
});

// ---- Device orientation toggle ----
function setDeviceOrient(mac, orientation, btn) {
    fetch('/device_orientation', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({mac, orientation})
    }).then(r => r.json()).then(d => {
        if (d.ok) {
            toast(`${mac} → ${orientation}`);
            setTimeout(() => window.location.reload(), 600);
        }
    });
}

// ---- Device debug toggle ----
function setDeviceDebug(mac, isChecked) {
    fetch('/device_debug', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({mac, debug: isChecked})
    }).then(r => r.json()).then(d => {
        if (d.ok) {
            toast(`${mac} debug mode ${isChecked ? 'enabled' : 'disabled'}`);
            setTimeout(() => window.location.reload(), 600);
        } else {
            toast('Failed to change device debug state');
        }
    });
}

// ---- Device shuffle toggle ----
function setDeviceShuffle(mac, isChecked) {
    fetch('/device_shuffle', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({mac, shuffle: isChecked})
    }).then(r => r.json()).then(d => {
        if (d.ok) {
            toast(`Shuffle for ${mac} ${isChecked ? 'enabled' : 'disabled'}`);
            setTimeout(() => window.location.reload(), 600);
        } else {
            toast('Failed to change device shuffle state');
        }
    });
}

// ---- Device flip toggle ----
function setDeviceFlip(mac, orient, isChecked) {
    fetch('/device_flip', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({mac, orient, flip: isChecked})
    }).then(r => r.json()).then(d => {
        if (d.ok) {
            toast(`Flip ${orient === 'l' ? 'Landscape' : 'Portrait'} for ${mac} ${isChecked ? 'enabled' : 'disabled'}`);
            setTimeout(() => window.location.reload(), 600);
        } else {
            toast('Failed to change device flip state');
        }
    });
}

// ---- Device mode toggle ----
function setDeviceMode(mac, mode, btn) {
    fetch('/device_mode', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({mac, mode})
    }).then(r => r.json()).then(d => {
        if (d.ok) {
            toast(`Mode for ${mac} set to ${mode}`);
            setTimeout(() => window.location.reload(), 600);
        } else {
            toast(`Error: ${d.error || 'failed to set mode'}`);
        }
    });
}

// ---- New device orient toggle ----
let newOrientVal = 'landscape';
function setNewOrient(o) {
    newOrientVal = o;
    document.getElementById('new-orient-val').value = o;
    document.getElementById('new-orient-l').className = o === 'landscape' ? 'active-l' : '';
    document.getElementById('new-orient-p').className = o === 'portrait'  ? 'active-p' : '';
}

// ---- Device management ----
let deviceCount = {{ config.devices|length }};
const deviceData = {{ config.devices | tojson }};

function addDevice() {
    const mac    = document.getElementById('new-mac').value.trim();
    const orient = document.getElementById('new-orient-val').value;
    const dbg    = document.getElementById('new-debug-val').checked;
    if (!mac) return;
    const list = document.getElementById('device-list');
    const placeholder = list.querySelector('p'); if (placeholder) placeholder.remove();
    const row = document.createElement('div');
    row.className = 'device-row'; row.dataset.idx = deviceCount;
    row.innerHTML = `
        <span class="device-idx">#${deviceCount}</span>
        <div class="device-name-wrap" style="flex: 1; min-width: 150px; display: flex; flex-direction: column;">
            <span class="device-name" style="font-weight:600; cursor:pointer; border-bottom: 1px dashed transparent;" onclick="startDeviceRename(this, '${mac}')">${mac}</span>
            <span class="device-mac">${mac}</span>
        </div>
        <div class="orient-pill" title="Toggle orientation">
            <button type="button" class="${orient==='landscape'?'active-l':''}"
                    onclick="setDeviceOrient('${mac}','landscape',this)">🌅 L</button>
            <button type="button" class="${orient==='portrait'?'active-p':''}"
                    onclick="setDeviceOrient('${mac}','portrait',this)">🤳 P</button>
        </div>
        <div class="mode-pill" title="Slideshow Mode: Group vs Individual" style="margin-left: 0.25rem;">
            <button type="button" class="active-g" onclick="setDeviceMode('${mac}', 'group', this)">👥 Group</button>
            <button type="button" disabled title="No individual images assigned" onclick="setDeviceMode('${mac}', 'individual', this)">🖼️ Indiv</button>
        </div>
        <div class="toggle-wrap" style="margin-left: 0.5rem; margin-right: 0.5rem;">
            <input type="checkbox" class="toggle" id="dbg_dev_${deviceCount}" ${dbg ? 'checked' : ''}
                   onchange="setDeviceDebug('${mac}', this.checked)">
            <label for="dbg_dev_${deviceCount}" style="font-size:0.8rem; font-weight:600; color:var(--warning);">DEBUG</label>
        </div>
        <input type="hidden" name="mac_${deviceCount}" value="${mac}">
        <input type="hidden" name="orient_${deviceCount}" value="${orient}" class="orient-hidden">
        <input type="hidden" name="debug_${deviceCount}" value="${dbg ? '1' : '0'}" class="debug-hidden">
        <input type="hidden" name="shuffle_${deviceCount}" value="0" class="shuffle-hidden">
        <input type="hidden" name="flip_l_${deviceCount}" value="0" class="flip-l-hidden">
        <input type="hidden" name="flip_p_${deviceCount}" value="0" class="flip-p-hidden">
        <button type="button" class="btn btn-danger btn-sm" onclick="removeDevice(${deviceCount})">✕</button>`;
    list.appendChild(row);
    deviceCount++;
    document.getElementById('device_count').value = deviceCount;
    document.getElementById('new-mac').value = '';
    document.getElementById('new-debug-val').checked = false;
}

function removeDevice(idx) {
    document.querySelector(`.device-row[data-idx="${idx}"]`)?.remove();
    document.querySelectorAll('.device-row').forEach((r, i) => {
        r.dataset.idx = i;
        r.querySelector('.device-idx').textContent = `#${i}`;
        const macInput = r.querySelector('input[name^="mac_"]');
        if (macInput) macInput.name = `mac_${i}`;
        const orientInput = r.querySelector('.orient-hidden');
        if (orientInput) orientInput.name = `orient_${i}`;
        const debugInput = r.querySelector('.debug-hidden');
        if (debugInput) debugInput.name = `debug_${i}`;
        const shuffleInput = r.querySelector('.shuffle-hidden');
        if (shuffleInput) shuffleInput.name = `shuffle_${i}`;
        const flipLInput = r.querySelector('.flip-l-hidden');
        if (flipLInput) flipLInput.name = `flip_l_${i}`;
        const flipPInput = r.querySelector('.flip-p-hidden');
        if (flipPInput) flipPInput.name = `flip_p_${i}`;
    });
    deviceCount = document.querySelectorAll('.device-row').length;
    document.getElementById('device_count').value = deviceCount;
}

// ---- Drag-and-Drop & Tabs Logic ----
let dragSrc = null;

function initDragEvents(card) {
    card.addEventListener('dragstart', ev => {
        dragSrc = card;
        card.classList.add('dragging');
        ev.dataTransfer.effectAllowed = 'move';
        ev.dataTransfer.setData('text/plain', card.dataset.base);
        ev.dataTransfer.setData('source-tab', card.dataset.sourceTab);
    });
    
    card.addEventListener('dragend', () => {
        card.classList.remove('dragging');
        document.querySelectorAll('.image-card').forEach(c => c.classList.remove('drag-target'));
        document.querySelectorAll('.tab-btn').forEach(t => t.classList.remove('drag-over'));
        document.querySelectorAll('.status-node').forEach(n => n.classList.remove('drag-over'));
    });
    
    card.addEventListener('dragover', ev => {
        ev.preventDefault();
        ev.dataTransfer.dropEffect = 'move';
        if (card !== dragSrc && card.dataset.sourceTab === dragSrc?.dataset.sourceTab) {
            document.querySelectorAll('.image-card').forEach(c => c.classList.remove('drag-target'));
            card.classList.add('drag-target');
        }
    });
    
    card.addEventListener('drop', ev => {
        ev.preventDefault();
        if (dragSrc && dragSrc !== card && card.dataset.sourceTab === dragSrc.dataset.sourceTab) {
            const container = card.closest('.image-list-container');
            const cards = [...container.querySelectorAll('.image-card')];
            if (cards.indexOf(dragSrc) < cards.indexOf(card)) card.after(dragSrc);
            else card.before(dragSrc);
            
            const sourceTab = card.dataset.sourceTab;
            if (sourceTab === 'general') {
                saveOrder();
            } else {
                const mac = container.dataset.mac;
                saveDeviceOrder(mac);
            }
        }
    });
}

document.querySelectorAll('.image-card').forEach(initDragEvents);

// Tab buttons dragover / drop
document.querySelectorAll('.tab-btn').forEach(tab => {
    tab.addEventListener('dragover', ev => {
        ev.preventDefault();
        ev.dataTransfer.dropEffect = 'move';
        if (dragSrc && dragSrc.dataset.sourceTab !== tab.dataset.tab) {
            tab.classList.add('drag-over');
        }
    });
    
    tab.addEventListener('dragleave', () => {
        tab.classList.remove('drag-over');
    });
    
    tab.addEventListener('drop', ev => {
        ev.preventDefault();
        tab.classList.remove('drag-over');
        if (!dragSrc) return;
        
        const base = dragSrc.dataset.base;
        const sourceTab = dragSrc.dataset.sourceTab;
        const targetTab = tab.dataset.tab;
        
        if (sourceTab === targetTab) return;
        handleMoveImage(base, sourceTab, targetTab);
    });
});

// Device status nodes dragover / drop
document.querySelectorAll('.status-node').forEach(node => {
    const onclickStr = node.getAttribute('onclick');
    const macMatch = onclickStr.match(/'([^']+)'/);
    if (!macMatch) return;
    const mac = macMatch[1];
    
    node.addEventListener('dragover', ev => {
        ev.preventDefault();
        ev.dataTransfer.dropEffect = 'move';
        if (dragSrc && dragSrc.dataset.sourceTab !== `device-${mac}`) {
            node.classList.add('drag-over');
        }
    });
    
    node.addEventListener('dragleave', () => {
        node.classList.remove('drag-over');
    });
    
    node.addEventListener('drop', ev => {
        ev.preventDefault();
        node.classList.remove('drag-over');
        if (!dragSrc) return;
        
        const base = dragSrc.dataset.base;
        const sourceTab = dragSrc.dataset.sourceTab;
        const targetTab = `device-${mac}`;
        
        if (sourceTab === targetTab) return;
        handleMoveImage(base, sourceTab, targetTab);
    });
});

function handleMoveImage(base, sourceTab, targetTab) {
    if (sourceTab === 'general' && targetTab.startsWith('device-')) {
        const mac = targetTab.replace('device-', '');
        fetch('/device_assign_image', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({mac, base})
        }).then(r => r.json()).then(d => {
            if (d.ok) {
                toast(`Assigned ${base} to device ${mac}`);
                localStorage.setItem('active_tab', targetTab);
                window.location.reload();
            } else {
                toast(`Error: ${d.error || 'assignment failed'}`);
            }
        });
    } else if (sourceTab.startsWith('device-') && targetTab === 'general') {
        const mac = sourceTab.replace('device-', '');
        fetch('/device_remove_image', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({mac, base})
        }).then(r => r.json()).then(d => {
            if (d.ok) {
                toast(`Removed ${base} from device ${mac}`);
                localStorage.setItem('active_tab', 'general');
                window.location.reload();
            } else {
                toast(`Error: ${d.error || 'removal failed'}`);
            }
        });
    } else if (sourceTab.startsWith('device-') && targetTab.startsWith('device-')) {
        const from_mac = sourceTab.replace('device-', '');
        const to_mac = targetTab.replace('device-', '');
        fetch('/device_move_image', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({from_mac, to_mac, base})
        }).then(r => r.json()).then(d => {
            if (d.ok) {
                toast(`Moved ${base} to device ${to_mac}`);
                localStorage.setItem('active_tab', targetTab);
                window.location.reload();
            } else {
                toast(`Error: ${d.error || 'move failed'}`);
            }
        });
    }
}

function saveOrder() {
    const bases = [...document.getElementById('image-list-general').querySelectorAll('.image-card')].map(c => c.dataset.base);
    fetch('/reorder', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({order:bases})});
}

function saveDeviceOrder(mac) {
    const listContainer = document.getElementById(`image-list-device-${mac}`);
    const bases = [...listContainer.querySelectorAll('.image-card')].map(c => c.dataset.base);
    fetch('/device_reorder_images', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({mac, order: bases})
    }).then(r => r.json()).then(d => {
        if (d.ok) {
            toast('Device image order saved');
        }
    });
}

// Tab Switching
function switchTab(tabId) {
    document.querySelectorAll('.tab-btn').forEach(btn => {
        if (btn.dataset.tab === tabId) btn.classList.add('active');
        else btn.classList.remove('active');
    });
    document.querySelectorAll('.tab-content').forEach(content => {
        if (content.id === `tab-content-${tabId}`) content.style.display = 'block';
        else content.style.display = 'none';
    });
    localStorage.setItem('active_tab', tabId);
}

document.querySelectorAll('.tab-btn').forEach(btn => {
    btn.addEventListener('click', () => {
        switchTab(btn.dataset.tab);
    });
});

function switchToDeviceTab(mac) {
    const tabId = `device-${mac}`;
    const btn = document.querySelector(`.tab-btn[data-tab="${tabId}"]`);
    if (btn) {
        switchTab(tabId);
        document.querySelector('.tabs-bar').scrollIntoView({ behavior: 'smooth' });
    } else {
        toast(`Device ${mac} has no tab`);
    }
}

// Restore active tab on load
window.addEventListener('DOMContentLoaded', () => {
    const activeTab = localStorage.getItem('active_tab') || 'general';
    if (document.querySelector(`.tab-btn[data-tab="${activeTab}"]`)) {
        switchTab(activeTab);
    } else {
        switchTab('general');
    }
});

// ---- Drag-to-Crop Overlay Physics ----
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
        if (r <= targetR) {
            widthPct = 100;
            heightPct = (r / targetR) * 100;
            leftPct = 0;
            topPct = offset * (100 - heightPct);
        } else {
            heightPct = 100;
            widthPct = (targetR / r) * 100;
            topPct = 0;
            leftPct = 50 - (widthPct / 2);
        }
    } else {
        const targetR = 3 / 5;
        if (r >= targetR) {
            heightPct = 100;
            widthPct = (targetR / r) * 100;
            topPct = 0;
            leftPct = offset * (100 - widthPct);
        } else {
            widthPct = 100;
            heightPct = (r / targetR) * 100;
            leftPct = 0;
            topPct = 50 - (heightPct / 2);
        }
    }
    
    box.style.width = widthPct + '%';
    box.style.height = heightPct + '%';
    box.style.left = leftPct + '%';
    box.style.top = topPct + '%';
}

document.querySelectorAll('.crop-container').forEach(container => {
    updateCropOverlay(container);
    
    const box = container.querySelector('.crop-overlay-box');
    let isDragging = false;
    let startY = 0;
    let startX = 0;
    let startOffset = 0;
    
    box.addEventListener('mousedown', e => {
        e.preventDefault();
        e.stopPropagation();
        isDragging = true;
        startY = e.clientY;
        startX = e.clientX;
        startOffset = parseFloat(container.dataset.offset);
        box.style.cursor = 'grabbing';
    });
    
    window.addEventListener('mousemove', e => {
        if (!isDragging) return;
        
        const containerRect = container.getBoundingClientRect();
        const orient = container.dataset.orient;
        const w = parseFloat(container.dataset.w);
        const h = parseFloat(container.dataset.h);
        const r = w / h;
        
        let deltaOffset = 0;
        
        if (orient === 'l') {
            const targetR = 5 / 3;
            if (r <= targetR) {
                const heightPct = (r / targetR) * 100;
                const maxDragPx = containerRect.height * (1 - heightPct / 100);
                if (maxDragPx > 0) {
                    const deltaY = e.clientY - startY;
                    deltaOffset = deltaY / maxDragPx;
                }
            }
        } else {
            const targetR = 3 / 5;
            if (r >= targetR) {
                const widthPct = (targetR / r) * 100;
                const maxDragPx = containerRect.width * (1 - widthPct / 100);
                if (maxDragPx > 0) {
                    const deltaX = e.clientX - startX;
                    deltaOffset = deltaX / maxDragPx;
                }
            }
        }
        
        let newOffset = Math.max(0, Math.min(1, startOffset + deltaOffset));
        container.dataset.offset = newOffset;
        updateCropOverlay(container);
    });
    
    window.addEventListener('mouseup', () => {
        if (!isDragging) return;
        isDragging = false;
        box.style.cursor = 'grab';
        
        const base = container.dataset.base;
        const orient = container.dataset.orient;
        const offset = parseFloat(container.dataset.offset);
        
        fetch('/recrop', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({base, orient, offset})
        }).then(r => r.json()).then(d => {
            if (d.ok) {
                toast(`Recropped ${base}_${orient}`);
                document.querySelectorAll('.status-node-img').forEach(img => {
                    const url = new URL(img.src);
                    url.searchParams.set('t', Date.now());
                    img.src = url.toString();
                });
                document.querySelectorAll(`.crop-container[data-base="${base}"][data-orient="${orient}"]`).forEach(other => {
                    other.dataset.offset = offset;
                    updateCropOverlay(other);
                });
            } else {
                toast(`Error recropping: ${d.error}`);
            }
        });
    });

    // Touch support for dragging
    box.addEventListener('touchstart', e => {
        if (e.touches.length !== 1) return;
        e.preventDefault();
        e.stopPropagation();
        isDragging = true;
        startY = e.touches[0].clientY;
        startX = e.touches[0].clientX;
        startOffset = parseFloat(container.dataset.offset);
    });
    window.addEventListener('touchmove', e => {
        if (!isDragging || e.touches.length !== 1) return;
        const containerRect = container.getBoundingClientRect();
        const orient = container.dataset.orient;
        const w = parseFloat(container.dataset.w);
        const h = parseFloat(container.dataset.h);
        const r = w / h;
        
        let deltaOffset = 0;
        if (orient === 'l') {
            const targetR = 5 / 3;
            if (r <= targetR) {
                const heightPct = (r / targetR) * 100;
                const maxDragPx = containerRect.height * (1 - heightPct / 100);
                if (maxDragPx > 0) {
                    const deltaY = e.touches[0].clientY - startY;
                    deltaOffset = deltaY / maxDragPx;
                }
            }
        } else {
            const targetR = 3 / 5;
            if (r >= targetR) {
                const widthPct = (targetR / r) * 100;
                const maxDragPx = containerRect.width * (1 - widthPct / 100);
                if (maxDragPx > 0) {
                    const deltaX = e.touches[0].clientX - startX;
                    deltaOffset = deltaX / maxDragPx;
                }
            }
        }
        let newOffset = Math.max(0, Math.min(1, startOffset + deltaOffset));
        container.dataset.offset = newOffset;
        updateCropOverlay(container);
    });
    window.addEventListener('touchend', () => {
        if (!isDragging) return;
        isDragging = false;
        const base = container.dataset.base;
        const orient = container.dataset.orient;
        const offset = parseFloat(container.dataset.offset);
        
        fetch('/recrop', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({base, orient, offset})
        }).then(r => r.json()).then(d => {
            if (d.ok) {
                toast(`Recropped ${base}_${orient}`);
                document.querySelectorAll('.status-node-img').forEach(img => {
                    const url = new URL(img.src);
                    url.searchParams.set('t', Date.now());
                    img.src = url.toString();
                });
                document.querySelectorAll(`.crop-container[data-base="${base}"][data-orient="${orient}"]`).forEach(other => {
                    other.dataset.offset = offset;
                    updateCropOverlay(other);
                });
            } else {
                toast(`Error recropping: ${d.error}`);
            }
        });
    });
});
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
            b, _ = os.path.splitext(f)
            if b == base: original_name = f; break
        if original_name is None: continue

        has_l = os.path.exists(os.path.join(IMAGES_DIR, base + LANDSCAPE_SUFFIX))
        has_p = os.path.exists(os.path.join(IMAGES_DIR, base + PORTRAIT_SUFFIX))
        flags = _flags(enabled, base)

        ensure_dithered_original(base)

        # Get original image dimensions for aspect ratio overlay calculations
        try:
            with Image.open(os.path.join(ORIGINALS_DIR, original_name)) as img_obj:
                orig_w, orig_h = img_obj.size
        except Exception:
            orig_w, orig_h = 800, 480

        crop_offsets = crops.get(base, {"l": 0.5, "p": 0.5})

        images.append({
            'base': base, 'original_name': original_name,
            'has_l': has_l, 'has_p': has_p,
            'l_on': flags["l"], 'p_on': flags["p"],
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
        _, ext = os.path.splitext(f)
        if ext.lower() in ALLOWED_EXTENSIONS:
            base = os.path.splitext(f)[0]
            convert_image(os.path.join(ORIGINALS_DIR, f), base)
            count += 1
    logger.info(f"convert_all: {count} images processed")
    return redirect(url_for('index'))


@app.route('/delete/<filename>', methods=['POST'])
def delete_file(filename):
    base, _ = os.path.splitext(filename)
    
    cfg = load_config()
    assigned_to_any_device = False
    for dev in cfg.get('devices', []):
        for img in dev.get('images', []):
            if img.get('base') == base:
                assigned_to_any_device = True
                break
        if assigned_to_any_device:
            break
            
    if assigned_to_any_device:
        # Only delete it from the general list (do not touch files, crops, enabled, or device lists)
        order = load_image_order()
        if base in order:
            order.remove(base)
            save_image_order(order)
        logger.info(f"Image '{base}' is assigned to device-specific lists. Removed only from general list.")
    else:
        # Delete completely
        for path in [
            os.path.join(ORIGINALS_DIR, filename),
            os.path.join(IMAGES_DIR, base + LANDSCAPE_SUFFIX),
            os.path.join(IMAGES_DIR, base + PORTRAIT_SUFFIX),
            os.path.join(IMAGES_DIR, base + '_dithered.png'),
            os.path.join(IMAGES_DIR, base + '_l.bin'),
            os.path.join(IMAGES_DIR, base + '_p.bin'),
        ]:
            if os.path.exists(path):
                try:
                    os.remove(path)
                except Exception as e:
                    logger.error(f"Error removing {path}: {e}")

        order = load_image_order()
        if base in order:
            order.remove(base)
            save_image_order(order)
            
        enabled = load_enabled()
        enabled.pop(base, None)
        save_enabled(enabled)
        
        crops = load_crops()
        crops.pop(base, None)
        save_crops(crops)

        # Clean up from devices configuration just in case
        changed = False
        for dev in cfg.get('devices', []):
            old_len = len(dev.get('images', []))
            dev['images'] = [img for img in dev.get('images', []) if img['base'] != base]
            if len(dev['images']) != old_len:
                changed = True
                if not dev['images']:
                    dev['mode'] = 'group'
        if changed:
            save_config(cfg)
            
        logger.info(f"Image '{base}' is not assigned to any device-specific lists. Deleted completely.")

    trigger_redownload()
    return redirect(url_for('index'))


@app.route('/rename', methods=['POST'])
def rename_image():
    data     = request.get_json()
    old_base = data.get('old_base', '').strip()
    new_base = data.get('new_base', '').strip()

    if not old_base or not new_base:
        return jsonify({'ok': False, 'error': 'Missing name'}), 400
    if old_base == new_base:
        return jsonify({'ok': True})
    if not re.match(r'^[\w\-]+$', new_base):
        return jsonify({'ok': False, 'error': 'Use letters, numbers, - or _'}), 400

    if any(os.path.splitext(f)[0] == new_base for f in os.listdir(ORIGINALS_DIR)):
        return jsonify({'ok': False, 'error': 'Name already in use'}), 409

    # Rename original
    for f in os.listdir(ORIGINALS_DIR):
        base, ext = os.path.splitext(f)
        if base == old_base:
            os.rename(os.path.join(ORIGINALS_DIR, f),
                      os.path.join(ORIGINALS_DIR, new_base + ext))
            break

    # Rename BMPs, BINs and dithered original PNG
    for old_f, new_f in [
        (old_base + LANDSCAPE_SUFFIX, new_base + LANDSCAPE_SUFFIX),
        (old_base + PORTRAIT_SUFFIX,  new_base + PORTRAIT_SUFFIX),
        (old_base + '_l.bin',         new_base + '_l.bin'),
        (old_base + '_p.bin',         new_base + '_p.bin'),
        (old_base + '_dithered.png',  new_base + '_dithered.png'),
    ]:
        old_p = os.path.join(IMAGES_DIR, old_f)
        new_p = os.path.join(IMAGES_DIR, new_f)
        if os.path.exists(old_p): os.rename(old_p, new_p)

    # Update order
    order = load_image_order()
    if old_base in order: order[order.index(old_base)] = new_base; save_image_order(order)

    # Update enabled
    enabled = load_enabled()
    if old_base in enabled: enabled[new_base] = enabled.pop(old_base); save_enabled(enabled)

    # Update crops database
    crops = load_crops()
    if old_base in crops: crops[new_base] = crops.pop(old_base); save_crops(crops)

    # Update devices list assignments
    cfg = load_config()
    changed = False
    for dev in cfg.get('devices', []):
        for img in dev.get('images', []):
            if img['base'] == old_base:
                img['base'] = new_base
                changed = True
    if changed:
        save_config(cfg)

    trigger_redownload()
    logger.info(f"Renamed {old_base} → {new_base}")
    return jsonify({'ok': True, 'new_base': new_base})


@app.route('/toggle_orient', methods=['POST'])
def toggle_orient():
    data   = request.get_json()
    base   = data.get('base')
    orient = data.get('orient')   # 'l' or 'p'
    val    = bool(data.get('enabled', True))
    if not base or orient not in ('l', 'p'):
        return jsonify({'ok': False}), 400
    enabled = load_enabled()
    flags = _flags(enabled, base)
    flags[orient] = val
    enabled[base] = flags
    save_enabled(enabled)
    trigger_redownload()
    return jsonify({'ok': True})


@app.route('/reorder', methods=['POST'])
def reorder():
    order = request.get_json().get('order', [])
    save_image_order(order)
    trigger_redownload()
    return jsonify({'ok': True})


@app.route('/recrop', methods=['POST'])
def recrop():
    data = request.get_json()
    base = data.get('base')
    orient = data.get('orient') # 'l' or 'p'
    offset = data.get('offset') # float between 0.0 and 1.0
    if not base or orient not in ('l', 'p') or offset is None:
        return jsonify({'ok': False, 'error': 'Invalid parameters'}), 400

    try:
        offset = float(offset)
        offset = max(0.0, min(1.0, offset))
    except ValueError:
        return jsonify({'ok': False, 'error': 'Invalid offset'}), 400

    crops = load_crops()
    if base not in crops:
        crops[base] = {"l": 0.5, "p": 0.5}
    crops[base][orient] = offset
    save_crops(crops)

    # Regenerate
    original_name = None
    for f in os.listdir(ORIGINALS_DIR):
        b, _ = os.path.splitext(f)
        if b == base: original_name = f; break

    if not original_name:
        return jsonify({'ok': False, 'error': 'Original not found'}), 404

    src_path = os.path.join(ORIGINALS_DIR, original_name)
    success = convert_image(src_path, base)
    if success:
        trigger_redownload()
        return jsonify({'ok': True})
    else:
        return jsonify({'ok': False, 'error': 'Re-conversion failed'}), 500


@app.route('/config', methods=['POST'])
def update_config():
    cfg = load_config()
    try:
        cfg['timer']       = int(request.form.get('timer', cfg['timer']))
        cfg['shuffle']     = 'shuffle' in request.form
        cfg['sync_images'] = 'sync_images' in request.form
    except Exception as e: logger.error(f"Config parse: {e}")
    count = int(request.form.get('device_count', 0))
    devices = []
    old_devices_map = {d['mac'].lower(): d for d in cfg.get('devices', []) if d.get('mac')}
    for i in range(count):
        mac = request.form.get(f'device_mac_{i}', '').strip()
        orient = request.form.get(f'device_orient_{i}', 'landscape')
        dbg = request.form.get(f'device_debug_{i}', '0') == '1'
        if mac:
            old = old_devices_map.get(mac.lower(), {})
            devices.append({
                'mac': mac, 'orientation': orient, 'debug': dbg,
                'name': old.get('name', mac),
                'mode': old.get('mode', 'group'),
                'shuffle': request.form.get(f'device_shuffle_{i}', '0') == '1',
                'flip_l': request.form.get(f'device_flip_l_{i}', '0') == '1',
                'flip_p': request.form.get(f'device_flip_p_{i}', '0') == '1',
                'images': old.get('images', [])
            })
    if devices: cfg['devices'] = devices
    save_config(cfg)
    trigger_redownload()
    return redirect(url_for('index'))


@app.route('/devices', methods=['POST'])
def update_devices():
    cfg = load_config()
    count = int(request.form.get('device_count', 0))
    devices = []
    old_devices_map = {d['mac'].lower(): d for d in cfg.get('devices', []) if d.get('mac')}
    for i in range(count):
        mac = request.form.get(f'mac_{i}', '').strip()
        orient = request.form.get(f'orient_{i}', 'landscape')
        dbg = request.form.get(f'debug_{i}', '0') == '1'
        if mac:
            old = old_devices_map.get(mac.lower(), {})
            devices.append({
                'mac': mac, 'orientation': orient, 'debug': dbg,
                'name': old.get('name', mac),
                'mode': old.get('mode', 'group'),
                'shuffle': request.form.get(f'shuffle_{i}', '0') == '1',
                'flip_l': request.form.get(f'flip_l_{i}', '0') == '1',
                'flip_p': request.form.get(f'flip_p_{i}', '0') == '1',
                'images': old.get('images', [])
            })
    cfg['devices'] = devices; save_config(cfg)
    trigger_redownload()
    return redirect(url_for('index'))


@app.route('/device_orientation', methods=['POST'])
def device_orientation():
    data = request.get_json()
    mac = data.get('mac'); orientation = data.get('orientation')
    if not mac or orientation not in ('landscape', 'portrait'):
        return jsonify({'ok': False}), 400
    cfg = load_config()
    for dev in cfg.get('devices', []):
        if dev['mac'].lower() == mac.lower():
            dev['orientation'] = orientation
            trigger_redownload(mac)
            break
    save_config(cfg)
    return jsonify({'ok': True, 'orientation': orientation})


@app.route('/api/queue', methods=['POST'])
def queue_image_api():
    data = request.get_json() or {}
    base = data.get('base')
    source = data.get('source')
    
    if not base or not source:
        return jsonify({'ok': False, 'error': 'Missing base or source'}), 400
        
    with _state_lock:
        state = load_state()
        current_queued = state.get('queued_image')
        
        if current_queued and current_queued.get('base') == base and current_queued.get('source') == source:
            state['queued_image'] = None
            action = 'dequeued'
        else:
            state['queued_image'] = {
                'base': base,
                'source': source
            }
            action = 'queued'
            
        _reset_round(state)
        
        state.setdefault('redownload', {})
        if source == 'general':
            cfg = load_config()
            for dev in cfg.get('devices', []):
                if dev.get('mode', 'group') == 'group' and dev.get('mac'):
                    state['redownload'][dev['mac'].lower()] = True
        else:
            state['redownload'][source.lower()] = True
            
        save_state(state)
        
    logger.info(f"Image {base} has been {action} from source {source}")
    return jsonify({'ok': True, 'action': action, 'queued_image': state['queued_image']})


@app.route('/device_shuffle', methods=['POST'])
def device_shuffle():
    data = request.get_json() or {}
    mac = data.get('mac')
    shuffle_val = bool(data.get('shuffle', False))
    if not mac:
        return jsonify({'ok': False, 'error': 'mac parameter required'}), 400
    cfg = load_config()
    device_found = False
    for dev in cfg.get('devices', []):
        if dev['mac'].lower() == mac.lower():
            dev['shuffle'] = shuffle_val
            device_found = True
            trigger_redownload(mac)
            break
    if not device_found:
        return jsonify({'ok': False, 'error': 'device not found'}), 404
    save_config(cfg)
    logger.info(f"Target device {mac} shuffle updated to: {shuffle_val}")
    return jsonify({'ok': True, 'shuffle': shuffle_val})


@app.route('/device_flip', methods=['POST'])
def device_flip():
    data = request.get_json() or {}
    mac = data.get('mac')
    orient_type = data.get('orient') # 'l' or 'p'
    flip_val = bool(data.get('flip', False))
    if not mac or orient_type not in ('l', 'p'):
        return jsonify({'ok': False, 'error': 'mac and orient parameters required'}), 400
    cfg = load_config()
    device_found = False
    for dev in cfg.get('devices', []):
        if dev['mac'].lower() == mac.lower():
            if orient_type == 'l':
                dev['flip_l'] = flip_val
            else:
                dev['flip_p'] = flip_val
            device_found = True
            trigger_redownload(mac)
            break
    if not device_found:
        return jsonify({'ok': False, 'error': 'device not found'}), 404
    save_config(cfg)
    logger.info(f"Target device {mac} flip {orient_type} updated to: {flip_val}")
    return jsonify({'ok': True, 'flip': flip_val})


@app.route('/device_debug', methods=['GET', 'POST'])
def device_debug():
    if request.method == 'POST':
        if request.is_json:
            data = request.get_json() or {}
            mac = data.get('mac')
            dbg_val = bool(data.get('debug', False))
        else:
            mac = request.form.get('mac')
            dbg_val = request.form.get('debug') in ('1', 'true', 'True', True)
    else: # GET
        mac = request.args.get('mac')
        dbg_val = request.args.get('debug') in ('1', 'true', 'True', True)

    if not mac:
        return jsonify({'ok': False, 'error': 'mac parameter required'}), 400

    cfg = load_config()
    device_found = False
    for dev in cfg.get('devices', []):
        if dev['mac'].lower() == mac.lower():
            dev['debug'] = dbg_val
            device_found = True
            break

    if not device_found:
        return jsonify({'ok': False, 'error': 'device not found'}), 404

    save_config(cfg)
    logger.info(f"Target device {mac} debug switch updated to: {dbg_val}")
    return jsonify({'ok': True, 'debug': dbg_val})


@app.route('/device_rename', methods=['POST'])
def device_rename():
    data = request.get_json()
    mac = data.get('mac')
    new_name = data.get('name', '').strip()
    if not mac or not new_name:
        return jsonify({'ok': False, 'error': 'Missing MAC or name'}), 400
    cfg = load_config()
    for dev in cfg.get('devices', []):
        if dev['mac'].lower() == mac.lower():
            dev['name'] = new_name
            break
    save_config(cfg)
    return jsonify({'ok': True})


@app.route('/device_mode', methods=['POST'])
def device_mode():
    data = request.get_json()
    mac = data.get('mac')
    mode = data.get('mode')
    if not mac or mode not in ('group', 'individual'):
        return jsonify({'ok': False}), 400
    cfg = load_config()
    for dev in cfg.get('devices', []):
        if dev['mac'].lower() == mac.lower():
            if mode == 'individual' and not dev.get('images', []):
                return jsonify({'ok': False, 'error': 'No individual images assigned'}), 400
            dev['mode'] = mode
            trigger_redownload(mac)
            break
    save_config(cfg)
    return jsonify({'ok': True, 'mode': mode})


@app.route('/device_assign_image', methods=['POST'])
def device_assign_image():
    data = request.get_json()
    mac = data.get('mac')
    base = data.get('base')
    if not mac or not base:
        return jsonify({'ok': False, 'error': 'Invalid parameters'}), 400
    
    cfg = load_config()
    enabled = load_enabled()
    flags = _flags(enabled, base)
    
    dev = next((d for d in cfg.get('devices', []) if d['mac'].lower() == mac.lower()), None)
    if not dev:
        return jsonify({'ok': False, 'error': 'Device not found'}), 404
    
    dev_imgs = dev.setdefault('images', [])
    if not any(img['base'] == base for img in dev_imgs):
        dev_imgs.append({
            'base': base,
            'l': flags['l'],
            'p': flags['p']
        })
        
    # Disable globally in general pool
    flags['l'] = False
    flags['p'] = False
    enabled[base] = flags
    
    save_enabled(enabled)
    save_config(cfg)
    trigger_redownload(mac)
    return jsonify({'ok': True})


@app.route('/device_remove_image', methods=['POST'])
def device_remove_image():
    data = request.get_json()
    mac = data.get('mac')
    base = data.get('base')
    if not mac or not base:
        return jsonify({'ok': False, 'error': 'Invalid parameters'}), 400
    
    cfg = load_config()
    for dev in cfg.get('devices', []):
        if dev['mac'].lower() == mac.lower():
            dev['images'] = [img for img in dev.get('images', []) if img['base'] != base]
            if not dev['images']:
                dev['mode'] = 'group'
            trigger_redownload(mac)
            break
    save_config(cfg)
    return jsonify({'ok': True})


@app.route('/device_move_image', methods=['POST'])
def device_move_image():
    data = request.get_json()
    from_mac = data.get('from_mac')
    to_mac = data.get('to_mac')
    base = data.get('base')
    if not from_mac or not to_mac or not base:
        return jsonify({'ok': False, 'error': 'Invalid parameters'}), 400
    
    cfg = load_config()
    from_dev = next((d for d in cfg.get('devices', []) if d['mac'].lower() == from_mac.lower()), None)
    to_dev = next((d for d in cfg.get('devices', []) if d['mac'].lower() == to_mac.lower()), None)
    
    if not from_dev or not to_dev:
        return jsonify({'ok': False, 'error': 'Device not found'}), 404
        
    target_img = next((img for img in from_dev.get('images', []) if img['base'] == base), None)
    if not target_img:
        return jsonify({'ok': False, 'error': 'Image not found on source device'}), 404
        
    from_dev['images'] = [img for img in from_dev['images'] if img['base'] != base]
    if not from_dev['images']:
        from_dev['mode'] = 'group'
        
    to_dev_imgs = to_dev.setdefault('images', [])
    if not any(img['base'] == base for img in to_dev_imgs):
        to_dev_imgs.append({
            'base': base,
            'l': target_img.get('l', True),
            'p': target_img.get('p', True)
        })
        
    save_config(cfg)
    trigger_redownload(from_mac)
    trigger_redownload(to_mac)
    return jsonify({'ok': True})


@app.route('/device_reorder_images', methods=['POST'])
def device_reorder_images():
    data = request.get_json()
    mac = data.get('mac')
    order = data.get('order', [])
    if not mac:
        return jsonify({'ok': False, 'error': 'Invalid parameters'}), 400
    cfg = load_config()
    for dev in cfg.get('devices', []):
        if dev['mac'].lower() == mac.lower():
            img_map = {img['base']: img for img in dev.get('images', [])}
            dev['images'] = [img_map[b] for b in order if b in img_map]
            trigger_redownload(mac)
            break
    save_config(cfg)
    return jsonify({'ok': True})


@app.route('/device_image_toggle_orient', methods=['POST'])
def device_image_toggle_orient():
    data = request.get_json()
    mac = data.get('mac')
    base = data.get('base')
    orient = data.get('orient')
    enabled = bool(data.get('enabled'))
    if not mac or not base or orient not in ('l', 'p'):
        return jsonify({'ok': False, 'error': 'Invalid parameters'}), 400
    cfg = load_config()
    for dev in cfg.get('devices', []):
        if dev['mac'].lower() == mac.lower():
            for img in dev.get('images', []):
                if img['base'] == base:
                    img[orient] = enabled
                    trigger_redownload(mac)
                    break
            break
    save_config(cfg)
    return jsonify({'ok': True})


# ---------------------------------------------------------------------------
# Static serving
# ---------------------------------------------------------------------------

@app.route('/serve/originals/<path:filename>')
def serve_original(filename): return send_from_directory(ORIGINALS_DIR, filename)

@app.route('/serve/images/<path:filename>')
def serve_image(filename): return send_from_directory(IMAGES_DIR, filename)

@app.route('/images/<path:filename>')
def download_image(filename):
    if not filename.lower().endswith('.bmp'): return "Invalid format", 400
    return send_from_directory(IMAGES_DIR, filename)


# ---------------------------------------------------------------------------
# Device API
# ---------------------------------------------------------------------------

@app.route('/api/config', methods=['GET'])
def api_config():
    cfg = load_config(); now = datetime.now()
    cfg['current_date'] = now.strftime('%Y-%m-%d'); cfg['timestamp'] = int(now.timestamp())
    return jsonify(cfg)


@app.route('/api/images', methods=['GET'])
def api_images():
    cfg = load_config(); caller_ip = _caller_ip()
    all_files = get_unified_index()
    
    mac = request.args.get('mac', '').strip().lower()
    dev_cfg = None
    if mac:
        dev_cfg = next((d for d in cfg.get('devices', []) if d['mac'].lower() == mac), None)
    if not dev_cfg:
        dev_cfg = next((d for d in cfg.get('devices', []) if d['ip'] == caller_ip or d['mac'].lower() == caller_ip.lower()), None)

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


@app.route('/api/update', methods=['GET'])
def api_update():
    from flask import Response
    import io
    if not os.path.exists(FIRMWARE_DIR):
        logger.error(f"Firmware directory {FIRMWARE_DIR} not found")
        return "Firmware directory not found", 404
        
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode='w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for f in os.listdir(FIRMWARE_DIR):
            if f.endswith('.py'):
                file_path = os.path.join(FIRMWARE_DIR, f)
                zf.write(file_path, arcname=f)
                
    size = buf.tell()
    buf.seek(0)
    logger.info(f"Serving firmware update ZIP: {size} bytes")
    return Response(buf, mimetype='application/zip',
                    headers={'Content-Disposition': 'attachment; filename="update.zip"',
                             'Content-Length': str(size)})


@app.route('/api/daily-zip', methods=['GET'])
def api_daily_zip():
    from flask import Response
    cfg = load_config(); caller_ip = _caller_ip()
    devices = cfg.get('devices', [])
    
    mac = request.args.get('mac', '').strip().lower()
    dev_cfg = None
    if mac:
        dev_cfg = next((d for d in devices if d['mac'].lower() == mac), None)
    if not dev_cfg:
        dev_cfg = next((d for d in devices if d.get('ip') == caller_ip or d.get('mac', '').lower() == caller_ip.lower()), None)
        
    if not dev_cfg:
        mac_addr = mac or caller_ip.lower()
        new_dev = {'mac': mac_addr, 'name': mac_addr, 'orientation': 'portrait', 'debug': False, 'mode': 'group', 'images': []}
        cfg.setdefault('devices', []).append(new_dev); save_config(cfg)
        dev_cfg = new_dev
        logger.info(f"daily-zip Auto-registered {mac_addr}")

    mac = dev_cfg.get('mac').lower()
    
    with _state_lock:
        state = load_state()
        state.setdefault('redownload', {})
        state['redownload'][mac] = False
        save_state(state)

    orientation = dev_cfg.get('orientation', 'portrait')
    
    if dev_cfg.get('mode', 'group') == 'individual':
        dev_imgs = dev_cfg.get('images', [])
        orient_char = 'l' if orientation == 'landscape' else 'p'
        active_bases = [item['base'] for item in dev_imgs if item.get(orient_char, True)]
    else:
        active_bases = get_active_bases(orientation)
        
    bin_suffix = '_l.bin' if orientation == 'landscape' else '_p.bin'
    candidates = [b + bin_suffix for b in active_bases]
    
    queued = state.get('queued_image')
    if queued:
        q_base = queued.get('base')
        q_source = queued.get('source', '')
        if (q_source == 'general' and dev_cfg.get('mode', 'group') == 'group') or \
           (q_source.lower() == mac and dev_cfg.get('mode', 'group') == 'individual'):
            q_filename = q_base + bin_suffix
            if q_filename not in candidates:
                candidates.append(q_filename)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode='w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        zf.writestr('config.json', json.dumps(cfg, indent=2))
        manifest_data = json.dumps(candidates, indent=2)
        zf.writestr('index.json', manifest_data)
        zf.writestr('list.json', manifest_data)
        
        for bin_filename in candidates:
            base = bin_filename[:-6]
            bin_path = ensure_bin_file(base, orientation)
            if bin_path and os.path.exists(bin_path):
                is_landscape = (orientation == 'landscape')
                should_flip = (is_landscape and dev_cfg.get('flip_l', False)) or (not is_landscape and dev_cfg.get('flip_p', False))
                if should_flip:
                    try:
                        with open(bin_path, 'rb') as f:
                            data = f.read()
                        # Swap low/high nibbles and reverse bytes to flip 180 degrees
                        flipped_data = bytes([ ((b & 0x0F) << 4) | ((b & 0xF0) >> 4) for b in reversed(data) ])
                        zf.writestr(bin_filename, flipped_data)
                    except Exception as e:
                        logger.error(f"Failed to flip bin file {bin_filename}: {e}")
                        zf.write(bin_path, arcname=bin_filename)
                else:
                    zf.write(bin_path, arcname=bin_filename)
            else:
                logger.warning(f"daily-zip: failed to package {bin_filename}")

    size = buf.tell(); buf.seek(0)
    logger.info(f"daily-zip for {mac}: {len(candidates)} files, {size:,} bytes")
    return Response(buf, mimetype='application/zip',
                    headers={'Content-Disposition': 'attachment; filename="daily.zip"',
                             'Content-Length': str(size)})


# ---------------------------------------------------------------------------
# Wakeup — 3-phase protocol
# ---------------------------------------------------------------------------

def _caller_ip():
    if request.headers.get('X-Forwarded-For'):
        return request.headers['X-Forwarded-For'].split(',')[0].strip()
    return request.remote_addr


def _advance_index(cfg, state, num_devices):
    state['last_change_ts'] = int(time.time())
    any_sequential = any(
        not (d.get('shuffle', False) if d.get('mode', 'group') == 'individual' else cfg.get('shuffle', False))
        for d in cfg.get('devices', [])
    )
    if any_sequential or not cfg.get('devices'):
        if cfg.get('sync_images', False):
            pool = get_active_bases(None)
            if pool: state['current_index'] = (state['current_index'] + 1) % len(pool)
        else:
            state['current_index'] = state['current_index'] + num_devices


def _reset_round(state):
    state['phase'] = PHASE_GATHERING
    state['phase_checkins'] = {}; state['phase_ready_ack'] = {}; state['phase_change_ack'] = {}
    state['round_assignments'] = {}
    state['queued_image'] = None


@app.route('/api/wakeup', methods=['POST'])
def api_wakeup():
    from flask import Response
    cfg     = load_config()
    devices = cfg.get('devices', [])

    data = request.get_json() or {}
    mac = data.get('mac', '').strip().lower()
    if not mac:
        mac = _caller_ip().lower()

    known_macs = [d['mac'].lower() for d in devices if d.get('mac')]

    if mac not in known_macs:
        # Check if there is an existing matching IP
        ip = _caller_ip()
        ip_device = next((d for d in devices if d.get('name') == ip and not d.get('mac')), None)
        if ip_device:
            ip_device['mac'] = mac
            if ip_device['name'] == ip:
                ip_device['name'] = mac
            logger.info(f"Associated MAC {mac} with existing device {ip}")
        else:
            new_dev = {
                'mac': mac,
                'name': mac,
                'orientation': 'portrait',
                'debug': False,
                'mode': 'group',
                'images': []
            }
            devices.append(new_dev)
            logger.info(f"Auto-registered new MAC {mac}")
        
        cfg['devices'] = devices
        save_config(cfg)
        known_macs = [d['mac'].lower() for d in devices]

    dev_cfg = next((d for d in devices if d['mac'].lower() == mac), None)
    if not dev_cfg:
        return Response("WAIT - None", mimetype='text/plain'), 200
    device_idx = devices.index(dev_cfg)
    num_devices = len(devices)

    with _state_lock:
        state  = load_state()
        now_ts = int(time.time())
        state.setdefault('last_seen', {})
        state.setdefault('device_ips', {})
        state.setdefault('device_images', {})
        state.setdefault('redownload', {})

        state['last_seen'][mac] = now_ts
        state['device_ips'][mac] = _caller_ip()

        # Check firmware version update requirement for MicroPython clients
        is_micropython = (
            'device_id' in data or 
            'mac' in data or 
            request.args.get('mac') or 
            request.headers.get('User-Agent', '').lower().startswith('micropython')
        )
        if is_micropython:
            version = data.get('version', '').strip() or request.args.get('version', '').strip()
            latest_version = get_latest_firmware_version()
            is_older = False
            if not version:
                is_older = True
            else:
                def parse_version(v_str):
                    try:
                        return [int(x) for x in v_str.split('.')]
                    except Exception:
                        return [0, 0, 0]
                if parse_version(version) < parse_version(latest_version):
                    is_older = True
            
            if is_older:
                save_state(state)
                logger.info(f"Device {mac} version '{version}' is older than latest '{latest_version}'. Responding with UPDATE.")
                return Response("UPDATE", mimetype='text/plain'), 200

        # ---- CRITICAL INTERCEPT: Server Debug Mode Override ----
        if dev_cfg.get('debug', False):
            save_state(state)
            logger.info(f"[INTERCEPT] Responding with DEBUG to client: {mac}")
            return Response("DEBUG", mimetype='text/plain'), 200

        phase  = state.get('phase', PHASE_GATHERING)

        state.setdefault('round_assignments', {})
        any_shuffle = any(
            (d.get('shuffle', False) if d.get('mode', 'group') == 'individual' else cfg.get('shuffle', False))
            for d in devices
        )
        if any_shuffle and not state['round_assignments']:
            _build_shuffle_assignments(cfg, state)

        target_file = _target_for_device(cfg, state, mac, device_idx, num_devices)
        if target_file and target_file.endswith('.bmp'):
            target_file = target_file[:-4] + '.bin'

        if target_file:
            base = target_file[:-6]
            ensure_bin_file(base, dev_cfg.get('orientation', 'landscape'))

        redownload_suffix = ""
        if state['redownload'].get(mac, False):
            redownload_suffix = " - REDOWNLOAD"

        # ---- Phase 1: GATHERING ----
        if phase == PHASE_GATHERING:
            state['phase_checkins'][mac] = now_ts
            all_in = set(known_macs) <= set(state['phase_checkins'].keys())
            if not all_in:
                if not state.get('last_change_ts'):
                    state['last_change_ts'] = now_ts
                timer_val = cfg.get('timer', 900)
                remaining = (state['last_change_ts'] + timer_val) - now_ts
                save_state(state)
                logger.info(f"GATHERING wait {mac} — target: {target_file}, remaining: {remaining}")
                if remaining > 10:
                    return Response(f"WAIT - {target_file} - {remaining}{redownload_suffix}", mimetype='text/plain'), 200
                else:
                    return Response(f"WAIT - {target_file}{redownload_suffix}", mimetype='text/plain'), 200

            state['phase'] = PHASE_READY
            state['phase_ready_ack'] = {}
            logger.info(f"All in → READY. Assignments: {state['round_assignments']}")
            phase = PHASE_READY

        # ---- Phase 2: READY ----
        if phase == PHASE_READY:
            state['phase_ready_ack'][mac] = now_ts
            all_acked = set(known_macs) <= set(state['phase_ready_ack'].keys())
            if not all_acked:
                save_state(state)
                logger.info(f"READY wait {mac} — target: {target_file}")
                return Response(f"READY - {target_file}{redownload_suffix}", mimetype='text/plain'), 200

            state['phase'] = PHASE_CHANGE
            state['phase_change_ack'] = {}
            logger.info("All READY → CHANGE")
            phase = PHASE_CHANGE

        # ---- Phase 3: CHANGE ----
        if phase == PHASE_CHANGE:
            state['phase_change_ack'][mac] = now_ts
            state['device_images'][mac] = target_file
            
            all_changed = set(known_macs) <= set(state['phase_change_ack'].keys())
            
            if all_changed:
                _advance_index(cfg, state, num_devices)
                sync_due = (now_ts - state.get('last_sync_ts', 0)) >= 86400
                if sync_due: state['last_sync_ts'] = now_ts
                _reset_round(state)
            
            save_state(state)
            logger.info(f"CHANGE → {mac}: {target_file}")
            return Response(f"CHANGE - {target_file}{redownload_suffix}", mimetype='text/plain'), 200

        save_state(state)
        return Response("WAIT - None", mimetype='text/plain'), 200


@app.route('/api/wakeup/reset', methods=['POST'])
def api_reset_state():
    state = load_state(); _reset_round(state); save_state(state)
    return jsonify({"ok": True, "phase": state['phase']})


@app.route('/api/wakeup/next', methods=['POST'])
def api_next_image():
    with _state_lock:
        state = load_state()
        _reset_round(state)
        save_state(state)
    return jsonify({"ok": True, "current_index": state.get('current_index', 0), "phase": state['phase']})


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8000))
    zeroconf_instance = start_mdns_broadcast(port)
    try:
        app.run(host='0.0.0.0', port=port, debug=False)
    finally:
        if zeroconf_instance:
            logger.info("Stopping mDNS broadcast...")
            zeroconf_instance.close()
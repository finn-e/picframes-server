import os
import json
import logging
import threading
import sqlite3

logger = logging.getLogger(__name__)

SHARE_DIR     = os.environ.get('SHARE_DIR', '/share')
CONFIG_DIR    = os.environ.get('CONFIG_DIR', '/config')
ORIGINALS_DIR = os.path.join(SHARE_DIR, 'originals')
IMAGES_DIR    = os.path.join(SHARE_DIR, 'images')

ALLOWED_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.webp', '.bmp'}
LANDSCAPE_SUFFIX   = '_l.bmp'
PORTRAIT_SUFFIX    = '_p.bmp'

for _d in (SHARE_DIR, ORIGINALS_DIR, IMAGES_DIR, CONFIG_DIR):
    os.makedirs(_d, exist_ok=True)

DB_PATH = os.path.join(CONFIG_DIR, 'picframes.db')

_legacy = os.path.join(SHARE_DIR, 'picframes.db')
if not os.path.exists(DB_PATH) and os.path.exists(_legacy):
    try:
        import shutil
        shutil.copy2(_legacy, DB_PATH)
        logger.info(f"Copied legacy database from {_legacy} to {DB_PATH}")
    except Exception as e:
        logger.error(f"Failed to copy legacy database: {e}")

state_lock = threading.Lock()


def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=10.0)
    conn.row_factory = sqlite3.Row
    return conn


def _col(conn, table, col, definition):
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {definition}")
    except sqlite3.OperationalError:
        pass


def init_db():
    conn = get_db()
    c = conn.cursor()

    c.execute("""CREATE TABLE IF NOT EXISTS global_settings (
        key TEXT PRIMARY KEY, value TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS devices (
        mac TEXT PRIMARY KEY, name TEXT, orientation TEXT, debug INTEGER,
        mode TEXT, shuffle INTEGER DEFAULT 0,
        flip_l INTEGER DEFAULT 0, flip_p INTEGER DEFAULT 0, images_json TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS crops (
        base TEXT PRIMARY KEY, l REAL, p REAL)""")
    c.execute("""CREATE TABLE IF NOT EXISTS enabled (
        base TEXT PRIMARY KEY, l INTEGER, p INTEGER)""")
    c.execute("""CREATE TABLE IF NOT EXISTS image_order (
        base TEXT PRIMARY KEY, sort_order INTEGER)""")

    # Playlist tables
    c.execute("""CREATE TABLE IF NOT EXISTS playlists (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        name           TEXT NOT NULL,
        shuffle        INTEGER DEFAULT 0,
        sync           INTEGER DEFAULT 1,
        sleep_interval INTEGER DEFAULT 900)""")
    c.execute("""CREATE TABLE IF NOT EXISTS playlist_images (
        playlist_id INTEGER NOT NULL,
        base        TEXT NOT NULL,
        sort_order  INTEGER DEFAULT 0,
        PRIMARY KEY (playlist_id, base),
        FOREIGN KEY (playlist_id) REFERENCES playlists(id) ON DELETE CASCADE)""")
    c.execute("""CREATE TABLE IF NOT EXISTS playlist_devices (
        mac         TEXT PRIMARY KEY,
        playlist_id INTEGER,
        FOREIGN KEY (playlist_id) REFERENCES playlists(id) ON DELETE SET NULL)""")

    for col, defn in [
        ('shuffle',  'INTEGER DEFAULT 0'),
        ('flip_l',   'INTEGER DEFAULT 0'),
        ('flip_p',   'INTEGER DEFAULT 0'),
    ]:
        _col(conn, 'devices', col, defn)
    for col, defn in [
        ('title',        "INTEGER DEFAULT 0"),
        ('caption_mode', "TEXT DEFAULT 'none'"),
        ('description',  "TEXT DEFAULT ''"),
    ]:
        _col(conn, 'enabled', col, defn)

    conn.commit()

    # One-time migration from legacy JSON files
    c.execute("SELECT COUNT(*) FROM global_settings")
    if c.fetchone()[0] == 0:
        _migrate_legacy(conn, c)

    # One-time migration: create Default playlist from old global shuffle/sync/timer
    c.execute("SELECT COUNT(*) FROM global_settings WHERE key='playlists_migrated'")
    if c.fetchone()[0] == 0:
        _migrate_playlists(conn, c)

    conn.close()


def _migrate_legacy(conn, c):
    logger.info("Migrating legacy JSON config/state files to SQLite…")
    cfg_path = os.path.join(SHARE_DIR, 'config.json')
    if os.path.exists(cfg_path):
        try:
            with open(cfg_path) as f:
                cfg = json.load(f)
            for k, v in [
                ('timer',       str(cfg.get('timer', 900))),
                ('wake_timeout', str(cfg.get('wake_timeout', 45))),
                ('shuffle',     '1' if cfg.get('shuffle') else '0'),
                ('sync_images', '1' if cfg.get('sync_images') else '0'),
            ]:
                c.execute("INSERT OR REPLACE INTO global_settings VALUES (?,?)", (k, v))
            for dev in cfg.get('devices', []):
                c.execute("""INSERT OR REPLACE INTO devices
                    (mac,name,orientation,debug,mode,images_json) VALUES (?,?,?,?,?,?)""",
                    (dev.get('mac','').lower(), dev.get('name',''),
                     dev.get('orientation','landscape'), 1 if dev.get('debug') else 0,
                     dev.get('mode','group'), json.dumps(dev.get('images',[]))))
        except Exception as e:
            logger.error(f"Migration of config.json failed: {e}")

    for path, loader in [
        (os.path.join(SHARE_DIR, 'image_crops.json'),   '_mig_crops'),
        (os.path.join(SHARE_DIR, 'image_enabled.json'), '_mig_enabled'),
        (os.path.join(SHARE_DIR, 'image_order.json'),   '_mig_order'),
        (os.path.join(SHARE_DIR, 'server_state.json'),  '_mig_state'),
    ]:
        if os.path.exists(path):
            try:
                with open(path) as f:
                    data = json.load(f)
                if loader == '_mig_crops':
                    for base, val in data.items():
                        c.execute("INSERT OR REPLACE INTO crops VALUES (?,?,?)",
                                  (base, val.get('l', 0.5), val.get('p', 0.5)))
                elif loader == '_mig_enabled':
                    for base, val in data.items():
                        lv = val if isinstance(val, bool) else val.get('l', True)
                        pv = val if isinstance(val, bool) else val.get('p', True)
                        c.execute("INSERT OR REPLACE INTO enabled (base,l,p) VALUES (?,?,?)",
                                  (base, 1 if lv else 0, 1 if pv else 0))
                elif loader == '_mig_order':
                    for idx, base in enumerate(data):
                        c.execute("INSERT OR REPLACE INTO image_order VALUES (?,?)", (base, idx))
                    c.execute("INSERT OR REPLACE INTO global_settings VALUES ('image_order_initialized','1')")
                elif loader == '_mig_state':
                    for k, v in data.items():
                        vs = json.dumps(v) if isinstance(v, (dict, list)) else str(v)
                        c.execute("INSERT OR REPLACE INTO global_settings VALUES (?,?)", (k, vs))
            except Exception as e:
                logger.error(f"Migration error ({path}): {e}")
    conn.commit()


def _migrate_playlists(conn, c):
    """Create a Default playlist from old global settings and assign all devices to it."""
    try:
        c.execute("SELECT value FROM global_settings WHERE key='timer'")
        row = c.fetchone()
        sleep = int(row['value']) if row else 900
        c.execute("SELECT value FROM global_settings WHERE key='shuffle'")
        row = c.fetchone(); shuffle = 1 if (row and row['value'] == '1') else 0
        c.execute("SELECT value FROM global_settings WHERE key='sync_images'")
        row = c.fetchone(); sync = 1 if (row and row['value'] == '1') else 0

        c.execute("INSERT INTO playlists (name, shuffle, sync, sleep_interval) VALUES (?,?,?,?)",
                  ('Default', shuffle, sync, sleep))
        pid = c.lastrowid
        c.execute("INSERT OR REPLACE INTO global_settings VALUES ('default_playlist_id', ?)", (str(pid),))
        c.execute("SELECT mac FROM devices")
        for row in c.fetchall():
            c.execute("INSERT OR REPLACE INTO playlist_devices VALUES (?,?)", (row['mac'], pid))
        c.execute("INSERT OR REPLACE INTO global_settings VALUES ('playlists_migrated','1')")
        conn.commit()
        logger.info(f"Created Default playlist (id={pid}) and migrated all devices to it")
    except Exception as e:
        logger.error(f"Playlist migration failed: {e}")


# ---------------------------------------------------------------------------
# Global settings
# ---------------------------------------------------------------------------

def get_global_setting(key, default=None):
    try:
        conn = get_db()
        row = conn.execute("SELECT value FROM global_settings WHERE key=?", (key,)).fetchone()
        conn.close()
        return row['value'] if row else default
    except Exception:
        return default


def set_global_setting(key, value):
    try:
        conn = get_db()
        conn.execute("INSERT OR REPLACE INTO global_settings VALUES (?,?)", (key, str(value)))
        conn.commit(); conn.close()
    except Exception as e:
        logger.error(f"set_global_setting({key}): {e}")


# ---------------------------------------------------------------------------
# Playlists
# ---------------------------------------------------------------------------

def load_playlists():
    try:
        conn = get_db()
        rows = conn.execute("SELECT id,name,shuffle,sync,sleep_interval FROM playlists ORDER BY id").fetchall()
        result = [dict(r) for r in rows]
        conn.close()
        return result
    except Exception as e:
        logger.error(f"load_playlists: {e}"); return []


def load_playlist(playlist_id):
    if playlist_id is None:
        return None
    try:
        conn = get_db()
        row = conn.execute("SELECT id,name,shuffle,sync,sleep_interval FROM playlists WHERE id=?",
                           (int(playlist_id),)).fetchone()
        conn.close()
        return dict(row) if row else None
    except Exception as e:
        logger.error(f"load_playlist({playlist_id}): {e}"); return None


def create_playlist(name, shuffle=False, sync=True, sleep_interval=900):
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute("INSERT INTO playlists (name,shuffle,sync,sleep_interval) VALUES (?,?,?,?)",
                  (name, 1 if shuffle else 0, 1 if sync else 0, int(sleep_interval)))
        pid = c.lastrowid; conn.commit(); conn.close()
        return pid
    except Exception as e:
        logger.error(f"create_playlist: {e}"); return None


def update_playlist(playlist_id, **kwargs):
    allowed = {'name', 'shuffle', 'sync', 'sleep_interval'}
    fields = {k: v for k, v in kwargs.items() if k in allowed}
    if not fields:
        return
    try:
        conn = get_db()
        for col, val in fields.items():
            conn.execute(f"UPDATE playlists SET {col}=? WHERE id=?", (val, int(playlist_id)))
        conn.commit(); conn.close()
    except Exception as e:
        logger.error(f"update_playlist({playlist_id}): {e}")


def delete_playlist(playlist_id):
    try:
        conn = get_db()
        conn.execute("DELETE FROM playlists WHERE id=?", (int(playlist_id),))
        conn.execute("UPDATE playlist_devices SET playlist_id=NULL WHERE playlist_id=?", (int(playlist_id),))
        conn.commit(); conn.close()
    except Exception as e:
        logger.error(f"delete_playlist({playlist_id}): {e}")


def get_playlist_images(playlist_id):
    try:
        conn = get_db()
        rows = conn.execute(
            "SELECT base FROM playlist_images WHERE playlist_id=? ORDER BY sort_order",
            (int(playlist_id),)).fetchall()
        conn.close()
        return [r['base'] for r in rows]
    except Exception as e:
        logger.error(f"get_playlist_images({playlist_id}): {e}"); return []


def add_playlist_image(playlist_id, base):
    try:
        conn = get_db()
        row = conn.execute(
            "SELECT COALESCE(MAX(sort_order),0)+1 AS next FROM playlist_images WHERE playlist_id=?",
            (int(playlist_id),)).fetchone()
        next_order = row['next'] if row else 0
        conn.execute("INSERT OR IGNORE INTO playlist_images VALUES (?,?,?)",
                     (int(playlist_id), base, next_order))
        conn.commit(); conn.close()
    except Exception as e:
        logger.error(f"add_playlist_image: {e}")


def remove_playlist_image(playlist_id, base):
    try:
        conn = get_db()
        conn.execute("DELETE FROM playlist_images WHERE playlist_id=? AND base=?",
                     (int(playlist_id), base))
        conn.commit(); conn.close()
    except Exception as e:
        logger.error(f"remove_playlist_image: {e}")


def reorder_playlist_images(playlist_id, bases):
    try:
        conn = get_db()
        for i, base in enumerate(bases):
            conn.execute("UPDATE playlist_images SET sort_order=? WHERE playlist_id=? AND base=?",
                         (i, int(playlist_id), base))
        conn.commit(); conn.close()
    except Exception as e:
        logger.error(f"reorder_playlist_images: {e}")


def get_playlist_devices(playlist_id):
    try:
        conn = get_db()
        rows = conn.execute("SELECT mac FROM playlist_devices WHERE playlist_id=?",
                            (int(playlist_id),)).fetchall()
        conn.close()
        return [r['mac'] for r in rows]
    except Exception as e:
        logger.error(f"get_playlist_devices: {e}"); return []


def get_device_playlist_id(mac):
    try:
        conn = get_db()
        row = conn.execute("SELECT playlist_id FROM playlist_devices WHERE mac=?",
                           (mac.lower(),)).fetchone()
        conn.close()
        return row['playlist_id'] if row else None
    except Exception as e:
        logger.error(f"get_device_playlist_id: {e}"); return None


def set_device_playlist(mac, playlist_id):
    """Assign device to a playlist (or None to unassign)."""
    try:
        conn = get_db()
        if playlist_id is None:
            conn.execute("DELETE FROM playlist_devices WHERE mac=?", (mac.lower(),))
        else:
            conn.execute("INSERT OR REPLACE INTO playlist_devices VALUES (?,?)",
                         (mac.lower(), int(playlist_id)))
        conn.commit(); conn.close()
    except Exception as e:
        logger.error(f"set_device_playlist: {e}")


# ---------------------------------------------------------------------------
# Config (devices)
# ---------------------------------------------------------------------------

def load_config():
    defaults = {"wake_timeout": 45, "devices": []}
    try:
        conn = get_db()
        row = conn.execute("SELECT value FROM global_settings WHERE key='wake_timeout'").fetchone()
        if row:
            defaults['wake_timeout'] = int(row['value'])
        rows = conn.execute(
            "SELECT mac,name,orientation,debug,mode,shuffle,flip_l,flip_p,images_json FROM devices"
        ).fetchall()
        defaults['devices'] = [{
            "mac": r['mac'].lower(), "name": r['name'],
            "orientation": r['orientation'], "debug": bool(r['debug']),
            "mode": r['mode'], "shuffle": bool(r['shuffle']),
            "flip_l": bool(r['flip_l']), "flip_p": bool(r['flip_p']),
            "images": json.loads(r['images_json'] or '[]'),
        } for r in rows]
        conn.close()
    except Exception as e:
        logger.error(f"load_config: {e}")
    return defaults


def save_config(cfg):
    try:
        conn = get_db()
        conn.execute("INSERT OR REPLACE INTO global_settings VALUES ('wake_timeout',?)",
                     (str(cfg.get('wake_timeout', 45)),))
        conn.execute("DELETE FROM devices")
        for dev in cfg.get('devices', []):
            conn.execute("""INSERT OR REPLACE INTO devices
                (mac,name,orientation,debug,mode,shuffle,flip_l,flip_p,images_json)
                VALUES (?,?,?,?,?,?,?,?,?)""", (
                dev.get('mac','').lower(), dev.get('name',''),
                dev.get('orientation','landscape'), 1 if dev.get('debug') else 0,
                dev.get('mode','group'), 1 if dev.get('shuffle') else 0,
                1 if dev.get('flip_l') else 0, 1 if dev.get('flip_p') else 0,
                json.dumps(dev.get('images', []))))
        conn.commit(); conn.close()
        return True
    except Exception as e:
        logger.error(f"save_config: {e}"); return False


# ---------------------------------------------------------------------------
# Crops
# ---------------------------------------------------------------------------

def load_crops():
    crops = {}
    try:
        conn = get_db()
        for r in conn.execute("SELECT base,l,p FROM crops").fetchall():
            crops[r['base']] = {"l": r['l'], "p": r['p']}
        conn.close()
    except Exception as e:
        logger.error(f"load_crops: {e}")
    return crops


def save_crops(crops):
    try:
        conn = get_db()
        for base, val in crops.items():
            conn.execute("INSERT OR REPLACE INTO crops VALUES (?,?,?)",
                         (base, val.get('l', 0.5), val.get('p', 0.5)))
        conn.commit(); conn.close()
    except Exception as e:
        logger.error(f"save_crops: {e}")


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

def load_state():
    defaults = {
        "last_sync_ts": 0, "last_change_ts": 0,
        "last_seen": {}, "device_ips": {}, "device_images": {},
        "redownload": {}, "queued_image": None,
        "device_indices": {}, "playlist_indices": {},
    }
    try:
        conn = get_db()
        for r in conn.execute("SELECT key,value FROM global_settings").fetchall():
            k, v = r['key'], r['value']
            if k in ('last_sync_ts', 'last_change_ts'):
                defaults[k] = int(v)
            elif k in ('last_seen', 'device_ips', 'device_images', 'redownload',
                        'queued_image', 'device_indices', 'playlist_indices'):
                try:
                    defaults[k] = json.loads(v)
                except Exception:
                    pass
        conn.close()
    except Exception as e:
        logger.error(f"load_state: {e}")
    return defaults


def save_state(state):
    try:
        conn = get_db()
        for k, v in state.items():
            vs = json.dumps(v) if isinstance(v, (dict, list)) else str(v)
            conn.execute("INSERT OR REPLACE INTO global_settings VALUES (?,?)", (k, vs))
        conn.commit(); conn.close()
    except Exception as e:
        logger.error(f"save_state: {e}")


def trigger_redownload(mac=None):
    state = load_state(); state.setdefault('redownload', {})
    if mac:
        state['redownload'][mac.lower()] = True
    else:
        for dev in load_config().get('devices', []):
            if dev.get('mac'):
                state['redownload'][dev['mac'].lower()] = True
    save_state(state)


# ---------------------------------------------------------------------------
# Image order
# ---------------------------------------------------------------------------

def load_image_order():
    order = []; initialized = False
    try:
        conn = get_db()
        order = [r['base'] for r in conn.execute(
            "SELECT base FROM image_order ORDER BY sort_order").fetchall()]
        row = conn.execute(
            "SELECT value FROM global_settings WHERE key='image_order_initialized'").fetchone()
        initialized = bool(row and row['value'] == '1')
        if not order and not initialized:
            order = sorted([
                os.path.splitext(f)[0] for f in os.listdir(ORIGINALS_DIR)
                if os.path.splitext(f)[1].lower() in ALLOWED_EXTENSIONS
            ])
            conn.execute("DELETE FROM image_order")
            for idx, base in enumerate(order):
                conn.execute("INSERT INTO image_order VALUES (?,?)", (base, idx))
            conn.execute("INSERT OR REPLACE INTO global_settings VALUES ('image_order_initialized','1')")
            conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"load_image_order: {e}")
    return order


def save_image_order(order):
    try:
        conn = get_db()
        conn.execute("DELETE FROM image_order")
        for idx, base in enumerate(order):
            conn.execute("INSERT INTO image_order VALUES (?,?)", (base, idx))
        conn.execute("INSERT OR REPLACE INTO global_settings VALUES ('image_order_initialized','1')")
        conn.commit(); conn.close()
    except Exception as e:
        logger.error(f"save_image_order: {e}")


# ---------------------------------------------------------------------------
# Enabled flags
# ---------------------------------------------------------------------------

def load_enabled():
    enabled = {}
    try:
        conn = get_db()
        for r in conn.execute("SELECT * FROM enabled").fetchall():
            cols = r.keys()
            enabled[r['base']] = {
                "l": bool(r['l']), "p": bool(r['p']),
                "title": bool(r['title']) if 'title' in cols else False,
                "caption_mode": r['caption_mode'] if 'caption_mode' in cols else 'none',
                "description": r['description'] if 'description' in cols else '',
            }
        conn.close()
    except Exception as e:
        logger.error(f"load_enabled: {e}")
    return enabled


def save_enabled(enabled):
    try:
        conn = get_db()
        for base, val in enabled.items():
            lv  = val.get('l', True) if isinstance(val, dict) else val
            pv  = val.get('p', True) if isinstance(val, dict) else val
            tv  = val.get('title', False) if isinstance(val, dict) else False
            cm  = val.get('caption_mode', 'none') if isinstance(val, dict) else 'none'
            dv  = val.get('description', '') if isinstance(val, dict) else ''
            conn.execute("""INSERT OR REPLACE INTO enabled
                (base,l,p,title,caption_mode,description) VALUES (?,?,?,?,?,?)""",
                (base, 1 if lv else 0, 1 if pv else 0, 1 if tv else 0, cm, dv))
        conn.commit(); conn.close()
    except Exception as e:
        logger.error(f"save_enabled: {e}")


def flags(enabled, base):
    v = enabled.get(base, {"l": True, "p": True, "title": False,
                            "caption_mode": 'none', "description": ''})
    if isinstance(v, bool):
        return {"l": v, "p": v, "title": False, "caption_mode": 'none', "description": ''}
    return {
        "l": v.get("l", True), "p": v.get("p", True), "title": v.get("title", False),
        "caption_mode": v.get("caption_mode", 'none'), "description": v.get("description", ''),
    }


# ---------------------------------------------------------------------------
# Active bases helpers
# ---------------------------------------------------------------------------

def landscape_file(base): return base + LANDSCAPE_SUFFIX
def portrait_file(base):  return base + PORTRAIT_SUFFIX
def orient_suffix(orientation):
    return LANDSCAPE_SUFFIX if orientation == 'landscape' else PORTRAIT_SUFFIX


def get_active_bases(orientation=None):
    """Return bases enabled in the general pool."""
    order   = load_image_order()
    enabled = load_enabled()
    result  = []
    for base in order:
        f = flags(enabled, base)
        has_l = os.path.exists(os.path.join(IMAGES_DIR, landscape_file(base)))
        has_p = os.path.exists(os.path.join(IMAGES_DIR, portrait_file(base)))
        if orientation == 'landscape'  and has_l and f["l"]: result.append(base)
        elif orientation == 'portrait' and has_p and f["p"]: result.append(base)
        elif orientation is None and ((has_l and f["l"]) or (has_p and f["p"])): result.append(base)
    return result


def get_device_active_bases(mac, orientation):
    """Return active bases for a device based on its playlist (or general pool)."""
    pid = get_device_playlist_id(mac)
    if pid is None:
        return get_active_bases(orientation)
    bases = get_playlist_images(pid)
    orient_char = 'l' if orientation == 'landscape' else 'p'
    return [b for b in bases
            if os.path.exists(os.path.join(IMAGES_DIR, b + f'_{orient_char}.bmp'))]


def get_playlist_settings(playlist_id):
    """Return shuffle/sync/sleep_interval for a playlist."""
    if playlist_id is None:
        return {'shuffle': False, 'sync': False, 'sleep_interval': 900}
    pl = load_playlist(playlist_id)
    if not pl:
        return {'shuffle': False, 'sync': False, 'sleep_interval': 900}
    return {
        'shuffle':        bool(pl.get('shuffle', False)),
        'sync':           bool(pl.get('sync', True)),
        'sleep_interval': int(pl.get('sleep_interval', 900)),
    }


def get_unified_index():
    order = load_image_order(); enabled = load_enabled(); result = []
    for base in order:
        f = flags(enabled, base)
        if f["l"] and os.path.exists(os.path.join(IMAGES_DIR, landscape_file(base))):
            result.append(landscape_file(base))
        if f["p"] and os.path.exists(os.path.join(IMAGES_DIR, portrait_file(base))):
            result.append(portrait_file(base))
    return result

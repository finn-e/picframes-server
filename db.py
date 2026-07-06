# ==========================================================================================
# DESCRIPTION: SQLite Database model & legacy migration layer. Handles users, devices, playlist queues, and config tables.
# DEPENDENCIES: sqlite3, json, threading
# ==========================================================================================
import os
import json
import logging
import re
import threading
import sqlite3
import time as _time
import uuid as _uuid_mod

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


def _get_owner_id(owner_id=None):
    if owner_id is not None:
        return owner_id
    try:
        from flask import has_request_context, session
        if has_request_context():
            return session.get('user_id', 1)
    except Exception:
        pass
    return 1


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

    # Users table
    c.execute("""CREATE TABLE IF NOT EXISTS users (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        username      TEXT NOT NULL UNIQUE,
        password_hash TEXT NOT NULL,
        is_admin      INTEGER DEFAULT 0,
        created_at    TEXT DEFAULT (datetime('now')))""")

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
        ('shuffle',    'INTEGER DEFAULT 0'),
        ('flip_l',     'INTEGER DEFAULT 0'),
        ('flip_p',     'INTEGER DEFAULT 0'),
        ('hw_profile', "TEXT DEFAULT ''"),
        # resolution stored as 'WxH' string; NULL = use type default / 800x480
        ('resolution', 'TEXT DEFAULT NULL'),
    ]:
        _col(conn, 'devices', col, defn)
    for col, defn in [
        ('title',        "INTEGER DEFAULT 0"),
        ('caption_mode', "TEXT DEFAULT 'none'"),
        ('description',  "TEXT DEFAULT ''"),
    ]:
        _col(conn, 'enabled', col, defn)

    for col, defn in [
        ('owner_id', 'INTEGER DEFAULT 1'),
    ]:
        _col(conn, 'playlists', col, defn)
        _col(conn, 'devices', col, defn)
        _col(conn, 'image_order', col, defn)

    # resolution lock: locked to 'WxH' by the first device assigned; NULL = unlocked
    _col(conn, 'playlists', 'resolution', 'TEXT DEFAULT NULL')

    # Battery history
    c.execute("""CREATE TABLE IF NOT EXISTS battery_history (
        mac TEXT NOT NULL, ts INTEGER NOT NULL, pct INTEGER NOT NULL)""")
    c.execute("""CREATE INDEX IF NOT EXISTS idx_battery_mac_ts
        ON battery_history (mac, ts)""")

    # Non-destructive image edit params — per base (legacy; obsoleted by playlist_entries).
    # Kept in DB for rollback; new code writes per-entry edits to playlist_entries instead.
    c.execute("""CREATE TABLE IF NOT EXISTS image_edits (
        base      TEXT PRIMARY KEY,
        crop_l_x  REAL, crop_l_y REAL, crop_l_w REAL, crop_l_h REAL,
        crop_p_x  REAL, crop_p_y REAL, crop_p_w REAL, crop_p_h REAL,
        hue_shift REAL DEFAULT 0,
        saturation REAL DEFAULT 1,
        value_adj  REAL DEFAULT 1,
        r_gain     REAL DEFAULT 1,
        g_gain     REAL DEFAULT 1,
        b_gain     REAL DEFAULT 1,
        bg_color   TEXT DEFAULT '#ffffff'
    )""")

    # Per-image identity table: uuid PK, original filename, owner scoping
    c.execute("""CREATE TABLE IF NOT EXISTS images (
        uuid              TEXT PRIMARY KEY,
        original_filename TEXT NOT NULL,
        owner_id          INTEGER DEFAULT 1
    )""")

    # First-class playlist entries: per-entry title (= bin caption) + per-entry editor settings
    c.execute("""CREATE TABLE IF NOT EXISTS playlist_entries (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        playlist_id INTEGER NOT NULL,
        image_uuid  TEXT NOT NULL,
        position    INTEGER DEFAULT 0,
        title       TEXT NOT NULL DEFAULT '',
        crop_l_x REAL, crop_l_y REAL, crop_l_w REAL, crop_l_h REAL,
        crop_p_x REAL, crop_p_y REAL, crop_p_w REAL, crop_p_h REAL,
        hue_shift  REAL DEFAULT 0,
        saturation REAL DEFAULT 1,
        value_adj  REAL DEFAULT 1,
        r_gain     REAL DEFAULT 1,
        g_gain     REAL DEFAULT 1,
        b_gain     REAL DEFAULT 1,
        bg_color   TEXT DEFAULT '#ffffff',
        rotate     INTEGER DEFAULT 0,
        enabled_l  INTEGER DEFAULT 1,
        enabled_p  INTEGER DEFAULT 1,
        FOREIGN KEY (playlist_id) REFERENCES playlists(id) ON DELETE CASCADE,
        FOREIGN KEY (image_uuid)  REFERENCES images(uuid)
    )""")

    # Migration guard: add rotate column if missing (table already exists in live DB)
    try:
        c.execute("ALTER TABLE image_edits ADD COLUMN rotate INTEGER DEFAULT 0")
        conn.commit()
    except Exception:
        pass  # column already exists

    conn.commit()

    _seed_admin(conn, c)

    # One-time migration from legacy JSON files
    c.execute("SELECT COUNT(*) FROM global_settings")
    if c.fetchone()[0] == 0:
        _migrate_legacy(conn, c)

    # One-time migration: create Default playlist from old global shuffle/sync/timer
    c.execute("SELECT COUNT(*) FROM global_settings WHERE key='playlists_migrated'")
    if c.fetchone()[0] == 0:
        _migrate_playlists(conn, c)

    # One-time migration: backfill images table + playlist_entries from playlist_images
    c.execute("SELECT COUNT(*) FROM global_settings WHERE key='entries_migration_done'")
    if c.fetchone()[0] == 0:
        _migrate_entries(conn, c)

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
# Users
# ---------------------------------------------------------------------------

def _seed_admin(conn, c):
    from werkzeug.security import generate_password_hash
    pwd = os.environ.get('ADMIN_PASSWORD', 'admin')
    c.execute("""INSERT INTO users (username, password_hash, is_admin) VALUES ('admin', ?, 1)
                 ON CONFLICT(username) DO UPDATE SET password_hash=excluded.password_hash""",
              (generate_password_hash(pwd),))
    conn.commit()


def get_user_by_username(username):
    try:
        conn = get_db()
        row = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        conn.close()
        return dict(row) if row else None
    except Exception:
        return None


def check_user_password(username, password):
    from werkzeug.security import check_password_hash
    user = get_user_by_username(username)
    if not user:
        return False
    return check_password_hash(user['password_hash'], password)


def get_user_stats():
    """Per-user counts of images, playlists and devices, keyed by user id."""
    stats = {}
    try:
        conn = get_db()
        for table, key in (('image_order', 'images'),
                           ('playlists', 'playlists'),
                           ('devices', 'devices')):
            for r in conn.execute(
                    f"SELECT owner_id, COUNT(*) AS n FROM {table} GROUP BY owner_id"):
                stats.setdefault(r['owner_id'],
                                 {'images': 0, 'playlists': 0, 'devices': 0})[key] = r['n']
        conn.close()
    except Exception as e:
        logger.error(f"get_user_stats: {e}")
    return stats


def list_users():
    try:
        conn = get_db()
        rows = conn.execute("SELECT id, username, is_admin, created_at FROM users ORDER BY id").fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.error(f"list_users: {e}"); return []


def create_user(username, password, is_admin=False):
    from werkzeug.security import generate_password_hash
    try:
        conn = get_db()
        c = conn.execute("INSERT INTO users (username, password_hash, is_admin) VALUES (?,?,?)",
                         (username, generate_password_hash(password), 1 if is_admin else 0))
        uid = c.lastrowid
        conn.commit(); conn.close()
        return uid
    except sqlite3.IntegrityError:
        return None
    finally:
        try: conn.close()
        except Exception: pass


def delete_user(user_id):
    try:
        conn = get_db()
        conn.execute("DELETE FROM users WHERE id=?", (user_id,))
        conn.commit(); conn.close()
    except Exception as e:
        logger.error(f"delete_user({user_id}): {e}")


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

def load_playlists(owner_id=None):
    owner_id = _get_owner_id(owner_id)
    try:
        conn = get_db()
        rows = conn.execute("SELECT id,name,shuffle,sync,sleep_interval FROM playlists WHERE owner_id=? ORDER BY id", (owner_id,)).fetchall()
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


def create_playlist(name, shuffle=False, sync=True, sleep_interval=900, owner_id=None):
    owner_id = _get_owner_id(owner_id)
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute("INSERT INTO playlists (name,shuffle,sync,sleep_interval,owner_id) VALUES (?,?,?,?,?)",
                  (name, 1 if shuffle else 0, 1 if sync else 0, int(sleep_interval), owner_id))
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

def load_config(owner_id=None):
    owner_id = _get_owner_id(owner_id)
    defaults = {"wake_timeout": 45, "devices": []}
    try:
        conn = get_db()
        row = conn.execute("SELECT value FROM global_settings WHERE key='wake_timeout'").fetchone()
        if row:
            defaults['wake_timeout'] = int(row['value'])
        rows = conn.execute(
            "SELECT mac,name,orientation,debug,mode,shuffle,flip_l,flip_p,images_json,hw_profile FROM devices WHERE owner_id=?", (owner_id,)
        ).fetchall()
        defaults['devices'] = [{
            "mac": r['mac'].lower(), "name": r['name'],
            "orientation": r['orientation'], "debug": bool(r['debug']),
            "mode": r['mode'], "shuffle": bool(r['shuffle']),
            "flip_l": bool(r['flip_l']), "flip_p": bool(r['flip_p']),
            "images": json.loads(r['images_json'] or '[]'),
            "hw_profile": r['hw_profile'] or '',
        } for r in rows]
        conn.close()
    except Exception as e:
        logger.error(f"load_config: {e}")
    return defaults


def save_config(cfg, owner_id=None):
    owner_id = _get_owner_id(owner_id)
    try:
        conn = get_db()
        conn.execute("INSERT OR REPLACE INTO global_settings VALUES ('wake_timeout',?)",
                     (str(cfg.get('wake_timeout', 45)),))
        conn.execute("DELETE FROM devices WHERE owner_id=?", (owner_id,))
        for dev in cfg.get('devices', []):
            conn.execute("""INSERT OR REPLACE INTO devices
                (mac,name,orientation,debug,mode,shuffle,flip_l,flip_p,images_json,hw_profile,owner_id)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)""", (
                dev.get('mac','').lower(), dev.get('name',''),
                dev.get('orientation','landscape'), 1 if dev.get('debug') else 0,
                dev.get('mode','group'), 1 if dev.get('shuffle') else 0,
                1 if dev.get('flip_l') else 0, 1 if dev.get('flip_p') else 0,
                json.dumps(dev.get('images', [])),
                dev.get('hw_profile', ''), owner_id))
        conn.commit(); conn.close()
        return True
    except Exception as e:
        logger.error(f"save_config: {e}"); return False


def get_device_owner_id(mac):
    """Return the owner_id of an existing device row, or None if unknown."""
    try:
        conn = get_db()
        row = conn.execute("SELECT owner_id FROM devices WHERE mac=?",
                           (mac.lower(),)).fetchone()
        conn.close()
        return row['owner_id'] if row else None
    except Exception as e:
        logger.error(f"get_device_owner_id({mac}): {e}")
        return None


def update_device_hw_profile(mac, hw_profile):
    """Persist hw_profile for a device.  Returns True if the stored value changed."""
    mac = mac.lower()
    try:
        conn = get_db()
        row = conn.execute("SELECT hw_profile FROM devices WHERE mac=?", (mac,)).fetchone()
        old = row['hw_profile'] if row else None
        changed = (old != hw_profile)
        if changed:
            conn.execute("UPDATE devices SET hw_profile=? WHERE mac=?", (hw_profile, mac))
            conn.commit()
        conn.close()
        return changed
    except Exception as e:
        logger.error(f"update_device_hw_profile({mac}): {e}")
        return False


def get_device_resolution(mac):
    """Return the stored 'WxH' resolution for *mac*, or None if not set."""
    mac = mac.lower()
    try:
        conn = get_db()
        row = conn.execute("SELECT resolution FROM devices WHERE mac=?", (mac,)).fetchone()
        conn.close()
        return row['resolution'] if row else None
    except Exception as e:
        logger.error(f"get_device_resolution({mac}): {e}")
        return None


def update_device_resolution(mac, resolution):
    """Persist the 'WxH' resolution string for a device.  Returns True if changed."""
    mac = mac.lower()
    try:
        conn = get_db()
        row = conn.execute("SELECT resolution FROM devices WHERE mac=?", (mac,)).fetchone()
        old = row['resolution'] if row else None
        changed = (old != resolution)
        if changed:
            conn.execute("UPDATE devices SET resolution=? WHERE mac=?", (resolution, mac))
            conn.commit()
        conn.close()
        return changed
    except Exception as e:
        logger.error(f"update_device_resolution({mac}): {e}")
        return False


def get_playlist_resolution(playlist_id):
    """Return the locked 'WxH' resolution for a playlist, or None if unlocked."""
    try:
        conn = get_db()
        row = conn.execute("SELECT resolution FROM playlists WHERE id=?",
                           (int(playlist_id),)).fetchone()
        conn.close()
        return row['resolution'] if row else None
    except Exception as e:
        logger.error(f"get_playlist_resolution({playlist_id}): {e}")
        return None


def set_playlist_resolution(playlist_id, resolution):
    """Lock *playlist_id* to *resolution* ('WxH' string)."""
    try:
        conn = get_db()
        conn.execute("UPDATE playlists SET resolution=? WHERE id=?",
                     (resolution, int(playlist_id)))
        conn.commit(); conn.close()
    except Exception as e:
        logger.error(f"set_playlist_resolution({playlist_id}): {e}")


def clear_playlist_resolution(playlist_id):
    """Clear the resolution lock for *playlist_id* (unlocks it for new assignments)."""
    try:
        conn = get_db()
        conn.execute("UPDATE playlists SET resolution=NULL WHERE id=?",
                     (int(playlist_id),))
        conn.commit(); conn.close()
    except Exception as e:
        logger.error(f"clear_playlist_resolution({playlist_id}): {e}")


def get_playlists_for_image(base):
    """Return list of playlist_ids that contain this image base."""
    try:
        conn = get_db()
        rows = conn.execute(
            "SELECT DISTINCT playlist_id FROM playlist_images WHERE base=?", (base,)
        ).fetchall()
        conn.close()
        return [r['playlist_id'] for r in rows]
    except Exception as e:
        logger.error(f"get_playlists_for_image({base}): {e}"); return []


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
# Non-destructive image edits (crop rects + colour adjustments)
# ---------------------------------------------------------------------------

def get_image_edits(base):
    """Return a dict of edit params for base, or None if no edits saved."""
    try:
        conn = get_db()
        row = conn.execute("SELECT * FROM image_edits WHERE base=?", (base,)).fetchone()
        conn.close()
        return dict(row) if row else None
    except Exception as e:
        logger.error(f"get_image_edits({base}): {e}")
        return None


def save_image_edits(base, params):
    """Persist non-destructive edit params for base."""
    try:
        conn = get_db()
        crop_l = params.get('crop_l') or {}
        crop_p = params.get('crop_p') or {}
        conn.execute("""INSERT OR REPLACE INTO image_edits
            (base, crop_l_x, crop_l_y, crop_l_w, crop_l_h,
             crop_p_x, crop_p_y, crop_p_w, crop_p_h,
             hue_shift, saturation, value_adj, r_gain, g_gain, b_gain, bg_color,
             rotate)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
            base,
            crop_l.get('x'), crop_l.get('y'), crop_l.get('w'), crop_l.get('h'),
            crop_p.get('x'), crop_p.get('y'), crop_p.get('w'), crop_p.get('h'),
            params.get('hue_shift', 0), params.get('saturation', 1),
            params.get('value_adj', 1), params.get('r_gain', 1),
            params.get('g_gain', 1), params.get('b_gain', 1),
            params.get('bg_color', '#ffffff'),
            int(params.get('rotate', 0) or 0) % 360,
        ))
        conn.commit(); conn.close()
    except Exception as e:
        logger.error(f"save_image_edits({base}): {e}")


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

def load_image_order(owner_id=None):
    owner_id = _get_owner_id(owner_id)
    order = []; initialized = False
    try:
        conn = get_db()
        order = [r['base'] for r in conn.execute(
            "SELECT base FROM image_order WHERE owner_id=? ORDER BY sort_order", (owner_id,)).fetchall()]
        row = conn.execute(
            "SELECT value FROM global_settings WHERE key='image_order_initialized'").fetchone()
        initialized = bool(row and row['value'] == '1')
        if not order and not initialized:
            order = sorted([
                os.path.splitext(f)[0] for f in os.listdir(ORIGINALS_DIR)
                if os.path.splitext(f)[1].lower() in ALLOWED_EXTENSIONS
            ])
            conn.execute("DELETE FROM image_order WHERE owner_id=?", (owner_id,))
            for idx, base in enumerate(order):
                conn.execute("INSERT INTO image_order (base, sort_order, owner_id) VALUES (?,?,?)", (base, idx, owner_id))
            conn.execute("INSERT OR REPLACE INTO global_settings VALUES ('image_order_initialized','1')")
            conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"load_image_order: {e}")
    return order


def save_image_order(order, owner_id=None):
    owner_id = _get_owner_id(owner_id)
    try:
        conn = get_db()
        conn.execute("DELETE FROM image_order WHERE owner_id=?", (owner_id,))
        for idx, base in enumerate(order):
            conn.execute("INSERT INTO image_order (base, sort_order, owner_id) VALUES (?,?,?)", (base, idx, owner_id))
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
    """Return bases enabled in the general pool.

    Presence of the original file is the only hard requirement; converted
    artifacts may not yet exist for all screen types.
    """
    order   = load_image_order()
    enabled = load_enabled()
    # Build a set of bases that have an original file on disk
    originals_on_disk = {
        os.path.splitext(f)[0]
        for f in os.listdir(ORIGINALS_DIR)
        if os.path.splitext(f)[1].lower() in ALLOWED_EXTENSIONS
    }
    result = []
    for base in order:
        if base not in originals_on_disk:
            continue
        f = flags(enabled, base)
        if orientation == 'landscape'  and f["l"]: result.append(base)
        elif orientation == 'portrait' and f["p"]: result.append(base)
        elif orientation is None and (f["l"] or f["p"]): result.append(base)
    return result


def get_device_active_bases(mac, orientation):
    """Return active bases for a device based on its playlist (or general pool)."""
    pid = get_device_playlist_id(mac)
    if pid is None:
        return get_active_bases(orientation)
    # Playlist images are trusted; return all that have an original
    bases = get_playlist_images(pid)
    originals_on_disk = {
        os.path.splitext(f)[0]
        for f in os.listdir(ORIGINALS_DIR)
        if os.path.splitext(f)[1].lower() in ALLOWED_EXTENSIONS
    }
    return [b for b in bases if b in originals_on_disk]


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


# ---------------------------------------------------------------------------
# Battery history
# ---------------------------------------------------------------------------

def record_battery(mac, pct):
    try:
        conn = get_db()
        conn.execute("INSERT INTO battery_history (mac, ts, pct) VALUES (?,?,?)",
                     (mac.lower(), int(_time.time()), int(pct)))
        conn.commit(); conn.close()
    except Exception as e:
        logger.error(f"record_battery({mac}, {pct}): {e}")


def get_battery_history(mac, since_ts=None):
    try:
        conn = get_db()
        if since_ts is not None:
            rows = conn.execute(
                "SELECT ts, pct FROM battery_history WHERE mac=? AND ts>=? ORDER BY ts",
                (mac.lower(), int(since_ts))).fetchall()
        else:
            rows = conn.execute(
                "SELECT ts, pct FROM battery_history WHERE mac=? ORDER BY ts",
                (mac.lower(),)).fetchall()
        conn.close()
        return [{"ts": r["ts"], "pct": r["pct"]} for r in rows]
    except Exception as e:
        logger.error(f"get_battery_history({mac}): {e}"); return []


def get_latest_battery(macs):
    """Return {mac: {pct, ts}} for the most recent reading per mac."""
    result = {}
    if not macs:
        return result
    try:
        conn = get_db()
        for mac in macs:
            row = conn.execute(
                "SELECT ts, pct FROM battery_history WHERE mac=? ORDER BY ts DESC LIMIT 1",
                (mac.lower(),)).fetchone()
            if row:
                result[mac.lower()] = {"pct": row["pct"], "ts": row["ts"]}
        conn.close()
    except Exception as e:
        logger.error(f"get_latest_battery: {e}")
    return result


def get_unified_index():
    order = load_image_order(); enabled = load_enabled(); result = []
    for base in order:
        f = flags(enabled, base)
        if f["l"] and os.path.exists(os.path.join(IMAGES_DIR, landscape_file(base))):
            result.append(landscape_file(base))
        if f["p"] and os.path.exists(os.path.join(IMAGES_DIR, portrait_file(base))):
            result.append(portrait_file(base))
    return result


# ---------------------------------------------------------------------------
# Images table (per-image identity with UUID)
# ---------------------------------------------------------------------------

def sanitize_title(title, existing_titles=None):
    """Sanitize an entry title: whitespace→underscore, strip non-[A-Za-z0-9_-],
    ensure non-empty, ensure unique within a set if *existing_titles* is given."""
    t = re.sub(r'\s+', '_', str(title).strip())
    t = re.sub(r'[^A-Za-z0-9_\-]', '', t)
    if not t:
        t = 'untitled'
    if existing_titles is None:
        return t
    if t not in existing_titles:
        return t
    base_t = t
    n = 2
    while t in existing_titles:
        t = f'{base_t}_{n}'
        n += 1
    return t


def create_image(original_filename, owner_id=None):
    """Create an images row; returns the new uuid string."""
    owner_id = _get_owner_id(owner_id)
    new_uuid = _uuid_mod.uuid4().hex
    try:
        conn = get_db()
        conn.execute("INSERT OR IGNORE INTO images (uuid, original_filename, owner_id) VALUES (?,?,?)",
                     (new_uuid, original_filename, owner_id))
        conn.commit(); conn.close()
        return new_uuid
    except Exception as e:
        logger.error(f"create_image({original_filename}): {e}")
        return None


def get_image_by_uuid(uuid):
    try:
        conn = get_db()
        row = conn.execute("SELECT * FROM images WHERE uuid=?", (uuid,)).fetchone()
        conn.close()
        return dict(row) if row else None
    except Exception as e:
        logger.error(f"get_image_by_uuid({uuid}): {e}")
        return None


def get_image_by_filename(filename, owner_id=None):
    """Look up an image by its exact original_filename."""
    try:
        conn = get_db()
        if owner_id is not None:
            row = conn.execute(
                "SELECT * FROM images WHERE original_filename=? AND owner_id=?",
                (filename, owner_id)).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM images WHERE original_filename=?", (filename,)).fetchone()
        conn.close()
        return dict(row) if row else None
    except Exception as e:
        logger.error(f"get_image_by_filename({filename}): {e}")
        return None


def get_image_by_base(base, owner_id=None):
    """Find an images row whose original_filename stem matches *base*."""
    try:
        conn = get_db()
        if owner_id is not None:
            rows = conn.execute(
                "SELECT * FROM images WHERE owner_id=?", (owner_id,)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM images").fetchall()
        conn.close()
        for r in rows:
            if os.path.splitext(r['original_filename'])[0] == base:
                return dict(r)
        return None
    except Exception as e:
        logger.error(f"get_image_by_base({base}): {e}")
        return None


def get_images_for_owner(owner_id=None):
    owner_id = _get_owner_id(owner_id)
    try:
        conn = get_db()
        rows = conn.execute("SELECT * FROM images WHERE owner_id=?", (owner_id,)).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.error(f"get_images_for_owner: {e}")
        return []


def rename_image_record(uuid, new_filename):
    """Update original_filename for an images row (does NOT rename the file)."""
    try:
        conn = get_db()
        conn.execute("UPDATE images SET original_filename=? WHERE uuid=?", (new_filename, uuid))
        conn.commit(); conn.close()
    except Exception as e:
        logger.error(f"rename_image_record({uuid}): {e}")


def delete_image_record(uuid):
    try:
        conn = get_db()
        conn.execute("DELETE FROM images WHERE uuid=?", (uuid,))
        conn.commit(); conn.close()
    except Exception as e:
        logger.error(f"delete_image_record({uuid}): {e}")


def get_or_create_image(base, owner_id=None):
    """Return uuid for *base*, creating an images row if needed."""
    owner_id = _get_owner_id(owner_id)
    img = get_image_by_base(base, owner_id)
    if img:
        return img['uuid']
    # Find original filename on disk
    for f in os.listdir(ORIGINALS_DIR):
        if os.path.splitext(f)[0] == base and \
                os.path.splitext(f)[1].lower() in ALLOWED_EXTENSIONS:
            return create_image(f, owner_id)
    return None


# ---------------------------------------------------------------------------
# Playlist entries (per-entry identity, title, and editor settings)
# ---------------------------------------------------------------------------

def get_playlist_entries(playlist_id):
    """Return all entries for a playlist ordered by position, as list of dicts."""
    try:
        conn = get_db()
        rows = conn.execute(
            "SELECT * FROM playlist_entries WHERE playlist_id=? ORDER BY position",
            (int(playlist_id),)).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.error(f"get_playlist_entries({playlist_id}): {e}")
        return []


def get_playlist_entry(entry_id):
    try:
        conn = get_db()
        row = conn.execute("SELECT * FROM playlist_entries WHERE id=?", (int(entry_id),)).fetchone()
        conn.close()
        return dict(row) if row else None
    except Exception as e:
        logger.error(f"get_playlist_entry({entry_id}): {e}")
        return None


def add_playlist_entry(playlist_id, image_uuid, title=None):
    """Insert a new playlist_entry; returns the new entry_id."""
    try:
        conn = get_db()
        # Compute unique title within playlist
        existing_titles = {r['title'] for r in conn.execute(
            "SELECT title FROM playlist_entries WHERE playlist_id=?", (int(playlist_id),)).fetchall()}
        base_title = title or 'untitled'
        safe_title = sanitize_title(base_title, existing_titles)
        row = conn.execute(
            "SELECT COALESCE(MAX(position),0)+1 AS next FROM playlist_entries WHERE playlist_id=?",
            (int(playlist_id),)).fetchone()
        next_pos = row['next'] if row else 0
        c = conn.execute(
            """INSERT INTO playlist_entries
               (playlist_id, image_uuid, position, title, enabled_l, enabled_p)
               VALUES (?,?,?,?,1,1)""",
            (int(playlist_id), image_uuid, next_pos, safe_title))
        entry_id = c.lastrowid
        conn.commit(); conn.close()
        return entry_id
    except Exception as e:
        logger.error(f"add_playlist_entry({playlist_id}): {e}")
        return None


def update_playlist_entry(entry_id, **kwargs):
    """Update one or more columns on a playlist_entries row."""
    allowed = {
        'position', 'title', 'enabled_l', 'enabled_p', 'rotate',
        'crop_l_x', 'crop_l_y', 'crop_l_w', 'crop_l_h',
        'crop_p_x', 'crop_p_y', 'crop_p_w', 'crop_p_h',
        'hue_shift', 'saturation', 'value_adj', 'r_gain', 'g_gain', 'b_gain', 'bg_color',
    }
    fields = {k: v for k, v in kwargs.items() if k in allowed}
    if not fields:
        return
    try:
        conn = get_db()
        for col, val in fields.items():
            conn.execute(f"UPDATE playlist_entries SET {col}=? WHERE id=?", (val, int(entry_id)))
        conn.commit(); conn.close()
    except Exception as e:
        logger.error(f"update_playlist_entry({entry_id}): {e}")


def rename_playlist_entry(entry_id, new_title):
    """Rename a playlist entry; sanitizes and ensures uniqueness within its playlist."""
    try:
        conn = get_db()
        row = conn.execute("SELECT playlist_id FROM playlist_entries WHERE id=?",
                           (int(entry_id),)).fetchone()
        if not row:
            conn.close(); return None
        pid = row['playlist_id']
        existing_titles = {r['title'] for r in conn.execute(
            "SELECT title FROM playlist_entries WHERE playlist_id=? AND id!=?",
            (pid, int(entry_id))).fetchall()}
        safe_title = sanitize_title(new_title, existing_titles)
        conn.execute("UPDATE playlist_entries SET title=? WHERE id=?", (safe_title, int(entry_id)))
        conn.commit(); conn.close()
        return safe_title
    except Exception as e:
        logger.error(f"rename_playlist_entry({entry_id}): {e}")
        return None


def remove_playlist_entry(entry_id):
    try:
        conn = get_db()
        conn.execute("DELETE FROM playlist_entries WHERE id=?", (int(entry_id),))
        conn.commit(); conn.close()
    except Exception as e:
        logger.error(f"remove_playlist_entry({entry_id}): {e}")


def reorder_playlist_entries(playlist_id, entry_ids):
    """Set position of each entry in entry_ids list."""
    try:
        conn = get_db()
        for i, eid in enumerate(entry_ids):
            conn.execute("UPDATE playlist_entries SET position=? WHERE id=? AND playlist_id=?",
                         (i, int(eid), int(playlist_id)))
        conn.commit(); conn.close()
    except Exception as e:
        logger.error(f"reorder_playlist_entries({playlist_id}): {e}")


def save_playlist_entry_edits(entry_id, params):
    """Persist non-destructive edit params into a playlist_entries row."""
    crop_l = params.get('crop_l') or {}
    crop_p = params.get('crop_p') or {}
    update_playlist_entry(entry_id,
        crop_l_x=crop_l.get('x'), crop_l_y=crop_l.get('y'),
        crop_l_w=crop_l.get('w'), crop_l_h=crop_l.get('h'),
        crop_p_x=crop_p.get('x'), crop_p_y=crop_p.get('y'),
        crop_p_w=crop_p.get('w'), crop_p_h=crop_p.get('h'),
        hue_shift=float(params.get('hue_shift', 0) or 0),
        saturation=float(params.get('saturation', 1) or 1),
        value_adj=float(params.get('value_adj', 1) or 1),
        r_gain=float(params.get('r_gain', 1) or 1),
        g_gain=float(params.get('g_gain', 1) or 1),
        b_gain=float(params.get('b_gain', 1) or 1),
        bg_color=params.get('bg_color', '#ffffff') or '#ffffff',
        rotate=int(params.get('rotate', 0) or 0) % 360,
    )


def get_device_active_entries(mac, orientation):
    """Return ordered list of enabled playlist_entries for *mac*'s playlist.

    Entries are filtered by enabled_l/enabled_p for the given orientation.
    Returns [] if the device has no playlist assigned.
    Each entry dict includes 'original_filename' from the images join.
    """
    pid = get_device_playlist_id(mac)
    if pid is None:
        return []
    orient_col = 'enabled_l' if orientation == 'landscape' else 'enabled_p'
    try:
        conn = get_db()
        rows = conn.execute(
            f"""SELECT pe.*, i.original_filename
                FROM playlist_entries pe
                JOIN images i ON pe.image_uuid = i.uuid
                WHERE pe.playlist_id=? AND pe.{orient_col}=1
                ORDER BY pe.position""",
            (int(pid),)).fetchall()
        conn.close()
        result = []
        for r in rows:
            if os.path.exists(os.path.join(ORIGINALS_DIR, r['original_filename'])):
                result.append(dict(r))
        return result
    except Exception as e:
        logger.error(f"get_device_active_entries({mac}): {e}")
        return []


def get_playlists_for_entry_image(image_uuid):
    """Return list of playlist_ids that contain entries for this image_uuid."""
    try:
        conn = get_db()
        rows = conn.execute(
            "SELECT DISTINCT playlist_id FROM playlist_entries WHERE image_uuid=?",
            (image_uuid,)).fetchall()
        conn.close()
        return [r['playlist_id'] for r in rows]
    except Exception as e:
        logger.error(f"get_playlists_for_entry_image({image_uuid}): {e}")
        return []


# ---------------------------------------------------------------------------
# entries migration (one-time)
# ---------------------------------------------------------------------------

def _migrate_entries(conn, c):
    """Backfill images table from ORIGINALS_DIR/image_order, and create
    playlist_entries rows from existing playlist_images rows.
    Idempotent — guarded by global_settings key 'entries_migration_done'.
    """
    logger.info("Migrating to images/playlist_entries tables…")

    # Build map base → original_filename from ORIGINALS_DIR
    orig_map = {}
    try:
        for f in os.listdir(ORIGINALS_DIR):
            base = os.path.splitext(f)[0]
            ext  = os.path.splitext(f)[1].lower()
            if ext in ALLOWED_EXTENSIONS:
                orig_map[base] = f
    except Exception as e:
        logger.error(f"_migrate_entries: cannot scan ORIGINALS_DIR: {e}")

    # Build base → owner_id from image_order
    owner_map = {}
    for row in c.execute("SELECT base, owner_id FROM image_order").fetchall():
        owner_map[row['base']] = row['owner_id']

    # Create images rows for every known base
    base_to_uuid = {}
    for base, orig_fn in orig_map.items():
        existing = c.execute(
            "SELECT uuid FROM images WHERE original_filename=?", (orig_fn,)).fetchone()
        if existing:
            base_to_uuid[base] = existing['uuid']
        else:
            new_uuid = _uuid_mod.uuid4().hex
            c.execute("INSERT OR IGNORE INTO images (uuid, original_filename, owner_id) VALUES (?,?,?)",
                      (new_uuid, orig_fn, owner_map.get(base, 1)))
            base_to_uuid[base] = new_uuid

    # Convert playlist_images → playlist_entries (skip if already migrated)
    for pi_row in c.execute(
            "SELECT playlist_id, base, sort_order FROM playlist_images ORDER BY playlist_id, sort_order"
            ).fetchall():
        pid  = pi_row['playlist_id']
        base = pi_row['base']
        pos  = pi_row['sort_order']
        if base not in base_to_uuid:
            logger.warning(f"_migrate_entries: no uuid for base={base!r}, skipping entry")
            continue
        image_uuid = base_to_uuid[base]
        # Skip if entry already exists (safe re-run)
        if c.execute("SELECT 1 FROM playlist_entries WHERE playlist_id=? AND image_uuid=?",
                     (pid, image_uuid)).fetchone():
            continue
        # Compute safe title (unique within playlist)
        existing_titles = {r['title'] for r in c.execute(
            "SELECT title FROM playlist_entries WHERE playlist_id=?", (pid,)).fetchall()}
        title = sanitize_title(base, existing_titles)
        # Carry over edits from image_edits if present
        edits = c.execute("SELECT * FROM image_edits WHERE base=?", (base,)).fetchone()
        if edits and edits['crop_l_w'] is not None:
            c.execute("""INSERT INTO playlist_entries
                (playlist_id, image_uuid, position, title,
                 crop_l_x, crop_l_y, crop_l_w, crop_l_h,
                 crop_p_x, crop_p_y, crop_p_w, crop_p_h,
                 hue_shift, saturation, value_adj, r_gain, g_gain, b_gain, bg_color, rotate,
                 enabled_l, enabled_p)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,1)""",
                (pid, image_uuid, pos, title,
                 edits['crop_l_x'], edits['crop_l_y'], edits['crop_l_w'], edits['crop_l_h'],
                 edits['crop_p_x'], edits['crop_p_y'], edits['crop_p_w'], edits['crop_p_h'],
                 edits['hue_shift'] or 0, edits['saturation'] or 1, edits['value_adj'] or 1,
                 edits['r_gain'] or 1, edits['g_gain'] or 1, edits['b_gain'] or 1,
                 edits['bg_color'] or '#ffffff',
                 int(edits['rotate'] or 0) if 'rotate' in edits.keys() else 0))
        else:
            c.execute("""INSERT INTO playlist_entries
                (playlist_id, image_uuid, position, title, enabled_l, enabled_p)
                VALUES (?,?,?,?,1,1)""",
                (pid, image_uuid, pos, title))

    c.execute("INSERT OR REPLACE INTO global_settings VALUES ('entries_migration_done','1')")
    conn.commit()
    logger.info("entries migration complete")

# ==========================================================================================
# DESCRIPTION: REST API endpoints for e-paper devices. Handles HMAC auth, state checks, checkin, ready, ack, daily-config, daily-zip, and OTA updates.
# DEPENDENCIES: Flask, db, image
# ==========================================================================================
import hashlib
import hmac
import io
import json
import logging
import os
import random
import time
import zipfile
from datetime import datetime

from flask import Blueprint, current_app, jsonify, request, Response

import db
from db import (
    load_config, save_config, load_state, save_state,
    load_enabled, load_crops,
    get_active_bases, get_device_active_bases, get_unified_index,
    get_device_playlist_id, get_playlist_settings, set_device_playlist,
    get_global_setting, trigger_redownload, state_lock, IMAGES_DIR,
    record_battery, get_battery_history,
    update_device_hw_profile, update_device_fw_version, get_device_owner_id,
    # Entry-based helpers
    get_device_active_entries, get_playlist_entry,
)
from image import (ensure_bin_files, ensure_bin_files_for_screen,
                   screen_size_for_profile, _artifact_infix,
                   ensure_entry_bin_files, ensure_entry_bin_files_for_screen,
                   entry_artifact_prefix, entry_bin_path,
                   convert_entry_for_screen, ratios_for_screen,
                   normalize_device_type, DEVICE_TYPE_ALIASES, _DEFAULT_SCREEN,
                   serve_cached_or_build, invalidate_device_zip)

logger = logging.getLogger(__name__)

api_bp = Blueprint('api', __name__)


def _caller_ip():
    if request.headers.get('X-Forwarded-For'):
        return request.headers['X-Forwarded-For'].split(',')[0].strip()
    return request.remote_addr


def _get_mac():
    return (request.headers.get('X-Device-Mac', '') or
            request.args.get('mac', '') or '').strip().lower()


def _device_token_for(mac):
    secret = current_app.secret_key
    return hmac.new(secret.encode(), mac.encode(), hashlib.sha256).hexdigest()[:32]


def _require_device(source):
    """Authenticate a device call: X-Device-Mac + X-Device-Token headers,
    where the token is the one issued by /api/register, and the device must
    already be registered. Returns (dev_cfg, cfg, owner_id, error_response)."""
    mac   = _get_mac()
    token = (request.headers.get('X-Device-Token', '') or '').strip()
    if not mac:
        return None, None, None, (jsonify({'error': 'mac required'}), 400)
    if not token or not hmac.compare_digest(token, _device_token_for(mac)):
        logger.warning(f"{source}: invalid device token for {mac}")
        return None, None, None, (jsonify({'error': 'invalid token'}), 403)
    owner_id = get_device_owner_id(mac)
    if owner_id is None:
        logger.warning(f"{source}: unregistered device {mac}")
        return None, None, None, (jsonify({'error': 'not registered'}), 403)
    cfg = load_config(owner_id=owner_id)
    dev_cfg = next((d for d in cfg.get('devices', [])
                    if d['mac'].lower() == mac), None)
    if not dev_cfg:
        return None, None, None, (jsonify({'error': 'not registered'}), 403)
    return dev_cfg, cfg, owner_id, None


def _auto_register(mac, devices, cfg, source, owner_id):
    """Create a minimal device record and auto-assign to default playlist.
    Only called from /api/register after user credentials are verified."""
    dev_cfg = {'mac': mac, 'name': mac, 'orientation': 'landscape',
               'debug': False, 'mode': 'group', 'images': [],
               'shuffle': False, 'flip_l': False, 'flip_p': False}
    devices.append(dev_cfg)
    cfg['devices'] = devices
    save_config(cfg, owner_id=owner_id)
    # Assign to default playlist
    dpid = get_global_setting('default_playlist_id')
    if dpid:
        set_device_playlist(mac, int(dpid))
    logger.info(f"Auto-registered new device {mac} via {source}")
    return dev_cfg


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

@api_bp.route('/register', methods=['POST'])
def api_register():
    data     = request.get_json() or {}
    mac      = data.get('mac', '').strip().lower()
    username = data.get('username', '').strip()
    password = data.get('password', '').strip()
    if not mac:
        return jsonify({'error': 'mac required'}), 400

    secret = current_app.secret_key
    device_token = hmac.new(secret.encode(), mac.encode(), hashlib.sha256).hexdigest()[:32]

    from db import (check_user_password, get_user_by_username,
                    update_device_resolution)

    if password != device_token:
        # Check re-pair window before requiring credentials.
        # Only applies to already-registered MACs — never creates new devices.
        existing_owner = get_device_owner_id(mac)
        if existing_owner is not None:
            try:
                conn = db.get_db()
                row = conn.execute(
                    "SELECT repair_until FROM devices WHERE mac=?", (mac,)).fetchone()
                conn.close()
                repair_until = row['repair_until'] if row and row['repair_until'] else 0
            except Exception:
                repair_until = 0
            if repair_until and int(time.time()) < repair_until:
                # One-shot: clear the window immediately.
                try:
                    conn = db.get_db()
                    conn.execute("UPDATE devices SET repair_until=NULL WHERE mac=?", (mac,))
                    conn.commit(); conn.close()
                except Exception as e:
                    logger.warning(f"api_register: failed to clear repair_until for {mac}: {e}")
                logger.info(f"re-pair window used by {mac}")
                # Fall through with existing_owner so the token is returned below.
                owner_id = existing_owner
                cfg     = load_config(owner_id=owner_id)
                devices = cfg.get('devices', [])
                dev_cfg = next((d for d in devices if d['mac'].lower() == mac), None)
                # Resolve hw_profile/resolution as normal then return token.
                hw_profile = data.get('hw_profile', data.get('device_type', '')).strip()
                if hw_profile:
                    canonical = normalize_device_type(hw_profile)
                    update_device_hw_profile(mac, canonical)
                else:
                    canonical = None
                res_raw = data.get('resolution')
                resolution = None
                if res_raw is not None:
                    if isinstance(res_raw, (list, tuple)) and len(res_raw) == 2:
                        try:
                            resolution = f'{int(res_raw[0])}x{int(res_raw[1])}'
                        except (TypeError, ValueError):
                            pass
                    elif isinstance(res_raw, str) and 'x' in res_raw.lower():
                        parts = res_raw.lower().split('x', 1)
                        try:
                            resolution = f'{int(parts[0])}x{int(parts[1])}'
                        except (TypeError, ValueError):
                            pass
                if resolution is None and canonical:
                    scr_w, scr_h = screen_size_for_profile(canonical)
                    if (scr_w, scr_h) != _DEFAULT_SCREEN:
                        resolution = f'{scr_w}x{scr_h}'
                if resolution is not None:
                    update_device_resolution(mac, resolution)
                return jsonify({'token': device_token})
        # Fresh registration requires the credentials of a real user account.
        if not username or not check_user_password(username, password):
            return jsonify({'error': 'invalid credentials'}), 403
        creds_verified = True
    else:
        creds_verified = False

    # Ownership: an existing device only moves to the claimed username when
    # real user credentials were verified. A token-authenticated re-register
    # must never change (or duplicate) ownership based on the stored username.
    owner_id = get_device_owner_id(mac)
    if username and (creds_verified or owner_id is None):
        user = get_user_by_username(username)
        if user:
            owner_id = user['id']
    if owner_id is None:
        return jsonify({'error': 'unknown user'}), 403

    cfg     = load_config(owner_id=owner_id)
    devices = cfg.get('devices', [])
    dev_cfg = next((d for d in devices if d['mac'].lower() == mac), None)
    if not dev_cfg:
        _auto_register(mac, devices, cfg, '/api/register', owner_id=owner_id)
    elif username and dev_cfg.get('name') == mac:
        dev_cfg['name'] = username
        save_config(cfg, owner_id=owner_id)

    # Normalize and persist hw_profile (device type) if provided.
    hw_profile = data.get('hw_profile', data.get('device_type', '')).strip()
    if hw_profile:
        canonical = normalize_device_type(hw_profile)
        update_device_hw_profile(mac, canonical)
    else:
        canonical = None

    # Resolve effective resolution: explicit field wins over type default.
    # Accept "WxH" string or [W, H] list/array.
    res_raw = data.get('resolution')
    resolution = None
    if res_raw is not None:
        if isinstance(res_raw, (list, tuple)) and len(res_raw) == 2:
            try:
                resolution = f'{int(res_raw[0])}x{int(res_raw[1])}'
            except (TypeError, ValueError):
                pass
        elif isinstance(res_raw, str) and 'x' in res_raw.lower():
            parts = res_raw.lower().split('x', 1)
            try:
                resolution = f'{int(parts[0])}x{int(parts[1])}'
            except (TypeError, ValueError):
                pass
    if resolution is None and canonical:
        # Fall back to the canonical type's implied default.
        scr_w, scr_h = screen_size_for_profile(canonical)
        if (scr_w, scr_h) != _DEFAULT_SCREEN:
            # Only store if non-default to keep NULL meaning "800x480 default"
            resolution = f'{scr_w}x{scr_h}'
    if resolution is not None:
        update_device_resolution(mac, resolution)

    return jsonify({'token': device_token})


# ---------------------------------------------------------------------------
# Orientation change (device-initiated)
# ---------------------------------------------------------------------------

@api_bp.route('/change-orientation', methods=['POST'])
def api_change_orientation():
    data = request.get_json() or {}
    orientation = data.get('orientation', '').strip()
    server_orient = 'portrait' if 'portrait' in orientation else 'landscape'
    if not orientation:
        return jsonify({'ok': False}), 400
    dev_cfg, cfg, owner_id, err = _require_device('/change-orientation')
    if err:
        return err
    dev_cfg['orientation'] = server_orient
    mac = dev_cfg['mac'].lower()
    trigger_redownload(mac)
    invalidate_device_zip(mac)
    save_config(cfg, owner_id=owner_id)
    return jsonify({'ok': True, 'orientation': server_orient,
                    'show_logo': True,
                    'message': 'No images for this orientation, syncing…'})


# ---------------------------------------------------------------------------
# Queue push
# ---------------------------------------------------------------------------

@api_bp.route('/queue', methods=['POST'])
def queue_image_api():
    data      = request.get_json() or {}
    source    = data.get('source')
    # Support both legacy base-based queuing and new entry_id-based queuing
    base      = data.get('base')
    entry_id  = data.get('entry_id')
    playlist_id = data.get('playlist_id')

    if not source or (not base and entry_id is None):
        return jsonify({'ok': False, 'error': 'Missing base/entry_id or source'}), 400

    with state_lock:
        state = load_state()
        current_queued = state.get('queued_image')

        if entry_id is not None:
            # Entry-based queue (playlist devices)
            new_q = {'entry_id': int(entry_id), 'source': source}
            if playlist_id is not None:
                new_q['playlist_id'] = int(playlist_id)
            if (current_queued and current_queued.get('entry_id') == int(entry_id)
                    and current_queued.get('source') == source):
                state['queued_image'] = None; action = 'dequeued'
            else:
                state['queued_image'] = new_q; action = 'queued'
        else:
            # Legacy base-based queue (general pool devices)
            new_q = {'base': base, 'source': source}
            if (current_queued and current_queued.get('base') == base
                    and current_queued.get('source') == source):
                state['queued_image'] = None; action = 'dequeued'
            else:
                state['queued_image'] = new_q; action = 'queued'

        state.setdefault('redownload', {})
        if source == 'general':
            cfg = load_config()
            for dev in cfg.get('devices', []):
                if dev.get('mac'):
                    state['redownload'][dev['mac'].lower()] = True
        else:
            state['redownload'][source.lower()] = True
        save_state(state)
    return jsonify({'ok': True, 'action': action, 'queued_image': state['queued_image']})


# ---------------------------------------------------------------------------
# Config (legacy device endpoint)
# ---------------------------------------------------------------------------

@api_bp.route('/config', methods=['GET'])
def api_config():
    cfg = load_config(); now = datetime.now()
    cfg['current_date'] = now.strftime('%Y-%m-%d')
    cfg['timestamp']    = int(now.timestamp())
    return jsonify(cfg)


@api_bp.route('/images', methods=['GET'])
def api_images():
    cfg        = load_config()
    caller_ip  = _caller_ip()
    all_files  = get_unified_index()
    mac        = _get_mac()
    dev_cfg    = None
    if mac:
        dev_cfg = next((d for d in cfg.get('devices', []) if d['mac'].lower() == mac), None)
    if not dev_cfg:
        dev_cfg = next((d for d in cfg.get('devices', []) if d.get('ip') == caller_ip), None)

    if dev_cfg:
        orientation = dev_cfg.get('orientation', 'landscape')
        suffix      = db.orient_suffix(orientation)
        active      = get_device_active_bases(dev_cfg['mac'], orientation)
        files       = [b + suffix for b in active
                       if os.path.exists(os.path.join(IMAGES_DIR, b + suffix))]
    else:
        files = all_files
    return jsonify(files)


# ---------------------------------------------------------------------------
# OTA update check
# ---------------------------------------------------------------------------

_github_cache = {'tag': None, 'assets': {}, 'last_updated': 0}


def _update_github_cache():
    import requests as py_requests
    now = time.time()
    if now - _github_cache['last_updated'] < 60:
        return
    repo = "finn-e/picframe-waveshare-ESP32-S3-PhotoPainter"
    url  = f"https://api.github.com/repos/{repo}/releases/latest"
    try:
        headers = {"Accept": "application/vnd.github+json"}
        token   = os.environ.get("GITHUB_TOKEN")
        if token:
            headers["Authorization"] = f"token {token}"
        r = py_requests.get(url, headers=headers, timeout=5)
        if r.status_code == 200:
            data   = r.json()
            tag    = data.get("tag_name", "").strip().lstrip('v')
            assets = {}
            for asset in data.get("assets", []):
                name = asset.get("name", "")
                if name.endswith(".zip"):
                    hp = name[:-4]; assets[hp] = asset.get("browser_download_url")
                    if '-' in hp: assets[hp.split('-', 1)[0]] = assets[hp]
            _github_cache.update({'tag': tag, 'assets': assets, 'last_updated': now})
            logger.info(f"GitHub Releases cache updated: {tag}")
    except Exception as e:
        logger.error(f"GitHub Releases API error: {e}")


def _get_update_url(hw_profile, current_version):
    try:
        _update_github_cache()
        tag = _github_cache['tag']
        def pv(v):
            try: return tuple(int(x) for x in v.split('.')[:3])
            except Exception: return (0, 0, 0)
        if tag and pv(tag) > pv(current_version):
            assets = _github_cache['assets']
            url = assets.get(hw_profile)
            if url is None:
                # Release zips are named after the firmware repo's board dirs
                # (old names). A device reporting a canonical name still needs
                # to match those assets — try every alias of this profile.
                canonical = normalize_device_type(hw_profile)
                for old, new in DEVICE_TYPE_ALIASES.items():
                    if new == canonical:
                        url = assets.get(old)
                        if url: break
            return url
    except Exception as e:
        logger.error(f"Error checking GitHub releases: {e}")
    return None


@api_bp.route('/update', methods=['GET'])
def api_update():
    hw      = request.args.get('hw', '').strip()
    version = request.args.get('version', '').strip()
    if not hw: return "Missing hw profile parameter", 400

    # Persist hw_profile so screen-type is known for artifact selection;
    # if it changed, trigger artifact conversion for this device's playlist.
    # Also update last_seen/device_ips and fw_version for authenticated devices.
    mac = _get_mac()
    if mac:
        changed = update_device_hw_profile(mac, hw)
        if changed:
            pid = get_device_playlist_id(mac)
            if pid is not None:
                from image import ensure_artifacts_for_playlist
                ensure_artifacts_for_playlist(pid)

        # Update firmware version from header if device is registered
        owner_id = get_device_owner_id(mac)
        if owner_id is not None:
            dev_cfg = None
            cfg = load_config(owner_id=owner_id)
            dev_cfg = next((d for d in cfg.get('devices', [])
                            if d['mac'].lower() == mac), None)
            if dev_cfg is not None:
                fw_ver_hdr = (request.headers.get('X-Firmware-Version', '') or '').strip()
                if fw_ver_hdr and fw_ver_hdr != (dev_cfg.get('fw_version') or ''):
                    update_device_fw_version(mac, fw_ver_hdr)
                with state_lock:
                    state = load_state()
                    state.setdefault('last_seen', {})[mac]  = int(time.time())
                    state.setdefault('device_ips', {})[mac] = _caller_ip()
                    save_state(state)

    url = _get_update_url(hw, version)
    return (url, 200) if url else ("", 204)


# ---------------------------------------------------------------------------
# Daily config  (called by device on boot)
# ---------------------------------------------------------------------------

def _daily_config_inner():
    dev_cfg, cfg, owner_id, err = _require_device('/frame-config')
    if err:
        return err
    mac = dev_cfg['mac'].lower()

    # Update fw_version from header if provided and changed
    fw_ver_hdr = (request.headers.get('X-Firmware-Version', '') or '').strip()
    if fw_ver_hdr and fw_ver_hdr != (dev_cfg.get('fw_version') or ''):
        update_device_fw_version(mac, fw_ver_hdr)

    orientation  = dev_cfg.get('orientation', 'landscape')
    pid          = get_device_playlist_id(mac)
    pl_settings  = get_playlist_settings(pid)
    sleep_interval = pl_settings['sleep_interval']

    # For playlist devices use entry titles; for non-playlist use base names
    if pid is not None:
        entries     = get_device_active_entries(mac, orientation)
        images_list = [e['title'] for e in entries]
    else:
        active_bases = get_device_active_bases(mac, orientation)
        images_list  = active_bases

    zip_version = hashlib.md5((','.join(images_list) + orientation).encode()).hexdigest()[:8]

    with state_lock:
        state = load_state()
        state.setdefault('last_seen', {})[mac]    = int(time.time())
        state.setdefault('device_ips', {})[mac]   = _caller_ip()
        save_state(state)

    return jsonify({
        'orientation':       orientation,
        'sleep_interval':    sleep_interval,
        'daily_zip_version': zip_version,
        'images':            images_list,
        'enabled':           {str(k): dict(v) for k, v in load_enabled().items()},
        'landscape_flipped': bool(dev_cfg.get('flip_l', False)),
        'portrait_flipped':  bool(dev_cfg.get('flip_p', False)),
        'debug':             bool(dev_cfg.get('debug', False)),
    })


@api_bp.route('/frame-config', methods=['GET'])
def device_daily_config():
    return _daily_config_inner()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _queued_idx_for(queued, mac, pool):
    """Return the index the queued image/entry occupies in this device's pool.

    *pool* is either:
      - list of base strings (general-pool / non-playlist devices), OR
      - list of entry dicts (playlist devices, each with key 'id').

    In-pool → pool.index(item), out-of-pool → len(pool) (appended by daily-zip).
    Returns None if the queue does not apply to this device.
    """
    if not queued:
        return None
    src = queued.get('source', '')
    if src != 'general' and src.lower() != mac:
        return None

    # Entry-based queue
    eid = queued.get('entry_id')
    if eid is not None:
        if pool and isinstance(pool[0], dict):
            pool_ids = [e['id'] for e in pool]
            return pool_ids.index(eid) if eid in pool_ids else len(pool)
        return len(pool)  # entry-queued but pool is base-based → append

    # Base-based queue (legacy)
    base = queued.get('base')
    if not base:
        return None
    if pool and isinstance(pool[0], dict):
        return len(pool)  # base-queued but pool is entry-based → append
    return pool.index(base) if base in pool else len(pool)


# ---------------------------------------------------------------------------
# Refresh  (device calls to get next image index)
# ---------------------------------------------------------------------------

def _refresh_inner():
    data   = request.get_json() or {}
    skip   = bool(data.get('skip', False))

    dev_cfg, cfg, owner_id, err = _require_device('/refresh')
    if err:
        return err
    mac = dev_cfg['mac'].lower()

    # Record battery level if provided
    battery = data.get('battery')
    if battery is not None:
        try:
            pct = int(battery)
            if 0 <= pct <= 100:
                record_battery(mac, pct)
        except (TypeError, ValueError):
            pass

    orientation  = dev_cfg.get('orientation', 'landscape')
    pid          = get_device_playlist_id(mac)
    pl_settings  = get_playlist_settings(pid)
    sleep_interval = pl_settings['sleep_interval']
    sync         = pl_settings['sync']
    shuffle      = pl_settings['shuffle']

    with state_lock:
        state   = load_state()
        now_ts  = int(time.time())
        state.setdefault('last_seen', {})[mac]  = now_ts
        state.setdefault('device_ips', {})[mac] = _caller_ip()

        # For pool-size calculations: use entry-based pool for playlist devices
        def _get_pool():
            if pid is not None:
                return get_device_active_entries(mac, orientation)
            return get_device_active_bases(mac, orientation)

        if pid is not None:
            # Playlist devices share one index pool (either synced or sequential/non-synced)
            p_indices = state.setdefault('playlist_indices', {})
            current_idx = p_indices.get(str(pid), 0)
            if skip:
                should_advance = True
                if sync:
                    # If sync is enabled, only advance once per interval to ensure all devices show the same image
                    p_advances = state.setdefault('playlist_last_advance', {})
                    last_info = p_advances.get(str(pid), {})
                    if not isinstance(last_info, dict):
                        last_info = {}
                    last_adv = last_info.get('ts', 0)
                    last_mac = last_info.get('mac', '')
                    cooldown = max(60, sleep_interval / 2)
                    if mac != last_mac and now_ts - last_adv < cooldown:
                        should_advance = False
                    else:
                        p_advances[str(pid)] = {'ts': now_ts, 'mac': mac}
                        state['playlist_last_advance'] = p_advances

                if should_advance:
                    pool = _get_pool()
                    n    = len(pool)
                    if shuffle and n > 1:
                        current_idx = (current_idx + random.randint(1, n - 1)) % n
                    else:
                        current_idx = (current_idx + 1) % n if n else 0
                    p_indices[str(pid)] = current_idx
                    state['playlist_indices'] = p_indices
        else:
            # Non-playlist devices (general pool) use per-device indices
            d_indices   = state.setdefault('device_indices', {})
            current_idx = d_indices.get(mac, 0)
            if skip:
                pool = _get_pool()
                n    = len(pool)
                if shuffle and n > 1:
                    current_idx = (current_idx + random.randint(1, n - 1)) % n
                else:
                    current_idx = (current_idx + 1) % n if n else 0
                d_indices[mac]          = current_idx
                state['device_indices'] = d_indices

        # Queued image: serve it if the device has already downloaded the updated zip.
        # If redownload is still True the device hasn't fetched since queueing, so the
        # appended index would be out of range on the device — leave the queue for next cycle.
        queued = state.get('queued_image')
        pool   = _get_pool()
        q_idx  = _queued_idx_for(queued, mac, pool)
        if q_idx is not None:
            redownload = state.get('redownload', {}).get(mac, False)
            if not redownload:
                current_idx              = q_idx
                state['queued_image']    = None
                # Store q_idx itself: firmware now sends skip=True on every
                # timer wake, so the NEXT poll will advance from q_idx to
                # q_idx+1 naturally. Out-of-pool queued images (q_idx ==
                # len(pool)) have no in-pool position to store; wrap to 0 so
                # the next skip lands on image 0.
                n = len(pool)
                store_idx = q_idx if 0 <= q_idx < n else 0
                if pid is not None:
                    state.setdefault('playlist_indices', {})[str(pid)] = store_idx
                else:
                    state.setdefault('device_indices', {})[mac]        = store_idx

        # Update device_images with the BMP filename for the status card preview
        pool = _get_pool()
        if pool and 0 <= current_idx < len(pool):
            orient_char = 'p' if orientation == 'portrait' else 'l'
            hw_profile  = dev_cfg.get('hw_profile', '')
            scr_w, scr_h = screen_size_for_profile(hw_profile)
            if pid is not None:
                entry = pool[current_idx]
                state.setdefault('device_images', {})[mac] = f"pe{entry['id']}_{scr_w}x{scr_h}_{orient_char}.bmp"
            else:
                base = pool[current_idx]
                state.setdefault('device_images', {})[mac] = f"{base}_{orient_char}.bmp"
        else:
            state.setdefault('device_images', {})[mac] = None

        save_state(state)

    return jsonify({
        'image_index':        current_idx,
        'current_orientation': orientation,
        'sleep_interval':     sleep_interval,
    })


@api_bp.route('/refresh', methods=['POST'])
def device_refresh():
    return _refresh_inner()


@api_bp.route('/battery-history/<mac>', methods=['GET'])
def battery_history(mac):
    days = request.args.get('days', 7)
    try:
        days = int(days)
    except (TypeError, ValueError):
        days = 7
    since_ts = int(time.time()) - days * 86400
    history = get_battery_history(mac, since_ts=since_ts)
    return jsonify({"mac": mac.lower(), "history": history})


# ---------------------------------------------------------------------------
# Daily ZIP  (device downloads image bundle)
# ---------------------------------------------------------------------------

def _daily_zip_inner():
    dev_cfg, cfg, owner_id, err = _require_device('/image-zip')
    if err:
        return err
    mac = dev_cfg['mac'].lower()

    with state_lock:
        state = load_state()
        force_redownload = state.get('redownload', {}).get(mac, False)
        state.setdefault('redownload', {})[mac] = False
        save_state(state)

    orientation  = dev_cfg.get('orientation', 'portrait')
    hw_profile   = dev_cfg.get('hw_profile', '')
    pid          = get_device_playlist_id(mac)
    pl_settings  = get_playlist_settings(pid)

    orient_char  = 'l' if orientation == 'landscape' else 'p'
    flip         = dev_cfg.get('flip_l', False) if orientation == 'landscape' else dev_cfg.get('flip_p', False)
    scr_w, scr_h = screen_size_for_profile(hw_profile)
    infix        = _artifact_infix(scr_w, scr_h)
    flip_char    = 'f' if flip else 'u'
    zip_suffix   = f'_{orient_char}.bin'

    serializable_cfg = {
        "timer":             pl_settings['sleep_interval'],
        "shuffle":           pl_settings['shuffle'],
        "sync_images":       pl_settings['sync'],
        "enabled":           {str(k): dict(v) for k, v in load_enabled().items()},
        "landscape_flipped": bool(dev_cfg.get('flip_l', False)),
        "portrait_flipped":  bool(dev_cfg.get('flip_p', False)),
    }

    queued    = state.get('queued_image')
    q_applies = queued and (queued.get('source') == 'general' or
                            queued.get('source', '').lower() == mac)

    if pid is not None:
        # ---- Entry-based path (playlist devices) — uses per-device zip cache ----
        entries           = get_device_active_entries(mac, orientation)
        candidate_entries = list(entries)
        entry_ids_in_pool = {e['id'] for e in entries}

        if q_applies and queued.get('entry_id') is not None:
            q_eid = queued['entry_id']
            if q_eid not in entry_ids_in_pool:
                q_entry = get_playlist_entry(q_eid)
                if q_entry:
                    candidate_entries.append(q_entry)

        # zip_version derived from the canonical pool (not including queued extras),
        # matching what _daily_config_inner sends so device can compare.
        entry_titles = [e['title'] for e in entries]
        zip_version  = hashlib.md5((','.join(entry_titles) + orientation).encode()).hexdigest()[:8]

        client_version = request.args.get('version', '').strip()
        if not force_redownload and client_version and client_version == zip_version:
            return Response(status=304)

        zip_bytes, _ = serve_cached_or_build(
            mac, candidate_entries, orient_char, flip_char, scr_w, scr_h,
            serializable_cfg, force=force_redownload,
        )
        return _zip_response(zip_bytes, zip_version, mac)

    else:
        # ---- Legacy base-based path (non-playlist devices) — built on-demand ----
        active_bases = get_device_active_bases(mac, orientation)
        store_suffix = f'{infix}_{orient_char}_{flip_char}.bin'

        zip_version    = hashlib.md5((','.join(active_bases) + orientation).encode()).hexdigest()[:8]
        client_version = request.args.get('version', '').strip()
        if not force_redownload and client_version and client_version == zip_version:
            return Response(status=304)

        candidates = list(active_bases)
        if q_applies and queued.get('base'):
            q_base = queued['base']
            if q_base not in candidates:
                candidates.append(q_base)

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, mode='w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
            zf.writestr('config.json', json.dumps(serializable_cfg, indent=2))
            zip_names = [b + zip_suffix for b in candidates]
            manifest  = json.dumps(zip_names, indent=2)
            zf.writestr('index.json', manifest)
            zf.writestr('list.json',  manifest)
            for base in candidates:
                ensure_bin_files_for_screen(base, scr_w, scr_h)
                bin_path = os.path.join(IMAGES_DIR, base + store_suffix)
                if os.path.exists(bin_path):
                    zf.write(bin_path, arcname=base + zip_suffix)

        zip_bytes = buf.getvalue()
        return _zip_response(zip_bytes, zip_version, mac)


def _zip_response(zip_bytes, zip_version, mac):
    """Return a zip Response and record that this device received this version."""
    with state_lock:
        state = load_state()
        state.setdefault('device_zip_versions', {})[mac] = zip_version
        save_state(state)
    return Response(
        zip_bytes,
        mimetype='application/zip',
        headers={
            'Content-Disposition': 'attachment; filename="daily.zip"',
            'Content-Length': str(len(zip_bytes)),
        },
    )


@api_bp.route('/image-zip', methods=['GET'])
def device_daily_zip():
    return _daily_zip_inner()

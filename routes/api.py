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
    update_device_hw_profile, get_device_owner_id,
)
from image import ensure_bin_files, ensure_bin_files_for_screen, screen_size_for_profile, _artifact_infix

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

@api_bp.route('/api/register', methods=['POST'])
def api_register():
    data     = request.get_json() or {}
    mac      = data.get('mac', '').strip().lower()
    username = data.get('username', '').strip()
    password = data.get('password', '').strip()
    if not mac:
        return jsonify({'error': 'mac required'}), 400

    secret = current_app.secret_key
    device_token = hmac.new(secret.encode(), mac.encode(), hashlib.sha256).hexdigest()[:32]

    from db import check_user_password, get_user_by_username
    if password != device_token:
        # Fresh registration requires the credentials of a real user account.
        if not username or not check_user_password(username, password):
            return jsonify({'error': 'invalid credentials'}), 403

    owner_id = None
    if username:
        user = get_user_by_username(username)
        if user:
            owner_id = user['id']
    if owner_id is None:
        # Token-authenticated re-registration keeps the existing owner.
        owner_id = get_device_owner_id(mac)
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

    return jsonify({'token': device_token})


# ---------------------------------------------------------------------------
# Orientation change (device-initiated)
# ---------------------------------------------------------------------------

@api_bp.route('/api/change-orientation', methods=['POST'])
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
    trigger_redownload(dev_cfg['mac'].lower())
    save_config(cfg, owner_id=owner_id)
    return jsonify({'ok': True, 'orientation': server_orient})


# ---------------------------------------------------------------------------
# Queue push
# ---------------------------------------------------------------------------

@api_bp.route('/api/queue', methods=['POST'])
def queue_image_api():
    data   = request.get_json() or {}
    base   = data.get('base')
    source = data.get('source')
    if not base or not source:
        return jsonify({'ok': False, 'error': 'Missing base or source'}), 400
    with state_lock:
        state = load_state()
        current_queued = state.get('queued_image')
        if current_queued and current_queued.get('base') == base and current_queued.get('source') == source:
            state['queued_image'] = None; action = 'dequeued'
        else:
            state['queued_image'] = {'base': base, 'source': source}; action = 'queued'
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

@api_bp.route('/api/config', methods=['GET'])
def api_config():
    cfg = load_config(); now = datetime.now()
    cfg['current_date'] = now.strftime('%Y-%m-%d')
    cfg['timestamp']    = int(now.timestamp())
    return jsonify(cfg)


@api_bp.route('/api/images', methods=['GET'])
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
            return _github_cache['assets'].get(hw_profile)
    except Exception as e:
        logger.error(f"Error checking GitHub releases: {e}")
    return None


@api_bp.route('/api/update', methods=['GET'])
@api_bp.route('/update', methods=['GET'])
def api_update():
    hw      = request.args.get('hw', '').strip()
    version = request.args.get('version', '').strip()
    if not hw: return "Missing hw profile parameter", 400

    # Persist hw_profile so screen-type is known for artifact selection;
    # if it changed, trigger artifact conversion for this device's playlist.
    mac = _get_mac()
    if mac:
        changed = update_device_hw_profile(mac, hw)
        if changed:
            pid = get_device_playlist_id(mac)
            if pid is not None:
                from image import ensure_artifacts_for_playlist
                ensure_artifacts_for_playlist(pid)

    url = _get_update_url(hw, version)
    return (url, 200) if url else ("", 204)


# ---------------------------------------------------------------------------
# Daily config  (called by device on boot)
# ---------------------------------------------------------------------------

def _daily_config_inner():
    dev_cfg, cfg, owner_id, err = _require_device('/daily-config')
    if err:
        return err
    mac = dev_cfg['mac'].lower()

    orientation  = dev_cfg.get('orientation', 'landscape')
    pid          = get_device_playlist_id(mac)
    pl_settings  = get_playlist_settings(pid)
    sleep_interval = pl_settings['sleep_interval']
    active_bases   = get_device_active_bases(mac, orientation)
    zip_version    = hashlib.md5((','.join(active_bases) + orientation).encode()).hexdigest()[:8]

    with state_lock:
        state = load_state()
        state.setdefault('last_seen', {})[mac]    = int(time.time())
        state.setdefault('device_ips', {})[mac]   = _caller_ip()
        save_state(state)

    return jsonify({
        'orientation':       orientation,
        'sleep_interval':    sleep_interval,
        'daily_zip_version': zip_version,
        'images':            active_bases,
        'enabled':           {str(k): dict(v) for k, v in load_enabled().items()},
        'landscape_flipped': bool(dev_cfg.get('flip_l', False)),
        'portrait_flipped':  bool(dev_cfg.get('flip_p', False)),
    })


@api_bp.route('/api/daily-config', methods=['GET'])
@api_bp.route('/daily-config',     methods=['GET'])
def device_daily_config():
    return _daily_config_inner()


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

        if sync and pid is not None:
            # All devices in playlist share one index
            p_indices  = state.setdefault('playlist_indices', {})
            current_idx = p_indices.get(str(pid), 0)
            if skip:
                pool = get_device_active_bases(mac, orientation)
                n    = len(pool)
                if shuffle and n > 1:
                    # advance to a different random position
                    current_idx = (current_idx + random.randint(1, n - 1)) % n
                else:
                    current_idx = (current_idx + 1) % n if n else 0
                p_indices[str(pid)] = current_idx
                state['playlist_indices'] = p_indices
        else:
            d_indices   = state.setdefault('device_indices', {})
            current_idx = d_indices.get(mac, 0)
            if skip:
                pool = get_device_active_bases(mac, orientation)
                n    = len(pool)
                if shuffle and n > 1:
                    current_idx = (current_idx + random.randint(1, n - 1)) % n
                else:
                    current_idx = (current_idx + 1) % n if n else 0
                d_indices[mac]          = current_idx
                state['device_indices'] = d_indices

        save_state(state)

    return jsonify({
        'image_index':        current_idx,
        'current_orientation': orientation,
        'sleep_interval':     sleep_interval,
    })


@api_bp.route('/api/refresh', methods=['POST'])
@api_bp.route('/refresh',     methods=['POST'])
def device_refresh():
    return _refresh_inner()


@api_bp.route('/api/battery-history/<mac>', methods=['GET'])
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
    caller_ip  = _caller_ip()
    dev_cfg, cfg, owner_id, err = _require_device('/daily-zip')
    if err:
        return err
    mac = dev_cfg['mac'].lower()

    mac_lower = mac
    with state_lock:
        state = load_state()
        state.setdefault('redownload', {})[mac_lower] = False
        save_state(state)

    orientation    = dev_cfg.get('orientation', 'portrait')
    hw_profile     = dev_cfg.get('hw_profile', '')
    pid            = get_device_playlist_id(mac_lower)
    pl_settings    = get_playlist_settings(pid)
    active_bases   = get_device_active_bases(mac_lower, orientation)

    orient_char  = 'l' if orientation == 'landscape' else 'p'
    flip         = dev_cfg.get('flip_l', False) if orientation == 'landscape' else dev_cfg.get('flip_p', False)
    scr_w, scr_h = screen_size_for_profile(hw_profile)
    infix        = _artifact_infix(scr_w, scr_h)
    # Flip not supported for non-standard screens (would require proper rotation);
    # fall through to unflipped for safety.
    effective_flip = flip if not infix else False
    store_suffix = f'{infix}_{orient_char}_{"f" if effective_flip else "u"}.bin'
    zip_suffix   = f'_{orient_char}.bin'  # arcname inside zip is always _l.bin/_p.bin

    zip_version    = hashlib.md5((','.join(active_bases) + orientation).encode()).hexdigest()[:8]
    client_version = request.args.get('version', '').strip()
    if client_version and client_version == zip_version:
        return Response(status=304)

    candidates = list(active_bases)
    queued = state.get('queued_image')
    if queued and (queued.get('source') == 'general' or
                   queued.get('source', '').lower() == mac_lower):
        q_base = queued.get('base')
        if q_base and q_base not in candidates:
            candidates.append(q_base)

    serializable_cfg = {
        "timer":             pl_settings['sleep_interval'],
        "shuffle":           pl_settings['shuffle'],
        "sync_images":       pl_settings['sync'],
        "enabled":           {str(k): dict(v) for k, v in load_enabled().items()},
        "landscape_flipped": bool(dev_cfg.get('flip_l', False)),
        "portrait_flipped":  bool(dev_cfg.get('flip_p', False)),
    }

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

    size = buf.tell(); buf.seek(0)
    return Response(buf, mimetype='application/zip',
                    headers={'Content-Disposition': 'attachment; filename="daily.zip"',
                             'Content-Length': str(size)})


@api_bp.route('/api/daily-zip', methods=['GET'])
@api_bp.route('/daily-zip',     methods=['GET'])
def device_daily_zip():
    return _daily_zip_inner()

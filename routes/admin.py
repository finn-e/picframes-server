import logging
import os

from flask import Blueprint, jsonify, redirect, request, session, url_for

import db
from db import (
    load_config, save_config, load_enabled, save_enabled,
    load_crops, save_crops, load_image_order, save_image_order,
    load_playlists, load_playlist, create_playlist, update_playlist, delete_playlist,
    get_playlist_images, add_playlist_image, remove_playlist_image, reorder_playlist_images,
    get_device_playlist_id, set_device_playlist,
    get_global_setting, set_global_setting,
    create_user, delete_user,
    trigger_redownload, flags, ORIGINALS_DIR, IMAGES_DIR, ALLOWED_EXTENSIONS,
    LANDSCAPE_SUFFIX, PORTRAIT_SUFFIX,
)
from image import (
    convert_image, ensure_artifacts_for_playlist,
    reconvert_all_intelligent, reconvert_for_playlist_screen,
    delete_all_artifacts,
)

logger = logging.getLogger(__name__)

admin_bp = Blueprint('admin', __name__)


# ---------------------------------------------------------------------------
# Upload / convert
# ---------------------------------------------------------------------------

@admin_bp.route('/upload', methods=['POST'])
def upload_file():
    for file in request.files.getlist('files'):
        if not file.filename: continue
        _, ext = os.path.splitext(file.filename)
        if ext.lower() not in ALLOWED_EXTENSIONS: continue
        original_path = os.path.join(ORIGINALS_DIR, file.filename)
        file.save(original_path)
        base, _ = os.path.splitext(file.filename)
        convert_image(original_path, base)
    return redirect(url_for('ui.index'))


@admin_bp.route('/convert/<filename>', methods=['POST'])
def convert_file(filename):
    original_path = os.path.join(ORIGINALS_DIR, filename)
    if not os.path.exists(original_path): return "File not found", 404
    base, _ = os.path.splitext(filename)
    if convert_image(original_path, base):
        return redirect(url_for('ui.index'))
    return "Conversion failed", 500


@admin_bp.route('/convert_all', methods=['POST'])
def convert_all():
    """Intelligent reconvert: delete all artifacts, then rebuild only for the
    screen types required by each image's playlists.  Pool-only images end up
    with no converted artifacts.  Crops are honoured."""
    reconvert_all_intelligent()
    trigger_redownload()
    return redirect(url_for('ui.index'))


@admin_bp.route('/convert_for_playlist/<int:pid>/<filename>', methods=['POST'])
def convert_file_for_playlist(pid, filename):
    """Per-image reconvert scoped to a playlist's screen types.
    Deletes only the screen-type-specific artifacts then reconverts from original."""
    base = os.path.splitext(filename)[0]
    if not os.path.exists(os.path.join(ORIGINALS_DIR, filename)):
        return "File not found", 404
    if reconvert_for_playlist_screen(base, pid):
        trigger_redownload()
        return redirect(url_for('ui.index'))
    return "Conversion failed", 500


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------

@admin_bp.route('/delete/<filename>', methods=['POST'])
def delete_file(filename):
    base, _ = os.path.splitext(filename)
    # Remove all converted artifacts via centralised helper, then the original
    delete_all_artifacts(base)
    orig_path = os.path.join(ORIGINALS_DIR, filename)
    if os.path.exists(orig_path):
        os.remove(orig_path)

    order = load_image_order()
    if base in order: order.remove(base); save_image_order(order)
    enabled = load_enabled(); enabled.pop(base, None); save_enabled(enabled)
    crops   = load_crops();   crops.pop(base, None);   save_crops(crops)
    trigger_redownload()
    return redirect(url_for('ui.index'))


# ---------------------------------------------------------------------------
# Rename
# ---------------------------------------------------------------------------

@admin_bp.route('/rename', methods=['POST'])
def rename_image():
    data = request.get_json()
    old_base = data.get('old_base', '').strip()
    new_base = data.get('new_base', '').strip()
    if not old_base or not new_base:
        return jsonify({'ok': False, 'error': 'Missing name'}), 400
    new_base = new_base.replace(' ', '_')
    if len(new_base) > 78:
        return jsonify({'ok': False, 'error': 'Title too long'}), 400

    for f in os.listdir(ORIGINALS_DIR):
        base, ext = os.path.splitext(f)
        if base == old_base:
            os.rename(os.path.join(ORIGINALS_DIR, f),
                      os.path.join(ORIGINALS_DIR, new_base + ext))
            break

    for old_f, new_f in [
        (old_base + LANDSCAPE_SUFFIX,          new_base + LANDSCAPE_SUFFIX),
        (old_base + PORTRAIT_SUFFIX,           new_base + PORTRAIT_SUFFIX),
        (old_base + '_l_u.bin',                new_base + '_l_u.bin'),
        (old_base + '_l_f.bin',                new_base + '_l_f.bin'),
        (old_base + '_p_u.bin',                new_base + '_p_u.bin'),
        (old_base + '_p_f.bin',                new_base + '_p_f.bin'),
        (old_base + '_dithered.png',           new_base + '_dithered.png'),
        # 13.3" artifacts
        (old_base + '_1600x1200_l.bmp',        new_base + '_1600x1200_l.bmp'),
        (old_base + '_1600x1200_p.bmp',        new_base + '_1600x1200_p.bmp'),
        (old_base + '_1600x1200_l_u.bin',      new_base + '_1600x1200_l_u.bin'),
        (old_base + '_1600x1200_l_f.bin',      new_base + '_1600x1200_l_f.bin'),
        (old_base + '_1600x1200_p_u.bin',      new_base + '_1600x1200_p_u.bin'),
        (old_base + '_1600x1200_p_f.bin',      new_base + '_1600x1200_p_f.bin'),
    ]:
        old_p = os.path.join(IMAGES_DIR, old_f)
        new_p = os.path.join(IMAGES_DIR, new_f)
        if os.path.exists(old_p): os.rename(old_p, new_p)

    order = load_image_order()
    if old_base in order:
        order[order.index(old_base)] = new_base
        save_image_order(order)

    enabled = load_enabled()
    if old_base in enabled:
        enabled[new_base] = enabled.pop(old_base)
        save_enabled(enabled)

    crops = load_crops()
    if old_base in crops:
        crops[new_base] = crops.pop(old_base)
        save_crops(crops)

    # Update playlist_images references
    try:
        conn = db.get_db()
        conn.execute("UPDATE playlist_images SET base=? WHERE base=?", (new_base, old_base))
        conn.commit(); conn.close()
    except Exception as e:
        logger.error(f"rename playlist_images: {e}")

    trigger_redownload()
    return jsonify({'ok': True, 'new_base': new_base})


# ---------------------------------------------------------------------------
# Enable/disable orientations
# ---------------------------------------------------------------------------

@admin_bp.route('/toggle_orient', methods=['POST'])
def toggle_orient():
    data   = request.get_json()
    base   = data.get('base')
    orient = data.get('orient')
    val    = bool(data.get('enabled', True))
    if not base or orient not in ('l', 'p', 'title'):
        return jsonify({'ok': False}), 400
    enabled = load_enabled()
    f = flags(enabled, base); f[orient] = val
    enabled[base] = f; save_enabled(enabled); trigger_redownload()
    return jsonify({'ok': True})


# ---------------------------------------------------------------------------
# Caption
# ---------------------------------------------------------------------------

@admin_bp.route('/update_caption', methods=['POST'])
def update_caption():
    data = request.get_json() or {}
    base = data.get('base')
    mode = data.get('caption_mode')
    desc = data.get('description')
    if not base: return jsonify({'ok': False, 'error': 'Missing base'}), 400
    enabled = load_enabled()
    f = flags(enabled, base)
    if mode is not None: f['caption_mode'] = mode
    if desc is not None: f['description']  = desc
    enabled[base] = f; save_enabled(enabled); trigger_redownload()
    return jsonify({'ok': True})


# ---------------------------------------------------------------------------
# Reorder (general pool)
# ---------------------------------------------------------------------------

@admin_bp.route('/reorder', methods=['POST'])
def reorder():
    order = request.get_json().get('order', [])
    save_image_order(order); trigger_redownload()
    return jsonify({'ok': True})


# ---------------------------------------------------------------------------
# Recrop
# ---------------------------------------------------------------------------

@admin_bp.route('/recrop', methods=['POST'])
def recrop():
    data   = request.get_json()
    base   = data.get('base')
    orient = data.get('orient')
    offset = data.get('offset')
    if not base or orient not in ('l', 'p') or offset is None:
        return jsonify({'ok': False, 'error': 'Invalid parameters'}), 400
    crops = load_crops()
    if base not in crops: crops[base] = {"l": 0.5, "p": 0.5}
    crops[base][orient] = max(0.0, min(1.0, float(offset)))
    save_crops(crops)

    original_name = None
    for f in os.listdir(ORIGINALS_DIR):
        if os.path.splitext(f)[0] == base: original_name = f; break
    if not original_name:
        return jsonify({'ok': False, 'error': 'Original not found'}), 404

    from image import convert_image_for_screen, get_screen_types_for_image, _DEFAULT_SCREEN
    src = os.path.join(ORIGINALS_DIR, original_name)
    # Always reconvert 800×480 (used for pool previews / dithered thumbnail)
    sizes = get_screen_types_for_image(base) or {_DEFAULT_SCREEN}
    ok = all(convert_image_for_screen(src, base, w, h) for w, h in sizes)
    if ok:
        trigger_redownload()
        return jsonify({'ok': True})
    return jsonify({'ok': False, 'error': 'Re-conversion failed'}), 500


# ---------------------------------------------------------------------------
# Device management
# ---------------------------------------------------------------------------

@admin_bp.route('/device_orientation', methods=['POST'])
def device_orientation():
    data        = request.get_json()
    mac         = data.get('mac')
    orientation = data.get('orientation')
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


@admin_bp.route('/device_rename', methods=['POST'])
def device_rename():
    data     = request.get_json()
    mac      = data.get('mac')
    new_name = data.get('name', '').strip()
    if not mac or not new_name:
        return jsonify({'ok': False, 'error': 'Missing fields'}), 400
    cfg = load_config()
    for dev in cfg.get('devices', []):
        if dev['mac'].lower() == mac.lower():
            dev['name'] = new_name; break
    save_config(cfg)
    return jsonify({'ok': True})


@admin_bp.route('/device_debug', methods=['GET', 'POST'])
def device_debug():
    if request.method == 'POST':
        if request.is_json:
            data    = request.get_json() or {}
            mac     = data.get('mac')
            dbg_val = bool(data.get('debug', False))
        else:
            mac     = request.form.get('mac')
            dbg_val = request.form.get('debug') in ('1', 'true', 'True')
    else:
        mac     = request.args.get('mac')
        dbg_val = request.args.get('debug') in ('1', 'true', 'True')
    if not mac: return jsonify({'ok': False, 'error': 'mac required'}), 400
    cfg = load_config(); device_found = False
    for dev in cfg.get('devices', []):
        if dev['mac'].lower() == mac.lower():
            dev['debug'] = dbg_val; device_found = True; break
    if not device_found: return jsonify({'ok': False, 'error': 'device not found'}), 404
    save_config(cfg)
    return jsonify({'ok': True, 'debug': dbg_val})


@admin_bp.route('/device_flip', methods=['POST'])
def device_flip():
    data        = request.get_json() or {}
    mac         = data.get('mac')
    orient_type = data.get('orient')
    flip_val    = bool(data.get('flip', False))
    if not mac or orient_type not in ('l', 'p'):
        return jsonify({'ok': False, 'error': 'Missing parameters'}), 400
    cfg = load_config(); device_found = False
    for dev in cfg.get('devices', []):
        if dev['mac'].lower() == mac.lower():
            key = 'flip_l' if orient_type == 'l' else 'flip_p'
            dev[key] = flip_val; device_found = True; trigger_redownload(mac); break
    if not device_found: return jsonify({'ok': False, 'error': 'device not found'}), 404
    save_config(cfg)
    return jsonify({'ok': True, 'flip': flip_val})


@admin_bp.route('/device_playlist', methods=['POST'])
def device_playlist_assign():
    data  = request.get_json() or {}
    mac   = data.get('mac', '').strip().lower()
    pid   = data.get('playlist_id')
    if not mac: return jsonify({'ok': False, 'error': 'mac required'}), 400
    set_device_playlist(mac, int(pid) if pid is not None else None)
    # Trigger conversion for all screen types now present in this playlist
    if pid is not None:
        ensure_artifacts_for_playlist(int(pid))
    trigger_redownload(mac)
    return jsonify({'ok': True})


# ---------------------------------------------------------------------------
# Playlist CRUD
# ---------------------------------------------------------------------------

@admin_bp.route('/playlists/create', methods=['POST'])
def playlist_create():
    data = request.get_json() or {}
    name = data.get('name', 'New Playlist').strip() or 'New Playlist'
    pid  = create_playlist(name,
                           shuffle=data.get('shuffle', False),
                           sync=data.get('sync', True),
                           sleep_interval=data.get('sleep_interval', 900))
    return jsonify({'ok': True, 'id': pid})


@admin_bp.route('/playlists/<int:pid>/rename', methods=['POST'])
def playlist_rename(pid):
    data = request.get_json() or {}
    name = data.get('name', '').strip()
    if not name: return jsonify({'ok': False, 'error': 'Empty name'}), 400
    update_playlist(pid, name=name)
    return jsonify({'ok': True})


@admin_bp.route('/playlists/<int:pid>/settings', methods=['POST'])
def playlist_settings(pid):
    data = request.get_json() or {}
    kwargs = {}
    if 'shuffle'        in data: kwargs['shuffle']        = 1 if data['shuffle'] else 0
    if 'sync'           in data: kwargs['sync']           = 1 if data['sync'] else 0
    if 'sleep_interval' in data: kwargs['sleep_interval'] = int(data['sleep_interval'])
    update_playlist(pid, **kwargs)
    return jsonify({'ok': True})


@admin_bp.route('/playlists/<int:pid>/delete', methods=['POST'])
def playlist_delete(pid):
    delete_playlist(pid)
    # If deleted playlist was the default, clear that setting
    if get_global_setting('default_playlist_id') == str(pid):
        set_global_setting('default_playlist_id', '')
    return jsonify({'ok': True})


@admin_bp.route('/playlists/<int:pid>/add_image', methods=['POST'])
def playlist_add_image(pid):
    data = request.get_json() or {}
    base = data.get('base', '').strip()
    if not base: return jsonify({'ok': False, 'error': 'Missing base'}), 400
    images = get_playlist_images(pid)
    if len(images) >= 10:
        return jsonify({'ok': False, 'error': 'Playlist is full (maximum 10 images)'}), 400
    add_playlist_image(pid, base)
    # Optionally disable in general pool
    if data.get('disable_in_pool', True):
        enabled = load_enabled()
        f = flags(enabled, base); f['l'] = False; f['p'] = False
        enabled[base] = f; save_enabled(enabled)
    # Trigger conversion for all screen types present in this playlist
    ensure_artifacts_for_playlist(pid)
    trigger_redownload()
    return jsonify({'ok': True})


@admin_bp.route('/playlists/<int:pid>/remove_image', methods=['POST'])
def playlist_remove_image(pid):
    data = request.get_json() or {}
    base = data.get('base', '').strip()
    if not base: return jsonify({'ok': False, 'error': 'Missing base'}), 400
    remove_playlist_image(pid, base)
    trigger_redownload()
    return jsonify({'ok': True})


@admin_bp.route('/playlists/<int:pid>/reorder', methods=['POST'])
def playlist_reorder(pid):
    data  = request.get_json() or {}
    bases = data.get('bases', [])
    reorder_playlist_images(pid, bases)
    return jsonify({'ok': True})


# ---------------------------------------------------------------------------
# Global settings
# ---------------------------------------------------------------------------

@admin_bp.route('/settings/default_playlist', methods=['POST'])
def settings_default_playlist():
    data = request.get_json() or {}
    pid  = data.get('playlist_id')
    set_global_setting('default_playlist_id', str(pid) if pid is not None else '')
    return jsonify({'ok': True})


# ---------------------------------------------------------------------------
# User management (admin only)
# ---------------------------------------------------------------------------

def _require_admin():
    if not session.get('is_admin', session.get('authenticated')):
        return jsonify({'ok': False, 'error': 'Forbidden'}), 403
    return None


@admin_bp.route('/users/create', methods=['POST'])
def user_create():
    err = _require_admin()
    if err: return err
    data     = request.get_json() or {}
    username = data.get('username', '').strip()
    password = data.get('password', '')
    is_admin = bool(data.get('is_admin', False))
    if not username or not password:
        return jsonify({'ok': False, 'error': 'Username and password required'}), 400
    uid = create_user(username, password, is_admin)
    if uid is None:
        return jsonify({'ok': False, 'error': 'Username already exists'}), 409
    return jsonify({'ok': True, 'id': uid})


@admin_bp.route('/users/<int:uid>/delete', methods=['POST'])
def user_delete(uid):
    err = _require_admin()
    if err: return err
    if uid == session.get('user_id'):
        return jsonify({'ok': False, 'error': 'Cannot delete yourself'}), 400
    delete_user(uid)
    return jsonify({'ok': True})

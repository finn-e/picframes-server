# ==========================================================================================
# DESCRIPTION: Administration REST and form endpoints. Handles user accounts, custom playlists, image sequencing, and trigger rebuilds.
# DEPENDENCIES: Flask, db, image
# ==========================================================================================
import io
import logging
import os
import threading
import time

import numpy as np
from flask import Blueprint, Response, jsonify, redirect, request, session, url_for
from PIL import Image, ImageOps

import db
from db import (
    load_config, save_config, load_enabled, save_enabled,
    load_crops, save_crops, load_image_order, save_image_order,
    load_playlists, load_playlist, create_playlist, update_playlist, delete_playlist,
    get_playlist_images, add_playlist_image, remove_playlist_image, reorder_playlist_images,
    get_device_playlist_id, set_device_playlist, get_playlist_devices,
    get_global_setting, set_global_setting,
    create_user, delete_user,
    trigger_redownload, flags, ORIGINALS_DIR, IMAGES_DIR, ALLOWED_EXTENSIONS,
    LANDSCAPE_SUFFIX, PORTRAIT_SUFFIX,
    get_image_edits, save_image_edits,
    # Per-entry data model
    get_playlist_entry, get_playlist_entries,
    add_playlist_entry, update_playlist_entry, remove_playlist_entry,
    rename_playlist_entry, reorder_playlist_entries, save_playlist_entry_edits,
    get_image_by_uuid, get_or_create_image, get_image_by_filename,
    sanitize_title,
    # Resolution helpers
    get_device_resolution, get_playlist_resolution,
    set_playlist_resolution, clear_playlist_resolution,
    # Friendships & Sharing
    is_playlist_accessible, add_friend_by_code, get_friends,
    share_playlist, unshare_playlist, get_playlist_collaborators,
)
from image import (
    convert_image, ensure_artifacts_for_playlist,
    reconvert_all_intelligent, reconvert_for_playlist_screen,
    delete_all_artifacts, delete_entry_artifacts,
    apply_color_adjustments, dither_floyd_steinberg, PALETTE,
    get_screen_types_for_image, _DEFAULT_SCREEN, convert_image_for_screen,
    convert_entry_for_screen, ensure_entry_bin_files,
    _default_landscape_crop_img, _default_portrait_crop_img, _crop_with_outfill,
    screen_size_for_profile, get_screen_types_for_playlist,
    entry_artifacts_ready,
)

logger = logging.getLogger(__name__)

admin_bp = Blueprint('admin', __name__)


def _check_entry_access(entry_id):
    entry = get_playlist_entry(entry_id)
    if not entry:
        return None, False, False
    return entry, True, is_playlist_accessible(entry['playlist_id'], session.get('user_id', 1))


# ---------------------------------------------------------------------------
# Upload / convert
# ---------------------------------------------------------------------------

def _convert_in_background(saved):
    for original_path, base in saved:
        try:
            convert_image(original_path, base)
        except Exception as e:
            logger.error(f"background convert_image({base}): {e}")


@admin_bp.route('/upload', methods=['POST'])
def upload_file():
    playlist_id = request.form.get('playlist_id')
    pid = None
    if playlist_id is not None:
        try:
            pid = int(playlist_id)
            if not is_playlist_accessible(pid, session.get('user_id', 1)):
                return jsonify({'ok': False, 'error': 'Access denied'}), 403
        except (ValueError, TypeError):
            pid = None

    saved = []
    for file in request.files.getlist('files'):
        if not file.filename: continue
        _, ext = os.path.splitext(file.filename)
        if ext.lower() not in ALLOWED_EXTENSIONS: continue
        original_path = os.path.join(ORIGINALS_DIR, file.filename)
        file.save(original_path)
        base, _ = os.path.splitext(file.filename)
        saved.append((original_path, base))

    if not saved:
        if pid is not None:
            return jsonify({'ok': False, 'error': 'No valid files uploaded'}), 400
        return redirect(url_for('ui.index'))

    # Always add to the general pool order
    order = load_image_order()
    changed = False
    for _, base in saved:
        if base not in order:
            order.append(base); changed = True
    if changed:
        save_image_order(order)

    if pid is not None:
        # Upload + add to playlist path: return JSON
        entries_added = []
        errors = []
        for original_path, base in saved:
            entries = get_playlist_entries(pid)
            if len(entries) >= 10:
                errors.append(f'{base}: Playlist is full (maximum 10 images)')
                continue
            image_uuid = get_or_create_image(base)
            if not image_uuid:
                errors.append(f'{base}: Could not register image')
                continue
            entry_id = add_playlist_entry(pid, image_uuid, title=base)
            if not entry_id:
                errors.append(f'{base}: Could not create playlist entry')
                continue
            add_playlist_image(pid, base)
            # Disable in general pool (consistent with drag-add behaviour)
            enabled = load_enabled()
            f = flags(enabled, base); f['l'] = False; f['p'] = False
            enabled[base] = f; save_enabled(enabled)
            entries_added.append({'base': base, 'entry_id': entry_id})
            # Background: generate entry artifacts
            def _bg_playlist(eid=entry_id, p=pid, op=original_path, b=base):
                try:
                    convert_image(op, b)
                    from image import convert_entry_for_screen, ensure_artifacts_for_playlist
                    convert_entry_for_screen(eid)
                    ensure_artifacts_for_playlist(p)
                    trigger_redownload()
                except Exception as e:
                    logger.error(f'upload playlist bg({eid}): {e}')
            threading.Thread(target=_bg_playlist, daemon=True).start()

        if errors and not entries_added:
            return jsonify({'ok': False, 'error': '; '.join(errors)}), 400
        return jsonify({'ok': True, 'added': entries_added, 'errors': errors})

    # Normal pool-only upload: background convert, redirect
    threading.Thread(target=_convert_in_background, args=(saved,), daemon=True).start()
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

    # Update playlist_images references (legacy table)
    try:
        conn = db.get_db()
        conn.execute("UPDATE playlist_images SET base=? WHERE base=?", (new_base, old_base))
        conn.commit(); conn.close()
    except Exception as e:
        logger.error(f"rename playlist_images: {e}")

    # Update images table original_filename
    try:
        conn = db.get_db()
        # Find the new filename on disk (ext may vary)
        new_orig_fn = None
        for f in os.listdir(ORIGINALS_DIR):
            if os.path.splitext(f)[0] == new_base:
                new_orig_fn = f; break
        if new_orig_fn:
            conn.execute("UPDATE images SET original_filename=? WHERE original_filename LIKE ?",
                         (new_orig_fn, old_base + '.%'))
        conn.commit(); conn.close()
    except Exception as e:
        logger.error(f"rename images table: {e}")

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


@admin_bp.route('/device_repair', methods=['POST'])
def device_repair():
    data = request.get_json() or {}
    mac  = (data.get('mac') or '').strip().lower()
    if not mac:
        return jsonify({'ok': False, 'error': 'mac required'}), 400
    cfg = load_config(); device_found = False
    for dev in cfg.get('devices', []):
        if dev['mac'].lower() == mac:
            dev['repair_until'] = int(time.time()) + 600
            device_found = True; break
    if not device_found:
        return jsonify({'ok': False, 'error': 'device not found'}), 404
    save_config(cfg)
    repair_until = next(d['repair_until'] for d in cfg['devices'] if d['mac'].lower() == mac)
    return jsonify({'ok': True, 'repair_until': repair_until})


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


def _device_effective_resolution(mac):
    """Return the 'WxH' resolution for *mac*, deriving it from hw_profile if needed.

    Priority: explicit resolution column > hw_profile type default > 800x480.
    Always returns a non-None string.
    """
    res = get_device_resolution(mac)
    if res:
        return res
    # Derive from hw_profile stored in the devices table
    try:
        conn = db.get_db()
        row = conn.execute("SELECT hw_profile FROM devices WHERE mac=?",
                           (mac.lower(),)).fetchone()
        conn.close()
        hw = row['hw_profile'] if row else ''
    except Exception:
        hw = ''
    scr_w, scr_h = screen_size_for_profile(hw or '')
    return f'{scr_w}x{scr_h}'


@admin_bp.route('/device_playlist', methods=['POST'])
def device_playlist_assign():
    data  = request.get_json() or {}
    mac   = data.get('mac', '').strip().lower()
    pid   = data.get('playlist_id')
    if not mac: return jsonify({'ok': False, 'error': 'mac required'}), 400

    if pid is not None:
        pid = int(pid)
        dev_res      = _device_effective_resolution(mac)
        playlist_res = get_playlist_resolution(pid)

        if playlist_res is None:
            # Playlist is unlocked: lock it to this device's resolution.
            set_playlist_resolution(pid, dev_res)
        elif playlist_res != dev_res:
            # Resolution mismatch — refuse the assignment.
            return jsonify({
                'ok': False,
                'error': (f'Resolution mismatch: playlist is locked to {playlist_res} '
                          f'but this device uses {dev_res}'),
            }), 409

        set_device_playlist(mac, pid)
        # Trigger conversion for all screen types now present in this playlist
        ensure_artifacts_for_playlist(pid)
    else:
        # Unassigning: remove from current playlist, then clear that playlist's
        # resolution lock if this was the last device in it.
        old_pid = get_device_playlist_id(mac)
        set_device_playlist(mac, None)
        if old_pid is not None:
            remaining = get_playlist_devices(old_pid)
            if not remaining:
                clear_playlist_resolution(old_pid)

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
    if not is_playlist_accessible(pid, session.get('user_id', 1)):
        return jsonify({'ok': False, 'error': 'Access denied'}), 403
    data = request.get_json() or {}
    name = data.get('name', '').strip()
    if not name: return jsonify({'ok': False, 'error': 'Empty name'}), 400
    update_playlist(pid, name=name)
    return jsonify({'ok': True})


@admin_bp.route('/playlists/<int:pid>/settings', methods=['POST'])
def playlist_settings(pid):
    if not is_playlist_accessible(pid, session.get('user_id', 1)):
        return jsonify({'ok': False, 'error': 'Access denied'}), 403
    data = request.get_json() or {}
    kwargs = {}
    if 'shuffle'        in data: kwargs['shuffle']        = 1 if data['shuffle'] else 0
    if 'sync'           in data: kwargs['sync']           = 1 if data['sync'] else 0
    if 'sleep_interval' in data: kwargs['sleep_interval'] = int(data['sleep_interval'])
    update_playlist(pid, **kwargs)
    return jsonify({'ok': True})


@admin_bp.route('/playlists/<int:pid>/delete', methods=['POST'])
def playlist_delete(pid):
    playlist = load_playlist(pid)
    if not playlist or playlist['owner_id'] != session.get('user_id', 1):
        return jsonify({'ok': False, 'error': 'Access denied'}), 403
    delete_playlist(pid)
    # If deleted playlist was the default, clear that setting
    if get_global_setting('default_playlist_id') == str(pid):
        set_global_setting('default_playlist_id', '')
    return jsonify({'ok': True})


@admin_bp.route('/playlists/<int:pid>/add_image', methods=['POST'])
def playlist_add_image(pid):
    if not is_playlist_accessible(pid, session.get('user_id', 1)):
        return jsonify({'ok': False, 'error': 'Access denied'}), 403
    data = request.get_json() or {}
    base = data.get('base', '').strip()
    if not base: return jsonify({'ok': False, 'error': 'Missing base'}), 400
    entries = get_playlist_entries(pid)
    if len(entries) >= 10:
        return jsonify({'ok': False, 'error': 'Playlist is full (maximum 10 images)'}), 400

    # Ensure images row exists for this base
    image_uuid = get_or_create_image(base)
    if not image_uuid:
        return jsonify({'ok': False, 'error': 'Original file not found'}), 404

    # Create playlist_entry with default neutral settings; title defaults to base
    entry_id = add_playlist_entry(pid, image_uuid, title=base)
    if not entry_id:
        return jsonify({'ok': False, 'error': 'Could not create entry'}), 500

    # Also keep legacy playlist_images row for backward compat / rollback
    add_playlist_image(pid, base)

    # Optionally disable in general pool
    if data.get('disable_in_pool', True):
        enabled = load_enabled()
        f = flags(enabled, base); f['l'] = False; f['p'] = False
        enabled[base] = f; save_enabled(enabled)

    # Background: generate entry artifacts + old screen-typed artifacts
    def _bg_convert(eid):
        try:
            convert_entry_for_screen(eid)
            ensure_artifacts_for_playlist(pid)
            trigger_redownload()
        except Exception as e:
            logger.error(f"playlist_add_image bg({eid}): {e}")

    threading.Thread(target=_bg_convert, args=(entry_id,), daemon=True).start()
    return jsonify({'ok': True, 'entry_id': entry_id})


@admin_bp.route('/playlists/<int:pid>/remove_image', methods=['POST'])
def playlist_remove_image(pid):
    if not is_playlist_accessible(pid, session.get('user_id', 1)):
        return jsonify({'ok': False, 'error': 'Access denied'}), 403
    data = request.get_json() or {}
    base = data.get('base', '').strip()
    if not base: return jsonify({'ok': False, 'error': 'Missing base'}), 400
    remove_playlist_image(pid, base)
    trigger_redownload()
    return jsonify({'ok': True})


@admin_bp.route('/playlists/<int:pid>/remove_entry/<int:entry_id>', methods=['POST'])
def playlist_remove_entry(pid, entry_id):
    entry = get_playlist_entry(entry_id)
    if not entry or entry['playlist_id'] != pid:
        return jsonify({'ok': False, 'error': 'Entry not found'}), 404
    if not is_playlist_accessible(pid, session.get('user_id', 1)):
        return jsonify({'ok': False, 'error': 'Access denied'}), 403
    delete_entry_artifacts(entry_id)
    remove_playlist_entry(entry_id)
    trigger_redownload()
    return jsonify({'ok': True})


@admin_bp.route('/playlists/<int:pid>/rename_entry/<int:entry_id>', methods=['POST'])
def playlist_rename_entry(pid, entry_id):
    entry = get_playlist_entry(entry_id)
    if not entry or entry['playlist_id'] != pid:
        return jsonify({'ok': False, 'error': 'Entry not found'}), 404
    if not is_playlist_accessible(pid, session.get('user_id', 1)):
        return jsonify({'ok': False, 'error': 'Access denied'}), 403
    data = request.get_json() or {}
    new_title = data.get('title', '').strip()
    if not new_title:
        return jsonify({'ok': False, 'error': 'Empty title'}), 400
    safe_title = rename_playlist_entry(entry_id, new_title)
    if safe_title is None:
        return jsonify({'ok': False, 'error': 'Rename failed'}), 500
    # Trigger reconversion (bin arcname changes with title)
    def _bg(eid):
        try:
            delete_entry_artifacts(eid)
            convert_entry_for_screen(eid)
            trigger_redownload()
        except Exception as e:
            logger.error(f"rename_entry bg({eid}): {e}")
    threading.Thread(target=_bg, args=(entry_id,), daemon=True).start()
    return jsonify({'ok': True, 'title': safe_title})


@admin_bp.route('/playlists/<int:pid>/reorder_entries', methods=['POST'])
def playlist_reorder_entries(pid):
    if not is_playlist_accessible(pid, session.get('user_id', 1)):
        return jsonify({'ok': False, 'error': 'Access denied'}), 403
    data      = request.get_json() or {}
    entry_ids = data.get('entry_ids', [])
    reorder_playlist_entries(pid, entry_ids)
    return jsonify({'ok': True})


@admin_bp.route('/playlists/<int:pid>/reorder', methods=['POST'])
def playlist_reorder(pid):
    if not is_playlist_accessible(pid, session.get('user_id', 1)):
        return jsonify({'ok': False, 'error': 'Access denied'}), 403
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


# ---------------------------------------------------------------------------
# Non-destructive image editor endpoints
# ---------------------------------------------------------------------------

@admin_bp.route('/image-edit/<base>', methods=['GET'])
def image_edit_get(base):
    """Return saved non-destructive edit params as JSON (or defaults if none)."""
    edits = get_image_edits(base)
    if edits is None:
        return jsonify({
            'crop_l': None, 'crop_p': None,
            'hue_shift': 0, 'saturation': 1, 'value_adj': 1,
            'r_gain': 1, 'g_gain': 1, 'b_gain': 1,
            'bg_color': '#ffffff', 'rotate': 0,
        })
    result = {
        'hue_shift': edits.get('hue_shift', 0) or 0,
        'saturation': edits.get('saturation', 1) or 1,
        'value_adj': edits.get('value_adj', 1) or 1,
        'r_gain': edits.get('r_gain', 1) or 1,
        'g_gain': edits.get('g_gain', 1) or 1,
        'b_gain': edits.get('b_gain', 1) or 1,
        'bg_color': edits.get('bg_color', '#ffffff') or '#ffffff',
        'rotate': int(edits.get('rotate', 0) or 0),
        'crop_l': (
            {'x': edits['crop_l_x'], 'y': edits['crop_l_y'],
             'w': edits['crop_l_w'], 'h': edits['crop_l_h']}
            if edits.get('crop_l_w') is not None else None
        ),
        'crop_p': (
            {'x': edits['crop_p_x'], 'y': edits['crop_p_y'],
             'w': edits['crop_p_w'], 'h': edits['crop_p_h']}
            if edits.get('crop_p_w') is not None else None
        ),
    }
    return jsonify(result)


@admin_bp.route('/image-edit/<base>', methods=['POST'])
def image_edit_save(base):
    """Persist edit params for base and trigger background reconversion."""
    data = request.get_json() or {}
    save_image_edits(base, data)

    original_name = None
    for f in os.listdir(ORIGINALS_DIR):
        if os.path.splitext(f)[0] == base:
            original_name = f; break
    if not original_name:
        return jsonify({'ok': False, 'error': 'Original not found'}), 404

    src = os.path.join(ORIGINALS_DIR, original_name)

    def _bg():
        try:
            delete_all_artifacts(base)
            sizes = get_screen_types_for_image(base) or {_DEFAULT_SCREEN}
            for w, h in sizes:
                convert_image_for_screen(src, base, w, h)
            trigger_redownload()
        except Exception as e:
            logger.error(f"image_edit_save bg convert({base}): {e}")

    threading.Thread(target=_bg, daemon=True).start()
    return jsonify({'ok': True})


@admin_bp.route('/entry-toggle-orient/<int:entry_id>', methods=['POST'])
def entry_toggle_orient(entry_id):
    """Toggle enabled_l or enabled_p for a playlist entry."""
    entry, exists, allowed = _check_entry_access(entry_id)
    if not exists:
        return jsonify({'ok': False, 'error': 'Not found'}), 404
    if not allowed:
        return jsonify({'ok': False, 'error': 'Access denied'}), 403
    data   = request.get_json() or {}
    orient = data.get('orient')
    val    = bool(data.get('enabled', True))
    if orient not in ('l', 'p'):
        return jsonify({'ok': False, 'error': 'Invalid orient'}), 400
    col = 'enabled_l' if orient == 'l' else 'enabled_p'
    update_playlist_entry(entry_id, **{col: 1 if val else 0})
    trigger_redownload()
    return jsonify({'ok': True})


@admin_bp.route('/entry-edit/<int:entry_id>', methods=['GET'])
def entry_edit_get(entry_id):
    """Return saved edit params for a playlist entry, or defaults if neutral."""
    entry, exists, allowed = _check_entry_access(entry_id)
    if not exists:
        return jsonify({'error': 'Not found'}), 404
    if not allowed:
        return jsonify({'error': 'Access denied'}), 403
    result = {
        'hue_shift':  float(entry.get('hue_shift',  0) or 0),
        'saturation': float(entry.get('saturation',  1) or 1),
        'value_adj':  float(entry.get('value_adj',   1) or 1),
        'r_gain':     float(entry.get('r_gain',      1) or 1),
        'g_gain':     float(entry.get('g_gain',      1) or 1),
        'b_gain':     float(entry.get('b_gain',      1) or 1),
        'bg_color':   entry.get('bg_color', '#ffffff') or '#ffffff',
        'rotate':     int(entry.get('rotate', 0) or 0),
        'crop_l': (
            {'x': entry['crop_l_x'], 'y': entry['crop_l_y'],
             'w': entry['crop_l_w'], 'h': entry['crop_l_h']}
            if entry.get('crop_l_w') is not None else None
        ),
        'crop_p': (
            {'x': entry['crop_p_x'], 'y': entry['crop_p_y'],
             'w': entry['crop_p_w'], 'h': entry['crop_p_h']}
            if entry.get('crop_p_w') is not None else None
        ),
        'crop_l43': (
            {'x': entry['crop_l43_x'], 'y': entry['crop_l43_y'],
             'w': entry['crop_l43_w'], 'h': entry['crop_l43_h']}
            if entry.get('crop_l43_w') is not None else None
        ),
        'crop_p34': (
            {'x': entry['crop_p34_x'], 'y': entry['crop_p34_y'],
             'w': entry['crop_p34_w'], 'h': entry['crop_p34_h']}
            if entry.get('crop_p34_w') is not None else None
        ),
    }
    entry_screen_types = get_screen_types_for_playlist(entry['playlist_id'])
    result['screen_ratios'] = list({
        '5x3' if (w, h) == (800, 480) else '4x3'
        for w, h in entry_screen_types
    }) or ['5x3']
    return jsonify(result)


@admin_bp.route('/entry-edit/<int:entry_id>', methods=['POST'])
def entry_edit_save(entry_id):
    """Persist edit params for an entry and trigger background reconversion."""
    entry, exists, allowed = _check_entry_access(entry_id)
    if not exists:
        return jsonify({'ok': False, 'error': 'Not found'}), 404
    if not allowed:
        return jsonify({'ok': False, 'error': 'Access denied'}), 403
    data = request.get_json() or {}
    save_playlist_entry_edits(entry_id, data)

    def _bg(eid):
        try:
            delete_entry_artifacts(eid)
            convert_entry_for_screen(eid)
            trigger_redownload()
        except Exception as e:
            logger.error(f"entry_edit_save bg({eid}): {e}")

    threading.Thread(target=_bg, args=(entry_id,), daemon=True).start()
    return jsonify({'ok': True})


@admin_bp.route('/entry-artifact-status/<int:entry_id>', methods=['GET'])
def entry_artifact_status(entry_id):
    entry, exists, allowed = _check_entry_access(entry_id)
    if not exists:
        return jsonify({'error': 'Not found'}), 404
    if not allowed:
        return jsonify({'error': 'Access denied'}), 403
    sizes = get_screen_types_for_playlist(entry['playlist_id'])
    w, h = next(iter(sizes)) if sizes else (800, 480)
    ready = entry_artifacts_ready(entry_id, w, h)
    return jsonify({'ready': ready})


@admin_bp.route('/entry-reconvert/<int:entry_id>', methods=['POST'])
def entry_reconvert(entry_id):
    """Delete and regenerate artifacts for a playlist entry."""
    entry, exists, allowed = _check_entry_access(entry_id)
    if not exists:
        return jsonify({'ok': False, 'error': 'Not found'}), 404
    if not allowed:
        return jsonify({'ok': False, 'error': 'Access denied'}), 403

    def _bg(eid):
        try:
            delete_entry_artifacts(eid)
            convert_entry_for_screen(eid)
            trigger_redownload()
        except Exception as e:
            logger.error(f"entry_reconvert bg({eid}): {e}")

    threading.Thread(target=_bg, args=(entry_id,), daemon=True).start()
    return jsonify({'ok': True})


@admin_bp.route('/entry-preview/<int:entry_id>', methods=['POST'])
def entry_preview(entry_id):
    """Return a dithered preview PNG for a playlist entry using its current params."""
    entry, exists, allowed = _check_entry_access(entry_id)
    if not exists:
        return '', 404
    if not allowed:
        return '', 403
    image = get_image_by_uuid(entry['image_uuid'])
    if not image:
        return '', 404
    src_path = None
    for f in os.listdir(ORIGINALS_DIR):
        if f == image['original_filename']:
            src_path = os.path.join(ORIGINALS_DIR, f); break
    if not src_path:
        return '', 404

    data = request.get_json() or {}
    try:
        img = ImageOps.exif_transpose(Image.open(src_path)).convert('RGB')
        rotate_deg = int(data.get('rotate', entry.get('rotate', 0) or 0)) % 360
        if rotate_deg == 90:
            img = img.transpose(Image.Transpose.ROTATE_90)
        elif rotate_deg == 180:
            img = img.transpose(Image.Transpose.ROTATE_180)
        elif rotate_deg == 270:
            img = img.transpose(Image.Transpose.ROTATE_270)

        crop_orient = data.get('crop_orient')
        crop_key = 'crop_l' if crop_orient != 'p' else 'crop_p'
        crop = data.get(crop_key) or data.get('crop_l')
        if crop and crop.get('w') and crop.get('h'):
            w_img, h_img = img.size
            cx = max(0, int(crop['x'])); cy = max(0, int(crop['y']))
            cw = min(int(crop['w']), w_img - cx)
            ch = min(int(crop['h']), h_img - cy)
            if cw > 0 and ch > 0:
                img = img.crop((cx, cy, cx + cw, cy + ch))

        max_dim = 800
        w_o, h_o = img.size
        if max(w_o, h_o) > max_dim:
            sc = max_dim / max(w_o, h_o)
            img = img.resize((int(w_o * sc), int(h_o * sc)), Image.Resampling.LANCZOS)

        img = apply_color_adjustments(
            img,
            hue_shift=float(data.get('hue_shift', entry.get('hue_shift', 0) or 0)),
            saturation=float(data.get('saturation', entry.get('saturation', 1) or 1)),
            value_adj=float(data.get('value_adj', entry.get('value_adj', 1) or 1)),
            r_gain=float(data.get('r_gain', entry.get('r_gain', 1) or 1)),
            g_gain=float(data.get('g_gain', entry.get('g_gain', 1) or 1)),
            b_gain=float(data.get('b_gain', entry.get('b_gain', 1) or 1)),
        )
        dithered = dither_floyd_steinberg(np.array(img, dtype=np.float32), PALETTE)
        buf = io.BytesIO()
        Image.fromarray(dithered).save(buf, format='PNG')
        buf.seek(0)
        return Response(buf.read(), mimetype='image/png')
    except Exception as e:
        logger.error(f"entry_preview({entry_id}): {e}")
        return '', 500


@admin_bp.route('/image-preview/<base>', methods=['POST'])
def image_preview(base):
    """Return a dithered preview PNG (full image, no crop) with current colour params applied."""
    data = request.get_json() or {}

    src_path = None
    for f in os.listdir(ORIGINALS_DIR):
        if os.path.splitext(f)[0] == base:
            src_path = os.path.join(ORIGINALS_DIR, f); break
    if not src_path:
        return '', 404

    try:
        img = ImageOps.exif_transpose(Image.open(src_path)).convert('RGB')

        # Apply rotation before everything else (same order as convert_image)
        rotate_deg = int(data.get('rotate', 0) or 0) % 360
        if rotate_deg == 90:
            img = img.transpose(Image.Transpose.ROTATE_90)
        elif rotate_deg == 180:
            img = img.transpose(Image.Transpose.ROTATE_180)
        elif rotate_deg == 270:
            img = img.transpose(Image.Transpose.ROTATE_270)

        # Apply crop if provided
        crop_orient = data.get('crop_orient')  # 'l' or 'p' — used by preview button
        crop_key = 'crop_l' if crop_orient != 'p' else 'crop_p'
        crop = data.get(crop_key) or data.get('crop_l')
        if crop and crop.get('w') and crop.get('h'):
            w_img, h_img = img.size
            cx, cy = int(crop['x']), int(crop['y'])
            cw, ch = int(crop['w']), int(crop['h'])
            cx, cy = max(0, cx), max(0, cy)
            cw = min(cw, w_img - cx)
            ch = min(ch, h_img - cy)
            if cw > 0 and ch > 0:
                img = img.crop((cx, cy, cx + cw, cy + ch))

        # Downscale to at most 800px on the long side for speed
        w_orig, h_orig = img.size
        max_dim = 800
        if max(w_orig, h_orig) > max_dim:
            if w_orig >= h_orig:
                img = img.resize((max_dim, int(h_orig * max_dim / w_orig)),
                                 Image.Resampling.LANCZOS)
            else:
                img = img.resize((int(w_orig * max_dim / h_orig), max_dim),
                                 Image.Resampling.LANCZOS)

        # Apply colour adjustments
        img = apply_color_adjustments(
            img,
            hue_shift=float(data.get('hue_shift', 0) or 0),
            saturation=float(data.get('saturation', 1) or 1),
            value_adj=float(data.get('value_adj', 1) or 1),
            r_gain=float(data.get('r_gain', 1) or 1),
            g_gain=float(data.get('g_gain', 1) or 1),
            b_gain=float(data.get('b_gain', 1) or 1),
        )

        # Dither with the 6-colour palette
        dithered = dither_floyd_steinberg(np.array(img, dtype=np.float32), PALETTE)
        result_img = Image.fromarray(dithered)

        buf = io.BytesIO()
        result_img.save(buf, format='PNG')
        buf.seek(0)
        return Response(buf.read(), mimetype='image/png')
    except Exception as e:
        logger.error(f"image_preview({base}): {e}")
        return '', 500


# ---------------------------------------------------------------------------
# Friendships & Sharing
# ---------------------------------------------------------------------------

@admin_bp.route('/friends/add', methods=['POST'])
def friends_add():
    uid = session.get('user_id', 1)
    data = request.get_json() or {}
    code = data.get('code', '').strip()
    if not code:
        return jsonify({'ok': False, 'error': 'Missing friend code'}), 400
    ok, result = add_friend_by_code(uid, code)
    if not ok:
        return jsonify({'ok': False, 'error': result}), 400
    return jsonify({'ok': True, 'username': result})


@admin_bp.route('/friends', methods=['GET'])
def friends_list():
    uid = session.get('user_id', 1)
    friends = get_friends(uid)
    return jsonify({'ok': True, 'friends': friends})


@admin_bp.route('/playlists/<int:pid>/share', methods=['POST'])
def playlist_share_endpoint(pid):
    uid = session.get('user_id', 1)
    playlist = load_playlist(pid)
    if not playlist or playlist['owner_id'] != uid:
        return jsonify({'ok': False, 'error': 'Only the playlist owner can share it'}), 403
    
    data = request.get_json() or {}
    friend_id = data.get('friend_id')
    if friend_id is None:
        return jsonify({'ok': False, 'error': 'Missing friend_id'}), 400
        
    friends = get_friends(uid)
    if not any(f['id'] == int(friend_id) for f in friends):
        return jsonify({'ok': False, 'error': 'User is not in your friends list'}), 400
        
    ok = share_playlist(pid, int(friend_id))
    return jsonify({'ok': ok})


@admin_bp.route('/playlists/<int:pid>/unshare', methods=['POST'])
def playlist_unshare_endpoint(pid):
    uid = session.get('user_id', 1)
    playlist = load_playlist(pid)
    if not playlist or playlist['owner_id'] != uid:
        return jsonify({'ok': False, 'error': 'Only the playlist owner can unshare it'}), 403
    
    data = request.get_json() or {}
    friend_id = data.get('friend_id')
    if friend_id is None:
        return jsonify({'ok': False, 'error': 'Missing friend_id'}), 400
        
    ok = unshare_playlist(pid, int(friend_id))
    return jsonify({'ok': ok})


@admin_bp.route('/playlists/<int:pid>/collaborators', methods=['GET'])
def playlist_collaborators_endpoint(pid):
    uid = session.get('user_id', 1)
    playlist = load_playlist(pid)
    if not playlist or playlist['owner_id'] != uid:
        return jsonify({'ok': False, 'error': 'Access denied'}), 403
        
    collabs = get_playlist_collaborators(pid)
    friends = get_friends(uid)
    return jsonify({'ok': True, 'collaborators': collabs, 'friends': friends})

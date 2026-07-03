import logging
import os
import time

from flask import Blueprint, redirect, render_template, request, send_from_directory, session, url_for

import db
from db import (
    load_config, load_state, load_image_order, load_enabled, load_crops,
    load_playlists, get_playlist_images, get_playlist_devices,
    get_device_playlist_id, get_global_setting,
    check_user_password, get_user_by_username, list_users,
    flags, ORIGINALS_DIR, IMAGES_DIR, LANDSCAPE_SUFFIX, PORTRAIT_SUFFIX,
    SHARE_DIR, get_latest_battery,
)
from image import ensure_dithered_original

from PIL import Image

logger = logging.getLogger(__name__)

ui_bp = Blueprint('ui', __name__)

LOGIN_HTML = """<!DOCTYPE html>
<html>
<head>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>PicFrames Login</title>
  <style>
    @import url('https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;600;700&display=swap');
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: 'Outfit', sans-serif;
      background: radial-gradient(circle at center, hsl(220, 30%, 12%), hsl(220, 35%, 6%));
      color: hsl(220, 20%, 94%);
      min-height: 100vh;
      display: flex; align-items: center; justify-content: center; padding: 24px;
    }
    .card {
      background: rgba(30, 41, 59, 0.75); backdrop-filter: blur(20px);
      border: 1px solid rgba(255,255,255,0.09); padding: 40px; border-radius: 22px;
      width: 100%; max-width: 400px; box-shadow: 0 24px 48px rgba(0,0,0,0.55);
    }
    h2 {
      font-weight: 700; font-size: 2rem; margin-bottom: 8px;
      background: linear-gradient(135deg, hsl(190,100%,55%), hsl(260,90%,65%));
      -webkit-background-clip: text; -webkit-text-fill-color: transparent; text-align: center;
    }
    .sub { text-align: center; font-size: 0.9rem; color: hsl(220,15%,58%); margin-bottom: 24px; }
    .error {
      color: hsl(0,85%,65%); background: rgba(239,68,68,0.12);
      border: 1px solid rgba(239,68,68,0.2); padding: 12px; border-radius: 10px;
      margin-bottom: 20px; font-size: 0.85rem; text-align: center;
    }
    label { display: block; font-size: 0.85rem; color: hsl(220,12%,68%); margin-bottom: 6px; font-weight: 500; }
    .input-group { margin-bottom: 20px; }
    input[type=text], input[type=password] {
      width: 100%; padding: 12px 14px; background: rgba(15,23,42,0.6);
      border: 1px solid rgba(255,255,255,0.11); border-radius: 10px;
      color: white; font-size: 0.95rem; transition: all 0.2s;
    }
    input[type=text]:focus, input[type=password]:focus { outline: none; border-color: hsl(190,100%,55%); }
    input[type=submit] {
      width: 100%; padding: 14px; border: none; border-radius: 10px;
      background: linear-gradient(135deg, hsl(190,100%,45%), hsl(260,90%,55%));
      color: white; font-size: 1rem; font-weight: 600; cursor: pointer;
      margin-top: 8px; box-shadow: 0 4px 12px rgba(56,189,248,0.2);
    }
  </style>
</head>
<body>
  <div class="card">
    <h2>PicFrames</h2>
    <div class="sub">Sign in to manage your frames</div>
    {% if error %}<div class="error">{{ error }}</div>{% endif %}
    <form method="POST">
      <div class="input-group">
        <label>Username</label>
        <input type="text" name="username" required placeholder="Username" autofocus autocomplete="username">
      </div>
      <div class="input-group">
        <label>Password</label>
        <input type="password" name="password" required placeholder="Password" autocomplete="current-password">
      </div>
      <input type="submit" value="Sign In">
    </form>
  </div>
</body>
</html>"""


@ui_bp.route('/login', methods=['GET', 'POST'])
def login():
    from flask import render_template_string
    if session.get('authenticated'):
        return redirect(url_for('ui.index'))
    error = None
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        if username and check_user_password(username, password):
            user = get_user_by_username(username)
            session['authenticated'] = True
            session['username'] = user['username']
            session['is_admin'] = bool(user['is_admin'])
            session['user_id'] = user['id']
            session.permanent = True
            return redirect(url_for('ui.index'))
        error = "Invalid username or password"
    return render_template_string(LOGIN_HTML, error=error)


@ui_bp.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('ui.login'))


@ui_bp.route('/originals/<path:filename>')
def serve_original(filename):
    safe = os.path.normpath(filename)
    return send_from_directory(os.path.join(SHARE_DIR, 'originals'), safe)


@ui_bp.route('/images/<path:filename>')
def serve_image(filename):
    safe = os.path.normpath(filename)
    full = os.path.join(IMAGES_DIR, safe)
    if os.path.isfile(full):
        return send_from_directory(IMAGES_DIR, safe)
    orig_full = os.path.join(ORIGINALS_DIR, safe)
    if os.path.isfile(orig_full):
        return send_from_directory(ORIGINALS_DIR, safe)
    return '', 404


@ui_bp.route('/')
def index():
    from flask import current_app
    config    = load_config()
    state     = load_state()
    order     = load_image_order()
    enabled   = load_enabled()
    crops     = load_crops()
    playlists = load_playlists()
    default_pid = get_global_setting('default_playlist_id')

    # Build playlist image sets and device membership
    playlist_image_sets = {}
    playlist_device_macs = {}
    for pl in playlists:
        playlist_image_sets[pl['id']]   = set(get_playlist_images(pl['id']))
        playlist_device_macs[pl['id']]  = set(get_playlist_devices(pl['id']))

    images = []
    for base in order:
        original_name = None
        for f in os.listdir(ORIGINALS_DIR):
            if os.path.splitext(f)[0] == base: original_name = f; break
        if original_name is None: continue

        has_l = os.path.exists(os.path.join(IMAGES_DIR, base + LANDSCAPE_SUFFIX))
        has_p = os.path.exists(os.path.join(IMAGES_DIR, base + PORTRAIT_SUFFIX))
        f     = flags(enabled, base)
        ensure_dithered_original(base)

        try:
            with Image.open(os.path.join(ORIGINALS_DIR, original_name)) as img_obj:
                orig_w, orig_h = img_obj.size
        except Exception:
            orig_w, orig_h = 800, 480

        crop_offsets = crops.get(base, {"l": 0.5, "p": 0.5})

        # Which playlists contain this image?
        in_playlists = [pl['id'] for pl in playlists
                        if base in playlist_image_sets.get(pl['id'], set())]

        images.append({
            'base': base, 'original_name': original_name,
            'has_l': has_l, 'has_p': has_p,
            'l_on': f["l"], 'p_on': f["p"], 'title_on': f.get("title", False),
            'caption_mode': f.get("caption_mode", 'none'),
            'description':  f.get("description", ''),
            'orig_w': orig_w, 'orig_h': orig_h,
            'offset_l': crop_offsets.get("l", 0.5),
            'offset_p': crop_offsets.get("p", 0.5),
            'in_playlists': in_playlists,
        })

    image_by_base = {img['base']: img for img in images}
    now_ts        = int(time.time())
    node_status   = state.get('last_seen', {})
    device_ips    = state.get('device_ips', {})
    device_images = state.get('device_images', {})

    # Device → playlist mapping for template
    device_playlist_ids = {
        dev['mac'].lower(): get_device_playlist_id(dev['mac'])
        for dev in config.get('devices', []) if dev.get('mac')
    }

    # Battery: most recent reading per device
    device_macs = [dev['mac'].lower() for dev in config.get('devices', []) if dev.get('mac')]
    device_battery = get_latest_battery(device_macs)

    current_user = {
        'username': session.get('username', 'admin'),
        'is_admin': session.get('is_admin', True),
        'user_id':  session.get('user_id'),
    }

    return render_template(
        'index.html',
        images=images,
        config=config,
        state=state,
        node_status=node_status,
        device_ips=device_ips,
        device_images=device_images,
        now_ts=now_ts,
        image_by_base=image_by_base,
        version=current_app.config.get('SERVER_VERSION', '0.0.0'),
        playlists=playlists,
        playlist_image_sets={str(k): list(v) for k, v in playlist_image_sets.items()},
        playlist_device_macs={str(k): list(v) for k, v in playlist_device_macs.items()},
        device_playlist_ids=device_playlist_ids,
        default_playlist_id=default_pid,
        current_user=current_user,
        users=list_users() if current_user['is_admin'] else [],
        device_battery=device_battery,
    )

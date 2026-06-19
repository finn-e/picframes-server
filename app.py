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
from PIL import Image
import numpy as np
from flask import Flask, request, jsonify, render_template_string, send_from_directory, redirect, url_for

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)

SHARE_DIR     = os.environ.get('SHARE_DIR', '/share')
ORIGINALS_DIR = os.path.join(SHARE_DIR, 'originals')
IMAGES_DIR    = os.path.join(SHARE_DIR, 'images')   # flat: base_l.bmp / base_p.bmp

for d in (SHARE_DIR, ORIGINALS_DIR, IMAGES_DIR):
    os.makedirs(d, exist_ok=True)

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
# Config
# ---------------------------------------------------------------------------

def get_config_path(): return os.path.join(SHARE_DIR, 'config.json')

def load_config():
    defaults = {
        "timer": 900, "wake_timeout": 45,
        "devices": [], "shuffle": False, "sync_images": False,
    }
    path = get_config_path()
    if os.path.exists(path):
        try:
            with open(path) as f: data = json.load(f)
            for k, v in defaults.items(): data.setdefault(k, v)
            # Ensure every device configuration has a default debug flag value
            for dev in data["devices"]:
                dev.setdefault("debug", False)
            return data
        except Exception as e: logger.error(f"Config load: {e}")
    return defaults

def save_config(cfg):
    try:
        with open(get_config_path(), 'w') as f: json.dump(cfg, f, indent=2)
        return True
    except Exception as e: logger.error(f"Config save: {e}"); return False

# ---------------------------------------------------------------------------
# Server state  (3-phase wakeup)
# ---------------------------------------------------------------------------

PHASE_GATHERING = 'GATHERING'
PHASE_READY     = 'READY'
PHASE_CHANGE    = 'CHANGE'

def get_state_path(): return os.path.join(SHARE_DIR, 'server_state.json')

def load_state():
    defaults = {
        "current_index": 0, "last_sync_ts": 0, "last_change_ts": 0,
        "phase": PHASE_GATHERING,
        "phase_checkins":  {},   # {ip: ts}
        "phase_ready_ack": {},   # {ip: ts}
        "phase_change_ack": {},  # {ip: ts}
        "round_assignments": {}, # {ip: filename}
    }
    path = get_state_path()
    if os.path.exists(path):
        try:
            with open(path) as f: data = json.load(f)
            for k, v in defaults.items(): data.setdefault(k, v)
            return data
        except Exception as e: logger.error(f"State load: {e}")
    return defaults

def save_state(state):
    try:
        with open(get_state_path(), 'w') as f: json.dump(state, f, indent=2)
    except Exception as e: logger.error(f"State save: {e}")

# ---------------------------------------------------------------------------
# Image order / enabled
# ---------------------------------------------------------------------------

def get_image_order_path(): return os.path.join(SHARE_DIR, 'image_order.json')

def load_image_order():
    path = get_image_order_path()
    if os.path.exists(path):
        try:
            with open(path) as f: return json.load(f)
        except Exception: pass
    return sorted([
        os.path.splitext(f)[0]
        for f in os.listdir(ORIGINALS_DIR)
        if os.path.splitext(f)[1].lower() in ALLOWED_EXTENSIONS
    ])

def save_image_order(order):
    with open(get_image_order_path(), 'w') as f: json.dump(order, f, indent=2)

def get_enabled_path(): return os.path.join(SHARE_DIR, 'image_enabled.json')

def load_enabled():
    """
    Returns {base: {"l": bool, "p": bool}}.
    Automatically migrates the old {base: bool} format.
    """
    path = get_enabled_path()
    if os.path.exists(path):
        try:
            with open(path) as f: raw = json.load(f)
            migrated = {}; changed = False
            for k, v in raw.items():
                if isinstance(v, bool):
                    migrated[k] = {"l": v, "p": v}; changed = True
                else:
                    migrated[k] = v
            if changed: save_enabled(migrated)
            return migrated
        except Exception: pass
    return {}

def save_enabled(enabled):
    with open(get_enabled_path(), 'w') as f: json.dump(enabled, f, indent=2)

def _flags(enabled, base):
    """Return {"l": bool, "p": bool} for a base name, defaulting to both True."""
    v = enabled.get(base, {"l": True, "p": True})
    if isinstance(v, bool): return {"l": v, "p": v}
    return {"l": v.get("l", True), "p": v.get("p", True)}

# ---------------------------------------------------------------------------
# Active image helpers
# ---------------------------------------------------------------------------

def landscape_file(base): return base + LANDSCAPE_SUFFIX
def portrait_file(base):  return base + PORTRAIT_SUFFIX

def get_active_bases(orientation=None):
    """
    Returns ordered list of base names that have a converted BMP and are
    enabled for the given orientation ('landscape', 'portrait', or None=either).
    """
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
    """
    Returns one flat ordered list:
    [base1_l.bmp, base1_p.bmp, base2_l.bmp, ...]
    Only enabled + existing files included.
    """
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

def crop_to_ratio(image, target_ratio):
    w, h = image.size; current = w / h
    if abs(current - target_ratio) < 1e-4: return image
    if current > target_ratio:
        new_w = int(h * target_ratio); left = (w - new_w) // 2
        return image.crop((left, 0, left + new_w, h))
    else:
        new_h = int(w / target_ratio); top = (h - new_h) // 2
        return image.crop((0, top, w, top + new_h))

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
    """Generates {base}_l.bmp (800×480) and {base}_p.bmp (rotated 800×480) in IMAGES_DIR, along with .bin files."""
    try:
        logger.info(f"Converting {src_path} → {base}_l.bmp / {base}_p.bmp")
        img = Image.open(src_path).convert('RGB')

        # Landscape: 5:3 → 800×480
        land = crop_to_ratio(img, 5.0/3.0).resize((800, 480), Image.Resampling.LANCZOS)
        Image.fromarray(dither_floyd_steinberg(np.array(land, dtype=np.float32), PALETTE))\
             .save(os.path.join(IMAGES_DIR, base + LANDSCAPE_SUFFIX), format='BMP')

        # Portrait: 3:5 → 480×800 → rotate 90°CW → stored as 800×480
        port = crop_to_ratio(img, 3.0/5.0).resize((480, 800), Image.Resampling.LANCZOS)
        port_img = Image.fromarray(dither_floyd_steinberg(np.array(port, dtype=np.float32), PALETTE))
        port_img.rotate(270, expand=True)\
                .save(os.path.join(IMAGES_DIR, base + PORTRAIT_SUFFIX), format='BMP')

        order = load_image_order()
        if base not in order: order.append(base); save_image_order(order)
        logger.info(f"  Saved: {base}_l.bmp, {base}_p.bmp")
        
        # Pre-generate bin files
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
            ip = dev['ip']; orientation = dev.get('orientation', 'landscape')
            pool = [b for b in get_active_bases(orientation) if b not in used]
            if not pool: pool = get_active_bases(orientation)
            if pool: chosen = random.choice(pool); used.add(chosen); assignments[ip] = chosen
    state['round_assignments'] = assignments

def _target_for_device(cfg, state, device_ip, device_idx, num_devices):
    devices = cfg.get('devices', []); shuffle = cfg.get('shuffle', False)
    sync_images = cfg.get('sync_images', False)
    dev_cfg = next((d for d in devices if d['ip'] == device_ip), {})
    orientation = dev_cfg.get('orientation', 'landscape')
    suffix = orient_suffix(orientation)
    active = get_active_bases(orientation)
    if not active: return None
    n = len(active); idx = state.get('current_index', 0)
    if shuffle and sync_images:
        base = state.get('round_assignments', {}).get('__sync__')
        return (base + suffix) if base else None
    if shuffle and not sync_images:
        base = state.get('round_assignments', {}).get(device_ip)
        return (base + suffix) if base else None
    if not shuffle and sync_images:
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
    <title>PhotoPainter Dashboard</title>
    <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;500;600;700&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg: #080d1a; --card: rgba(16,24,48,0.65); --border: rgba(255,255,255,0.07);
            --text: #f1f3f9; --muted: #8b95ae; --accent: #4f8ef7; --accent-h: #3371e0;
            --danger: #ef4444; --danger-h: #dc2626; --success: #22c55e; --warning: #f59e0b;
        }
        *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
        body { font-family: 'Outfit', sans-serif;
            background: radial-gradient(ellipse at 20% 0%, #1a2545 0%, var(--bg) 60%);
            color: var(--text); min-height: 100vh; padding: 2rem 1.5rem; }
        .container { width: 100%; max-width: 1400px; margin: 0 auto; }
        header { display: flex; justify-content: space-between; align-items: center;
            margin-bottom: 2rem; padding-bottom: 1.5rem; border-bottom: 1px solid var(--border); }
        h1 { font-size: 2rem; font-weight: 700;
            background: linear-gradient(135deg, #60a5fa, #a78bfa);
            -webkit-background-clip: text; -webkit-text-fill-color: transparent; }
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
        /* Device list */
        .device-row { display: flex; align-items: center; gap: 0.75rem; padding: 0.65rem 0;
            border-bottom: 1px solid var(--border); flex-wrap: wrap; }
        .device-row:last-child { border-bottom: none; }
        .device-ip { font-family: monospace; font-size: 0.9rem; color: var(--accent); flex: 1; min-width: 130px; }
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
        /* Image cards */
        #image-list { margin-bottom: 3rem; }
        .image-card { background: var(--card); border: 1px solid var(--border); border-radius: 16px;
            overflow: hidden; backdrop-filter: blur(14px); margin-bottom: 1.25rem; }
        .image-card.dragging { opacity: 0.5; outline: 2px dashed var(--accent); }
        .image-card.drag-target { outline: 2px solid var(--accent); }
        .card-header { padding: 0.9rem 1.4rem; border-bottom: 1px solid var(--border);
            display: flex; align-items: center; gap: 0.75rem; flex-wrap: wrap; }
        .drag-handle { cursor: grab; color: var(--muted); font-size: 1.2rem; user-select: none; }
        .drag-handle:active { cursor: grabbing; }
        /* Inline rename */
        .img-name-wrap { flex: 1; display: flex; align-items: center; gap: 0.5rem; min-width: 0; }
        .img-name { font-weight: 600; font-size: 1rem; word-break: break-all;
            cursor: pointer; border-bottom: 1px dashed transparent; transition: border-color 0.2s; }
        .img-name:hover { border-color: var(--muted); }
        .rename-input { font-weight: 600; font-size: 1rem; width: 100%; max-width: 300px;
            background: rgba(0,0,0,0.4); border: 1px solid var(--accent);
            border-radius: 6px; padding: 0.2rem 0.5rem; color: var(--text); font-family: inherit; }
        .rename-hint { font-size: 0.75rem; color: var(--muted); white-space: nowrap; }
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
        .preview-portrait { transform: rotate(-90deg); max-height: 130px; max-width: 78px; margin-top: auto; }
        .no-preview { width: 100%; min-height: 80px; display: flex; align-items: center;
            justify-content: center; background: rgba(0,0,0,0.3); border-radius: 6px;
            color: var(--muted); font-size: 0.85rem; }
        .card-actions { padding: 0.8rem 1.4rem; display: flex; justify-content: flex-end;
            gap: 0.75rem; flex-wrap: wrap; background: rgba(0,0,0,0.08); }
        /* Node status */
        .status-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr)); gap: 0.75rem; }
        .status-node { background: rgba(0,0,0,0.25); border: 1px solid var(--border);
            border-radius: 10px; padding: 0.75rem 1rem; }
        .status-node .node-ip { font-family: monospace; font-size: 0.9rem; color: var(--accent); }
        .status-node .node-status { font-size: 0.8rem; color: var(--muted); margin-top: 0.3rem; }
        .status-node.online { border-color: rgba(34,197,94,0.4); }
        .status-node.online .node-status { color: var(--success); }
        .status-node.debug-active { border-color: rgba(245,158,11,0.5); }
        .status-node.debug-active .node-status { color: var(--warning); }
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
            <h1>PhotoPainter Controller</h1>
            <div class="subtitle">E-Paper Frame Fleet Manager &nbsp;·&nbsp; {{ images|length }} image(s)</div>
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
                        <input type="checkbox" class="toggle" id="shuffle" name="shuffle"
                               {% if config.shuffle %}checked{% endif %} onchange="this.form.submit()">
                        <label for="shuffle">Shuffle images</label>
                    </span>
                </div>
                <div class="form-row">
                    <span class="toggle-wrap">
                        <input type="checkbox" class="toggle" id="sync_images" name="sync_images"
                               {% if config.sync_images %}checked{% endif %} onchange="this.form.submit()">
                        <label for="sync_images">Same image on all devices</label>
                    </span>
                </div>
                <div style="margin-top:1rem;">
                    <button type="submit" class="btn">💾 Save Settings</button>
                </div>
                {% for dev in config.devices %}
                <input type="hidden" name="device_ip_{{ loop.index0 }}" value="{{ dev.ip }}">
                <input type="hidden" name="device_orient_{{ loop.index0 }}" value="{{ dev.orientation }}">
                <input type="hidden" name="device_debug_{{ loop.index0 }}" value="{{ '1' if dev.debug else '0' }}">
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
                    <span class="device-ip">{{ dev.ip }}</span>
                    
                    <div class="orient-pill" title="Toggle orientation">
                        <button type="button"
                                class="{% if dev.orientation == 'landscape' %}active-l{% endif %}"
                                onclick="setDeviceOrient('{{ dev.ip }}', 'landscape', this)">🌅 L</button>
                        <button type="button"
                                class="{% if dev.orientation == 'portrait' %}active-p{% endif %}"
                                onclick="setDeviceOrient('{{ dev.ip }}', 'portrait', this)">🤳 P</button>
                    </div>

                    <div class="toggle-wrap" style="margin-left: 0.5rem; margin-right: 0.5rem;" title="Enable safe REPL debug mode">
                        <input type="checkbox" class="toggle" id="dbg_dev_{{ loop.index0 }}"
                               {% if dev.debug %}checked{% endif %}
                               onchange="setDeviceDebug('{{ dev.ip }}', this.checked)">
                        <label for="dbg_dev_{{ loop.index0 }}" style="font-size:0.8rem; font-weight:600; color:var(--warning);">DEBUG</label>
                    </div>

                    <button type="button" class="btn btn-danger btn-sm"
                            onclick="removeDevice({{ loop.index0 }})">✕</button>
                </div>
                {% else %}
                <p style="color:var(--muted);font-size:0.9rem;">No devices configured yet.</p>
                {% endfor %}
                </div>
                <input type="hidden" name="device_count" id="device_count" value="{{ config.devices|length }}">
                <div class="form-row" style="margin-top:1rem;">
                    <input type="text" id="new-ip" placeholder="192.168.1.x" style="flex:1;min-width:130px;">
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
            {% set ts = node_status.get(dev.ip, 0) %}
            <div class="status-node {% if dev.debug %}debug-active{% elif ts > now_ts - 120 %}online{% endif %}">
                <div class="node-ip">{{ dev.ip }}</div>
                <div class="node-status">
                    {% if dev.debug %}<span style="color:var(--warning); font-weight:600;">⚠️ REPL DEBUG LOCK</span>
                    {% elif ts > 0 %}Last seen {{ ((now_ts - ts)|int) }}s ago
                    {% else %}Never seen{% endif %}
                </div>
            </div>
        {% else %}
            <p style="color:var(--muted);font-size:0.85rem;">Add devices above.</p>
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
        <span style="font-size:0.8rem;color:var(--muted);">Drag to reorder · Click name to rename</span>
    </div>

    <div id="image-list">
    {% for img in images %}
    <div class="image-card" data-base="{{ img.base }}" draggable="true">
        <div class="card-header">
            <span class="drag-handle" title="Drag to reorder">⣿</span>

            <div class="img-name-wrap">
                <span class="img-name" title="Click to rename"
                      onclick="startRename(this, '{{ img.base }}')">{{ img.base }}</span>
                <span class="rename-hint" style="display:none;">Enter to save · Esc to cancel</span>
            </div>
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
                    <span style="flex:1;">{{ img.base }}_l.bmp &nbsp;·&nbsp; Landscape</span>
                    <span class="toggle-wrap">
                        <input type="checkbox" class="toggle" id="tog_l_{{ img.base }}"
                               {% if img.l_on %}checked{% endif %}
                               onchange="toggleOrient('{{ img.base }}', 'l', this.checked)"
                               {% if not img.has_l %}disabled title="Not converted yet"{% endif %}>
                        <label for="tog_l_{{ img.base }}" style="font-size:0.75rem;">On</label>
                    </span>
                </div>
                {% if img.has_l %}
                    <img class="preview-img"
                         src="{{ url_for('serve_image', filename=img.base + '_l.bmp') }}"
                         alt="Landscape">
                {% else %}
                    <div class="no-preview">Not converted</div>
                {% endif %}
            </div>

            <div class="preview-cell">
                <div class="preview-label">
                    <span style="flex:1;">{{ img.base }}_p.bmp &nbsp;·&nbsp; Portrait</span>
                    <span class="toggle-wrap">
                        <input type="checkbox" class="toggle" id="tog_p_{{ img.base }}"
                               {% if img.p_on %}checked{% endif %}
                               onchange="toggleOrient('{{ img.base }}', 'p', this.checked)"
                               {% if not img.has_p %}disabled title="Not converted yet"{% endif %}>
                        <label for="tog_p_{{ img.base }}" style="font-size:0.75rem;">On</label>
                    </span>
                </div>
                {% if img.has_p %}
                    <img class="preview-img preview-portrait"
                         src="{{ url_for('serve_image', filename=img.base + '_p.bmp') }}"
                         alt="Portrait">
                {% else %}
                    <div class="no-preview">Not converted</div>
                {% endif %}
            </div>
        </div>

        <div class="card-actions">
            <form action="{{ url_for('convert_file', filename=img.original_name) }}" method="POST" style="display:inline;">
                <button type="submit" class="btn btn-ghost btn-sm">
                    {% if img.has_l or img.has_p %}🔄 Re-Convert{% else %}⚙️ Convert{% endif %}
                </button>
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

// ---- Inline rename ----
function startRename(span, base) {
    const wrap = span.closest('.img-name-wrap');
    const hint = wrap.querySelector('.rename-hint');
    const input = document.createElement('input');
    input.type = 'text'; input.className = 'rename-input'; input.value = base;
    span.style.display = 'none'; hint.style.display = 'inline';
    wrap.insertBefore(input, hint);
    input.focus(); input.select();

    function commit() {
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
        input.remove(); span.style.display = ''; hint.style.display = 'none';
    }
    input.addEventListener('keydown', ev => {
        if (ev.key === 'Enter') commit();
        if (ev.key === 'Escape') cancel();
    });
    input.addEventListener('blur', cancel);
    input.addEventListener('mousedown', ev => ev.stopPropagation());
}

// ---- Drag-to-reorder ----
let dragSrc = null;
document.querySelectorAll('.image-card').forEach(card => {
    card.addEventListener('dragstart', ev => {
        dragSrc = card; card.classList.add('dragging');
        ev.dataTransfer.effectAllowed = 'move';
    });
    card.addEventListener('dragend', () => {
        card.classList.remove('dragging');
        document.querySelectorAll('.image-card').forEach(c => c.classList.remove('drag-target'));
        saveOrder();
    });
    card.addEventListener('dragover', ev => {
        ev.preventDefault(); ev.dataTransfer.dropEffect = 'move';
        if (card !== dragSrc) {
            document.querySelectorAll('.image-card').forEach(c => c.classList.remove('drag-target'));
            card.classList.add('drag-target');
        }
    });
    card.addEventListener('drop', ev => {
        ev.preventDefault();
        if (dragSrc && dragSrc !== card) {
            const cards = [...document.getElementById('image-list').querySelectorAll('.image-card')];
            if (cards.indexOf(dragSrc) < cards.indexOf(card)) card.after(dragSrc);
            else card.before(dragSrc);
        }
    });
});
function saveOrder() {
    const bases = [...document.querySelectorAll('.image-card')].map(c => c.dataset.base);
    fetch('/reorder', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({order:bases})});
}

// ---- Device orientation toggle ----
function setDeviceOrient(ip, orientation, btn) {
    fetch('/device_orientation', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ip, orientation})
    }).then(r => r.json()).then(d => {
        if (d.ok) {
            const pill = btn.closest('.orient-pill');
            pill.querySelectorAll('button').forEach(b => b.className = '');
            btn.className = orientation === 'landscape' ? 'active-l' : 'active-p';
            toast(`${ip} → ${orientation}`);
        }
    });
}

// ---- Device debug toggle asynchronous action ----
function setDeviceDebug(ip, isChecked) {
    fetch('/device_debug', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ip, debug: isChecked})
    }).then(r => r.json()).then(d => {
        if (d.ok) {
            toast(`${ip} debug mode ${isChecked ? 'enabled' : 'disabled'}`);
            // Briefly delay page refresh to show toast, helping reflect Node Status view changes
            setTimeout(() => window.location.reload(), 600);
        } else {
            toast('Failed to change device debug state');
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
    const ip     = document.getElementById('new-ip').value.trim();
    const orient = document.getElementById('new-orient-val').value;
    const dbg    = document.getElementById('new-debug-val').checked;
    if (!ip) return;
    const list = document.getElementById('device-list');
    const placeholder = list.querySelector('p'); if (placeholder) placeholder.remove();
    const row = document.createElement('div');
    row.className = 'device-row'; row.dataset.idx = deviceCount;
    row.innerHTML = `
        <span class="device-idx">#${deviceCount}</span>
        <span class="device-ip">${ip}</span>
        <div class="orient-pill">
            <button type="button" class="${orient==='landscape'?'active-l':''}"
                    onclick="setDeviceOrient('${ip}','landscape',this)">🌅 L</button>
            <button type="button" class="${orient==='portrait'?'active-p':''}"
                    onclick="setDeviceOrient('${ip}','portrait',this)">🤳 P</button>
        </div>
        <div class="toggle-wrap" style="margin-left: 0.5rem; margin-right: 0.5rem;">
            <input type="checkbox" class="toggle" id="dbg_dev_${deviceCount}" ${dbg ? 'checked' : ''}
                   onchange="setDeviceDebug('${ip}', this.checked)">
            <label for="dbg_dev_${deviceCount}" style="font-size:0.8rem; font-weight:600; color:var(--warning);">DEBUG</label>
        </div>
        <input type="hidden" name="ip_${deviceCount}" value="${ip}">
        <input type="hidden" name="orient_${deviceCount}" value="${orient}" class="orient-hidden">
        <input type="hidden" name="debug_${deviceCount}" value="${dbg ? '1' : '0'}" class="debug-hidden">
        <button type="button" class="btn btn-danger btn-sm" onclick="removeDevice(${deviceCount})">✕</button>`;
    list.appendChild(row);
    deviceCount++;
    document.getElementById('device_count').value = deviceCount;
    document.getElementById('new-ip').value = '';
    document.getElementById('new-debug-val').checked = false;
}

function removeDevice(idx) {
    document.querySelector(`.device-row[data-idx="${idx}"]`)?.remove();
    document.querySelectorAll('.device-row').forEach((r, i) => {
        r.dataset.idx = i; r.querySelector('.device-idx').textContent = `#${i}`;
    });
    deviceCount = document.querySelectorAll('.device-row').length;
    document.getElementById('device_count').value = deviceCount;
}

// Inject hidden fields for existing devices on manual form save
(function() {
    const form = document.getElementById('device-form');
    deviceData.forEach((dev, i) => {
        const h = document.createElement('input');
        h.type = 'hidden'; h.name = `ip_${i}`; h.value = dev.ip; form.appendChild(h);
        const o = document.createElement('input');
        o.type = 'hidden'; o.name = `orient_${i}`; o.value = dev.orientation; form.appendChild(o);
        const d = document.createElement('input');
        d.type = 'hidden'; d.name = `debug_${i}`; d.value = dev.debug ? "1" : "0"; form.appendChild(d);
    });
})();
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

        images.append({
            'base': base, 'original_name': original_name,
            'has_l': has_l, 'has_p': has_p,
            'l_on': flags["l"], 'p_on': flags["p"],
        })

    now_ts      = int(time.time())
    node_status = state.get('phase_checkins', {})
    phase       = state.get('phase', PHASE_GATHERING)

    return render_template_string(HTML_TEMPLATE, images=images, config=config,
                                  node_status=node_status, now_ts=now_ts, phase=phase)


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
    for path in [
        os.path.join(ORIGINALS_DIR, filename),
        os.path.join(IMAGES_DIR, base + LANDSCAPE_SUFFIX),
        os.path.join(IMAGES_DIR, base + PORTRAIT_SUFFIX),
    ]:
        if os.path.exists(path): os.remove(path)

    order = load_image_order()
    if base in order: order.remove(base); save_image_order(order)
    enabled = load_enabled(); enabled.pop(base, None); save_enabled(enabled)
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

    # Rename BMPs
    for old_f, new_f in [
        (old_base + LANDSCAPE_SUFFIX, new_base + LANDSCAPE_SUFFIX),
        (old_base + PORTRAIT_SUFFIX,  new_base + PORTRAIT_SUFFIX),
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
    return jsonify({'ok': True})


@app.route('/reorder', methods=['POST'])
def reorder():
    order = request.get_json().get('order', [])
    save_image_order(order); return jsonify({'ok': True})


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
    for i in range(count):
        ip = request.form.get(f'device_ip_{i}', '').strip()
        orient = request.form.get(f'device_orient_{i}', 'landscape')
        dbg = request.form.get(f'device_debug_{i}', '0') == '1'
        if ip: devices.append({'ip': ip, 'orientation': orient, 'debug': dbg})
    if devices: cfg['devices'] = devices
    save_config(cfg); return redirect(url_for('index'))


@app.route('/devices', methods=['POST'])
def update_devices():
    cfg = load_config()
    count = int(request.form.get('device_count', 0))
    devices = []
    for i in range(count):
        ip = request.form.get(f'ip_{i}', '').strip()
        orient = request.form.get(f'orient_{i}', 'landscape')
        dbg = request.form.get(f'debug_{i}', '0') == '1'
        if ip: devices.append({'ip': ip, 'orientation': orient, 'debug': dbg})
    cfg['devices'] = devices; save_config(cfg); return redirect(url_for('index'))


@app.route('/device_orientation', methods=['POST'])
def device_orientation():
    data = request.get_json()
    ip = data.get('ip'); orientation = data.get('orientation')
    if not ip or orientation not in ('landscape', 'portrait'):
        return jsonify({'ok': False}), 400
    cfg = load_config()
    for dev in cfg.get('devices', []):
        if dev['ip'] == ip: dev['orientation'] = orientation; break
    save_config(cfg)
    return jsonify({'ok': True, 'orientation': orientation})


@app.route('/device_debug', methods=['POST'])
def device_debug():
    data = request.get_json()
    ip = data.get('ip'); dbg_val = bool(data.get('debug', False))
    if not ip:
        return jsonify({'ok': False}), 400
    cfg = load_config()
    for dev in cfg.get('devices', []):
        if dev['ip'] == ip: 
            dev['debug'] = dbg_val
            break
    save_config(cfg)
    logger.info(f"Target device {ip} debug switch updated to: {dbg_val}")
    return jsonify({'ok': True, 'debug': dbg_val})


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
    """Returns the unified index, filtered to the caller's orientation if known."""
    cfg = load_config(); caller_ip = _caller_ip()
    all_files = get_unified_index()
    dev_cfg = next((d for d in cfg.get('devices', []) if d['ip'] == caller_ip), None)
    if dev_cfg:
        suffix = orient_suffix(dev_cfg.get('orientation', 'landscape'))
        files  = [f for f in all_files if f.endswith(suffix)]
    else:
        files = all_files
    return jsonify(files)


@app.route('/api/daily-zip', methods=['GET'])
def api_daily_zip():
    """
    Returns a ZIP of all .bin files for the requesting device, index.json, list.json, and config.json.
    Unknown IPs are auto-registered as portrait.
    """
    from flask import Response
    cfg = load_config(); caller_ip = _caller_ip()

    dev_cfg = next((d for d in cfg.get('devices', []) if d['ip'] == caller_ip), None)
    if not dev_cfg:
        new_dev = {'ip': caller_ip, 'orientation': 'portrait', 'debug': False}
        cfg.setdefault('devices', []).append(new_dev); save_config(cfg)
        dev_cfg = new_dev
        logger.info(f"Auto-registered {caller_ip} as portrait")

    orientation = dev_cfg.get('orientation', 'portrait')
    active_bases = get_active_bases(orientation)
    bin_suffix = '_l.bin' if orientation == 'landscape' else '_p.bin'
    candidates = [b + bin_suffix for b in active_bases]

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode='w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        # Include config.json
        zf.writestr('config.json', json.dumps(cfg, indent=2))
        # Include index.json and list.json
        manifest_data = json.dumps(candidates, indent=2)
        zf.writestr('index.json', manifest_data)
        zf.writestr('list.json', manifest_data)
        
        # Include .bin files, ensuring they are generated
        for bin_filename in candidates:
            base = bin_filename[:-6] # strip '_l.bin' or '_p.bin'
            bin_path = ensure_bin_file(base, orientation)
            if bin_path and os.path.exists(bin_path):
                zf.write(bin_path, arcname=bin_filename)
            else:
                logger.warning(f"daily-zip: failed to package {bin_filename}")

    size = buf.tell(); buf.seek(0)
    logger.info(f"daily-zip for {caller_ip}: {len(candidates)} files, {size:,} bytes")
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
    if not cfg.get('shuffle', False):
        if cfg.get('sync_images', False):
            pool = get_active_bases(None)
            if pool: state['current_index'] = (state['current_index'] + 1) % len(pool)
        else:
            state['current_index'] = state['current_index'] + num_devices


def _reset_round(state):
    state['phase'] = PHASE_GATHERING
    state['phase_checkins'] = {}; state['phase_ready_ack'] = {}; state['phase_change_ack'] = {}
    state['round_assignments'] = {}


@app.route('/api/wakeup', methods=['POST'])
def api_wakeup():
    """
    3-phase wakeup handshake returning text:
      WAIT - image.bin
      READY - image.bin
      CHANGE - image.bin
      DEBUG (Overrode via server switch)
    """
    from flask import Response
    cfg     = load_config()
    devices = cfg.get('devices', [])
    if not devices:
        return Response("WAIT - None", mimetype='text/plain'), 200

    ip = _caller_ip()
    known_ips = [d['ip'] for d in devices]
    if ip not in known_ips:
        logger.warning(f"Wakeup from unknown IP {ip}")
        return Response("WAIT - None", mimetype='text/plain'), 200

    device_idx    = known_ips.index(ip)
    num_devices   = len(devices)
    dev_cfg       = devices[device_idx]

    # ---- CRITICAL INTERCEPT: Server Debug Mode Override ----
    if dev_cfg.get('debug', False):
        logger.info(f"[INTERCEPT] Responding with DEBUG to client: {ip}")
        return Response("DEBUG", mimetype='text/plain'), 200

    with _state_lock:
        state  = load_state()
        now_ts = int(time.time())
        phase  = state.get('phase', PHASE_GATHERING)

        # Ensure assignments exist for this round so we always have a filename to return
        state.setdefault('round_assignments', {})
        if cfg.get('shuffle', False) and not state['round_assignments']:
            _build_shuffle_assignments(cfg, state)

        # Get target filename for this device
        target_file = _target_for_device(cfg, state, ip, device_idx, num_devices)
        if target_file and target_file.endswith('.bmp'):
            target_file = target_file[:-4] + '.bin'

        # Ensure the bin file is generated in the background/on the fly if needed
        if target_file:
            base = target_file[:-6] # strip '_l.bin' or '_p.bin'
            ensure_bin_file(base, dev_cfg.get('orientation', 'landscape'))

        # ---- Phase 1: GATHERING ----
        if phase == PHASE_GATHERING:
            state['phase_checkins'][ip] = now_ts
            all_in = set(known_ips) <= set(state['phase_checkins'].keys())
            if not all_in:
                if not state.get('last_change_ts'):
                    state['last_change_ts'] = now_ts
                timer_val = cfg.get('timer', 900)
                remaining = (state['last_change_ts'] + timer_val) - now_ts
                save_state(state)
                logger.info(f"GATHERING wait {ip} — target: {target_file}, remaining: {remaining}")
                if remaining > 10:
                    return Response(f"WAIT - {target_file} - {remaining}", mimetype='text/plain'), 200
                else:
                    return Response(f"WAIT - {target_file}", mimetype='text/plain'), 200

            # All are in — we move to READY phase
            state['phase'] = PHASE_READY
            state['phase_ready_ack'] = {}
            logger.info(f"All in → READY. Assignments: {state['round_assignments']}")
            phase = PHASE_READY

        # ---- Phase 2: READY ----
        if phase == PHASE_READY:
            state['phase_ready_ack'][ip] = now_ts
            all_acked = set(known_ips) <= set(state['phase_ready_ack'].keys())
            if not all_acked:
                save_state(state)
                logger.info(f"READY wait {ip} — target: {target_file}")
                return Response(f"READY - {target_file}", mimetype='text/plain'), 200

            # All ACKed READY → move to CHANGE
            state['phase'] = PHASE_CHANGE
            state['phase_change_ack'] = {}
            logger.info("All READY → CHANGE")
            phase = PHASE_CHANGE

        # ---- Phase 3: CHANGE ----
        if phase == PHASE_CHANGE:
            state['phase_change_ack'][ip] = now_ts
            all_changed = set(known_ips) <= set(state['phase_change_ack'].keys())
            
            # If all devices have checked in for the CHANGE phase, advance index and reset round
            if all_changed:
                _advance_index(cfg, state, num_devices)
                sync_due = (now_ts - state.get('last_sync_ts', 0)) >= 86400
                if sync_due: state['last_sync_ts'] = now_ts
                _reset_round(state)
            
            save_state(state)
            logger.info(f"CHANGE → {ip}: {target_file}")
            return Response(f"CHANGE - {target_file}", mimetype='text/plain'), 200

        save_state(state)
        return Response("WAIT - None", mimetype='text/plain'), 200


@app.route('/api/wakeup/reset', methods=['POST'])
def api_reset_state():
    state = load_state(); _reset_round(state); save_state(state)
    return jsonify({"ok": True, "phase": state['phase']})


@app.route('/api/wakeup/next', methods=['POST'])
def api_next_image():
    cfg = load_config()
    devices = cfg.get('devices', [])
    num_devices = len(devices) if devices else 1
    with _state_lock:
        state = load_state()
        _advance_index(cfg, state, num_devices)
        _reset_round(state)
        save_state(state)
    return jsonify({"ok": True, "current_index": state.get('current_index', 0), "phase": state['phase']})


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8000))
    app.run(host='0.0.0.0', port=port, debug=False)
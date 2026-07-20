# ==========================================================================================
# DESCRIPTION: Image processing pipeline. Manages orientation rotation, smart cropping, scaling, and Floyd-Steinberg dithering.
# DEPENDENCIES: PIL (Pillow), numpy, db, converters
# ==========================================================================================
import os
import logging
import numpy as np
from PIL import Image, ImageOps
from db import (ORIGINALS_DIR, IMAGES_DIR, LANDSCAPE_SUFFIX, PORTRAIT_SUFFIX,
                load_crops, load_image_order, save_image_order, get_image_edits)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Screen-type registry
# hw_profile → (landscape_width, landscape_height)
# ---------------------------------------------------------------------------

# Canonical device-type names are the primary keys.
SCREEN_TYPES = {
    # Canonical names
    'Seeed-EE04-Spectra6-7in3':   (800,  480),
    'Seeed-EE04-Spectra6-13in3':  (1600, 1200),
    'Waveshare-PhotoPainter-7in3': (800,  480),
    # Legacy names kept so pre-alias-map entries in the DB still resolve.
    'ESP32-S3-PhotoPainter': (800,  480),
    'XIAO-EE04-7in3':        (800,  480),
    'XIAO-EE04-13in3':       (1600, 1200),
}
_DEFAULT_SCREEN = (800, 480)

# Alias map: old firmware type strings → canonical name.
# Any incoming hw_profile should be run through normalize_device_type()
# before being stored in the database or used for artifact selection.
DEVICE_TYPE_ALIASES = {
    'ESP32-S3-PhotoPainter': 'Waveshare-PhotoPainter-7in3',
    'XIAO-EE04-7in3':        'Seeed-EE04-Spectra6-7in3',
    'XIAO-EE04-13in3':       'Seeed-EE04-Spectra6-13in3',
}


def normalize_device_type(hw_profile):
    """Return the canonical device-type name for *hw_profile*.

    Canonical names pass through unchanged; legacy aliases are mapped to their
    canonical equivalent.  Unknown strings pass through unchanged (future-proof).
    """
    return DEVICE_TYPE_ALIASES.get(hw_profile or '', hw_profile or '')


def screen_size_for_profile(hw_profile):
    """Return (landscape_w, landscape_h) for a hw_profile string."""
    canonical = normalize_device_type(hw_profile)
    return SCREEN_TYPES.get(canonical or '', _DEFAULT_SCREEN)


def _artifact_infix(w, h):
    """Empty for the legacy 800×480 screen; '_WxH' for others."""
    if (w, h) == _DEFAULT_SCREEN:
        return ''
    return f'_{w}x{h}'


# ---------------------------------------------------------------------------
# Colour palette & dithering
# ---------------------------------------------------------------------------

PALETTE = np.array([
    [0,   0,   0  ],
    [255, 255, 255],
    [0,   255, 0  ],
    [0,   0,   255],
    [255, 0,   0  ],
    [255, 255, 0  ],
], dtype=np.float32)


def dither_floyd_steinberg(img_array, palette):
    h, w, _ = img_array.shape
    orig   = img_array.astype(np.float32)
    padded = np.pad(img_array, ((0, 1), (1, 1), (0, 0)), mode='edge').astype(np.float32)
    for y in range(h):
        for x in range(1, w + 1):
            orig_val = orig[y, x - 1]
            if np.all(orig_val == 0):
                padded[y, x] = [0, 0, 0]; continue
            if np.all(orig_val == 255):
                padded[y, x] = [255, 255, 255]; continue
            old_val = padded[y, x].copy()
            l1_black = np.sum(np.abs(old_val - [0, 0, 0]))
            l1_white = np.sum(np.abs(old_val - [255, 255, 255]))
            if l1_black < 15:
                padded[y, x] = [0, 0, 0]; continue
            elif l1_white < 15:
                padded[y, x] = [255, 255, 255]; continue
            diff = palette - old_val; dist = np.sum(diff ** 2, axis=1); idx = np.argmin(dist)
            new_val = palette[idx]; padded[y, x] = new_val; err = old_val - new_val
            padded[y,     x + 1] += err * (7.0 / 16.0)
            padded[y + 1, x - 1] += err * (3.0 / 16.0)
            padded[y + 1, x    ] += err * (5.0 / 16.0)
            padded[y + 1, x + 1] += err * (1.0 / 16.0)
    return padded[0:h, 1:w+1].astype(np.uint8)


def _crop_with_outfill(img, crop_x, crop_y, crop_w, crop_h, bg_color='#ffffff'):
    """Crop a region from img; out-of-bounds areas are filled with bg_color.

    Coordinates are in original-image pixels and may extend beyond the image
    boundaries (negative, or past width/height).  The returned image has the
    requested (crop_w × crop_h) size.
    """
    iw, ih = img.size
    cw, ch = max(1, int(round(crop_w))), max(1, int(round(crop_h)))
    try:
        r = int(bg_color[1:3], 16)
        g = int(bg_color[3:5], 16)
        b = int(bg_color[5:7], 16)
    except Exception:
        r, g, b = 255, 255, 255
    canvas = Image.new('RGB', (cw, ch), (r, g, b))
    sx1 = max(0, int(round(crop_x)))
    sy1 = max(0, int(round(crop_y)))
    sx2 = min(iw, int(round(crop_x + crop_w)))
    sy2 = min(ih, int(round(crop_y + crop_h)))
    if sx2 > sx1 and sy2 > sy1:
        region = img.crop((sx1, sy1, sx2, sy2))
        canvas.paste(region, (sx1 - int(round(crop_x)), sy1 - int(round(crop_y))))
    return canvas


def apply_color_adjustments(img, hue_shift=0.0, saturation=1.0, value_adj=1.0,
                             r_gain=1.0, g_gain=1.0, b_gain=1.0):
    """Apply HSV shift and per-channel RGB gain to a PIL RGB Image.

    Uses PIL's built-in HSV colour space for the HSV pass to avoid hand-rolled
    numpy vectorisation bugs.  Returns a new PIL RGB Image.  Identity fast-path
    skips all processing when all params are neutral.
    """
    if (hue_shift == 0 and saturation == 1.0 and value_adj == 1.0
            and r_gain == 1.0 and g_gain == 1.0 and b_gain == 1.0):
        return img  # fast path: no-op
    if hue_shift != 0 or saturation != 1.0 or value_adj != 1.0:
        arr = np.array(img.convert('HSV'), dtype=np.float32)
        arr[..., 0] = (arr[..., 0] + hue_shift / 360.0 * 255.0) % 256.0
        arr[..., 1] = np.clip(arr[..., 1] * saturation, 0, 255)
        arr[..., 2] = np.clip(arr[..., 2] * value_adj, 0, 255)
        img = Image.fromarray(arr.astype(np.uint8), 'HSV').convert('RGB')
    if r_gain != 1.0 or g_gain != 1.0 or b_gain != 1.0:
        arr = np.array(img, dtype=np.float32)
        arr[..., 0] = np.clip(arr[..., 0] * r_gain, 0, 255)
        arr[..., 1] = np.clip(arr[..., 1] * g_gain, 0, 255)
        arr[..., 2] = np.clip(arr[..., 2] * b_gain, 0, 255)
        img = Image.fromarray(arr.astype(np.uint8))
    return img


def ensure_dithered_original(base):
    dith_path = os.path.join(IMAGES_DIR, base + '_dithered.png')
    if os.path.exists(dith_path):
        return dith_path
    for f in os.listdir(ORIGINALS_DIR):
        if os.path.splitext(f)[0] == base:
            try:
                src = os.path.join(ORIGINALS_DIR, f)
                img = ImageOps.exif_transpose(Image.open(src)).convert('RGB')
                img.thumbnail((800, 800), Image.Resampling.LANCZOS)
                Image.fromarray(dither_floyd_steinberg(np.array(img, dtype=np.float32), PALETTE)
                                ).save(dith_path, format='PNG')
                return dith_path
            except Exception as e:
                logger.error(f"Dither overlay error for {base}: {e}")
                return None
    return None


# ---------------------------------------------------------------------------
# Bitstream serialisers
# ---------------------------------------------------------------------------

def rgb_array_to_spectra6_bitstream_fast(img_array):
    """Full-width row-major 4bpp packed bitstream (800×480 / 480×800 screens)."""
    h, w, _ = img_array.shape
    hardware_map = np.array([0, 1, 6, 5, 3, 2], dtype=np.uint8)
    pixels = img_array.reshape(-1, 3)
    dists  = np.sum((pixels[:, None, :] - PALETTE[None, :, :])**2, axis=2)
    hw_idx = hardware_map[np.argmin(dists, axis=1)].reshape(h, w)
    packed = (hw_idx[:, 0::2] << 4) | hw_idx[:, 1::2]
    return packed.tobytes()


def rgb_array_to_spectra6_bitstream_13in3(img_array):
    """
    Left-half-then-right-half 4bpp bitstream for the 13.3" dual-controller panel.

    Expected input: (1600, 1200, 3) — portrait-oriented (panel native).
    Output layout:
      bytes [0, 480000)       → CS_M: cols 0-599, rows 0-1599, row-major 4bpp
      bytes [480000, 960000)  → CS_S: cols 600-1199, rows 0-1599, row-major 4bpp
    """
    h, w, _ = img_array.shape
    assert (h, w) == (1600, 1200), f"13in3 serialiser expects (1600,1200,3), got {img_array.shape}"
    hardware_map = np.array([0, 1, 6, 5, 3, 2], dtype=np.uint8)
    pixels = img_array.reshape(-1, 3)
    dists  = np.sum((pixels[:, None, :] - PALETTE[None, :, :])**2, axis=2)
    hw_idx = hardware_map[np.argmin(dists, axis=1)].reshape(h, w)

    # Left half: cols 0-599 for all rows
    left  = hw_idx[:, :600]                              # (1600, 600)
    left_packed  = (left[:, 0::2] << 4) | left[:, 1::2] # (1600, 300)

    # Right half: cols 600-1199 for all rows
    right = hw_idx[:, 600:]                              # (1600, 600)
    right_packed = (right[:, 0::2] << 4) | right[:, 1::2]  # (1600, 300)

    return left_packed.tobytes() + right_packed.tobytes()


def _flip_bitstream(data):
    """180° rotation for full-width row-major bitstreams (small screen only)."""
    return bytes(((b & 0x0F) << 4) | ((b & 0xF0) >> 4) for b in reversed(data))


def _flip_bitstream_13in3(data):
    """
    180° rotation for 13.3" left|right split bitstream.
    Must reverse within each half independently (each half is independently row-major),
    then swap the halves so left stays left and right stays right after 180°.
    """
    assert len(data) == 960000
    left_half  = data[:480000]
    right_half = data[480000:]
    flip_left  = bytes(((b & 0x0F) << 4) | ((b & 0xF0) >> 4) for b in reversed(left_half))
    flip_right = bytes(((b & 0x0F) << 4) | ((b & 0xF0) >> 4) for b in reversed(right_half))
    return flip_left + flip_right


# ---------------------------------------------------------------------------
# Artifact path helpers
# ---------------------------------------------------------------------------

def artifact_suffixes_for_screen(w, h):
    """Return list of suffixes (relative to base) for all artifacts of one screen size."""
    infix = _artifact_infix(w, h)
    return [
        infix + '_l.bmp',
        infix + '_p.bmp',
        infix + '_l_u.bin',
        infix + '_l_f.bin',
        infix + '_p_u.bin',
        infix + '_p_f.bin',
    ]


def artifact_suffixes_all():
    """All artifact suffixes across every known screen size, plus legacy names."""
    suffixes = set()
    for size in set(SCREEN_TYPES.values()):
        suffixes.update(artifact_suffixes_for_screen(*size))
    # legacy bare bin names and dithered preview
    suffixes.update(['_l.bin', '_p.bin', '_dithered.png'])
    return list(suffixes)


def delete_artifacts_for_screen(base, w, h):
    """Delete BMP and bin artifacts for one screen size; leave originals intact."""
    for sfx in artifact_suffixes_for_screen(w, h):
        p = os.path.join(IMAGES_DIR, base + sfx)
        if os.path.exists(p):
            try:
                os.remove(p)
            except OSError as e:
                logger.warning(f"delete_artifacts_for_screen: {p}: {e}")


def delete_all_artifacts(base):
    """Delete every converted artifact for *base* across all known screen sizes."""
    for sfx in artifact_suffixes_all():
        p = os.path.join(IMAGES_DIR, base + sfx)
        if os.path.exists(p):
            try:
                os.remove(p)
            except OSError as e:
                logger.warning(f"delete_all_artifacts: {p}: {e}")


def get_screen_types_for_playlist(playlist_id):
    """
    Return set of (w, h) tuples for the screen sizes needed by a playlist's devices.
    If no devices have a known hw_profile, returns {_DEFAULT_SCREEN} as a safe fallback.
    """
    try:
        from db import get_playlist_devices, load_config
        dev_macs = get_playlist_devices(playlist_id)
        cfg = load_config()
        sizes = set()
        for mac in dev_macs:
            dev = next((d for d in cfg.get('devices', []) if d['mac'].lower() == mac.lower()), None)
            if dev and dev.get('hw_profile'):
                sizes.add(screen_size_for_profile(dev['hw_profile']))
        return sizes if sizes else {_DEFAULT_SCREEN}
    except Exception as e:
        logger.error(f"get_screen_types_for_playlist({playlist_id}): {e}")
        return {_DEFAULT_SCREEN}


def get_screen_types_for_image(base):
    """
    Return union of screen sizes needed across all playlists that contain *base*.
    Returns empty set if the image is not in any playlist.
    """
    try:
        from db import get_playlists_for_image
        pids = get_playlists_for_image(base)
        sizes = set()
        for pid in pids:
            sizes.update(get_screen_types_for_playlist(pid))
        return sizes
    except Exception as e:
        logger.error(f"get_screen_types_for_image({base}): {e}")
        return set()


# ---------------------------------------------------------------------------
# Bin file helpers
# ---------------------------------------------------------------------------

def ensure_bin_files(base):
    """Ensure 800×480 bin files (_l_u.bin, _l_f.bin, _p_u.bin, _p_f.bin) exist."""
    ok = True
    for orient_sfx, bmp_sfx in [('_l', '_l.bmp'), ('_p', '_p.bmp')]:
        bmp_path = os.path.join(IMAGES_DIR, base + bmp_sfx)
        if not os.path.exists(bmp_path):
            ok = False; continue
        u_path = os.path.join(IMAGES_DIR, base + orient_sfx + '_u.bin')
        f_path = os.path.join(IMAGES_DIR, base + orient_sfx + '_f.bin')
        try:
            if not os.path.exists(u_path):
                img = Image.open(bmp_path).convert('RGB')
                data = rgb_array_to_spectra6_bitstream_fast(np.array(img, dtype=np.uint8))
                with open(u_path, 'wb') as fh: fh.write(data)
            else:
                with open(u_path, 'rb') as fh: data = fh.read()
            if not os.path.exists(f_path):
                with open(f_path, 'wb') as fh: fh.write(_flip_bitstream(data))
        except Exception as e:
            logger.error(f"Bin variant error for {base}{orient_sfx}: {e}"); ok = False
    return ok


def ensure_bin_files_13in3(base):
    """Ensure 13.3" bin files (_1600x1200_l_u.bin etc.) exist."""
    infix = '_1600x1200'
    ok = True
    for orient_sfx, bmp_sfx in [('_l', infix + '_l.bmp'), ('_p', infix + '_p.bmp')]:
        bmp_path = os.path.join(IMAGES_DIR, base + bmp_sfx)
        if not os.path.exists(bmp_path):
            ok = False; continue
        u_path = os.path.join(IMAGES_DIR, base + infix + orient_sfx + '_u.bin')
        f_path = os.path.join(IMAGES_DIR, base + infix + orient_sfx + '_f.bin')
        try:
            if not os.path.exists(u_path):
                img = Image.open(bmp_path).convert('RGB')
                assert img.size == (1200, 1600), f"Expected (1200,1600) BMP for 13in3, got {img.size}"
                data = rgb_array_to_spectra6_bitstream_13in3(np.array(img, dtype=np.uint8))
                with open(u_path, 'wb') as fh: fh.write(data)
            else:
                with open(u_path, 'rb') as fh: data = fh.read()
            if not os.path.exists(f_path):
                with open(f_path, 'wb') as fh: fh.write(_flip_bitstream_13in3(data))
        except Exception as e:
            logger.error(f"13in3 bin variant error for {base}{orient_sfx}: {e}"); ok = False
    return ok


def ensure_bin_files_for_screen(base, w, h):
    """Dispatch to the appropriate bin-file ensurer based on target screen size."""
    if (w, h) == _DEFAULT_SCREEN:
        return ensure_bin_files(base)
    if (w, h) == (1600, 1200):
        return ensure_bin_files_13in3(base)
    logger.warning(f"No bin-file handler for {w}x{h}; skipping {base}")
    return False


def ensure_bin_file(base, orientation):
    ensure_bin_files(base)
    u = os.path.join(IMAGES_DIR, base + ('_l_u.bin' if orientation == 'landscape' else '_p_u.bin'))
    return u if os.path.exists(u) else None


# ---------------------------------------------------------------------------
# Image conversion
# ---------------------------------------------------------------------------

def _find_original(base):
    for f in os.listdir(ORIGINALS_DIR):
        if os.path.splitext(f)[0] == base:
            return os.path.join(ORIGINALS_DIR, f)
    return None


def convert_image(src_path, base):
    """Convert an uploaded image to 800×480 landscape + portrait BMPs and bins.

    If non-destructive edits are saved in image_edits for this base, the new
    pipeline is used (explicit crop rect + colour adjustments).  Otherwise the
    original offset-based crop path is used verbatim so output is byte-identical
    to the pre-editor behaviour.
    """
    try:
        img = ImageOps.exif_transpose(Image.open(src_path)).convert('RGB')
        w, h = img.size
        edits = get_image_edits(base)

        if edits and edits.get('crop_l_w') is not None:
            # ---- New path: explicit crop rect + colour adjustments ----
            bg = edits.get('bg_color', '#ffffff') or '#ffffff'
            hue_s  = float(edits.get('hue_shift',  0) or 0)
            sat    = float(edits.get('saturation',  1) or 1)
            val_a  = float(edits.get('value_adj',   1) or 1)
            r_gain = float(edits.get('r_gain',      1) or 1)
            g_gain = float(edits.get('g_gain',      1) or 1)
            b_gain = float(edits.get('b_gain',      1) or 1)
            rotate_deg = int(edits.get('rotate', 0) or 0) % 360

            # Apply rotation before crop (crop coords are in rotated-image space)
            if rotate_deg == 90:
                img = img.transpose(Image.Transpose.ROTATE_90)
            elif rotate_deg == 180:
                img = img.transpose(Image.Transpose.ROTATE_180)
            elif rotate_deg == 270:
                img = img.transpose(Image.Transpose.ROTATE_270)

            # Landscape: crop → outfill → resize 800×480 → colour adjust → dither
            land = _crop_with_outfill(img, edits['crop_l_x'], edits['crop_l_y'],
                                      edits['crop_l_w'], edits['crop_l_h'], bg)
            land = land.resize((800, 480), Image.Resampling.LANCZOS)
            land = apply_color_adjustments(land, hue_s, sat, val_a, r_gain, g_gain, b_gain)
            Image.fromarray(dither_floyd_steinberg(np.array(land, dtype=np.float32), PALETTE)
                            ).save(os.path.join(IMAGES_DIR, base + LANDSCAPE_SUFFIX), format='BMP')

            # Portrait: crop → outfill → resize 480×800 → colour adjust → dither → rotate
            if edits.get('crop_p_w') is not None:
                port = _crop_with_outfill(img, edits['crop_p_x'], edits['crop_p_y'],
                                          edits['crop_p_w'], edits['crop_p_h'], bg)
            else:
                # Fall back to legacy portrait crop if portrait rect not set
                crops   = load_crops()
                offsets = crops.get(base, {"l": 0.5, "p": 0.5})
                op = offsets.get("p", 0.5)
                tp = 3.0 / 5.0
                if w / h >= tp:
                    hc, wc = h, int(h * tp); yc, xc = 0, int(op * (w - wc))
                else:
                    wc, hc = w, int(w / tp); xc, yc = 0, int(0.5 * (h - hc))
                port = img.crop((xc, yc, xc + wc, yc + hc))
            port = port.resize((480, 800), Image.Resampling.LANCZOS)
            port = apply_color_adjustments(port, hue_s, sat, val_a, r_gain, g_gain, b_gain)
            Image.fromarray(dither_floyd_steinberg(np.array(port, dtype=np.float32), PALETTE)
                            ).rotate(270, expand=True).save(
                                os.path.join(IMAGES_DIR, base + PORTRAIT_SUFFIX), format='BMP')

        else:
            # ---- Legacy path: offset-based crop, no colour adjustments ----
            # This branch is verbatim-identical to the pre-editor code so that
            # existing images without edits produce byte-identical output.
            crops  = load_crops()
            offsets = crops.get(base, {"l": 0.5, "p": 0.5})
            ol, op = offsets.get("l", 0.5), offsets.get("p", 0.5)

            # Landscape crop → 800×480
            tl = 5.0 / 3.0
            if w / h <= tl:
                wc, hc = w, int(w / tl); xc, yc = 0, int(ol * (h - hc))
            else:
                hc, wc = h, int(h * tl); yc, xc = 0, int(0.5 * (w - wc))
            land = img.crop((xc, yc, xc + wc, yc + hc)).resize((800, 480), Image.Resampling.LANCZOS)
            Image.fromarray(dither_floyd_steinberg(np.array(land, dtype=np.float32), PALETTE)
                            ).save(os.path.join(IMAGES_DIR, base + LANDSCAPE_SUFFIX), format='BMP')

            # Portrait crop → 480×800 (rotated 270°)
            tp = 3.0 / 5.0
            if w / h >= tp:
                hc, wc = h, int(h * tp); yc, xc = 0, int(op * (w - wc))
            else:
                wc, hc = w, int(w / tp); xc, yc = 0, int(0.5 * (h - hc))
            port = img.crop((xc, yc, xc + wc, yc + hc)).resize((480, 800), Image.Resampling.LANCZOS)
            Image.fromarray(dither_floyd_steinberg(np.array(port, dtype=np.float32), PALETTE)
                            ).rotate(270, expand=True).save(
                                os.path.join(IMAGES_DIR, base + PORTRAIT_SUFFIX), format='BMP')

        # Invalidate old bin files then regenerate
        for sfx in ('_l_u.bin', '_l_f.bin', '_p_u.bin', '_p_f.bin', '_l.bin', '_p.bin'):
            p = os.path.join(IMAGES_DIR, base + sfx)
            if os.path.exists(p): os.remove(p)

        # NOTE: pool membership (image_order) is deliberately NOT touched here.
        # convert_image runs from device API calls and background threads where
        # there is no session, so appending here would assign the image to the
        # default owner. The /upload route assigns ownership explicitly.
        ensure_bin_files(base)
        return True
    except Exception as e:
        logger.error(f"convert_image({base}): {e}"); return False


def convert_image_13in3(src_path, base):
    """
    Convert an uploaded image to 13.3" BMPs and bins.

    The 13.3" panel is 1200×1600 portrait-native.
    - Landscape artifact: crop to 1600:1200 aspect, resize to 1600×1200, rotate 90°
      → stored as 1200×1600 BMP; serialised left-half-first.
    - Portrait artifact: crop to 1200:1600 aspect, resize to 1200×1600.
      → stored as 1200×1600 BMP; serialised left-half-first.
    """
    infix = '_1600x1200'
    try:
        img = ImageOps.exif_transpose(Image.open(src_path)).convert('RGB')
        w, h = img.size
        crops   = load_crops()
        offsets = crops.get(base, {"l": 0.5, "p": 0.5})
        ol, op  = offsets.get("l", 0.5), offsets.get("p", 0.5)

        # Colour adjustments (applied after resize, before dither; legacy crop geometry unchanged)
        edits  = get_image_edits(base)
        hue_s  = float(edits['hue_shift']  or 0) if edits else 0
        sat    = float(edits['saturation'] or 1) if edits else 1.0
        val_a  = float(edits['value_adj']  or 1) if edits else 1.0
        r_gain = float(edits['r_gain']     or 1) if edits else 1.0
        g_gain = float(edits['g_gain']     or 1) if edits else 1.0
        b_gain = float(edits['b_gain']     or 1) if edits else 1.0

        # Landscape crop → 1600×1200, then rotate 90° → 1200×1600 on panel
        tl = 4.0 / 3.0  # 1600:1200
        if w / h <= tl:
            wc, hc = w, int(w / tl); xc, yc = 0, int(ol * max(h - hc, 0))
        else:
            hc, wc = h, int(h * tl); yc, xc = 0, int(0.5 * (w - wc))
        land = img.crop((xc, yc, xc + wc, yc + hc)).resize((1600, 1200), Image.Resampling.LANCZOS)
        land = apply_color_adjustments(land, hue_s, sat, val_a, r_gain, g_gain, b_gain)
        # Rotate 90° → becomes 1200×1600 (portrait), which is the panel's native orientation
        land_panel = land.rotate(90, expand=True)
        Image.fromarray(dither_floyd_steinberg(np.array(land_panel, dtype=np.float32), PALETTE)
                        ).save(os.path.join(IMAGES_DIR, base + infix + '_l.bmp'), format='BMP')

        # Portrait crop → 1200×1600 (panel native, no rotation needed)
        tp = 3.0 / 4.0  # 1200:1600
        if w / h >= tp:
            hc, wc = h, int(h * tp); yc, xc = 0, int(op * max(w - wc, 0))
        else:
            wc, hc = w, int(w / tp); xc, yc = 0, int(0.5 * (h - hc))
        port = img.crop((xc, yc, xc + wc, yc + hc)).resize((1200, 1600), Image.Resampling.LANCZOS)
        port = apply_color_adjustments(port, hue_s, sat, val_a, r_gain, g_gain, b_gain)
        Image.fromarray(dither_floyd_steinberg(np.array(port, dtype=np.float32), PALETTE)
                        ).save(os.path.join(IMAGES_DIR, base + infix + '_p.bmp'), format='BMP')

        # Invalidate old 13in3 bin files then regenerate
        for sfx in (infix + '_l_u.bin', infix + '_l_f.bin',
                    infix + '_p_u.bin', infix + '_p_f.bin'):
            p = os.path.join(IMAGES_DIR, base + sfx)
            if os.path.exists(p): os.remove(p)

        ensure_bin_files_13in3(base)
        return True
    except Exception as e:
        logger.error(f"convert_image_13in3({base}): {e}"); return False


def convert_image_for_screen(src_path, base, w, h):
    """Dispatch to the right converter for a given screen size."""
    if (w, h) == _DEFAULT_SCREEN:
        return convert_image(src_path, base)
    if (w, h) == (1600, 1200):
        return convert_image_13in3(src_path, base)
    logger.warning(f"No converter for {w}x{h}; skipping {base}")
    return False


def ensure_converted_for_screen(base, w, h):
    """
    Ensure BMP + bin artifacts exist for the given screen size.
    Finds the original file by scanning ORIGINALS_DIR.
    Returns True if artifacts exist or were successfully produced.
    """
    infix = _artifact_infix(w, h)
    # Check if BMP artifacts already exist
    l_bmp = os.path.join(IMAGES_DIR, base + infix + '_l.bmp')
    p_bmp = os.path.join(IMAGES_DIR, base + infix + '_p.bmp')
    if os.path.exists(l_bmp) and os.path.exists(p_bmp):
        # BMPs exist; ensure bins
        return ensure_bin_files_for_screen(base, w, h)
    # Need to convert
    src_path = _find_original(base)
    if not src_path:
        logger.warning(f"ensure_converted_for_screen: no original for {base}")
        return False
    return convert_image_for_screen(src_path, base, w, h)


def ensure_artifacts_for_playlist(playlist_id):
    """
    Ensure all screen-type-specific artifacts exist for every image in a playlist,
    for every hw_profile among the playlist's devices.
    Called as a best-effort background trigger; errors are logged only.
    """
    try:
        from db import get_playlist_images
        images = get_playlist_images(playlist_id)
        sizes  = get_screen_types_for_playlist(playlist_id)
        for w, h in sizes:
            for base in images:
                ensure_converted_for_screen(base, w, h)
    except Exception as e:
        logger.error(f"ensure_artifacts_for_playlist({playlist_id}): {e}")


def reconvert_all_intelligent():
    """
    For every image in ORIGINALS_DIR:
      - Delete all existing converted artifacts.
      - Reconvert only for the screen sizes required by the playlists it belongs to.
      - Images in no playlist are left as originals only (no artifacts).
    Crops are honoured because convert_image_for_screen reads load_crops().
    """
    from db import get_playlists_for_image
    for f in sorted(os.listdir(ORIGINALS_DIR)):
        ext = os.path.splitext(f)[1].lower()
        from db import ALLOWED_EXTENSIONS
        if ext not in ALLOWED_EXTENSIONS:
            continue
        base = os.path.splitext(f)[0]
        src  = os.path.join(ORIGINALS_DIR, f)
        delete_all_artifacts(base)
        sizes = get_screen_types_for_image(base)
        for w, h in sizes:
            convert_image_for_screen(src, base, w, h)


# ---------------------------------------------------------------------------
# Per-entry artifact helpers  (storage key = pe<entry_id>_*)
# ---------------------------------------------------------------------------

def entry_artifact_prefix(entry_id):
    """Return the storage-name prefix for all artifacts of a playlist entry."""
    return f'pe{entry_id}'


def delete_entry_artifacts(entry_id):
    """Delete BMP and bin artifacts for a playlist entry; leaves originals intact."""
    prefix = entry_artifact_prefix(entry_id)
    for sfx in ('_l.bmp', '_p.bmp', '_l_u.bin', '_l_f.bin', '_p_u.bin', '_p_f.bin'):
        p = os.path.join(IMAGES_DIR, prefix + sfx)
        if os.path.exists(p):
            try:
                os.remove(p)
            except OSError as e:
                logger.warning(f"delete_entry_artifacts(entry={entry_id}): {p}: {e}")


def ensure_entry_bin_files(entry_id):
    """Ensure _l_u/_l_f/_p_u/_p_f bin files exist for pe<entry_id> BMPs.
    Delegates to the existing ensure_bin_files() since the naming convention is compatible."""
    return ensure_bin_files(entry_artifact_prefix(entry_id))


def _default_landscape_crop_img(img, rw, rh):
    """Return a 5:3 landscape crop of img (rw×rh are its current dims after rotation)."""
    tl = 5.0 / 3.0
    if rw / rh <= tl:
        wc, hc = rw, int(rw / tl); xc, yc = 0, (rh - hc) // 2
    else:
        hc, wc = rh, int(rh * tl); yc, xc = 0, (rw - wc) // 2
    return img.crop((xc, yc, xc + wc, yc + hc))


def _default_portrait_crop_img(img, rw, rh):
    """Return a 3:5 portrait crop of img (rw×rh are its current dims after rotation)."""
    tp = 3.0 / 5.0
    if rw / rh >= tp:
        hc, wc = rh, int(rh * tp); yc, xc = 0, (rw - wc) // 2
    else:
        wc, hc = rw, int(rw / tp); xc, yc = 0, (rh - hc) // 2
    return img.crop((xc, yc, xc + wc, yc + hc))


def convert_entry(entry_id):
    """Convert an image for a specific playlist_entry using the canonical pipeline:
    1. user rotate  2. crop  3. portrait device-rotate (before scale)
    4. scale to 800×480  5. colour adjust + dither.

    Artifacts stored as pe<entry_id>_l.bmp / pe<entry_id>_p.bmp and matching bins.
    Returns True on success.
    """
    from db import get_playlist_entry, get_image_by_uuid
    entry = get_playlist_entry(entry_id)
    if not entry:
        logger.error(f"convert_entry: entry {entry_id} not found")
        return False
    image = get_image_by_uuid(entry['image_uuid'])
    if not image:
        logger.error(f"convert_entry: image {entry['image_uuid']} not found")
        return False

    orig_fn  = image['original_filename']
    src_path = os.path.join(ORIGINALS_DIR, orig_fn)
    if not os.path.exists(src_path):
        logger.error(f"convert_entry: original not found: {src_path}")
        return False

    try:
        img = ImageOps.exif_transpose(Image.open(src_path)).convert('RGB')
        rw, rh = img.size

        # Extract params
        bg     = entry.get('bg_color', '#ffffff') or '#ffffff'
        hue_s  = float(entry.get('hue_shift',  0) or 0)
        sat    = float(entry.get('saturation',  1) or 1)
        val_a  = float(entry.get('value_adj',   1) or 1)
        r_gain = float(entry.get('r_gain',      1) or 1)
        g_gain = float(entry.get('g_gain',      1) or 1)
        b_gain = float(entry.get('b_gain',      1) or 1)
        rotate_deg = int(entry.get('rotate', 0) or 0) % 360

        # Step 1: user rotate
        if rotate_deg == 90:
            img = img.transpose(Image.Transpose.ROTATE_90)
        elif rotate_deg == 180:
            img = img.transpose(Image.Transpose.ROTATE_180)
        elif rotate_deg == 270:
            img = img.transpose(Image.Transpose.ROTATE_270)
        rw, rh = img.size

        prefix = entry_artifact_prefix(entry_id)

        # --- Landscape artifact ---
        if entry.get('crop_l_w') is not None:
            land = _crop_with_outfill(img, entry['crop_l_x'], entry['crop_l_y'],
                                      entry['crop_l_w'], entry['crop_l_h'], bg)
        else:
            land = _default_landscape_crop_img(img, rw, rh)
        # Step 4: scale to final dims
        land = land.resize((800, 480), Image.Resampling.LANCZOS)
        # Step 5: colour adjust + dither
        land = apply_color_adjustments(land, hue_s, sat, val_a, r_gain, g_gain, b_gain)
        Image.fromarray(
            dither_floyd_steinberg(np.array(land, dtype=np.float32), PALETTE)
        ).save(os.path.join(IMAGES_DIR, prefix + '_l.bmp'), format='BMP')

        # --- Portrait artifact ---
        if entry.get('crop_p_w') is not None:
            port = _crop_with_outfill(img, entry['crop_p_x'], entry['crop_p_y'],
                                      entry['crop_p_w'], entry['crop_p_h'], bg)
        else:
            port = _default_portrait_crop_img(img, rw, rh)
        # Step 3: device portrait rotate (BEFORE scale — keeps orientation identical to old output)
        port = port.rotate(270, expand=True)
        # Step 4: scale to 800×480 (now in landscape pixel order for the panel)
        port = port.resize((800, 480), Image.Resampling.LANCZOS)
        # Step 5: colour adjust + dither
        port = apply_color_adjustments(port, hue_s, sat, val_a, r_gain, g_gain, b_gain)
        Image.fromarray(
            dither_floyd_steinberg(np.array(port, dtype=np.float32), PALETTE)
        ).save(os.path.join(IMAGES_DIR, prefix + '_p.bmp'), format='BMP')

        # Invalidate old bins and regenerate
        for sfx in ('_l_u.bin', '_l_f.bin', '_p_u.bin', '_p_f.bin'):
            p = os.path.join(IMAGES_DIR, prefix + sfx)
            if os.path.exists(p):
                os.remove(p)
        ensure_entry_bin_files(entry_id)
        return True
    except Exception as e:
        logger.error(f"convert_entry({entry_id}): {e}")
        return False


def _default_landscape_crop_13in3(img, rw, rh):
    """4:3 landscape center crop for 13in3 entries."""
    t = 4.0 / 3.0
    if rw / rh <= t:
        wc, hc = rw, int(rw / t); xc, yc = 0, (rh - hc) // 2
    else:
        hc, wc = rh, int(rh * t); yc, xc = 0, (rw - wc) // 2
    return img.crop((xc, yc, xc + wc, yc + hc))


def _default_portrait_crop_13in3(img, rw, rh):
    """3:4 portrait center crop for 13in3 entries."""
    t = 3.0 / 4.0
    if rw / rh >= t:
        hc, wc = rh, int(rh * t); yc, xc = 0, (rw - wc) // 2
    else:
        wc, hc = rw, int(rw / t); xc, yc = 0, (rh - hc) // 2
    return img.crop((xc, yc, xc + wc, yc + hc))


def convert_entry_13in3(entry_id):
    """Convert a playlist entry for the 13.3" (1600×1200) screen.

    - Landscape: 4:3 center crop → resize (1600, 1200) → rotate 90° CW → 1200×1600 BMP
    - Portrait:  3:4 center crop → resize (1200, 1600) → 1200×1600 BMP
    Returns True on success.
    """
    from db import get_playlist_entry, get_image_by_uuid
    entry = get_playlist_entry(entry_id)
    if not entry:
        logger.error(f"convert_entry_13in3: entry {entry_id} not found")
        return False
    image = get_image_by_uuid(entry['image_uuid'])
    if not image:
        logger.error(f"convert_entry_13in3: image {entry['image_uuid']} not found")
        return False

    orig_fn  = image['original_filename']
    src_path = os.path.join(ORIGINALS_DIR, orig_fn)
    if not os.path.exists(src_path):
        logger.error(f"convert_entry_13in3: original not found: {src_path}")
        return False

    try:
        img = ImageOps.exif_transpose(Image.open(src_path)).convert('RGB')

        bg     = entry.get('bg_color', '#ffffff') or '#ffffff'
        hue_s  = float(entry.get('hue_shift',  0) or 0)
        sat    = float(entry.get('saturation',  1) or 1)
        val_a  = float(entry.get('value_adj',   1) or 1)
        r_gain = float(entry.get('r_gain',      1) or 1)
        g_gain = float(entry.get('g_gain',      1) or 1)
        b_gain = float(entry.get('b_gain',      1) or 1)
        rotate_deg = int(entry.get('rotate', 0) or 0) % 360

        # Step 1: user rotate
        if rotate_deg == 90:
            img = img.transpose(Image.Transpose.ROTATE_90)
        elif rotate_deg == 180:
            img = img.transpose(Image.Transpose.ROTATE_180)
        elif rotate_deg == 270:
            img = img.transpose(Image.Transpose.ROTATE_270)
        rw, rh = img.size

        prefix = entry_artifact_prefix(entry_id)

        # --- Landscape artifact: 4:3 crop → 1600×1200 → rotate 90° → 1200×1600 ---
        land = _default_landscape_crop_13in3(img, rw, rh)
        land = land.resize((1600, 1200), Image.Resampling.LANCZOS)
        land = apply_color_adjustments(land, hue_s, sat, val_a, r_gain, g_gain, b_gain)
        land = land.rotate(90, expand=True)  # → 1200×1600
        Image.fromarray(
            dither_floyd_steinberg(np.array(land, dtype=np.float32), PALETTE)
        ).save(os.path.join(IMAGES_DIR, prefix + '_l.bmp'), format='BMP')

        # --- Portrait artifact: 3:4 crop → 1200×1600 ---
        port = _default_portrait_crop_13in3(img, rw, rh)
        port = port.resize((1200, 1600), Image.Resampling.LANCZOS)
        port = apply_color_adjustments(port, hue_s, sat, val_a, r_gain, g_gain, b_gain)
        Image.fromarray(
            dither_floyd_steinberg(np.array(port, dtype=np.float32), PALETTE)
        ).save(os.path.join(IMAGES_DIR, prefix + '_p.bmp'), format='BMP')

        # Invalidate old bins and regenerate
        for sfx in ('_l_u.bin', '_l_f.bin', '_p_u.bin', '_p_f.bin'):
            p = os.path.join(IMAGES_DIR, prefix + sfx)
            if os.path.exists(p):
                os.remove(p)
        ensure_entry_bin_files_13in3(entry_id)
        return True
    except Exception as e:
        logger.error(f"convert_entry_13in3({entry_id}): {e}")
        return False


def ensure_entry_bin_files_13in3(entry_id):
    """Ensure 13in3 bin files for pe<entry_id> (no infix — entry names are unambiguous)."""
    prefix = entry_artifact_prefix(entry_id)
    ok = True
    for orient_sfx in ('_l', '_p'):
        bmp_path = os.path.join(IMAGES_DIR, prefix + orient_sfx + '.bmp')
        u_path   = os.path.join(IMAGES_DIR, prefix + orient_sfx + '_u.bin')
        f_path   = os.path.join(IMAGES_DIR, prefix + orient_sfx + '_f.bin')
        if not os.path.exists(bmp_path):
            ok = False; continue
        try:
            # Delete stale bins of the wrong size (e.g. old 800×480 artifacts)
            for p in (u_path, f_path):
                if os.path.exists(p) and os.path.getsize(p) != 960000:
                    os.remove(p)
            if not os.path.exists(u_path):
                img = Image.open(bmp_path).convert('RGB')
                if img.size != (1200, 1600):
                    logger.error(f"ensure_entry_bin_files_13in3: BMP wrong size {img.size} for entry {entry_id}; reconvert needed")
                    ok = False; continue
                data = rgb_array_to_spectra6_bitstream_13in3(np.array(img, dtype=np.uint8))
                with open(u_path, 'wb') as fh: fh.write(data)
            else:
                with open(u_path, 'rb') as fh: data = fh.read()
            if not os.path.exists(f_path):
                with open(f_path, 'wb') as fh: fh.write(_flip_bitstream_13in3(data))
        except Exception as e:
            logger.error(f"ensure_entry_bin_files_13in3(entry={entry_id}){orient_sfx}: {e}"); ok = False
    return ok


def ensure_entry_bin_files_for_screen(entry_id, w, h):
    """Dispatch to the appropriate entry bin-file ensurer based on target screen size."""
    if (w, h) == _DEFAULT_SCREEN:
        return ensure_entry_bin_files(entry_id)
    if (w, h) == (1600, 1200):
        return ensure_entry_bin_files_13in3(entry_id)
    logger.warning(f"No entry bin handler for {w}x{h}; skipping entry {entry_id}")
    return False


def convert_entry_for_screen(entry_id, w=None, h=None):
    """Convert a playlist entry for its playlist's screen size (auto-detected if not given)."""
    if w is None or h is None:
        from db import get_playlist_entry
        entry = get_playlist_entry(entry_id)
        if not entry:
            logger.error(f"convert_entry_for_screen: entry {entry_id} not found")
            return False
        sizes = get_screen_types_for_playlist(entry['playlist_id'])
        w, h = next(iter(sizes)) if sizes else _DEFAULT_SCREEN
    if (w, h) == _DEFAULT_SCREEN:
        return convert_entry(entry_id)
    if (w, h) == (1600, 1200):
        return convert_entry_13in3(entry_id)
    logger.warning(f"No entry converter for {w}x{h}; entry {entry_id}")
    return False


def reconvert_for_playlist_screen(base, playlist_id):
    """
    Delete artifacts for the screen types used by *playlist_id*, then reconvert
    *base* from scratch for those sizes.  Other playlists' screen types are left
    untouched.  Crops are honoured.
    """
    src = _find_original(base)
    if not src:
        logger.warning(f"reconvert_for_playlist_screen: no original for {base}")
        return False
    sizes = get_screen_types_for_playlist(playlist_id)
    ok = True
    for w, h in sizes:
        delete_artifacts_for_screen(base, w, h)
        if not convert_image_for_screen(src, base, w, h):
            ok = False
    return ok

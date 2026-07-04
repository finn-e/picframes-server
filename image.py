import os
import logging
import numpy as np
from PIL import Image, ImageOps
from db import (ORIGINALS_DIR, IMAGES_DIR, LANDSCAPE_SUFFIX, PORTRAIT_SUFFIX,
                load_crops, load_image_order, save_image_order)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Screen-type registry
# hw_profile → (landscape_width, landscape_height)
# ---------------------------------------------------------------------------

SCREEN_TYPES = {
    'ESP32-S3-PhotoPainter': (800, 480),
    'XIAO-EE04-7in3':        (800, 480),
    'XIAO-EE04-13in3':       (1600, 1200),
}
_DEFAULT_SCREEN = (800, 480)


def screen_size_for_profile(hw_profile):
    """Return (landscape_w, landscape_h) for a hw_profile string."""
    return SCREEN_TYPES.get(hw_profile or '', _DEFAULT_SCREEN)


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
    """Convert an uploaded image to 800×480 landscape + portrait BMPs and bins."""
    try:
        img = ImageOps.exif_transpose(Image.open(src_path)).convert('RGB')
        w, h = img.size
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

        order = load_image_order()
        if base not in order:
            order.append(base); save_image_order(order)

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

        # Landscape crop → 1600×1200, then rotate 90° → 1200×1600 on panel
        tl = 4.0 / 3.0  # 1600:1200
        if w / h <= tl:
            wc, hc = w, int(w / tl); xc, yc = 0, int(ol * max(h - hc, 0))
        else:
            hc, wc = h, int(h * tl); yc, xc = 0, int(0.5 * (w - wc))
        land = img.crop((xc, yc, xc + wc, yc + hc)).resize((1600, 1200), Image.Resampling.LANCZOS)
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
        from db import get_playlist_images, get_playlist_devices, load_config
        images  = get_playlist_images(playlist_id)
        dev_macs = get_playlist_devices(playlist_id)
        cfg = load_config()
        hw_profiles = set()
        for mac in dev_macs:
            dev = next((d for d in cfg.get('devices', []) if d['mac'].lower() == mac.lower()), None)
            if dev and dev.get('hw_profile'):
                hw_profiles.add(dev['hw_profile'])
        # Always ensure 800×480 artifacts exist (used as proxy for general pool checks)
        hw_profiles.add('ESP32-S3-PhotoPainter')
        for hw in hw_profiles:
            w, h = screen_size_for_profile(hw)
            for base in images:
                ensure_converted_for_screen(base, w, h)
    except Exception as e:
        logger.error(f"ensure_artifacts_for_playlist({playlist_id}): {e}")

import numpy as np
from PIL import Image

try:
    from dither_rs import dither_floyd_steinberg as _dither_rs_impl
    _RUST_DITHER = True
except ImportError:
    _RUST_DITHER = False

PALETTE = np.array([
    [0,   0,   0  ],
    [255, 255, 255],
    [0,   255, 0  ],
    [0,   0,   255],
    [255, 0,   0  ],
    [255, 255, 0  ],
], dtype=np.float32)


def dither_floyd_steinberg(img_array, palette):
    if _RUST_DITHER:
        return _dither_rs_impl(
            np.ascontiguousarray(img_array, dtype=np.float32),
            np.ascontiguousarray(palette, dtype=np.float32),
        )
    # Pure-Python fallback (used when dither_rs extension is not compiled)
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


def apply_color_adjustments(img, hue_shift=0.0, saturation=1.0, value_adj=1.0,
                             r_gain=1.0, g_gain=1.0, b_gain=1.0):
    if (hue_shift == 0 and saturation == 1.0 and value_adj == 1.0
            and r_gain == 1.0 and g_gain == 1.0 and b_gain == 1.0):
        return img
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


def _crop_with_outfill(img, crop_x, crop_y, crop_w, crop_h, bg_color='#ffffff'):
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
    left  = hw_idx[:, :600]
    left_packed  = (left[:, 0::2] << 4) | left[:, 1::2]
    right = hw_idx[:, 600:]
    right_packed = (right[:, 0::2] << 4) | right[:, 1::2]
    return left_packed.tobytes() + right_packed.tobytes()


def _flip_bitstream(data):
    """180° rotation for full-width row-major bitstreams (small screen only)."""
    return bytes(((b & 0x0F) << 4) | ((b & 0xF0) >> 4) for b in reversed(data))


def _flip_bitstream_13in3(data):
    """
    180° rotation for 13.3" left|right split bitstream.
    Reverses within each half independently then keeps halves in place.
    """
    assert len(data) == 960000
    left_half  = data[:480000]
    right_half = data[480000:]
    flip_left  = bytes(((b & 0x0F) << 4) | ((b & 0xF0) >> 4) for b in reversed(left_half))
    flip_right = bytes(((b & 0x0F) << 4) | ((b & 0xF0) >> 4) for b in reversed(right_half))
    return flip_left + flip_right


# --- Default crop helpers ---

def _default_landscape_crop_img(img, rw, rh):
    """5:3 landscape center crop."""
    tl = 5.0 / 3.0
    if rw / rh <= tl:
        wc, hc = rw, int(rw / tl); xc, yc = 0, (rh - hc) // 2
    else:
        hc, wc = rh, int(rh * tl); yc, xc = 0, (rw - wc) // 2
    return img.crop((xc, yc, xc + wc, yc + hc))


def _default_portrait_crop_img(img, rw, rh):
    """3:5 portrait center crop."""
    tp = 3.0 / 5.0
    if rw / rh >= tp:
        hc, wc = rh, int(rh * tp); yc, xc = 0, (rw - wc) // 2
    else:
        wc, hc = rw, int(rw / tp); xc, yc = 0, (rh - hc) // 2
    return img.crop((xc, yc, xc + wc, yc + hc))


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

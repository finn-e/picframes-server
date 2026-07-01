import os
import logging
import numpy as np
from PIL import Image, ImageOps
from db import (ORIGINALS_DIR, IMAGES_DIR, LANDSCAPE_SUFFIX, PORTRAIT_SUFFIX,
                load_crops, load_image_order, save_image_order)

logger = logging.getLogger(__name__)

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


def rgb_array_to_spectra6_bitstream_fast(img_array):
    h, w, _ = img_array.shape
    hardware_map = np.array([0, 1, 6, 5, 3, 2], dtype=np.uint8)
    pixels = img_array.reshape(-1, 3)
    dists  = np.sum((pixels[:, None, :] - PALETTE[None, :, :])**2, axis=2)
    hw_idx = hardware_map[np.argmin(dists, axis=1)].reshape(h, w)
    packed = (hw_idx[:, 0::2] << 4) | hw_idx[:, 1::2]
    return packed.tobytes()


def _flip_bitstream(data):
    return bytes(((b & 0x0F) << 4) | ((b & 0xF0) >> 4) for b in reversed(data))


def ensure_bin_files(base):
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


def ensure_bin_file(base, orientation):
    ensure_bin_files(base)
    u = os.path.join(IMAGES_DIR, base + ('_l_u.bin' if orientation == 'landscape' else '_p_u.bin'))
    return u if os.path.exists(u) else None


def convert_image(src_path, base):
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

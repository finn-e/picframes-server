import logging
import os
import re

import numpy as np
from PIL import Image, ImageOps

from db import (ORIGINALS_DIR, IMAGES_DIR, LANDSCAPE_SUFFIX, PORTRAIT_SUFFIX,
                load_crops, get_image_edits)
from . import pipeline as _pipeline
from .constants import (
    _DEFAULT_SCREEN, SCREEN_TYPES, ratios_for_screen,
    screen_size_for_profile, screen_spec_for_profile, _artifact_infix,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Entry artifact path helpers (new naming: pe{id}_{W}x{H}_{ratio}_{flip}.bin)
# ---------------------------------------------------------------------------

def entry_artifact_prefix(entry_id):
    return f'pe{entry_id}'


def entry_bin_path(entry_id, W, H, ratio, flip):
    """e.g. entry_bin_path(14, 800, 480, 'l53', 'u') → IMAGES_DIR/pe14_800x480_l53_u.bin"""
    return os.path.join(IMAGES_DIR, f'pe{entry_id}_{W}x{H}_{ratio}_{flip}.bin')


def entry_bmp_path(entry_id, W, H, orient):
    """e.g. entry_bmp_path(14, 800, 480, 'l') → IMAGES_DIR/pe14_800x480_l.bmp"""
    return os.path.join(IMAGES_DIR, f'pe{entry_id}_{W}x{H}_{orient}.bmp')


# ---------------------------------------------------------------------------
# Old-name cleanup
# ---------------------------------------------------------------------------

_OLD_BIN_RE = re.compile(r'^pe\d+_[lp](?:_[uf])?\.bin$')


def wipe_old_named_bins(entry_id):
    """Delete bins using the pre-resolution naming scheme."""
    for sfx in ('_l_u.bin', '_l_f.bin', '_p_u.bin', '_p_f.bin', '_l.bin', '_p.bin'):
        p = os.path.join(IMAGES_DIR, f'pe{entry_id}{sfx}')
        if os.path.exists(p):
            try:
                os.remove(p)
            except OSError as e:
                logger.warning(f'wipe_old_named_bins({entry_id}): {p}: {e}')


def wipe_all_old_named_bins():
    """Startup sweep: delete any old-format pe{id}_l*.bin / pe{id}_p*.bin files."""
    try:
        for fname in os.listdir(IMAGES_DIR):
            if _OLD_BIN_RE.match(fname):
                try:
                    os.remove(os.path.join(IMAGES_DIR, fname))
                    logger.info(f'wipe_all_old_named_bins: removed {fname}')
                except OSError as e:
                    logger.warning(f'wipe_all_old_named_bins: {fname}: {e}')
    except Exception as e:
        logger.error(f'wipe_all_old_named_bins: {e}')


# ---------------------------------------------------------------------------
# Entry artifacts — ensure bins
# ---------------------------------------------------------------------------

def ensure_bins(entry_id, W, H):
    """Ensure all 4 bin files exist for entry at given resolution.

    Reads pe{id}_{W}x{H}_{orient}.bmp, serialises to bitstream, writes _u.bin
    and derives _f.bin via flip transform.  Returns True if all 4 bins present.
    """
    l_ratio, p_ratio = ratios_for_screen(W, H)
    expected = (W * H) // 2
    is_13in3 = (W, H) == (1600, 1200)
    ok = True

    for orient, ratio in (('l', l_ratio), ('p', p_ratio)):
        bmp_path = entry_bmp_path(entry_id, W, H, orient)
        u_path   = entry_bin_path(entry_id, W, H, ratio, 'u')
        f_path   = entry_bin_path(entry_id, W, H, ratio, 'f')

        if not os.path.exists(bmp_path):
            logger.warning(f'ensure_bins({entry_id},{W}x{H}): BMP missing {bmp_path}')
            ok = False; continue

        try:
            # Purge wrong-size bins
            for p in (u_path, f_path):
                if os.path.exists(p) and os.path.getsize(p) != expected:
                    os.remove(p)

            if not os.path.exists(u_path):
                img = Image.open(bmp_path).convert('RGB')
                arr = np.array(img, dtype=np.uint8)
                if is_13in3:
                    if arr.shape[:2] != (1600, 1200):
                        logger.error(f'ensure_bins: 13in3 BMP wrong shape {arr.shape} entry {entry_id}')
                        ok = False; continue
                    data = _pipeline.rgb_array_to_spectra6_bitstream_13in3(arr)
                else:
                    data = _pipeline.rgb_array_to_spectra6_bitstream_fast(arr)
                with open(u_path, 'wb') as fh:
                    fh.write(data)
            else:
                with open(u_path, 'rb') as fh:
                    data = fh.read()

            if not os.path.exists(f_path):
                flip_fn = _pipeline._flip_bitstream_13in3 if is_13in3 else _pipeline._flip_bitstream
                with open(f_path, 'wb') as fh:
                    fh.write(flip_fn(data))

        except Exception as e:
            logger.error(f'ensure_bins({entry_id},{W}x{H},{orient}): {e}')
            ok = False

    return ok


def entry_artifacts_ready(entry_id, W, H):
    """Return True if all 4 bin files exist at the correct size for (W,H)."""
    l_ratio, p_ratio = ratios_for_screen(W, H)
    expected = (W * H) // 2
    for ratio, flip in ((l_ratio, 'u'), (l_ratio, 'f'), (p_ratio, 'u'), (p_ratio, 'f')):
        p = entry_bin_path(entry_id, W, H, ratio, flip)
        if not os.path.exists(p) or os.path.getsize(p) != expected:
            return False
    return True


# ---------------------------------------------------------------------------
# Entry conversion  (unified — dispatches on W×H)
# ---------------------------------------------------------------------------

def _load_entry_and_image(entry_id):
    from db import get_playlist_entry, get_image_by_uuid
    entry = get_playlist_entry(entry_id)
    if not entry:
        logger.error(f'convert_entry: entry {entry_id} not found')
        return None, None, None
    image = get_image_by_uuid(entry['image_uuid'])
    if not image:
        logger.error(f'convert_entry: image {entry["image_uuid"]} not found')
        return None, None, None
    src_path = os.path.join(ORIGINALS_DIR, image['original_filename'])
    if not os.path.exists(src_path):
        logger.error(f'convert_entry: original not found: {src_path}')
        return None, None, None
    return entry, image, src_path


def _entry_color_params(entry):
    return (
        entry.get('bg_color', '#ffffff') or '#ffffff',
        float(entry.get('hue_shift',  0) or 0),
        float(entry.get('saturation',  1) or 1),
        float(entry.get('value_adj',   1) or 1),
        float(entry.get('r_gain',      1) or 1),
        float(entry.get('g_gain',      1) or 1),
        float(entry.get('b_gain',      1) or 1),
        int(entry.get('rotate', 0) or 0) % 360,
    )


def _apply_user_rotate(img, rotate_deg):
    if rotate_deg == 90:
        return img.transpose(Image.Transpose.ROTATE_90)
    if rotate_deg == 180:
        return img.transpose(Image.Transpose.ROTATE_180)
    if rotate_deg == 270:
        return img.transpose(Image.Transpose.ROTATE_270)
    return img


def convert_entry(entry_id, W=800, H=480):
    """Convert a playlist entry to BMPs and bins for the given resolution.

    For 800×480: landscape uses crop_l_* (5:3), portrait uses crop_p_* (3:5).
    For 1600×1200: landscape uses crop_l43_* (4:3), portrait uses crop_p34_* (3:4).
    Artifacts: pe{id}_{W}x{H}_{orient}.bmp + pe{id}_{W}x{H}_{ratio}_{flip}.bin
    """
    entry, _image, src_path = _load_entry_and_image(entry_id)
    if entry is None:
        return False

    try:
        img = ImageOps.exif_transpose(Image.open(src_path)).convert('RGB')
        bg, hue_s, sat, val_a, r_gain, g_gain, b_gain, rotate_deg = _entry_color_params(entry)
        img = _apply_user_rotate(img, rotate_deg)
        rw, rh = img.size

        wipe_old_named_bins(entry_id)

        if (W, H) == (800, 480):
            _convert_entry_800x480(entry_id, entry, img, rw, rh, bg, hue_s, sat, val_a, r_gain, g_gain, b_gain)
        elif (W, H) == (1600, 1200):
            _convert_entry_1600x1200(entry_id, entry, img, rw, rh, bg, hue_s, sat, val_a, r_gain, g_gain, b_gain)
        else:
            logger.warning(f'convert_entry: unsupported resolution {W}x{H} for entry {entry_id}')
            return False

        # Invalidate stale new-named bins before regenerating
        l_ratio, p_ratio = ratios_for_screen(W, H)
        for ratio, flip in ((l_ratio, 'u'), (l_ratio, 'f'), (p_ratio, 'u'), (p_ratio, 'f')):
            p = entry_bin_path(entry_id, W, H, ratio, flip)
            if os.path.exists(p):
                os.remove(p)

        ensure_bins(entry_id, W, H)
        return True
    except Exception as e:
        logger.error(f'convert_entry({entry_id},{W}x{H}): {e}')
        return False


def _convert_entry_800x480(entry_id, entry, img, rw, rh, bg, hue_s, sat, val_a, r_gain, g_gain, b_gain):
    # Landscape: 5:3 crop → 800×480
    if entry.get('crop_l_w') is not None:
        land = _pipeline._crop_with_outfill(img, entry['crop_l_x'], entry['crop_l_y'],
                                            entry['crop_l_w'], entry['crop_l_h'], bg)
    else:
        land = _pipeline._default_landscape_crop_img(img, rw, rh)
    land = land.resize((800, 480), Image.Resampling.LANCZOS)
    land = _pipeline.apply_color_adjustments(land, hue_s, sat, val_a, r_gain, g_gain, b_gain)
    Image.fromarray(
        _pipeline.dither_floyd_steinberg(np.array(land, dtype=np.float32), _pipeline.PALETTE)
    ).save(entry_bmp_path(entry_id, 800, 480, 'l'), format='BMP')

    # Portrait: 3:5 crop → rotate 270° → 800×480 (landscape pixel order)
    if entry.get('crop_p_w') is not None:
        port = _pipeline._crop_with_outfill(img, entry['crop_p_x'], entry['crop_p_y'],
                                            entry['crop_p_w'], entry['crop_p_h'], bg)
    else:
        port = _pipeline._default_portrait_crop_img(img, rw, rh)
    port = port.rotate(270, expand=True)
    port = port.resize((800, 480), Image.Resampling.LANCZOS)
    port = _pipeline.apply_color_adjustments(port, hue_s, sat, val_a, r_gain, g_gain, b_gain)
    Image.fromarray(
        _pipeline.dither_floyd_steinberg(np.array(port, dtype=np.float32), _pipeline.PALETTE)
    ).save(entry_bmp_path(entry_id, 800, 480, 'p'), format='BMP')


def _convert_entry_1600x1200(entry_id, entry, img, rw, rh, bg, hue_s, sat, val_a, r_gain, g_gain, b_gain):
    # Landscape: 4:3 crop → 1600×1200 → rotate 90° → 1200×1600 (panel native)
    if entry.get('crop_l43_w') is not None:
        land = _pipeline._crop_with_outfill(img, entry['crop_l43_x'], entry['crop_l43_y'],
                                            entry['crop_l43_w'], entry['crop_l43_h'], bg)
    else:
        land = _pipeline._default_landscape_crop_13in3(img, rw, rh)
    land = land.resize((1600, 1200), Image.Resampling.LANCZOS)
    land = _pipeline.apply_color_adjustments(land, hue_s, sat, val_a, r_gain, g_gain, b_gain)
    land = land.rotate(90, expand=True)  # → 1200×1600
    Image.fromarray(
        _pipeline.dither_floyd_steinberg(np.array(land, dtype=np.float32), _pipeline.PALETTE)
    ).save(entry_bmp_path(entry_id, 1600, 1200, 'l'), format='BMP')

    # Portrait: 3:4 crop → 1200×1600 (panel native, no rotation)
    if entry.get('crop_p34_w') is not None:
        port = _pipeline._crop_with_outfill(img, entry['crop_p34_x'], entry['crop_p34_y'],
                                            entry['crop_p34_w'], entry['crop_p34_h'], bg)
    else:
        port = _pipeline._default_portrait_crop_13in3(img, rw, rh)
    port = port.resize((1200, 1600), Image.Resampling.LANCZOS)
    port = _pipeline.apply_color_adjustments(port, hue_s, sat, val_a, r_gain, g_gain, b_gain)
    Image.fromarray(
        _pipeline.dither_floyd_steinberg(np.array(port, dtype=np.float32), _pipeline.PALETTE)
    ).save(entry_bmp_path(entry_id, 1600, 1200, 'p'), format='BMP')


def convert_entry_13in3(entry_id):
    """Backward-compat alias for convert_entry(entry_id, 1600, 1200)."""
    return convert_entry(entry_id, 1600, 1200)


def ensure_entry_bin_files(entry_id):
    """Backward-compat alias for ensure_bins(entry_id, 800, 480)."""
    return ensure_bins(entry_id, 800, 480)


def ensure_entry_bin_files_13in3(entry_id):
    """Backward-compat alias for ensure_bins(entry_id, 1600, 1200)."""
    return ensure_bins(entry_id, 1600, 1200)


def ensure_entry_bin_files_for_screen(entry_id, w, h):
    """Backward-compat alias for ensure_bins."""
    return ensure_bins(entry_id, w, h)


def convert_entry_for_screen(entry_id, w=None, h=None):
    """Convert a playlist entry for its playlist's screen size (auto-detected if not given)."""
    if w is None or h is None:
        from db import get_playlist_entry
        entry = get_playlist_entry(entry_id)
        if not entry:
            logger.error(f'convert_entry_for_screen: entry {entry_id} not found')
            return False
        sizes = get_screen_types_for_playlist(entry['playlist_id'])
        w, h = next(iter(sizes)) if sizes else _DEFAULT_SCREEN
    return convert_entry(entry_id, w, h)


def delete_entry_artifacts(entry_id):
    """Delete all BMP and bin artifacts for a playlist entry."""
    prefix = entry_artifact_prefix(entry_id)
    try:
        for fname in os.listdir(IMAGES_DIR):
            if fname.startswith(prefix + '_') and (fname.endswith('.bmp') or fname.endswith('.bin')):
                try:
                    os.remove(os.path.join(IMAGES_DIR, fname))
                except OSError as e:
                    logger.warning(f'delete_entry_artifacts({entry_id}): {e}')
    except Exception as e:
        logger.error(f'delete_entry_artifacts({entry_id}): {e}')


# ---------------------------------------------------------------------------
# General pool (base-image) functions — left as-is for now
# ---------------------------------------------------------------------------

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
                Image.fromarray(
                    _pipeline.dither_floyd_steinberg(np.array(img, dtype=np.float32), _pipeline.PALETTE)
                ).save(dith_path, format='PNG')
                return dith_path
            except Exception as e:
                logger.error(f'Dither overlay error for {base}: {e}')
                return None
    return None


def artifact_suffixes_for_screen(w, h):
    infix = _artifact_infix(w, h)
    return [
        infix + '_l.bmp', infix + '_p.bmp',
        infix + '_l_u.bin', infix + '_l_f.bin',
        infix + '_p_u.bin', infix + '_p_f.bin',
    ]


def artifact_suffixes_all():
    suffixes = set()
    for size in set(SCREEN_TYPES.values()):
        suffixes.update(artifact_suffixes_for_screen(*size))
    suffixes.update(['_l.bin', '_p.bin', '_dithered.png'])
    return list(suffixes)


def delete_artifacts_for_screen(base, w, h):
    for sfx in artifact_suffixes_for_screen(w, h):
        p = os.path.join(IMAGES_DIR, base + sfx)
        if os.path.exists(p):
            try:
                os.remove(p)
            except OSError as e:
                logger.warning(f'delete_artifacts_for_screen: {p}: {e}')


def delete_all_artifacts(base):
    for sfx in artifact_suffixes_all():
        p = os.path.join(IMAGES_DIR, base + sfx)
        if os.path.exists(p):
            try:
                os.remove(p)
            except OSError as e:
                logger.warning(f'delete_all_artifacts: {p}: {e}')


def get_screen_types_for_playlist(playlist_id):
    """Return set of (w,h) tuples for the playlist's devices (no owner scoping)."""
    try:
        from db import get_db
        conn = get_db()
        rows = conn.execute(
            """SELECT d.hw_profile FROM playlist_devices pd
               JOIN devices d ON d.mac = pd.mac
               WHERE pd.playlist_id = ?""",
            (int(playlist_id),)
        ).fetchall()
        conn.close()
        sizes = set()
        for row in rows:
            if row['hw_profile']:
                sizes.add(screen_size_for_profile(row['hw_profile']))
        return sizes if sizes else {_DEFAULT_SCREEN}
    except Exception as e:
        logger.error(f'get_screen_types_for_playlist({playlist_id}): {e}')
        return {_DEFAULT_SCREEN}


def get_screen_types_for_image(base):
    try:
        from db import get_playlists_for_image
        pids = get_playlists_for_image(base)
        sizes = set()
        for pid in pids:
            sizes.update(get_screen_types_for_playlist(pid))
        return sizes
    except Exception as e:
        logger.error(f'get_screen_types_for_image({base}): {e}')
        return set()


def ensure_bin_files(base):
    """Ensure 800×480 bin files for a base image (general pool, old naming)."""
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
                data = _pipeline.rgb_array_to_spectra6_bitstream_fast(np.array(img, dtype=np.uint8))
                with open(u_path, 'wb') as fh: fh.write(data)
            else:
                with open(u_path, 'rb') as fh: data = fh.read()
            if not os.path.exists(f_path):
                with open(f_path, 'wb') as fh: fh.write(_pipeline._flip_bitstream(data))
        except Exception as e:
            logger.error(f'ensure_bin_files({base}{orient_sfx}): {e}'); ok = False
    return ok


def ensure_bin_files_13in3(base):
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
                assert img.size == (1200, 1600)
                data = _pipeline.rgb_array_to_spectra6_bitstream_13in3(np.array(img, dtype=np.uint8))
                with open(u_path, 'wb') as fh: fh.write(data)
            else:
                with open(u_path, 'rb') as fh: data = fh.read()
            if not os.path.exists(f_path):
                with open(f_path, 'wb') as fh: fh.write(_pipeline._flip_bitstream_13in3(data))
        except Exception as e:
            logger.error(f'ensure_bin_files_13in3({base}{orient_sfx}): {e}'); ok = False
    return ok


def ensure_bin_files_for_screen(base, w, h):
    if (w, h) == _DEFAULT_SCREEN:
        return ensure_bin_files(base)
    if (w, h) == (1600, 1200):
        return ensure_bin_files_13in3(base)
    logger.warning(f'No bin-file handler for {w}x{h}; skipping {base}')
    return False


def ensure_bin_file(base, orientation):
    ensure_bin_files(base)
    u = os.path.join(IMAGES_DIR, base + ('_l_u.bin' if orientation == 'landscape' else '_p_u.bin'))
    return u if os.path.exists(u) else None


def _find_original(base):
    for f in os.listdir(ORIGINALS_DIR):
        if os.path.splitext(f)[0] == base:
            return os.path.join(ORIGINALS_DIR, f)
    return None


def convert_image(src_path, base):
    """Convert an uploaded image to 800×480 BMPs and bins (general pool)."""
    try:
        img = ImageOps.exif_transpose(Image.open(src_path)).convert('RGB')
        w, h = img.size
        edits = get_image_edits(base)

        if edits and edits.get('crop_l_w') is not None:
            bg = edits.get('bg_color', '#ffffff') or '#ffffff'
            hue_s  = float(edits.get('hue_shift',  0) or 0)
            sat    = float(edits.get('saturation',  1) or 1)
            val_a  = float(edits.get('value_adj',   1) or 1)
            r_gain = float(edits.get('r_gain',      1) or 1)
            g_gain = float(edits.get('g_gain',      1) or 1)
            b_gain = float(edits.get('b_gain',      1) or 1)
            rotate_deg = int(edits.get('rotate', 0) or 0) % 360
            img = _apply_user_rotate(img, rotate_deg)

            land = _pipeline._crop_with_outfill(img, edits['crop_l_x'], edits['crop_l_y'],
                                                edits['crop_l_w'], edits['crop_l_h'], bg)
            land = land.resize((800, 480), Image.Resampling.LANCZOS)
            land = _pipeline.apply_color_adjustments(land, hue_s, sat, val_a, r_gain, g_gain, b_gain)
            Image.fromarray(
                _pipeline.dither_floyd_steinberg(np.array(land, dtype=np.float32), _pipeline.PALETTE)
            ).save(os.path.join(IMAGES_DIR, base + LANDSCAPE_SUFFIX), format='BMP')

            if edits.get('crop_p_w') is not None:
                port = _pipeline._crop_with_outfill(img, edits['crop_p_x'], edits['crop_p_y'],
                                                    edits['crop_p_w'], edits['crop_p_h'], bg)
            else:
                rw, rh = img.size
                crops   = load_crops()
                offsets = crops.get(base, {"l": 0.5, "p": 0.5})
                op = offsets.get("p", 0.5)
                tp = 3.0 / 5.0
                if rw / rh >= tp:
                    hc, wc = rh, int(rh * tp); yc, xc = 0, int(op * (rw - wc))
                else:
                    wc, hc = rw, int(rw / tp); xc, yc = 0, int(0.5 * (rh - hc))
                port = img.crop((xc, yc, xc + wc, yc + hc))
            port = port.resize((480, 800), Image.Resampling.LANCZOS)
            port = _pipeline.apply_color_adjustments(port, hue_s, sat, val_a, r_gain, g_gain, b_gain)
            Image.fromarray(
                _pipeline.dither_floyd_steinberg(np.array(port, dtype=np.float32), _pipeline.PALETTE)
            ).rotate(270, expand=True).save(os.path.join(IMAGES_DIR, base + PORTRAIT_SUFFIX), format='BMP')

        else:
            crops   = load_crops()
            offsets = crops.get(base, {"l": 0.5, "p": 0.5})
            ol, op  = offsets.get("l", 0.5), offsets.get("p", 0.5)

            tl = 5.0 / 3.0
            if w / h <= tl:
                wc, hc = w, int(w / tl); xc, yc = 0, int(ol * (h - hc))
            else:
                hc, wc = h, int(h * tl); yc, xc = 0, int(0.5 * (w - wc))
            land = img.crop((xc, yc, xc + wc, yc + hc)).resize((800, 480), Image.Resampling.LANCZOS)
            Image.fromarray(
                _pipeline.dither_floyd_steinberg(np.array(land, dtype=np.float32), _pipeline.PALETTE)
            ).save(os.path.join(IMAGES_DIR, base + LANDSCAPE_SUFFIX), format='BMP')

            tp = 3.0 / 5.0
            if w / h >= tp:
                hc, wc = h, int(h * tp); yc, xc = 0, int(op * (w - wc))
            else:
                wc, hc = w, int(w / tp); xc, yc = 0, int(0.5 * (h - hc))
            port = img.crop((xc, yc, xc + wc, yc + hc)).resize((480, 800), Image.Resampling.LANCZOS)
            Image.fromarray(
                _pipeline.dither_floyd_steinberg(np.array(port, dtype=np.float32), _pipeline.PALETTE)
            ).rotate(270, expand=True).save(os.path.join(IMAGES_DIR, base + PORTRAIT_SUFFIX), format='BMP')

        for sfx in ('_l_u.bin', '_l_f.bin', '_p_u.bin', '_p_f.bin', '_l.bin', '_p.bin'):
            p = os.path.join(IMAGES_DIR, base + sfx)
            if os.path.exists(p): os.remove(p)

        ensure_bin_files(base)
        return True
    except Exception as e:
        logger.error(f'convert_image({base}): {e}'); return False


def convert_image_13in3(src_path, base):
    infix = '_1600x1200'
    try:
        img = ImageOps.exif_transpose(Image.open(src_path)).convert('RGB')
        w, h = img.size
        crops   = load_crops()
        offsets = crops.get(base, {"l": 0.5, "p": 0.5})
        ol, op  = offsets.get("l", 0.5), offsets.get("p", 0.5)
        edits  = get_image_edits(base)
        hue_s  = float(edits['hue_shift']  or 0) if edits else 0
        sat    = float(edits['saturation'] or 1) if edits else 1.0
        val_a  = float(edits['value_adj']  or 1) if edits else 1.0
        r_gain = float(edits['r_gain']     or 1) if edits else 1.0
        g_gain = float(edits['g_gain']     or 1) if edits else 1.0
        b_gain = float(edits['b_gain']     or 1) if edits else 1.0

        tl = 4.0 / 3.0
        if w / h <= tl:
            wc, hc = w, int(w / tl); xc, yc = 0, int(ol * max(h - hc, 0))
        else:
            hc, wc = h, int(h * tl); yc, xc = 0, int(0.5 * (w - wc))
        land = img.crop((xc, yc, xc + wc, yc + hc)).resize((1600, 1200), Image.Resampling.LANCZOS)
        land = _pipeline.apply_color_adjustments(land, hue_s, sat, val_a, r_gain, g_gain, b_gain)
        land_panel = land.rotate(90, expand=True)
        Image.fromarray(
            _pipeline.dither_floyd_steinberg(np.array(land_panel, dtype=np.float32), _pipeline.PALETTE)
        ).save(os.path.join(IMAGES_DIR, base + infix + '_l.bmp'), format='BMP')

        tp = 3.0 / 4.0
        if w / h >= tp:
            hc, wc = h, int(h * tp); yc, xc = 0, int(op * max(w - wc, 0))
        else:
            wc, hc = w, int(w / tp); xc, yc = 0, int(0.5 * (h - hc))
        port = img.crop((xc, yc, xc + wc, yc + hc)).resize((1200, 1600), Image.Resampling.LANCZOS)
        port = _pipeline.apply_color_adjustments(port, hue_s, sat, val_a, r_gain, g_gain, b_gain)
        Image.fromarray(
            _pipeline.dither_floyd_steinberg(np.array(port, dtype=np.float32), _pipeline.PALETTE)
        ).save(os.path.join(IMAGES_DIR, base + infix + '_p.bmp'), format='BMP')

        for sfx in (infix + '_l_u.bin', infix + '_l_f.bin', infix + '_p_u.bin', infix + '_p_f.bin'):
            p = os.path.join(IMAGES_DIR, base + sfx)
            if os.path.exists(p): os.remove(p)

        ensure_bin_files_13in3(base)
        return True
    except Exception as e:
        logger.error(f'convert_image_13in3({base}): {e}'); return False


def convert_image_for_screen(src_path, base, w, h):
    if (w, h) == _DEFAULT_SCREEN:
        return convert_image(src_path, base)
    if (w, h) == (1600, 1200):
        return convert_image_13in3(src_path, base)
    logger.warning(f'No converter for {w}x{h}; skipping {base}')
    return False


def ensure_converted_for_screen(base, w, h):
    infix = _artifact_infix(w, h)
    l_bmp = os.path.join(IMAGES_DIR, base + infix + '_l.bmp')
    p_bmp = os.path.join(IMAGES_DIR, base + infix + '_p.bmp')
    if os.path.exists(l_bmp) and os.path.exists(p_bmp):
        return ensure_bin_files_for_screen(base, w, h)
    src_path = _find_original(base)
    if not src_path:
        logger.warning(f'ensure_converted_for_screen: no original for {base}')
        return False
    return convert_image_for_screen(src_path, base, w, h)


def ensure_artifacts_for_playlist(playlist_id):
    try:
        from db import get_playlist_images
        images = get_playlist_images(playlist_id)
        sizes  = get_screen_types_for_playlist(playlist_id)
        for w, h in sizes:
            for base in images:
                ensure_converted_for_screen(base, w, h)
    except Exception as e:
        logger.error(f'ensure_artifacts_for_playlist({playlist_id}): {e}')


def reconvert_all_intelligent():
    from db import get_playlists_for_image, ALLOWED_EXTENSIONS
    for f in sorted(os.listdir(ORIGINALS_DIR)):
        ext = os.path.splitext(f)[1].lower()
        if ext not in ALLOWED_EXTENSIONS:
            continue
        base = os.path.splitext(f)[0]
        src  = os.path.join(ORIGINALS_DIR, f)
        delete_all_artifacts(base)
        sizes = get_screen_types_for_image(base)
        for w, h in sizes:
            convert_image_for_screen(src, base, w, h)


def reconvert_for_playlist_screen(base, playlist_id):
    src = _find_original(base)
    if not src:
        logger.warning(f'reconvert_for_playlist_screen: no original for {base}')
        return False
    sizes = get_screen_types_for_playlist(playlist_id)
    ok = True
    for w, h in sizes:
        delete_artifacts_for_screen(base, w, h)
        if not convert_image_for_screen(src, base, w, h):
            ok = False
    return ok

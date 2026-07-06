# Conversion pipeline: convert_entry artifacts, rotate param, out-of-bounds crop
# outfill, and one tiny run of the real Floyd-Steinberg dither.
import os

import numpy as np
from PIL import Image

import db
from image import (PALETTE, _crop_with_outfill, convert_entry,
                   dither_floyd_steinberg, entry_artifact_prefix)


def _make_entry(base='photo', size=(100, 70), color=(200, 60, 60), **edits):
    """Create original + playlist + entry directly via db layer; returns entry_id."""
    path = os.path.join(db.ORIGINALS_DIR, base + '.jpg')
    Image.new('RGB', size, color).save(path, format='JPEG')
    order = db.load_image_order(owner_id=1)
    if base not in order:
        order.append(base); db.save_image_order(order, owner_id=1)
    pid = db.create_playlist('convtest', owner_id=1)
    uuid = db.get_or_create_image(base, owner_id=1)
    eid = db.add_playlist_entry(pid, uuid, title=base)
    if edits:
        db.update_playlist_entry(eid, **edits)
    return eid


def test_convert_entry_produces_pe_artifacts_with_right_dimensions():
    eid = _make_entry()
    assert convert_entry(eid) is True
    prefix = entry_artifact_prefix(eid)
    assert prefix == f'pe{eid}'

    l_bmp = os.path.join(db.IMAGES_DIR, prefix + '_l.bmp')
    p_bmp = os.path.join(db.IMAGES_DIR, prefix + '_p.bmp')
    assert Image.open(l_bmp).size == (800, 480)
    # Portrait artifact is device-rotated into landscape pixel order
    assert Image.open(p_bmp).size == (800, 480)

    for sfx in ('_l_u.bin', '_l_f.bin', '_p_u.bin', '_p_f.bin'):
        p = os.path.join(db.IMAGES_DIR, prefix + sfx)
        assert os.path.exists(p), sfx
        assert os.path.getsize(p) == 800 * 480 // 2  # 4bpp packed


def test_convert_entry_rotate_90_changes_output():
    """Rotate is applied before crop: a left-red/right-blue image produces
    different artifacts when rotated 90 deg (the split becomes horizontal)."""
    def two_tone(base, rotate):
        path = os.path.join(db.ORIGINALS_DIR, base + '.jpg')
        img = Image.new('RGB', (100, 70), (255, 0, 0))
        img.paste(Image.new('RGB', (50, 70), (0, 0, 255)), (50, 0))
        img.save(path, format='JPEG', quality=95)
        # NOTE: load_image_order's first-run init branch scans ORIGINALS_DIR and
        # may already contain *base*; appending unconditionally violates the
        # image_order PK and trips the db.py connection-leak bug (see bugs.md).
        order = db.load_image_order(owner_id=1)
        if base not in order:
            order.append(base); db.save_image_order(order, owner_id=1)
        pid = db.create_playlist('rot' + base, owner_id=1)
        uuid = db.get_or_create_image(base, owner_id=1)
        eid = db.add_playlist_entry(pid, uuid, title=base)
        if rotate:
            db.update_playlist_entry(eid, rotate=rotate)
        assert convert_entry(eid)
        return Image.open(os.path.join(
            db.IMAGES_DIR, entry_artifact_prefix(eid) + '_l.bmp'))

    plain   = two_tone('tt0', 0)
    rotated = two_tone('tt90', 90)
    # Unrotated: left/right split → top-left red-ish, top-right blue-ish
    assert plain.getpixel((10, 240)) != plain.getpixel((790, 240))
    # Rotated 90: split is now top/bottom → left and right edges match
    assert rotated.getpixel((10, 240)) == rotated.getpixel((790, 240))
    assert rotated.getpixel((400, 10)) != rotated.getpixel((400, 470))


def test_crop_with_outfill_out_of_bounds_uses_bg_color():
    img = Image.new('RGB', (100, 70), (200, 60, 60))
    out = _crop_with_outfill(img, -50, -50, 100, 100, '#00ff00')
    assert out.size == (100, 100)
    assert out.getpixel((0, 0)) == (0, 255, 0)      # outfilled corner
    assert out.getpixel((99, 99)) == (200, 60, 60)  # image content
    # Bad bg string falls back to white
    out = _crop_with_outfill(img, -10, -10, 50, 50, 'nonsense')
    assert out.getpixel((0, 0)) == (255, 255, 255)


def test_convert_entry_out_of_bounds_crop_outfills():
    eid = _make_entry(base='oob', crop_l_x=-100, crop_l_y=-60,
                      crop_l_w=100, crop_l_h=60, bg_color='#00ff00')
    assert convert_entry(eid) is True
    l_bmp = Image.open(os.path.join(db.IMAGES_DIR,
                                    entry_artifact_prefix(eid) + '_l.bmp'))
    assert l_bmp.size == (800, 480)
    # Crop rect is entirely out of bounds → whole output is bg color
    # (green is in the device palette, so it survives quantisation exactly)
    assert l_bmp.getpixel((400, 240)) == (0, 255, 0)


def test_convert_entry_missing_entry_returns_false():
    assert convert_entry(999999) is False


def test_flip_bin_is_bitreversed_variant():
    eid = _make_entry(base='flip')
    convert_entry(eid)
    prefix = entry_artifact_prefix(eid)
    u = open(os.path.join(db.IMAGES_DIR, prefix + '_l_u.bin'), 'rb').read()
    f = open(os.path.join(db.IMAGES_DIR, prefix + '_l_f.bin'), 'rb').read()
    assert len(u) == len(f)
    # flipping twice returns the original
    from image import _flip_bitstream
    assert _flip_bitstream(f) == u


def test_real_dither_tiny():
    """Exercise the REAL Floyd-Steinberg dither (unpatched) on a tiny array."""
    rng = np.random.default_rng(42)
    arr = rng.integers(0, 256, size=(12, 16, 3)).astype(np.float32)
    out = dither_floyd_steinberg(arr, PALETTE)
    assert out.shape == (12, 16, 3)
    palette_set = {tuple(p) for p in PALETTE.astype(int)}
    for px in out.reshape(-1, 3):
        assert tuple(int(v) for v in px) in palette_set

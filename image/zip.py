import glob
import hashlib
import io
import json
import os
import zipfile

from db import IMAGES_DIR, ZIPS_DIR
from image.constants import ratios_for_screen
from image.artifacts import ensure_bins, entry_bin_path


def build_entry_zip(mac, entries, orient_char, flip_char, scr_w, scr_h, serializable_cfg):
    """Build a per-device zip for playlist entries. Returns (zip_bytes: bytes, zip_version: str).

    Zip arcnames are always {title}_{l|p}.bin — the firmware contract.
    """
    l_ratio, p_ratio = ratios_for_screen(scr_w, scr_h)
    ratio = l_ratio if orient_char == 'l' else p_ratio
    zip_suffix = f'_{orient_char}.bin'

    entry_titles = [e['title'] for e in entries]
    zip_version = hashlib.md5((','.join(entry_titles) + orient_char + flip_char).encode()).hexdigest()[:8]

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode='w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        zf.writestr('config.json', json.dumps(serializable_cfg, indent=2))
        zip_names = [e['title'] + zip_suffix for e in entries]
        manifest = json.dumps(zip_names, indent=2)
        zf.writestr('index.json', manifest)
        zf.writestr('list.json', manifest)
        for entry in entries:
            ensure_bins(entry['id'], scr_w, scr_h)
            bin_path = entry_bin_path(entry['id'], scr_w, scr_h, ratio, flip_char)
            if os.path.exists(bin_path):
                zf.write(bin_path, arcname=entry['title'] + zip_suffix)

    return buf.getvalue(), zip_version


def cache_path(mac, zip_version):
    mac_nodots = mac.replace(':', '')
    return os.path.join(ZIPS_DIR, f'{mac_nodots}_{zip_version}.zip')


def serve_cached_or_build(mac, entries, orient_char, flip_char, scr_w, scr_h,
                           serializable_cfg, force=False):
    """Return (zip_bytes, zip_version), using cache when valid.

    Builds and caches the zip if missing or force=True; purges stale versions.
    """
    l_ratio, p_ratio = ratios_for_screen(scr_w, scr_h)
    entry_titles = [e['title'] for e in entries]
    zip_version = hashlib.md5((','.join(entry_titles) + orient_char + flip_char).encode()).hexdigest()[:8]

    path = cache_path(mac, zip_version)

    if not force and os.path.exists(path):
        with open(path, 'rb') as f:
            return f.read(), zip_version

    zip_bytes, _ = build_entry_zip(mac, entries, orient_char, flip_char, scr_w, scr_h, serializable_cfg)

    tmp = path + '.tmp'
    with open(tmp, 'wb') as f:
        f.write(zip_bytes)
    os.replace(tmp, path)

    _purge_old_zips(mac, zip_version)

    return zip_bytes, zip_version


def _purge_old_zips(mac, keep_version):
    mac_nodots = mac.replace(':', '')
    for p in glob.glob(os.path.join(ZIPS_DIR, f'{mac_nodots}_*.zip')):
        if not p.endswith(f'_{keep_version}.zip'):
            try:
                os.remove(p)
            except OSError:
                pass


def invalidate_device_zip(mac):
    """Delete all cached zips for a device (e.g. on orientation change)."""
    mac_nodots = mac.replace(':', '')
    for p in glob.glob(os.path.join(ZIPS_DIR, f'{mac_nodots}_*.zip')):
        try:
            os.remove(p)
        except OSError:
            pass

# ==========================================================================================
# DESCRIPTION: Tests for multi-resolution device support:
#   - alias normalization at /api/register
#   - explicit resolution overriding the type default
#   - resolution persistence on the devices row
#   - playlist resolution lock set on first device assignment
#   - mismatch rejection (409)
#   - lock cleared when last device is removed from a playlist
# ==========================================================================================
import pytest

from tests.conftest import (
    TEST_MAC, device_headers, make_original, register_device, setup_playlist_device,
)
import db
from image import normalize_device_type, DEVICE_TYPE_ALIASES, SCREEN_TYPES


# ---------------------------------------------------------------------------
# normalize_device_type / alias map
# ---------------------------------------------------------------------------

def test_alias_normalization_xiao_7in3():
    assert normalize_device_type('XIAO-EE04-7in3') == 'Seeed-EE04-Spectra6-7in3'


def test_alias_normalization_xiao_13in3():
    assert normalize_device_type('XIAO-EE04-13in3') == 'Seeed-EE04-Spectra6-13in3'


def test_alias_normalization_photpainter():
    assert normalize_device_type('ESP32-S3-PhotoPainter') == 'Waveshare-PhotoPainter-7in3'


def test_canonical_names_pass_through():
    for name in ('Seeed-EE04-Spectra6-7in3', 'Seeed-EE04-Spectra6-13in3',
                 'Waveshare-PhotoPainter-7in3'):
        assert normalize_device_type(name) == name


def test_unknown_type_passes_through():
    assert normalize_device_type('SomeFutureThing') == 'SomeFutureThing'


def test_canonical_names_in_screen_types():
    for name in ('Seeed-EE04-Spectra6-7in3', 'Seeed-EE04-Spectra6-13in3',
                 'Waveshare-PhotoPainter-7in3'):
        assert name in SCREEN_TYPES


# ---------------------------------------------------------------------------
# /api/register stores canonical hw_profile
# ---------------------------------------------------------------------------

def test_register_normalizes_old_type(client, logged_in):
    """Registering with an old firmware type stores the canonical name."""
    r = client.post('/api/register', json={
        'mac': TEST_MAC, 'username': 'admin', 'password': 'admin',
        'hw_profile': 'XIAO-EE04-7in3',
    })
    assert r.status_code == 200
    conn = db.get_db()
    row = conn.execute("SELECT hw_profile FROM devices WHERE mac=?",
                       (TEST_MAC,)).fetchone()
    conn.close()
    assert row['hw_profile'] == 'Seeed-EE04-Spectra6-7in3'


def test_register_normalizes_photpainter(client, logged_in):
    r = client.post('/api/register', json={
        'mac': TEST_MAC, 'username': 'admin', 'password': 'admin',
        'hw_profile': 'ESP32-S3-PhotoPainter',
    })
    assert r.status_code == 200
    conn = db.get_db()
    row = conn.execute("SELECT hw_profile FROM devices WHERE mac=?",
                       (TEST_MAC,)).fetchone()
    conn.close()
    assert row['hw_profile'] == 'Waveshare-PhotoPainter-7in3'


# ---------------------------------------------------------------------------
# Explicit resolution at /api/register wins over type default
# ---------------------------------------------------------------------------

def test_register_explicit_resolution_string(client, logged_in):
    """Explicit resolution='1200x1600' is stored on the device row."""
    r = client.post('/api/register', json={
        'mac': TEST_MAC, 'username': 'admin', 'password': 'admin',
        'hw_profile': 'Seeed-EE04-Spectra6-7in3',
        'resolution': '1200x1600',
    })
    assert r.status_code == 200
    res = db.get_device_resolution(TEST_MAC)
    assert res == '1200x1600'


def test_register_explicit_resolution_list(client, logged_in):
    """Explicit resolution=[800, 480] (list form) is stored as '800x480'."""
    r = client.post('/api/register', json={
        'mac': TEST_MAC, 'username': 'admin', 'password': 'admin',
        'resolution': [800, 480],
    })
    assert r.status_code == 200
    res = db.get_device_resolution(TEST_MAC)
    assert res == '800x480'


def test_register_type_default_13in3_stored(client, logged_in):
    """13in3 type default (1600x1200) is persisted even without explicit resolution."""
    r = client.post('/api/register', json={
        'mac': TEST_MAC, 'username': 'admin', 'password': 'admin',
        'hw_profile': 'Seeed-EE04-Spectra6-13in3',
    })
    assert r.status_code == 200
    res = db.get_device_resolution(TEST_MAC)
    assert res == '1600x1200'


def test_register_no_type_no_resolution_default(client, logged_in):
    """Old firmware sends neither hw_profile nor resolution: device row keeps NULL resolution."""
    r = client.post('/api/register', json={
        'mac': TEST_MAC, 'username': 'admin', 'password': 'admin',
    })
    assert r.status_code == 200
    res = db.get_device_resolution(TEST_MAC)
    # NULL — defaults to 800x480 at use time; we do NOT store the default
    assert res is None


# ---------------------------------------------------------------------------
# Playlist resolution lock
# ---------------------------------------------------------------------------

MAC2 = 'aa:bb:cc:dd:ee:f2'


def test_playlist_locked_on_first_assignment(logged_in):
    """Assigning the first device locks the playlist to that device's resolution."""
    client = logged_in
    r = client.post('/playlists/create', json={'name': 'TestPL'})
    pid = r.get_json()['id']

    # Register a 13in3 device
    client.post('/api/register', json={
        'mac': TEST_MAC, 'username': 'admin', 'password': 'admin',
        'hw_profile': 'Seeed-EE04-Spectra6-13in3',
    })
    r = client.post('/device_playlist', json={'mac': TEST_MAC, 'playlist_id': pid})
    assert r.status_code == 200
    assert r.get_json()['ok'] is True

    locked = db.get_playlist_resolution(pid)
    assert locked == '1600x1200'


def test_playlist_lock_mismatch_rejected(logged_in):
    """Assigning a device whose resolution differs from the locked playlist → 409."""
    client = logged_in
    r = client.post('/playlists/create', json={'name': 'TestPL'})
    pid = r.get_json()['id']

    # First device: 13in3
    client.post('/api/register', json={
        'mac': TEST_MAC, 'username': 'admin', 'password': 'admin',
        'hw_profile': 'Seeed-EE04-Spectra6-13in3',
    })
    r = client.post('/device_playlist', json={'mac': TEST_MAC, 'playlist_id': pid})
    assert r.status_code == 200

    # Second device: 7in3 (800x480 — different from 1600x1200)
    client.post('/api/register', json={
        'mac': MAC2, 'username': 'admin', 'password': 'admin',
        'hw_profile': 'Seeed-EE04-Spectra6-7in3',
    })
    r = client.post('/device_playlist', json={'mac': MAC2, 'playlist_id': pid})
    assert r.status_code == 409
    assert 'mismatch' in r.get_json().get('error', '').lower()


def test_playlist_same_resolution_second_device_ok(logged_in):
    """Two 7in3 devices (same resolution) can both be in the same playlist."""
    client = logged_in
    r = client.post('/playlists/create', json={'name': 'TestPL'})
    pid = r.get_json()['id']

    for mac in (TEST_MAC, MAC2):
        client.post('/api/register', json={
            'mac': mac, 'username': 'admin', 'password': 'admin',
            'hw_profile': 'Seeed-EE04-Spectra6-7in3',
        })

    r = client.post('/device_playlist', json={'mac': TEST_MAC, 'playlist_id': pid})
    assert r.status_code == 200
    r = client.post('/device_playlist', json={'mac': MAC2, 'playlist_id': pid})
    assert r.status_code == 200


def test_playlist_unlocked_when_last_device_removed(logged_in):
    """Removing the only device from a playlist clears its resolution lock."""
    client = logged_in
    r = client.post('/playlists/create', json={'name': 'TestPL'})
    pid = r.get_json()['id']

    client.post('/api/register', json={
        'mac': TEST_MAC, 'username': 'admin', 'password': 'admin',
        'hw_profile': 'Seeed-EE04-Spectra6-13in3',
    })
    r = client.post('/device_playlist', json={'mac': TEST_MAC, 'playlist_id': pid})
    assert r.status_code == 200
    assert db.get_playlist_resolution(pid) == '1600x1200'

    # Remove the device
    r = client.post('/device_playlist', json={'mac': TEST_MAC, 'playlist_id': None})
    assert r.status_code == 200
    assert db.get_playlist_resolution(pid) is None


def test_playlist_stays_locked_with_remaining_devices(logged_in):
    """Removing one of two devices does NOT unlock the playlist."""
    client = logged_in
    r = client.post('/playlists/create', json={'name': 'TestPL'})
    pid = r.get_json()['id']

    for mac in (TEST_MAC, MAC2):
        client.post('/api/register', json={
            'mac': mac, 'username': 'admin', 'password': 'admin',
            'hw_profile': 'Seeed-EE04-Spectra6-7in3',
        })
        client.post('/device_playlist', json={'mac': mac, 'playlist_id': pid})

    # Remove first device only
    client.post('/device_playlist', json={'mac': TEST_MAC, 'playlist_id': None})
    # Lock should still be set because MAC2 remains
    assert db.get_playlist_resolution(pid) is not None


def test_old_firmware_no_type_uses_default_resolution(logged_in):
    """Old firmware (no hw_profile, no resolution) → treated as 800x480; can join an 800x480 playlist."""
    client = logged_in
    r = client.post('/playlists/create', json={'name': 'TestPL'})
    pid = r.get_json()['id']

    # Old-style registration (no type)
    client.post('/api/register', json={
        'mac': TEST_MAC, 'username': 'admin', 'password': 'admin',
    })

    r = client.post('/device_playlist', json={'mac': TEST_MAC, 'playlist_id': pid})
    assert r.status_code == 200
    # Playlist should be locked to 800x480
    assert db.get_playlist_resolution(pid) == '800x480'


# ------------------------------------------------------------------------------
# OTA update asset lookup: release zips are named after the OLD board dirs, so
# canonical hw_profiles must fall back to their alias names (routes/api.py
# _get_update_url).
# ------------------------------------------------------------------------------

def test_update_url_falls_back_to_alias_asset_names(monkeypatch):
    import routes.api as api_mod
    monkeypatch.setitem(api_mod._github_cache, 'tag', '9.9.9')
    monkeypatch.setitem(api_mod._github_cache, 'assets', {
        'ESP32-S3-PhotoPainter': 'http://example/pp.zip',
        'XIAO-EE04-7in3': 'http://example/xiao.zip',
    })
    # no_github autouse fixture already stubs _update_github_cache to a no-op
    assert api_mod._get_update_url('Waveshare-PhotoPainter-7in3', '0.1.0') == 'http://example/pp.zip'
    assert api_mod._get_update_url('Seeed-EE04-Spectra6-7in3', '0.1.0') == 'http://example/xiao.zip'
    # old names still resolve directly
    assert api_mod._get_update_url('XIAO-EE04-7in3', '0.1.0') == 'http://example/xiao.zip'
    # up-to-date firmware gets nothing
    assert api_mod._get_update_url('XIAO-EE04-7in3', '9.9.9') is None

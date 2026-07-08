# Daily-config + daily-zip device contract.
import io
import zipfile

import db
from tests.conftest import (TEST_MAC, device_headers, setup_playlist_device, register_device)


def _zip_names(resp):
    return set(zipfile.ZipFile(io.BytesIO(resp.data)).namelist())


def test_daily_config_images_are_entry_titles(logged_in):
    pid, eids, _ = setup_playlist_device(logged_in, bases=('alpha', 'beta'))
    logged_in.post(f'/playlists/{pid}/rename_entry/{eids[0]}', json={'title': 'Beach Day'})
    r = logged_in.get('/api/daily-config', headers=device_headers())
    body = r.get_json()
    assert body['images'] == ['Beach_Day', 'beta']


def test_daily_config_respects_entry_orientation_toggle(logged_in):
    pid, eids, _ = setup_playlist_device(logged_in, bases=('alpha', 'beta'))
    # Device default orientation is landscape; disable landscape for one entry
    logged_in.post(f'/entry-toggle-orient/{eids[0]}', json={'orient': 'l', 'enabled': False})
    r = logged_in.get('/api/daily-config', headers=device_headers())
    assert r.get_json()['images'] == ['beta']


def test_daily_zip_contents(logged_in):
    pid, eids, _ = setup_playlist_device(logged_in, bases=('alpha', 'beta'))
    r = logged_in.get('/api/daily-zip', headers=device_headers())
    assert r.status_code == 200
    assert r.mimetype == 'application/zip'
    names = _zip_names(r)
    # Device orientation defaults to landscape → _l.bin arcnames named by title
    assert names == {'config.json', 'index.json', 'list.json',
                     'alpha_l.bin', 'beta_l.bin'}


def test_daily_zip_manifest_matches_bins(logged_in):
    import json
    setup_playlist_device(logged_in, bases=('alpha',))
    r = logged_in.get('/api/daily-zip', headers=device_headers())
    zf = zipfile.ZipFile(io.BytesIO(r.data))
    manifest = json.loads(zf.read('index.json'))
    assert manifest == ['alpha_l.bin']
    assert json.loads(zf.read('list.json')) == manifest
    cfg = json.loads(zf.read('config.json'))
    assert 'timer' in cfg and 'shuffle' in cfg and 'sync_images' in cfg


def test_daily_zip_304_on_matching_version(logged_in):
    setup_playlist_device(logged_in, bases=('alpha',))
    # setup assigns the device, which sets the redownload flag; consume it first.
    logged_in.get('/api/daily-zip', headers=device_headers())
    version = logged_in.get('/api/daily-config',
                            headers=device_headers()).get_json()['daily_zip_version']
    r = logged_in.get(f'/api/daily-zip?version={version}', headers=device_headers())
    assert r.status_code == 304
    # Non-matching version → full zip
    r = logged_in.get('/api/daily-zip?version=00000000', headers=device_headers())
    assert r.status_code == 200


def test_zip_version_changes_when_entries_change(logged_in):
    pid, eids, _ = setup_playlist_device(logged_in, bases=('alpha',))
    v1 = logged_in.get('/api/daily-config',
                       headers=device_headers()).get_json()['daily_zip_version']
    from tests.conftest import make_original
    make_original('gamma')
    r = logged_in.post(f'/playlists/{pid}/add_image', json={'base': 'gamma'})
    assert r.status_code == 200
    v2 = logged_in.get('/api/daily-config',
                       headers=device_headers()).get_json()['daily_zip_version']
    assert v1 != v2
    # Old version now stale → zip served, not 304
    r = logged_in.get(f'/api/daily-zip?version={v1}', headers=device_headers())
    assert r.status_code == 200


def test_zip_version_changes_on_rename(logged_in):
    pid, eids, _ = setup_playlist_device(logged_in, bases=('alpha',))
    v1 = logged_in.get('/api/daily-config',
                       headers=device_headers()).get_json()['daily_zip_version']
    logged_in.post(f'/playlists/{pid}/rename_entry/{eids[0]}', json={'title': 'renamed'})
    v2 = logged_in.get('/api/daily-config',
                       headers=device_headers()).get_json()['daily_zip_version']
    assert v1 != v2


def test_daily_zip_clears_redownload_flag(logged_in):
    setup_playlist_device(logged_in, bases=('alpha',))
    db.trigger_redownload(TEST_MAC)
    assert db.load_state()['redownload'][TEST_MAC] is True
    logged_in.get('/api/daily-zip', headers=device_headers())
    assert db.load_state()['redownload'][TEST_MAC] is False


def test_daily_zip_force_redownload_bypasses_304(logged_in):
    """When trigger_redownload is set, daily-zip returns the full zip even if the
    version matches — so the device can recover from a missed/empty download."""
    setup_playlist_device(logged_in, bases=('alpha',))
    # Consume the setup-triggered redownload so we have a clean baseline.
    logged_in.get('/api/daily-zip', headers=device_headers())
    version = logged_in.get('/api/daily-config',
                            headers=device_headers()).get_json()['daily_zip_version']
    # Manually flag for redownload.
    db.trigger_redownload(TEST_MAC)
    r = logged_in.get(f'/api/daily-zip?version={version}', headers=device_headers())
    assert r.status_code == 200  # forced, not 304
    assert db.load_state()['redownload'][TEST_MAC] is False  # flag cleared


def test_fw_version_stored_from_header(logged_in):
    """X-Firmware-Version header on /api/daily-config is persisted to devices.fw_version."""
    register_device(logged_in, TEST_MAC)
    hdrs = dict(device_headers())
    hdrs['X-Firmware-Version'] = '1.2.3'
    logged_in.get('/api/daily-config', headers=hdrs)
    cfg = db.load_config(owner_id=1)
    dev = next(d for d in cfg['devices'] if d['mac'] == TEST_MAC)
    assert dev['fw_version'] == '1.2.3'


def test_fw_version_not_overwritten_if_same(logged_in):
    """fw_version is only updated when it actually changes."""
    register_device(logged_in, TEST_MAC)
    hdrs = dict(device_headers())
    hdrs['X-Firmware-Version'] = '2.0.0'
    logged_in.get('/api/daily-config', headers=hdrs)
    # Second call with same version — should not error
    r = logged_in.get('/api/daily-config', headers=hdrs)
    assert r.status_code == 200
    cfg = db.load_config(owner_id=1)
    dev = next(d for d in cfg['devices'] if d['mac'] == TEST_MAC)
    assert dev['fw_version'] == '2.0.0'


def test_show_fw_toggle_endpoint(logged_in):
    """POST /device_show_fw toggles the show_fw flag on a device."""
    register_device(logged_in, TEST_MAC)
    r = logged_in.post('/device_show_fw',
                       json={'mac': TEST_MAC, 'show_fw': True},
                       content_type='application/json')
    assert r.status_code == 200
    assert r.get_json()['ok'] is True
    assert r.get_json()['show_fw'] is True
    cfg = db.load_config(owner_id=1)
    dev = next(d for d in cfg['devices'] if d['mac'] == TEST_MAC)
    assert dev['show_fw'] is True
    # Toggle off
    logged_in.post('/device_show_fw',
                   json={'mac': TEST_MAC, 'show_fw': False},
                   content_type='application/json')
    cfg = db.load_config(owner_id=1)
    dev = next(d for d in cfg['devices'] if d['mac'] == TEST_MAC)
    assert dev['show_fw'] is False


def test_daily_config_includes_show_fw_version(logged_in):
    """show_fw_version field is returned in /api/daily-config response."""
    register_device(logged_in, TEST_MAC)
    r = logged_in.get('/api/daily-config', headers=device_headers())
    body = r.get_json()
    assert 'show_fw_version' in body
    assert body['show_fw_version'] is False

    # Enable show_fw and re-check
    logged_in.post('/device_show_fw',
                   json={'mac': TEST_MAC, 'show_fw': True},
                   content_type='application/json')
    r = logged_in.get('/api/daily-config', headers=device_headers())
    assert r.get_json()['show_fw_version'] is True

# Daily-config + daily-zip device contract.
import io
import zipfile

import db
from tests.conftest import (TEST_MAC, device_headers, setup_playlist_device)


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


def test_daily_zip_clears_redownload_even_on_304(logged_in):
    setup_playlist_device(logged_in, bases=('alpha',))
    version = logged_in.get('/api/daily-config',
                            headers=device_headers()).get_json()['daily_zip_version']
    db.trigger_redownload(TEST_MAC)
    r = logged_in.get(f'/api/daily-zip?version={version}', headers=device_headers())
    assert r.status_code == 304
    assert db.load_state()['redownload'][TEST_MAC] is False

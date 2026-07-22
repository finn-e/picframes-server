# Playlist-entry flow: add, rename (sanitization + uniqueness), edit round-trip, preview.
import os

import db
from image import entry_artifact_prefix, entry_bmp_path, entry_bin_path
from tests.conftest import make_original


def _create_playlist(client, name='TestPL'):
    r = client.post('/admin/playlists/create', json={'name': name})
    assert r.status_code == 200
    return r.get_json()['id']


def _add_entry(client, pid, base='photo'):
    r = client.post(f'/admin/playlists/{pid}/add_image', json={'base': base})
    assert r.status_code == 200, r.get_data(as_text=True)
    return r.get_json()['entry_id']


def test_add_image_creates_entry_with_sanitized_title(logged_in):
    make_original('My Photo')
    pid = _create_playlist(logged_in)
    eid = _add_entry(logged_in, pid, 'My Photo')
    entry = db.get_playlist_entry(eid)
    assert entry['title'] == 'My_Photo'
    assert entry['playlist_id'] == pid
    assert entry['enabled_l'] == 1 and entry['enabled_p'] == 1


def test_add_image_produces_entry_artifacts(logged_in):
    make_original('photo')
    pid = _create_playlist(logged_in)
    eid = _add_entry(logged_in, pid)
    assert os.path.exists(entry_bmp_path(eid, 800, 480, 'l')), '_800x480_l.bmp'
    assert os.path.exists(entry_bmp_path(eid, 800, 480, 'p')), '_800x480_p.bmp'
    for ratio, flip in (('l53', 'u'), ('l53', 'f'), ('p35', 'u'), ('p35', 'f')):
        assert os.path.exists(entry_bin_path(eid, 800, 480, ratio, flip)), f'{ratio}_{flip}.bin'


def test_rename_entry_sanitizes(logged_in):
    make_original('photo')
    pid = _create_playlist(logged_in)
    eid = _add_entry(logged_in, pid)
    r = logged_in.post(f'/admin/playlists/{pid}/rename_entry/{eid}',
                       json={'title': 'Beach Day!'})
    assert r.status_code == 200
    assert r.get_json()['title'] == 'Beach_Day'
    assert db.get_playlist_entry(eid)['title'] == 'Beach_Day'


def test_rename_entry_uniqueness_suffix(logged_in):
    make_original('photo')
    make_original('other')
    pid = _create_playlist(logged_in)
    e1 = _add_entry(logged_in, pid, 'photo')
    e2 = _add_entry(logged_in, pid, 'other')
    r1 = logged_in.post(f'/admin/playlists/{pid}/rename_entry/{e1}', json={'title': 'Beach Day'})
    assert r1.get_json()['title'] == 'Beach_Day'
    r2 = logged_in.post(f'/admin/playlists/{pid}/rename_entry/{e2}', json={'title': 'Beach Day'})
    assert r2.get_json()['title'] == 'Beach_Day_2'


def test_rename_entry_wrong_playlist_404(logged_in):
    make_original('photo')
    pid = _create_playlist(logged_in)
    eid = _add_entry(logged_in, pid)
    r = logged_in.post(f'/admin/playlists/{pid + 99}/rename_entry/{eid}', json={'title': 'x'})
    assert r.status_code == 404


def test_entry_edit_save_get_roundtrip_including_rotate(logged_in):
    make_original('photo')
    pid = _create_playlist(logged_in)
    eid = _add_entry(logged_in, pid)

    params = {
        'crop_l': {'x': -10, 'y': 5, 'w': 100, 'h': 60},
        'crop_p': {'x': 2, 'y': 3, 'w': 42, 'h': 70},
        'hue_shift': 15, 'saturation': 1.2, 'value_adj': 0.9,
        'r_gain': 1.1, 'g_gain': 0.95, 'b_gain': 1.0,
        'bg_color': '#00ff00', 'rotate': 90,
    }
    r = logged_in.post(f'/admin/entry-edit/{eid}', json=params)
    assert r.status_code == 200 and r.get_json()['ok']

    got = logged_in.get(f'/admin/entry-edit/{eid}').get_json()
    assert got['rotate'] == 90
    assert got['bg_color'] == '#00ff00'
    assert got['crop_l'] == {'x': -10, 'y': 5, 'w': 100, 'h': 60}
    assert got['crop_p'] == {'x': 2, 'y': 3, 'w': 42, 'h': 70}
    assert got['hue_shift'] == 15
    assert abs(got['saturation'] - 1.2) < 1e-9
    assert abs(got['value_adj'] - 0.9) < 1e-9


def test_entry_edit_get_defaults_for_neutral_entry(logged_in):
    make_original('photo')
    pid = _create_playlist(logged_in)
    eid = _add_entry(logged_in, pid)
    got = logged_in.get(f'/admin/entry-edit/{eid}').get_json()
    assert got['crop_l'] is None and got['crop_p'] is None
    assert got['rotate'] == 0 and got['saturation'] == 1


def test_entry_edit_unknown_entry_404(logged_in):
    assert logged_in.get('/admin/entry-edit/9999').status_code == 404
    assert logged_in.post('/admin/entry-edit/9999', json={}).status_code == 404


def test_entry_preview_returns_png(logged_in):
    make_original('photo')
    pid = _create_playlist(logged_in)
    eid = _add_entry(logged_in, pid)
    r = logged_in.post(f'/admin/entry-preview/{eid}', json={'rotate': 90})
    assert r.status_code == 200
    assert r.mimetype == 'image/png'
    assert r.data[:8] == b'\x89PNG\r\n\x1a\n'


def test_entry_toggle_orient(logged_in):
    make_original('photo')
    pid = _create_playlist(logged_in)
    eid = _add_entry(logged_in, pid)
    r = logged_in.post(f'/admin/entry-toggle-orient/{eid}',
                       json={'orient': 'p', 'enabled': False})
    assert r.status_code == 200
    assert db.get_playlist_entry(eid)['enabled_p'] == 0


def test_remove_entry_deletes_row_and_artifacts(logged_in):
    make_original('photo')
    pid = _create_playlist(logged_in)
    eid = _add_entry(logged_in, pid)
    assert os.path.exists(entry_bmp_path(eid, 800, 480, 'l'))
    r = logged_in.post(f'/admin/playlists/{pid}/remove_entry/{eid}')
    assert r.status_code == 200
    assert db.get_playlist_entry(eid) is None
    assert not os.path.exists(entry_bmp_path(eid, 800, 480, 'l'))


def test_playlist_cap_of_10_entries(logged_in):
    pid = _create_playlist(logged_in)
    for i in range(10):
        make_original(f'img{i}')
        _add_entry(logged_in, pid, f'img{i}')
    make_original('overflow')
    r = logged_in.post(f'/admin/playlists/{pid}/add_image', json={'base': 'overflow'})
    assert r.status_code == 400

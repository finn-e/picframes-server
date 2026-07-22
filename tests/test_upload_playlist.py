# Tests for upload endpoint with playlist_id (drop-to-playlist feature).
import io
from PIL import Image
import db
from tests.conftest import make_original


def _jpeg_bytes(name='photo', size=(50, 40)):
    buf = io.BytesIO()
    Image.new('RGB', size, (200, 100, 50)).save(buf, format='JPEG')
    buf.seek(0)
    return buf.read()


def _create_playlist(client, name='TestPL'):
    r = client.post('/admin/playlists/create', json={'name': name})
    assert r.status_code == 200
    return r.get_json()['id']


def test_upload_without_playlist_id_still_redirects(logged_in):
    """Backward compat: no playlist_id → 302 redirect to /"""
    data = {'files': (io.BytesIO(_jpeg_bytes()), 'img.jpg')}
    r = logged_in.post('/admin/upload', data=data, content_type='multipart/form-data')
    assert r.status_code == 302
    assert '/' in r.headers.get('Location', '/')


def test_upload_with_playlist_id_returns_json_and_creates_entry(logged_in):
    pid = _create_playlist(logged_in)
    data = {
        'files': (io.BytesIO(_jpeg_bytes()), 'newphoto.jpg'),
        'playlist_id': str(pid),
    }
    r = logged_in.post('/admin/upload', data=data, content_type='multipart/form-data')
    assert r.status_code == 200
    j = r.get_json()
    assert j['ok'] is True
    assert len(j['added']) == 1
    assert j['added'][0]['base'] == 'newphoto'
    # Entry exists in DB
    eid = j['added'][0]['entry_id']
    entry = db.get_playlist_entry(eid)
    assert entry is not None
    assert entry['playlist_id'] == pid


def test_upload_with_playlist_id_adds_to_pool_order(logged_in):
    pid = _create_playlist(logged_in)
    data = {
        'files': (io.BytesIO(_jpeg_bytes()), 'poolcheck.jpg'),
        'playlist_id': str(pid),
    }
    logged_in.post('/admin/upload', data=data, content_type='multipart/form-data')
    order = db.load_image_order(owner_id=1)
    assert 'poolcheck' in order


def test_upload_with_playlist_id_disables_in_pool(logged_in):
    pid = _create_playlist(logged_in)
    data = {
        'files': (io.BytesIO(_jpeg_bytes()), 'disabletest.jpg'),
        'playlist_id': str(pid),
    }
    logged_in.post('/admin/upload', data=data, content_type='multipart/form-data')
    enabled = db.load_enabled()
    f = db.flags(enabled, 'disabletest')
    assert f['l'] is False
    assert f['p'] is False


def test_upload_playlist_cap_returns_error(logged_in):
    pid = _create_playlist(logged_in)
    # Fill the playlist with 10 existing images
    for i in range(10):
        make_original(f'existing{i}')
        r = logged_in.post(f'/admin/playlists/{pid}/add_image', json={'base': f'existing{i}'})
        assert r.status_code == 200
    # Now try to upload one more via playlist upload
    data = {
        'files': (io.BytesIO(_jpeg_bytes()), 'overflow.jpg'),
        'playlist_id': str(pid),
    }
    r = logged_in.post('/admin/upload', data=data, content_type='multipart/form-data')
    assert r.status_code == 400
    j = r.get_json()
    assert j['ok'] is False
    assert 'full' in j['error'].lower()


def test_upload_no_valid_files_with_playlist_id_returns_error(logged_in):
    pid = _create_playlist(logged_in)
    data = {
        'files': (io.BytesIO(b'notanimage'), 'file.exe'),
        'playlist_id': str(pid),
    }
    r = logged_in.post('/admin/upload', data=data, content_type='multipart/form-data')
    assert r.status_code == 400
    assert r.get_json()['ok'] is False

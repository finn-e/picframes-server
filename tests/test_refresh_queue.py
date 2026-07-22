# /api/refresh index math, skip advance, queue hand-out + redownload guard.
import db
from routes.api import _queued_idx_for
from tests.conftest import (TEST_MAC, device_headers, make_original,
                            setup_playlist_device)


def _refresh(client, **payload):
    r = client.post('/api/refresh', json=payload, headers=device_headers())
    assert r.status_code == 200
    return r.get_json()


def test_refresh_returns_index_and_settings(logged_in):
    setup_playlist_device(logged_in, bases=('a', 'b', 'c'))
    body = _refresh(logged_in)
    assert body['image_index'] == 0
    assert body['current_orientation'] == 'landscape'
    assert body['sleep_interval'] == 900


def test_refresh_without_skip_does_not_advance(logged_in):
    setup_playlist_device(logged_in, bases=('a', 'b', 'c'))
    assert _refresh(logged_in)['image_index'] == 0
    assert _refresh(logged_in)['image_index'] == 0


def test_skip_advances_and_wraps(logged_in):
    setup_playlist_device(logged_in, bases=('a', 'b', 'c'))
    assert _refresh(logged_in, skip=True)['image_index'] == 1
    assert _refresh(logged_in, skip=True)['image_index'] == 2
    assert _refresh(logged_in, skip=True)['image_index'] == 0  # wrap mod 3


def test_refresh_records_battery(logged_in):
    setup_playlist_device(logged_in, bases=('a',))
    _refresh(logged_in, battery=77)
    hist = db.get_battery_history(TEST_MAC)
    assert hist and hist[-1]['pct'] == 77


def test_queue_entry_then_refresh_without_zip_is_guarded(logged_in):
    """Redownload guard: queued index is NOT handed out until the device has
    re-fetched the zip (else the appended index would be out of range)."""
    pid, eids, _ = setup_playlist_device(logged_in, bases=('a', 'b', 'c'))
    r = logged_in.post('/api/queue', json={
        'entry_id': eids[2], 'source': TEST_MAC, 'playlist_id': pid})
    assert r.status_code == 200 and r.get_json()['action'] == 'queued'

    body = _refresh(logged_in)
    assert body['image_index'] == 0                     # normal index
    assert db.load_state()['queued_image'] is not None  # queue persists


def test_queue_entry_served_after_zip_refetch_and_autocleared(logged_in):
    pid, eids, _ = setup_playlist_device(logged_in, bases=('a', 'b', 'c'))
    logged_in.post('/api/queue', json={
        'entry_id': eids[2], 'source': TEST_MAC, 'playlist_id': pid})
    # Device re-downloads the zip → redownload flag clears
    assert logged_in.get('/api/image-zip', headers=device_headers()).status_code == 200

    body = _refresh(logged_in)
    assert body['image_index'] == 2                 # in-pool entry → pool index
    state = db.load_state()
    assert state['queued_image'] is None            # auto-cleared on hand-out
    # Stored index is q_idx itself; firmware sends skip=True on the next wake
    # which will advance to q_idx+1 naturally (eids[2] is last of 3 → wraps to 0).
    assert state['playlist_indices'][str(pid)] == 2


def test_queued_out_of_pool_entry_gets_appended_index(logged_in):
    pid, eids, _ = setup_playlist_device(logged_in, bases=('a', 'b'))
    # Disable landscape on entry 1 → it drops out of this device's pool
    logged_in.post(f'/admin/entry-toggle-orient/{eids[1]}', json={'orient': 'l', 'enabled': False})
    logged_in.post('/api/queue', json={
        'entry_id': eids[1], 'source': TEST_MAC, 'playlist_id': pid})
    r = logged_in.get('/api/image-zip', headers=device_headers())
    # Zip appends the queued out-of-pool bin
    import io, zipfile
    names = zipfile.ZipFile(io.BytesIO(r.data)).namelist()
    assert 'b_l.bin' in names

    body = _refresh(logged_in)
    assert body['image_index'] == 1  # len(pool)==1 → appended index


def test_queued_served_once_then_next_poll_advances(logged_in):
    """The bug fix: a queued image is handed out exactly once; the following
    skip=True wake-poll (firmware always sends skip=True on timer wake) returns
    queued_idx+1 and the slideshow continues from there."""
    pid, eids, _ = setup_playlist_device(logged_in, bases=('a', 'b', 'c'))
    logged_in.post('/api/queue', json={
        'entry_id': eids[1], 'source': TEST_MAC, 'playlist_id': pid})
    logged_in.get('/api/image-zip', headers=device_headers())

    assert _refresh(logged_in)['image_index'] == 1           # queued, served once
    assert _refresh(logged_in, skip=True)['image_index'] == 2  # next wake: skip advances to q+1
    assert _refresh(logged_in, skip=True)['image_index'] == 0  # continues wrapping


def test_queued_last_entry_next_poll_wraps_to_zero(logged_in):
    pid, eids, _ = setup_playlist_device(logged_in, bases=('a', 'b', 'c'))
    logged_in.post('/api/queue', json={
        'entry_id': eids[2], 'source': TEST_MAC, 'playlist_id': pid})
    logged_in.get('/api/image-zip', headers=device_headers())

    assert _refresh(logged_in)['image_index'] == 2            # queued = last pool entry
    assert _refresh(logged_in, skip=True)['image_index'] == 0  # skip wraps past end


def test_queued_out_of_pool_next_poll_wraps_to_zero(logged_in):
    """Out-of-pool queued entry gets the appended index len(pool); it has no
    in-pool successor, so the next poll restarts the playlist at 0."""
    pid, eids, _ = setup_playlist_device(logged_in, bases=('a', 'b'))
    logged_in.post(f'/admin/entry-toggle-orient/{eids[1]}', json={'orient': 'l', 'enabled': False})
    logged_in.post('/api/queue', json={
        'entry_id': eids[1], 'source': TEST_MAC, 'playlist_id': pid})
    logged_in.get('/api/image-zip', headers=device_headers())

    assert _refresh(logged_in)['image_index'] == 1  # appended (len(pool)==1)
    assert _refresh(logged_in)['image_index'] == 0  # wrap into the real pool


def test_queue_toggle_dequeues(logged_in):
    pid, eids, _ = setup_playlist_device(logged_in, bases=('a', 'b'))
    payload = {'entry_id': eids[0], 'source': TEST_MAC, 'playlist_id': pid}
    assert logged_in.post('/api/queue', json=payload).get_json()['action'] == 'queued'
    assert logged_in.post('/api/queue', json=payload).get_json()['action'] == 'dequeued'
    assert db.load_state()['queued_image'] is None


def test_queue_for_other_device_does_not_apply(logged_in):
    setup_playlist_device(logged_in, bases=('a', 'b', 'c'))
    logged_in.post('/api/queue', json={'entry_id': 1, 'source': '11:22:33:44:55:66'})
    logged_in.get('/api/image-zip', headers=device_headers())
    assert _refresh(logged_in)['image_index'] == 0
    assert db.load_state()['queued_image'] is not None


# --- _queued_idx_for unit tests -------------------------------------------------

def test_queued_idx_entry_pool():
    pool = [{'id': 5}, {'id': 7}, {'id': 9}]
    assert _queued_idx_for({'entry_id': 7, 'source': 'general'}, 'm', pool) == 1
    assert _queued_idx_for({'entry_id': 99, 'source': 'general'}, 'm', pool) == 3
    assert _queued_idx_for(None, 'm', pool) is None
    assert _queued_idx_for({'entry_id': 7, 'source': 'other'}, 'm', pool) is None


def test_queued_idx_base_pool():
    pool = ['a', 'b', 'c']
    assert _queued_idx_for({'base': 'b', 'source': 'general'}, 'm', pool) == 1
    assert _queued_idx_for({'base': 'zz', 'source': 'general'}, 'm', pool) == 3
    assert _queued_idx_for({'base': 'b', 'source': 'M'}, 'm', pool) == 1  # mac match, case-insensitive


def test_device_images_tracking(logged_in):
    pid, eids, _ = setup_playlist_device(logged_in, bases=('a', 'b'))
    
    # Check that device_images updates on refresh
    res = _refresh(logged_in)
    assert res['image_index'] == 0
    state = db.load_state()
    assert state['device_images'].get(TEST_MAC) == f"pe{eids[0]}_800x480_l.bmp"

    # Advance
    res2 = _refresh(logged_in, skip=True)
    assert res2['image_index'] == 1
    state2 = db.load_state()
    assert state2['device_images'].get(TEST_MAC) == f"pe{eids[1]}_800x480_l.bmp"


def test_playlist_sync_toggled_vs_non_sync(logged_in):
    # Setup playlist with 3 images
    pid, eids, _ = setup_playlist_device(logged_in, bases=('a', 'b', 'c'))
    
    # Register a second device on the same playlist
    mac2 = '00:11:22:33:44:55'
    from tests.conftest import register_device
    register_device(logged_in, mac2)
    r_assign = logged_in.post('/admin/device_playlist', json={'mac': mac2, 'playlist_id': pid})
    print("ASSIGN STATUS:", r_assign.status_code, r_assign.get_json())
    assert r_assign.status_code == 200

    # Case 1: Sync is disabled on this playlist.
    # When multiple devices phone home, they should advance sequentially (taking turns).
    r_settings = logged_in.post(f'/admin/playlists/{pid}/settings', json={
        'name': 'TestPL', 'sleep_interval': 900, 'shuffle': False, 'sync': False
    })
    print("SETTINGS STATUS:", r_settings.status_code, r_settings.get_json())
    assert r_settings.status_code == 200

    # First device: gets index 0 -> advances to 1
    res1 = _refresh(logged_in, skip=True)
    print("REFRESH 1 STATUS:", res1)
    assert res1['image_index'] == 1

    # Second device: gets index 1 -> advances to 2
    r = logged_in.post('/api/refresh', json={'skip': True}, headers=device_headers(mac2))
    print("REFRESH 2 STATUS:", r.status_code, r.get_json())
    assert r.status_code == 200
    assert r.get_json()['image_index'] == 2

    # First device again: gets index 2 -> advances to 0 (wrap)
    res2 = _refresh(logged_in, skip=True)
    print("REFRESH 3 STATUS:", res2)
    assert res2['image_index'] == 0

    # Case 2: Sync is enabled on this playlist.
    # They should display the SAME image (cooldown stops the second one from advancing).
    logged_in.post(f'/admin/playlists/{pid}/settings', json={
        'name': 'TestPL', 'sleep_interval': 900, 'shuffle': False, 'sync': True
    })

    # Device 1 checks in first, advances index (now 0 -> 1)
    res_sync1 = _refresh(logged_in, skip=True)
    assert res_sync1['image_index'] == 1

    # Device 2 checks in immediately after, stays on index 1
    r_sync2 = logged_in.post('/api/refresh', json={'skip': True}, headers=device_headers(mac2))
    assert r_sync2.status_code == 200
    assert r_sync2.get_json()['image_index'] == 1


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
    assert logged_in.get('/api/daily-zip', headers=device_headers()).status_code == 200

    body = _refresh(logged_in)
    assert body['image_index'] == 2                 # in-pool entry → pool index
    state = db.load_state()
    assert state['queued_image'] is None            # auto-cleared on hand-out
    # Stored index is the FOLLOWING one (wrapped): eids[2] is the last of 3,
    # so the next wake-poll wraps to 0 instead of re-serving the queued image.
    assert state['playlist_indices'][str(pid)] == 0


def test_queued_out_of_pool_entry_gets_appended_index(logged_in):
    pid, eids, _ = setup_playlist_device(logged_in, bases=('a', 'b'))
    # Disable landscape on entry 1 → it drops out of this device's pool
    logged_in.post(f'/entry-toggle-orient/{eids[1]}', json={'orient': 'l', 'enabled': False})
    logged_in.post('/api/queue', json={
        'entry_id': eids[1], 'source': TEST_MAC, 'playlist_id': pid})
    r = logged_in.get('/api/daily-zip', headers=device_headers())
    # Zip appends the queued out-of-pool bin
    import io, zipfile
    names = zipfile.ZipFile(io.BytesIO(r.data)).namelist()
    assert 'b_l.bin' in names

    body = _refresh(logged_in)
    assert body['image_index'] == 1  # len(pool)==1 → appended index


def test_queued_served_once_then_next_poll_advances(logged_in):
    """The bug fix: a queued image is handed out exactly once; the following
    non-skip wake-poll returns queued_idx+1 and the slideshow continues."""
    pid, eids, _ = setup_playlist_device(logged_in, bases=('a', 'b', 'c'))
    logged_in.post('/api/queue', json={
        'entry_id': eids[1], 'source': TEST_MAC, 'playlist_id': pid})
    logged_in.get('/api/daily-zip', headers=device_headers())

    assert _refresh(logged_in)['image_index'] == 1  # queued entry, served once
    assert _refresh(logged_in)['image_index'] == 2  # next poll: queued_idx + 1
    assert _refresh(logged_in)['image_index'] == 2  # then holds (non-skip poll)
    assert _refresh(logged_in, skip=True)['image_index'] == 0  # skip wraps on


def test_queued_last_entry_next_poll_wraps_to_zero(logged_in):
    pid, eids, _ = setup_playlist_device(logged_in, bases=('a', 'b', 'c'))
    logged_in.post('/api/queue', json={
        'entry_id': eids[2], 'source': TEST_MAC, 'playlist_id': pid})
    logged_in.get('/api/daily-zip', headers=device_headers())

    assert _refresh(logged_in)['image_index'] == 2  # queued = last pool entry
    assert _refresh(logged_in)['image_index'] == 0  # wraps past the end


def test_queued_out_of_pool_next_poll_wraps_to_zero(logged_in):
    """Out-of-pool queued entry gets the appended index len(pool); it has no
    in-pool successor, so the next poll restarts the playlist at 0."""
    pid, eids, _ = setup_playlist_device(logged_in, bases=('a', 'b'))
    logged_in.post(f'/entry-toggle-orient/{eids[1]}', json={'orient': 'l', 'enabled': False})
    logged_in.post('/api/queue', json={
        'entry_id': eids[1], 'source': TEST_MAC, 'playlist_id': pid})
    logged_in.get('/api/daily-zip', headers=device_headers())

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
    logged_in.get('/api/daily-zip', headers=device_headers())
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

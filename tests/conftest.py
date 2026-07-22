# ==========================================================================================
# DESCRIPTION: pytest fixtures for the picframes server test suite.
#
# IMPORTANT CONSTRAINT: db.py reads SHARE_DIR / CONFIG_DIR from the environment AT
# IMPORT TIME (module-level constants, directory creation, DB_PATH). So we set the
# env vars here at conftest import time — before any test module imports app/db —
# pointing at a session-scoped temp directory. Per-test isolation is achieved by
# the autouse `fresh_db` fixture, which deletes the DB file and wipes the image
# dirs, then re-runs init_db(). Do NOT import app/db/image/routes at the top of a
# test module before conftest has been imported by pytest (pytest always imports
# conftest first, so normal `import db` in test modules is safe).
# ==========================================================================================
import os
import shutil
import sys
import tempfile

import pytest
from PIL import Image

# --- Environment must be set before importing any server module ---------------
_TMP_ROOT = tempfile.mkdtemp(prefix='picframes-tests-')
os.environ['SHARE_DIR']      = os.path.join(_TMP_ROOT, 'share')
os.environ['CONFIG_DIR']     = os.path.join(_TMP_ROOT, 'config')
os.environ['SECRET_KEY']     = 'test-secret-key'
os.environ['ADMIN_PASSWORD'] = 'admin'

# Make the server package importable when pytest is run from anywhere
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db                    # noqa: E402  (env-dependent import, see header)
from app import app as flask_app  # noqa: E402  (runs init_db once at import)


def pytest_sessionfinish(session, exitstatus):
    shutil.rmtree(_TMP_ROOT, ignore_errors=True)


# ------------------------------------------------------------------------------
# Core fixtures
# ------------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def fresh_db():
    """Full isolation per test: wipe DB + share dirs and re-init schema."""
    if os.path.exists(db.DB_PATH):
        os.remove(db.DB_PATH)
    for d in (db.ORIGINALS_DIR, db.IMAGES_DIR):
        shutil.rmtree(d, ignore_errors=True)
        os.makedirs(d, exist_ok=True)
    db.init_db()
    yield


@pytest.fixture
def app():
    flask_app.config['TESTING'] = True
    return flask_app


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def logged_in(client):
    """Client with an authenticated admin dashboard session."""
    r = client.post('/ui/login', data={'username': 'admin', 'password': 'admin'})
    assert r.status_code == 302 and '/ui/login' not in r.headers['Location']
    return client


# ------------------------------------------------------------------------------
# Speed helpers
# ------------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def fast_dither(monkeypatch):
    """The pure-Python Floyd-Steinberg dither takes ~14 s for an 800x480 frame,
    and every conversion resizes to 800x480 regardless of source size. Replace it
    with a cheap nearest-palette quantisation everywhere it is referenced so
    integration tests stay fast. The REAL dither is exercised in
    test_convert.py::test_real_dither_tiny on a tiny array."""
    import numpy as np
    import image as image_mod
    import image.pipeline as pipeline_mod
    import image.artifacts as artifacts_mod
    import routes.admin as admin_mod

    def _fake_dither(arr, palette):
        px = arr.reshape(-1, 3).astype('float32')
        idx = np.argmin(((px[:, None, :] - palette[None, :, :]) ** 2).sum(axis=2), axis=1)
        return palette[idx].reshape(arr.shape).astype('uint8')

    monkeypatch.setattr(image_mod, 'dither_floyd_steinberg', _fake_dither)
    monkeypatch.setattr(pipeline_mod, 'dither_floyd_steinberg', _fake_dither)
    monkeypatch.setattr(admin_mod, 'dither_floyd_steinberg', _fake_dither)
    yield


@pytest.fixture(autouse=True)
def sync_threads(monkeypatch):
    """Run routes.admin background conversion threads synchronously so tests can
    assert on artifacts immediately after the request returns."""
    import routes.admin as admin_mod

    class _SyncThread:
        def __init__(self, target=None, args=(), kwargs=None, daemon=None):
            self._target, self._args, self._kwargs = target, args, kwargs or {}
        def start(self):
            if self._target:
                self._target(*self._args, **self._kwargs)
        def join(self, timeout=None):
            pass

    monkeypatch.setattr(admin_mod.threading, 'Thread', _SyncThread)
    yield


@pytest.fixture(autouse=True)
def no_github(monkeypatch):
    """Keep /api/update offline (no GitHub Releases API calls in CI)."""
    import routes.api as api_mod
    monkeypatch.setattr(api_mod, '_update_github_cache', lambda: None)
    yield


# ------------------------------------------------------------------------------
# Domain helpers
# ------------------------------------------------------------------------------

TEST_MAC = 'aa:bb:cc:dd:ee:ff'


def make_original(name='photo', size=(100, 70), color=(200, 60, 60)):
    """Create a tiny original image on disk and add it to the general pool."""
    path = os.path.join(db.ORIGINALS_DIR, name + '.jpg')
    Image.new('RGB', size, color).save(path, format='JPEG')
    order = db.load_image_order(owner_id=1)
    if name not in order:
        order.append(name)
        db.save_image_order(order, owner_id=1)
    return path


def device_token(mac=TEST_MAC):
    import hashlib
    import hmac as hmac_mod
    return hmac_mod.new(os.environ['SECRET_KEY'].encode(), mac.encode(),
                        hashlib.sha256).hexdigest()[:32]


def register_device(client, mac=TEST_MAC):
    """Register a device using real user credentials; returns its token."""
    r = client.post('/api/register', json={
        'mac': mac, 'username': 'admin', 'password': 'admin'})
    assert r.status_code == 200, r.get_data(as_text=True)
    return r.get_json()['token']


def device_headers(mac=TEST_MAC, token=None):
    return {'X-Device-Mac': mac, 'X-Device-Token': token or device_token(mac)}


def setup_playlist_device(client, mac=TEST_MAC, bases=('photo',), playlist='PL'):
    """Login-authenticated *client*: create originals, a playlist with entries,
    register a device and assign it to the playlist.
    Returns (pid, [entry_ids], token)."""
    r = client.post('/admin/playlists/create', json={'name': playlist})
    pid = r.get_json()['id']
    entry_ids = []
    for base in bases:
        make_original(base)
        r = client.post(f'/admin/playlists/{pid}/add_image', json={'base': base})
        assert r.status_code == 200, r.get_data(as_text=True)
        entry_ids.append(r.get_json()['entry_id'])
    token = register_device(client, mac)
    r = client.post('/admin/device_playlist', json={'mac': mac, 'playlist_id': pid})
    assert r.status_code == 200
    return pid, entry_ids, token


@pytest.fixture
def helpers():
    """Expose module helpers to tests without import gymnastics."""
    import types
    return types.SimpleNamespace(
        make_original=make_original,
        device_token=device_token,
        register_device=register_device,
        device_headers=device_headers,
        TEST_MAC=TEST_MAC,
    )

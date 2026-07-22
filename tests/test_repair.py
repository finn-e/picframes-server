# Tests for the re-pair window feature.
import time
import db
from tests.conftest import TEST_MAC, device_token, register_device


def test_repair_window_allows_token_reissue(client, logged_in):
    """Known device with bad token gets a new token when repair window is open."""
    register_device(client)
    # Open repair window via dashboard endpoint.
    r = logged_in.post('/admin/device_repair', json={'mac': TEST_MAC})
    assert r.status_code == 200
    d = r.get_json()
    assert d['ok'] is True
    assert d['repair_until'] > int(time.time())

    # Now attempt registration with a bogus password (not the HMAC token, not real creds).
    r2 = client.post('/api/register', json={'mac': TEST_MAC, 'password': 'bogus-bad-token'})
    assert r2.status_code == 200
    assert r2.get_json()['token'] == device_token(TEST_MAC)


def test_repair_window_is_one_shot(client, logged_in):
    """Second attempt with bogus password after window already used gets 403."""
    register_device(client)
    logged_in.post('/admin/device_repair', json={'mac': TEST_MAC})

    # First attempt consumes the window.
    r1 = client.post('/api/register', json={'mac': TEST_MAC, 'password': 'bogus'})
    assert r1.status_code == 200

    # Second attempt must be rejected.
    r2 = client.post('/api/register', json={'mac': TEST_MAC, 'password': 'bogus'})
    assert r2.status_code == 403


def test_unknown_mac_rejected_even_with_repair_logic(client):
    """An unregistered MAC cannot get a token even if someone tries the empty-password trick."""
    unknown_mac = 'de:ad:be:ef:00:01'
    r = client.post('/api/register', json={'mac': unknown_mac, 'password': ''})
    assert r.status_code == 403


def test_device_repair_endpoint_sets_column(client, logged_in):
    """POST /device_repair persists repair_until into the devices table."""
    register_device(client)
    before = int(time.time())
    logged_in.post('/admin/device_repair', json={'mac': TEST_MAC})
    conn = db.get_db()
    row = conn.execute("SELECT repair_until FROM devices WHERE mac=?",
                       (TEST_MAC,)).fetchone()
    conn.close()
    assert row is not None
    assert row['repair_until'] is not None
    assert row['repair_until'] >= before + 599


def test_token_reregister_keeps_owner_despite_username(client):
    """A token-authenticated re-register with a stale stored username must not
    move or duplicate the device under that username's account."""
    register_device(client)  # owner: admin (id 1)
    # Create a second user and move the device to them.
    conn = db.get_db()
    conn.execute("INSERT INTO users (username, password_hash, is_admin) VALUES ('fin', 'x', 0)")
    fin_id = conn.execute("SELECT id FROM users WHERE username='fin'").fetchone()['id']
    conn.execute("UPDATE devices SET owner_id=? WHERE mac=?", (fin_id, TEST_MAC))
    conn.commit(); conn.close()

    # Device re-registers with its VALID token but stale username 'admin'.
    r = client.post('/api/register', json={
        'mac': TEST_MAC, 'username': 'admin', 'password': device_token(TEST_MAC)})
    assert r.status_code == 200

    conn = db.get_db()
    rows = conn.execute("SELECT owner_id FROM devices WHERE mac=?", (TEST_MAC,)).fetchall()
    conn.close()
    assert len(rows) == 1
    assert rows[0]['owner_id'] == fin_id  # ownership unchanged

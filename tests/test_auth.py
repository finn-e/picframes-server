# Device HTTP auth contract — regressions here brick the deployed frame fleet.
from tests.conftest import (TEST_MAC, device_headers, device_token,
                            make_original, register_device)


def test_refresh_without_mac_is_400(client):
    r = client.post('/api/refresh', json={})
    assert r.status_code == 400


def test_refresh_without_token_is_403(client):
    r = client.post('/api/refresh', json={}, headers={'X-Device-Mac': TEST_MAC})
    assert r.status_code == 403


def test_refresh_with_wrong_token_is_403(client):
    r = client.post('/api/refresh', json={},
                    headers={'X-Device-Mac': TEST_MAC,
                             'X-Device-Token': 'deadbeef' * 4})
    assert r.status_code == 403


def test_valid_token_but_unregistered_device_is_403(client):
    # Correct HMAC but no device row: deliberately NO auto-registration.
    r = client.post('/api/refresh', json={}, headers=device_headers())
    assert r.status_code == 403


def test_register_with_bad_credentials_is_403(client):
    r = client.post('/api/register', json={
        'mac': TEST_MAC, 'username': 'admin', 'password': 'wrong'})
    assert r.status_code == 403


def test_register_without_mac_is_400(client):
    r = client.post('/api/register', json={'username': 'admin', 'password': 'admin'})
    assert r.status_code == 400


def test_register_issues_hmac_token_and_creates_device(client):
    token = register_device(client)
    assert token == device_token(TEST_MAC)  # HMAC-SHA256(SECRET_KEY, mac)[:32]
    import db
    assert db.get_device_owner_id(TEST_MAC) == 1  # admin user id


def test_registered_device_with_valid_token_gets_200(client):
    register_device(client)
    r = client.get('/api/daily-config', headers=device_headers())
    assert r.status_code == 200
    body = r.get_json()
    assert 'daily_zip_version' in body and 'images' in body


def test_reregister_with_token_as_password_keeps_owner(client):
    register_device(client)
    r = client.post('/api/register', json={'mac': TEST_MAC,
                                           'password': device_token(TEST_MAC)})
    assert r.status_code == 200
    assert r.get_json()['token'] == device_token(TEST_MAC)


def test_daily_zip_requires_token(client):
    register_device(client)
    r = client.get('/api/daily-zip', headers={'X-Device-Mac': TEST_MAC})
    assert r.status_code == 403


def test_update_endpoint_is_deliberately_tokenless(client):
    # OTA update check must work for unauthenticated devices.
    r = client.get('/api/update?hw=XIAO-EE04-7in3&version=0.1.0')
    assert r.status_code in (200, 204)


def test_update_without_hw_is_400(client):
    r = client.get('/api/update')
    assert r.status_code == 400

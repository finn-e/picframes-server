# Tests for /api/update — last_seen tracking and fw_version recording.
import time
import db
from tests.conftest import TEST_MAC, device_headers, register_device


def test_api_update_updates_last_seen_for_registered_device(client):
    register_device(client)
    before = int(time.time()) - 1
    r = client.get('/api/update?hw=XIAO-EE04-7in3', headers=device_headers())
    assert r.status_code in (200, 204)
    import routes.api as api_mod
    state = api_mod.load_state()
    assert TEST_MAC in state.get('last_seen', {}), "last_seen not recorded"
    assert state['last_seen'][TEST_MAC] >= before


def test_api_update_records_fw_version_header(client):
    register_device(client)
    r = client.get(
        '/api/update?hw=XIAO-EE04-7in3',
        headers={**device_headers(), 'X-Firmware-Version': '0.8.0'},
    )
    assert r.status_code in (200, 204)
    cfg = db.load_config(owner_id=1)
    dev = next(d for d in cfg['devices'] if d['mac'].lower() == TEST_MAC)
    assert dev.get('fw_version') == '0.8.0'


def test_api_update_no_last_seen_for_unknown_device(client):
    # No registered device — last_seen should NOT be touched.
    r = client.get('/api/update?hw=XIAO-EE04-7in3',
                   headers={'X-Device-Mac': TEST_MAC,
                            'X-Device-Token': 'deadbeef' * 4})
    # endpoint doesn't auth-gate, returns 204/200 based on update URL
    import routes.api as api_mod
    state = api_mod.load_state()
    assert TEST_MAC not in state.get('last_seen', {})

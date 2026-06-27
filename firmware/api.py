# ==========================================
# FILE VERSION: 2.0.0
# DESCRIPTION: API client for the PicFrames server. Handles all endpoint
#              communication with auth headers, NTP sync, and fail-soft.
# ==========================================
import time
import json
import urequests as requests
import network
import ntptime

NTP_SERVERS = ['pool.ntp.org', 'time.nist.gov', 'time.google.com']

def sync_ntp():
    """Sync hardware clock via SNTP. Tries primary then fallbacks."""
    for srv in NTP_SERVERS:
        try:
            ntptime.host = srv
            ntptime.settime()
            print('NTP synced via', srv)
            return True
        except Exception as e:
            print('NTP sync failed for', srv, ':', e)
    print('All NTP servers failed.')
    return False

def make_headers(mac, token):
    return {
        'Content-Type': 'application/json',
        'X-Device-Mac': mac,
        'X-Device-Token': token,
    }

def call_update(server_url, mac, token, hw_profile, update_version):
    """
    GET /update?hw=ESP32-S3-PhotoPainter&version=<update_version>
    Returns: None if current, or GitHub ZIP URL string if update available.
    Raises exception on network failure.
    """
    url = server_url + '/update'
    params = '?hw=' + hw_profile + '&version=' + (update_version or '')
    res = requests.get(url + params, headers=make_headers(mac, token), timeout=10)
    if res.status_code == 200 and res.text:
        body = res.text.strip()
        res.close()
        return body if body else None
    res.close()
    return None

def call_daily_config(server_url, mac, token):
    """
    GET /daily-config
    Returns parsed JSON dict.
    Raises exception on failure.
    """
    url = server_url + '/daily-config'
    res = requests.get(url, headers=make_headers(mac, token), timeout=10)
    data = json.loads(res.text)
    res.close()
    return data

def call_daily_zip(server_url, mac, token, daily_zip_version, dest_path):
    """
    GET /daily-zip?version=<daily_zip_version>
    Streams ZIP directly to dest_path.
    Returns True if new ZIP downloaded, False if already current (304/same version).
    Raises exception on failure.
    """
    url = server_url + '/daily-zip?version=' + (daily_zip_version or '0')
    res = requests.get(url, headers=make_headers(mac, token), timeout=30)
    if res.status_code == 304 or res.status_code == 204:
        res.close()
        return False
    chunk = bytearray(4096)
    with open(dest_path, 'wb') as f:
        while True:
            n = res.raw.readinto(chunk)
            if not n:
                break
            f.write(chunk if n == len(chunk) else chunk[:n])
    res.close()
    return True

def call_refresh(server_url, mac, token, skip=False):
    """
    POST /refresh
    skip=True means KEY button was pressed (manual skip).
    Returns dict with image_index, current_orientation, sleep_interval.
    Raises exception on failure.
    """
    url = server_url + '/refresh'
    body = json.dumps({'mac': mac, 'skip': skip})
    res = requests.post(url, data=body, headers=make_headers(mac, token), timeout=10)
    data = json.loads(res.text)
    res.close()
    return data

def call_change_orientation(server_url, mac, token, orientation):
    """
    POST /change-orientation
    Notifies server of orientation change.
    """
    url = server_url + '/change-orientation'
    body = json.dumps({'mac': mac, 'orientation': orientation})
    try:
        res = requests.post(url, data=body, headers=make_headers(mac, token), timeout=5)
        res.close()
    except Exception as e:
        print('change-orientation call failed:', e)

def call_register(server_url, mac, username, password):
    """
    POST /register
    Registers device using username+password.
    Returns token string on success, or raises.
    """
    url = server_url + '/register'
    body = json.dumps({'mac': mac, 'username': username, 'password': password})
    res = requests.post(url, data=body, headers={'Content-Type': 'application/json'}, timeout=10)
    data = json.loads(res.text)
    res.close()
    if 'token' not in data:
        raise Exception('Registration failed: ' + str(data))
    return data['token']

# ==========================================
# FILE VERSION: 2.0.0
# DESCRIPTION: Main slideshow loop for ESP32-S3-PhotoPainter.
#              Sequential API flow: NTP->update->daily-config->daily-zip->refresh->render->sleep
#              AP captive portal for initial setup (Flash-only, no SD needed).
#              BOOT button: orientation cycle. KEY button: image skip.
# ==========================================
import time
import machine
import os
import json
import network
import ubinascii

print('--- PicFrame v2.0 starting ---')

# --- Buttons ---
boot_btn = machine.Pin(0, machine.Pin.IN, machine.Pin.PULL_UP)
key_btn  = machine.Pin(4, machine.Pin.IN, machine.Pin.PULL_UP)
pwr_btn  = machine.Pin(5, machine.Pin.IN, machine.Pin.PULL_DOWN)

time.sleep_ms(100)

boot_pressed_on_boot = False
if boot_btn.value() == 0:
    print('BOOT held at startup - orientation toggle requested.')
    boot_pressed_on_boot = True
    while boot_btn.value() == 0:
        time.sleep_ms(10)

key_pressed_on_boot = False
if key_btn.value() == 0:
    print('KEY held at startup - image skip requested.')
    key_pressed_on_boot = True
    while key_btn.value() == 0:
        time.sleep_ms(10)

# --- MAC ---
wlan = network.WLAN(network.STA_IF)
mac_bytes = wlan.config('mac')
mac_str = ubinascii.hexlify(mac_bytes, ':').decode()
print('Device MAC:', mac_str)

# --- Config ---
import sys
sys.path.insert(0, '/sd')
sys.path.insert(1, '/')

try:
    from config import (
        load_flash_config, save_flash_config,
        load_sd_config, save_sd_config, deep_merge_sd_config
    )
except Exception as e:
    print('Config module load failed:', e)
    # Minimal fallback
    def load_flash_config(): return {'wifi_ssid':'','wifi_pass':'','server_url':'https://picframe.treee.house','username':'','token':'','update_version':'','landscape_flipped':False,'portrait_flipped':False}
    def save_flash_config(c): pass
    def load_sd_config(): return {'orientation':'landscape','sleep_interval':900,'image_index':0,'daily_zip_version':'','images':[]}
    def save_sd_config(c): pass
    def deep_merge_sd_config(a, b): merged=dict(a); merged.update(b); return merged

flash_cfg = load_flash_config()
sd_cfg    = load_sd_config()

HW_PROFILE = 'ESP32-S3-PhotoPainter'

# --- SD helpers ---
def sd_mounted():
    try:
        os.stat('/sd')
        return True
    except OSError:
        return False

def wipe_sd_images():
    print('Wiping SD images...')
    try:
        for f in os.listdir('/sd'):
            if f.endswith('.py') or f in ('config.json',):
                continue
            try:
                os.remove('/sd/' + f)
            except Exception:
                pass
    except Exception as e:
        print('SD wipe error:', e)

# --- Power/Sleep ---
def get_bat_pct():
    try:
        from axp import AXP2101
        return AXP2101().get_battery_percentage()
    except Exception:
        return None

def go_to_sleep(seconds):
    try:
        from axp import AXP2101
        axp = AXP2101()
        if axp.is_usb_connected():
            print('USB connected - simulating sleep for', seconds, 's')
            _wait_with_buttons(seconds)
            return
        axp.disable_power()
    except Exception as e:
        print('PMIC sleep prep error:', e)
    print('Deep sleeping for', seconds, 's')
    machine.deepsleep(seconds * 1000)

def _wait_with_buttons(seconds):
    end = time.time() + seconds
    while time.time() < end:
        _check_buttons()
        time.sleep_ms(50)

def _check_buttons():
    if boot_btn.value() == 0:
        time.sleep_ms(50)
        if boot_btn.value() == 0:
            while boot_btn.value() == 0:
                time.sleep_ms(10)
            action_toggle_orientation()
    if key_btn.value() == 0:
        time.sleep_ms(50)
        if key_btn.value() == 0:
            while key_btn.value() == 0:
                time.sleep_ms(10)
            action_key_skip()

# --- WiFi ---
def connect_wifi(ssid, password, timeout=15):
    wlan.active(True)
    if wlan.isconnected():
        return True
    print('Connecting to WiFi:', ssid)
    wlan.connect(ssid, password)
    start = time.time()
    while not wlan.isconnected() and time.time() - start < timeout:
        time.sleep_ms(200)
    connected = wlan.isconnected()
    print('WiFi', 'connected' if connected else 'FAILED')
    return connected

def disconnect_wifi():
    try:
        wlan.active(False)
    except Exception:
        pass

# --- Display ---
def render_and_sleep(img_path, orientation, sleep_interval):
    disconnect_wifi()
    bat_pct = get_bat_pct()
    try:
        from display_overlay import apply_battery_square, apply_branding_text
        buf = bytearray(192000)
        with open(img_path, 'rb') as f:
            f.readinto(buf)
        apply_battery_square(buf, bat_pct)
        apply_branding_text(buf)
        tmp_path = '/tmp_render.bin'
        with open(tmp_path, 'wb') as f:
            f.write(buf)
        del buf
        from epd import EPD_7in3f
        epd = EPD_7in3f()
        epd.display_file(tmp_path, orientation=orientation)
        try:
            os.remove(tmp_path)
        except Exception:
            pass
        print('Display updated successfully.')
    except Exception as e:
        print('Display render failed:', e)
    go_to_sleep(sleep_interval)

def resolve_image_path(image_basename, orientation):
    suffix = '_l.bin' if 'landscape' in orientation else '_p.bin'
    return '/sd/' + image_basename + suffix

# --- AP Captive Portal ---
def re_url_decode(s):
    res, i = [], 0
    while i < len(s):
        if s[i] == '%' and i + 2 < len(s):
            try:
                res.append(chr(int(s[i+1:i+3], 16)))
                i += 3
            except ValueError:
                res.append(s[i]); i += 1
        elif s[i] == '+':
            res.append(' '); i += 1
        else:
            res.append(s[i]); i += 1
    return ''.join(res)

ap_active = False

def start_ap_and_portal():
    global ap_active, flash_cfg
    dev_id = mac_str.replace(':', '')[-8:].upper()
    ap_ssid = 'PicFrame-' + mac_str.replace(':', '')

    ap = network.WLAN(network.AP_IF)
    ap.active(True)
    ap.config(essid=ap_ssid, authmode=network.AUTH_OPEN)
    print('AP started:', ap_ssid)

    ap_active = True
    try:
        import _thread
        _thread.start_new_thread(dns_thread, ())
    except Exception as e:
        print('DNS thread failed:', e)

    import socket
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(('', 80))
    s.listen(1)
    s.settimeout(0.2)

    AP_SETUP_HTML = """<!DOCTYPE html><html>
<head><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"><title>PicFrame Setup</title>
<style>*{{box-sizing:border-box;margin:0;padding:0}}body{{font-family:system-ui,sans-serif;background:#0f172a;color:#e2e8f0;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:20px}}.card{{background:rgba(30,41,59,.8);border:1px solid rgba(255,255,255,.08);padding:32px;border-radius:20px;width:100%;max-width:440px;box-shadow:0 20px 40px rgba(0,0,0,.5)}}h2{{font-weight:700;font-size:1.7rem;margin-bottom:6px;background:linear-gradient(135deg,hsl(190,100%,55%),hsl(260,90%,65%));-webkit-background-clip:text;-webkit-text-fill-color:transparent;text-align:center}}.sub{{text-align:center;color:#64748b;font-size:.85rem;margin-bottom:20px}}.notice{{background:rgba(99,102,241,.12);border:1px solid rgba(99,102,241,.3);border-radius:10px;padding:12px;font-size:.82rem;margin-bottom:20px;color:#a5b4fc;line-height:1.5}}label{{display:block;font-size:.82rem;color:#94a3b8;margin-bottom:4px;font-weight:500}}.ig{{margin-bottom:14px}}input[type=text],input[type=password]{{width:100%;padding:10px 12px;background:rgba(15,23,42,.6);border:1px solid rgba(255,255,255,.1);border-radius:8px;color:#fff;font-size:.92rem}}input:focus{{outline:none;border-color:hsl(190,100%,55%);box-shadow:0 0 0 2px rgba(56,189,248,.15)}}input[type=submit]{{width:100%;padding:12px;border:none;border-radius:9px;background:linear-gradient(135deg,hsl(190,100%,45%),hsl(260,90%,55%));color:#fff;font-size:.97rem;font-weight:600;cursor:pointer;margin-top:4px}}.err{{color:hsl(0,85%,65%);background:rgba(239,68,68,.12);border:1px solid rgba(239,68,68,.2);padding:10px;border-radius:8px;margin-bottom:14px;font-size:.82rem;text-align:center}}</style></head>
<body><div class=\"card\">
<h2>PicFrame Setup</h2>
<div class=\"sub\">Device: {dev_id} | MAC: {mac}</div>
{err}
<div class=\"notice\">&#x24D8;&nbsp; If you don't know your credentials, contact <strong>Fin O'Flaherty</strong> to get access.</div>
<form method=\"POST\" action=\"/save\">
<div class=\"ig\"><label>Wi-Fi Network (SSID)</label><input type=\"text\" name=\"ssid\" value=\"{ssid}\" required></div>
<div class=\"ig\"><label>Wi-Fi Password</label><input type=\"password\" name=\"pass\" value=\"{pw}\"></div>
<div class=\"ig\"><label>PicFrames Server URL</label><input type=\"text\" name=\"server_url\" value=\"{srv}\" placeholder=\"https://picframe.treee.house\"></div>
<div class=\"ig\"><label>Username</label><input type=\"text\" name=\"username\" value=\"{uname}\"></div>
<div class=\"ig\"><label>Password / Token</label><input type=\"password\" name=\"token\"></div>
<input type=\"submit\" value=\"Save & Connect\">
</form></div></body></html>"""

    while True:
        try:
            conn, addr = s.accept()
            req = conn.recv(2048).decode('utf-8', 'ignore')
            lines = req.split('\r\n')
            if not lines or not lines[0]:
                conn.close()
                continue
            first = lines[0].split(' ')
            method = first[0] if first else ''
            path = first[1] if len(first) > 1 else '/'

            host = ""
            for line in lines:
                if line.lower().startswith("host:"):
                    host = line.split(":", 1)[1].strip()
                    break

            is_portal_host = (host == "192.168.4.1")

            if not is_portal_host:
                # Send a 302 redirect with no-cache headers to trigger Captive Portal Assistant pop-ups
                redirect_resp = (
                    "HTTP/1.1 302 Found\r\n"
                    "Location: http://192.168.4.1/\r\n"
                    "Cache-Control: no-cache, no-store, must-revalidate\r\n"
                    "Pragma: no-cache\r\n"
                    "Expires: 0\r\n"
                    "Content-Length: 0\r\n"
                    "Connection: close\r\n\r\n"
                )
                conn.send(redirect_resp)
                conn.close()
                continue

            if method == 'POST' and '/save' in path:
                body = req.split('\r\n\r\n', 1)[-1]
                p = {}
                for kv in body.split('&'):
                    if '=' in kv:
                        k, v = kv.split('=', 1)
                        p[k] = re_url_decode(v)
                ssid = p.get('ssid', '').strip()
                pw   = p.get('pass', '').strip()
                srv  = p.get('server_url', '').strip() or 'https://picframe.treee.house'
                uname = p.get('username', '').strip()
                tok   = p.get('token', '').strip()
                if ssid:
                    flash_cfg['wifi_ssid'] = ssid
                    flash_cfg['wifi_pass'] = pw
                    flash_cfg['server_url'] = srv
                    flash_cfg['username'] = uname
                    flash_cfg['token'] = tok
                    save_flash_config(flash_cfg)
                    conn.send('HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n\r\n<h2>Saved! Rebooting in 3 seconds...</h2>')
                    conn.close()
                    s.close()
                    ap_active = False
                    ap.active(False)
                    time.sleep_ms(500)
                    machine.reset()
            else:
                html = AP_SETUP_HTML.format(
                    dev_id=dev_id, mac=mac_str.upper(),
                    ssid=flash_cfg.get('wifi_ssid',''), pw=flash_cfg.get('wifi_pass',''),
                    srv=flash_cfg.get('server_url','https://picframe.treee.house'),
                    uname=flash_cfg.get('username',''), err=''
                )
                conn.send('HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n\r\n')
                conn.send(html)
                conn.close()
        except OSError:
            pass

def dns_thread():
    global ap_active
    import socket
    udps = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udps.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    udps.settimeout(1.0)
    for _ in range(5):
        try:
            udps.bind(('', 53))
            break
        except Exception:
            time.sleep_ms(200)
    while ap_active:
        try:
            data, addr = udps.recvfrom(512)
            if data and len(data) >= 12:
                tx_id = data[0:2]
                idx = 12
                while idx < len(data):
                    l = data[idx]
                    if l == 0:
                        idx += 1
                        break
                    idx += 1 + l
                question = data[12:idx+4]
                resp = (tx_id + b'\x81\x80' + data[4:6] + b'\x00\x01\x00\x00\x00\x00' +
                        question + b'\xc0\x0c\x00\x01\x00\x01\x00\x00\x00\x3c\x00\x04\xc0\xa8\x04\x01')
                udps.sendto(resp, addr)
        except OSError:
            pass
    udps.close()

# --- Setup screen ---
def show_setup_screen():
    orientation = sd_cfg.get('orientation', 'landscape')
    is_portrait = orientation.startswith('portrait')
    logo_file = 'picframes_logo_p.bin' if is_portrait else 'picframes_logo_l.bin'
    buf = bytearray(192000)
    logo_loaded = False
    for path in [logo_file, '/images/' + logo_file]:
        try:
            with open(path, 'rb') as f:
                f.readinto(buf)
            logo_loaded = True
            break
        except Exception:
            pass
    if not logo_loaded:
        buf = bytearray(192000)
        for i in range(0, 192000, 2):
            buf[i] = 0x11  # white/white

    # Clear bottom quarter to solid white
    for y in range(360, 480):
        for xb in range(400):
            buf[y * 400 + xb] = 0x11

    from display_overlay import _render_text_line, apply_battery_square, apply_branding_text
    ap_name = 'PicFrame-' + mac_str.replace(':', '')
    msg_lines = [
        'PLEASE CONNECT USB POWER.',
        'TO SET UP YOUR PICFRAME, CONNECT TO WI-FI NETWORK:',
        ap_name[:40],
        'THEN OPEN http://192.168.4.1/ ON YOUR PHONE OR COMPUTER.',
        "NEED HELP? CONTACT FIN O'FLAHERTY."
    ]
    scale = 1
    line_h = 8 * scale + 4
    total_h = len(msg_lines) * line_h
    y_start = 360 + (120 - total_h) // 2
    y = y_start
    for line in msg_lines:
        _render_text_line(buf, line, y, scale=scale)
        y += line_h

    apply_battery_square(buf, get_bat_pct())
    apply_branding_text(buf)

    orient_suffix = '_p' if is_portrait else '_l'
    out_path = '/no_images' + orient_suffix + '.bin'
    with open(out_path, 'wb') as f:
        f.write(buf)
    del buf
    try:
        from epd import EPD_7in3f
        epd = EPD_7in3f()
        epd.display_file(out_path, orientation=orientation)
        print('Setup screen displayed.')
    except Exception as e:
        print('Setup screen EPD error:', e)

# --- Button actions ---
def action_toggle_orientation():
    print('BOOT button: toggling orientation')
    cur = sd_cfg.get('orientation', 'landscape')
    cycle = {
        'landscape': 'portrait',
        'portrait': 'landscape-upside-down',
        'landscape-upside-down': 'portrait-upside-down',
        'portrait-upside-down': 'landscape',
    }
    new_orient = cycle.get(cur, 'landscape')
    sd_cfg['orientation'] = new_orient
    save_sd_config(sd_cfg)
    server_orient = 'portrait' if 'portrait' in new_orient else 'landscape'
    ssid = flash_cfg.get('wifi_ssid', '')
    pw   = flash_cfg.get('wifi_pass', '')
    if ssid and connect_wifi(ssid, pw, timeout=8):
        try:
            from api import call_change_orientation
            call_change_orientation(
                flash_cfg.get('server_url',''),
                mac_str,
                flash_cfg.get('token',''),
                server_orient
            )
        except Exception as e:
            print('change-orientation failed:', e)
    print('Orientation ->', new_orient, '- rebooting')
    time.sleep_ms(300)
    machine.reset()

def action_key_skip():
    print('KEY button: requesting image skip')
    ssid = flash_cfg.get('wifi_ssid', '')
    pw   = flash_cfg.get('wifi_pass', '')
    if not ssid:
        print('No WiFi config - cannot skip.')
        return
    if not connect_wifi(ssid, pw, timeout=8):
        print('WiFi failed - cannot skip.')
        return
    try:
        from api import call_refresh, sync_ntp
        sync_ntp()
        result = call_refresh(
            flash_cfg.get('server_url',''),
            mac_str,
            flash_cfg.get('token',''),
            skip=True
        )
        idx       = result.get('image_index', sd_cfg.get('image_index', 0))
        orient    = result.get('current_orientation', sd_cfg.get('orientation', 'landscape'))
        sleep_int = result.get('sleep_interval', sd_cfg.get('sleep_interval', 900))
        sd_cfg['image_index'] = idx
        sd_cfg['orientation'] = orient
        sd_cfg['sleep_interval'] = sleep_int
        save_sd_config(sd_cfg)
        images = sd_cfg.get('images', [])
        if images and idx < len(images):
            img_path = resolve_image_path(images[idx], orient)
            render_and_sleep(img_path, orient, sleep_int)
        else:
            go_to_sleep(sleep_int)
    except Exception as e:
        print('Key skip failed:', e)
        go_to_sleep(sd_cfg.get('sleep_interval', 900))

# --- Offline fallback ---
def run_offline_fallback():
    print('Network fail-soft: offline from SD cache.')
    images    = sd_cfg.get('images', [])
    idx       = sd_cfg.get('image_index', 0)
    orient    = sd_cfg.get('orientation', 'landscape')
    sleep_int = sd_cfg.get('sleep_interval', 900)
    if images and idx < len(images):
        img_path = resolve_image_path(images[idx], orient)
        try:
            os.stat(img_path)
            render_and_sleep(img_path, orient, sleep_int)
            return
        except OSError:
            pass
    print('No cached image available.')
    go_to_sleep(sleep_int)

# --- Main connected sequence ---
def run_connected_sequence():
    from api import sync_ntp, call_update, call_daily_config, call_daily_zip, call_refresh
    from update import download_and_apply_update
    server_url = flash_cfg.get('server_url', 'https://picframe.treee.house')
    token      = flash_cfg.get('token', '')
    update_ver = flash_cfg.get('update_version', '')

    # Step 0: NTP
    sync_ntp()

    # Step 1: /update
    try:
        zip_url = call_update(server_url, mac_str, token, HW_PROFILE, update_ver)
        if zip_url:
            print('Update available:', zip_url)
            flash_updated = download_and_apply_update(zip_url)
            if flash_updated:
                print('Flash updated - soft resetting.')
                time.sleep_ms(300)
                machine.soft_reset()
    except Exception as e:
        print('/update failed:', e)
        run_offline_fallback()
        return

    # Step 2: /daily-config
    try:
        dcfg = call_daily_config(server_url, mac_str, token)
        merged = deep_merge_sd_config(sd_cfg, dcfg)
        save_sd_config(merged)
        sd_cfg.update(merged)
    except Exception as e:
        print('/daily-config failed:', e)
        run_offline_fallback()
        return

    # Step 3: /daily-zip
    try:
        daily_ver = sd_cfg.get('daily_zip_version', '')
        new_zip = call_daily_zip(server_url, mac_str, token, daily_ver, '/sd/daily.zip')
        if new_zip:
            from unzip import extract_zip
            wipe_sd_images()
            extract_zip('/sd/daily.zip', '/sd')
            try:
                os.remove('/sd/daily.zip')
            except Exception:
                pass
            if 'daily_zip_version' in dcfg:
                sd_cfg['daily_zip_version'] = dcfg['daily_zip_version']
            save_sd_config(sd_cfg)
    except Exception as e:
        print('/daily-zip failed:', e)
        run_offline_fallback()
        return

    # Step 4: /refresh
    try:
        result = call_refresh(server_url, mac_str, token, skip=False)
        idx       = result.get('image_index', sd_cfg.get('image_index', 0))
        orient    = result.get('current_orientation', sd_cfg.get('orientation', 'landscape'))
        sleep_int = result.get('sleep_interval', sd_cfg.get('sleep_interval', 900))
        sd_cfg['image_index'] = idx
        sd_cfg['orientation'] = orient
        sd_cfg['sleep_interval'] = sleep_int
        save_sd_config(sd_cfg)
    except Exception as e:
        print('/refresh failed:', e)
        run_offline_fallback()
        return

    # Step 5: render & sleep
    images = sd_cfg.get('images', [])
    if images and idx < len(images):
        img_path = resolve_image_path(images[idx], orient)
        try:
            os.stat(img_path)
        except OSError:
            print('Image not found on SD:', img_path)
            run_offline_fallback()
            return
        render_and_sleep(img_path, orient, sleep_int)
    else:
        print('No images in playlist - sleeping.')
        go_to_sleep(sd_cfg.get('sleep_interval', 900))

# --- Entry point ---
if boot_pressed_on_boot:
    action_toggle_orientation()

if key_pressed_on_boot:
    action_key_skip()

if not sd_mounted():
    print('SD not mounted - AP mode (Flash-only).')
    show_setup_screen()
    start_ap_and_portal()

ssid = flash_cfg.get('wifi_ssid', '')
pw   = flash_cfg.get('wifi_pass', '')

if not ssid:
    print('No WiFi configured - running bootstrap.')
    show_setup_screen()
    start_ap_and_portal()
else:
    if connect_wifi(ssid, pw):
        run_connected_sequence()
    else:
        print('WiFi failed - offline fallback.')
        run_offline_fallback()

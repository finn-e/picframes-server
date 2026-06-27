# ==========================================
# FILE VERSION: 1.0.0
# DESCRIPTION: Bootloader script that mounts the SD card, handles AXP PMIC, and starts captive portal if forced.
# ==========================================
import machine
import os
import network
import json
import time

print("--- Frame bootup ---")
try:
    print("Initializing AXP2101 PMIC...")
    from axp import AXP2101
    axp = AXP2101()
    axp.init()
    print("PMIC initialized successfully.")
except Exception as e:
    print("PMIC initialization failed:", e)

# Check BOOT button (GPIO 0) immediately to see if Setup Mode was forced
boot_btn = machine.Pin(0, machine.Pin.IN, machine.Pin.PULL_UP)
time.sleep_ms(50)
force_ap = (boot_btn.value() == 0)
if force_ap:
    print("BOOT button held. Forcing setup AP mode portal.")

# 1. Mount SD Card via 4-bit SDMMC Interface (with retries and delay)
sd_mounted = False
for attempt in range(5):
    try:
        sd = machine.SDCard(slot=1, width=4, sck=machine.Pin(39), cmd=machine.Pin(41), data=(machine.Pin(40), machine.Pin(1), machine.Pin(2), machine.Pin(38)))
        os.mount(sd, '/sd')
        print("SD card mounted successfully at /sd")
        sd_mounted = True
        break
    except Exception as e:
        print("SD mount attempt {} failed: {}".format(attempt + 1, e))
        time.sleep_ms(200)

# URL Decoding Helper
def re_url_decode(s):
    res = []
    i = 0
    while i < len(s):
        if s[i] == '%' and i + 2 < len(s):
            try:
                char_code = int(s[i+1:i+3], 16)
                res.append(chr(char_code))
                i += 3
            except ValueError:
                res.append(s[i])
                i += 1
        elif s[i] == '+':
            res.append(' ')
            i += 1
        else:
            res.append(s[i])
            i += 1
    return "".join(res)
def save_config(config, increment_version=True):
    if increment_version:
        config["version"] = config.get("version", 0) + 1
    try:
        with open('/config.json', 'w') as f:
            json.dump(config, f)
        try:
            os.remove('/wifi_config.json')
        except:
            pass
    except Exception as e:
        print("Failed to save config to Flash:", e)
        
    if sd_mounted:
        try:
            with open('/sd/config.json', 'w') as f:
                json.dump(config, f)
            try:
                os.remove('/sd/wifi_config.json')
            except:
                pass
        except Exception as e:
            print("Failed to save config to SD:", e)

def sync_and_load_config():
    flash_cfg = {}
    sd_cfg = {}
    
    try:
        with open('/config.json', 'r') as f:
            flash_cfg = json.load(f)
    except Exception:
        pass
        
    if sd_mounted:
        try:
            with open('/sd/config.json', 'r') as f:
                sd_cfg = json.load(f)
        except Exception:
            pass
            
    if not flash_cfg:
        try:
            with open('/wifi_config.json', 'r') as f:
                flash_cfg = json.load(f)
                flash_cfg['version'] = flash_cfg.get('version', 1)
        except Exception:
            pass
            
    if sd_mounted and not sd_cfg:
        try:
            with open('/sd/wifi_config.json', 'r') as f:
                sd_cfg = json.load(f)
                sd_cfg['version'] = sd_cfg.get('version', 1)
        except Exception:
            pass

    flash_ver = flash_cfg.get('version', 0)
    sd_ver = sd_cfg.get('version', 0)
    
    config = {}
    needs_sync = False
    
    if flash_ver >= sd_ver and flash_cfg:
        config = flash_cfg
        if sd_mounted and (sd_ver < flash_ver or not sd_cfg):
            needs_sync = True
    elif sd_cfg:
        config = sd_cfg
        needs_sync = True
    else:
        config = {
            "version": 1,
            "ssid": "",
            "password": "",
            "orientation": "landscape",
            "device_id": "",
            "last_image": "",
            "horizontal_flipped": False,
            "vertical_flipped": False,
            "daily_zip_url": "https://picframes.treee.house/api/daily-zip",
            "timer": 60
        }
        needs_sync = True
        
    if not config.get("device_id") or len(config["device_id"]) != 8:
        import urandom
        import ubinascii
        b = bytes([urandom.getrandbits(8) for _ in range(4)])
        config["device_id"] = ubinascii.hexlify(b).decode()
        needs_sync = True
        
    if needs_sync:
        save_config(config, increment_version=False)
        
    return config

def get_or_create_device_id(config):
    # Already handled in sync_and_load_config, but keep for backward compatibility
    return config.get("device_id", "picframe")


def is_usb_connected():
    try:
        from axp import AXP2101
        axp_pmic = AXP2101()
        return axp_pmic.is_usb_connected()
    except Exception as e:
        print("Failed to read VBUS from PMIC in boot:", e)
        return False

ap_portal_active = False

def dns_server_thread():
    global ap_portal_active
    import socket
    import time
    
    udps = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udps.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    
    bound = False
    for attempt in range(5):
        try:
            udps.bind(('', 53))
            bound = True
            break
        except Exception as e:
            print("DNS bind attempt {} failed: {}".format(attempt + 1, e))
            time.sleep_ms(200)
            
    if not bound:
        print("DNS Server failed to bind to port 53")
        udps.close()
        return
        
    udps.settimeout(1.0)
    print("DNS Server thread started on port 53")
    
    while ap_portal_active:
        try:
            data, addr = udps.recvfrom(512)
            if not data or len(data) < 12:
                continue
            
            tx_id = data[0:2]
            flags = b'\x81\x80'
            qdcount = data[4:6]
            ancount = b'\x00\x01'
            nscount = b'\x00\x00'
            arcount = b'\x00\x00'
            
            idx = 12
            while idx < len(data):
                length = data[idx]
                if length == 0:
                    idx += 1
                    break
                idx += 1 + length
            
            question_end = idx + 4
            question = data[12:question_end]
            
            ans_name = b'\xc0\x0c'
            ans_type = b'\x00\x01'
            ans_class = b'\x00\x01'
            ans_ttl = b'\x00\x00\x00\x3c'
            ans_len = b'\x00\x04'
            ans_ip = b'\xc0\xa8\x04\x01'
            
            response = tx_id + flags + qdcount + ancount + nscount + arcount + question + ans_name + ans_type + ans_class + ans_ttl + ans_len + ans_ip
            udps.sendto(response, addr)
        except OSError:
            pass
        except Exception as e:
            print("DNS loop error:", e)
            
    udps.close()
    print("DNS Server thread stopped")

def start_ap_portal():
    global ap_portal_active
    print("Starting Setup Access Point Portal...")
    import ubinascii
    wlan_sta = network.WLAN(network.STA_IF)
    wlan_sta.active(True)
    
    device_id = get_or_create_device_id(wifi_config)
    ap_ssid = "PicFrame - " + device_id
    
    ap = network.WLAN(network.AP_IF)
    ap.active(True)
    ap.config(essid=ap_ssid, authmode=network.AUTH_OPEN)
    
    print("AP started. SSID:", ap_ssid)
    print("AP IPConfig:", ap.ifconfig())
    
    # Start the DNS responder thread
    ap_portal_active = True
    try:
        import _thread
        _thread.start_new_thread(dns_server_thread, ())
    except Exception as e:
        print("Failed to start DNS thread:", e)
        
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(('', 80))
    s.listen(1)
    s.settimeout(2.0)
    
    mac_bytes = wlan_sta.config('mac')
    mac_str = ubinascii.hexlify(mac_bytes, ':').decode()
    
    html_template = """<!DOCTYPE html>
<html>
<head>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>PicFrame Onboarding</title>
    <style>
        @import url('https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;500;700&display=swap');
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{
            font-family: 'Outfit', sans-serif;
            background: radial-gradient(circle at center, hsl(220, 30%, 12%), hsl(220, 35%, 6%));
            color: hsl(220, 20%, 94%);
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
            padding: 24px;
        }}
        .card {{
            background: rgba(30, 41, 59, 0.7);
            backdrop-filter: blur(16px);
            border: 1px solid rgba(255, 255, 255, 0.08);
            padding: 32px;
            border-radius: 20px;
            width: 100%;
            max-width: 440px;
            box-shadow: 0 20px 40px rgba(0,0,0,0.5);
            animation: fadeIn 0.6s ease-out;
        }}
        @keyframes fadeIn {{
            from {{ opacity: 0; transform: translateY(20px); }}
            to {{ opacity: 1; transform: translateY(0); }}
        }}
        h2 {{
            font-weight: 700;
            font-size: 1.8rem;
            margin-bottom: 8px;
            background: linear-gradient(135deg, hsl(190, 100%, 55%), hsl(260, 90%, 65%));
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            text-align: center;
        }}
        .subtitle {{
            text-align: center;
            font-size: 0.9rem;
            color: hsl(220, 15%, 60%);
            margin-bottom: 24px;
        }}
        .info-row {{
            display: flex;
            justify-content: space-between;
            font-size: 0.85rem;
            font-family: monospace;
            background: rgba(0,0,0,0.2);
            padding: 8px 12px;
            border-radius: 8px;
            margin-bottom: 8px;
            color: hsl(200, 100%, 75%);
        }}
        .status-card {{
            border-radius: 12px;
            padding: 16px;
            margin-bottom: 20px;
            display: flex;
            flex-direction: column;
            gap: 12px;
            background: rgba(0,0,0,0.15);
        }}
        .status-item {{
            display: flex;
            align-items: center;
            justify-content: space-between;
            font-size: 0.9rem;
        }}
        .status-label {{
            color: hsl(220, 10%, 70%);
            display: flex;
            align-items: center;
            gap: 8px;
        }}
        .status-value {{
            font-weight: 500;
        }}
        .battery-container {{
            display: flex;
            align-items: center;
            gap: 8px;
        }}
        .battery-outer {{
            width: 50px;
            height: 22px;
            border: 2px solid hsl(220, 15%, 60%);
            border-radius: 4px;
            padding: 2px;
            position: relative;
        }}
        .battery-outer::after {{
            content: '';
            position: absolute;
            right: -5px;
            top: 5px;
            width: 3px;
            height: 8px;
            background: hsl(220, 15%, 60%);
            border-radius: 0 2px 2px 0;
        }}
        .battery-inner {{
            height: 100%;
            border-radius: 2px;
            width: {battery_pct}%;
            background: {battery_color};
            transition: width 0.3s ease;
        }}
        .alert {{
            border-radius: 12px;
            padding: 14px;
            font-size: 0.85rem;
            line-height: 1.4;
            margin-bottom: 20px;
            display: flex;
            align-items: flex-start;
            gap: 10px;
        }}
        .alert-warning {{
            background: rgba(239, 68, 68, 0.12);
            border: 1px solid rgba(239, 68, 68, 0.3);
            color: hsl(0, 85%, 70%);
        }}
        .alert-success {{
            background: rgba(16, 185, 129, 0.12);
            border: 1px solid rgba(16, 185, 129, 0.3);
            color: hsl(140, 75%, 70%);
        }}
        label {{
            display: block;
            font-size: 0.85rem;
            color: hsl(220, 15%, 70%);
            margin-bottom: 6px;
            font-weight: 500;
        }}
        .input-group {{
            margin-bottom: 18px;
            position: relative;
        }}
        input[type=text], input[type=password] {{
            width: 100%;
            padding: 12px 14px;
            background: rgba(15, 23, 42, 0.6);
            border: 1px solid rgba(255, 255, 255, 0.12);
            border-radius: 10px;
            color: #ffffff;
            font-size: 0.95rem;
            transition: all 0.25s ease;
        }}
        input[type=text]:focus, input[type=password]:focus {{
            outline: none;
            border-color: hsl(190, 100%, 55%);
            box-shadow: 0 0 0 3px rgba(56, 189, 248, 0.15);
            background: rgba(15, 23, 42, 0.8);
        }}
        input[type=submit] {{
            width: 100%;
            padding: 14px;
            border: none;
            border-radius: 10px;
            background: linear-gradient(135deg, hsl(190, 100%, 45%), hsl(260, 90%, 55%));
            color: white;
            font-size: 1rem;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.25s ease;
            box-shadow: 0 4px 12px rgba(56, 189, 248, 0.2);
            margin-top: 8px;
        }}
        input[type=submit]:hover {{
            background: linear-gradient(135deg, hsl(190, 100%, 50%), hsl(260, 90%, 60%));
            transform: translateY(-1px);
            box-shadow: 0 6px 16px rgba(56, 189, 248, 0.3);
        }}
        input[type=submit]:active {{
            transform: translateY(1px);
        }}
        .error-box {{
            color: hsl(0, 85%, 65%);
            background: rgba(239, 68, 68, 0.12);
            border: 1px solid rgba(239, 68, 68, 0.2);
            padding: 12px;
            border-radius: 10px;
            margin-bottom: 20px;
            font-size: 0.85rem;
            text-align: center;
        }}
    </style>
</head>
<body>
    <div class="card">
        <h2>📷 PicFrame Onboarding</h2>
        <div class="subtitle">Device Initialization Portal</div>
        
        <div class="info-row">
            <span>ID: {dev_id}</span>
            <span>MAC: {mac}</span>
        </div>
        
        <div class="status-card">
            <div class="status-item">
                <span class="status-label">🔋 Battery Level</span>
                <div class="battery-container">
                    <span class="status-value">{battery_pct}%</span>
                    <div class="battery-outer">
                        <div class="battery-inner"></div>
                    </div>
                </div>
            </div>
            <div class="status-item">
                <span class="status-label">⚡ Power Source</span>
                <span class="status-value" style="color: {power_color};">{power_source}</span>
            </div>
        </div>
        
        {alert_card}
        {error_msg}
        
        <form method="POST" action="/save">
            <div class="input-group">
                <label>Wi-Fi Network Name (SSID)</label>
                <input type="text" name="ssid" value="{ssid}" placeholder="Enter Wi-Fi SSID" required>
            </div>
            <div class="input-group">
                <label>Wi-Fi Password</label>
                <input type="password" name="password" value="{password}" placeholder="Enter Wi-Fi Password">
            </div>
            <div class="input-group">
                <label>PicFrames Server IP/DNS {req_label}</label>
                <input type="text" name="server_ip" value="{server_ip}" {req_attr} placeholder="e.g. 192.168.1.100:8000">
            </div>
            <input type="submit" value="Save & Configure Frame">
        </form>
    </div>
</body>
</html>"""

    error_msg = ""
    req_label = "(Optional)"
    req_attr = ""
    
    ssid_val = wifi_config.get("ssid", "")
    password_val = wifi_config.get("password", "")
    server_ip_val = wifi_config.get("server_ip", "")
    
    start_time = time.time()
    portal_timeout = 180 # 3 minutes
    config_saved = False
    
    while time.time() - start_time < portal_timeout:
        try:
            conn, addr = s.accept()
            req_bytes = conn.recv(1024)
            if not req_bytes:
                conn.close()
                continue
            request = req_bytes.decode('utf-8', 'ignore')
            
            # Parse request line to detect captive portal probes
            lines = request.split("\r\n")
            first_line = lines[0] if lines else ""
            parts = first_line.split(" ")
            method = parts[0] if len(parts) > 0 else ""
            path = parts[1] if len(parts) > 1 else ""
            
            is_portal_path = (path == "/" or path.startswith("/?") or path.startswith("/save"))
            is_local_host = ("192.168.4.1" in request)
            
            if not (is_portal_path and is_local_host):
                # Redirect non-portal requests to the portal root
                redirect_resp = (
                    "HTTP/1.1 302 Found\r\n"
                    "Location: http://192.168.4.1/\r\n"
                    "Content-Length: 0\r\n"
                    "Connection: close\r\n\r\n"
                )
                conn.send(redirect_resp)
                conn.close()
                continue
                
            if "POST /save" in request:
                body = request.split("\r\n\r\n")[-1]
                params = {}
                for param in body.split("&"):
                    if "=" in param:
                        k, v = param.split("=")
                        params[k] = re_url_decode(v)
                        
                ssid = params.get("ssid", "").strip()
                password = params.get("password", "").strip()
                server_ip = params.get("server_ip", "").strip()
                
                if ssid:
                    print("Testing Wi-Fi connection to:", ssid)
                    wlan_sta.active(True)
                    wlan_sta.connect(ssid, password)
                    
                    connect_success = False
                    test_start = time.time()
                    while time.time() - test_start < 10:
                        if wlan_sta.isconnected():
                            connect_success = True
                            break
                        time.sleep_ms(100)
                        
                    # Always save credentials and reboot
                    wifi_config["ssid"] = ssid
                    wifi_config["password"] = password
                    wifi_config["server_ip"] = server_ip
                    
                    if server_ip:
                        resolved_ip = server_ip
                        resolved_port = 8000
                        if ":" in resolved_ip:
                            resolved_ip, port_str = resolved_ip.split(":")
                            try: resolved_port = int(port_str)
                            except ValueError: pass
                        wifi_config["api_url"] = "http://{}:{}/api/wakeup".format(resolved_ip, resolved_port)
                        wifi_config["daily_zip_url"] = "http://{}:{}/api/daily-zip".format(resolved_ip, resolved_port)
                        wifi_config["update_url"] = "http://{}:{}/api/update".format(resolved_ip, resolved_port)
                    else:
                        wifi_config.pop("api_url", None)
                        wifi_config.pop("daily_zip_url", None)
                        wifi_config.pop("update_url", None)
                    
                    save_config(wifi_config)                        
                    if connect_success:
                        print("Connection successful! Saving credentials and rebooting...")
                        conn.send("HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n\r\n")
                        conn.send("<html><body><h3>Connection successful! Config saved. Rebooting...</h3></body></html>")
                    else:
                        print("Connection test failed. Saving and rebooting to retry connection...")
                        conn.send("HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n\r\n")
                        conn.send("<html><body><h3>Connection test failed, but credentials saved. Rebooting to retry connection...</h3></body></html>")
                    conn.close()
                    config_saved = True
                    break
            else:
                # Query PMIC state dynamically
                try:
                    from axp import AXP2101
                    axp_pmic_instance = AXP2101()
                    bat_pct = axp_pmic_instance.get_battery_percentage()
                    usb_conn = axp_pmic_instance.is_usb_connected()
                except Exception:
                    bat_pct = 100
                    usb_conn = True

                if bat_pct >= 60:
                    bat_color = "hsl(140, 75%, 50%)"
                elif bat_pct >= 30:
                    bat_color = "hsl(45, 85%, 50%)"
                else:
                    bat_color = "hsl(10, 80%, 55%)"

                if usb_conn:
                    power_source = "USB Power"
                    power_color = "hsl(140, 75%, 65%)"
                    alert_card = """
                    <div class="alert alert-success">
                        <span>🔌</span>
                        <div>
                            <strong>USB Power Connected</strong><br>
                            Perfect! Keep the device plugged in to ensure a successful onboarding process.
                        </div>
                    </div>
                    """
                else:
                    power_source = "Battery"
                    power_color = "hsl(45, 85%, 60%)"
                    alert_card = """
                    <div class="alert alert-warning">
                        <span>⚠️</span>
                        <div>
                            <strong>USB Power Disconnected!</strong><br>
                            Please plug the frame into USB power during setup to prevent it from shutting down.
                        </div>
                    </div>
                    """

                html = html_template.format(
                    dev_id=device_id.upper(),
                    mac=mac_str.upper(),
                    battery_pct=bat_pct,
                    battery_color=bat_color,
                    power_source=power_source,
                    power_color=power_color,
                    alert_card=alert_card,
                    error_msg=error_msg,
                    ssid=ssid_val,
                    password=password_val,
                    server_ip=server_ip_val,
                    req_label=req_label,
                    req_attr=req_attr
                )
                conn.send("HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n\r\n")
                conn.send(html)
                conn.close()
        except OSError:
            pass
            
    s.close()
    ap_portal_active = False
    ap.active(False)
    if config_saved:
        print("Rebooting device...")
        time.sleep_ms(500)
        machine.reset()

wlan = network.WLAN(network.STA_IF)
wlan.active(True)

wifi_config = sync_and_load_config()
ssid = wifi_config.get("ssid", "")
password = wifi_config.get("password", "")
device_id = wifi_config.get("device_id", "picframe")

if force_ap:
    # Explicitly forced AP portal via button hold
    start_ap_portal()
elif not ssid:
    print("No Wi-Fi credentials. Proceeding to main.py for display and onboarding AP portal setup.")
else:
    # We have credentials, try connecting
    print("Connecting to Wi-Fi:", ssid)
    wlan.connect(ssid, password)
    
    start_time = time.time()
    while not wlan.isconnected():
        if time.time() - start_time > 10:
            print("Wi-Fi Connection Timeout!")
            break
        time.sleep_ms(100)
    
    if wlan.isconnected():
        print("Connected! Network Config:", wlan.ifconfig())
    else:
        print("Could not connect. Proceeding to main.py for AP portal/slideshow handling.")

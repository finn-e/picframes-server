import machine
import os
import network
import json
import time
from axp import AXP2101

print("--- Frame bootup ---")
try:
    print("Initializing AXP2101 PMIC...")
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

def start_ap_portal():
    print("Starting Setup Access Point Portal...")
    import ubinascii
    wlan_sta = network.WLAN(network.STA_IF)
    wlan_sta.active(True)
    mac_bytes = wlan_sta.config('mac')
    mac_str = ubinascii.hexlify(mac_bytes).decode()
    ap_ssid = "PicFrame-" + mac_str[-6:].upper()
    
    ap = network.WLAN(network.AP_IF)
    ap.active(True)
    ap.config(essid=ap_ssid, authmode=network.AUTH_OPEN)
    
    print("AP started. SSID:", ap_ssid)
    print("AP IPConfig:", ap.ifconfig())
    
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(('', 80))
    s.listen(1)
    s.settimeout(2.0)
    
    html = """<!DOCTYPE html>
<html>
<head>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>PicFrame Wi-Fi Setup</title>
    <style>
        body { font-family: sans-serif; background: #0f172a; color: #f1f3f9; padding: 20px; }
        h2 { color: #38bdf8; }
        .card { background: #1e293b; padding: 20px; border-radius: 12px; max-width: 400px; margin: 0 auto; box-shadow: 0 4px 6px rgba(0,0,0,0.3); }
        input[type=text], input[type=password] { width: 100%; padding: 10px; margin: 10px 0; box-sizing: border-box; background: #0f172a; color: white; border: 1px solid #334155; border-radius: 6px; }
        input[type=submit] { background: #0ea5e9; color: white; border: none; padding: 12px; width: 100%; border-radius: 6px; font-weight: bold; cursor: pointer; }
        input[type=submit]:hover { background: #0284c7; }
    </style>
</head>
<body>
    <div class="card">
        <h2>📷 PicFrame Wi-Fi Config</h2>
        <p style="font-family: monospace; font-size: 0.9rem; color: #38bdf8;">MAC: {}</p>
        <p>Enter the credentials to connect your frame to your local Wi-Fi:</p>
        <form method="POST" action="/save">
            <label>SSID (Network Name):</label>
            <input type="text" name="ssid" placeholder="MyHomeWiFi" required>
            <label>Password:</label>
            <input type="password" name="password" placeholder="••••••••" required>
            <input type="submit" value="Save & Connect">
        </form>
    </div>
</body>
</html>""".format(ubinascii.hexlify(mac_bytes, ':').decode().upper())

    start_time = time.time()
    portal_timeout = 180 # 3 minutes
    config_saved = False
    
    while time.time() - start_time < portal_timeout:
        try:
            conn, addr = s.accept()
            request = conn.recv(1024).decode('utf-8')
            if not request:
                conn.close()
                continue
                
            if "POST /save" in request:
                body = request.split("\r\n\r\n")[-1]
                params = {}
                for param in body.split("&"):
                    if "=" in param:
                        k, v = param.split("=")
                        params[k] = re_url_decode(v)
                        
                ssid = params.get("ssid")
                password = params.get("password")
                
                if ssid:
                    wifi_config = {"ssid": ssid, "password": password or ""}
                    if sd_mounted:
                        try:
                            with open('/sd/wifi_config.json', 'w') as f:
                                json.dump(wifi_config, f)
                        except Exception as e:
                            print("Write to SD failed:", e)
                    try:
                        with open('wifi_config.json', 'w') as f:
                            json.dump(wifi_config, f)
                    except Exception:
                        pass
                        
                    conn.send("HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n\r\n")
                    conn.send("<html><body><h3>Settings saved successfully! Rebooting...</h3></body></html>")
                    conn.close()
                    config_saved = True
                    break
            else:
                conn.send("HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n\r\n")
                conn.send(html)
                conn.close()
        except OSError:
            pass
            
    s.close()
    ap.active(False)
    if config_saved:
        print("Rebooting device...")
        time.sleep_ms(500)
        machine.reset()

# 2. Connect to Wi-Fi network
wlan = network.WLAN(network.STA_IF)
wlan.active(True)

wifi_config = {}
if sd_mounted:
    try:
        with open('/sd/wifi_config.json', 'r') as f:
            wifi_config = json.load(f)
            print("Loaded Wi-Fi config from SD card.")
    except Exception:
        pass

if not wifi_config:
    try:
        with open('wifi_config.json', 'r') as f:
            wifi_config = json.load(f)
            print("Loaded Wi-Fi config from Flash.")
    except Exception:
        pass

ssid = wifi_config.get("ssid", "")
password = wifi_config.get("password", "")

# If boot button was held or we have no Wi-Fi credentials, start the AP setup portal
if force_ap or not ssid:
    start_ap_portal()

if ssid:
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
        print("Could not connect. Skipping AP Portal to allow offline playback fallback.")
else:
    print("No Wi-Fi credentials. Starting AP Portal.")
    start_ap_portal()

import time
import machine
import os
import json
import network
import urequests as requests
import socket
import urandom as random
import ubinascii
from unzip import extract_zip
from epd import EPD_7in3f

# Hardcoded safe fallback
def is_usb_connected():
    return True

print("--- Frame main loop starting ---")

# Track awake time to enforce the 45s safety timeout
start_awake = time.time()
SAFETY_TIMEOUT = 45 # seconds

# Initialize Button Pins (active Low, with internal pull-ups)
boot_btn = machine.Pin(0, machine.Pin.IN, machine.Pin.PULL_UP)
key_btn = machine.Pin(4, machine.Pin.IN, machine.Pin.PULL_UP)
pwr_btn = machine.Pin(5, machine.Pin.IN, machine.Pin.PULL_DOWN)

# Settle and check buttons immediately on boot (debounced)
time.sleep_ms(100)

boot_pressed_on_boot = False
if boot_btn.value() == 0:
    print("BOOT button detected on boot.")
    boot_pressed_on_boot = True
    while boot_btn.value() == 0:
        time.sleep_ms(10)

key_pressed_on_boot = False
if key_btn.value() == 0:
    print("KEY button detected on boot.")
    key_pressed_on_boot = True
    while key_btn.value() == 0:
        time.sleep_ms(10)

# WLAN configuration and MAC Address resolution
wlan = network.WLAN(network.STA_IF)
mac_bytes = wlan.config('mac')
mac_str = ubinascii.hexlify(mac_bytes, ':').decode()
print("Device MAC Address:", mac_str)

# Load config settings
wifi_cfg = {}
try:
    with open('/sd/wifi_config.json', 'r') as f:
        wifi_cfg = json.load(f)
except Exception:
    try:
        with open('wifi_config.json', 'r') as f:
            wifi_cfg = json.load(f)
    except Exception:
        pass

FIRMWARE_VERSION = "0.1.1"

# Default server URLs (will be overridden by mDNS if discovered)
api_url = wifi_cfg.get("api_url", "https://picframes.treee.house/api/wakeup")
daily_zip_url = wifi_cfg.get("daily_zip_url", "https://picframes.treee.house/api/daily-zip")
update_url = wifi_cfg.get("update_url", "https://picframes.treee.house/api/update")
sleep_time = 900 # default 15 minutes

# Try loading from the sync config.json if present on SD
try:
    with open('/sd/config.json', 'r') as f:
        c = json.load(f)
        sleep_time = c.get("timer", sleep_time)
except Exception:
    pass

def go_to_sleep(seconds):
    if is_usb_connected():
        print("USB host detected. Skipping deep sleep to prevent disconnect/reconnect loop.")
        print("Waiting {} seconds instead (REPL/buttons active)...".format(seconds))
        wait_with_button_check(seconds)
        return

    print("Entering deep sleep for {} seconds...".format(seconds))
    try:
        from axp import AXP2101
        axp = AXP2101()
        axp.disable_power()
        print("PMIC power rails disabled for deep sleep.")
    except Exception as e:
        print("Failed to disable PMIC power rails:", e)
    # Configure RTC wakeup timer
    rtc = machine.RTC()
    rtc.datetime() # init RTC
    machine.deepsleep(seconds * 1000)

def ensure_wifi_connected():
    if not wlan.active():
        wlan.active(True)
    if not wlan.isconnected():
        print("Re-connecting to Wi-Fi...")
        ssid = wifi_cfg.get("ssid", "")
        password = wifi_cfg.get("password", "")
        if ssid:
            wlan.connect(ssid, password)
            start_t = time.time()
            while not wlan.isconnected() and time.time() - start_t < 10:
                time.sleep_ms(100)
    return wlan.isconnected()

# Discovery of central server via mDNS query
def discover_server_mdns():
    print("Attempting to discover PicFrames server via mDNS...")
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(2.0)
    try:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 20)
    except Exception:
        pass
        
    # Unicast Response (QU bit) query packet for PTR of _picframes._tcp.local
    packet = b'\x00\x00\x00\x00\x00\x01\x00\x00\x00\x00\x00\x00\x0b_picframes\x04_tcp\x05local\x00\x00\x0c\x80\x01'
    
    for attempt in range(3):
        try:
            print("Sending mDNS query attempt {}...".format(attempt + 1))
            sock.sendto(packet, ('224.0.0.251', 5353))
            
            start_t = time.time()
            while time.time() - start_t < 2.0:
                data, addr = sock.recvfrom(1024)
                if b'_picframes' in data:
                    port = 8000
                    # Try to parse the port from the SRV record if present
                    idx = data.find(b'\x00\x21\x00\x01')
                    if idx == -1:
                        idx = data.find(b'\x00\x21\x80\x01')
                    if idx != -1:
                        if idx + 16 <= len(data):
                            port = (data[idx+14] << 8) | data[idx+15]
                            print("Parsed port from SRV record:", port)
                    
                    print("Discovered server at:", "http://{}:{}".format(addr[0], port))
                    sock.close()
                    return "http://{}:{}".format(addr[0], port)
        except Exception:
            pass
            
    sock.close()
    return None

# Fallback offline playback mode
def run_offline_fallback():
    print("Falling back to local offline slideshow...")
    offline_sleep = 900
    shuffle = False
    try:
        with open('/sd/config.json', 'r') as f:
            c = json.load(f)
            offline_sleep = c.get("timer", offline_sleep)
            shuffle = c.get("shuffle", shuffle)
    except Exception:
        pass
        
    files = []
    try:
        with open('/sd/index.json', 'r') as f:
            files = json.load(f)
    except Exception:
        try:
            with open('/sd/list.json', 'r') as f:
                files = json.load(f)
        except Exception:
            pass
            
    if not files:
        try:
            files = [f for f in os.listdir('/sd') if f.endswith('.bin')]
        except Exception:
            pass
            
    if not files:
        print("No image bin files found on SD card!")
        go_to_sleep(offline_sleep)
        return

    current_displayed = ""
    try:
        with open('/sd/current_image.txt', 'r') as f:
            current_displayed = f.read().strip()
    except Exception:
        pass
        
    target_image = None
    if shuffle:
        target_image = files[random.getrandbits(12) % len(files)]
    else:
        try:
            idx = files.index(current_displayed)
            target_image = files[(idx + 1) % len(files)]
        except ValueError:
            target_image = files[0]
            
    if target_image:
        print("Offline selected target image:", target_image)
        if target_image != current_displayed:
            disconnect_wifi_and_refresh(target_image)
        else:
            print("Target image already displayed offline.")
            
    go_to_sleep(offline_sleep)

def toggle_orientation():
    print("Toggling orientation...")
    current_orient = wifi_cfg.get('orientation', 'landscape')
    new_orient = 'portrait' if current_orient == 'landscape' else 'landscape'
    wifi_cfg['orientation'] = new_orient
    
    try:
        with open('/sd/wifi_config.json', 'w') as f:
            json.dump(wifi_cfg, f)
        print("Orientation updated locally to:", new_orient)
    except Exception as e:
        print("Failed to save local orientation:", e)
        
    if ensure_wifi_connected():
        base_url = api_url.rsplit('/', 2)[0]
        orient_url = base_url + "/device_orientation"
        reset_url = base_url + "/api/wakeup/reset"
        
        try:
            print("Notifying server: {} -> {}".format(mac_str, new_orient))
            res = requests.post(orient_url, json={"mac": mac_str, "orientation": new_orient}, timeout=5)
            print("Server response:", res.text)
            res.close()
            
            print("Resetting server wakeup state...")
            res2 = requests.post(reset_url, timeout=5)
            print("Server response:", res2.text)
            res2.close()
        except Exception as e:
            print("Failed to notify server of orientation change:", e)
    else:
        print("Cannot update server (no Wi-Fi).")
        
    print("Rebooting device...")
    time.sleep_ms(500)
    machine.reset()

def advance_next_image():
    print("Advancing next image...")
    if ensure_wifi_connected():
        base_url = api_url.rsplit('/', 2)[0]
        next_url = base_url + "/api/wakeup/next"
        try:
            print("Notifying server to advance slideshow...")
            res = requests.post(next_url, timeout=5)
            print("Server response:", res.text)
            res.close()
        except Exception as e:
            print("Failed to notify server:", e)
    else:
        print("Cannot notify server (no Wi-Fi).")
        
    print("Rebooting device...")
    time.sleep_ms(500)
    machine.reset()

def wait_with_button_check(seconds):
    start = time.time()
    while time.time() - start < seconds:
        if not is_usb_connected():
            elapsed = time.time() - start_awake
            if elapsed > SAFETY_TIMEOUT:
                print("Safety timeout (45s) exceeded inside wait. Sleeping.")
                go_to_sleep(sleep_time)
            
        if boot_btn.value() == 0:
            time.sleep_ms(50)
            if boot_btn.value() == 0:
                while boot_btn.value() == 0:
                    time.sleep_ms(10)
                toggle_orientation()
                
        if key_btn.value() == 0:
            time.sleep_ms(50)
            if key_btn.value() == 0:
                while key_btn.value() == 0:
                    time.sleep_ms(10)
                advance_next_image()
                
        if pwr_btn.value() == 1:
            time.sleep_ms(50)
            if pwr_btn.value() == 1:
                while pwr_btn.value() == 1:
                    time.sleep_ms(10)
                print("PWR button pressed. Rebooting to force sync check-in...")
                machine.reset()
                
        time.sleep_ms(50)

# Check if orientation toggle or image skip was requested at boot
if boot_pressed_on_boot:
    toggle_orientation()

if key_pressed_on_boot:
    advance_next_image()

# Discover central server via mDNS if Wi-Fi connected
if wlan.isconnected():
    discovered_server = discover_server_mdns()
    if discovered_server:
        api_url = discovered_server + "/api/wakeup"
        daily_zip_url = discovered_server + "/api/daily-zip"
        update_url = discovered_server + "/api/update"
        print("Discovered server endpoint dynamically via mDNS:", discovered_server)
else:
    print("No Wi-Fi connection. Falling back to offline fallback.")
    run_offline_fallback()

poll_interval = 15
device_id = wifi_cfg.get("device_id", "picframe_node")

def disconnect_wifi_and_refresh(target_image):
    if wlan.isconnected():
        print("Disconnecting Wi-Fi to prevent power brownout during refresh...")
        wlan.active(False)
    try:
        epd = EPD_7in3f()
        print("Writing to display:", target_image)
        epd.display_file("/sd/" + target_image)
        print("Display updated successfully.")
        try:
            with open('/sd/current_image.txt', 'w') as f:
                f.write(target_image)
        except Exception as e:
            print("Failed to save current_image.txt:", e)
    except Exception as e:
        print("Display refresh failed:", e)

print("Polling server at:", api_url)

while True:
    if not is_usb_connected():
        elapsed = time.time() - start_awake
        if elapsed > SAFETY_TIMEOUT:
            print("Safety timeout (45s) exceeded! Entering deep sleep to protect battery.")
            if wlan.active():
                wlan.active(False)
            go_to_sleep(sleep_time)

    wifi_ok = ensure_wifi_connected()
    if not wifi_ok:
        print("Wi-Fi down. Falling back to offline playback.")
        run_offline_fallback()
        continue

    # Poll wakeup API sending MAC address
    try:
        payload = {"device_id": device_id, "mac": mac_str, "version": FIRMWARE_VERSION}
        res = requests.post(api_url, json=payload, headers={"Content-Type": "application/json"}, timeout=5)
        response_text = res.text.strip()
        res.close()
        print("Wakeup response:", response_text)
    except Exception as e:
        print("HTTP request failed:", e)
        run_offline_fallback()
        continue

    # Parse response
    remaining = None
    parts = [p.strip() for p in response_text.split(" - ")]
    if len(parts) >= 2:
        status = parts[0]
        target_image = parts[1]
        if len(parts) >= 3:
            try:
                remaining = int(parts[2])
            except ValueError:
                pass
    else:
        status = response_text.strip()
        target_image = "None"

    if status == "DEBUG":
        poll_interval = 10
        print("[SERVER DEBUG MODE] Active. Wi-Fi kept alive, e-paper bypassed.")
        wait_with_button_check(poll_interval)
        continue

    if status == "UPDATE":
        print("Firmware update available! Downloading ZIP from:", update_url)
        try:
            res = requests.get(update_url, timeout=10)
            zip_path = "/sd/update.zip"
            try:
                os.stat("/sd")
            except OSError:
                zip_path = "update.zip"
            
            with open(zip_path, 'wb') as f:
                chunk = bytearray(2048)
                while True:
                    n = res.raw.readinto(chunk)
                    if not n:
                        break
                    f.write(chunk if n == len(chunk) else chunk[:n])
            res.close()
            print("Downloaded firmware update. Extracting...")
            extract_zip(zip_path, "")
            os.remove(zip_path)
            print("Firmware update extracted successfully! Soft-rebooting...")
            time.sleep(0.5)
            machine.soft_reset()
        except Exception as e:
            print("Firmware update failed:", e)
        continue

    # Sync trigger check
    force_redownload = "REDOWNLOAD" in response_text

    if target_image and target_image != "None" and target_image.endswith(".bin"):
        local_path = "/sd/" + target_image
        image_exists = False
        try:
            os.stat(local_path)
            image_exists = True
        except OSError:
            pass

        if not image_exists or force_redownload:
            print("Target image missing or sync triggered by REDOWNLOAD. Downloading daily-zip...")
            try:
                url_with_mac = daily_zip_url + "?mac=" + mac_str
                print("Downloading ZIP from:", url_with_mac)
                res = requests.get(url_with_mac, timeout=10)
                zip_path = "/sd/daily.zip"
                
                with open(zip_path, 'wb') as f:
                    chunk = bytearray(2048)
                    while True:
                        n = res.raw.readinto(chunk)
                        if not n:
                            break
                        f.write(chunk if n == len(chunk) else chunk[:n])
                res.close()
                print("Downloaded daily.zip successfully. Extracting...")
                
                extract_zip(zip_path, "/sd")
                os.remove(zip_path)
                print("Sync complete.")
                
                try:
                    with open('/sd/config.json', 'r') as f:
                        c = json.load(f)
                        sleep_time = c.get("timer", sleep_time)
                except Exception:
                    pass
            except Exception as e:
                print("Sync failed:", e)
                run_offline_fallback()
                continue

    if status == "WAIT":
        current_displayed = ""
        try:
            with open('/sd/current_image.txt', 'r') as f:
                current_displayed = f.read().strip()
        except OSError:
            pass
            
        if remaining is not None:
            print("Status: WAIT. Remaining sleep: {}s. Target: {}".format(remaining, target_image))
            if target_image and target_image != "None" and target_image.endswith(".bin"):
                if target_image != current_displayed:
                    print("Refreshing screen first...")
                    disconnect_wifi_and_refresh(target_image)
            
            if wlan.active():
                wlan.active(False)
            go_to_sleep(remaining)
        else:
            poll_interval = 15
            print("Status: WAIT (GATHERING). Polling every 15s...")
            wait_with_button_check(poll_interval)
            
    elif status == "READY":
        poll_interval = 1
        print("Status: READY. Fast polling (1s)...")
        wait_with_button_check(poll_interval)
        
    elif status == "CHANGE":
        print("Status: CHANGE. Commencing e-paper refresh...")
        if target_image and target_image != "None" and target_image.endswith(".bin"):
            disconnect_wifi_and_refresh(target_image)
        
        if wlan.active():
            wlan.active(False)
        go_to_sleep(sleep_time)
        
    else:
        print("Unknown status: {}. Falling back to offline loop.".format(status))
        run_offline_fallback()

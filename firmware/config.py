# ==========================================
# FILE VERSION: 2.0.0
# DESCRIPTION: Configuration management. Internal Flash stores infrastructure
#              parameters only. SD card stores runtime/presentation state.
# ==========================================
import json
import os

# Keys that live exclusively on internal Flash
FLASH_KEYS = {
    'wifi_ssid', 'wifi_pass', 'server_url', 'username', 'token',
    'update_version', 'landscape_flipped', 'portrait_flipped'
}

FLASH_CONFIG_PATH = '/config.json'
SD_CONFIG_PATH = '/sd/config.json'

DEFAULT_FLASH_CONFIG = {
    'wifi_ssid': '',
    'wifi_pass': '',
    'server_url': 'https://picframe.treee.house',
    'username': '',
    'token': '',
    'update_version': '',
    'landscape_flipped': False,
    'portrait_flipped': False,
}

DEFAULT_SD_CONFIG = {
    'orientation': 'landscape',
    'sleep_interval': 900,
    'image_index': 0,
    'daily_zip_version': '',
    'images': [],
}

def load_flash_config():
    """Load config from internal Flash. Returns merged with defaults."""
    cfg = dict(DEFAULT_FLASH_CONFIG)
    try:
        with open(FLASH_CONFIG_PATH, 'r') as f:
            data = json.load(f)
        for k in FLASH_KEYS:
            if k in data:
                cfg[k] = data[k]
    except Exception as e:
        print('Flash config load error:', e)
    return cfg

def save_flash_config(cfg):
    """Save only Flash-eligible keys to /config.json."""
    to_save = {k: cfg[k] for k in FLASH_KEYS if k in cfg}
    try:
        with open(FLASH_CONFIG_PATH, 'w') as f:
            json.dump(to_save, f)
        print('Flash config saved.')
        return True
    except Exception as e:
        print('Flash config save error:', e)
        return False

def load_sd_config():
    """Load runtime config from SD card. Returns merged with defaults."""
    cfg = dict(DEFAULT_SD_CONFIG)
    try:
        with open(SD_CONFIG_PATH, 'r') as f:
            data = json.load(f)
        cfg.update(data)
    except Exception as e:
        print('SD config load error:', e)
    return cfg

def save_sd_config(cfg):
    """Save runtime state to /sd/config.json. Only SD-eligible keys (not Flash keys)."""
    to_save = {k: v for k, v in cfg.items() if k not in FLASH_KEYS}
    try:
        with open(SD_CONFIG_PATH, 'w') as f:
            json.dump(to_save, f)
        print('SD config saved.')
        return True
    except Exception as e:
        print('SD config save error:', e)
        return False

def deep_merge_sd_config(existing, incoming):
    """Deep merge incoming dict into existing, skipping Flash keys."""
    merged = dict(existing)
    for k, v in incoming.items():
        if k not in FLASH_KEYS:
            merged[k] = v
    return merged

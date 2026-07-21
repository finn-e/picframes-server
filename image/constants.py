_DEFAULT_SCREEN = (800, 480)

# hw_profile → (landscape_width, landscape_height)
SCREEN_TYPES = {
    # Canonical names
    'Seeed-EE04-Spectra6-7in3':   (800,  480),
    'Seeed-EE02-Spectra6-13in3':  (1600, 1200),
    'Waveshare-PhotoPainter-7in3': (800,  480),
    # Legacy names kept so pre-alias-map DB entries still resolve
    'ESP32-S3-PhotoPainter': (800,  480),
    'XIAO-EE04-7in3':        (800,  480),
    'XIAO-EE04-13in3':       (1600, 1200),  # legacy alias
}

DEVICE_TYPE_ALIASES = {
    'ESP32-S3-PhotoPainter': 'Waveshare-PhotoPainter-7in3',
    'XIAO-EE04-7in3':        'Seeed-EE04-Spectra6-7in3',
    'XIAO-EE04-13in3':       'Seeed-EE02-Spectra6-13in3',
}

# hw_profile → (W, H, landscape_ratio_str, portrait_ratio_str)
SCREEN_SPECS = {
    'Waveshare-PhotoPainter-7in3':  (800,  480,  'l53', 'p35'),
    'Seeed-EE04-Spectra6-7in3':     (800,  480,  'l53', 'p35'),
    'Seeed-EE02-Spectra6-13in3':    (1600, 1200, 'l43', 'p34'),
}

# Resolution → (landscape_ratio_str, portrait_ratio_str)
_RESOLUTION_RATIOS = {
    (800,  480):  ('l53', 'p35'),
    (1600, 1200): ('l43', 'p34'),
}


def normalize_device_type(hw_profile):
    return DEVICE_TYPE_ALIASES.get(hw_profile or '', hw_profile or '')


def screen_size_for_profile(hw_profile):
    canonical = normalize_device_type(hw_profile)
    return SCREEN_TYPES.get(canonical or '', _DEFAULT_SCREEN)


def screen_spec_for_profile(hw_profile):
    """Return (W, H, l_ratio, p_ratio) for a canonical hw_profile, or default."""
    canonical = normalize_device_type(hw_profile)
    return SCREEN_SPECS.get(canonical, (800, 480, 'l53', 'p35'))


def ratios_for_screen(W, H):
    """Return (landscape_ratio, portrait_ratio) strings for a resolution."""
    return _RESOLUTION_RATIOS.get((W, H), ('l53', 'p35'))


def _artifact_infix(w, h):
    """Empty for the legacy 800×480 screen; '_WxH' for others."""
    if (w, h) == _DEFAULT_SCREEN:
        return ''
    return f'_{w}x{h}'

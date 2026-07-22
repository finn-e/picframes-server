# ==========================================================================================
# DESCRIPTION: Flask server entrypoint. Initialises app, DB, registers UI/API/Admin routes, and runs mDNS zeroconf broadcasting.
# DEPENDENCIES: Flask, zeroconf, routes (ui, api, admin), db
# ==========================================================================================
import logging
import os
import socket

from flask import Flask, make_response, redirect, request, session, url_for
from zeroconf import IPVersion, Zeroconf, ServiceInfo

from db import init_db

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Informational fallback only — CI computes the real version from commit history
# (paulhatch/semantic-version) and injects it via the SERVER_VERSION_OVERRIDE env
# var (Docker build ARG → ENV). Manual bumps here are no longer needed for deploys.
SERVER_VERSION = "0.21.0"
EFFECTIVE_SERVER_VERSION = os.environ.get('SERVER_VERSION_OVERRIDE') or SERVER_VERSION
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', 'admin')


def create_app():
    app = Flask(__name__, template_folder='templates')
    app.secret_key           = os.environ.get('SECRET_KEY', 'picframes_secret_session_key_12345')
    app.config['ADMIN_PASSWORD'] = ADMIN_PASSWORD
    app.config['SERVER_VERSION'] = EFFECTIVE_SERVER_VERSION

    # Auth gate — runs before every request
    @app.before_request
    def auth_gate():
        path = request.path
        # Deprecated bare-root paths pass through (device firmware still uses them)
        # Bare-root deprecated paths (firmware still uses these; /api/* already open above)
        deprecated_open = (
            '/daily-config', '/api/daily-config',
            '/daily-zip',    '/api/daily-zip',
            '/refresh',
            '/update',
        )
        if (path.startswith('/api/')
                or path.startswith('/static/')
                or path.startswith('/ui/originals/')
                or path.startswith('/ui/images/')
                or path == '/ui/login'
                or path == '/'
                or path in deprecated_open):
            return None
        if not session.get('authenticated'):
            return redirect(url_for('ui.login'))

    from routes.ui    import ui_bp
    from routes.api   import api_bp
    from routes.admin import admin_bp

    app.register_blueprint(ui_bp,    url_prefix='/ui')
    app.register_blueprint(api_bp,   url_prefix='/api')
    app.register_blueprint(admin_bp, url_prefix='/admin')

    @app.route('/')
    def root_redirect():
        return redirect('/ui/')

    def _deprecated(new_url, view_fn):
        """Wrap a view to add Deprecation headers and log a warning."""
        import functools
        @functools.wraps(view_fn)
        def wrapper(*args, **kwargs):
            logger.warning(f"Deprecated route called: {request.path} → {new_url}")
            resp = make_response(view_fn(*args, **kwargs))
            resp.headers['Deprecation'] = 'true'
            resp.headers['Link'] = f'<{new_url}>; rel="successor-version"'
            return resp
        wrapper.__name__ = f'dep_{view_fn.__name__}_{new_url.replace("/", "_")}'
        return wrapper

    from routes.api import device_daily_config, device_daily_zip, device_refresh, api_update

    # TODO: remove after next firmware OTA update lands on all frames
    app.add_url_rule('/daily-config',     'dep_daily_config',     _deprecated('/api/frame-config', device_daily_config))
    app.add_url_rule('/api/daily-config', 'dep_api_daily_config', _deprecated('/api/frame-config', device_daily_config))
    app.add_url_rule('/daily-zip',        'dep_daily_zip',        _deprecated('/api/image-zip',    device_daily_zip))
    app.add_url_rule('/api/daily-zip',    'dep_api_daily_zip',    _deprecated('/api/image-zip',    device_daily_zip))
    app.add_url_rule('/refresh',          'dep_refresh',          _deprecated('/api/refresh',      device_refresh),  methods=['POST'])
    app.add_url_rule('/update',           'dep_update',           _deprecated('/api/update',       api_update))

    return app


def start_mdns_broadcast(port=8000):
    try:
        zeroconf = Zeroconf(ip_version=IPVersion.V4Only)
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(('10.255.255.255', 1)); local_ip = s.getsockname()[0]
        except Exception:
            local_ip = '127.0.0.1'
        finally:
            s.close()

        info = ServiceInfo(
            "_picframes._tcp.local.",
            "PicFrames Server._picframes._tcp.local.",
            addresses=[socket.inet_aton(local_ip)],
            port=port,
            properties={"path": "/"},
        )
        zeroconf.register_service(info)
        logger.info(f"Broadcasting mDNS service at {local_ip}:{port}")
        return zeroconf
    except Exception as e:
        logger.error(f"Failed to start mDNS broadcast: {e}")
        return None


app = create_app()

try:
    init_db()
except Exception as e:
    logger.error(f"init_db failed: {e}")

try:
    from image import wipe_all_old_named_bins
    wipe_all_old_named_bins()
except Exception as e:
    logger.error(f"wipe_all_old_named_bins failed: {e}")

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8000))
    zc   = start_mdns_broadcast(port)
    try:
        app.run(host='0.0.0.0', port=port, debug=False)
    finally:
        if zc: zc.close()

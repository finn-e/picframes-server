import logging
import os
import socket

from flask import Flask, redirect, request, session, url_for
from zeroconf import IPVersion, Zeroconf, ServiceInfo

from db import init_db

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

SERVER_VERSION = "0.9.0"
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', 'admin')


def create_app():
    app = Flask(__name__, template_folder='templates')
    app.secret_key           = os.environ.get('SECRET_KEY', 'picframes_secret_session_key_12345')
    app.config['ADMIN_PASSWORD'] = ADMIN_PASSWORD
    app.config['SERVER_VERSION'] = SERVER_VERSION

    # Auth gate — runs before every request
    @app.before_request
    def auth_gate():
        path = request.path
        open_paths = {'/login', '/device_orientation', '/daily-config', '/daily-zip', '/refresh', '/update'}
        if (path.startswith('/api/')
                or path.startswith('/static/')
                or path in open_paths):
            return None
        if not session.get('authenticated'):
            return redirect(url_for('ui.login'))

    from routes.ui    import ui_bp
    from routes.api   import api_bp
    from routes.admin import admin_bp

    app.register_blueprint(ui_bp)
    app.register_blueprint(api_bp)
    app.register_blueprint(admin_bp)

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

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8000))
    zc   = start_mdns_broadcast(port)
    try:
        app.run(host='0.0.0.0', port=port, debug=False)
    finally:
        if zc: zc.close()

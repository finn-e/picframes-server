import os
import logging

logger = logging.getLogger(__name__)

REDIS_URL = os.environ.get('REDIS_URL', 'redis://localhost:6379/0')

try:
    from celery import Celery
    celery_app = Celery('picframes', broker=REDIS_URL, backend=REDIS_URL)
    celery_app.conf.update(
        task_serializer='json',
        result_expires=3600,
        worker_prefetch_multiplier=1,
    )
    _CELERY_AVAILABLE = True
except ImportError:
    celery_app = None
    _CELERY_AVAILABLE = False
    logger.warning("Celery not available — tasks will run synchronously")


def _make_task(fn):
    """Wrap a function as a Celery task if Celery is available, else a no-op stub."""
    if _CELERY_AVAILABLE and celery_app is not None:
        return celery_app.task(bind=True, name=f'tasks.{fn.__name__}')(fn)
    return fn


if _CELERY_AVAILABLE and celery_app is not None:
    @celery_app.task(bind=True, name='tasks.dither_entry')
    def dither_entry(self, entry_id, W, H):
        """Dither a playlist entry for the given resolution. Writes 4 bins to /share/images/."""
        try:
            from image.artifacts import convert_entry
            convert_entry(entry_id, W, H)
            return {'ok': True, 'entry_id': entry_id, 'resolution': f'{W}x{H}'}
        except Exception as exc:
            raise self.retry(exc=exc, countdown=30, max_retries=2)

    @celery_app.task(bind=True, name='tasks.build_device_zip')
    def build_device_zip(self, mac):
        """Build and cache the per-device zip for a given MAC address."""
        try:
            from image.zip import serve_cached_or_build
            from db import (get_db, get_device_playlist_id, get_device_active_entries,
                            get_playlist_settings, load_enabled, screen_size_for_profile)
            conn = get_db()
            row = conn.execute("SELECT * FROM devices WHERE mac=?", (mac,)).fetchone()
            conn.close()
            if not row:
                return {'ok': False, 'reason': 'device not found'}

            orientation  = row['orientation'] or 'landscape'
            hw_profile   = row['hw_profile'] or ''
            flip         = row['flip_l'] if orientation == 'landscape' else row['flip_p']
            orient_char  = 'l' if orientation == 'landscape' else 'p'
            flip_char    = 'f' if flip else 'u'
            scr_w, scr_h = screen_size_for_profile(hw_profile)

            pid = get_device_playlist_id(mac)
            if pid is None:
                return {'ok': False, 'reason': 'no playlist'}

            pl_settings = get_playlist_settings(pid)
            entries     = get_device_active_entries(mac, orientation)

            serializable_cfg = {
                "timer":             pl_settings['sleep_interval'],
                "shuffle":           pl_settings['shuffle'],
                "sync_images":       pl_settings['sync'],
                "enabled":           {str(k): dict(v) for k, v in load_enabled().items()},
                "landscape_flipped": bool(row['flip_l']),
                "portrait_flipped":  bool(row['flip_p']),
            }

            serve_cached_or_build(mac, entries, orient_char, flip_char, scr_w, scr_h, serializable_cfg)
            return {'ok': True, 'mac': mac}
        except Exception as exc:
            raise self.retry(exc=exc, countdown=30, max_retries=2)

else:
    def dither_entry(entry_id, W, H):
        from image.artifacts import convert_entry
        convert_entry(entry_id, W, H)

    def build_device_zip(mac):
        pass


def enqueue_entry_dither(entry_id, playlist_id):
    """Enqueue dither tasks for all resolutions needed by the playlist.
    Falls back to synchronous execution if Celery/Redis is unavailable."""
    try:
        from image import get_screen_types_for_playlist
        sizes = get_screen_types_for_playlist(playlist_id)
        for (W, H) in sizes:
            if _CELERY_AVAILABLE and celery_app is not None:
                try:
                    dither_entry.delay(entry_id, W, H)
                except Exception as e:
                    logger.warning(f"enqueue_entry_dither: Redis unavailable, running sync: {e}")
                    from image.artifacts import convert_entry
                    convert_entry(entry_id, W, H)
            else:
                from image.artifacts import convert_entry
                convert_entry(entry_id, W, H)
    except Exception as e:
        logger.error(f"enqueue_entry_dither({entry_id}, {playlist_id}): {e}")

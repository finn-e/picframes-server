# Migration idempotency: init_db twice, legacy playlist_images → playlist_entries once.
import os

from PIL import Image

import db


def test_init_db_twice_is_idempotent():
    db.init_db()  # fresh_db already ran it once; run again
    db.init_db()
    conn = db.get_db()
    n_users = conn.execute("SELECT COUNT(*) FROM users WHERE username='admin'").fetchone()[0]
    n_default = conn.execute("SELECT COUNT(*) FROM playlists WHERE name='Default'").fetchone()[0]
    conn.close()
    assert n_users == 1
    assert n_default == 1  # _migrate_playlists guarded by playlists_migrated key


def _seed_legacy_rows(base='legacyimg'):
    """Create an original file + legacy playlist_images row, and clear the
    entries-migration guard so init_db re-runs _migrate_entries."""
    Image.new('RGB', (100, 70), (10, 120, 200)).save(
        os.path.join(db.ORIGINALS_DIR, base + '.jpg'), format='JPEG')
    conn = db.get_db()
    conn.execute("INSERT INTO playlists (name, owner_id) VALUES ('LegacyPL', 1)")
    pid = conn.execute("SELECT id FROM playlists WHERE name='LegacyPL'").fetchone()['id']
    conn.execute("INSERT INTO playlist_images VALUES (?,?,0)", (pid, base))
    conn.execute("INSERT OR REPLACE INTO image_order (base, sort_order, owner_id) VALUES (?,0,1)", (base,))
    conn.execute("DELETE FROM global_settings WHERE key='entries_migration_done'")
    conn.commit(); conn.close()
    return pid


def test_legacy_playlist_images_migrate_to_entries_once():
    pid = _seed_legacy_rows()
    db.init_db()  # runs _migrate_entries

    entries = db.get_playlist_entries(pid)
    assert len(entries) == 1
    entry = entries[0]
    assert entry['title'] == 'legacyimg'
    img = db.get_image_by_uuid(entry['image_uuid'])
    assert img is not None
    assert img['original_filename'] == 'legacyimg.jpg'

    # Re-running the migration (guard removed again) must not duplicate entries
    conn = db.get_db()
    conn.execute("DELETE FROM global_settings WHERE key='entries_migration_done'")
    conn.commit(); conn.close()
    db.init_db()
    assert len(db.get_playlist_entries(pid)) == 1

    # And with the guard in place it is a no-op too
    db.init_db()
    assert len(db.get_playlist_entries(pid)) == 1


def test_migration_sets_guard_key():
    assert db.get_global_setting('entries_migration_done') == '1'
    assert db.get_global_setting('playlists_migrated') == '1'

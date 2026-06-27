-- Users
CREATE TABLE IF NOT EXISTS users (
  id           TEXT PRIMARY KEY,
  username     TEXT UNIQUE NOT NULL,
  password_hash TEXT NOT NULL,
  is_admin     INTEGER DEFAULT 0,
  created_at   INTEGER NOT NULL
);

-- Frames/devices
CREATE TABLE IF NOT EXISTS frames (
  id                 TEXT PRIMARY KEY,
  user_id            TEXT NOT NULL,
  mac                TEXT UNIQUE NOT NULL,
  name               TEXT,
  orientation        TEXT DEFAULT 'landscape',
  sleep_interval     INTEGER DEFAULT 900,
  landscape_flipped  INTEGER DEFAULT 0,
  portrait_flipped   INTEGER DEFAULT 0,
  image_index        INTEGER DEFAULT 0,
  queued_image       TEXT,
  daily_zip_version  TEXT DEFAULT '0',
  update_version     TEXT DEFAULT '',
  created_at         INTEGER NOT NULL,
  FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

-- Device auth tokens
CREATE TABLE IF NOT EXISTS tokens (
  token      TEXT PRIMARY KEY,
  frame_id   TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  FOREIGN KEY (frame_id) REFERENCES frames(id) ON DELETE CASCADE
);

-- Images metadata
CREATE TABLE IF NOT EXISTS images (
  id          TEXT PRIMARY KEY,
  user_id     TEXT NOT NULL,
  basename    TEXT NOT NULL,
  filename    TEXT NOT NULL,
  size_bytes  INTEGER NOT NULL,
  crop_l      REAL DEFAULT 0.5,
  crop_p      REAL DEFAULT 0.5,
  flip_h      INTEGER DEFAULT 0,
  flip_v      INTEGER DEFAULT 0,
  rendered    INTEGER DEFAULT 0,
  created_at  INTEGER NOT NULL,
  FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

-- Per-frame playlist ordering
CREATE TABLE IF NOT EXISTS frame_images (
  frame_id   TEXT NOT NULL,
  image_id   TEXT NOT NULL,
  sort_order INTEGER NOT NULL DEFAULT 0,
  enabled    INTEGER DEFAULT 1,
  PRIMARY KEY (frame_id, image_id),
  FOREIGN KEY (frame_id) REFERENCES frames(id) ON DELETE CASCADE,
  FOREIGN KEY (image_id) REFERENCES images(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_frames_mac ON frames(mac);
CREATE INDEX IF NOT EXISTS idx_tokens_frame ON tokens(frame_id);
CREATE INDEX IF NOT EXISTS idx_images_user ON images(user_id);
CREATE INDEX IF NOT EXISTS idx_frame_images ON frame_images(frame_id, sort_order);

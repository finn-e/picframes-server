export interface Env {
  DB: D1Database;
  BUCKET: R2Bucket;
  ADMIN_USERNAME: string;
  ADMIN_PASSWORD_HASH?: string;
}

export interface Frame {
  id: string;
  user_id: string;
  mac: string;
  name: string | null;
  orientation: string;
  sleep_interval: number;
  landscape_flipped: number;
  portrait_flipped: number;
  image_index: number;
  queued_image: string | null;
  daily_zip_version: string;
  update_version: string;
}

export interface Image {
  id: string;
  user_id: string;
  basename: string;
  filename: string;
  size_bytes: number;
  crop_l: number;
  crop_p: number;
  flip_h: number;
  flip_v: number;
  rendered: number;
}

export interface User {
  id: string;
  username: string;
  password_hash: string;
  is_admin: number;
}

# picframes-server

Open-source backend for the **PicFrames** e-ink photo frame ecosystem.

Flask/Python server with a multi-user web dashboard for managing images,
playlists, and devices. Live instance: https://picframes.treee.house.

## Running Locally

```bash
pip install -r requirements.txt
python app.py
```

Or with Docker:

```bash
docker-compose up
```

Environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `ADMIN_PASSWORD` | `admin` | Bootstrap password for the initial admin user |
| `SECRET_KEY` | (insecure default) | Flask session secret **and** the key device tokens are derived from — set it in production and don't rotate it casually (rotating invalidates every device token) |
| `SHARE_DIR` | `/share` | Image storage root |
| `CONFIG_DIR` | `/config` | SQLite DB location |
| `GITHUB_TOKEN` | — | Optional; increases GitHub API rate limit for firmware update checks |

## Deployment

Push to `trunk` → GitHub Actions builds the Docker image and pushes it to GHCR
→ [keel](https://keel.sh) auto-pulls it into the Kubernetes cluster
(`k8s.yaml`). Versioning is automated from Conventional Commits via
`paulhatch/semantic-version`. (fly.io deployment is retired; `fly.toml` is
historical.)

## Architecture

- `app.py` — entry point, app factory, mDNS broadcast (`_picframes._tcp.local.`)
- `db.py` — SQLite persistence (`$CONFIG_DIR/picframes.db`); tables include
  `users`, `devices` (mac PK, `owner_id` → users), `playlists`,
  `playlist_images`, `device_playlists`, `global_settings`
- `routes/api.py` — device-facing API
- `routes/ui.py`, `routes/admin.py`, `templates/` — web dashboard
- `image.py`, `converters/` — uploads stored as originals, converted to
  6-color Floyd-Steinberg dithered assets (800×480 landscape / 480×800
  portrait) and packed 4bpp `.bin` bitstreams for device download

## Device Registration & Auth

Devices never auto-register. The flow:

1. Device POSTs `/api/register` with `{mac, username, password}` where
   username/password are a real dashboard user's credentials (or `password` is
   an already-issued device token, for re-registration — owner is kept).
2. Server responds with the device token: `HMAC-SHA256(SECRET_KEY, mac)[:32]`.
   The device is created under that user's `owner_id` and auto-assigned to the
   default playlist.
3. Every subsequent device call (`/api/daily-config`, `/api/refresh`,
   `/api/daily-zip`, `/api/change-orientation`) must carry `X-Device-Mac` and
   `X-Device-Token` headers. The token is verified statelessly (HMAC recompute)
   and the device must exist in the DB; otherwise 403.

`/api/update` (firmware update check) is intentionally tokenless — the firmware
also uses it as a bare reachability probe.

## Development and Releases

Follow [Conventional Commits](https://www.conventionalcommits.org/en/v1.0.0/#specification):
* **Fixes:** `fix: resolve dither rendering offset`
* **Features:** `feat: support debug overlay display toggles`

## License

MIT — see [LICENSE](LICENSE)

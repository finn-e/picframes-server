# picframes-server

Open-source backend for the **PicFrames** e-ink photo frame ecosystem.

Flask/Python server with a web dashboard for managing images and devices. Deployed to [fly.io](https://fly.io) and published to GHCR for local or Kubernetes use.

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
| `ADMIN_PASSWORD` | `admin` | Web dashboard password |
| `SECRET_KEY` | (insecure default) | Flask session secret — set this in production |
| `SHARE_DIR` | `/share` | Image storage root |
| `CONFIG_DIR` | `/config` | SQLite DB location |
| `GITHUB_TOKEN` | — | Optional; increases GitHub API rate limit for firmware update checks |

## Deployment

**fly.io:**
```bash
fly deploy
```

**Kubernetes:** apply `k8s.yaml` after pulling the image from GHCR.

## Architecture

Single-file Flask app (`app.py`). Images are uploaded as originals then converted to 6-color Floyd-Steinberg dithered BMPs (800×480 landscape, 480×800 portrait) and packed 4bpp `.bin` bitstreams for device download. State is stored in SQLite.

Devices poll `/api/checkin`, `/api/ready`, and `/api/ack` to coordinate a three-phase slideshow sync (`GATHERING → READY → CHANGE`). The server broadcasts its presence via mDNS (`_picframes._tcp.local.`).

## Development and Releases

When contributing, please follow the [Conventional Commits](https://www.conventionalcommits.org/en/v1.0.0/#specification) specification for commit messages:
* **Fixes:** `fix: resolve dither rendering offset`
* **Features:** `feat: support debug overlay display toggles`

## License

MIT — see [LICENSE](LICENSE)

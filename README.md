# picframes-server

Open-source backend for the **PicFrames** e-ink photo frame ecosystem.

Provides two deployment targets:
1. **Cloudflare Workers** (primary) — `worker/`
2. **Flask/Python** (legacy, local dev) — `app.py`

## Cloudflare Workers Architecture

```
worker/
├── wrangler.toml          # Worker config & D1/R2 bindings
├── schema.sql             # D1 database schema
├── src/
│   ├── index.ts           # Main router
│   ├── types.ts           # TypeScript interfaces
│   ├── auth.ts            # Token gen + password hashing
│   └── routes/
│       ├── register.ts         # POST /register
│       ├── update.ts           # GET /update
│       ├── daily-config.ts     # GET /daily-config
│       ├── daily-zip.ts        # GET /daily-zip
│       ├── refresh.ts          # POST /refresh
│       ├── change-orientation.ts # POST /change-orientation
│       └── admin.ts            # /admin/* management
```

## Initial Setup

```bash
cd worker
npm install

# Create D1 database
npx wrangler d1 create picframes
# Paste the database_id into wrangler.toml

# Apply schema
npx wrangler d1 execute picframes --file=schema.sql

# Create R2 buckets
npx wrangler r2 bucket create picframes-originals
npx wrangler r2 bucket create picframes-processed
npx wrangler r2 bucket create picframes-zips

# Bootstrap admin account (run once)
curl -X POST https://YOUR_WORKER.workers.dev/admin/bootstrap \
  -H 'Content-Type: application/json' \
  -d '{"username": "admin", "password": "YOUR_PASSWORD"}'

# Deploy
npm run deploy

# Local dev
npm run dev
```

## API Reference

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `POST` | `/register` | username+password (JSON body) | Register device; returns Base62 token |
| `GET` | `/update` | MAC+Token headers | Check for firmware updates |
| `GET` | `/daily-config` | MAC+Token headers | Get runtime config + playlist |
| `GET` | `/daily-zip` | MAC+Token headers | Download asset ZIP |
| `POST` | `/refresh` | MAC+Token headers | Advance image index |
| `POST` | `/change-orientation` | MAC+Token headers | Sync orientation |
| `GET` | `/admin/users` | Basic Auth | List users |
| `POST` | `/admin/users` | Basic Auth | Create user |
| `DELETE` | `/admin/users/:id` | Basic Auth | Delete user |
| `POST` | `/admin/users/:id/password` | Basic Auth | Reset password |
| `GET` | `/admin/frames` | Basic Auth | List all frames |
| `DELETE` | `/admin/frames/:id` | Basic Auth | Delete frame |

## Device Authentication

All device endpoints require:
```
X-Device-Mac: aa:bb:cc:dd:ee:ff
X-Device-Token: <Base62 token from /register>
```

## Storage Layout (R2)

| Bucket | Key Pattern | Contents |
|--------|-------------|----------|
| `picframes-originals` | `<user_id>/<image_id>.<ext>` | Raw uploaded images |
| `picframes-processed` | `<frame_id>/<basename>_l.bin` | Landscape 4bpp assets |
| `picframes-processed` | `<frame_id>/<basename>_p.bin` | Portrait 4bpp assets |
| `picframes-zips` | `<frame_id>/daily.zip` | Per-frame asset bundle |

## Development and Releases

When contributing, please follow the [Conventional Commits](https://www.conventionalcommits.org/en/v1.0.0/#specification) specification for commit messages:
* **Fixes:** `fix: resolve dither rendering offset`
* **Features:** `feat: support debug overlay display toggles`

## License

MIT — see [LICENSE](LICENSE)

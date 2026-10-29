# Clippie cloud control plane

A small always-on service for the one job a laptop cannot do: keep collecting metrics for published clips while it sleeps. The local app pulls the results with `POST /api/publish/cloud/sync`. This service never receives video, only YouTube refresh tokens (encrypted) and video ids.

YouTube already publishes scheduled videos by itself (`privacyStatus=private` plus `publishAt`), so uploads need no cloud help.

## What it stores

| Data | Where | Protection |
|---|---|---|
| Refresh token, OAuth client id and secret, per channel | `accounts.sealed` | Fernet, key from `CLOUD_FERNET_KEY` (not in the database) |
| Device bearer token | `devices.token_hash` | SHA-256 only; the token is shown once at registration |
| Tracked posts (video id, publish time, poll stage) | `posts` | none needed |
| Metric snapshots and retention curves | `snapshots` | none needed |

## Run it

1. Generate the encryption key and an admin key:

   ```
   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
   python -c "import secrets; print(secrets.token_urlsafe(32))"
   ```

2. Build and run (any host that gives you HTTPS and a persistent volume: Fly.io, Railway, a small VPS behind Caddy):

   ```
   docker build -t clippie-cloud cloud/
   docker run -d --name clippie-cloud -p 8080:8080 -v clippie-cloud-data:/data \
     -e CLOUD_FERNET_KEY=... -e CLOUD_ADMIN_KEY=... -e CLOUD_REQUIRE_HTTPS=1 clippie-cloud
   ```

3. Register your laptop once:

   ```
   curl -X POST https://YOUR-HOST/devices -H "Authorization: Bearer $CLOUD_ADMIN_KEY" \
     -H "Content-Type: application/json" -d '{"name": "my laptop"}'
   ```

   The reply holds `token`. Paste the host URL and this token into Channels, Cloud tracking in Clippie, then press Sync now.

## Environment

| Variable | Default | Meaning |
|---|---|---|
| `CLOUD_FERNET_KEY` | required | Fernet key for tokens at rest. Losing it means channels must be reconnected. |
| `CLOUD_ADMIN_KEY` | required to register devices | Bearer key for `POST /devices` |
| `CLOUD_DB` | `cloud.sqlite3` (`/data/cloud.sqlite3` in the image) | SQLite path |
| `CLOUD_POLL_SECONDS` | `60` | How often the poller looks for due posts |
| `CLOUD_REQUIRE_HTTPS` | off | Reject requests whose `X-Forwarded-Proto` is not `https` |

TLS terminates at your platform's proxy. The service trusts `X-Forwarded-Proto` only to refuse plain HTTP.

## API

All device routes need `Authorization: Bearer <device token>`.

- `PUT /accounts/{channel_id}` `{platform: "youtube", refresh_token, client_id, client_secret}`
- `DELETE /accounts/{channel_id}` removes the token and stops tracking its posts
- `PUT /posts/{post_id}` `{channel_id, platform, external_id, published_at, scheduled_at, created_at}`
- `DELETE /posts/{post_id}`
- `GET /snapshots?since=<stored_at>` returns `{snapshots: [{post_id, fetched_at, stored_at, metrics, retention}]}` in `stored_at` order; pass the last `stored_at` back as `since`
- `GET /health`

Polls run 1 h, 6 h, 24 h, 72 h, 7 d and 28 d after publication, with exponential backoff (15 min doubling to 6 h) when Google refuses a call.

## Develop

```
pip install -r cloud/requirements.txt pytest
PYTHONPATH=cloud pytest cloud/tests
```

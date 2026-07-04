# NotifyByWebApp

A very lightweight **Progressive Web App (PWA)** for receiving push notifications on modern
Android and iOS smartphones — no app store, no user accounts.

## The idea

1. **Anyone can create a message channel.** Creating a channel produces a long, secure,
   non-guessable channel code (also shown as a QR code).
2. **Subscribers** open the start page, add one or more channel codes, and get a link that
   **contains all chosen codes — this link is the app**: opened on the phone and added to
   the Home Screen, the installed PWA is pre-configured for exactly those channels. From
   then on they receive channel messages as native push notifications (Android, and
   iOS 16.4+ where the app must be installed to the Home Screen).
3. **Senders** — anyone who has the channel code — send messages to all subscribers,
   through the app itself or through a plain HTTP API from any script or service.

The code is the capability: possession of a channel code is all that's needed to subscribe
and to send. No accounts, no logins, no personal data beyond the technical push
subscription. The server never stores raw channel codes (only their SHA-256 hashes) and
codes never appear in URLs sent to the server.

## Using the API

Everything is JSON over POST; the channel code goes in the body (never in URLs):

```bash
# create a channel (returns the secret code — save it, it cannot be recovered)
curl -X POST https://YOUR-DEPLOYMENT/api/channel \
  -H "Content-Type: application/json" \
  -d '{"name":"Build alerts"}'

# send a message to all subscribers (this is the third-party integration endpoint)
curl -X POST https://YOUR-DEPLOYMENT/api/message \
  -H "Content-Type: application/json" \
  -d '{"code":"YOUR_CHANNEL_CODE","title":"Build failed","body":"main is red","url":"https://ci.example.com/run/42"}'

# read recent messages / channel info
curl -X POST https://YOUR-DEPLOYMENT/api/messages \
  -H "Content-Type: application/json" \
  -d '{"code":"YOUR_CHANNEL_CODE"}'
```

Limits: title ≤ 120 chars, body ≤ 2000, url optional http(s) ≤ 500 chars, rate limit
120 requests/min/IP, ≤ 200 subscribed devices per channel, last 50 messages kept.
Subscribing/unsubscribing (`/api/subscribe`, `/api/unsubscribe`) is normally done by the
app itself.

## Deploy your own

1. Fork/import this repo into [Vercel](https://vercel.com) (zero config — one Python
   serverless function serves everything).
2. Add an **Upstash for Redis** integration from the Vercel Marketplace (sets
   `KV_REST_API_URL`/`KV_REST_API_TOKEN` automatically). Without it, an in-memory store
   is used — fine for trying it out, but data is lost whenever the instance recycles.
3. Generate Web Push keys locally and add them as Vercel environment variables:
   ```bash
   python scripts/generate_vapid_keys.py
   # → VAPID_PRIVATE_KEY, VAPID_PUBLIC_KEY, and set VAPID_SUBJECT=mailto:you@your.domain
   ```

No personal data and no secrets are ever committed to this repository; runtime secrets
live only in local `.env` files (gitignored) and Vercel environment variables.

## Development

```bash
python -m venv .venv && . .venv/Scripts/activate   # .venv/bin/activate on Linux/macOS
pip install -r requirements-dev.txt
python -m playwright install chromium              # once, for the browser UI tests
python -m pytest                                   # 75 tests, fully offline
```

Tech: one Python serverless function (`api/index.py`) serving the API, both web pages,
the service worker and all assets; Web Push via VAPID (`pywebpush`); Upstash Redis over
its REST API; vendored MIT QR library (no CDN). See `CLAUDE.md` for the full design notes.

## Disclaimer

This is a hobby project without any warranty. It may stop working at any time and there
is no guarantee of support. Use at your own risk.

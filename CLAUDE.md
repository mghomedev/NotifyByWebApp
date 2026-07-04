# CLAUDE.md — NotifyByWebApp

Very lightweight PWA ("Notify by Web Application"): users install it to the Home Screen of
modern Android/iOS phones and receive **push notifications** for **message channels** they
subscribed to. Channels are created by anyone; a channel is identified by a **long, secure,
non-guessable key** (shareable as QR code). Anyone holding the key can **send** messages to
the channel's subscribers — via a small web interface or via a plain **HTTP API** usable
from any third-party service. No accounts, no logins.

**Disclaimer:** Hobby project, no warranty, may stop working anytime. Public repo on GitHub.

## Core model (keep this simple)

- **Channel** = random ID + capability key(s). The key IS the authorization (bearer
  capability). ≥128 bit randomness, URL-safe, generated server-side, non-enumerable.
- Possession of the channel key ⇒ may subscribe AND send. (Open design option, decide at
  implementation: separate *send* key vs. cheaper *subscribe-only* key. Default per the
  project goal: one key does both.)
- **Subscriber** = a Web Push subscription (endpoint + browser keys) attached to a channel.
- **Message** = title/body (+ optional URL), stored per channel for the "recent messages"
  view, pushed to all subscribers on send.
- No personal data. Never log channel keys or push endpoints. Consider storing only a
  hash of the channel key server-side so a DB leak doesn't leak send capability.

## Install-URL trick (core UX idea — the app URL carries the channel codes)

The URL used to install the Home Screen app **contains the channel code(s)** the user
wants to subscribe to. Flow:

1. A user-friendly page lets the user select/enter one or more channel codes (or scan
   their QR codes).
2. The page **generates an app URL containing all chosen codes** (e.g.
   `/a#codes=KEY1,KEY2`) and shows it with instructions (+ QR of that URL).
3. The user opens that URL on the phone and adds it to the Home Screen → the installed
   PWA knows its channels from its own URL, no post-install setup or local account.

Implementation facts:
- Put the codes in the **URL fragment (`#`)**, not the query string: fragments are never
  sent to the server → keys stay out of server/CDN logs; page JS reads them client-side.
- The **manifest must preserve the codes**: serve `manifest.webmanifest` dynamically (or
  reference it with the codes, e.g. `<link rel="manifest" href="/manifest?...">`) so its
  `start_url` is the app URL *including* the codes — a static fixed `start_url` would make
  every install open the same code-less page. Verify behavior on both iOS Safari
  (Add to Home Screen) and Android Chrome at implementation time.
- The URL codes are the *initial* channel set. The push subscription itself is one
  endpoint per install (per origin + service worker); the server maps channel → endpoints.
  The installed app can still add/remove channels later without changing its URL.
- Multiple codes in one URL ⇒ one installed app, several channels.

## Tech stack

- **Vercel** Hobby tier, single app, public GitHub repo.
- **Python 3.12 serverless functions**: one entrypoint `api/index.py`
  (`BaseHTTPRequestHandler`), `pyproject.toml` with `[tool.vercel]` entrypoint,
  `vercel.json` for rewrites/headers. HTML UI served inline from the function.
- PWA static assets (must be real static files, correct MIME, root scope):
  `public/manifest.webmanifest`, `public/sw.js` (service worker), icons.
- **Web Push via VAPID** (`pywebpush`). Public VAPID key goes to the client; the
  **private key is a secret** → env var only.
- **Storage** (subscriptions/messages must persist across serverless instances):
  Vercel Marketplace store; default candidate
  **Upstash Redis** (free tier, KV/list model fits channels→subscriptions/messages).
  Decide and document when implementation starts.
- **Tests**: pytest (+ Playwright headless Chromium for UI flows), offline with mocks,
  green before every deploy.

## Verified platform facts (do NOT re-derive)

- **iOS**: Web Push works since iOS 16.4, ONLY for PWAs added to the Home Screen
  (`display: standalone` manifest), and permission must be requested from a user gesture.
- **Android** (Chrome et al.): Web Push works in browser and installed PWA; installability
  needs manifest + service worker over HTTPS.
- Push payloads are encrypted per subscription; sending needs the VAPID key pair.
  Expired/invalid subscriptions return 404/410 on send → prune them.

## Planned HTTP surface (sketch — refine at implementation)

- `POST /api/channel` → create channel, returns key (+ QR on the web UI)
- `POST /api/message` (key + title/body/url) → store + push to all subscribers; this is
  the third-party integration endpoint — keep it a dead-simple POST
- `GET /api/messages?key=…` → recent messages (also backs the channel web page)
- `POST /api/subscribe` (key + PushSubscription JSON) / `POST /api/unsubscribe`
- Web UI routes: `/` (create channel + **URL generator**: pick/enter codes → app URL with
  all codes + QR), `/a#codes=…` (the installable app page: subscribes, shows channels,
  send + message list per channel), served inline from the function

## Security & operations requirements

- **No secrets in the repo — ever** (public GitHub!). Secrets only in `.env`/`.env.local`
  (gitignored, `.env.example` documents names without values) and in Vercel env vars.
  Expected secrets: VAPID private key, storage credentials/URL.
- Validate inputs before any upstream/storage call; keys strictly format-checked.
- Rate-limit per IP (best effort per instance) + Vercel Firewall in production.
- Security headers on all responses (`nosniff`, `Referrer-Policy: no-referrer` — URLs may
  carry channel keys!, CSP on HTML pages). If CDN scripts are used (e.g. QR code lib):
  SRI hashes mandatory.
- Don't log request lines/query strings (keys!) — override
  `BaseHTTPRequestHandler.log_message` (its default writes every request line, including
  the query string, to the function logs).
- Message size limits and per-channel caps (subscribers, stored messages) to stay within
  Hobby-tier and free-storage limits.

## Commands (local, once implemented)

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # then fill in local dev secrets
python -m pytest      # must be green before every deploy
```

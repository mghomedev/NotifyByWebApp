# NotifyByWebApp

A very lightweight **Progressive Web App (PWA)** for receiving **push notifications** on
modern phones and desktops — no app store, no user accounts, no personal data.

Live demo: <https://notify-by-web-app.vercel.app/>

## The idea

1. **Anyone can create a message channel.** Creating one gives you a long, secure,
   non-guessable channel code, shown as text and as a **QR code / install link**.
2. **Subscribers** open that link (or scan the QR) on their phone and **Add it to the Home
   Screen** — the installed PWA is pre-configured for exactly those channels and receives
   messages as native push notifications (Android and other browsers; iOS/iPadOS 16.4+ when
   installed to the Home Screen).
3. **Senders** — anyone who has the channel code — send messages through the app's web form
   or a plain HTTP API from any script or service. A channel can optionally require a
   **send-password** so that only holders of the password (e.g. an event manager) can send,
   while anyone with the code can still receive.

The code is the capability: it's all that's needed to subscribe and send. No accounts, no
logins, no personal data beyond the technical push subscription. The server stores only the
**SHA-256 hash** of each code (never the raw code), and codes never appear in URLs sent to
the server.

## What you can do

- **Create & share channels** — get a QR code / install link; combine several channels into
  one installed app.
- **Receive push notifications** on Android, iPhone/iPad (iOS 16.4+, installed), Mac, and
  desktop browsers. The app lists the **supported devices & minimum versions** and warns you
  if your device is too old.
- **Live in-app updates** — with the app page open, new messages appear on their own (no
  reload) **even if OS notifications are off**. A new arrival pops an in-app *"New message
  in ‹channel›"* toast with **Go to channel / Reply / Delete** actions, and the message is
  highlighted.
- **Mute** any channel per-device (🔔/🔕).
- **Delete messages** — one at a time, all at once, or just the older ones (the newest 3
  stay; older ones fold into a "More…" expander).
- **Optional send-password** per channel (only holders of the password can send).
- Channels are **sorted by latest activity**, with local-time timestamps, newest on top.
- Your channels are **saved automatically** on your device (see below).

## Using the API

Everything is JSON over `POST`; the channel code goes in the body (never in a URL):

```bash
# create a channel (returns the secret code — save it, it cannot be recovered)
curl -X POST https://YOUR-DEPLOYMENT/api/channel \
  -H "Content-Type: application/json" \
  -d '{"name":"Build alerts"}'
# optional: {"name":"Event","send_password":"only-managers"} → sending needs the password

# send a message (this is the third-party integration endpoint)
curl -X POST https://YOUR-DEPLOYMENT/api/message \
  -H "Content-Type: application/json" \
  -d '{"code":"YOUR_CODE","title":"Build failed","body":"main is red","url":"https://ci/run/42"}'
# title OR body is required (title is optional; if omitted it is derived from the body).
# add "send_password":"…" if the channel is protected.

# read recent messages + channel info
curl -X POST https://YOUR-DEPLOYMENT/api/messages \
  -H "Content-Type: application/json" -d '{"code":"YOUR_CODE"}'

# delete one message by id, or clear all (add send_password if protected)
curl -X POST https://YOUR-DEPLOYMENT/api/message/delete \
  -H "Content-Type: application/json" -d '{"code":"YOUR_CODE","id":"MESSAGE_ID"}'
curl -X POST https://YOUR-DEPLOYMENT/api/messages/clear \
  -H "Content-Type: application/json" -d '{"code":"YOUR_CODE"}'   # or {"keep":3}
```

`/api/subscribe` and `/api/unsubscribe` (push subscription per device) are normally handled
by the app itself. Limits: title ≤ 120 chars, body ≤ 2000, optional http(s) url ≤ 500,
send-password 4–120 chars, rate limit 120 requests/min/IP, ≤ 200 subscribed devices per
channel, newest 50 messages kept, channels expire after 400 days of inactivity.

## Supported devices (minimum versions)

| Device | Minimum |
|---|---|
| iPhone | iOS **16.4**+ (2023) — must be added to the Home Screen |
| iPad | iPadOS **16.4**+ (2023) — must be added to the Home Screen |
| Mac | Safari **16.1**+ on macOS 13 Ventura+, or Chrome / Firefox / Edge |
| Android | Chrome / Firefox / Edge / Opera / Samsung Internet (Android **10**+ recommended) |
| Windows / Linux | Chrome **52**+, Firefox **44**+, Edge **17**+, Opera **42**+ |

On iPhone/iPad you must open the app from its **Home Screen icon** — push does not work in a
Safari browser tab. The app detects an unsupported/too-old device and shows a clear warning.

## Your saved channels stay on your device

Your channels are **saved automatically** on the start page (no opt-in) — stored in this
browser's **local storage and a cookie**, re-saved and self-healed on every visit, so you
won't lose them. A confirmation ("✅ Saved on this device") is shown. They are never sent
anywhere for safekeeping. To stop and clear them (e.g. on a shared computer) use **Forget &
stop saving**; **Save my channels here** turns saving back on, and a channel's **Remove**
button drops just that one. Browsers can still clear local storage on their own (Safari, for
example, limits script-set cookies to ~7 days), so **your real backup is the channel code /
app-link QR** — keep those and you can always restore a channel.

## Deploy your own

1. Fork/import this repo into [Vercel](https://vercel.com) — zero config, one Python
   serverless function serves everything.
2. Add an **Upstash for Redis** integration from the Vercel Marketplace (auto-sets
   `KV_REST_API_URL`/`KV_REST_API_TOKEN`). Without it, an in-memory store is used — fine for
   trying it out, but data is lost when the instance recycles. Pick a region near your users.
3. Generate Web Push keys and set them as Vercel environment variables:
   ```bash
   python scripts/generate_vapid_keys.py
   # → VAPID_PRIVATE_KEY, VAPID_PUBLIC_KEY, and VAPID_SUBJECT=mailto:you@your.domain
   ```
4. Optional: set `NBW_STATUS_SECRET` to enable the secret-gated `GET /api/status`
   diagnostics endpoint (reports push-configured?, storage backend + reachability, deployed
   commit/region — no secrets). Query it with an `Authorization: Bearer <secret>` header.

No personal data and no secrets are ever committed to this repository; runtime secrets live
only in local `.env` files (gitignored) and Vercel environment variables.

## Development

```bash
python -m venv .venv && . .venv/Scripts/activate   # .venv/bin/activate on Linux/macOS
pip install -r requirements-dev.txt
python -m playwright install chromium              # once, for the browser UI tests
python -m pytest                                   # 176 tests, fully offline
```

Tests include real-crypto Web Push (a fake device decrypts the actual payload) and browser
tests. The notification-display tests run a **headed** browser (headless denies notification
permission) and auto-skip without a display — skip them with `-k "not notifications"`.

Tech: one Python serverless function (`api/index.py`) serving the JSON API, both web pages,
the service worker and all assets; Web Push via VAPID (`pywebpush`); Upstash Redis over its
REST API with an in-memory fallback; a vendored MIT QR library (no CDN). See `CLAUDE.md` for
the full design notes.

## Disclaimer

Free, open-source hobby project provided **"AS IS", without any warranty**. It may stop
working at any time and there is no guarantee of support. Message delivery is not guaranteed
— do not rely on it for urgent, critical, medical, or emergency notifications. **Use at your
own risk.**

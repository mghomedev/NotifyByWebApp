# CLAUDE.md — NotifyByWebApp

Very lightweight PWA ("Notify by Web Application"): users install it to the Home Screen of
modern Android/iOS phones and receive **push notifications** for **message channels** they
subscribed to. Channels are created by anyone; a channel is identified by a **long, secure,
non-guessable code** (shareable as QR code). Anyone holding the code can **send** messages to
the channel's subscribers — via a small web interface or via a plain **HTTP API** usable
from any third-party service. No accounts, no logins.

**Disclaimer:** Hobby project, no warranty, may stop working anytime. Public repo:
https://github.com/mghomedev/NotifyByWebApp

## Core model

- **Channel** = secret code (bearer capability), `secrets.token_urlsafe(24)` → 32 chars,
  192 bits. Format-checked everywhere: `^[A-Za-z0-9_-]{16,64}$`.
- The server stores only `sha256(code)` ("kh") — never the raw code → a storage leak does
  not leak send capability. Codes travel only in POST bodies (never URLs → never in
  platform request logs) and in the app URL **fragment** (never sent to servers).
- Possession of the code ⇒ may subscribe AND send (deliberate; a separate subscribe-only
  key remains a possible future option).
- **Subscriber** = Web Push subscription (endpoint + p256dh/auth keys), stored per channel
  keyed by `sha256(endpoint)`. One push subscription per install; server maps
  channel → endpoints.
- **Message** = optional title (≤120) + optional body (≤2000) + optional http(s) url
  (≤500). At least one of title/body is required; a missing title is derived from the
  body's first line (first `TITLE_SNIPPET`=60 chars + "…"). Stored per channel (newest
  first, capped at `NBW_MAX_MESSAGES`=50) and pushed to all subscribers.
- **Send-password (optional)**: a channel may set a send-password at creation; only its
  `sha256` hash is stored in the channel meta (`send_pw`) — never the raw phrase. When set,
  publishing requires the matching password (constant-time compare → `SendForbidden`/403
  otherwise); subscribing and receiving stay open to anyone holding the code. Channel
  snapshots expose `send_protected` so the send UIs show a password field only when needed.
  `/api/channel` accepts `send_password` (min 4, max 128); `/api/message` accepts it too.
- **Deleting messages**: `/api/message/delete` (one, by message `id`) and
  `/api/messages/clear` (all) — gated by the same send-password when the channel is
  protected (`_require_send_password`, shared with publish). The app page shows a trash
  icon per message and a clear-all trash in the message header; on a protected channel the
  UI reuses the send-password field or prompts for it.

## Install-URL trick (core UX idea — the app URL carries the channel codes)

The URL used to install the Home Screen app **contains the channel code(s)**:
`/a#codes=CODE1,CODE2`. The landing page `/` is the generator: create channels, combine
codes, get the app URL + QR. Implementation layers (all implemented in notify_pages.py):

1. Codes live in the **URL fragment** — never sent to any server.
2. `/a` injects a **data:-URI web app manifest** (client-side, absolute URLs, `start_url`
   includes the fragment, `id` = hash of the code set so different code sets are distinct
   installs). There is deliberately **no static manifest** — it would override the install
   URL with a code-less `start_url`. CSP includes `manifest-src 'self' data:`.
3. Codes are mirrored to **localStorage** (`nbw_codes`); removed channels are remembered
   in `nbw_removed` so an install-URL fragment does not resurrect them.
4. Final fallback (matters on iOS, where the installed app has separate storage): in-app
   “Add a channel” by pasting a code.

## Architecture (all UI served inline from one function — no static bundling risk)

- `api/index.py` — single Vercel entrypoint (`BaseHTTPRequestHandler` named `handler`;
  `pyproject.toml` `[tool.vercel] entrypoint = "api.index:handler"`).
  GET: `/` `/a` `/sw.js` `/vendor/qrcode.js` `/icon.svg` `/icon-192.png` `/icon-512.png`
  `/apple-touch-icon.png` `/favicon.ico` `/robots.txt` `/api/health` `/api/status`.
  POST (JSON, code in body): `/api/channel` `/api/subscribe` `/api/unsubscribe`
  `/api/message` `/api/message/delete` (by `id`) `/api/messages` `/api/messages/clear`.
  OPTIONS: CORS preflight for `/api/*`.
- `notify_core.py` — codes/hashing, validation, rate limiter, storage backends, push.
- `notify_pages.py` — landing + app HTML/JS, service worker, CSP, icon SVG, robots.
- `notify_icons.py` / `notify_vendor.py` — base64-embedded PNGs / vendored
  qrcode-generator 1.4.4 (MIT, served from `/vendor/qrcode.js`; no CDN → CSP stays
  `script-src 'self' 'unsafe-inline'`).
- `scripts/` — VAPID key generation, icon regeneration (`make_icons.ps1` + `embed_icons.py`).

## Storage (decided: Upstash Redis via REST; in-memory fallback)

- `RedisStorage`: Upstash REST `/pipeline` endpoint, Bearer token, JSON command arrays.
  Env: `KV_REST_API_URL`/`KV_REST_API_TOKEN` (Vercel Marketplace “Upstash for Redis”) or
  `UPSTASH_REDIS_REST_URL`/`UPSTASH_REDIS_REST_TOKEN`.
  Keys: `nbw:meta:{kh}` (JSON string, `SET NX EX`), `nbw:subs:{kh}` (hash eh→sub JSON),
  `nbw:msgs:{kh}` (list, `LPUSH`+`LTRIM`). All keys get TTL `NBW_CHANNEL_TTL_DAYS`=400,
  refreshed on activity → dead channels expire by themselves.
- `MemoryStorage`: used automatically when no Redis env is set (tests/local dev; on
  Vercel it would be per-instance only — configure Redis for real deployments).

## Web Push facts (verified against implementation; do NOT re-derive)

- VAPID keys as raw URL-safe base64 (32-byte private scalar / 65-byte uncompressed public
  point) — `scripts/generate_vapid_keys.py`. pywebpush accepts the raw private string;
  the public key is embedded into `/a` and converted client-side (urlB64ToU8).
- `webpush(...)` per subscriber in a ThreadPoolExecutor (≤8 workers), `ttl=86400`,
  timeout 10s, on a `requests.Session` with `max_redirects=0` (never follow a redirect
  off the push service); `WebPushException` 404/410 → subscription pruned, other statuses
  → `failed` (NOT pruned — a 5xx/401 is transient/misconfig, not a dead endpoint).
- **Payload is byte-bounded, not char-bounded**: services hard-cap the encrypted body at
  4096 bytes. `publish()` serializes the push JSON with `ensure_ascii=False` and truncates
  the pushed title/body/channel by UTF-8 bytes (`PUSH_*_MAX_BYTES`); the FULL text is still
  stored for the in-app message list. (A 2000-char CJK body would otherwise be ~12 KB and
  every push would 413 — regression-tested in `test_push_e2e`.)
- Without `VAPID_PRIVATE_KEY` the message is stored, push skipped,
  `push_disabled: true` returned (local dev works without keys).
- `publish()` stores the message first; a storage error AFTER the store (touch/get_subs) is
  swallowed (returns `push_error`) so a retry can’t duplicate the message.
- **iOS**: Web Push only for Home-Screen-installed PWAs since iOS 16.4; `Notification`/
  `PushManager` absent in the Safari tab → the app shows install instructions
  (`isIOS() && !isStandalone()`, enable button hidden); permission request must come from a
  user gesture (the “Enable notifications” button). `ensureSubscribed` reports ON only when
  the server accepted ≥1 channel (never a false ON on 404/409/502) — regression-tested.
- **VAPID key rotation**: the client compares `subscription.options.applicationServerKey`
  with the current key and re-subscribes on mismatch.
- SW (`/sw.js`, `Service-Worker-Allowed: /`, no-cache): push → `showNotification` inside
  `waitUntil`; `notificationclick` → prefer an existing `/a` client, else open, cross-origin
  message links open in a new window (don’t destroy the app tab); `pushsubscriptionchange`
  → re-subscribe + re-POST `/api/subscribe` for the codes mirrored into a Cache entry
  (`/__nbw_state`, SWs can’t read localStorage); tiny app-shell cache network-first.

## Security & operations requirements

- **No secrets in the repo — ever** (public GitHub!). Secrets: `VAPID_PRIVATE_KEY`
  (+public/subject), Redis URL/token — only in `.env` (gitignored) / Vercel env vars.
  `.env.example` documents names without values.
- All user content rendered with `textContent` (never innerHTML with untrusted data);
  message urls must be http(s) and are length-capped; control chars (C0/C1/DEL, via
  `unicodedata` category `Cc`) stripped server-side from titles/bodies/names; a
  control-char-only title is rejected (cleaned before the required-check).
- **SSRF guard**: `validate_subscription` rejects endpoints whose host is loopback,
  private, link-local, reserved/metadata (169.254.x), multicast or `localhost` — the push
  endpoint is a URL the server will POST to. (Combined with `max_redirects=0`. DNS-rebind
  to internal is a documented residual for a hobby app.) Tested in `test_core.py`.
- Security headers on every response: `nosniff`, `Referrer-Policy: no-referrer`,
  `Strict-Transport-Security` (HSTS, guards custom domains); HTML additionally gets the CSP
  from `notify_pages.CSP` (default-src 'none' + minimum grants; specific directives asserted
  in `test_csp_grants_exactly_what_the_pages_need`).
- `handler.log_message` is overridden to be silent — NEVER remove
  (test: `test_no_request_logging`). Don't log codes, endpoints, or request lines.
  Catch-all handlers use `_safe_error` (no second response after a failed write → no
  double-fault tracebacks); client-disconnect errors are swallowed.
- Client IP for rate limiting = `x-real-ip` (set by Vercel, not client-spoofable) then
  leftmost `x-forwarded-for` then socket. Rate limit per IP (`NBW_RATE_PER_MIN`=120/min,
  best effort per instance) on all `/api` POSTs → 429; plus a per-instance channel-creation
  soft cap (`NBW_MAX_CHANNELS_PER_MIN`=60) so header-spoofing can’t drive unbounded channel
  creation. Vercel Firewall is the hard backstop. The `RateLimiter` prunes on an interval
  and is deterministically size-bounded (no O(N) rebuild per request).
- Early error responses (413/429/404/400) **drain the request body** first so the socket
  isn’t reset with unread data (client would otherwise see a connection reset, not the
  error). Handler sets `timeout=20` so a short/slow body can’t park a thread forever.
- Caps: body ≤64KB (413), subscribers/channel `NBW_MAX_SUBS_PER_CHANNEL`=200 (409, enforced
  write-then-verify so concurrent subscribes can’t overshoot), messages kept 50.
  `Server:` header reveals no Python version.
- CORS `*` on `/api` (the code IS the auth; enables third-party browser senders); the
  hand-built 429 responses also carry CORS.
- `GET /api/status` = **secret-gated** diagnostics (`NBW_STATUS_SECRET` via
  `Authorization: Bearer <secret>` header or `?key=<secret>`, constant-time compare):
  reports push-configured?, storage backend + live ping reachability, deployed
  commit/env/region (from `VERCEL_*`), and config limits. Returns NO secret values.
  Fails closed: 404 when the secret env var is unset, 401 on a wrong/missing secret.
  Lets the deployment be health-checked black-box (`core.diagnostics()`).

## Tests (pytest; must be green before every deploy) — 124 tests

- `tests/test_core.py` — unit: codes, validation, SSRF host guard, control-char cleaning,
  limiter (deterministic clock + bounded size), config parsing, both storage backends
  (RedisStorage against captured command pipelines), UTF-8 truncation.
- `tests/test_api.py` — integration: the real handler on a local port, in-memory storage,
  monkeypatched `webpush` capture; happy path + error/cap/prune/rate-limit + channel-create
  soft cap + oversized-body drain + CSP directive + StorageError→502 + security headers +
  the no-logging guarantee.
- `tests/test_storage_http.py` — the real `RedisStorage` HTTP layer against a fake Upstash
  REST server (request shape, Bearer auth, `/pipeline`, per-command error, non-JSON, refused
  connection → `StorageError`).
- `tests/test_push_e2e.py` + `tests/pushkit.py` — **real-crypto Web Push**: a mock push
  service captures the actual `pywebpush` request; a `FakeDevice` (real P-256 keys)
  DECRYPTS the aes128gcm payload and verifies the VAPID JWT signature. Proves encryption,
  VAPID, fan-out, 410-pruning, 5xx=failed, and byte-bounding of long unicode.
- `tests/test_ui_playwright.py` — headless Chromium: landing flow, app page from fragment,
  manifest injection, localStorage persistence/removal, UI send.
- `tests/test_ui_notifications.py` + `tests/uikit.py` — **HEADED** Chromium (headless denies
  notification permission): delivers a real push into the SW via CDP
  `ServiceWorker.deliverPushMessage` and asserts the displayed notification’s
  title/body/tag/click-url; incl. a Pixel device-emulation run (Android = Chrome).
- `tests/test_ui_platforms.py` — iOS Safari-tab emulation (Push APIs stripped → install
  banner, enable hidden), iOS installed-standalone emulation (fake push stack → subscribe
  registers on the server, UI ON), and the false-ON regression guard (server 404 → error
  state, not ON).
- Playwright suites auto-skip without Chromium/display: `python -m playwright install
  chromium`. The headed notification tests open a browser window; skip with
  `-k "not (notifications)"` if running unattended.
- Local python is 3.10, Vercel 3.12 — keep code compatible with both.

## Commands (local)

```bash
python -m venv .venv && . .venv/Scripts/activate   # Windows (.venv/bin/activate on Unix)
pip install -r requirements-dev.txt
python -m playwright install chromium              # once, for UI tests
python scripts/generate_vapid_keys.py              # → paste into .env
python -m pytest                                   # green before every deploy
python -m http.server ...                          # NO — run via tests; for manual dev use vercel dev
```

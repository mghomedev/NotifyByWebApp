"""Single Vercel entrypoint for NotifyByWebApp.

Serves the web UI, PWA assets and the JSON API. All API calls are POST
with the channel code in the body — codes never appear in URLs, so they
cannot end up in platform request logs.
"""
from __future__ import annotations

import hmac
import json
import os
import sys
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import notify_core as core  # noqa: E402
import notify_icons as icons  # noqa: E402
import notify_pages as pages  # noqa: E402
from notify_vendor import QRCODE_JS  # noqa: E402

MAX_BODY_BYTES = 64 * 1024
DRAIN_CHUNK = 64 * 1024
DRAIN_CAP = 8 * 1024 * 1024  # never read more than this when discarding a body

CACHE_NONE = "no-store"
CACHE_SW = "no-cache"
CACHE_ASSET = "public, max-age=86400"
CACHE_VENDOR = "public, max-age=604800, immutable"
HSTS = "max-age=63072000; includeSubDomains; preload"


class handler(BaseHTTPRequestHandler):
    # Never log request lines: keep function logs free of anything
    # user-related (the default would write every request line, query
    # string included). Guarded by test_no_request_logging.
    def log_message(self, format, *args):  # noqa: A002 (stdlib signature)
        pass

    server_version = "notify"
    sys_version = ""
    # Bound how long a slow/short client can park a handler thread (a client
    # that declares a large Content-Length but never sends it would otherwise
    # block rfile.read forever).
    timeout = 20

    # ------------------------------------------------------------ helpers

    def _client_ip(self) -> str:
        # x-real-ip is set by Vercel to the true client IP and is not
        # client-spoofable; the leftmost x-forwarded-for hop is. Prefer the
        # former, fall back for local/self-hosted use.
        real = self.headers.get("x-real-ip")
        if real:
            return real.strip()
        fwd = self.headers.get("x-forwarded-for")
        if fwd:
            return fwd.split(",")[0].strip()
        return self.client_address[0]

    def _drain_body(self) -> None:
        """Discard the request body so an early response does not close the
        socket with unread data (which would send a TCP RST and hide the
        response from the client)."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return
        remaining = min(max(length, 0), DRAIN_CAP)
        while remaining > 0:
            chunk = self.rfile.read(min(DRAIN_CHUNK, remaining))
            if not chunk:
                break
            remaining -= len(chunk)

    def _send(self, status, body, ctype, extra=None, cache=CACHE_NONE):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self._responded = True
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Strict-Transport-Security", HSTS)
        self.send_header("Cache-Control", cache)
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _html(self, html: str) -> None:
        self._send(
            200,
            html,
            "text/html; charset=utf-8",
            {"Content-Security-Policy": pages.CSP},
        )

    def _json(self, status: int, obj: dict) -> None:
        self._send(
            status,
            json.dumps(obj),
            "application/json",
            {"Access-Control-Allow-Origin": "*"},
        )

    def _error(self, status: int, message: str) -> None:
        self._json(status, {"ok": False, "error": message})

    def _read_json_body(self) -> "dict | None":
        """Returns the parsed JSON object, or None after sending an error."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            self._error(400, "request body required")
            return None
        if length > MAX_BODY_BYTES:
            self._drain_body()
            self._error(413, "request body too large")
            return None
        raw = self.rfile.read(length)
        if len(raw) != length:
            self._error(400, "incomplete request body")
            return None
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._error(400, "invalid JSON")
            return None
        if not isinstance(payload, dict):
            self._error(400, "JSON object expected")
            return None
        return payload

    def _get_code(self, payload: dict) -> "str | None":
        code = payload.get("code")
        if not core.valid_code(code):
            self._error(400, "invalid or missing channel code")
            return None
        return code

    def _status(self) -> None:
        """Secret-gated operational diagnostics. Provide the secret via an
        `Authorization: Bearer <secret>` header (preferred — stays out of
        URLs/logs) or a `?key=<secret>` query param."""
        secret = core.status_secret()
        if not secret:
            self._error(404, "not found")  # diagnostics disabled (fail closed)
            return
        provided = ""
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            provided = auth[7:].strip()
        if not provided:
            provided = parse_qs(urlparse(self.path).query).get("key", [""])[0]
        if not (provided and hmac.compare_digest(provided, secret)):
            self._error(401, "unauthorized")
            return
        self._json(200, core.diagnostics())

    # -------------------------------------------------------------- GET

    def do_GET(self):  # noqa: N802 (stdlib naming)
        path = self.path.split("?", 1)[0]
        try:
            if path == "/":
                self._html(pages.index_html())
            elif path == "/a":
                _, public_key, _ = core.vapid_config()
                self._html(pages.app_html(public_key))
            elif path == "/sw.js":
                self._send(
                    200,
                    pages.SW_JS,
                    "text/javascript; charset=utf-8",
                    {"Service-Worker-Allowed": "/"},
                    cache=CACHE_SW,
                )
            elif path == "/vendor/qrcode.js":
                self._send(
                    200,
                    QRCODE_JS,
                    "text/javascript; charset=utf-8",
                    cache=CACHE_VENDOR,
                )
            elif path == "/icon.svg":
                self._send(200, pages.ICON_SVG, "image/svg+xml", cache=CACHE_ASSET)
            elif path == "/icon-192.png":
                self._send(200, icons.ICON_192, "image/png", cache=CACHE_ASSET)
            elif path == "/icon-512.png":
                self._send(200, icons.ICON_512, "image/png", cache=CACHE_ASSET)
            elif path in ("/apple-touch-icon.png", "/apple-touch-icon-precomposed.png"):
                self._send(
                    200, icons.APPLE_TOUCH_ICON, "image/png", cache=CACHE_ASSET
                )
            elif path == "/favicon.ico":
                self._send(200, icons.ICON_192, "image/png", cache=CACHE_ASSET)
            elif path == "/robots.txt":
                self._send(200, pages.ROBOTS_TXT, "text/plain; charset=utf-8")
            elif path == "/api/health":
                self._json(200, {"ok": True})
            elif path == "/api/status":
                self._status()
            elif path.startswith("/api/"):
                self._error(404, "not found")
            else:
                self._send(404, "Not found\n", "text/plain; charset=utf-8")
        except (ConnectionError, BrokenPipeError, TimeoutError):
            return  # client went away mid-response; nothing to say
        except Exception:
            self._safe_error(500, "internal error")

    # -------------------------------------------------------------- POST

    def do_POST(self):  # noqa: N802
        path = self.path.split("?", 1)[0]
        if not path.startswith("/api/"):
            self._drain_body()
            self._error(404, "not found")
            return
        if not core.limiter.allow(self._client_ip(), core.rate_per_min()):
            self._drain_body()
            self._send(
                429,
                json.dumps({"ok": False, "error": "rate limit exceeded"}),
                "application/json",
                {"Retry-After": "60", "Access-Control-Allow-Origin": "*"},
            )
            return
        payload = self._read_json_body()
        if payload is None:
            return
        try:
            if path == "/api/channel":
                self._post_channel(payload)
            elif path == "/api/subscribe":
                self._post_subscribe(payload)
            elif path == "/api/unsubscribe":
                self._post_unsubscribe(payload)
            elif path == "/api/message":
                self._post_message(payload)
            elif path == "/api/messages":
                self._post_messages(payload)
            else:
                self._error(404, "not found")
        except (ConnectionError, BrokenPipeError, TimeoutError):
            return
        except core.ChannelFull:
            self._safe_error(409, "channel subscriber limit reached")
        except core.StorageError:
            self._safe_error(502, "storage unavailable")
        except json.JSONDecodeError:
            # corrupt data read back from storage — a server problem, not a
            # bad request (validation errors raise plain ValueError instead)
            self._safe_error(500, "internal error")
        except ValueError as exc:
            self._safe_error(400, str(exc))
        except Exception:
            self._safe_error(500, "internal error")

    def _safe_error(self, status: int, message: str) -> None:
        """Send an error unless a response was already started (e.g. the
        original failure was the response write itself)."""
        if getattr(self, "_responded", False):
            return
        try:
            self._error(status, message)
        except (ConnectionError, BrokenPipeError, TimeoutError, OSError):
            pass

    def _post_channel(self, payload: dict) -> None:
        # A per-instance soft cap on channel creation bounds storage growth
        # even if the per-IP limiter is defeated by header spoofing.
        if not core.limiter.allow("@channel-create", core.max_channels_per_min()):
            self._send(
                429,
                json.dumps({"ok": False, "error": "too many new channels, slow down"}),
                "application/json",
                {"Retry-After": "60", "Access-Control-Allow-Origin": "*"},
            )
            return
        name = core.clean_channel_name(payload.get("name"))
        result = core.create_channel(name)
        self._json(200, {"ok": True, **result})

    def _post_subscribe(self, payload: dict) -> None:
        code = self._get_code(payload)
        if code is None:
            return
        count = core.subscribe(code, payload.get("subscription"))
        if count is None:
            self._error(404, "unknown channel")
            return
        self._json(200, {"ok": True, "subscribers": count})

    def _post_unsubscribe(self, payload: dict) -> None:
        code = self._get_code(payload)
        if code is None:
            return
        removed = core.unsubscribe(code, payload.get("endpoint"))
        if removed is None:
            self._error(404, "unknown channel")
            return
        self._json(200, {"ok": True, "removed": removed})

    def _post_message(self, payload: dict) -> None:
        code = self._get_code(payload)
        if code is None:
            return
        message = core.validate_message(payload)
        result = core.publish(code, message)
        if result is None:
            self._error(404, "unknown channel")
            return
        self._json(200, {"ok": True, **result})

    def _post_messages(self, payload: dict) -> None:
        code = self._get_code(payload)
        if code is None:
            return
        limit = payload.get("limit", 20)
        if not isinstance(limit, int) or not 1 <= limit <= 50:
            limit = 20
        snapshot = core.channel_snapshot(code, limit)
        if snapshot is None:
            self._error(404, "unknown channel")
            return
        self._json(200, {"ok": True, **snapshot})

    # ----------------------------------------------------------- OPTIONS

    def do_OPTIONS(self):  # noqa: N802
        if self.path.split("?", 1)[0].startswith("/api/"):
            self._send(
                204,
                b"",
                "text/plain",
                {
                    "Access-Control-Allow-Origin": "*",
                    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
                    "Access-Control-Allow-Headers": "Content-Type",
                    "Access-Control-Max-Age": "86400",
                },
            )
        else:
            self._error(404, "not found")

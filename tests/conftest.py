"""Shared fixtures: isolated env + storage, mocked web push, and the real
handler served on a local port (offline — no external services touched)."""
import base64
import json
import os
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "api"))

import notify_core  # noqa: E402


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _test_vapid_pair():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    key = ec.generate_private_key(ec.SECP256R1())
    private_raw = key.private_numbers().private_value.to_bytes(32, "big")
    public_raw = key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    return _b64url(private_raw), _b64url(public_raw)


TEST_VAPID_PRIVATE, TEST_VAPID_PUBLIC = _test_vapid_pair()


def fake_subscription(n: int = 1) -> dict:
    return {
        "endpoint": f"https://push.example.invalid/send/device-{n}",
        "expirationTime": None,
        "keys": {
            "p256dh": "BPtest_p256dh_key_0123456789abcdef",
            "auth": "test_auth_16byte",
        },
    }


@pytest.fixture()
def env(monkeypatch):
    """Isolated config: in-memory storage, test VAPID keys, fresh limiter."""
    for var in (
        "KV_REST_API_URL",
        "KV_REST_API_TOKEN",
        "UPSTASH_REDIS_REST_URL",
        "UPSTASH_REDIS_REST_TOKEN",
        "NBW_RATE_PER_MIN",
        "NBW_MAX_SUBS_PER_CHANNEL",
        "NBW_MAX_MESSAGES",
        "NBW_CHANNEL_TTL_DAYS",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("VAPID_PRIVATE_KEY", TEST_VAPID_PRIVATE)
    monkeypatch.setenv("VAPID_PUBLIC_KEY", TEST_VAPID_PUBLIC)
    monkeypatch.setenv("VAPID_SUBJECT", "mailto:test@example.invalid")
    notify_core.reset_storage_for_tests()
    notify_core.limiter = notify_core.RateLimiter()
    yield monkeypatch
    notify_core.reset_storage_for_tests()


@pytest.fixture()
def push_calls(monkeypatch):
    """Replace pywebpush.webpush; captures every send."""
    calls = []

    def fake_webpush(subscription_info, data=None, **kwargs):
        calls.append(
            {
                "sub": subscription_info,
                "payload": json.loads(data),
                "kwargs": kwargs,
            }
        )

    monkeypatch.setattr(notify_core, "webpush", fake_webpush)
    return calls


class Client:
    def __init__(self, base: str) -> None:
        self.base = base

    def _request(self, method: str, path: str, body=None, headers=None):
        data = None
        req_headers = dict(headers or {})
        if body is not None:
            if isinstance(body, (bytes, bytearray)):
                data = bytes(body)
            else:
                data = json.dumps(body).encode("utf-8")
            req_headers.setdefault("Content-Type", "application/json")
        req = urllib.request.Request(
            self.base + path, method=method, data=data, headers=req_headers
        )
        try:
            resp = urllib.request.urlopen(req, timeout=10)
        except urllib.error.HTTPError as exc:
            resp = exc
        except (urllib.error.URLError, ConnectionError, OSError) as exc:
            # surface a transport failure (e.g. connection reset) as a
            # distinct status rather than raising, so tests can assert on it
            return SimpleNamespace(
                status=0, headers={}, raw=str(exc).encode(), json=None, error=exc
            )
        raw = resp.read()
        parsed = None
        ctype = resp.headers.get("Content-Type", "")
        if ctype.startswith("application/json"):
            parsed = json.loads(raw.decode("utf-8"))
        return SimpleNamespace(
            status=resp.status if hasattr(resp, "status") else resp.code,
            headers=resp.headers,
            raw=raw,
            json=parsed,
        )

    def get(self, path: str, **kw):
        return self._request("GET", path, **kw)

    def post(self, path: str, body=None, **kw):
        return self._request("POST", path, body=body, **kw)

    def options(self, path: str, **kw):
        return self._request("OPTIONS", path, **kw)


class _TestServer(ThreadingHTTPServer):
    # Join per-request handler threads on close so an in-flight request from
    # one test cannot leak into the next test's fresh storage/limiter.
    daemon_threads = False
    block_on_close = True


@pytest.fixture()
def server(env):
    """The real handler on a local port, backed by in-memory storage."""
    from index import handler

    srv = _TestServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield Client(f"http://127.0.0.1:{srv.server_port}")
    srv.shutdown()
    srv.server_close()
    thread.join(timeout=5)


@pytest.fixture()
def channel(server):
    """A freshly created channel; returns its code."""
    resp = server.post("/api/channel", {"name": "Test Channel"})
    assert resp.status == 200 and resp.json["ok"]
    return resp.json["code"]

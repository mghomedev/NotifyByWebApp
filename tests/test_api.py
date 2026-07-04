"""Integration tests: the real handler served on a local port, in-memory
storage, mocked push delivery. Fully offline."""
import json
from types import SimpleNamespace

import pytest
from pywebpush import WebPushException

import notify_core as core
from conftest import TEST_VAPID_PUBLIC, fake_subscription


# ---------------------------------------------------------- happy path


def test_full_flow_create_subscribe_send_list(server, channel, push_calls):
    # subscribe one device
    resp = server.post(
        "/api/subscribe", {"code": channel, "subscription": fake_subscription(1)}
    )
    assert resp.status == 200
    assert resp.json == {"ok": True, "subscribers": 1}

    # send a message
    resp = server.post(
        "/api/message",
        {
            "code": channel,
            "title": "Hello",
            "body": "World",
            "url": "https://example.com/details",
        },
    )
    assert resp.status == 200
    body = resp.json
    assert body["ok"] and body["stored"]
    assert body["sent"] == 1 and body["failed"] == 0 and body["pruned"] == 0
    assert not body["push_disabled"]

    # the push went to the right endpoint with the right payload
    assert len(push_calls) == 1
    call = push_calls[0]
    assert call["sub"]["endpoint"] == fake_subscription(1)["endpoint"]
    assert call["payload"]["title"] == "Hello"
    assert call["payload"]["body"] == "World"
    assert call["payload"]["url"] == "https://example.com/details"
    assert call["payload"]["channel"] == "Test Channel"
    assert call["payload"]["tag"]
    assert call["kwargs"]["vapid_claims"]["sub"] == "mailto:test@example.invalid"
    assert call["kwargs"]["vapid_private_key"]

    # message is listed, newest first
    server.post("/api/message", {"code": channel, "title": "Second"})
    resp = server.post("/api/messages", {"code": channel})
    assert resp.status == 200
    snap = resp.json
    assert snap["channel"]["name"] == "Test Channel"
    assert snap["subscribers"] == 1
    titles = [m["title"] for m in snap["messages"]]
    assert titles == ["Second", "Hello"]
    first = snap["messages"][1]
    assert first["body"] == "World"
    assert first["url"] == "https://example.com/details"
    assert first["ts"] > 0 and first["id"]


def test_channel_create_cleans_name(server):
    resp = server.post("/api/channel", {"name": "  My \n Channel  "})
    assert resp.status == 200
    assert resp.json["name"] == "My Channel"
    assert core.valid_code(resp.json["code"])


def test_channel_create_rejects_long_name(server):
    resp = server.post("/api/channel", {"name": "x" * 81})
    assert resp.status == 400


# ------------------------------------------------------- error handling


def test_unknown_channel_is_404(server):
    ghost = core.generate_code()
    assert server.post("/api/messages", {"code": ghost}).status == 404
    assert server.post("/api/message", {"code": ghost, "title": "x"}).status == 404
    assert (
        server.post(
            "/api/subscribe", {"code": ghost, "subscription": fake_subscription()}
        ).status
        == 404
    )
    assert (
        server.post(
            "/api/unsubscribe", {"code": ghost, "endpoint": "https://p.example/x"}
        ).status
        == 404
    )


def test_bad_code_format_is_400(server):
    for bad in ["", "short", "x" * 65, "bad code!!", None, 5]:
        resp = server.post("/api/messages", {"code": bad})
        assert resp.status == 400, bad


def test_bad_bodies_are_400(server):
    assert server.post("/api/channel").status == 400  # no body
    assert (
        server.post("/api/channel", body=b"not json{{").status == 400
    )
    assert server.post("/api/channel", body=b'["list"]').status == 400


def test_message_validation_via_api(server, channel):
    assert server.post("/api/message", {"code": channel}).status == 400
    assert (
        server.post(
            "/api/message", {"code": channel, "title": "x", "url": "ftp://x.example"}
        ).status
        == 400
    )


def test_subscription_validation_via_api(server, channel):
    resp = server.post("/api/subscribe", {"code": channel, "subscription": "junk"})
    assert resp.status == 400


def test_oversized_body_is_413(server):
    big = {"name": "x", "pad": "y" * (70 * 1024)}
    resp = server.post("/api/channel", big)
    assert resp.status == 413


def test_large_oversized_body_still_gets_clean_413_not_reset(server):
    # ~1 MB body: the server must drain it and return a clean 413, not close
    # the socket with unread data (which would surface as a connection reset).
    big = {"name": "x", "pad": "z" * (1024 * 1024)}
    resp = server.post("/api/message", big)
    assert resp.status == 413, f"got status {resp.status} ({getattr(resp,'error',None)})"


def test_unknown_api_routes_404(server):
    assert server.post("/api/nope", {"x": 1}).status == 404
    assert server.get("/api/nope").status == 404
    assert server.get("/nope").status == 404


# ------------------------------------------------------- subscriptions


def test_subscribe_is_idempotent_per_endpoint(server, channel):
    for _ in range(3):
        resp = server.post(
            "/api/subscribe", {"code": channel, "subscription": fake_subscription(7)}
        )
        assert resp.status == 200
    assert resp.json["subscribers"] == 1


def test_subscriber_cap(server, channel, env):
    env.setenv("NBW_MAX_SUBS_PER_CHANNEL", "2")
    for n in (1, 2):
        assert (
            server.post(
                "/api/subscribe",
                {"code": channel, "subscription": fake_subscription(n)},
            ).status
            == 200
        )
    resp = server.post(
        "/api/subscribe", {"code": channel, "subscription": fake_subscription(3)}
    )
    assert resp.status == 409
    # updating an existing subscription still works at the cap
    resp = server.post(
        "/api/subscribe", {"code": channel, "subscription": fake_subscription(2)}
    )
    assert resp.status == 200


def test_unsubscribe_stops_push(server, channel, push_calls):
    sub = fake_subscription(1)
    server.post("/api/subscribe", {"code": channel, "subscription": sub})
    resp = server.post(
        "/api/unsubscribe", {"code": channel, "endpoint": sub["endpoint"]}
    )
    assert resp.status == 200 and resp.json["removed"]
    resp = server.post("/api/message", {"code": channel, "title": "x"})
    assert resp.json["sent"] == 0
    assert push_calls == []


def test_gone_subscriptions_are_pruned(server, channel, env, monkeypatch):
    server.post(
        "/api/subscribe", {"code": channel, "subscription": fake_subscription(1)}
    )

    def gone_webpush(subscription_info, data=None, **kwargs):
        exc = WebPushException("gone")
        exc.response = SimpleNamespace(status_code=410)
        raise exc

    monkeypatch.setattr(core, "webpush", gone_webpush)
    resp = server.post("/api/message", {"code": channel, "title": "x"})
    assert resp.json["pruned"] == 1
    assert resp.json["sent"] == 0
    snap = server.post("/api/messages", {"code": channel}).json
    assert snap["subscribers"] == 0


def test_failed_push_is_counted_not_pruned(server, channel, env, monkeypatch):
    server.post(
        "/api/subscribe", {"code": channel, "subscription": fake_subscription(1)}
    )

    def failing_webpush(subscription_info, data=None, **kwargs):
        exc = WebPushException("server error")
        exc.response = SimpleNamespace(status_code=500)
        raise exc

    monkeypatch.setattr(core, "webpush", failing_webpush)
    resp = server.post("/api/message", {"code": channel, "title": "x"})
    assert resp.json["failed"] == 1 and resp.json["pruned"] == 0
    snap = server.post("/api/messages", {"code": channel}).json
    assert snap["subscribers"] == 1  # still subscribed


def test_push_disabled_without_vapid_keys(server, channel, env, push_calls):
    env.setenv("VAPID_PRIVATE_KEY", "")
    server.post(
        "/api/subscribe", {"code": channel, "subscription": fake_subscription(1)}
    )
    resp = server.post("/api/message", {"code": channel, "title": "x"})
    assert resp.status == 200
    assert resp.json["push_disabled"] is True
    assert push_calls == []
    # stored anyway
    snap = server.post("/api/messages", {"code": channel}).json
    assert snap["messages"][0]["title"] == "x"


# ------------------------------------------------------------ throttling


def test_rate_limit(server, env):
    env.setenv("NBW_RATE_PER_MIN", "3")
    for _ in range(3):
        assert server.post("/api/channel", {"name": "x"}).status == 200
    resp = server.post("/api/channel", {"name": "x"})
    assert resp.status == 429
    assert resp.headers.get("Retry-After") == "60"
    # the hand-built 429 response must still carry CORS for browser senders
    assert resp.headers.get("Access-Control-Allow-Origin") == "*"


def test_channel_creation_soft_cap(server, env):
    # bounds storage growth even if the per-IP limiter is bypassed
    env.setenv("NBW_MAX_CHANNELS_PER_MIN", "2")
    env.setenv("NBW_RATE_PER_MIN", "1000")
    assert server.post("/api/channel", {"name": "a"}).status == 200
    assert server.post("/api/channel", {"name": "b"}).status == 200
    resp = server.post("/api/channel", {"name": "c"})
    assert resp.status == 429


# ------------------------------------------------------------- the web


def test_landing_page(server):
    resp = server.get("/")
    assert resp.status == 200
    html = resp.raw.decode("utf-8")
    assert "Create a channel" in html
    assert "/vendor/qrcode.js" in html
    assert resp.headers.get("Content-Security-Policy")
    assert resp.headers.get("X-Content-Type-Options") == "nosniff"
    assert resp.headers.get("Referrer-Policy") == "no-referrer"


def _csp_directives(header: str) -> dict:
    out = {}
    for part in header.split(";"):
        part = part.strip()
        if not part:
            continue
        name, _, rest = part.partition(" ")
        out[name] = rest.split()
    return out


def test_app_page_embeds_vapid_key(server):
    resp = server.get("/a")
    assert resp.status == 200
    html = resp.raw.decode("utf-8")
    assert TEST_VAPID_PUBLIC in html
    assert "Enable notifications" in html
    # load-bearing markers of the runtime manifest injection (not just the
    # word "manifest" appearing somewhere in the script)
    assert "manifest-link" in html
    assert "data:application/manifest+json" in html
    assert "start_url" in html
    assert resp.headers.get("Content-Security-Policy")


def test_csp_grants_exactly_what_the_pages_need(server):
    # A regression that drops e.g. `data:` from manifest-src would silently
    # break Add-to-Home-Screen; assert the specific directives, not just that
    # a CSP header exists.
    for path in ("/", "/a"):
        csp = _csp_directives(server.get(path).headers.get("Content-Security-Policy"))
        assert csp["default-src"] == ["'none'"]
        assert "'self'" in csp["script-src"] and "'unsafe-inline'" in csp["script-src"]
        assert "'self'" in csp["connect-src"]
        assert "'self'" in csp["manifest-src"] and "data:" in csp["manifest-src"]
        assert "'self'" in csp["worker-src"]
        assert "data:" in csp["img-src"]
        assert csp["frame-ancestors"] == ["'none'"]


def test_storage_outage_returns_502(server, monkeypatch):
    class BrokenStorage:
        def get_channel(self, kh):
            raise core.StorageError("down")

    monkeypatch.setattr(core, "get_storage", lambda: BrokenStorage())
    code = core.generate_code()
    resp = server.post("/api/messages", {"code": code})
    assert resp.status == 502
    assert resp.json["ok"] is False
    assert resp.headers.get("Access-Control-Allow-Origin") == "*"


def test_service_worker_served_correctly(server):
    resp = server.get("/sw.js")
    assert resp.status == 200
    assert resp.headers.get("Content-Type", "").startswith("text/javascript")
    assert resp.headers.get("Service-Worker-Allowed") == "/"
    js = resp.raw.decode("utf-8")
    assert "showNotification" in js
    assert "notificationclick" in js


def test_static_assets(server):
    assert b"qrcode" in server.get("/vendor/qrcode.js").raw
    assert server.get("/icon.svg").headers.get("Content-Type") == "image/svg+xml"
    for path in ("/icon-192.png", "/icon-512.png", "/apple-touch-icon.png"):
        resp = server.get(path)
        assert resp.status == 200
        assert resp.headers.get("Content-Type") == "image/png"
        assert resp.raw[:8] == b"\x89PNG\r\n\x1a\n"
    robots = server.get("/robots.txt").raw.decode()
    assert "Disallow: /api/" in robots and "Disallow: /a" in robots


def test_health(server):
    resp = server.get("/api/health")
    assert resp.status == 200 and resp.json == {"ok": True}


def test_status_endpoint_with_secret_header(server):
    from conftest import TEST_STATUS_SECRET, TEST_VAPID_PRIVATE

    resp = server.get(
        "/api/status", headers={"Authorization": "Bearer " + TEST_STATUS_SECRET}
    )
    assert resp.status == 200
    d = resp.json
    assert d["ok"] is True
    assert d["push"]["configured"] is True
    assert d["storage"] == {"backend": "memory", "reachable": True}
    assert d["limits"]["rate_per_min"] == 120
    assert resp.headers.get("Access-Control-Allow-Origin") == "*"
    # the diagnostics must not leak any secret value
    body = resp.raw.decode("utf-8")
    assert TEST_VAPID_PRIVATE not in body
    assert TEST_STATUS_SECRET not in body


def test_status_endpoint_with_query_secret(server):
    from conftest import TEST_STATUS_SECRET

    resp = server.get("/api/status?key=" + TEST_STATUS_SECRET)
    assert resp.status == 200
    assert resp.json["storage"]["backend"] == "memory"


def test_status_rejects_missing_or_wrong_secret(server):
    assert server.get("/api/status").status == 401
    assert server.get("/api/status?key=nope").status == 401
    assert (
        server.get(
            "/api/status", headers={"Authorization": "Bearer wrong"}
        ).status
        == 401
    )


def test_status_disabled_without_secret_configured(server, env):
    env.delenv("NBW_STATUS_SECRET", raising=False)
    # fails closed: without the secret configured the endpoint is invisible
    from conftest import TEST_STATUS_SECRET

    assert server.get("/api/status").status == 404
    assert (
        server.get(
            "/api/status", headers={"Authorization": "Bearer " + TEST_STATUS_SECRET}
        ).status
        == 404
    )


def test_cors_preflight(server):
    resp = server.options("/api/message")
    assert resp.status == 204
    assert resp.headers.get("Access-Control-Allow-Origin") == "*"
    assert "POST" in resp.headers.get("Access-Control-Allow-Methods", "")


def test_successful_post_carries_cors(server):
    resp = server.post("/api/channel", {"name": "cors"})
    assert resp.status == 200
    assert resp.headers.get("Access-Control-Allow-Origin") == "*"


def test_server_header_does_not_leak_python_version(server):
    resp = server.get("/")
    assert "Python" not in (resp.headers.get("Server") or "")


def test_security_headers_present_everywhere(server):
    for path in ("/", "/a", "/api/health"):
        resp = server.get(path)
        assert resp.headers.get("X-Content-Type-Options") == "nosniff"
        assert resp.headers.get("Referrer-Policy") == "no-referrer"
        assert "max-age=" in (resp.headers.get("Strict-Transport-Security") or "")


def test_control_chars_stripped_via_api(server, channel):
    resp = server.post(
        "/api/message", {"code": channel, "title": "a\x00b\x1fc", "body": "x\x00y"}
    )
    assert resp.status == 200
    snap = server.post("/api/messages", {"code": channel}).json
    assert snap["messages"][0]["title"] == "abc"
    assert snap["messages"][0]["body"] == "xy"


def test_control_char_only_title_via_api_is_400(server, channel):
    resp = server.post("/api/message", {"code": channel, "title": "\x00\x1f"})
    assert resp.status == 400


def test_no_request_logging(server, channel, capfd):
    """The handler must never write request lines (query strings could carry
    user data) to the function logs."""
    server.get("/?probe=SECRET_MARKER")
    server.post("/api/messages", {"code": channel})
    server.get("/definitely-missing-page")
    out, err = capfd.readouterr()
    assert "SECRET_MARKER" not in out + err
    assert "GET /" not in out + err
    assert "POST /" not in out + err

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


def test_send_password_channel_flow(server):
    resp = server.post(
        "/api/channel",
        {"name": "Event", "send_password": "manager-key", "message_store": "max"},
    )
    assert resp.status == 200 and resp.json["send_protected"] is True
    code = resp.json["code"]
    # snapshot advertises that sending is protected
    snap = server.post("/api/messages", {"code": code}).json
    assert snap["channel"]["send_protected"] is True
    # sending without / with a wrong password is forbidden
    assert server.post("/api/message", {"code": code, "title": "x"}).status == 403
    assert (
        server.post(
            "/api/message", {"code": code, "title": "x", "send_password": "nope"}
        ).status
        == 403
    )
    # correct password sends
    resp = server.post(
        "/api/message", {"code": code, "title": "x", "send_password": "manager-key"}
    )
    assert resp.status == 200 and resp.json["stored"]


def test_unprotected_channel_needs_no_password(server, channel):
    assert (
        server.post("/api/messages", {"code": channel}).json["channel"]["send_protected"]
        is False
    )
    assert server.post("/api/message", {"code": channel, "title": "x"}).status == 200


def test_create_channel_rejects_short_send_password(server):
    assert server.post("/api/channel", {"name": "x", "send_password": "ab"}).status == 400


# ------------------------------------------------------- error handling


def test_delete_and_clear_messages_via_api(server, channel):
    id1 = server.post("/api/message", {"code": channel, "title": "one"}).json["message"]["id"]
    server.post("/api/message", {"code": channel, "title": "two"})
    assert len(server.post("/api/messages", {"code": channel}).json["messages"]) == 2
    resp = server.post("/api/message/delete", {"code": channel, "id": id1})
    assert resp.status == 200 and resp.json["removed"] is True
    titles = [m["title"] for m in server.post("/api/messages", {"code": channel}).json["messages"]]
    assert titles == ["two"]
    assert server.post("/api/messages/clear", {"code": channel}).status == 200
    assert server.post("/api/messages", {"code": channel}).json["messages"] == []


def test_clear_messages_keep_via_api(server, channel):
    for i in range(5):
        server.post("/api/message", {"code": channel, "title": f"m{i}"})
    resp = server.post("/api/messages/clear", {"code": channel, "keep": 3})
    assert resp.status == 200
    titles = [m["title"] for m in server.post("/api/messages", {"code": channel}).json["messages"]]
    assert titles == ["m4", "m3", "m2"]


def test_delete_message_bad_id_is_400(server, channel):
    assert server.post("/api/message/delete", {"code": channel, "id": ""}).status == 400
    assert server.post("/api/message/delete", {"code": channel}).status == 400


def test_delete_on_protected_channel_needs_password(server):
    code = server.post(
        "/api/channel",
        {"name": "P", "send_password": "key-phrase", "message_store": "max"},
    ).json["code"]
    mid = server.post(
        "/api/message", {"code": code, "title": "x", "send_password": "key-phrase"}
    ).json["message"]["id"]
    assert server.post("/api/message/delete", {"code": code, "id": mid}).status == 403
    assert server.post("/api/messages/clear", {"code": code}).status == 403
    ok = server.post(
        "/api/message/delete",
        {"code": code, "id": mid, "send_password": "key-phrase"},
    )
    assert ok.status == 200 and ok.json["removed"] is True


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
    assert server.post("/api/message", {"code": channel}).status == 400  # no title/body
    assert (
        server.post(
            "/api/message", {"code": channel, "title": "x", "url": "ftp://x.example"}
        ).status
        == 400
    )


def test_message_body_only_derives_title(server, channel):
    resp = server.post("/api/message", {"code": channel, "body": "Kickoff at 10am"})
    assert resp.status == 200
    snap = server.post("/api/messages", {"code": channel}).json
    assert snap["messages"][0]["title"] == "Kickoff at 10am"
    assert snap["messages"][0]["body"] == "Kickoff at 10am"


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
    assert "Create your channel" in html
    assert "Further technical information for developers" in html  # API moved here
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


def test_disclaimer_on_both_pages(server):
    for path in ("/", "/a"):
        html = server.get(path).raw.decode("utf-8")
        low = html.lower()
        assert "no warranty" in low
        assert "at your own risk" in low
        assert "open-source" in low and "host their own copy" in low
        assert "emergency notifications" in low
        assert 'lang="de"' in html  # German section present
        assert "Nutzung auf eigene Gefahr" in html
        assert "__DISCLAIMER__" not in html  # placeholder was substituted


def test_compatibility_list_on_both_pages(server):
    for path in ("/", "/a"):
        html = server.get(path).raw.decode("utf-8")
        assert "Supported devices" in html
        assert "16.4" in html  # iOS/iPadOS minimum
        assert "16.1" in html and "Ventura" in html  # macOS Safari
        assert "Android" in html and "Chrome" in html
        assert "__COMPAT__" not in html  # placeholder substituted
    # landing page shows the list expanded (open) so devices are visible;
    # app page keeps it collapsed
    assert '<details class="compat" open>' in server.get("/").raw.decode("utf-8")
    assert '<details class="compat" open>' not in server.get("/a").raw.decode("utf-8")


def test_app_page_has_too_old_banner_element(server):
    html = server.get("/a").raw.decode("utf-8")
    assert 'id="too-old"' in html
    assert "iosAtLeast" in html and "pushStatus" in html  # detection logic present


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
    svg = server.get("/icon.svg")
    assert svg.headers.get("Content-Type") == "image/svg+xml"
    assert "#FBBF24" in svg.raw.decode()  # the amber broadcast-signal accent of the mark
    for path in ("/icon-192.png", "/icon-512.png", "/apple-touch-icon.png", "/badge.png"):
        resp = server.get(path)
        assert resp.status == 200
        assert resp.headers.get("Content-Type") == "image/png"
        assert resp.raw[:8] == b"\x89PNG\r\n\x1a\n"
    # The Android notification badge MUST carry an alpha channel (PNG colour type
    # 4 or 6, byte 25 of the IHDR) — Android masks the small icon to its alpha, so
    # an opaque icon would render as a plain white square in the status bar.
    assert server.get("/badge.png").raw[25] in (4, 6)
    robots = server.get("/robots.txt").raw.decode()
    assert "Disallow: /api/" in robots and "Disallow: /a" in robots


def test_google_site_verification(server):
    resp = server.get("/google775b279a195202b2.html")
    assert resp.status == 200
    body = resp.raw.decode("utf-8")
    assert body.strip() == "google-site-verification: google775b279a195202b2.html"


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
    # the GET API puts the code into the request line — our own function log
    # must still record NOTHING (platform logs are the documented residual)
    server.get(f"/api/messages?code={channel}&probe=SECRET_MARKER_GET")
    server.get("/definitely-missing-page")
    out, err = capfd.readouterr()
    assert "SECRET_MARKER" not in out + err
    assert channel not in out + err
    assert "GET /" not in out + err
    assert "POST /" not in out + err


# ---------------------------------------------------------- auto-remove


def test_create_channel_with_auto_remove_days(server):
    resp = server.post("/api/channel", {"name": "Timed", "auto_remove_days": 7})
    assert resp.status == 200
    code = resp.json["code"]
    assert core.code_expiry(code) == resp.json["expires"]
    snap = server.post("/api/messages", {"code": code})
    assert snap.json["channel"]["expires"] == resp.json["expires"]
    # default stays exactly as before: no suffix, no expiry
    plain = server.post("/api/channel", {"name": "Forever"}).json
    assert plain["expires"] is None and core.code_expiry(plain["code"]) is None
    for bad in ("soon", -1, 99999, 1.5):
        assert server.post("/api/channel", {"auto_remove_days": bad}).status == 400


def test_extend_channel_endpoint(server, push_calls):
    old = server.post(
        "/api/channel",
        {"name": "Evt", "auto_remove_days": 2, "message_store": "max"},
    ).json
    server.post("/api/message", {"code": old["code"], "title": "keep"})
    server.post(
        "/api/subscribe", {"code": old["code"], "subscription": fake_subscription(9)}
    )
    resp = server.post(
        "/api/channel/extend", {"code": old["code"], "auto_remove_days": 30}
    )
    assert resp.status == 200
    new = resp.json
    assert new["code"] != old["code"] and new["messages_copied"] == 1
    assert new["expires"] == core.code_expiry(new["code"])
    # successor carries the message; the old channel gets the migration notice
    snap_new = server.post("/api/messages", {"code": new["code"]}).json
    assert [m["title"] for m in snap_new["messages"]] == ["keep"]
    snap_old = server.post("/api/messages", {"code": old["code"]}).json
    assert snap_old["messages"][0]["title"].startswith("Channel extended")
    # the successor's raw code is never in stored data — push payload only
    assert new["code"] not in json.dumps(snap_old)
    assert push_calls[-1]["payload"]["url"] == "/a#codes=" + new["code"]
    assert new["notified"] == 1
    # unknown old code → 404
    ghost = server.post("/api/channel/extend", {"code": core.generate_code()})
    assert ghost.status == 404


# ---------------------------------------------------------- GET API twins


def _q(code):
    from urllib.parse import quote

    return quote(code, safe="")


def test_get_api_send_list_and_headers(server, push_calls):
    ch = server.post("/api/channel", {"name": "GetChan", "message_store": "max"}).json
    code = ch["code"]
    server.post("/api/subscribe", {"code": code, "subscription": fake_subscription(7)})
    r = server.get(f"/api/message?code={_q(code)}&title=Hello%20GET&body=World")
    assert r.status == 200 and r.json["ok"] and r.json["sent"] == 1
    assert r.headers.get("Cache-Control") == "no-store"
    assert r.headers.get("Access-Control-Allow-Origin") == "*"
    assert push_calls[-1]["payload"]["title"] == "Hello GET"
    server.get(f"/api/message?code={_q(code)}&title=Second")
    # list via GET returns the same JSON as POST; limit is coerced and honored
    r2 = server.get(f"/api/messages?code={_q(code)}&limit=1")
    assert r2.status == 200
    assert [m["title"] for m in r2.json["messages"]] == ["Second"]
    assert r2.json == server.post("/api/messages", {"code": code, "limit": 1}).json


def test_get_api_create_extend_delete_clear_unsubscribe(server, push_calls):
    r = server.get(
        "/api/channel?name=Via%20GET&auto_remove_days=7&message_store=max"
        "&send_cooloff_minutes=0"
    )
    assert r.status == 200 and r.json["name"] == "Via GET"
    code = r.json["code"]
    assert core.code_expiry(code) is not None  # auto_remove_days coerced
    assert r.json["message_store"] == -1
    # subscribe via GET with URL-encoded JSON
    sub = _q(json.dumps(fake_subscription(9)))
    assert (
        server.get(f"/api/subscribe?code={_q(code)}&subscription={sub}").status == 200
    )
    assert server.get("/api/subscribe?code=" + _q(code) + "&subscription=notjson").status == 400
    # send + delete + clear via GET
    mid = server.get(f"/api/message?code={_q(code)}&title=temp").json["message"]["id"]
    assert server.get(f"/api/message/delete?code={_q(code)}&id={mid}").status == 200
    server.get(f"/api/message?code={_q(code)}&title=a")
    server.get(f"/api/message?code={_q(code)}&title=b")
    assert server.get(f"/api/messages/clear?code={_q(code)}&keep=1").status == 200
    assert len(server.get(f"/api/messages?code={_q(code)}").json["messages"]) == 1
    # extend via GET with notify=false must NOT push a migration notice
    push_calls.clear()
    r3 = server.get(
        f"/api/channel/extend?code={_q(code)}&auto_remove_days=30&notify=false"
    )
    assert r3.status == 200 and push_calls == []  # the bool("false") trap
    # unsubscribe via GET
    endpoint = fake_subscription(9)["endpoint"]
    assert (
        server.get(f"/api/unsubscribe?code={_q(code)}&endpoint={_q(endpoint)}").status
        == 200
    )


def test_get_api_standard_error_codes(server):
    prot = server.post(
        "/api/channel",
        {"name": "P", "send_password": "gate-pass9", "message_store": "max"},
    ).json["code"]
    # 403 wrong password — proves the shared exception chain (naive dispatch
    # from do_GET would have produced a 500 here)
    r = server.get(f"/api/message?code={_q(prot)}&title=x&send_password=wrong")
    assert r.status == 403 and r.json["ok"] is False
    # 404 unknown channel, as JSON with CORS
    ghost = core.generate_code()
    r = server.get(f"/api/messages?code={_q(ghost)}")
    assert r.status == 404 and r.json["ok"] is False
    assert r.headers.get("Access-Control-Allow-Origin") == "*"
    # 400s: blank code, bad code, code-only send (load-bearing: a bare URL
    # fetched by anything is inert), bad params
    assert server.get("/api/messages?code=").status == 400
    assert server.get("/api/messages?code=short").status == 400
    ok_code = server.post("/api/channel", {"name": "x"}).json["code"]
    assert server.get(f"/api/message?code={_q(ok_code)}").status == 400
    assert server.get(f"/api/channel?auto_remove_days=nope").status == 400
    # unknown GET /api/* path stays a JSON 404
    r = server.get("/api/definitely-not-a-thing")
    assert r.status == 404 and r.json["ok"] is False
    # duplicate params: the FIRST value wins
    r = server.get(f"/api/messages?code={_q(ok_code)}&code={_q(ghost)}")
    assert r.status == 200


def test_get_api_prefetch_and_preview_agents_refused(server, push_calls):
    code = server.post("/api/channel", {"name": "G", "message_store": "max"}).json[
        "code"
    ]
    # speculative fetches (omnibox preload etc.) must never send
    r = server.get(
        f"/api/message?code={_q(code)}&title=spec",
        headers={"Sec-Purpose": "prefetch"},
    )
    assert r.status == 403
    # chat link-preview bots must never send
    r = server.get(
        f"/api/message?code={_q(code)}&title=bot",
        headers={"User-Agent": "WhatsApp/2.23.20"},
    )
    assert r.status == 403
    assert server.post("/api/messages", {"code": code}).json["messages"] == []
    assert push_calls == []
    # the read-only list endpoint is fine for such agents
    assert (
        server.get(
            f"/api/messages?code={_q(code)}", headers={"Sec-Purpose": "prefetch"}
        ).status
        == 200
    )


def test_get_api_rate_limited_and_status_limited(server, env):
    env.setenv("NBW_RATE_PER_MIN", "3")
    code = server.post("/api/channel", {"name": "RL"}).json["code"]  # uses 1 POST
    statuses = [server.get(f"/api/messages?code={_q(code)}").status for _ in range(4)]
    assert 429 in statuses  # GET now counts against the same per-IP bucket
    # /api/health stays exempt for uptime probes
    assert server.get("/api/health").status == 200
    # /api/status is now under the limiter too
    from conftest import TEST_STATUS_SECRET

    r = server.get(
        "/api/status", headers={"Authorization": f"Bearer {TEST_STATUS_SECRET}"}
    )
    assert r.status == 429


def test_head_requests_have_no_side_effects(server):
    code = server.post("/api/channel", {"name": "H", "message_store": "max"}).json[
        "code"
    ]
    r = server._request("HEAD", f"/api/message?code={_q(code)}&title=via-head")
    assert r.status == 501  # unsupported method, nothing executed
    assert server.post("/api/messages", {"code": code}).json["messages"] == []


# ------------------------------------------- send cool-off (spam protection)


def test_send_cooloff_via_api(server, env, monkeypatch):
    env.setenv("NBW_DEFAULT_COOLOFF_MINUTES", "5")
    env.setenv("NBW_MIN_COOLOFF_MINUTES", "1")
    ch = server.post("/api/channel", {"name": "Cool"}).json
    assert ch["send_cooloff"] == 300  # production default: 1 msg / 5 min
    code = ch["code"]
    assert server.post("/api/message", {"code": code, "title": "first"}).status == 200
    # too fast → standard 429 with Retry-After header + retry_after JSON
    r = server.post("/api/message", {"code": code, "title": "second"})
    assert r.status == 429
    assert "try again later" in r.json["error"]
    assert 1 <= r.json["retry_after"] <= 300
    assert int(r.headers.get("Retry-After")) == r.json["retry_after"]
    # the GET twin hits the same wall
    rg = server.get(f"/api/message?code={_q(code)}&title=third")
    assert rg.status == 429 and rg.json["retry_after"] > 0
    # the snapshot is the "get the timelimit" API
    snap = server.post("/api/messages", {"code": code}).json
    assert snap["channel"]["send_cooloff"] == 300
    assert 0 < snap["send_ready_in"] <= 300
    # once the window has passed, sending works again
    import time as _time

    real = _time.time
    monkeypatch.setattr(_time, "time", lambda: real() + 301)
    assert server.post("/api/message", {"code": code, "title": "later"}).status == 200
    # bounds are enforced at creation (and never changeable afterwards —
    # there is no API that updates a channel's cooloff)
    assert server.post("/api/channel", {"send_cooloff_minutes": 0}).status == 400
    assert server.post("/api/channel", {"send_cooloff_minutes": 43201}).status == 400
    assert server.post("/api/channel", {"send_cooloff_minutes": 1}).json[
        "send_cooloff"
    ] == 60


def test_deploy_commit_footer_on_both_pages(server, env):
    sha = "abc123def456" + "0" * 28
    env.setenv("VERCEL_GIT_COMMIT_SHA", sha)
    env.setattr(core, "_commit_dates", {})
    env.setattr(core, "_fetch_commit_date", lambda s: "2026-07-16")
    for path in ("/", "/a"):
        html = server.get(path).raw.decode()
        assert 'class="deploy-line"' in html
        assert f"https://github.com/{core.GITHUB_REPO}/commit/{sha}" in html
        assert ">abc123d</a> of 2026-07-16" in html  # linked short hash + date
    # a missing date degrades to the linked hash alone
    env.setattr(core, "_commit_dates", {sha: None})
    html = server.get("/").raw.decode()
    assert ">abc123d</a>" in html and "of 2026" not in html


def test_no_deploy_footer_outside_git_deployments(server):
    # the env fixture clears VERCEL_GIT_COMMIT_SHA (local dev / tests)
    for path in ("/", "/a"):
        assert 'class="deploy-line"' not in server.get(path).raw.decode()


def test_message_store_via_api(server, push_calls):
    # default channel: server stores nothing — pure relay
    plain = server.post("/api/channel", {"name": "Anon"}).json
    assert plain["message_store"] == 0
    server.post(
        "/api/subscribe", {"code": plain["code"], "subscription": fake_subscription(3)}
    )
    resp = server.post("/api/message", {"code": plain["code"], "title": "ghost"})
    assert resp.status == 200
    assert resp.json["stored"] is False and resp.json["sent"] == 1
    assert resp.json["message"]["title"] == "ghost"  # sender's local echo
    snap = server.post("/api/messages", {"code": plain["code"]}).json
    assert snap["messages"] == [] and snap["channel"]["message_store"] == 0
    # the push still went out, carrying the channel-id prefix
    assert push_calls[-1]["payload"]["ch"] == core.code_hash(plain["code"])[:12]

    # per-message override: store this one despite the channel default
    resp = server.post(
        "/api/message", {"code": plain["code"], "title": "keep", "store": "max"}
    )
    assert resp.json["stored"] is True
    titles = [
        m["title"]
        for m in server.post("/api/messages", {"code": plain["code"]}).json["messages"]
    ]
    assert titles == ["keep"]

    # retention channel: seconds-based storage setting round-trips
    timed = server.post("/api/channel", {"name": "T", "message_store": 3600}).json
    assert timed["message_store"] == 3600
    server.post("/api/message", {"code": timed["code"], "title": "hour"})
    msgs = server.post("/api/messages", {"code": timed["code"]}).json["messages"]
    assert msgs[0]["title"] == "hour" and msgs[0]["expires_at"] > msgs[0]["ts"]

    # invalid values are rejected
    for bad in ("forever", 59, True):
        assert server.post("/api/channel", {"message_store": bad}).status == 400
    assert (
        server.post(
            "/api/message", {"code": plain["code"], "title": "x", "store": 59}
        ).status
        == 400
    )


def test_expired_channel_is_404_everywhere(server, monkeypatch):
    import time as _time

    code = server.post("/api/channel", {"name": "Brief", "auto_remove_days": 1}).json[
        "code"
    ]
    assert server.post("/api/messages", {"code": code}).status == 200
    real = _time.time
    monkeypatch.setattr(_time, "time", lambda: real() + 3 * 86400)
    assert server.post("/api/messages", {"code": code}).status == 404
    assert server.post("/api/message", {"code": code, "title": "t"}).status == 404
    assert (
        server.post(
            "/api/subscribe", {"code": code, "subscription": fake_subscription(2)}
        ).status
        == 404
    )
    assert server.post("/api/channel/extend", {"code": code}).status == 404

"""End-to-end Web Push tests with REAL encryption.

These exercise the actual delivery pipeline: notify_core.publish() ->
pywebpush (VAPID sign + ECDH + HKDF + AES128GCM) -> a mock push service ->
a fake device that decrypts the payload. If encryption, key formats, VAPID
claims, TTL, byte-size bounding, or 410-pruning were wrong, these fail.

The device subscriptions are inserted straight into storage so the loopback
mock endpoint bypasses the SSRF host guard (which correctly blocks loopback
for the public API); the SSRF guard itself is tested in test_core.py.
"""
import json

import pytest

import notify_core as core
from pushkit import FakeDevice, MockPushService, parse_vapid_authorization


@pytest.fixture()
def push_service():
    svc = MockPushService()
    yield svc
    svc.close()


def _attach(code: str, device: FakeDevice) -> None:
    storage = core.get_storage()
    kh = core.code_hash(code)
    storage.put_sub(
        kh, core.endpoint_hash(device.endpoint), json.dumps(device.subscription())
    )


def test_encrypted_push_is_delivered_and_decryptable(server, channel, push_service):
    dev = FakeDevice(push_service, "phone1")
    _attach(channel, dev)

    resp = server.post(
        "/api/message",
        {
            "code": channel,
            "title": "Café ☕ Ålert",
            "body": "Grüße — line2\nunicode ✓",
            "url": "https://example.com/x",
        },
    )
    assert resp.status == 200
    assert resp.json["sent"] == 1
    assert resp.json["failed"] == 0

    reqs = push_service.requests_for("/push/phone1")
    assert len(reqs) == 1
    req = reqs[0]

    # correct Web Push wire headers
    headers = {k.lower(): v for k, v in req["headers"].items()}
    assert headers["content-encoding"] == "aes128gcm"
    assert headers["ttl"] == "86400"

    # the device can actually DECRYPT the payload the server produced
    payload = dev.decrypt(req["body"])
    assert payload["title"] == "Café ☕ Ålert"
    assert payload["body"] == "Grüße — line2\nunicode ✓"
    assert payload["url"] == "https://example.com/x"
    assert payload["channel"] == "Test Channel"
    assert payload["tag"] and payload["ts"] > 0

    # VAPID Authorization header is present and signature-valid
    claims = parse_vapid_authorization(headers["authorization"])
    assert claims["sub"] == "mailto:test@example.invalid"
    assert claims["aud"] == push_service.base  # endpoint origin
    assert claims["exp"] > 0


def test_push_fans_out_to_every_device(server, channel, push_service):
    devices = [FakeDevice(push_service, f"d{i}") for i in range(5)]
    for dev in devices:
        _attach(channel, dev)

    resp = server.post("/api/message", {"code": channel, "title": "Broadcast"})
    assert resp.json["sent"] == 5

    for dev in devices:
        reqs = push_service.requests_for(f"/push/{dev.name}")
        assert len(reqs) == 1
        assert dev.decrypt(reqs[0]["body"])["title"] == "Broadcast"


def test_timed_channel_delivers_push_with_future_exp(server, push_service):
    """A channel created WITH an auto-remove date must deliver pushes exactly
    like a normal channel while it is alive; the payload carries the future
    `exp` so the SW can suppress only genuinely late deliveries."""
    resp = server.post("/api/channel", {"name": "Timed", "auto_remove_days": 30})
    assert resp.status == 200
    code = resp.json["code"]
    dev = FakeDevice(push_service, "timedphone")
    _attach(code, dev)

    r = server.post("/api/message", {"code": code, "title": "Ping", "body": "now"})
    assert r.status == 200
    assert r.json["sent"] == 1 and r.json["failed"] == 0

    payload = dev.decrypt(push_service.requests_for("/push/timedphone")[0]["body"])
    assert payload["title"] == "Ping"
    assert payload["exp"] == resp.json["expires"]  # future end date rides along
    assert payload["exp"] > payload["ts"]  # sanity: not already expired


def test_gone_endpoint_returns_410_and_is_pruned(server, channel, push_service):
    live = FakeDevice(push_service, "live")
    gone = FakeDevice(push_service, "gone")
    _attach(channel, live)
    _attach(channel, gone)
    push_service.set_status("/push/gone", 410)

    resp = server.post("/api/message", {"code": channel, "title": "hi"})
    assert resp.json["sent"] == 1
    assert resp.json["pruned"] == 1
    assert resp.json["failed"] == 0

    # the gone endpoint was removed; the live one remains
    snap = server.post("/api/messages", {"code": channel}).json
    assert snap["subscribers"] == 1
    # a second send only reaches the live device
    push_service.received.clear()
    server.post("/api/message", {"code": channel, "title": "again"})
    assert len(push_service.requests_for("/push/gone")) == 0
    assert len(push_service.requests_for("/push/live")) == 1


def test_server_error_from_push_service_counts_as_failed_not_pruned(
    server, channel, push_service
):
    dev = FakeDevice(push_service, "flaky")
    _attach(channel, dev)
    push_service.set_status("/push/flaky", 500)

    resp = server.post("/api/message", {"code": channel, "title": "hi"})
    assert resp.json["failed"] == 1
    assert resp.json["pruned"] == 0
    # still subscribed (a 5xx is transient, must not drop the endpoint)
    assert server.post("/api/messages", {"code": channel}).json["subscribers"] == 1


def test_long_unicode_body_is_bounded_but_still_delivered(server, channel, push_service):
    """A 2000-char CJK body would blow past the 4 KB Web Push limit if sent
    verbatim (ensure_ascii escaping makes it ~12 KB). It must be byte-bounded
    for the push yet stored in full for the message list."""
    dev = FakeDevice(push_service, "cjk")
    _attach(channel, dev)
    big_body = "あ" * 2000  # 6000 UTF-8 bytes, ~12000 with \\uXXXX escaping

    resp = server.post(
        "/api/message", {"code": channel, "title": "T", "body": big_body}
    )
    assert resp.status == 200
    assert resp.json["sent"] == 1  # delivered, not rejected as oversized

    payload = dev.decrypt(push_service.requests_for("/push/cjk")[0]["body"])
    # pushed body is bounded by UTF-8 bytes and remains valid text
    assert len(payload["body"].encode("utf-8")) <= core.PUSH_BODY_MAX_BYTES + 8
    assert payload["body"].startswith("あ")
    # the FULL body is preserved for the in-app message list
    snap = server.post("/api/messages", {"code": channel}).json
    assert snap["messages"][0]["body"] == big_body


def test_whole_encrypted_body_stays_under_web_push_limit(server, channel, push_service):
    dev = FakeDevice(push_service, "size")
    _attach(channel, dev)
    server.post(
        "/api/message",
        {"code": channel, "title": "T" * 120, "body": "𐍈" * 2000},
    )
    body = push_service.requests_for("/push/size")[0]["body"]
    assert len(body) <= 4096  # hard Web Push ceiling

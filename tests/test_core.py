"""Unit tests for notify_core: codes, validation, rate limiting, storage."""
import json
import time

import pytest

import notify_core as core


# ------------------------------------------------------------ codes


def test_generated_codes_are_valid_and_unique():
    codes = {core.generate_code() for _ in range(200)}
    assert len(codes) == 200
    for code in codes:
        assert core.valid_code(code)
        assert len(code) == 32


@pytest.mark.parametrize(
    "bad",
    [
        None,
        123,
        "",
        "short",
        "a" * 15,
        "a" * 65,
        "has space aaaaaaaaaa",
        "slash/aaaaaaaaaaaaaa",
        "umlautäaaaaaaaaaaaaaa",
        "newline\naaaaaaaaaaaa",
    ],
)
def test_invalid_codes_rejected(bad):
    assert not core.valid_code(bad)


def test_code_hash_is_stable_and_not_the_code():
    code = core.generate_code()
    assert core.code_hash(code) == core.code_hash(code)
    assert code not in core.code_hash(code)
    assert len(core.code_hash(code)) == 64


# ------------------------------------------------------------ validation


def test_clean_channel_name():
    assert core.clean_channel_name(None) == ""
    assert core.clean_channel_name("  My \r\n Channel  ") == "My Channel"
    with pytest.raises(ValueError):
        core.clean_channel_name("x" * 81)
    with pytest.raises(ValueError):
        core.clean_channel_name(123)


def test_validate_message_minimal():
    msg = core.validate_message({"title": " Hello "})
    assert msg == {"title": "Hello", "body": "", "url": ""}


def test_validate_message_full():
    msg = core.validate_message(
        {"title": "T", "body": "line1\nline2", "url": "https://example.com/x"}
    )
    assert msg["body"] == "line1\nline2"
    assert msg["url"] == "https://example.com/x"


def test_message_title_optional_short_body_becomes_title():
    m = core.validate_message({"body": "Kickoff at 10am, bring cleats"})
    assert m["title"] == "Kickoff at 10am, bring cleats"
    assert m["body"] == "Kickoff at 10am, bring cleats"


def test_message_title_derived_and_truncated_from_long_body():
    body = "word " * 60  # 300 chars
    m = core.validate_message({"body": body})
    assert m["title"].endswith("…")
    assert len(m["title"]) <= core.TITLE_SNIPPET + 2
    assert m["body"] == body.strip()


def test_message_title_derived_from_first_line():
    m = core.validate_message({"body": "Line one\nLine two\nLine three"})
    assert m["title"] == "Line one"
    assert m["body"] == "Line one\nLine two\nLine three"


def test_message_requires_title_or_body():
    for payload in [{}, {"title": ""}, {"title": "   "}, {"body": "  "}, {"title": "", "body": ""}]:
        with pytest.raises(ValueError):
            core.validate_message(payload)


def test_validate_message_strips_control_chars():
    msg = core.validate_message({"title": "a\x00b\x1fc", "body": "x\rY\nz\x00"})
    assert msg["title"] == "abc"
    assert msg["body"] == "xY\nz"


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"title": ""},
        {"title": "   "},
        {"title": 5},
        {"title": "x" * 121},
        {"title": "ok", "body": 5},
        {"title": "ok", "body": "x" * 2001},
        {"title": "ok", "url": "ftp://example.com"},
        {"title": "ok", "url": "javascript:alert(1)"},
        {"title": "ok", "url": "https://exa mple.com"},
        {"title": "ok", "url": "https://" + "x" * 500},
    ],
)
def test_validate_message_rejects(payload):
    with pytest.raises(ValueError):
        core.validate_message(payload)


def test_validate_subscription_normalizes():
    sub = core.validate_subscription(
        {
            "endpoint": "https://push.example.invalid/x",
            "expirationTime": None,
            "keys": {"p256dh": "BPtestkey0123456789", "auth": "authauth12345678", "extra": "y"},
            "junk": True,
        }
    )
    assert sub == {
        "endpoint": "https://push.example.invalid/x",
        "keys": {"p256dh": "BPtestkey0123456789", "auth": "authauth12345678"},
    }


@pytest.mark.parametrize(
    "sub",
    [
        None,
        "string",
        {},
        {"endpoint": "http://insecure.example/x", "keys": {"p256dh": "a" * 20, "auth": "b" * 20}},
        {"endpoint": "https://x.example/" + "e" * 1000, "keys": {"p256dh": "a" * 20, "auth": "b" * 20}},
        {"endpoint": "https://x.example/ok"},
        {"endpoint": "https://x.example/ok", "keys": {"p256dh": "bad key!", "auth": "b" * 20}},
        {"endpoint": "https://x.example/ok", "keys": {"p256dh": "a" * 20}},
    ],
)
def test_validate_subscription_rejects(sub):
    with pytest.raises(ValueError):
        core.validate_subscription(sub)


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://127.0.0.1/push/x",
        "https://127.0.0.1:8080/push/x",
        "https://[::1]/push/x",
        "https://localhost/push/x",
        "https://sub.localhost/push/x",
        "https://10.0.0.5/push/x",
        "https://192.168.1.1/push/x",
        "https://169.254.169.254/latest/meta-data",  # cloud metadata
        "https://172.16.5.5/x",
        "https://0.0.0.0/x",
    ],
)
def test_validate_subscription_blocks_ssrf_endpoints(endpoint):
    sub = {"endpoint": endpoint, "keys": {"p256dh": "a" * 20, "auth": "b" * 20}}
    with pytest.raises(ValueError):
        core.validate_subscription(sub)


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://fcm.googleapis.com/fcm/send/abc123",
        "https://updates.push.services.mozilla.com/wpush/v2/xyz",
        "https://web.push.apple.com/AB12",
        "https://push.example.invalid/send/1",  # public-looking hostname
    ],
)
def test_validate_subscription_allows_real_push_hosts(endpoint):
    sub = {"endpoint": endpoint, "keys": {"p256dh": "a" * 20, "auth": "b" * 20}}
    assert core.validate_subscription(sub)["endpoint"] == endpoint


def test_control_char_only_title_is_rejected():
    with pytest.raises(ValueError):
        core.validate_message({"title": "\x00\x1f\x08"})
    with pytest.raises(ValueError):
        core.validate_message({"title": "\x7f\x9b"})  # DEL + C1


def test_clean_channel_name_strips_control_chars():
    assert core.clean_channel_name("My\x00Chan\x1bnel") == "MyChannel"
    assert core.clean_channel_name("a\x7fb\x9bc") == "abc"


def test_truncate_utf8_never_splits_a_codepoint():
    text = "あ" * 100  # 3 bytes each
    out = core._truncate_utf8(text, 10)
    assert out.encode("utf-8")[:9].decode("utf-8")  # valid, no partial char
    assert len(out.encode("utf-8")) <= 10 + 4  # + ellipsis
    assert core._truncate_utf8("short", 100) == "short"


# --------------------------------------------------------- rate limiting


def test_rate_limiter_blocks_after_limit():
    rl = core.RateLimiter()
    assert all(rl.allow("ip1", 3) for _ in range(3))
    assert not rl.allow("ip1", 3)
    assert rl.allow("ip2", 3)  # other keys unaffected


def test_rate_limiter_window_expires(monkeypatch):
    # deterministic clock (no sleeps → no CI timing flakes)
    now = {"t": 1000.0}
    monkeypatch.setattr(core.time, "monotonic", lambda: now["t"])
    rl = core.RateLimiter()
    assert rl.allow("ip", 1, window=60)
    assert not rl.allow("ip", 1, window=60)
    now["t"] += 61
    assert rl.allow("ip", 1, window=60)


def test_rate_limiter_size_is_bounded(monkeypatch):
    now = {"t": 0.0}
    monkeypatch.setattr(core.time, "monotonic", lambda: now["t"])
    rl = core.RateLimiter()
    rl.MAX_KEYS = 100
    for i in range(500):
        now["t"] += 0.001  # all within one window
        rl.allow(f"ip-{i}", 5)
    # the table is deterministically bounded, not unbounded growth
    assert len(rl._hits) <= rl.MAX_KEYS + 1


def test_config_env_parsing(monkeypatch):
    monkeypatch.setenv("NBW_RATE_PER_MIN", "50")
    monkeypatch.setenv("NBW_MAX_SUBS_PER_CHANNEL", "7")
    monkeypatch.setenv("NBW_MAX_MESSAGES", "9")
    monkeypatch.setenv("NBW_MAX_CHANNELS_PER_MIN", "3")
    assert core.rate_per_min() == 50
    assert core.max_subs_per_channel() == 7
    assert core.max_messages() == 9
    assert core.max_channels_per_min() == 3
    monkeypatch.setenv("NBW_RATE_PER_MIN", "garbage")
    assert core.rate_per_min() == 120  # falls back to default
    monkeypatch.setenv("NBW_MAX_MESSAGES", "0")
    assert core.max_messages() == 1  # clamped to >= 1


# --------------------------------------------------------------- storage


def test_memory_storage_channel_lifecycle():
    st = core.MemoryStorage()
    assert st.create_channel("kh1", '{"name":"a"}')
    assert not st.create_channel("kh1", '{"name":"b"}')  # already exists
    assert st.get_channel("kh1") == '{"name":"a"}'
    assert st.get_channel("nope") is None


def test_memory_storage_subs():
    st = core.MemoryStorage()
    st.create_channel("kh", "{}")
    st.put_sub("kh", "e1", "s1")
    st.put_sub("kh", "e2", "s2")
    st.put_sub("kh", "e1", "s1b")  # update, not duplicate
    assert st.sub_count("kh") == 2
    assert sorted(st.get_subs("kh")) == ["s1b", "s2"]
    assert st.delete_sub("kh", "e1")
    assert not st.delete_sub("kh", "e1")
    assert st.sub_count("kh") == 1
    assert st.get_subs("missing") == []
    assert st.sub_count("missing") == 0


def test_memory_storage_message_cap_and_order():
    st = core.MemoryStorage()
    st.create_channel("kh", "{}")
    for i in range(5):
        st.add_message("kh", f"m{i}", cap=3)
    assert st.get_messages("kh", 10) == ["m4", "m3", "m2"]
    assert st.get_messages("kh", 2) == ["m4", "m3"]
    assert st.get_messages("missing", 5) == []


def test_memory_storage_delete_and_clear():
    st = core.MemoryStorage()
    st.create_channel("kh", "{}")
    st.add_message("kh", '{"id":"a"}', 50)
    st.add_message("kh", '{"id":"b"}', 50)
    assert st.delete_message("kh", "a") is True
    assert st.delete_message("kh", "a") is False  # already gone
    assert [core._msg_id_of(m) for m in st.get_messages("kh", 10)] == ["b"]
    assert st.clear_messages("kh") == 1
    assert st.get_messages("kh", 10) == []


class FakeRedis(core.RedisStorage):
    def __init__(self, responses):
        super().__init__("https://fake.example.invalid", "token")
        self.calls = []
        self.responses = list(responses)

    def _pipeline(self, commands):
        self.calls.append(commands)
        return self.responses.pop(0)


def test_redis_storage_create_channel_commands():
    st = FakeRedis([["OK"], [None]])
    assert st.create_channel("kh", '{"n":1}')
    assert not st.create_channel("kh", '{"n":1}')
    cmd = st.calls[0][0]
    assert cmd[0] == "SET"
    assert cmd[1] == "nbw:meta:kh"
    assert "NX" in cmd and "EX" in cmd


def test_redis_storage_message_commands():
    st = FakeRedis([[1, "OK", 1]])
    st.add_message("kh", "msg", cap=50)
    cmds = st.calls[0]
    assert cmds[0][:2] == ["LPUSH", "nbw:msgs:kh"]
    assert cmds[1] == ["LTRIM", "nbw:msgs:kh", "0", "49"]
    assert cmds[2][0] == "EXPIRE"


def test_redis_storage_reads_handle_empty():
    st = FakeRedis([[None], [None], [0]])
    assert st.get_subs("kh") == []
    assert st.get_messages("kh", 5) == []
    assert st.sub_count("kh") == 0


def test_get_storage_picks_backend(monkeypatch):
    for var in (
        "KV_REST_API_URL",
        "KV_REST_API_TOKEN",
        "UPSTASH_REDIS_REST_URL",
        "UPSTASH_REDIS_REST_TOKEN",
    ):
        monkeypatch.delenv(var, raising=False)
    core.reset_storage_for_tests()
    assert isinstance(core.get_storage(), core.MemoryStorage)
    core.reset_storage_for_tests()
    monkeypatch.setenv("KV_REST_API_URL", "https://fake.example.invalid")
    monkeypatch.setenv("KV_REST_API_TOKEN", "token")
    assert isinstance(core.get_storage(), core.RedisStorage)
    core.reset_storage_for_tests()


def test_storage_status_memory(env):
    core.reset_storage_for_tests()
    assert core.storage_status() == {"backend": "memory", "reachable": True}


def test_storage_status_redis_unreachable(monkeypatch):
    class DeadRedis(core.RedisStorage):
        def ping(self):
            raise core.StorageError("down")

    monkeypatch.setattr(core, "get_storage", lambda: DeadRedis("http://x", "t"))
    assert core.storage_status() == {"backend": "redis", "reachable": False}


def test_diagnostics_reports_config_and_hides_secrets(env):
    core.reset_storage_for_tests()
    d = core.diagnostics()
    assert d["ok"] is True
    assert d["push"]["configured"] is True  # env fixture sets VAPID keys
    assert d["storage"] == {"backend": "memory", "reachable": True}
    assert d["limits"]["max_subs_per_channel"] == 200
    assert "env" in d and "commit" in d and "region" in d
    priv, _pub, _subj = core.vapid_config()
    blob = json.dumps(d)
    assert priv not in blob  # never leak the private key
    assert "token" not in blob.lower()


def test_diagnostics_push_disabled_without_key(env):
    env.setenv("VAPID_PRIVATE_KEY", "")
    core.reset_storage_for_tests()
    assert core.diagnostics()["push"]["configured"] is False


def test_diagnostics_surfaces_vercel_markers(env, monkeypatch):
    monkeypatch.setenv("VERCEL_ENV", "production")
    monkeypatch.setenv("VERCEL_GIT_COMMIT_SHA", "abcdef1234567890")
    monkeypatch.setenv("VERCEL_REGION", "fra1")
    core.reset_storage_for_tests()
    d = core.diagnostics()
    assert d["env"] == "production"
    assert d["commit"] == "abcdef1"
    assert d["region"] == "fra1"


def test_create_channel_stores_only_hash(env):
    # env fixture clears ALL four storage env vars (KV_* and UPSTASH_*) and
    # resets the singleton in teardown, so this can never touch a real Redis.
    core.reset_storage_for_tests()
    storage = core.get_storage()
    assert isinstance(storage, core.MemoryStorage)
    result = core.create_channel("My Channel")
    code = result["code"]
    assert storage.get_channel(core.code_hash(code)) is not None
    assert code not in storage._ch  # raw code is never a storage key


def test_clean_send_password():
    assert core.clean_send_password(None) == ""
    assert core.clean_send_password("   ") == ""
    assert core.clean_send_password("  event key  ") == "event key"
    for bad in ["abc", "x" * 129, 5]:
        with pytest.raises(ValueError):
            core.clean_send_password(bad)


def test_send_password_gates_publish(env):
    core.reset_storage_for_tests()
    ch = core.create_channel("Event", send_password="manager-key")
    assert ch["send_protected"] is True
    code = ch["code"]
    # only a hash is stored, never the raw password
    meta = json.loads(core.get_storage().get_channel(core.code_hash(code)))
    assert "manager-key" not in json.dumps(meta)
    assert meta["send_pw"] == core._send_password_hash("manager-key")

    msg = core.validate_message({"title": "hi"})
    with pytest.raises(core.SendForbidden):
        core.publish(code, msg)  # no password
    with pytest.raises(core.SendForbidden):
        core.publish(code, msg, "wrong")
    assert core.publish(code, msg, "manager-key")["stored"] is True


def test_unprotected_channel_ignores_send_password(env):
    core.reset_storage_for_tests()
    ch = core.create_channel("Open")
    assert ch["send_protected"] is False
    code = ch["code"]
    msg = core.validate_message({"title": "hi"})
    assert core.publish(code, msg, "whatever")["stored"] is True
    assert core.publish(code, msg)["stored"] is True


def test_snapshot_reports_send_protected(env):
    core.reset_storage_for_tests()
    prot = core.create_channel("P", send_password="phrase")["code"]
    openc = core.create_channel("O")["code"]
    assert core.channel_snapshot(prot, 5)["channel"]["send_protected"] is True
    assert core.channel_snapshot(openc, 5)["channel"]["send_protected"] is False


def test_delete_and_clear_messages(env):
    core.reset_storage_for_tests()
    code = core.create_channel("C")["code"]
    id1 = core.publish(code, core.validate_message({"title": "one"}))["message"]["id"]
    core.publish(code, core.validate_message({"title": "two"}))
    assert len(core.channel_snapshot(code, 10)["messages"]) == 2
    assert core.delete_message(code, id1) is True
    assert [m["title"] for m in core.channel_snapshot(code, 10)["messages"]] == ["two"]
    assert core.delete_message(code, "nonexistent") is False
    assert core.clear_messages(code) is True
    assert core.channel_snapshot(code, 10)["messages"] == []


def test_delete_message_bad_id_and_unknown_channel(env):
    core.reset_storage_for_tests()
    code = core.create_channel("C")["code"]
    with pytest.raises(ValueError):
        core.delete_message(code, "")
    ghost = core.generate_code()
    assert core.delete_message(ghost, "x") is None
    assert core.clear_messages(ghost) is None


def test_delete_requires_password_on_protected_channel(env):
    core.reset_storage_for_tests()
    code = core.create_channel("P", send_password="phrase-key")["code"]
    mid = core.publish(
        code, core.validate_message({"title": "x"}), "phrase-key"
    )["message"]["id"]
    with pytest.raises(core.SendForbidden):
        core.delete_message(code, mid)
    with pytest.raises(core.SendForbidden):
        core.clear_messages(code)
    assert core.delete_message(code, mid, "phrase-key") is True

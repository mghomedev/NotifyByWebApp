"""Core logic for NotifyByWebApp.

Design rules (see CLAUDE.md):
- A channel code is a bearer capability. The server stores only
  sha256(code) ("kh", key hash) — never the raw code — so a storage leak
  does not leak the ability to send.
- Codes never appear in URLs; every API call carries them in a POST body,
  keeping them out of platform request logs.
- No personal data: a subscription is just the browser push endpoint plus
  its crypto keys; messages are whatever a sender posts (size-capped).
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import threading
import time
import unicodedata
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import requests
from pywebpush import WebPushException, webpush

# --------------------------------------------------------------- config

CODE_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
B64ISH_RE = re.compile(r"^[A-Za-z0-9+/_=-]{10,400}$")

MAX_NAME = 80
MAX_TITLE = 120
TITLE_SNIPPET = 60  # chars taken from the body when no title is given
MAX_BODY = 2000
MAX_URL = 500
MAX_ENDPOINT = 1000
PUSH_TTL_SECONDS = 86400

# Web Push services hard-cap the encrypted payload at 4096 bytes. Keep the
# JSON envelope comfortably under that after AES128GCM overhead by bounding
# the pushed (not stored) text by UTF-8 byte length.
PUSH_BODY_MAX_BYTES = 1400
PUSH_TITLE_MAX_BYTES = 300
PUSH_CHANNEL_MAX_BYTES = 200


def _int_env(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name) or default))
    except ValueError:
        return default


def rate_per_min() -> int:
    return _int_env("NBW_RATE_PER_MIN", 120)


def max_channels_per_min() -> int:
    return _int_env("NBW_MAX_CHANNELS_PER_MIN", 60)


def max_subs_per_channel() -> int:
    return _int_env("NBW_MAX_SUBS_PER_CHANNEL", 200)


def max_messages() -> int:
    return _int_env("NBW_MAX_MESSAGES", 50)


def channel_ttl_seconds() -> int:
    return _int_env("NBW_CHANNEL_TTL_DAYS", 400) * 86400


def vapid_config() -> "tuple[str, str, str]":
    """(private_key, public_key, subject) — empty strings when unset."""
    return (
        os.environ.get("VAPID_PRIVATE_KEY", "").strip(),
        os.environ.get("VAPID_PUBLIC_KEY", "").strip(),
        os.environ.get("VAPID_SUBJECT", "").strip() or "mailto:admin@example.invalid",
    )


def status_secret() -> str:
    """Shared secret that gates GET /api/status. Empty → diagnostics disabled."""
    return os.environ.get("NBW_STATUS_SECRET", "").strip()


class StorageError(Exception):
    """The persistent store is unreachable or returned an error."""


class ChannelFull(Exception):
    """Subscriber cap for this channel reached."""


class SendForbidden(Exception):
    """The channel requires a send password that was missing or wrong."""


# ---------------------------------------------------------- codes & keys


def generate_code() -> str:
    """32-char URL-safe code, 192 bits of randomness."""
    return secrets.token_urlsafe(24)


def valid_code(code: object) -> bool:
    return isinstance(code, str) and bool(CODE_RE.fullmatch(code))


def code_hash(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def endpoint_hash(endpoint: str) -> str:
    return hashlib.sha256(endpoint.encode("utf-8")).hexdigest()


# ------------------------------------------------------------ validation


def _clean_text(value: str, allow_newlines: bool = False) -> str:
    """Drop Unicode control characters (C0, C1, DEL) — optionally keep \\n."""
    out = []
    for ch in value:
        if ch == "\n":
            if allow_newlines:
                out.append(ch)
            continue
        if unicodedata.category(ch) == "Cc":
            continue
        out.append(ch)
    return "".join(out)


def clean_channel_name(name: object) -> str:
    if name is None:
        return ""
    if not isinstance(name, str):
        raise ValueError("name must be a string")
    name = " ".join(_clean_text(name).split())
    if len(name) > MAX_NAME:
        raise ValueError(f"name too long (max {MAX_NAME} characters)")
    return name


def clean_send_password(pw: object) -> str:
    """Normalize an optional send-password; returns '' when none is set."""
    if pw is None:
        return ""
    if not isinstance(pw, str):
        raise ValueError("send_password must be a string")
    pw = _clean_text(pw).strip()
    if not pw:
        return ""
    if len(pw) < 4:
        raise ValueError("send password too short (min 4 characters)")
    if len(pw) > 128:
        raise ValueError("send password too long (max 128 characters)")
    return pw


def _send_password_hash(pw: str) -> str:
    return hashlib.sha256(pw.encode("utf-8")).hexdigest()


def _require_send_password(meta: dict, send_password: object) -> None:
    """Raise SendForbidden unless the channel is unprotected or the password
    matches. Used for every mutating channel operation (send/delete/clear)."""
    required = meta.get("send_pw")
    if required:
        provided = send_password if isinstance(send_password, str) else ""
        if not hmac.compare_digest(_send_password_hash(provided), required):
            raise SendForbidden()


def _msg_id_of(raw: str) -> "str | None":
    try:
        return json.loads(raw).get("id")
    except (json.JSONDecodeError, AttributeError, TypeError):
        return None


def _derive_title(body: str) -> str:
    """Make a title from the body's first line when none was given."""
    first = body.split("\n", 1)[0].strip() or body.strip()
    if len(first) <= TITLE_SNIPPET:
        return first
    return first[:TITLE_SNIPPET].rstrip() + "…"


def validate_message(payload: dict) -> dict:
    """Returns {"title","body","url"} or raises ValueError. The title is
    optional: if omitted, one is derived from the body. At least one of
    title/body must be present."""
    title = payload.get("title", "")
    if title is None:
        title = ""
    if not isinstance(title, str):
        raise ValueError("title must be a string")
    title = _clean_text(title).strip()
    if len(title) > MAX_TITLE:
        raise ValueError(f"title too long (max {MAX_TITLE} characters)")

    body = payload.get("body", "")
    if body is None:
        body = ""
    if not isinstance(body, str):
        raise ValueError("body must be a string")
    body = _clean_text(body, allow_newlines=True).strip()
    if len(body) > MAX_BODY:
        raise ValueError(f"body too long (max {MAX_BODY} characters)")

    if not title and not body:
        raise ValueError("a title or a message body is required")
    if not title:
        title = _derive_title(body)

    url = payload.get("url", "")
    if url is None:
        url = ""
    if not isinstance(url, str):
        raise ValueError("url must be a string")
    url = url.strip()
    if url:
        if len(url) > MAX_URL:
            raise ValueError(f"url too long (max {MAX_URL} characters)")
        if not (url.startswith("https://") or url.startswith("http://")):
            raise ValueError("url must start with http:// or https://")
        if any(ch <= " " for ch in url):
            raise ValueError("url must not contain spaces or control characters")

    return {"title": title, "body": body, "url": url}


def _endpoint_host_blocked(endpoint: str) -> bool:
    """SSRF guard: block push endpoints that point at the loopback,
    private, link-local or otherwise-internal address space. We do not
    resolve DNS here (avoids latency and false negatives for the real push
    services); the direct IP-literal and localhost vectors are what an
    attacker controls, and redirects are disabled on the send side."""
    host = (urllib.parse.urlsplit(endpoint).hostname or "").strip().lower()
    if not host:
        return True
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False  # a real hostname (e.g. fcm.googleapis.com)
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def validate_subscription(sub: object) -> dict:
    """Returns a normalized PushSubscription dict or raises ValueError."""
    if not isinstance(sub, dict):
        raise ValueError("subscription must be an object")
    endpoint = sub.get("endpoint")
    if not isinstance(endpoint, str) or not endpoint.startswith("https://"):
        raise ValueError("subscription endpoint must be an https URL")
    if len(endpoint) > MAX_ENDPOINT or any(ch <= " " for ch in endpoint):
        raise ValueError("invalid subscription endpoint")
    if _endpoint_host_blocked(endpoint):
        raise ValueError("subscription endpoint host is not allowed")
    keys = sub.get("keys")
    if not isinstance(keys, dict):
        raise ValueError("subscription keys missing")
    p256dh = keys.get("p256dh")
    auth = keys.get("auth")
    if not (isinstance(p256dh, str) and B64ISH_RE.fullmatch(p256dh)):
        raise ValueError("invalid subscription key p256dh")
    if not (isinstance(auth, str) and B64ISH_RE.fullmatch(auth)):
        raise ValueError("invalid subscription key auth")
    return {"endpoint": endpoint, "keys": {"p256dh": p256dh, "auth": auth}}


# ---------------------------------------------------------- rate limiting


class RateLimiter:
    """Sliding-window per-key limiter. Best effort: per warm instance only
    (a hard platform-level limit belongs in the Vercel Firewall)."""

    MAX_KEYS = 10000
    PRUNE_INTERVAL = 5.0

    def __init__(self) -> None:
        self._hits: "dict[str, list[float]]" = {}
        self._lock = threading.Lock()
        self._last_prune = 0.0

    def _prune_locked(self, now: float, window: float) -> None:
        self._hits = {
            k: kept
            for k, v in self._hits.items()
            if (kept := [t for t in v if now - t < window])
        }
        # Deterministic bound: if still oversized, drop the least-recently-hit.
        if len(self._hits) > self.MAX_KEYS:
            ordered = sorted(self._hits.items(), key=lambda kv: max(kv[1]))
            for k, _ in ordered[: len(self._hits) - self.MAX_KEYS]:
                del self._hits[k]
        self._last_prune = now

    def allow(self, key: str, limit: int, window: float = 60.0) -> bool:
        now = time.monotonic()
        with self._lock:
            if (
                now - self._last_prune > self.PRUNE_INTERVAL
                or len(self._hits) > self.MAX_KEYS
            ):
                self._prune_locked(now, window)
            hits = [t for t in self._hits.get(key, []) if now - t < window]
            if len(hits) >= limit:
                self._hits[key] = hits
                return False
            hits.append(now)
            self._hits[key] = hits
            return True


limiter = RateLimiter()


# --------------------------------------------------------------- storage


class MemoryStorage:
    """Per-process store: for tests/local dev, and the explicit degraded
    mode when no Redis is configured (data lost on instance recycling)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._ch: "dict[str, dict]" = {}

    def _entry(self, kh: str) -> "dict | None":
        return self._ch.get(kh)

    def create_channel(self, kh: str, meta_json: str) -> bool:
        with self._lock:
            if kh in self._ch:
                return False
            self._ch[kh] = {"meta": meta_json, "subs": {}, "msgs": []}
            return True

    def get_channel(self, kh: str) -> "str | None":
        with self._lock:
            e = self._entry(kh)
            return e["meta"] if e else None

    def put_sub(self, kh: str, eh: str, sub_json: str) -> None:
        with self._lock:
            e = self._entry(kh)
            if e is not None:
                e["subs"][eh] = sub_json

    def sub_exists(self, kh: str, eh: str) -> bool:
        with self._lock:
            e = self._entry(kh)
            return bool(e) and eh in e["subs"]

    def delete_sub(self, kh: str, eh: str) -> bool:
        with self._lock:
            e = self._entry(kh)
            if e is not None and eh in e["subs"]:
                del e["subs"][eh]
                return True
            return False

    def get_subs(self, kh: str) -> "list[str]":
        with self._lock:
            e = self._entry(kh)
            return list(e["subs"].values()) if e else []

    def sub_count(self, kh: str) -> int:
        with self._lock:
            e = self._entry(kh)
            return len(e["subs"]) if e else 0

    def add_message(self, kh: str, msg_json: str, cap: int) -> None:
        with self._lock:
            e = self._entry(kh)
            if e is not None:
                e["msgs"].insert(0, msg_json)
                del e["msgs"][cap:]

    def get_messages(self, kh: str, limit: int) -> "list[str]":
        with self._lock:
            e = self._entry(kh)
            return list(e["msgs"][:limit]) if e else []

    def delete_message(self, kh: str, msg_id: str) -> bool:
        with self._lock:
            e = self._entry(kh)
            if not e:
                return False
            before = len(e["msgs"])
            e["msgs"] = [m for m in e["msgs"] if _msg_id_of(m) != msg_id]
            return len(e["msgs"]) < before

    def clear_messages(self, kh: str, keep: int = 0) -> int:
        with self._lock:
            e = self._entry(kh)
            if not e:
                return 0
            if keep > 0:
                removed = max(0, len(e["msgs"]) - keep)
                del e["msgs"][keep:]
                return removed
            n = len(e["msgs"])
            e["msgs"] = []
            return n

    def touch(self, kh: str, ttl: int) -> None:
        pass

    def ping(self) -> bool:
        return True


class RedisStorage:
    """Upstash Redis over its REST API (works well from serverless — no
    connection pooling worries). Only a handful of commands are used."""

    def __init__(self, url: str, token: str) -> None:
        self._url = url.rstrip("/")
        self._token = token

    def _pipeline(self, commands: "list[list]") -> list:
        body = json.dumps(commands).encode("utf-8")
        req = urllib.request.Request(
            self._url + "/pipeline",
            data=body,
            method="POST",
            headers={
                "Authorization": "Bearer " + self._token,
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                results = json.loads(resp.read().decode("utf-8"))
        except StorageError:
            raise
        except Exception as exc:  # URLError, timeout, JSON errors, HTTPError
            raise StorageError("storage request failed") from exc
        if not isinstance(results, list):
            raise StorageError("unexpected storage response")
        out = []
        for item in results:
            if isinstance(item, dict) and "error" in item:
                raise StorageError("storage command failed")
            out.append(item.get("result") if isinstance(item, dict) else None)
        return out

    @staticmethod
    def _k(kind: str, kh: str) -> str:
        return f"nbw:{kind}:{kh}"

    def create_channel(self, kh: str, meta_json: str) -> bool:
        ttl = str(channel_ttl_seconds())
        res = self._pipeline(
            [["SET", self._k("meta", kh), meta_json, "NX", "EX", ttl]]
        )
        return res[0] == "OK"

    def get_channel(self, kh: str) -> "str | None":
        return self._pipeline([["GET", self._k("meta", kh)]])[0]

    def put_sub(self, kh: str, eh: str, sub_json: str) -> None:
        ttl = str(channel_ttl_seconds())
        self._pipeline(
            [
                ["HSET", self._k("subs", kh), eh, sub_json],
                ["EXPIRE", self._k("subs", kh), ttl],
            ]
        )

    def sub_exists(self, kh: str, eh: str) -> bool:
        return bool(self._pipeline([["HEXISTS", self._k("subs", kh), eh]])[0])

    def delete_sub(self, kh: str, eh: str) -> bool:
        res = self._pipeline([["HDEL", self._k("subs", kh), eh]])
        return bool(res[0])

    def get_subs(self, kh: str) -> "list[str]":
        return self._pipeline([["HVALS", self._k("subs", kh)]])[0] or []

    def sub_count(self, kh: str) -> int:
        return int(self._pipeline([["HLEN", self._k("subs", kh)]])[0] or 0)

    def add_message(self, kh: str, msg_json: str, cap: int) -> None:
        ttl = str(channel_ttl_seconds())
        self._pipeline(
            [
                ["LPUSH", self._k("msgs", kh), msg_json],
                ["LTRIM", self._k("msgs", kh), "0", str(cap - 1)],
                ["EXPIRE", self._k("msgs", kh), ttl],
            ]
        )

    def get_messages(self, kh: str, limit: int) -> "list[str]":
        return (
            self._pipeline([["LRANGE", self._k("msgs", kh), "0", str(limit - 1)]])[0]
            or []
        )

    def delete_message(self, kh: str, msg_id: str) -> bool:
        # find the exact stored JSON for this id, then remove that value
        for raw in self.get_messages(kh, 1000):
            if _msg_id_of(raw) == msg_id:
                res = self._pipeline([["LREM", self._k("msgs", kh), "0", raw]])
                return bool(res[0])
        return False

    def clear_messages(self, kh: str, keep: int = 0) -> int:
        if keep > 0:
            # keep the newest `keep` (index 0 is newest); drop the rest
            self._pipeline([["LTRIM", self._k("msgs", kh), "0", str(keep - 1)]])
            return 0  # LTRIM does not report how many were removed
        return int(self._pipeline([["DEL", self._k("msgs", kh)]])[0] or 0)

    def touch(self, kh: str, ttl: int) -> None:
        t = str(ttl)
        self._pipeline(
            [
                ["EXPIRE", self._k("meta", kh), t],
                ["EXPIRE", self._k("subs", kh), t],
                ["EXPIRE", self._k("msgs", kh), t],
            ]
        )

    def ping(self) -> bool:
        return self._pipeline([["PING"]])[0] == "PONG"


_storage = None
_storage_lock = threading.Lock()


def get_storage():
    global _storage
    with _storage_lock:
        if _storage is None:
            url = os.environ.get("KV_REST_API_URL") or os.environ.get(
                "UPSTASH_REDIS_REST_URL"
            )
            token = os.environ.get("KV_REST_API_TOKEN") or os.environ.get(
                "UPSTASH_REDIS_REST_TOKEN"
            )
            if url and token:
                _storage = RedisStorage(url, token)
            else:
                _storage = MemoryStorage()
        return _storage


def reset_storage_for_tests() -> None:
    global _storage
    with _storage_lock:
        _storage = None


def storage_status() -> dict:
    """Which storage backend is active and whether it answers a live ping.
    Never raises — an unreachable store reports reachable: false."""
    storage = get_storage()
    backend = "redis" if isinstance(storage, RedisStorage) else "memory"
    try:
        reachable = bool(storage.ping())
    except StorageError:
        reachable = False
    return {"backend": backend, "reachable": reachable}


def diagnostics() -> dict:
    """Operational status for GET /api/status. Reports config presence and
    live storage reachability but NEVER any secret value (no VAPID keys, no
    storage URL/token) and no per-channel data."""
    private_key, public_key, _subject = vapid_config()
    store = storage_status()
    commit = (os.environ.get("VERCEL_GIT_COMMIT_SHA") or "")[:7] or None
    return {
        "ok": store["reachable"],
        "service": "notify-by-web-app",
        "env": os.environ.get("VERCEL_ENV") or "local",
        "commit": commit,
        "region": os.environ.get("VERCEL_REGION") or None,
        "push": {"configured": bool(private_key and public_key)},
        "storage": store,
        "limits": {
            "rate_per_min": rate_per_min(),
            "max_channels_per_min": max_channels_per_min(),
            "max_subs_per_channel": max_subs_per_channel(),
            "max_messages": max_messages(),
            "channel_ttl_days": channel_ttl_seconds() // 86400,
        },
    }


# ------------------------------------------------------ domain operations


def create_channel(name: str, send_password: str = "") -> dict:
    storage = get_storage()
    for _ in range(3):
        code = generate_code()
        meta = {"name": name, "created": int(time.time())}
        if send_password:
            meta["send_pw"] = _send_password_hash(send_password)
        if storage.create_channel(code_hash(code), json.dumps(meta)):
            return {
                "code": code,
                "name": name,
                "created": meta["created"],
                "send_protected": bool(send_password),
            }
    raise StorageError("could not create channel")


def channel_snapshot(code: str, limit: int) -> "dict | None":
    """Channel meta + recent messages + subscriber count, or None."""
    storage = get_storage()
    kh = code_hash(code)
    meta_json = storage.get_channel(kh)
    if meta_json is None:
        return None
    meta = _load_stored(meta_json)
    messages = [_load_stored(m) for m in storage.get_messages(kh, limit)]
    return {
        "channel": {
            "name": meta.get("name", ""),
            "created": meta.get("created"),
            "send_protected": bool(meta.get("send_pw")),
        },
        "subscribers": storage.sub_count(kh),
        "messages": messages,
    }


def _load_stored(raw: str) -> dict:
    """json.loads for data read back from storage; a corrupt value is a
    server-side (storage) problem, not a client error."""
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        raise StorageError("corrupt stored data") from exc


def subscribe(code: str, subscription: object) -> "int | None":
    """Attach a push subscription to a channel. Returns subscriber count,
    None for unknown channels. Raises ValueError/ChannelFull.

    The cap is enforced write-then-verify so concurrent subscribers cannot
    overshoot it (each round trip is independent against Redis)."""
    sub = validate_subscription(subscription)
    storage = get_storage()
    kh = code_hash(code)
    if storage.get_channel(kh) is None:
        return None
    eh = endpoint_hash(sub["endpoint"])
    existed = storage.sub_exists(kh, eh)
    cap = max_subs_per_channel()
    if not existed and storage.sub_count(kh) >= cap:
        raise ChannelFull()
    storage.put_sub(kh, eh, json.dumps(sub))
    if not existed and storage.sub_count(kh) > cap:
        storage.delete_sub(kh, eh)
        raise ChannelFull()
    storage.touch(kh, channel_ttl_seconds())
    return storage.sub_count(kh)


def unsubscribe(code: str, endpoint: object) -> "bool | None":
    """Detach an endpoint. Returns removal status, None for unknown channels."""
    if not isinstance(endpoint, str) or not endpoint:
        raise ValueError("endpoint is required")
    storage = get_storage()
    kh = code_hash(code)
    if storage.get_channel(kh) is None:
        return None
    return storage.delete_sub(kh, endpoint_hash(endpoint))


def delete_message(code: str, msg_id: object, send_password: object = None) -> "bool | None":
    """Delete one stored message by id. None for unknown channels. Raises
    ValueError (bad id) / SendForbidden (protected channel, wrong password)."""
    if not isinstance(msg_id, str) or not msg_id:
        raise ValueError("message id is required")
    storage = get_storage()
    kh = code_hash(code)
    meta_json = storage.get_channel(kh)
    if meta_json is None:
        return None
    _require_send_password(_load_stored(meta_json), send_password)
    return storage.delete_message(kh, msg_id)


def clear_messages(
    code: str, send_password: object = None, keep: int = 0
) -> "bool | None":
    """Delete stored messages for a channel — all of them, or all but the
    newest `keep`. None for unknown channels. Raises SendForbidden for a
    protected channel with a wrong password."""
    storage = get_storage()
    kh = code_hash(code)
    meta_json = storage.get_channel(kh)
    if meta_json is None:
        return None
    _require_send_password(_load_stored(meta_json), send_password)
    storage.clear_messages(kh, keep if isinstance(keep, int) and keep > 0 else 0)
    return True


def _truncate_utf8(text: str, max_bytes: int) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode("utf-8", "ignore").rstrip() + "…"


def publish(code: str, message: dict, send_password: object = None) -> "dict | None":
    """Store a validated message and push it to all subscribers.
    Returns result counts, or None for unknown channels. Raises SendForbidden
    if the channel requires a send password and it is missing/wrong."""
    storage = get_storage()
    kh = code_hash(code)
    meta_json = storage.get_channel(kh)
    if meta_json is None:
        return None
    meta = _load_stored(meta_json)
    _require_send_password(meta, send_password)

    msg = {
        "id": secrets.token_hex(4),
        "ts": int(time.time()),
        "title": message["title"],
        "body": message["body"],
        "url": message["url"],
    }
    # Storing the message is the load-bearing side effect; if it fails the
    # caller gets 502 and may safely retry (nothing was delivered).
    storage.add_message(kh, json.dumps(msg), max_messages())

    result = {
        "stored": True,
        "sent": 0,
        "failed": 0,
        "pruned": 0,
        "push_disabled": False,
        "message": msg,
    }

    # From here on a storage hiccup must NOT turn into a 502 — the message is
    # already stored, so a retry would duplicate it.
    try:
        storage.touch(kh, channel_ttl_seconds())
    except StorageError:
        pass

    private_key, _public_key, subject = vapid_config()
    if not private_key:
        result["push_disabled"] = True
        return result

    # Bound the PUSHED payload by UTF-8 bytes (services reject >4096 bytes);
    # the stored message keeps its full text for the message list.
    payload = json.dumps(
        {
            "title": _truncate_utf8(msg["title"], PUSH_TITLE_MAX_BYTES),
            "body": _truncate_utf8(msg["body"], PUSH_BODY_MAX_BYTES),
            "url": msg["url"],
            "channel": _truncate_utf8(meta.get("name", ""), PUSH_CHANNEL_MAX_BYTES),
            "tag": msg["id"],
            "ts": msg["ts"],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )

    try:
        subs = storage.get_subs(kh)
    except StorageError:
        result["push_error"] = True
        return result

    if not subs:
        return result

    session = requests.Session()
    session.max_redirects = 0  # never follow a redirect off the push service

    def _send(sub_json: str) -> str:
        try:
            sub = json.loads(sub_json)
            webpush(
                subscription_info=sub,
                data=payload,
                vapid_private_key=private_key,
                vapid_claims={"sub": subject},
                ttl=PUSH_TTL_SECONDS,
                timeout=10,
                requests_session=session,
            )
            return "sent"
        except WebPushException as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status in (404, 410):
                try:
                    storage.delete_sub(kh, endpoint_hash(sub["endpoint"]))
                except Exception:
                    pass
                return "pruned"
            return "failed"
        except Exception:
            return "failed"

    try:
        workers = min(8, len(subs))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for outcome in pool.map(_send, subs):
                result[outcome] += 1
    finally:
        session.close()
    return result

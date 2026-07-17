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

import datetime
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
# Auto-remove: a channel created with an end date carries it INSIDE the code as
# a "-expYYYYMMDD" suffix (still matches CODE_RE → no format migration). The
# server hashes the whole code, so tampering with the suffix simply addresses a
# different (non-existent) channel — the date is integrity-protected for free.
CODE_EXP_RE = re.compile(r"-exp(\d{8})$")
MAX_AUTO_REMOVE_DAYS = 3650

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


def expiry_suffix(auto_remove_days: "int | None") -> str:
    """'-expYYYYMMDD' (UTC) for a self-removing channel, '' for never."""
    if not auto_remove_days:
        return ""
    end = time.gmtime(time.time() + auto_remove_days * 86400)
    return "-exp%04d%02d%02d" % (end.tm_year, end.tm_mon, end.tm_mday)


def code_expiry(code: str) -> "int | None":
    """Epoch seconds at which this code's channel auto-removes (the end of
    the UTC day encoded in the '-expYYYYMMDD' suffix), or None for a code
    without a (valid) suffix — a random code virtually never matches one."""
    m = CODE_EXP_RE.search(code or "")
    if not m:
        return None
    s = m.group(1)
    try:
        day = datetime.datetime(
            int(s[:4]), int(s[4:6]), int(s[6:8]), tzinfo=datetime.timezone.utc
        )
    except ValueError:
        return None
    return int(day.timestamp()) + 86400


def validate_auto_remove_days(value: object) -> "int | None":
    """None/0/'' mean never; otherwise a whole number of days (1..3650)."""
    if value in (None, 0, ""):
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("auto_remove_days must be a whole number of days")
    if not 1 <= value <= MAX_AUTO_REMOVE_DAYS:
        raise ValueError(
            f"auto_remove_days must be between 1 and {MAX_AUTO_REMOVE_DAYS}"
        )
    return value


# Message storage is OPT-IN (anonymous-by-default): 0 = the server stores no
# message content (push relay only — the DEFAULT for new channels), -1 = keep
# until pushed out of the newest-50 / channel end (the legacy behavior, and
# what channels created before this setting existed keep doing), or a
# retention in seconds after which the stored message expires.
STORE_OFF = 0
STORE_MAX = -1
MIN_STORE_SECONDS = 60
MAX_STORE_SECONDS = MAX_AUTO_REMOVE_DAYS * 86400


def validate_message_store(value: object) -> int:
    """Parse a message-storage setting: 'off'/0 → STORE_OFF, 'max'/-1 →
    STORE_MAX, otherwise retention seconds (60 s .. 3650 d)."""
    if value in ("off", 0):
        return STORE_OFF
    if value in ("max", -1):
        return STORE_MAX
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("message storage must be 'off', 'max' or seconds")
    if not MIN_STORE_SECONDS <= value <= MAX_STORE_SECONDS:
        raise ValueError(
            "message storage seconds must be between "
            f"{MIN_STORE_SECONDS} and {MAX_STORE_SECONDS}"
        )
    return value


def _ttl_for(expires: "int | None") -> int:
    """Storage TTL: the inactivity window, but never past the auto-remove
    date — Redis then deletes an expired channel entirely by itself."""
    ttl = channel_ttl_seconds()
    if expires:
        ttl = max(1, min(ttl, expires - int(time.time())))
    return ttl


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
        self._kv: "dict[str, str]" = {}

    def _entry(self, kh: str) -> "dict | None":
        return self._ch.get(kh)

    def create_channel(self, kh: str, meta_json: str, ttl: "int | None" = None) -> bool:
        with self._lock:
            if kh in self._ch:
                return False
            self._ch[kh] = {"meta": meta_json, "subs": {}, "msgs": []}
            return True

    def get_channel(self, kh: str) -> "str | None":
        with self._lock:
            e = self._entry(kh)
            return e["meta"] if e else None

    def put_sub(self, kh: str, eh: str, sub_json: str, ttl: "int | None" = None) -> None:
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

    def add_message(self, kh: str, msg_json: str, cap: int, ttl: "int | None" = None) -> None:
        with self._lock:
            e = self._entry(kh)
            if e is not None:
                e["msgs"].insert(0, msg_json)
                del e["msgs"][cap:]

    def seed_messages(
        self, kh: str, msgs_json: "list[str]", cap: int, ttl: "int | None" = None
    ) -> None:
        """Bulk-fill a (fresh) channel's message list, newest first — used when
        an extended successor channel inherits the old channel's messages."""
        with self._lock:
            e = self._entry(kh)
            if e is not None:
                e["msgs"] = list(msgs_json)[:cap]

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

    def remove_message_values(self, kh: str, raws: "list[str]") -> None:
        """Physically drop specific stored message values (expiry pruning)."""
        with self._lock:
            e = self._entry(kh)
            if e is not None and raws:
                gone = set(raws)
                e["msgs"] = [m for m in e["msgs"] if m not in gone]

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

    def get_value(self, key: str) -> "str | None":
        with self._lock:
            return self._kv.get(key)

    def set_value(self, key: str, value: str, ttl: int) -> None:
        with self._lock:
            self._kv[key] = value

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

    def create_channel(self, kh: str, meta_json: str, ttl: "int | None" = None) -> bool:
        t = str(ttl if ttl else channel_ttl_seconds())
        res = self._pipeline(
            [["SET", self._k("meta", kh), meta_json, "NX", "EX", t]]
        )
        return res[0] == "OK"

    def get_channel(self, kh: str) -> "str | None":
        return self._pipeline([["GET", self._k("meta", kh)]])[0]

    def put_sub(self, kh: str, eh: str, sub_json: str, ttl: "int | None" = None) -> None:
        t = str(ttl if ttl else channel_ttl_seconds())
        self._pipeline(
            [
                ["HSET", self._k("subs", kh), eh, sub_json],
                ["EXPIRE", self._k("subs", kh), t],
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

    def add_message(self, kh: str, msg_json: str, cap: int, ttl: "int | None" = None) -> None:
        t = str(ttl if ttl else channel_ttl_seconds())
        self._pipeline(
            [
                ["LPUSH", self._k("msgs", kh), msg_json],
                ["LTRIM", self._k("msgs", kh), "0", str(cap - 1)],
                ["EXPIRE", self._k("msgs", kh), t],
            ]
        )

    def seed_messages(
        self, kh: str, msgs_json: "list[str]", cap: int, ttl: "int | None" = None
    ) -> None:
        if not msgs_json:
            return
        t = str(ttl if ttl else channel_ttl_seconds())
        # the stored list is newest-first; RPUSH in that same order recreates it
        self._pipeline(
            [
                ["RPUSH", self._k("msgs", kh), *msgs_json],
                ["LTRIM", self._k("msgs", kh), "0", str(cap - 1)],
                ["EXPIRE", self._k("msgs", kh), t],
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

    def remove_message_values(self, kh: str, raws: "list[str]") -> None:
        """Physically drop specific stored message values (expiry pruning) —
        one pipeline call regardless of how many values expire at once."""
        if raws:
            self._pipeline(
                [["LREM", self._k("msgs", kh), "0", raw] for raw in raws]
            )

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

    def get_value(self, key: str) -> "str | None":
        return self._pipeline([["GET", "nbw:" + key]])[0]

    def set_value(self, key: str, value: str, ttl: int) -> None:
        self._pipeline([["SET", "nbw:" + key, value, "EX", str(ttl)]])

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


# ------------------------------------------------- deployed-commit footer

GITHUB_REPO = "mghomedev/NotifyByWebApp"

# sha -> "YYYY-MM-DD" | None; one GitHub-API fetch attempt per instance at
# most (and usually zero, thanks to the Redis cache written on first fetch)
_commit_dates: "dict[str, str | None]" = {}


def _fetch_commit_date(sha: str) -> "str | None":
    """Commit date for the traceability footer. Vercel's env exposes the SHA
    but NOT the commit date, so it is looked up once per deploy from the
    public GitHub API (fixed host, no user input) and cached in storage —
    every later instance/cold start gets it from Redis. Failures degrade
    gracefully to None (the footer then shows just the linked hash)."""
    key = "cdate:" + sha
    try:
        cached = get_storage().get_value(key)
        if cached:
            return cached
    except StorageError:
        pass
    try:
        req = urllib.request.Request(
            f"https://api.github.com/repos/{GITHUB_REPO}/commits/{sha}",
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "notify-by-web-app",
            },
        )
        with urllib.request.urlopen(req, timeout=4) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        date = ((data.get("commit") or {}).get("committer") or {}).get("date")
        if not (isinstance(date, str) and len(date) >= 10):
            return None
        date = date[:10]  # YYYY-MM-DD (UTC committer date)
    except Exception:
        return None
    try:
        get_storage().set_value(key, date, 400 * 86400)
    except StorageError:
        pass
    return date


def commit_info() -> "dict | None":
    """{sha, short, date, url} of the deployed commit, or None outside a
    git-connected deployment (local dev / tests)."""
    sha = (os.environ.get("VERCEL_GIT_COMMIT_SHA") or "").strip()
    if not re.fullmatch(r"[0-9a-f]{7,40}", sha):
        return None
    if sha not in _commit_dates:
        _commit_dates[sha] = _fetch_commit_date(sha)
    return {
        "sha": sha,
        "short": sha[:7],
        "date": _commit_dates[sha],
        "url": f"https://github.com/{GITHUB_REPO}/commit/{sha}",
    }


# ------------------------------------------------------ domain operations


def _live_meta(storage, kh: str) -> "dict | None":
    """Channel meta, or None when the channel is unknown OR past its
    auto-remove date. The Redis TTL deletes the data at that moment by
    itself; this guard covers the in-memory backend and any clock lag."""
    meta_json = storage.get_channel(kh)
    if meta_json is None:
        return None
    meta = _load_stored(meta_json)
    exp = meta.get("expires")
    if exp and int(time.time()) >= exp:
        return None
    return meta


def _new_channel(storage, name: str, created_meta: dict, suffix: str) -> dict:
    """Allocate a fresh code (+ optional expiry suffix) and store its meta."""
    for _ in range(3):
        code = generate_code() + suffix
        expires = code_expiry(code)
        meta = dict(created_meta)
        if expires:
            meta["expires"] = expires
        if storage.create_channel(code_hash(code), json.dumps(meta), _ttl_for(expires)):
            return {
                "code": code,
                "name": name,
                "created": meta["created"],
                "send_protected": bool(meta.get("send_pw")),
                "expires": expires,
                "message_store": meta.get("msg_store", STORE_MAX),
            }
    raise StorageError("could not create channel")


def create_channel(
    name: str,
    send_password: str = "",
    auto_remove_days: "int | None" = None,
    message_store: int = STORE_OFF,
) -> dict:
    storage = get_storage()
    meta = {"name": name, "created": int(time.time()), "msg_store": message_store}
    if send_password:
        meta["send_pw"] = _send_password_hash(send_password)
    return _new_channel(storage, name, meta, expiry_suffix(auto_remove_days))


def extend_channel(
    old_code: str,
    auto_remove_days: "int | None",
    send_password: object = None,
    notify: bool = True,
    message_store: "int | None" = None,
) -> "dict | None":
    """'Extend' a self-removing channel by creating a SUCCESSOR channel with a
    new end date (the old date is baked into the old code's hash, so it cannot
    be changed in place). The successor inherits the channel name, the
    send-password hash and all stored messages; the old channel is left
    untouched and still dies at its own date. Optionally a final migration
    message is pushed to the old channel's subscribers so they can switch with
    one tap — the new code travels ONLY in the encrypted push payload (as a
    fragment URL), never in the stored message, so storage still never holds a
    raw code. Returns the new channel dict, or None when the old channel is
    unknown or already expired. Raises SendForbidden on a wrong password."""
    storage = get_storage()
    old_kh = code_hash(old_code)
    old_meta = _live_meta(storage, old_kh)
    if old_meta is None:
        return None
    _require_send_password(old_meta, send_password)

    meta = {"name": old_meta.get("name", ""), "created": int(time.time())}
    if old_meta.get("send_pw"):
        meta["send_pw"] = old_meta["send_pw"]
    # the successor inherits the message-storage setting unless overridden
    meta["msg_store"] = (
        message_store
        if message_store is not None
        else old_meta.get("msg_store", STORE_MAX)
    )
    result = _new_channel(
        storage, meta["name"], meta, expiry_suffix(auto_remove_days)
    )

    # copy the stored messages BEFORE the migration notice, so the successor
    # starts with exactly the old content (order + timestamps preserved)
    msgs = storage.get_messages(old_kh, max_messages())
    if msgs:
        storage.seed_messages(
            code_hash(result["code"]), msgs, max_messages(), _ttl_for(result["expires"])
        )
    result["messages_copied"] = len(msgs)

    if notify:
        exp = result["expires"]
        until = (
            "now runs until " + time.strftime("%Y-%m-%d", time.gmtime(exp - 1))
            if exp
            else "now runs without an end date"
        )
        notice = publish(
            old_code,
            {
                "title": "Channel extended: " + (meta["name"] or "this channel"),
                "body": "This channel "
                + until
                + " under a NEW code. Tap this notification to switch, or ask "
                "the sender for the new QR code / link. The old channel stops "
                "on its original end date.",
                "url": "",
            },
            send_password,
            push_url="/a#codes=" + result["code"],
        )
        result["notified"] = (notice or {}).get("sent", 0)
    return result


def _msg_expired(msg: dict, now: int) -> bool:
    exp = msg.get("expires_at")
    return bool(exp) and now >= exp


def channel_snapshot(code: str, limit: int) -> "dict | None":
    """Channel meta + recent stored messages + subscriber count, or None.
    Messages past their per-message retention are filtered out on read (the
    physical prune happens on the next write — Redis lists have no
    per-element TTL). No-storage channels simply have an empty list."""
    storage = get_storage()
    kh = code_hash(code)
    meta = _live_meta(storage, kh)
    if meta is None:
        return None
    now = int(time.time())
    messages = [
        m
        for m in (_load_stored(r) for r in storage.get_messages(kh, limit))
        if not _msg_expired(m, now)
    ]
    return {
        "channel": {
            "name": meta.get("name", ""),
            "created": meta.get("created"),
            "send_protected": bool(meta.get("send_pw")),
            "expires": meta.get("expires"),
            "message_store": meta.get("msg_store", STORE_MAX),
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
    meta = _live_meta(storage, kh)
    if meta is None:
        return None
    ttl = _ttl_for(meta.get("expires"))
    eh = endpoint_hash(sub["endpoint"])
    existed = storage.sub_exists(kh, eh)
    cap = max_subs_per_channel()
    if not existed and storage.sub_count(kh) >= cap:
        raise ChannelFull()
    storage.put_sub(kh, eh, json.dumps(sub), ttl)
    if not existed and storage.sub_count(kh) > cap:
        storage.delete_sub(kh, eh)
        raise ChannelFull()
    storage.touch(kh, ttl)
    return storage.sub_count(kh)


def unsubscribe(code: str, endpoint: object) -> "bool | None":
    """Detach an endpoint. Returns removal status, None for unknown channels."""
    if not isinstance(endpoint, str) or not endpoint:
        raise ValueError("endpoint is required")
    storage = get_storage()
    kh = code_hash(code)
    if _live_meta(storage, kh) is None:
        return None
    return storage.delete_sub(kh, endpoint_hash(endpoint))


def delete_message(code: str, msg_id: object, send_password: object = None) -> "bool | None":
    """Delete one stored message by id. None for unknown channels. Raises
    ValueError (bad id) / SendForbidden (protected channel, wrong password)."""
    if not isinstance(msg_id, str) or not msg_id:
        raise ValueError("message id is required")
    storage = get_storage()
    kh = code_hash(code)
    meta = _live_meta(storage, kh)
    if meta is None:
        return None
    _require_send_password(meta, send_password)
    return storage.delete_message(kh, msg_id)


def clear_messages(
    code: str, send_password: object = None, keep: int = 0
) -> "bool | None":
    """Delete stored messages for a channel — all of them, or all but the
    newest `keep`. None for unknown channels. Raises SendForbidden for a
    protected channel with a wrong password."""
    storage = get_storage()
    kh = code_hash(code)
    meta = _live_meta(storage, kh)
    if meta is None:
        return None
    _require_send_password(meta, send_password)
    storage.clear_messages(kh, keep if isinstance(keep, int) and keep > 0 else 0)
    return True


def _truncate_utf8(text: str, max_bytes: int) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode("utf-8", "ignore").rstrip() + "…"


def _prune_expired_messages(storage, kh: str, now: int) -> None:
    """Physically remove stored messages past their per-message retention
    (reads only ever serve unexpired ones; this bounds the list). Best
    effort — a storage hiccup here must never fail the send."""
    try:
        expired = [
            raw
            for raw in storage.get_messages(kh, max_messages())
            if _msg_expired(_load_stored(raw), now)
        ]
        storage.remove_message_values(kh, expired)
    except StorageError:
        pass


def publish(
    code: str,
    message: dict,
    send_password: object = None,
    push_url: "str | None" = None,
    store: "int | None" = None,
) -> "dict | None":
    """Push a validated message to all subscribers, storing it on the server
    ONLY when the channel (or the per-message `store` override) opts in.
    Returns result counts, or None for unknown channels. Raises SendForbidden
    if the channel requires a send password and it is missing/wrong.

    Storage is opt-in by design (anonymous default): effective retention is
    the per-message `store` value if given, else the channel's `msg_store`
    (channels created before the setting existed keep the legacy keep-max
    behavior). STORE_OFF → pure relay, nothing written; a positive retention
    stamps `expires_at` into the stored message (filtered on read, pruned on
    write).

    push_url overrides the link in the PUSHED payload only (the stored
    message keeps the original url) — used by the extend-channel migration
    notice so the successor's raw code is never written to storage."""
    storage = get_storage()
    kh = code_hash(code)
    meta = _live_meta(storage, kh)
    if meta is None:
        return None
    _require_send_password(meta, send_password)
    ttl = _ttl_for(meta.get("expires"))
    retention = store if store is not None else meta.get("msg_store", STORE_MAX)

    now = int(time.time())
    msg = {
        "id": secrets.token_hex(4),
        "ts": now,
        "title": message["title"],
        "body": message["body"],
        "url": message["url"],
    }
    if retention != STORE_OFF:
        stored_msg = dict(msg)
        if retention > 0:
            stored_msg["expires_at"] = now + retention
        # Storing is the load-bearing side effect here; if it fails the caller
        # gets 502 and may safely retry (nothing was pushed yet).
        storage.add_message(kh, json.dumps(stored_msg), max_messages(), ttl)
        _prune_expired_messages(storage, kh, now)

    result = {
        "stored": retention != STORE_OFF,
        "sent": 0,
        "failed": 0,
        "pruned": 0,
        "push_disabled": False,
        "message": msg,
    }

    # From here on a storage hiccup must NOT turn into a 502 — the message
    # (if stored) is already stored, so a retry could duplicate it.
    try:
        storage.touch(kh, ttl)
    except StorageError:
        pass

    private_key, _public_key, subject = vapid_config()
    if not private_key:
        result["push_disabled"] = True
        return result

    # Bound the PUSHED payload by UTF-8 bytes (services reject >4096 bytes);
    # the stored message keeps its full text for the message list.
    payload_obj = {
        "title": _truncate_utf8(msg["title"], PUSH_TITLE_MAX_BYTES),
        "body": _truncate_utf8(msg["body"], PUSH_BODY_MAX_BYTES),
        "url": push_url if push_url is not None else msg["url"],
        "channel": _truncate_utf8(meta.get("name", ""), PUSH_CHANNEL_MAX_BYTES),
        "tag": msg["id"],
        "ts": msg["ts"],
        # channel identifier for the device-local history: a 48-bit kh prefix,
        # computable client-side from the code alone (sha256(code)[:12]). It
        # rides only inside the E2E-encrypted payload — never in URLs — and
        # gives a recipient (who must already hold the code) nothing new.
        "ch": kh[:12],
    }
    if meta.get("expires"):
        # lets the SW suppress a late-delivered push after the auto-remove date
        payload_obj["exp"] = meta["expires"]
    payload = json.dumps(payload_obj, ensure_ascii=False, separators=(",", ":"))

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

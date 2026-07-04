"""Real-crypto Web Push test harness.

- MockPushService: a local HTTP server that stands in for FCM/Mozilla/Apple.
  It captures the exact bytes pywebpush sends and can be told to answer a
  given status (e.g. 410 Gone) for a specific endpoint.
- FakeDevice: a "phone". It owns a real P-256 key pair, hands out a
  PushSubscription, and DECRYPTS the aes128gcm body the server produced —
  proving the whole VAPID + ECDH + HKDF + AES-GCM pipeline actually works,
  not just that webpush() was called.

This is the automated stand-in for a physical Android/iOS device receiving a
push: same wire format, same encryption, same VAPID auth.
"""
import base64
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import http_ece
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric import utils as asym_utils


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64url_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class _PushHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        svc = self.server.svc
        svc.record(self.path, dict(self.headers), body)
        status = svc.status_for(self.path)
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.end_headers()


class MockPushService:
    def __init__(self) -> None:
        self._srv = ThreadingHTTPServer(("127.0.0.1", 0), _PushHandler)
        self._srv.svc = self
        self._lock = threading.Lock()
        self.received = []
        self._status = {}
        self._default = 201  # Web Push success is 201 Created
        self.port = self._srv.server_port
        self.base = f"http://127.0.0.1:{self.port}"
        self._thread = threading.Thread(target=self._srv.serve_forever, daemon=True)
        self._thread.start()

    def record(self, path, headers, body):
        with self._lock:
            self.received.append({"path": path, "headers": headers, "body": body})

    def set_status(self, path_substr: str, status: int) -> None:
        self._status[path_substr] = status

    def status_for(self, path: str) -> int:
        for key, value in self._status.items():
            if key in path:
                return value
        return self._default

    def requests_for(self, path_substr: str):
        with self._lock:
            return [r for r in self.received if path_substr in r["path"]]

    def close(self):
        self._srv.shutdown()
        self._srv.server_close()


class FakeDevice:
    """A phone with a real Web Push subscription that can decrypt payloads."""

    def __init__(self, service: MockPushService, name: str = "dev") -> None:
        self._priv = ec.generate_private_key(ec.SECP256R1())
        pub = self._priv.public_key().public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
        )
        self.p256dh = b64url(pub)
        self._auth = os.urandom(16)
        self.auth = b64url(self._auth)
        self.name = name
        self.endpoint = f"{service.base}/push/{name}"

    def subscription(self) -> dict:
        return {
            "endpoint": self.endpoint,
            "expirationTime": None,
            "keys": {"p256dh": self.p256dh, "auth": self.auth},
        }

    def decrypt(self, body: bytes) -> dict:
        """Decrypt an aes128gcm push body and parse it as JSON."""
        plaintext = http_ece.decrypt(
            body, private_key=self._priv, auth_secret=self._auth
        )
        return json.loads(plaintext.decode("utf-8"))


def parse_vapid_authorization(header: str) -> dict:
    """Parse and cryptographically VERIFY a 'vapid t=<jwt>,k=<key>' header.
    Returns the JWT claims; raises on a bad signature."""
    assert header.lower().startswith("vapid "), header
    fields = dict(part.split("=", 1) for part in header[6:].split(","))
    jwt = fields["t"]
    key_b64 = fields["k"]
    h_seg, p_seg, s_seg = jwt.split(".")

    # verify ES256 signature over header.payload with the advertised key
    pub = ec.EllipticCurvePublicKey.from_encoded_point(
        ec.SECP256R1(), b64url_decode(key_b64)
    )
    raw_sig = b64url_decode(s_seg)
    r = int.from_bytes(raw_sig[:32], "big")
    s = int.from_bytes(raw_sig[32:], "big")
    der = asym_utils.encode_dss_signature(r, s)
    pub.verify(
        der, f"{h_seg}.{p_seg}".encode("ascii"), ec.ECDSA(hashes.SHA256())
    )  # raises InvalidSignature on tampering

    return json.loads(b64url_decode(p_seg).decode("utf-8"))

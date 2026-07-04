"""Exercise the real RedisStorage HTTP layer against a fake Upstash REST
endpoint — request construction, Bearer auth, /pipeline path, JSON decode,
per-command error handling and transport failure -> StorageError.

Without this, RedisStorage (the only production backend) is covered purely
as command lists via FakeRedis, so a regression in _pipeline itself would
ship green.
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import notify_core as core


class _FakeUpstashHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        srv = self.server
        srv.calls.append(
            {
                "path": self.path,
                "auth": self.headers.get("Authorization"),
                "body": json.loads(raw.decode("utf-8")),
            }
        )
        status, payload = srv.responder(srv.calls[-1])
        data = payload if isinstance(payload, (bytes, bytearray)) else json.dumps(
            payload
        ).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture()
def upstash():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _FakeUpstashHandler)
    srv.calls = []
    srv.responder = lambda call: (200, [{"result": "OK"}])
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    srv.base = f"http://127.0.0.1:{srv.server_port}"
    yield srv
    srv.shutdown()
    srv.server_close()
    thread.join(timeout=5)


def _store(upstash):
    return core.RedisStorage(upstash.base, "test-token")


def test_pipeline_request_shape(upstash):
    st = _store(upstash)
    upstash.responder = lambda call: (200, [{"result": "OK"}])
    assert st.create_channel("kh1", '{"name":"x"}') is True
    call = upstash.calls[0]
    assert call["path"] == "/pipeline"
    assert call["auth"] == "Bearer test-token"
    cmd = call["body"][0]
    assert cmd[0] == "SET" and cmd[1] == "nbw:meta:kh1"
    assert "NX" in cmd and "EX" in cmd


def test_create_channel_nx_conflict(upstash):
    st = _store(upstash)
    upstash.responder = lambda call: (200, [{"result": None}])  # SET NX -> nil
    assert st.create_channel("kh1", "{}") is False


def test_roundtrip_reads(upstash):
    st = _store(upstash)
    upstash.responder = lambda call: (200, [{"result": '{"name":"chan"}'}])
    assert st.get_channel("kh") == '{"name":"chan"}'

    upstash.responder = lambda call: (200, [{"result": ['{"a":1}', '{"b":2}']}])
    assert st.get_subs("kh") == ['{"a":1}', '{"b":2}']

    upstash.responder = lambda call: (200, [{"result": 3}])
    assert st.sub_count("kh") == 3

    upstash.responder = lambda call: (200, [{"result": 1}])
    assert st.sub_exists("kh", "eh") is True


def test_per_command_error_becomes_storage_error(upstash):
    st = _store(upstash)
    upstash.responder = lambda call: (200, [{"error": "WRONGTYPE ..."}])
    with pytest.raises(core.StorageError):
        st.get_channel("kh")


def test_non_json_body_becomes_storage_error(upstash):
    st = _store(upstash)
    upstash.responder = lambda call: (200, b"<html>gateway timeout</html>")
    with pytest.raises(core.StorageError):
        st.get_channel("kh")


def test_http_error_status_becomes_storage_error(upstash):
    st = _store(upstash)
    upstash.responder = lambda call: (500, {"error": "boom"})
    with pytest.raises(core.StorageError):
        st.get_channel("kh")


def test_connection_refused_becomes_storage_error():
    # nothing is listening here
    st = core.RedisStorage("http://127.0.0.1:1", "token")
    with pytest.raises(core.StorageError):
        st.get_channel("kh")


def test_ping_ok(upstash):
    st = _store(upstash)
    upstash.responder = lambda call: (200, [{"result": "PONG"}])
    assert st.ping() is True
    assert upstash.calls[-1]["body"] == [["PING"]]


def test_ping_failure_raises(upstash):
    st = _store(upstash)
    upstash.responder = lambda call: (200, [{"error": "boom"}])
    with pytest.raises(core.StorageError):
        st.ping()


def test_add_message_pipeline_encoding(upstash):
    st = _store(upstash)
    upstash.responder = lambda call: (200, [{"result": 1}, {"result": "OK"}, {"result": 1}])
    st.add_message("kh", '{"m":1}', cap=50)
    cmds = upstash.calls[-1]["body"]
    assert cmds[0][:2] == ["LPUSH", "nbw:msgs:kh"]
    assert cmds[1] == ["LTRIM", "nbw:msgs:kh", "0", "49"]
    assert cmds[2][0] == "EXPIRE"

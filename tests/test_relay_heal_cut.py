"""When the relay's self-heal starts, pooled connections to the old WDA stop
being a trap: an idle one answers its next request with the marked 503, and a
request still waiting on the old phone side fails at once."""

import asyncio
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from phone_harness import config, relay  # noqa: E402
from phone_harness.wda_client import WDAClient, WDAError, WDAUnreachable  # noqa: E402


class _FakeWDA(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"  # keep-alive, like WDA
    seen: list = []
    slow = threading.Event()  # set: the next request waits ~10 s before answering

    def log_message(self, *args):  # noqa: vulture
        pass

    def do_POST(self):  # noqa: vulture
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        _FakeWDA.seen.append(self.path)
        if _FakeWDA.slow.is_set():
            time.sleep(10)
        body = b'{"value": null}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def relay_to_fake_wda(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE_DIR", tmp_path)
    _FakeWDA.seen = []
    _FakeWDA.slow.clear()
    wda = ThreadingHTTPServer(("127.0.0.1", 0), _FakeWDA)
    wda.daemon_threads = True
    threading.Thread(target=wda.serve_forever, daemon=True).start()
    monkeypatch.setattr(relay, "DEVICE_WDA_PORT", wda.server_port)
    monkeypatch.setattr(relay, "_tunnel_address", lambda: "127.0.0.1")
    monkeypatch.setattr(relay, "_heal", lambda: None)  # the repair itself is not under test
    monkeypatch.setattr(relay, "_healing", False)
    monkeypatch.setattr(relay, "_heal_started", 0.0)
    monkeypatch.setattr(relay, "_open", set())
    loop = asyncio.new_event_loop()
    holder = {}
    started = threading.Event()

    async def run():
        server = await asyncio.start_server(relay._handler(wda.server_port), "127.0.0.1", 0)
        holder["port"] = server.sockets[0].getsockname()[1]
        holder["stop"] = asyncio.Event()
        started.set()
        await holder["stop"].wait()
        server.close()

    t = threading.Thread(target=lambda: loop.run_until_complete(run()), daemon=True)
    t.start()
    started.wait(5)

    def start_heal():
        async def go():
            relay._maybe_heal()

        asyncio.run_coroutine_threadsafe(go(), loop).result(5)

    yield f"http://127.0.0.1:{holder['port']}", start_heal
    loop.call_soon_threadsafe(holder["stop"].set)
    t.join(5)
    wda.shutdown()


def test_an_idle_pooled_connection_answers_the_marked_503_after_a_heal_starts(relay_to_fake_wda):
    url, start_heal = relay_to_fake_wda
    c = WDAClient(base_url=url, timeout=5)
    c._request("POST", "/session/x/actions", {})  # opens and pools a connection
    assert len(relay._open) == 1
    start_heal()
    with pytest.raises(WDAUnreachable, match="self-heal"):
        c._request("POST", "/session/x/actions", {"x": 1})  # same pooled connection
    assert _FakeWDA.seen == ["/session/x/actions"]  # the second call never reached WDA


def test_a_request_waiting_on_the_old_phone_side_fails_at_once(relay_to_fake_wda):
    url, start_heal = relay_to_fake_wda
    c = WDAClient(base_url=url, timeout=30)
    c._request("POST", "/session/x/actions", {})  # pool a connection
    _FakeWDA.slow.set()
    result = {}

    def gesture():
        t0 = time.monotonic()
        try:
            c._request("POST", "/session/x/actions", {"x": 1})
        except WDAError as exc:
            result["exc"] = exc
        result["took"] = time.monotonic() - t0

    th = threading.Thread(target=gesture)
    th.start()
    deadline = time.monotonic() + 5
    while len(_FakeWDA.seen) < 2 and time.monotonic() < deadline:
        time.sleep(0.02)
    assert len(_FakeWDA.seen) == 2  # the gesture reached WDA and waits for its reply
    start_heal()
    th.join(10)
    assert result["took"] < 5  # not the 10 s reply, nor a TCP give-up
    # It may have acted, so it is a broken call, never the replayable 503.
    assert isinstance(result["exc"], WDAError)
    assert not isinstance(result["exc"], WDAUnreachable)

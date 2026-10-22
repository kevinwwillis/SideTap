"""Pooled connections through the relay that lead to a WDA that is gone are
closed, so a client's next request opens a fresh connection instead of going
into a dead tunnel and hanging until TCP gives up (~20 s, seen live after a
USB/Wi-Fi switch)."""

import asyncio
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from phone_harness import config, link, relay  # noqa: E402
from phone_harness.wda_client import WDAClient, WDAUnreachable  # noqa: E402


class _FakeWDA(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"  # keep-alive, like WDA
    seen: list = []

    def log_message(self, *args):  # noqa: vulture
        pass

    def do_POST(self):  # noqa: vulture
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        _FakeWDA.seen.append(self.path)
        body = b'{"value": null}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def relay_to_fake_wda(tmp_path, monkeypatch):
    """The real relay handler in front of a fake WDA. `where["address"]` is
    what the tunnel lookup answers; an exception there is raised by it."""
    monkeypatch.setattr(config, "STATE_DIR", tmp_path)
    _FakeWDA.seen = []
    wda = ThreadingHTTPServer(("127.0.0.1", 0), _FakeWDA)
    wda.daemon_threads = True
    threading.Thread(target=wda.serve_forever, daemon=True).start()
    where = {"address": "127.0.0.1"}

    def lookup():
        if isinstance(where["address"], Exception):
            raise where["address"]
        return where["address"]

    heals = []
    monkeypatch.setattr(relay, "DEVICE_WDA_PORT", wda.server_port)
    monkeypatch.setattr(relay, "_tunnel_address", lookup)
    monkeypatch.setattr(relay, "_maybe_heal", lambda: heals.append(1))
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
    yield f"http://127.0.0.1:{holder['port']}", where, wda, heals
    loop.call_soon_threadsafe(holder["stop"].set)
    t.join(5)
    try:
        wda.shutdown()
    except Exception:
        pass


def _wait_until(cond, timeout: float = 2.0) -> None:
    """The relay answers the client before it finishes its own bookkeeping."""
    deadline = time.monotonic() + timeout
    while not cond() and time.monotonic() < deadline:
        time.sleep(0.02)


def _wait_closed(conn_count: int) -> None:
    _wait_until(lambda: len(relay._open) <= conn_count)


def test_a_pooled_connection_to_an_old_tunnel_is_closed_and_the_next_call_works(
    relay_to_fake_wda,
):
    url, where, _, _ = relay_to_fake_wda
    agent = WDAClient(base_url=url, timeout=5)
    agent._request("POST", "/session/x/actions", {})  # pooled, on 127.0.0.1
    assert len(relay._open) == 1
    where["address"] = "localhost"  # the link changed (same fake WDA, new name)
    other = WDAClient(base_url=url, timeout=5)
    other._request("POST", "/session/x/actions", {"other": 1})  # a new connection sees it
    _wait_closed(1)
    assert len(relay._open) == 1  # only the new one; the agent's old pipe was closed
    t0 = time.monotonic()
    agent._request("POST", "/session/x/actions", {"x": 1})  # a gesture, not resent
    assert time.monotonic() - t0 < 6  # "localhost" tries ::1 first (~2 s on Windows), not a ~20 s hang
    assert _FakeWDA.seen.count("/session/x/actions") == 3  # each landed exactly once


def test_after_wda_refuses_a_pooled_gesture_gets_the_replayable_503(relay_to_fake_wda):
    url, where, wda, heals = relay_to_fake_wda
    agent = WDAClient(base_url=url, timeout=5)
    agent._request("POST", "/session/x/actions", {})  # pooled
    wda.shutdown()
    wda.server_close()  # WDA gone: connections to it are refused now
    with pytest.raises(WDAUnreachable):
        WDAClient(base_url=url, timeout=5).status()  # refused: pipes there close, heal starts
    _wait_until(lambda: heals)
    assert heals
    _wait_closed(0)
    t0 = time.monotonic()
    with pytest.raises(WDAUnreachable):  # never reached WDA, so safe to resend
        agent._request("POST", "/session/x/actions", {"x": 1})
    assert time.monotonic() - t0 < 4  # a fresh connection, not a hang on the old one
    assert _FakeWDA.seen == ["/session/x/actions"]


def test_tunneld_down_leaves_open_pipes_alone(relay_to_fake_wda):
    url, where, _, heals = relay_to_fake_wda
    agent = WDAClient(base_url=url, timeout=5)
    agent._request("POST", "/session/x/actions", {})  # pooled
    where["address"] = link.TunneldDown("tunneld is not answering")
    with pytest.raises(WDAUnreachable):
        WDAClient(base_url=url, timeout=5).status()
    _wait_until(lambda: heals)
    assert heals and len(relay._open) == 1  # open pipes do not use tunneld
    agent._request("POST", "/session/x/actions", {"x": 1})  # same pooled connection
    assert _FakeWDA.seen == ["/session/x/actions"] * 2


def test_a_close_that_fails_never_stops_the_others(monkeypatch):
    class Broken:
        def abort(self):
            raise OSError("boom")

    class W:
        def __init__(self, transport):
            self.transport = transport

    closed = []

    class Ok:
        def abort(self):
            closed.append(1)

    bad = relay._Piped(W(Broken()), W(Broken()), wda=True, address="a")
    good = relay._Piped(W(Ok()), W(Ok()), wda=True, address="a")
    monkeypatch.setattr(relay, "_open", {bad, good})
    relay._close_stale("b")
    assert len(closed) == 2


def test_pipes_on_the_current_tunnel_are_kept(monkeypatch):
    closed = []

    class T:
        def abort(self):
            closed.append(1)

    class W:
        transport = T()

    keep = relay._Piped(W(), W(), wda=True, address="a")
    monkeypatch.setattr(relay, "_open", {keep})
    relay._close_stale("a")
    assert closed == []

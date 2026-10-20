"""Agent calls ride out a WDA restart; health probes still fail fast (TASK-001.11)."""

import asyncio
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from phone_harness import config, relay, wda_client  # noqa: E402
from phone_harness.wda_client import WDAClient, WDAError, WDAUnreachable  # noqa: E402


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(autouse=True)
def _state(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE_DIR", tmp_path)
    monkeypatch.setattr(wda_client, "_RECONNECT_POLL", 0.02)


@pytest.fixture
def relay_to_nowhere(monkeypatch):
    """The real relay handler, with the phone's WDA port closed."""
    dead = _free_port()
    monkeypatch.setattr(relay, "DEVICE_WDA_PORT", dead)
    monkeypatch.setattr(relay, "_tunnel_address", lambda: "127.0.0.1")
    monkeypatch.setattr(relay, "_maybe_heal", lambda: None)
    loop = asyncio.new_event_loop()
    started = threading.Event()
    holder = {}

    async def run():
        server = await asyncio.start_server(relay._handler(dead), "127.0.0.1", 0)
        holder["port"] = server.sockets[0].getsockname()[1]
        holder["stop"] = asyncio.Event()
        started.set()
        await holder["stop"].wait()
        server.close()
        await server.wait_closed()

    t = threading.Thread(target=lambda: loop.run_until_complete(run()), daemon=True)
    t.start()
    started.wait(5)
    yield f"http://127.0.0.1:{holder['port']}"
    loop.call_soon_threadsafe(holder["stop"].set)
    t.join(5)
    loop.close()


def test_relay_answers_a_marked_503_when_wda_is_out_of_reach(relay_to_nowhere):
    c = WDAClient(base_url=relay_to_nowhere, timeout=5)
    with pytest.raises(WDAUnreachable, match="relay"):
        c.status()
    # a POST with a body is read before the reply, so it is not reset either
    with pytest.raises(WDAUnreachable):
        c._request("POST", "/wda/homescreen", {"x": 1})


def test_probe_client_fails_fast(relay_to_nowhere):
    c = WDAClient(base_url=relay_to_nowhere, timeout=5)
    t0 = time.monotonic()
    assert c.link_state() == "down"
    assert time.monotonic() - t0 < 5  # no 30 s retry (a refused connect on Windows takes ~2 s)


def test_refused_connect_counts_as_unreachable():
    c = WDAClient(base_url=f"http://127.0.0.1:{_free_port()}", timeout=10)
    with pytest.raises(WDAUnreachable):
        c.status()


def test_agent_client_retries_until_wda_is_back(monkeypatch):
    calls = []

    def once(self, method, path, payload):
        calls.append((method, path))
        if len(calls) < 3:
            raise WDAUnreachable("down")
        return {"ok": True}

    monkeypatch.setattr(WDAClient, "_request_once", once)
    c = WDAClient(base_url="http://127.0.0.1:1", reconnect_wait=5)
    assert c._request("POST", "/wda/homescreen") == {"ok": True}
    assert len(calls) == 3


def test_agent_client_gives_up_after_its_wait(monkeypatch):
    monkeypatch.setattr(
        WDAClient, "_request_once", lambda *a: (_ for _ in ()).throw(WDAUnreachable("down"))
    )
    c = WDAClient(base_url="http://127.0.0.1:1", reconnect_wait=0.2)
    t0 = time.monotonic()
    with pytest.raises(WDAUnreachable):
        c.status()
    assert 0.1 < time.monotonic() - t0 < 1.0


def test_stop_switch_is_checked_before_each_retry(monkeypatch):
    calls = []

    def once(self, method, path, payload):
        calls.append(path)
        wda_client.stop_file().write_text("")
        raise WDAUnreachable("down")

    monkeypatch.setattr(WDAClient, "_request_once", once)
    c = WDAClient(base_url="http://127.0.0.1:1", reconnect_wait=5)
    with pytest.raises(WDAError, match="STOP"):
        c._request("POST", "/wda/homescreen")
    assert len(calls) == 1


class _HangUp(BaseHTTPRequestHandler):
    """Reads the request, then closes without answering: WDA dying mid-call."""

    def log_message(self, *args):  # noqa: vulture
        pass

    def do_POST(self):  # noqa: vulture
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        _HangUp.seen += 1
        self.close_connection = True
        self.connection.shutdown(socket.SHUT_RDWR)

    seen = 0


@pytest.mark.parametrize("inside_old_refusal", [False, True])
def test_a_call_that_reached_wda_is_never_replayed(inside_old_refusal):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _HangUp)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        _HangUp.seen = 0
        c = WDAClient(base_url=f"http://127.0.0.1:{server.server_port}", reconnect_wait=5)

        def gesture():
            with pytest.raises(WDAError) as info:
                c._request("POST", "/session/x/actions", {"actions": []})
            assert not isinstance(info.value, WDAUnreachable)

        if inside_old_refusal:
            # a caller handling an earlier refusal must not taint this call
            try:
                WDAClient(base_url=f"http://127.0.0.1:{_free_port()}", timeout=10).status()
            except WDAUnreachable:
                gesture()
        else:
            gesture()
        assert _HangUp.seen == 1
    finally:
        server.shutdown()


class _Ok(BaseHTTPRequestHandler):
    def log_message(self, *args):  # noqa: vulture
        pass

    def do_GET(self):  # noqa: vulture
        body = b'{"value": {"ready": true}}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def relay_port(monkeypatch):
    port = _free_port()
    monkeypatch.setattr(config, "WDA_PORT", port)
    servers = []
    yield port, servers
    for s in servers:
        s.shutdown()


def test_agent_call_starts_a_relay_that_is_not_running(relay_port, monkeypatch):
    """After a reboot nothing starts the relay (TASK-001.15); the first agent call does."""
    port, servers = relay_port
    starts = []

    def start():
        starts.append(1)
        s = ThreadingHTTPServer(("127.0.0.1", port), _Ok)
        threading.Thread(target=s.serve_forever, daemon=True).start()
        servers.append(s)

    monkeypatch.setattr(wda_client, "_start_relay", start)
    c = WDAClient(base_url=f"http://127.0.0.1:{port}", reconnect_wait=10)
    assert c.status() == {"ready": True}
    assert starts == [1]


def test_relay_is_started_once_per_call(relay_port, monkeypatch):
    port, _ = relay_port
    starts = []
    monkeypatch.setattr(wda_client, "_start_relay", lambda: starts.append(1))
    c = WDAClient(base_url=f"http://127.0.0.1:{port}", reconnect_wait=0.5)
    with pytest.raises(WDAUnreachable):
        c.status()
    assert starts == [1]


def test_probe_client_never_starts_the_relay(relay_port, monkeypatch):
    """`doctor` and `up` must see a down link as it is."""
    port, _ = relay_port
    monkeypatch.setattr(wda_client, "_start_relay", lambda: pytest.fail("probe started the relay"))
    with pytest.raises(WDAUnreachable):
        WDAClient(base_url=f"http://127.0.0.1:{port}").status()


def test_a_marked_503_does_not_start_another_relay(relay_to_nowhere, monkeypatch):
    """The relay answered, so it is running; only WDA behind it is down."""
    monkeypatch.setattr(config, "WDA_PORT", int(relay_to_nowhere.rsplit(":", 1)[1]))
    monkeypatch.setattr(wda_client, "_start_relay", lambda: pytest.fail("relay was running"))
    with pytest.raises(WDAUnreachable):
        WDAClient(base_url=relay_to_nowhere, timeout=5, reconnect_wait=0.3).status()

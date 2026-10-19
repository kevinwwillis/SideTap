"""Loopback relay: 127.0.0.1:WDA_PORT/MJPEG_PORT -> WebDriverAgent on the tunnel.

WebDriverAgent is started bound to the device's tunnel address (USE_IP), so
nothing on the home network can reach it, and that address changes with every
tunnel (each replug, each Wi-Fi reconnect). Everything else in this package
talks to http://127.0.0.1:8100, so this relay looks the tunnel up per new
connection and pipes bytes both ways. It listens on loopback only: unlike
`ios forward` (0.0.0.0, needing a firewall block), nothing here faces the LAN.

It also stops WDA after config.WDA_IDLE_STOP_MINUTES with no traffic, because
iOS shows its "Automation Running" banner for as long as WDA runs; the next
call finds WDA stopped and the self-heal starts it again.

Run detached by device.start_forwards(): `pythonw -m phone_harness.relay`.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time

from . import config, link

DEVICE_WDA_PORT = 8100
DEVICE_MJPEG_PORT = 9100
_LOOKUP_TTL = 2.0  # seconds a tunnel lookup is reused across a burst of connections

_cached: tuple[float, str] | None = None
_last_failure = ""  # last reason a connection could not reach the phone, logged once


def _tunnel_address() -> str:
    global _cached
    now = time.monotonic()
    if _cached and now - _cached[0] < _LOOKUP_TTL:
        return _cached[1]
    address = link.tunnel_for()[0]
    _cached = (now, address)
    return address


_last_traffic = time.monotonic()  # bytes through either port, or a new connection
_open: set[asyncio.StreamWriter] = set()  # both ends of every piped connection
_gate: asyncio.Event | None = None  # set while an idle stop runs; new connections wait


def _touch() -> None:
    global _last_traffic
    _last_traffic = time.monotonic()


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(65536):
            _touch()
            writer.write(data)
            await writer.drain()
    except (ConnectionError, OSError, asyncio.CancelledError):
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


UNREACHABLE_HEADER = "X-Phone-Relay"  # value "unreachable"; read by wda_client


async def _answer_unreachable(
    client_r: asyncio.StreamReader, client_w: asyncio.StreamWriter, reason: str
) -> None:
    """Answer an HTTP request that never reached WDA with a marked 503.

    The request is read first (headers and any body), so closing afterwards
    does not reset the connection under the reply. Never raises."""
    try:
        head = await asyncio.wait_for(client_r.readuntil(b"\r\n\r\n"), timeout=2)
        length = 0
        for line in head.split(b"\r\n"):
            name, _, value = line.partition(b":")
            if name.strip().lower() == b"content-length":
                length = int(value.strip() or 0)
        if length:
            await asyncio.wait_for(client_r.readexactly(length), timeout=2)
        body = json.dumps(
            {"value": {"error": "wda unreachable", "message": f"relay: {reason}"}}
        ).encode()
        client_w.write(
            b"HTTP/1.1 503 Service Unavailable\r\n"
            + f"{UNREACHABLE_HEADER}: unreachable\r\n".encode()
            + b"Content-Type: application/json\r\n"
            + f"Content-Length: {len(body)}\r\n".encode()
            + b"Connection: close\r\n\r\n"
            + body
        )
        await asyncio.wait_for(client_w.drain(), timeout=2)
    except Exception:
        pass  # a bare close still reads as "WDA not answering"


_HEAL_COOLDOWN = 30.0  # seconds between two self-heal attempts
_heal_started = 0.0
_healing = False
# Held while WDA is being started or stopped, so an idle stop can never kill
# the runner a self-heal has just started.
_lifecycle = threading.Lock()


def _heal() -> None:
    """Bring WDA back with the same routine as `phone-harness up`.

    WDA is bound to the tunnel it was started on (USE_IP), so a replug, a
    Wi-Fi reconnect or a switch between USB and Wi-Fi leaves it unreachable at
    the new tunnel address, and the MCP tools and helpers never restart it
    themselves. Only a REFUSED connection lands here: a wedged WDA accepts the
    connection, and restarting a wedged runner is the wrong repair (admin.up
    handles that state itself)."""
    global _healing
    try:
        from . import admin

        with _lifecycle:
            admin.up()
    except Exception as exc:  # the relay must outlive a failed repair
        print(f"self-heal failed: {exc}", flush=True)
    finally:
        _healing = False


def _maybe_heal() -> None:
    global _heal_started, _healing
    now = time.monotonic()
    if _healing or now - _heal_started < _HEAL_COOLDOWN:
        return
    _healing, _heal_started = True, now
    print(f"WDA unreachable through the tunnel; starting self-heal at {time.ctime()}", flush=True)
    asyncio.get_running_loop().run_in_executor(None, _heal)


def _handler(device_port: int):
    async def handle(client_r: asyncio.StreamReader, client_w: asyncio.StreamWriter) -> None:
        global _cached, _last_failure
        _touch()
        while (gate := _gate) is not None:
            await gate.wait()
        try:
            address = await asyncio.to_thread(_tunnel_address)
            dev_r, dev_w = await asyncio.wait_for(
                asyncio.open_connection(address, device_port), timeout=5
            )
        except (link.LinkError, OSError, asyncio.TimeoutError) as exc:
            if device_port == DEVICE_WDA_PORT:
                # Nothing was forwarded, so say so: the client may replay any
                # request after this answer (wda_client.UNREACHABLE_HEADER),
                # which it could not do after a bare close (a WDA dying
                # mid-gesture looks the same).
                await _answer_unreachable(client_r, client_w, f"{type(exc).__name__}: {exc}")
            client_w.close()
            _cached = None  # look the tunnel up afresh next time
            reason = f"{type(exc).__name__}: {exc}"
            if reason != _last_failure:
                _last_failure = reason
                print(f"{time.ctime()} cannot reach the phone: {reason}", flush=True)
            if device_port == DEVICE_WDA_PORT and not isinstance(exc, link.LinkError):
                _maybe_heal()
            return
        _last_failure = ""
        pair = {client_w, dev_w}
        _open.update(pair)
        try:
            await asyncio.gather(_pipe(client_r, dev_w), _pipe(dev_r, client_w))
        finally:
            _open.difference_update(pair)

    return handle


_IDLE_CHECK = 30.0  # seconds between two idle checks


def _idle_stop(idle_seconds: float) -> bool:
    """Stop WDA if nothing has gone through the relay for `idle_seconds`.

    Idle means no bytes and no new connection on either port. Open but silent
    connections do not count: an agent's HTTP client keeps one pooled for
    good, while a live MJPEG stream sends bytes all the time."""
    from . import device

    with _lifecycle, device.wda_lifecycle_lock():
        if _healing or time.monotonic() - _last_traffic < idle_seconds:
            return False
        if device.proc_status("runwda") != "running":
            return False
        age = device.proc_age("runwda")
        if age is not None and age < idle_seconds:
            return False  # started from outside the relay (`up`, the viewer) just now
        device.stop_all(("runwda",))
        device.mark_wda_idle()
    print(f"{time.ctime()} WDA idle for {idle_seconds / 60:g} min; stopped it", flush=True)
    return True


async def _idle_watch(idle_seconds: float) -> None:
    while True:
        await asyncio.sleep(_IDLE_CHECK)
        await _idle_check(idle_seconds)


async def _idle_check(idle_seconds: float) -> None:
    """Decide on the event loop, where every byte is piped, so nothing can be
    forwarded between "idle" and the gate closing: a request that reached WDA
    must never meet a WDA being killed under it (it could not be replayed).
    Open connections are silent ones (pooled by HTTP clients); they are closed
    so a client opens a fresh one, which waits at the gate and then starts WDA."""
    global _gate
    if _gate is not None or _healing or time.monotonic() - _last_traffic < idle_seconds:
        return
    _gate = asyncio.Event()
    for writer in list(_open):
        writer.close()
    try:
        await asyncio.to_thread(_idle_stop, idle_seconds)
    except Exception as exc:  # the relay must outlive a failed stop
        print(f"{time.ctime()} idle stop failed: {exc}", flush=True)
    finally:
        _gate.set()
        _gate = None


async def serve() -> None:
    servers = [
        await asyncio.start_server(_handler(DEVICE_WDA_PORT), "127.0.0.1", config.WDA_PORT),
        await asyncio.start_server(_handler(DEVICE_MJPEG_PORT), "127.0.0.1", config.MJPEG_PORT),
    ]
    tasks = [s.serve_forever() for s in servers]
    if config.WDA_IDLE_STOP_MINUTES > 0:
        tasks.append(_idle_watch(config.WDA_IDLE_STOP_MINUTES * 60))
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    asyncio.run(serve())

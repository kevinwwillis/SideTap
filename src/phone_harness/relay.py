"""Loopback relay: 127.0.0.1:WDA_PORT/MJPEG_PORT -> WebDriverAgent on the tunnel.

WebDriverAgent is started bound to the device's tunnel address (USE_IP), so
nothing on the home network can reach it, and that address changes with every
tunnel (each replug, each Wi-Fi reconnect). Everything else in this package
talks to http://127.0.0.1:8100, so this relay looks the tunnel up per new
connection and pipes bytes both ways. It listens on loopback only: unlike
`ios forward` (0.0.0.0, needing a firewall block), nothing here faces the LAN.

Run detached by device.start_forwards(): `pythonw -m phone_harness.relay`.
"""

from __future__ import annotations

import asyncio
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


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except (ConnectionError, OSError, asyncio.CancelledError):
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


_HEAL_COOLDOWN = 30.0  # seconds between two self-heal attempts
_heal_started = 0.0
_healing = False


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
        try:
            address = await asyncio.to_thread(_tunnel_address)
            dev_r, dev_w = await asyncio.wait_for(
                asyncio.open_connection(address, device_port), timeout=5
            )
        except (link.LinkError, OSError, asyncio.TimeoutError) as exc:
            # Closing at once looks to the client like "WDA not answering",
            # which is exactly what the callers already handle.
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
        await asyncio.gather(_pipe(client_r, dev_w), _pipe(dev_r, client_w))

    return handle


async def serve() -> None:
    servers = [
        await asyncio.start_server(_handler(DEVICE_WDA_PORT), "127.0.0.1", config.WDA_PORT),
        await asyncio.start_server(_handler(DEVICE_MJPEG_PORT), "127.0.0.1", config.MJPEG_PORT),
    ]
    await asyncio.gather(*(s.serve_forever() for s in servers))


if __name__ == "__main__":
    asyncio.run(serve())

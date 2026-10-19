"""Device link over USB or Wi-Fi: pymobiledevice3 through tunneld.

go-ios finds devices only through Apple's usbmux service, so it cannot reach a
phone that is on Wi-Fi only (checked 2026-10-18 on iPadOS 26.5.2: `Device not
found` even with --address/--rsd-port). pymobiledevice3's `remote tunneld` keeps
one tunnel per device over whichever link exists (USB or Wi-Fi) and lists them on
a local HTTP API; every call here connects to the device's RemoteServiceDiscovery
(RSD) endpoint through that tunnel. tunneld itself runs as its own long-lived
admin process (it creates TUN interfaces); this module never starts it.

The calls are in-process (import ~1.4 s once, connect ~0.45 s, app list ~0.22 s
measured over Wi-Fi) rather than one `python -m pymobiledevice3` per call (3-7 s).
"""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from typing import Any, Awaitable, Callable, TypeVar

from . import config

T = TypeVar("T")

TUNNELD_URL = config.get("TUNNELD_URL", "http://127.0.0.1:49151") or "http://127.0.0.1:49151"


class LinkError(RuntimeError):
    pass


def tunnels(timeout: float = 3.0) -> dict[str, list[dict]]:
    """tunneld's listing: {udid: [{"tunnel-address", "tunnel-port", "interface", ...}]}.

    Raises LinkError when tunneld itself is not answering, which is a different
    fault from "no phone": the fix is to start tunneld, not to plug anything in.
    """
    try:
        with urllib.request.urlopen(TUNNELD_URL + "/", timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise LinkError(f"tunneld is not answering at {TUNNELD_URL}: {exc}") from exc
    return {udid: list(entries or []) for udid, entries in (data or {}).items() if entries}


def tunneld_up(timeout: float = 3.0) -> bool:
    try:
        tunnels(timeout)
    except LinkError:
        return False
    return True


def list_devices() -> list[str]:
    """UDIDs with a live tunnel, USB or Wi-Fi."""
    return sorted(tunnels())


def _is_usb(entry: dict) -> bool:
    # tunneld names a USB tunnel's interface after the usbmux/CoreDevice link and a
    # Wi-Fi one after the phone's IP address; a ':' or '.' means an address.
    iface = str(entry.get("interface", ""))
    return not (":" in iface or iface.replace(".", "").isdigit())


def tunnel_for(udid: str | None = None) -> tuple[str, int, str]:
    """(address, rsd port, "usb"|"wifi") of the device's tunnel, USB first."""
    listing = tunnels()
    if udid is None:
        udid = config.SIDETAP_UDID or (sorted(listing)[0] if listing else None)
    entries = listing.get(udid or "", [])
    if not entries:
        raise LinkError(
            "no tunnel to the phone: it is not on USB, or not on the same Wi-Fi as this PC, "
            "or it is asleep"
        )
    entries = sorted(entries, key=lambda e: not _is_usb(e))
    best = entries[0]
    return str(best["tunnel-address"]), int(best["tunnel-port"]), (
        "usb" if _is_usb(best) else "wifi"
    )


def run(fn: Callable[[Any], Awaitable[T]], udid: str | None = None, timeout: float = 30.0) -> T:
    """Connect to the device's RSD through its tunnel, await fn(rsd), close."""
    from pymobiledevice3.remote.remote_service_discovery import (
        RemoteServiceDiscoveryService,
    )

    address, port, _ = tunnel_for(udid)

    async def main() -> T:
        rsd = RemoteServiceDiscoveryService((address, port))
        await rsd.connect()
        try:
            return await fn(rsd)
        finally:
            await rsd.close()

    async def bounded() -> T:
        return await asyncio.wait_for(main(), timeout)

    try:
        return asyncio.run(bounded())
    except LinkError:
        raise
    except Exception as exc:  # transport, lockdown or DVT failure, named
        raise LinkError(f"{type(exc).__name__}: {exc}") from exc


# ---- the calls SideTap needs ------------------------------------------------


def list_apps() -> list[dict]:
    """User-installed apps as [{bundle_id, name}]."""
    from pymobiledevice3.services.installation_proxy import InstallationProxyService

    async def fn(rsd):
        return await InstallationProxyService(lockdown=rsd).get_apps(application_type="User")

    apps = run(fn)
    return [
        {"bundle_id": bid, "name": info.get("CFBundleName") or info.get("CFBundleDisplayName", "")}
        for bid, info in apps.items()
    ]


def running_apps() -> list[dict]:
    """Processes that are applications: [{name, pid, started}], as go-ios `ps` gave."""
    from pymobiledevice3.services.dvt.instruments.device_info import DeviceInfo
    from pymobiledevice3.services.dvt.instruments.dvt_provider import DvtProvider

    async def fn(rsd):
        async with DvtProvider(rsd) as dvt, DeviceInfo(dvt) as info:
            return await info.proclist()

    return [
        {"name": str(p.get("name", "")), "pid": p.get("pid"), "started": str(p.get("startDate", ""))}
        for p in run(fn)
        if p.get("isApplication")
    ]


def kill_app(bundle_id: str) -> bool:
    """Kill the app's process. False when it is not running."""
    from pymobiledevice3.services.dvt.instruments.dvt_provider import DvtProvider
    from pymobiledevice3.services.dvt.instruments.process_control import ProcessControl

    async def fn(rsd):
        async with DvtProvider(rsd) as dvt, ProcessControl(dvt) as pc:
            pid = await pc.process_identifier_for_bundle_identifier(bundle_id)
            if not pid:
                return False
            await pc.kill(pid)
            return True

    return run(fn)


def launch(bundle_id: str) -> bool:
    """Launch (or bring to front) an app by bundle id."""
    from pymobiledevice3.services.dvt.instruments.dvt_provider import DvtProvider
    from pymobiledevice3.services.dvt.instruments.process_control import ProcessControl

    async def fn(rsd):
        async with DvtProvider(rsd) as dvt, ProcessControl(dvt) as pc:
            return bool(await pc.launch(bundle_id, kill_existing=False))

    return run(fn)


def screenshot_png() -> bytes:
    from pymobiledevice3.services.dvt.instruments.dvt_provider import DvtProvider
    from pymobiledevice3.services.dvt.instruments.screenshot import Screenshot

    async def fn(rsd):
        async with DvtProvider(rsd) as dvt, Screenshot(dvt) as shot:
            return await shot.get_screenshot()

    return run(fn)


def ddi_mounted() -> bool:
    from pymobiledevice3.services.mobile_image_mounter import MobileImageMounterService

    async def fn(rsd):
        return await MobileImageMounterService(rsd).is_image_mounted("Personalized")

    return run(fn)


def mount_ddi() -> None:
    """Mount the personalized developer image (Apple signs it per device: needs internet)."""
    from pymobiledevice3.services.mobile_image_mounter import auto_mount

    async def fn(rsd):
        await auto_mount(rsd)

    run(fn, timeout=300)


def device_answers() -> bool:
    """Cheap "is the phone reachable and talking" probe (replaces `ios date`)."""
    try:
        run(lambda rsd: _noop(), timeout=10)
    except LinkError:
        return False
    return True


async def _noop() -> None:
    return None

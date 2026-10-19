"""ios_path fallback for truncated-PATH launches (shortcut/Startup). No phone needed."""

import json
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from phone_harness import device  # noqa: E402


def test_ios_path_prefers_path_lookup(monkeypatch):
    monkeypatch.setattr(device.shutil, "which", lambda _: r"C:\somewhere\ios.exe")
    assert device.ios_path() == r"C:\somewhere\ios.exe"


def test_ios_path_falls_back_to_npm_global_dir(monkeypatch, tmp_path):
    # Windows truncates a registry PATH past ~4095 chars when it builds the
    # logon environment, so shortcut/Startup launches can miss the npm dir
    # even though terminals see it (bit the Startup shortcut live 2026-08-10).
    monkeypatch.setattr(device.shutil, "which", lambda _: None)
    exe = tmp_path / "npm" / "ios.exe"
    exe.parent.mkdir()
    exe.write_bytes(b"")
    monkeypatch.setenv("APPDATA", str(tmp_path))
    assert device.ios_path() == str(exe)


def test_ios_path_falls_back_to_installer_bin_dir(monkeypatch, tmp_path):
    # The sidetap.io one-line installer puts ios.exe in %LOCALAPPDATA%\SideTap\bin
    # (no Node/npm on the machine at all), so PATH lookup and the npm dir both miss.
    monkeypatch.setattr(device.shutil, "which", lambda _: None)
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
    exe = tmp_path / "SideTap" / "bin" / "ios.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert device.ios_path() == str(exe)


def test_ios_path_none_when_missing_everywhere(monkeypatch, tmp_path):
    monkeypatch.setattr(device.shutil, "which", lambda _: None)
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert device.ios_path() is None


# ---- WDA bundle cache: deep sleep empties `ios apps --list` (seen live
# 2026-08-10) while the app is still installed. The cache keeps up() working
# so the link can heal the moment the phone wakes.


def test_wda_bundle_cached_on_live_detection(monkeypatch, tmp_path):
    monkeypatch.setattr(device.config, "STATE_DIR", tmp_path)
    monkeypatch.setattr(device.config, "WDA_BUNDLE_ID", "")
    monkeypatch.setattr(
        device,
        "list_apps",
        lambda: [{"bundle_id": "com.x.WebDriverAgentRunner.xctrunner", "name": "WDA"}],
    )
    assert device.detect_wda_bundle() == "com.x.WebDriverAgentRunner.xctrunner"
    cached = (tmp_path / "wda_bundle").read_text(encoding="utf-8").strip()
    assert cached == "com.x.WebDriverAgentRunner.xctrunner"


def test_wda_bundle_falls_back_to_cache_when_list_empty(monkeypatch, tmp_path):
    monkeypatch.setattr(device.config, "STATE_DIR", tmp_path)
    monkeypatch.setattr(device.config, "WDA_BUNDLE_ID", "")
    (tmp_path / "wda_bundle").write_text("com.x.cached.xctrunner", encoding="utf-8")
    monkeypatch.setattr(device, "list_apps", lambda: [])
    assert device.detect_wda_bundle() == "com.x.cached.xctrunner"


def test_wda_bundle_none_when_empty_list_and_no_cache(monkeypatch, tmp_path):
    monkeypatch.setattr(device.config, "STATE_DIR", tmp_path)
    monkeypatch.setattr(device.config, "WDA_BUNDLE_ID", "")
    monkeypatch.setattr(device, "list_apps", lambda: [])
    assert device.detect_wda_bundle() is None


# ---- Developer Disk Image: an iOS update silently unmounts it (bit live
# 2026-08-10, the 26.6 update) — runwda then dies in dtx channel timeouts.
# The image mounter is asked through the tunnel (link.py).


class _Proc:
    def __init__(self, stdout="", returncode=0):
        self.stdout = stdout
        self.stderr = ""
        self.returncode = returncode


def test_ddi_mounted_asks_the_link(monkeypatch):
    monkeypatch.setattr(device.link, "ddi_mounted", lambda: True)
    assert device.ddi_mounted()
    monkeypatch.setattr(device.link, "ddi_mounted", lambda: False)
    assert not device.ddi_mounted()


def test_ddi_mounted_false_when_phone_unreachable(monkeypatch):
    def unreachable():
        raise device.link.LinkError("no tunnel to the phone")

    monkeypatch.setattr(device.link, "ddi_mounted", unreachable)
    assert not device.ddi_mounted()


def test_mount_ddi_names_locked_phone(monkeypatch):
    # The one mount failure a human can fix on the spot must be named, not
    # buried in a log tail: DeviceLocked -> "unlock it".
    def locked():
        raise device.link.LinkError("PyMobileDevice3Exception: {'Error': 'DeviceLocked'}")

    monkeypatch.setattr(device.link, "mount_ddi", locked)
    monkeypatch.setattr(device, "ddi_mounted", lambda: False)
    ok, msg = device.mount_ddi()
    assert not ok
    assert "unlock" in msg.lower()


def test_mount_ddi_verifies_by_reprobe(monkeypatch):
    monkeypatch.setattr(device.link, "mount_ddi", lambda: None)
    monkeypatch.setattr(device, "ddi_mounted", lambda: True)
    ok, msg = device.mount_ddi()
    assert ok
    assert "mounted" in msg


def test_wda_bundle_no_cache_fallback_when_app_really_absent(monkeypatch, tmp_path):
    # A non-empty list without WDA means genuinely uninstalled: the stale
    # cache must NOT resurrect it.
    monkeypatch.setattr(device.config, "STATE_DIR", tmp_path)
    monkeypatch.setattr(device.config, "WDA_BUNDLE_ID", "")
    (tmp_path / "wda_bundle").write_text("com.x.cached.xctrunner", encoding="utf-8")
    monkeypatch.setattr(
        device,
        "list_apps",
        lambda: [{"bundle_id": "com.apple.mobilesafari", "name": ""}],
    )
    assert device.detect_wda_bundle() is None


# ---- list_apps() persistent cache: same deep-sleep-empties-the-list failure
# mode as detect_wda_bundle's own cache, but now lives INSIDE list_apps()
# itself so every caller (helpers.open_app, admin._check_wda_installed,
# detect_wda_bundle) benefits without touching helpers.py.


def _ios_apps_proc(bundle_ids):
    """Stand-in for link.list_apps returning these bundle ids."""
    return lambda: [{"bundle_id": bid, "name": bid} for bid in bundle_ids]


def test_list_apps_caches_nonempty_result_to_disk(monkeypatch, tmp_path):
    monkeypatch.setattr(device.config, "STATE_DIR", tmp_path)
    monkeypatch.setattr(device.link, "list_apps", _ios_apps_proc(["com.a.App"]))
    apps = device.list_apps()
    assert apps == [{"bundle_id": "com.a.App", "name": "com.a.App"}]
    cached = json.loads((tmp_path / "apps_cache.json").read_text(encoding="utf-8"))
    assert cached == apps


def test_list_apps_falls_back_to_cache_when_live_list_empty(monkeypatch, tmp_path):
    # Deep sleep empties `ios apps --list` while apps stay installed
    # (docs/ERRORS.md) — an empty live result must never overwrite, or shadow,
    # a previously cached good list.
    monkeypatch.setattr(device.config, "STATE_DIR", tmp_path)
    (tmp_path / "apps_cache.json").write_text(
        json.dumps([{"bundle_id": "com.a.App", "name": "com.a.App"}]), encoding="utf-8"
    )
    monkeypatch.setattr(device.link, "list_apps", _ios_apps_proc([]))
    assert device.list_apps() == [{"bundle_id": "com.a.App", "name": "com.a.App"}]


def test_list_apps_empty_with_no_cache_returns_empty(monkeypatch, tmp_path):
    monkeypatch.setattr(device.config, "STATE_DIR", tmp_path)
    monkeypatch.setattr(device.link, "list_apps", _ios_apps_proc([]))
    assert device.list_apps() == []


def test_list_apps_fresh_nonempty_overwrites_stale_cache(monkeypatch, tmp_path):
    # A newly installed app must still be findable — staleness must not stick.
    monkeypatch.setattr(device.config, "STATE_DIR", tmp_path)
    (tmp_path / "apps_cache.json").write_text(
        json.dumps([{"bundle_id": "com.old.App", "name": "com.old.App"}]),
        encoding="utf-8",
    )
    monkeypatch.setattr(device.link, "list_apps", _ios_apps_proc(["com.new.App"]))
    assert device.list_apps() == [{"bundle_id": "com.new.App", "name": "com.new.App"}]
    cached = json.loads((tmp_path / "apps_cache.json").read_text(encoding="utf-8"))
    assert cached == [{"bundle_id": "com.new.App", "name": "com.new.App"}]


# ---- memoized_run(): scopes ios/netstat subprocess results to ONE caller-
# defined run (e.g. one doctor pass). Must never leak across two separate
# runs — "a stale check is a lying check" (project rule).


def test_list_devices_memoized_within_one_run(monkeypatch, tmp_path):
    monkeypatch.setattr(device.config, "STATE_DIR", tmp_path)
    calls = []

    def fake_list():
        calls.append("list")
        return ["00008150-X"]

    monkeypatch.setattr(device.link, "list_devices", fake_list)
    with device.memoized_run():
        assert device.list_devices() == ["00008150-X"]
        assert device.list_devices() == ["00008150-X"]
    assert len(calls) == 1  # spawned once, not twice, inside the block
    device.list_devices()
    assert len(calls) == 2  # outside the block, a fresh call always re-spawns


def test_list_apps_memoized_within_one_run(monkeypatch, tmp_path):
    monkeypatch.setattr(device.config, "STATE_DIR", tmp_path)
    calls = []

    def fake_apps():
        calls.append("apps")
        return [{"bundle_id": "com.a.App", "name": "com.a.App"}]

    monkeypatch.setattr(device.link, "list_apps", fake_apps)
    with device.memoized_run():
        device.list_apps()
        device.list_apps()
    assert len(calls) == 1
    with device.memoized_run():  # a separate, later run always re-spawns
        device.list_apps()
    assert len(calls) == 2


def test_netstat_memoized_within_one_run(monkeypatch):
    calls = []

    def fake_subprocess_run(cmd, **kw):
        calls.append(cmd)
        return _Proc("")

    monkeypatch.setattr(device.sys, "platform", "win32")
    monkeypatch.setattr(device.subprocess, "run", fake_subprocess_run)
    with device.memoized_run():
        device.port_exposed_to_lan(8100)
        device.port_exposed_to_lan(9100)  # different port, same netstat output
    assert len(calls) == 1
    device.port_exposed_to_lan(8100)
    assert len(calls) == 2  # outside the block: fresh spawn again


def test_memoized_run_thread_isolated(monkeypatch, tmp_path):
    """A module-global cache lets two overlapping memoized_run() blocks on
    different threads stomp each other's restore: A enters (previous=None),
    B enters while A is still open (previous=A's dict), A exits (global=None),
    B exits (global=A's now-orphaned, populated dict) — every block has
    exited, but the global is a non-None dict nothing ever clears, so a later
    call outside any block returns that frozen data instead of spawning.
    threading.local() gives each thread an independent cache, so neither
    thread's exit can touch the other's, and nothing survives past either
    block. Reproduces the exact interleaving via real threads + events, not a
    simulation, and checks purely through spawn counts (portable against the
    pre-fix module-global implementation too)."""
    monkeypatch.setattr(device.config, "STATE_DIR", tmp_path)
    calls = []
    lock = threading.Lock()

    def fake_list():
        with lock:
            calls.append("list")
        return ["00008150-X"]

    monkeypatch.setattr(device.link, "list_devices", fake_list)

    a_cached = threading.Event()
    b_cached = threading.Event()
    a_exited = threading.Event()
    errors = []

    def thread_a():
        try:
            with device.memoized_run():
                device.list_devices()  # call #1, cached inside A's own block
                a_cached.set()
                assert b_cached.wait(timeout=5), "B never entered its block"
        except Exception as exc:  # don't let a thread swallow its own failure
            errors.append(exc)
        finally:
            a_exited.set()

    def thread_b():
        try:
            assert a_cached.wait(timeout=5), "A never entered its block"
            with device.memoized_run():  # nested-in-time while A is still open
                device.list_devices()  # call #2, B's own block, not A's cache
                b_cached.set()
                assert a_exited.wait(timeout=5), "A never exited its block"
        except Exception as exc:
            errors.append(exc)

    ta = threading.Thread(target=thread_a)
    tb = threading.Thread(target=thread_b)
    ta.start()
    tb.start()
    ta.join(timeout=5)
    tb.join(timeout=5)

    assert not errors
    assert not ta.is_alive() and not tb.is_alive()
    assert len(calls) == 2  # each thread's own block spawned once

    device.list_devices()  # both blocks are closed now: must re-spawn, never
    assert len(calls) == 3  # return whichever thread's cache leaked through


def test_safe_kill_can_spare_the_process_tree(monkeypatch):
    # The tunnel and the forwards are CHILDREN of whatever launched them, so a
    # tree kill aimed at a stale viewer takes the phone link down with it.
    # Measured 2026-08-12: 11 green checks, then a second SideTap launch left
    # the tunnel and WDA dead.
    cmds = []
    monkeypatch.setattr(device.sys, "platform", "win32")
    monkeypatch.setattr(device, "_pid_image", lambda pid: "python.exe")
    monkeypatch.setattr(
        device.subprocess, "run", lambda cmd, **kw: cmds.append((cmd, kw))
    )
    assert device._safe_kill(4242, "python", tree=False) is True
    assert cmds[0][0] == ["taskkill", "/PID", "4242", "/F"]
    assert device._safe_kill(4242, "python") is True  # default still kills the tree
    assert "/T" in cmds[1][0]


# ---- start_tunnel: tunneld runs as its own admin task; start_tunnel starts
# that task only when tunneld is not answering, then waits for a tunnel, and
# names which of the two is missing when it gives up.


def _tunnel_env(monkeypatch, listed_on_call, tunneld_up=True):
    clock = [0.0]
    probes = []
    started = []
    monkeypatch.setattr(device.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(device.time, "sleep", lambda s: clock.__setitem__(0, clock[0] + s))
    monkeypatch.setattr(device.link, "tunneld_up", lambda timeout=3.0: tunneld_up)
    monkeypatch.setattr(
        device.subprocess, "run", lambda cmd, **kw: started.append(cmd) or _Proc("")
    )

    def fake_running(timeout=10.0):
        probes.append(clock[0])
        return len(probes) >= listed_on_call

    monkeypatch.setattr(device, "tunnel_running", fake_running)
    return clock, probes, started


def test_start_tunnel_returns_as_soon_as_a_tunnel_is_listed(monkeypatch):
    clock, probes, started = _tunnel_env(monkeypatch, listed_on_call=2)
    assert device.start_tunnel() is None
    assert len(probes) == 2
    assert clock[0] < device._TUNNEL_READY_TIMEOUT
    assert started == []  # tunneld was up: its task is not touched


def test_start_tunnel_starts_the_tunneld_task_when_tunneld_is_down(monkeypatch):
    _clock, _probes, started = _tunnel_env(monkeypatch, listed_on_call=1, tunneld_up=False)
    device.start_tunnel()
    assert started and started[0][:3] == ["schtasks", "/Run", "/TN"]
    assert started[0][3] == device.config.TUNNELD_TASK


def test_start_tunnel_names_tunneld_when_it_never_comes_up(monkeypatch):
    _tunnel_env(monkeypatch, listed_on_call=10_000, tunneld_up=False)
    with pytest.raises(device.DeviceError) as exc:
        device.start_tunnel()
    assert "tunneld is not running" in str(exc.value)


def test_start_tunnel_names_the_phone_when_tunneld_has_no_tunnel(monkeypatch):
    clock, _probes, _started = _tunnel_env(monkeypatch, listed_on_call=10_000)
    with pytest.raises(device.DeviceError) as exc:
        device.start_tunnel()
    assert "no tunnel to the phone" in str(exc.value)
    assert clock[0] >= device._TUNNEL_READY_TIMEOUT

"""device.sign_app: signs with wda-sign and installs through the tunnel, so no cable is needed.

`ios sign app` looked the phone up on usbmux before it signed, and its --install went
through usbmux too; with the phone on Wi-Fi only, both failed.
"""

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from phone_harness import device, link  # noqa: E402

UDID = "00008020-000A1B2C3D4E5F60"


@pytest.fixture
def signer(monkeypatch, tmp_path):
    """A fake wda-sign that writes its --output, and a recorded tunnel install."""
    calls = {"sign": [], "install": []}

    def fake_run(args, **kwargs):
        calls["sign"].append(args)
        out = next(a.split("=", 1)[1] for a in args if a.startswith("--output="))
        Path(out).write_bytes(b"signed")
        return subprocess.CompletedProcess(args, 0, "signed ok", "")

    def fake_install(package, udid=None, timeout=300.0):
        assert Path(package).read_bytes() == b"signed"  # installs what was signed, while it exists
        calls["install"].append((Path(package).name, udid))

    monkeypatch.setattr(device, "wda_sign_path", lambda: "wda-sign.exe")
    monkeypatch.setattr(device.subprocess, "run", fake_run)
    monkeypatch.setattr(link, "install_app", fake_install)
    monkeypatch.setattr(device, "current_udid", lambda: UDID)
    monkeypatch.setattr(device, "_run", lambda *a, **k: pytest.fail("go-ios must not be called"))
    return calls


def test_signs_with_wda_sign_and_installs_through_the_tunnel(signer, tmp_path):
    out = device.sign_app(tmp_path / "WebDriverAgent.ipa", tmp_path / "id.p12", tmp_path / "p.mobileprovision",
                          p12password="pw", bundleid="com.example.wda")
    (args,) = signer["sign"]
    assert args[0] == "wda-sign.exe"
    assert "--p12password=pw" in args and "--bundleid=com.example.wda" in args
    assert signer["install"] == [("WebDriverAgent.ipa", UDID)]
    assert out == "signed ok"


def test_a_failed_sign_installs_nothing(signer, monkeypatch, tmp_path):
    monkeypatch.setattr(device.subprocess, "run",
                        lambda args, **k: subprocess.CompletedProcess(args, 1, "", "provisioning profile has expired"))
    with pytest.raises(device.DeviceError, match="profile has expired"):
        device.sign_app(tmp_path / "a.ipa", tmp_path / "id.p12", tmp_path / "p.mobileprovision")
    assert signer["install"] == []


def test_a_failed_install_is_named(signer, monkeypatch, tmp_path):
    def broken(*a, **k):
        raise link.LinkError("no tunnel to the phone")

    monkeypatch.setattr(link, "install_app", broken)
    with pytest.raises(device.DeviceError, match="installing the signed app failed: no tunnel"):
        device.sign_app(tmp_path / "a.ipa", tmp_path / "id.p12", tmp_path / "p.mobileprovision")


def test_missing_wda_sign_says_how_to_build_it(monkeypatch, tmp_path):
    monkeypatch.setattr(device, "wda_sign_path", lambda: None)
    with pytest.raises(device.DeviceError, match="build_wda_sign.ps1"):
        device.sign_app(tmp_path / "a.ipa", tmp_path / "id.p12", tmp_path / "p.mobileprovision")


def test_link_install_app_uses_the_installation_proxy_over_the_tunnel(monkeypatch, tmp_path):
    seen = {}

    def fake_run(fn, udid=None, timeout=30.0):
        seen["udid"], seen["timeout"] = udid, timeout
        seen["fn"] = fn

    monkeypatch.setattr(link, "run", fake_run)
    link.install_app(tmp_path / "x.ipa", UDID)
    assert seen["udid"] == UDID and seen["timeout"] == 300.0

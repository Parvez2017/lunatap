# Copyright 2026 Google LLC
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# Modified by the Lunatap Authors in 2026 (forked from google/artemis).

"""WebDriverAgent-backed simulator driver, tested without Xcode or a simulator."""

import io

import pytest

from lunatap.drivers import factory
from lunatap.drivers.ios import discovery, wda_simulator_driver, xcode_driver
from lunatap.drivers.ios.physical_driver import PhysicalIosDriver
from lunatap.drivers.ios.wda_simulator_driver import WdaSimulatorDriver
from lunatap.drivers.ios.xcode_driver import XcodeSimulatorDriver

SIM_UDID = "DE345DD3-5792-4DAD-B863-144682629565"
SIM_ENTRY = {"udid": SIM_UDID, "name": "iPhone 17 Pro", "state": "Booted", "runtime": "iOS-26-5"}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in (
        "ARTEMIS_IOS_SIM_BACKEND",
        "ARTEMIS_IOS_WDA_URL",
        "ARTEMIS_IOS_WDA_HOST",
        "ARTEMIS_IOS_WDA_PORT",
        "ARTEMIS_IOS_WDA_XCTESTRUN",
    ):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture(autouse=True)
def _ports_free(monkeypatch):
    """Keep port choice off the host: tests that need an owner patch it themselves."""

    async def free(port):
        return wda_simulator_driver.PortOwner("free")

    monkeypatch.setattr(wda_simulator_driver, "port_owner", free)


class _FakeWda:
    def __init__(self, name="iPhone 17 Pro", is_simulator=True):
        self.base_url = "http://127.0.0.1:8100"
        self.info = {"name": name, "isSimulator": is_simulator}
        self.session_id = None

    async def device_info(self, timeout=10.0):
        return self.info

    async def open_session(self, adopt_existing=False):
        self.session_id = "sim-session"
        return self.session_id

    async def close_session(self):
        self.session_id = None

    async def screenshot_png(self):
        from PIL import Image

        buffer = io.BytesIO()
        Image.new("RGB", (1206, 2622)).save(buffer, format="PNG")
        return buffer.getvalue()

    async def window_size(self):
        return 402.0, 874.0

    async def source_json(self):
        return {
            "type": "XCUIElementTypeApplication",
            "rect": {"x": 0, "y": 0, "width": 402, "height": 874},
            "children": [
                {
                    "type": "XCUIElementTypeButton",
                    "label": "General",
                    "name": "com.apple.settings.general",
                    "rect": {"x": 16, "y": 300, "width": 370, "height": 52},
                }
            ],
        }


# --- Backend selection ---


@pytest.mark.parametrize(
    ("major", "expected"), [(26, "wda"), (15, "wda"), (None, "wda"), (27, "xcode"), (28, "xcode")]
)
def test_auto_backend_follows_xcode_version(monkeypatch, major, expected):
    monkeypatch.setattr(discovery, "xcode_major_version_sync", lambda: major)
    assert discovery.simulator_backend() == expected


@pytest.mark.parametrize("forced", ["wda", "xcode", " WDA "])
def test_backend_env_overrides_auto(monkeypatch, forced):
    monkeypatch.setattr(discovery, "xcode_major_version_sync", lambda: 26)
    monkeypatch.setenv("ARTEMIS_IOS_SIM_BACKEND", forced)
    assert discovery.simulator_backend() == forced.strip().lower()


def test_backend_env_rejects_unknown_values(monkeypatch):
    monkeypatch.setenv("ARTEMIS_IOS_SIM_BACKEND", "idb")
    with pytest.raises(ValueError, match="ARTEMIS_IOS_SIM_BACKEND"):
        discovery.simulator_backend()


@pytest.mark.parametrize(
    ("backend", "driver_class"), [("wda", WdaSimulatorDriver), ("xcode", XcodeSimulatorDriver)]
)
def test_factory_routes_booted_simulators_by_backend(monkeypatch, backend, driver_class):
    monkeypatch.setenv("ARTEMIS_IOS_SIM_BACKEND", backend)
    assert factory.ios_driver_class("booted") is driver_class


def test_factory_routes_simulator_udids_to_wda_driver(monkeypatch):
    monkeypatch.setenv("ARTEMIS_IOS_SIM_BACKEND", "wda")
    monkeypatch.setattr(discovery, "list_ios_simulators_sync", lambda *a, **k: [SIM_ENTRY])
    assert factory.ios_driver_class(SIM_UDID) is WdaSimulatorDriver


def test_wda_simulator_driver_keeps_isinstance_contracts():
    # Recording (XcodeSimulatorDriver) and diagnostics (PhysicalIosDriver ->
    # "wda" hierarchy backend) both key off these base classes.
    assert issubclass(WdaSimulatorDriver, XcodeSimulatorDriver)
    assert issubclass(WdaSimulatorDriver, PhysicalIosDriver)


# --- Host requirements ---


def _xcode_version(text):
    async def fake(*arguments, timeout=30.0):
        assert arguments[0] == "xcodebuild"
        return text.encode()

    return fake


@pytest.mark.asyncio
async def test_wda_driver_accepts_xcode_26(monkeypatch):
    monkeypatch.setattr(
        xcode_driver, "run_xcrun", _xcode_version("Xcode 26.6\nBuild version 17F113")
    )
    monkeypatch.setattr(xcode_driver.sys, "platform", "darwin")
    await WdaSimulatorDriver(device_id=SIM_UDID)._require_ios_host()


@pytest.mark.asyncio
async def test_native_driver_on_xcode_26_points_at_the_wda_backend(monkeypatch):
    monkeypatch.setattr(
        xcode_driver, "run_xcrun", _xcode_version("Xcode 26.6\nBuild version 17F113")
    )
    monkeypatch.setattr(xcode_driver.sys, "platform", "darwin")
    with pytest.raises(RuntimeError, match="ARTEMIS_IOS_SIM_BACKEND=wda"):
        await XcodeSimulatorDriver(device_id=SIM_UDID)._require_ios_host()


@pytest.mark.asyncio
async def test_wda_driver_rejects_pre_devicectl_xcode(monkeypatch):
    monkeypatch.setattr(xcode_driver, "run_xcrun", _xcode_version("Xcode 14.3\nBuild version 1"))
    monkeypatch.setattr(xcode_driver.sys, "platform", "darwin")
    with pytest.raises(RuntimeError, match="Xcode 15 or later"):
        await WdaSimulatorDriver(device_id=SIM_UDID)._require_ios_host()


# --- WDA endpoint selection and identity ---


@pytest.mark.asyncio
async def test_candidates_default_to_localhost_8100():
    assert await WdaSimulatorDriver(SIM_UDID)._wda_candidates() == ["http://127.0.0.1:8100"]


@pytest.mark.asyncio
async def test_candidates_honor_port_and_explicit_url(monkeypatch):
    monkeypatch.setenv("ARTEMIS_IOS_WDA_PORT", "8105")
    monkeypatch.setenv("ARTEMIS_IOS_WDA_URL", "http://10.0.0.5:9000")
    assert await WdaSimulatorDriver(SIM_UDID)._wda_candidates() == [
        "http://10.0.0.5:9000",
        "http://127.0.0.1:8105",
    ]


@pytest.mark.parametrize("raw", ["abc", "0", "70000"])
def test_invalid_port_is_rejected(monkeypatch, raw):
    monkeypatch.setenv("ARTEMIS_IOS_WDA_PORT", raw)
    with pytest.raises(ValueError, match="ARTEMIS_IOS_WDA_PORT"):
        WdaSimulatorDriver._wda_port()


@pytest.mark.asyncio
async def test_identity_accepts_the_selected_simulator():
    driver = WdaSimulatorDriver(SIM_UDID)
    driver._wda = _FakeWda()
    await driver._verify_wda_device(SIM_ENTRY)


@pytest.mark.asyncio
async def test_identity_rejects_a_physical_endpoint():
    driver = WdaSimulatorDriver(SIM_UDID)
    driver._wda = _FakeWda(name="iPhone", is_simulator=False)
    with pytest.raises(RuntimeError, match="not running on a simulator"):
        await driver._verify_wda_device(SIM_ENTRY)


@pytest.mark.asyncio
async def test_identity_rejects_another_simulators_server():
    driver = WdaSimulatorDriver(SIM_UDID)
    driver._wda = _FakeWda(name="iPad Air")
    with pytest.raises(RuntimeError, match="not the selected"):
        await driver._verify_wda_device(SIM_ENTRY)


# --- Starting WDA ---


@pytest.mark.asyncio
async def test_start_without_xctestrun_explains_setup(monkeypatch):
    monkeypatch.setattr(wda_simulator_driver, "default_simulator_xctestrun", lambda: None)
    with pytest.raises(RuntimeError, match="setup_ios_wda_simulator.sh"):
        await WdaSimulatorDriver(SIM_UDID)._start_wda_server()


@pytest.mark.asyncio
async def test_start_hosts_the_configured_xctestrun_on_the_chosen_port(monkeypatch, tmp_path):
    xctestrun = tmp_path / "WebDriverAgentRunner_iphonesimulator26.5-arm64.xctestrun"
    xctestrun.write_text("plist")
    monkeypatch.setenv("ARTEMIS_IOS_WDA_XCTESTRUN", str(xctestrun))
    monkeypatch.setenv("ARTEMIS_IOS_WDA_PORT", "8102")
    driver = WdaSimulatorDriver(SIM_UDID)
    started = []

    async def fake_start(path):
        started.append(path)

    monkeypatch.setattr(driver, "_start_xctest_session", fake_start)
    await driver._start_wda_server()
    assert started == [str(xctestrun)]
    assert driver._xctest_env() == {"TEST_RUNNER_USE_PORT": "8102"}


def test_default_xctestrun_discovery_prefers_newest_simulator_build(monkeypatch, tmp_path):
    monkeypatch.setattr(wda_simulator_driver, "DEFAULT_SIM_XCTESTRUN_DIR", tmp_path)
    assert wda_simulator_driver.default_simulator_xctestrun() is None
    (tmp_path / "WebDriverAgentRunner_iphoneos26.5-arm64.xctestrun").write_text("x")
    sim = tmp_path / "WebDriverAgentRunner_iphonesimulator26.5-arm64.xctestrun"
    sim.write_text("x")
    assert wda_simulator_driver.default_simulator_xctestrun() == sim


# --- Connected behavior ---


@pytest.fixture
def connected(monkeypatch):
    driver = WdaSimulatorDriver(SIM_UDID)

    async def resolve():
        driver._device_id = SIM_UDID
        return SIM_ENTRY

    async def nothing(*_args):
        return None

    async def ensure():
        return _FakeWda()

    monkeypatch.setattr(driver, "_resolve_device", resolve)
    monkeypatch.setattr(driver, "_prepare_device", nothing)
    monkeypatch.setattr(driver, "_ensure_wda", ensure)
    return driver


@pytest.mark.asyncio
async def test_connect_observes_in_pixels_with_retina_scale(connected):
    await connected.connect()
    data = await connected.get_screen_data()
    assert data.platform == "ios"
    assert (data.width, data.height) == (1206, 2622)
    general = next(e for e in data.ui_elements if e.get("text") == "General")
    # 16pt * 3x = 48px; bounds stay in screenshot pixels like Android.
    assert general["parsed_bounds"]["left"] == 48
    assert connected._scaled_point(603, 1311) == pytest.approx((201.0, 437.0))


@pytest.mark.asyncio
async def test_app_lifecycle_uses_simctl(connected, monkeypatch):
    calls = []

    async def fake_xcrun(*arguments, timeout=30.0):
        calls.append(arguments)
        return b""

    monkeypatch.setattr(xcode_driver, "run_xcrun", fake_xcrun)
    await connected.connect()
    await connected.launch_app("com.apple.Preferences")
    await connected.stop_app("com.apple.Preferences")
    await connected.open_url("https://example.com")
    assert calls == [
        ("simctl", "launch", SIM_UDID, "com.apple.Preferences"),
        ("simctl", "terminate", SIM_UDID, "com.apple.Preferences"),
        ("simctl", "openurl", SIM_UDID, "https://example.com"),
    ]


@pytest.mark.asyncio
async def test_shell_stays_unsupported(connected):
    with pytest.raises(NotImplementedError):
        await connected.execute_shell("ls")


def test_recorder_is_native_simctl_recording():
    from lunatap.drivers.ios.recording import IosScreenRecorder

    assert isinstance(WdaSimulatorDriver(SIM_UDID)._new_recorder(), IosScreenRecorder)


# --- Diagnostics probe ---


def _patch_probe(monkeypatch, xcode="Xcode 26.6\nBuild version 17F113"):
    from unittest.mock import AsyncMock

    module = "lunatap.core.diagnostics.probes.ios_probe"
    monkeypatch.setattr(f"{module}.simctl_available", lambda: True)
    monkeypatch.setattr(f"{module}.run_xcrun", AsyncMock(return_value=xcode.encode()))
    monkeypatch.setattr(f"{module}.list_ios_simulators", AsyncMock(return_value=[SIM_ENTRY]))
    monkeypatch.setattr(f"{module}.list_core_devices", AsyncMock(return_value=[]))


@pytest.mark.asyncio
async def test_probe_asks_for_a_wda_build_on_xcode_26(monkeypatch):
    from lunatap.core.diagnostics.probes.ios_probe import IosDeviceProbe
    from lunatap.core.diagnostics.schema import ProbeStatus

    _patch_probe(monkeypatch)
    monkeypatch.setenv("ARTEMIS_IOS_SIM_BACKEND", "wda")
    monkeypatch.setattr(wda_simulator_driver, "default_simulator_xctestrun", lambda: None)
    result = await IosDeviceProbe().probe()
    assert result.status is ProbeStatus.WARN
    assert result.summary == "WebDriverAgent Build Needed"
    assert result.actions[0].payload == "bash scripts/setup_ios_wda_simulator.sh"
    assert result.metadata["xcode_27_or_newer"] is False


@pytest.mark.asyncio
async def test_probe_passes_on_xcode_26_once_wda_is_built(monkeypatch, tmp_path):
    from lunatap.core.diagnostics.probes.ios_probe import IosDeviceProbe
    from lunatap.core.diagnostics.schema import ProbeStatus

    _patch_probe(monkeypatch)
    monkeypatch.setenv("ARTEMIS_IOS_SIM_BACKEND", "wda")
    xctestrun = tmp_path / "WebDriverAgentRunner_iphonesimulator26.5-arm64.xctestrun"
    monkeypatch.setattr(wda_simulator_driver, "default_simulator_xctestrun", lambda: xctestrun)
    result = await IosDeviceProbe().probe()
    assert result.status is ProbeStatus.PASS
    assert result.metadata["ios_automation_ready"] is True
    assert result.metadata["simulator_backend"] == "wda"


@pytest.mark.asyncio
async def test_probe_forced_native_backend_still_needs_xcode_27(monkeypatch):
    from lunatap.core.diagnostics.probes.ios_probe import IosDeviceProbe
    from lunatap.core.diagnostics.schema import ProbeStatus

    _patch_probe(monkeypatch)
    monkeypatch.setenv("ARTEMIS_IOS_SIM_BACKEND", "xcode")
    result = await IosDeviceProbe().probe()
    assert result.status is ProbeStatus.WARN
    assert result.summary == "Xcode 27+ Required"
    assert result.metadata["ios_automation_ready"] is False

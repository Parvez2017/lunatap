# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# Modified by the Lunatap Authors in 2026 (forked from google/artemis).

"""Unit tests for the end-to-end device observation smoke test (no real device)."""

import asyncio
import base64
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from lunatap.core.diagnostics import smoke_test_device
from lunatap.core.diagnostics import device_smoke
from lunatap.runtime.device_lock import DeviceExecutionLock, DeviceLockOwner

SERIAL = "59100DLCR0033X"
FAKE_JPEG = b"\xff\xd8\xff" + bytes(range(256)) * 8 + b"\xff\xd9"


@pytest.fixture(autouse=True)
def idle_devices(monkeypatch):
    """No live lock owners unless a test installs some."""
    monkeypatch.setattr(DeviceExecutionLock, "get_active_owners", classmethod(lambda cls: {}))
    monkeypatch.delenv("ARTEMIS_DEVICE_ID", raising=False)
    monkeypatch.delenv("ADB_DEVICE_SERIAL", raising=False)


@pytest.fixture
def patch_controller(monkeypatch):
    """Install a fake ``_get_controller`` and return the controller it hands out."""

    def _install(get_screen_data=None, factory=None):
        controller = MagicMock()
        controller.ctx = SimpleNamespace(device=SimpleNamespace(device_id=SERIAL))
        if get_screen_data is not None:
            controller.get_screen_data = get_screen_data
        calls: list[str | None] = []

        def _fake_get_controller(device_serial=None, target_platform=None):
            calls.append(device_serial)
            if factory is not None:
                if target_platform is None:
                    # Preserve the Android call shape: no platform kwarg.
                    return factory(device_serial)
                return factory(device_serial, target_platform=target_platform)
            return controller

        monkeypatch.setattr("lunatap.mcp.device_controllers.get_controller", _fake_get_controller)
        controller.calls = calls
        return controller

    return _install


def _screen_data(elements, jpeg=FAKE_JPEG):
    return SimpleNamespace(
        base64=base64.b64encode(jpeg).decode("ascii"),
        elements=elements,
        width=1080,
        height=2400,
    )


def _owner(device_id: str, description: str = "task-1") -> DeviceLockOwner:
    return DeviceLockOwner(
        pid=4242,
        process_created_at=1.0,
        token="tok",
        device_id=device_id,
        description=description,
        acquired_at="2026-09-09T00:00:00Z",
    )


# --------------------------------------------------------------------------- #
# iOS path
# --------------------------------------------------------------------------- #

IOS_UDID = "AAAA-1111-0000"


def _ios_screen_data():
    return SimpleNamespace(
        base64=base64.b64encode(FAKE_JPEG).decode("ascii"),
        elements='<hierarchy><node class="XCUIElementTypeButton"/></hierarchy>',
        width=1170,
        height=2532,
    )


def _ios_controller(device_id=IOS_UDID):
    controller = MagicMock()
    controller.ctx = SimpleNamespace(
        device=SimpleNamespace(device_id=device_id, mobile_platform="ios")
    )
    controller._driver = SimpleNamespace()  # not a PhysicalIosDriver -> xcode
    return controller


@pytest.mark.asyncio
async def test_ios_smoke_runs_observation_helper_and_reports_xcode(patch_controller, monkeypatch):
    """iOS observes through the lease-owning helper on the caller's loop."""
    observe = AsyncMock(return_value=_ios_screen_data())
    monkeypatch.setattr(device_smoke, "observe_ios_controller", observe)
    captured: dict = {}

    def factory(device_serial, target_platform=None):
        captured["device_serial"] = device_serial
        captured["target_platform"] = target_platform
        return _ios_controller()

    patch_controller(factory=factory)

    result = await smoke_test_device(device_serial=IOS_UDID, platform="ios")

    assert result["ok"] is True
    assert result["error"] is None
    assert result["fix"] == []
    assert result["serial"] == IOS_UDID
    assert result["hierarchy_backend"] == "xcode"
    assert captured == {"device_serial": IOS_UDID, "target_platform": "ios"}
    observe.assert_awaited_once()


@pytest.mark.asyncio
async def test_ios_smoke_ignores_adb_serial_env(patch_controller, monkeypatch):
    """ADB_DEVICE_SERIAL must never seed an iOS target."""
    monkeypatch.setenv("ADB_DEVICE_SERIAL", "emulator-5554")
    observe = AsyncMock(return_value=_ios_screen_data())
    monkeypatch.setattr(device_smoke, "observe_ios_controller", observe)

    def factory(device_serial, target_platform=None):
        return _ios_controller(device_id=device_serial or "booted")

    patch_controller(factory=factory)

    result = await smoke_test_device(platform="ios")

    assert result["ok"] is True
    # Serial comes from ctx after canonical resolution — never the ADB serial.
    assert result["serial"] == "booted"
    assert "emulator-5554" not in str(result)


@pytest.mark.asyncio
async def test_ios_smoke_skips_unscoped_android_busy_guard(patch_controller, monkeypatch):
    """The helper performs scope-correct admission; an Android owner on the
    same serial text must not veto the iOS smoke."""
    monkeypatch.setattr(
        DeviceExecutionLock,
        "get_active_owners",
        classmethod(lambda cls: {IOS_UDID: _owner(IOS_UDID, "android task")}),
    )
    observe = AsyncMock(return_value=_ios_screen_data())
    monkeypatch.setattr(device_smoke, "observe_ios_controller", observe)
    patch_controller(factory=lambda device_serial, target_platform=None: _ios_controller())

    result = await smoke_test_device(device_serial=IOS_UDID, platform="ios")

    assert result["ok"] is True
    observe.assert_awaited_once()


@pytest.mark.asyncio
async def test_ios_smoke_failure_maps_to_ios_fixes(patch_controller, monkeypatch):
    observe = AsyncMock(side_effect=RuntimeError("WebDriverAgent did not answer within 45s"))
    monkeypatch.setattr(device_smoke, "observe_ios_controller", observe)
    patch_controller(factory=lambda device_serial, target_platform=None: _ios_controller())

    result = await smoke_test_device(device_serial=IOS_UDID, platform="ios")

    assert result["ok"] is False
    assert "WebDriverAgent" in result["error"]
    assert any("WebDriverAgent" in step for step in result["fix"])
    assert not any("adb " in step for step in result["fix"])


@pytest.mark.asyncio
async def test_ios_smoke_init_failure_reports_ios_target(patch_controller):
    def _boom(device_serial, target_platform=None):
        raise Exception("iOS support requires macOS and Xcode 27 or later")

    patch_controller(factory=_boom)

    result = await smoke_test_device(device_serial=IOS_UDID, platform="ios")

    assert result["ok"] is False
    assert "Failed to initialize iOS device controller" in result["error"]
    assert any("Xcode" in step for step in result["fix"])

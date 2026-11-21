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

"""End-to-end device observation smoke test.

The readiness probe only proves that ``xcrun simctl`` / ``devicectl`` list the
device. The common "device shows up but Lunatap cannot observe it" failure is
WebDriverAgent (or Xcode's device tools) not answering, which otherwise only
surfaces as a failed task. :func:`smoke_test_device` performs exactly the
observation the ``mobile_get_device_state`` MCP tool performs -- build (or
reuse) the cached controller through ``lunatap.mcp.device_controllers`` and
observe the screen -- and reports a structured verdict with concrete repair
guidance instead of raising.

The observation runs through ``observe_ios_controller``, which takes the
device lease itself, so a device another task holds is reported as busy rather
than disturbed. Nothing is left behind: no screenshot is written to disk and
the cached controller is kept for reuse.
"""

from __future__ import annotations

import asyncio
import base64
import os
import time
from typing import Any

from lunatap.runtime.ios_observation import observe_ios_controller
from third_party.mobile_use.utils.logger import get_logger

logger = get_logger(__name__)


#: A real screenshot of any phone screen is tens of kilobytes; a tiny image means
#: the capture returned a placeholder.
_MIN_PLAUSIBLE_SCREENSHOT_BYTES = 512

_SERIAL_PLACEHOLDER = "<serial>"


def _serial_or_placeholder(serial: str | None) -> str:
    return serial or _SERIAL_PLACEHOLDER


def _ios_fix_for_error(error: str | None, serial: str | None) -> list[str]:
    """iOS repair steps (simctl/Xcode for simulators, devicectl/WDA for hardware)."""
    if not error:
        return []
    s = serial or "<UDID>"
    text = (error or "").lower()
    if "busy" in text or "another task" in text:
        return [
            "Wait for the running task to finish, or stop it with mobile_manage_task(action='stop', ...).",
            "Then rerun the smoke test.",
        ]
    if "approv" in text or "authoriz" in text or "permission" in text:
        return [
            "Approve Artemis's agent access in Xcode (the first run opens a workspace approval prompt; choose persistent approval).",
            "Confirm the permitted agent and workspace under Xcode > Settings > Agentic Tools.",
        ]
    if "setup_ios_wda_simulator" in text or ("webdriveragent" in text and "simulator" in text):
        return [
            "Build WebDriverAgent for simulators once (no signing needed): bash scripts/setup_ios_wda_simulator.sh",
            "Or point ARTEMIS_IOS_WDA_URL at a WebDriverAgent you already run; use ARTEMIS_IOS_WDA_PORT per simulator for parallel runs.",
        ]
    if "xcode" in text and "or later" in text or "macos" in text or "darwin" in text:
        return [
            "Install Xcode 27 or later for native simulator interaction, or use Xcode 15+ with "
            "ARTEMIS_IOS_SIM_BACKEND=wda (WebDriverAgent). Select it: "
            "sudo xcode-select -s /Applications/Xcode.app/Contents/Developer",
        ]
    if (
        "webdriveragent" in text
        or "wda" in text
        or "physical" in text
        or "pair" in text
        or "trust" in text
        or "devicectl" in text
        or "developer mode" in text
    ):
        return [
            f"List paired devices: xcrun devicectl list devices; check capture: xcrun devicectl device capture screenshot --device {s} --destination /tmp/ios.png",
            "Pair the device (plug in, tap Trust), enable Developer Mode under Settings > Privacy & Security, and ensure WebDriverAgent is installed "
            "(build-for-testing via Xcode, or set ARTEMIS_IOS_WDA_URL to a running server).",
        ]
    if "simulator" in text or "udid" in text or "boot" in text or "not available" in text:
        return [
            f"List simulators: xcrun simctl list devices; boot one with: xcrun simctl boot {s}",
            "Create a simulator in Xcode > Settings > Platforms if none exist.",
        ]
    return [
        "Check native capture manually: xcrun simctl io booted screenshot /tmp/ios.png",
        'Verify Xcode MCP access: echo \'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}\' | xcrun mcpbridge',
    ]


def fix_for_error(error: str | None, serial: str | None, platform: str = "ios") -> list[str]:
    """Map a smoke-test failure to concrete recovery steps."""
    del platform  # iOS is the only platform; kept for call-site compatibility
    return _ios_fix_for_error(error, serial)


#: Exceptions the smoke test must never swallow.
_PASSTHROUGH_EXCEPTIONS: tuple[type[BaseException], ...] = (
    KeyboardInterrupt,
    SystemExit,
    asyncio.CancelledError,
)


def _describe_failure(exc: BaseException) -> str:
    """Some driver errors derive from ``BaseException`` and carry an empty message."""
    text = str(exc).strip()
    name = type(exc).__name__
    if not text:
        return name
    return text if name in ("Exception", "RuntimeError") else f"{name}: {text}"


def _resolve_requested_serial(device_serial: str | None) -> str | None:
    """Mirror ``get_controller``'s serial resolution (explicit arg, then env)."""
    return device_serial or os.environ.get("ARTEMIS_DEVICE_ID")


def _count_elements(elements: Any) -> int | None:
    if elements is None:
        return None
    if isinstance(elements, str):
        return elements.count("<node")
    if isinstance(elements, dict):
        return 1
    try:
        return len(elements)
    except TypeError:
        return None


def _decoded_length(b64: Any) -> int | None:
    if not isinstance(b64, str) or not b64:
        return None
    try:
        return len(base64.b64decode(b64))
    except (ValueError, TypeError):
        return None


async def smoke_test_device(
    device_serial: str | None = None,
    timeout_seconds: float = 20.0,
    platform: str = "ios",
) -> dict[str, Any]:
    """Observe the device exactly like ``mobile_get_device_state`` and report a verdict.

    Exercises the native iOS path: ``simctl`` + WebDriverAgent or Xcode's device
    tools for simulators, ``devicectl`` + WebDriverAgent for physical devices.

    Never raises. Returns::

        {"ok": bool, "serial": str | None, "elapsed_seconds": float,
         "screenshot_bytes": int | None, "element_count": int | None,
         "hierarchy_backend": "xcode" | "wda" | None,
         "error": str | None, "fix": list[str]}
    """
    del platform  # iOS is the only platform; kept for call-site compatibility
    started = time.monotonic()
    requested_serial = _resolve_requested_serial(device_serial)
    result: dict[str, Any] = {
        "ok": False,
        "serial": requested_serial,
        "elapsed_seconds": 0.0,
        "screenshot_bytes": None,
        "element_count": None,
        "hierarchy_backend": None,
        "error": None,
        "fix": [],
    }

    def _finish(error: str | None = None, cause: str | None = None) -> dict[str, Any]:
        """Record the verdict; ``cause`` is the raw failure text used to pick the fix."""
        result["elapsed_seconds"] = round(time.monotonic() - started, 3)
        result["error"] = error
        result["ok"] = error is None
        result["fix"] = fix_for_error(cause if cause is not None else error, result["serial"])
        return result

    try:
        from lunatap.mcp import device_controllers

        # Controller construction enumerates simctl/devicectl; keep it off the loop.
        controller = await asyncio.wait_for(
            asyncio.to_thread(
                device_controllers.get_controller,
                device_serial=device_serial,
                target_platform="ios",
            ),
            timeout_seconds,
        )
    except TimeoutError:
        return _finish(f"iOS driver initialization did not respond within {timeout_seconds:g}s")
    except _PASSTHROUGH_EXCEPTIONS:
        raise
    except BaseException as exc:  # pylint: disable=broad-exception-caught
        cause = _describe_failure(exc)
        return _finish(f"Failed to initialize iOS device controller: {cause}", cause)

    controller_ctx = getattr(controller, "ctx", None)
    device = getattr(controller_ctx, "device", None)
    device_id = getattr(device, "device_id", None)
    if isinstance(device_id, str) and device_id:
        result["serial"] = device_id

    remaining = max(0.5, timeout_seconds - (time.monotonic() - started))
    try:
        # The helper owns lease + connect + capture + disconnect + release and is
        # cancellation-safe; it also refuses a device another task holds.
        device_data = await asyncio.wait_for(observe_ios_controller(controller), remaining)
    except TimeoutError:
        return _finish(f"iOS session capture did not respond within {timeout_seconds:g}s")
    except _PASSTHROUGH_EXCEPTIONS:
        raise
    except BaseException as exc:  # pylint: disable=broad-exception-caught
        cause = _describe_failure(exc)
        return _finish(f"Screen capture failed: {cause}", cause)

    result["screenshot_bytes"] = _decoded_length(getattr(device_data, "base64", None))
    result["element_count"] = _count_elements(getattr(device_data, "elements", None))
    from lunatap.drivers.ios.physical_driver import PhysicalIosDriver

    # Report the canonical UDID the observation lease actually resolved.
    resolved_serial = getattr(device, "device_id", None)
    if isinstance(resolved_serial, str) and resolved_serial:
        result["serial"] = resolved_serial
    result["hierarchy_backend"] = (
        "wda" if isinstance(getattr(controller, "_driver", None), PhysicalIosDriver) else "xcode"
    )

    if result["screenshot_bytes"] is None:
        return _finish("Screen capture returned no screenshot data")
    if result["screenshot_bytes"] < _MIN_PLAUSIBLE_SCREENSHOT_BYTES:
        return _finish(
            f"Screenshot capture failed (driver returned a {result['screenshot_bytes']}-byte placeholder image)"
        )
    if not result["element_count"]:
        return _finish(
            "Xcode accessibility tree returned no UI elements (screenshot worked, hierarchy did not)"
        )
    return _finish(None)


__all__ = ["fix_for_error", "smoke_test_device"]

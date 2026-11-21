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

"""Physical iPhone/iPad support via ``devicectl`` plus WebDriverAgent.

Lifecycle operations (install, launch, terminate, app list, URL open,
screenshots) run through ``xcrun devicectl`` against a paired device. Xcode's
``DeviceInteraction*`` MCP tools accept simulators only, so UI observation and
input on hardware go through WebDriverAgent (WDA), the XCUITest bridge Appium
uses: the driver finds a signed WDA runner on the device, launches it through
``devicectl``, and talks to its HTTP endpoint over the CoreDevice tunnel, a
LAN address, or a forwarded port.

Prerequisites surface as actionable errors: the device must appear in
``devicectl list devices`` as ``paired`` and ``connected`` (USB or network),
Developer Mode must be on, and a WebDriverAgent runner must be installed —
set ``ARTEMIS_IOS_WDA_URL`` to reach an existing server directly.
"""

import asyncio
import base64
from io import BytesIO
import json
import os
from pathlib import Path
import plistlib
import re
import tempfile
from typing import Any, Literal

from PIL import Image

from lunatap.drivers.base import KeyCode, ScreenData, SwipeDirection
from lunatap.drivers.ios.discovery import (
    BOOTED_SIMULATOR_ID,
    WDA_MIN_XCODE,
    device_matches_identifier,
    devicectl_screenshot,
    is_physical_ios,
    list_core_devices,
    reap_process,
    run_xcrun,
)
from lunatap.drivers.ios.physical_recording import PhysicalIosRecorder
from lunatap.drivers.ios.recording import IosScreenRecorder
from lunatap.drivers.ios.wda import (
    WdaBusyError,
    WdaClient,
    WdaUnavailableError,
    PixelRect,
    parse_wda_elements,
    probe_wda,
    wda_overlay_rects,
    wda_url_candidates,
)
from lunatap.drivers.ios.wda_health import classify_wda_log, resign_command
from lunatap.drivers.ios.xcode_driver import XcodeSimulatorDriver
from third_party.mobile_use.controllers.types import ElementQuery
from third_party.mobile_use.utils.logger import get_logger
from third_party.mobile_use.utils.video import get_active_session, remove_active_session

logger = get_logger(__name__)

DEVICECTL_OP_TIMEOUT = 30.0
DEVICECTL_LAUNCH_TIMEOUT = 60.0
DEVICECTL_INSTALL_TIMEOUT = 300.0
WDA_START_TIMEOUT = 45.0
#: Swipes keep this far (points, capped at 8%/5% of the screen height) from the
#: screen edges: iOS treats a touch starting at the bottom edge as the go-home
#: gesture and one at the top edge as Notification/Control Center, and neither
#: is ever a content scroll.
SWIPE_EDGE_MARGIN_TOP_POINTS = 60.0
SWIPE_EDGE_MARGIN_BOTTOM_POINTS = 40.0
#: Extra clearance (points) kept from a floating bar when moving a swipe off it.
SWIPE_OVERLAY_CLEARANCE_POINTS = 12.0
#: xcodebuild says this when it cannot launch the runner on a locked device.
_DEVICE_LOCKED_MARKERS = ("because the device is locked", "the device is locked")
WDA_RUNNER_PATTERN = re.compile(r"webdriveragent", re.IGNORECASE)
WDA_BUNDLE_ENV = "ARTEMIS_IOS_WDA_BUNDLE_ID"
WDA_XCTESTRUN_ENV = "ARTEMIS_IOS_WDA_XCTESTRUN"
# Where scripts/setup_ios_env.sh builds WebDriverAgent for physical devices.
DEFAULT_DEVICE_XCTESTRUN_DIR = Path("~/.artemis/wda/device").expanduser()

_WDA_SETUP_HINT = (
    "Physical iOS UI automation needs WebDriverAgent on the device. Build it once with "
    "'xcodebuild build-for-testing -project <WebDriverAgent>/WebDriverAgent.xcodeproj "
    "-scheme WebDriverAgentRunner -destination id=<UDID> -allowProvisioningUpdates "
    "DEVELOPMENT_TEAM=<team>', install the produced WebDriverAgentRunner-Runner.app via "
    "'xcrun devicectl device install app', point ARTEMIS_IOS_WDA_XCTESTRUN at the "
    "generated .xctestrun for Artemis to host it, or expose a running server through "
    "ARTEMIS_IOS_WDA_URL (iproxy, pymobiledevice3, or the device LAN address)."
)


#: One WDA start at a time per device: two tool calls arriving together must
#: not each spawn an xcodebuild ("** BUILD INTERRUPTED **").
_WDA_START_LOCKS: dict[str, asyncio.Lock] = {}


def _wda_start_lock(device_id: str) -> asyncio.Lock:
    return _WDA_START_LOCKS.setdefault(device_id, asyncio.Lock())


class WdaLaunchError(RuntimeError):
    """WebDriverAgent could not be started on the device.

    ``code`` names the cause (``wda_profile_expired``, ``wda_cert_untrusted``,
    ``device_locked``, ``ui_automation_disabled``, ``wda_launch_failed``, ...)
    and ``needs_human`` says whether someone at the phone has to act, so a
    calling agent can branch instead of guessing.
    """

    def __init__(self, message: str, *, code: str = "wda_launch_failed", needs_human: bool = False):
        super().__init__(message)
        self.code = code
        self.needs_human = needs_human


def default_device_xctestrun() -> Path | None:
    """Newest physical-device ``.xctestrun`` produced by the setup script, if any."""
    if not DEFAULT_DEVICE_XCTESTRUN_DIR.is_dir():
        return None
    candidates = [
        path
        for path in DEFAULT_DEVICE_XCTESTRUN_DIR.rglob("*.xctestrun")
        if "iphoneos" in path.name.lower()
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime)


class PhysicalIosDriver(XcodeSimulatorDriver):
    """One paired physical iOS device driven by devicectl and WebDriverAgent.

    Shares lifecycle conventions with ``XcodeSimulatorDriver`` but replaces the
    interaction core entirely: CoreDevice handles discovery and app lifecycle,
    WDA supplies hierarchy, screenshots, taps, swipes, text, and keys. The
    simulator-only Xcode MCP bridge is never started for a physical target.
    """

    MIN_XCODE_MAJOR = WDA_MIN_XCODE
    #: Seconds to wait for a freshly started WDA server to answer ``/status``.
    WDA_START_TIMEOUT_SECONDS: float = WDA_START_TIMEOUT

    def __init__(
        self,
        device_id: str,
        width: int = 0,
        height: int = 0,
        *,
        workspace_path: str | Path | None = None,
    ):
        super().__init__(
            device_id=device_id, width=width, height=height, workspace_path=workspace_path
        )
        self._launched_pids: dict[str, int] = {}
        self._wda: WdaClient | None = None
        self._wda_runner_pid: int | None = None
        self._wda_test_process: asyncio.subprocess.Process | None = None
        self._wda_log_path: Path | None = None
        #: Floating bars from the latest capture, in screenshot pixels.
        self._overlay_rects: list[PixelRect] = []
        #: WDA window size (points) of the latest capture; a cheap rotation check.
        self._window: tuple[float, float] | None = None

    # --- Resolution and connection ---

    async def _resolve_device(self) -> dict[str, Any]:
        self._validate_workspace()
        if not self._device_id.strip() or self._device_id.strip().lower() == BOOTED_SIMULATOR_ID:
            raise ValueError(
                "Physical iOS devices require --device-serial <device UDID>; "
                "'booted' only selects simulators. Find UDIDs via 'xcrun devicectl list devices'."
            )
        await self._require_ios_host()
        devices = await list_core_devices(force_refresh=True)
        if devices is None:
            raise RuntimeError(
                "Could not enumerate physical devices; 'xcrun devicectl list devices' failed."
            )
        matches = [
            device
            for device in devices
            if is_physical_ios(device) and device_matches_identifier(device, self._device_id)
        ]
        if not matches:
            needle = self._device_id.lower()
            simulator = [
                device
                for device in devices
                if device.get("udid", "").lower() == needle and device.get("reality") == "simulated"
            ]
            if simulator:
                raise ValueError(
                    f"UDID {self._device_id} is an iOS Simulator, not a physical device."
                )
            raise ValueError(
                f"No paired physical iOS device matches {self._device_id!r}. "
                "Attach it, trust this Mac, and verify 'xcrun devicectl list devices'."
            )
        if len(matches) > 1:
            udids = sorted(device.get("udid", "?") for device in matches)
            raise ValueError(
                f"{len(matches)} physical iOS devices match {self._device_id!r} "
                f"({', '.join(udids)}); target the device UDID instead."
            )
        candidate = matches[0]
        self._device_id = candidate["udid"]
        return candidate

    async def _prepare_device(self, candidate: dict[str, Any]) -> None:
        """Verify the paired device is reachable instead of booting it."""
        if candidate.get("pairing_state") != "paired":
            raise RuntimeError(
                f"iOS device {self._device_id} is not paired. Connect it and tap Trust."
            )
        connection_state = candidate.get("connection_state")
        # Older iOS versions over USB expose no CoreDevice tunnelState — an
        # absent value on a paired device is acceptable. A paired device that
        # is merely "available" has no tunnel yet: any devicectl request to it
        # opens one, so ask before giving up.
        if connection_state is not None and connection_state != "connected":
            connection_state = await self._open_tunnel()
        if connection_state is not None and connection_state != "connected":
            raise RuntimeError(
                f"iOS device {self._device_id} ({candidate.get('name') or 'unknown'}) is not "
                "connected. Attach it over USB or ensure network pairing is reachable; "
                "on iOS 16+ also enable Developer Mode in Settings > Privacy & Security."
            )

    async def _open_tunnel(self) -> str | None:
        """Ask CoreDevice for the device details, which brings its tunnel up."""
        try:
            details = await self._devicectl_json("info", "details")
        except (OSError, RuntimeError, TimeoutError, ValueError) as exc:
            logger.debug(f"Could not open a CoreDevice tunnel to {self._device_id}: {exc}")
            return "disconnected"
        state = (details.get("connectionProperties") or {}).get("tunnelState")
        if state == "connected":
            logger.info(f"Opened a CoreDevice tunnel to {self._device_id}")
        return state if isinstance(state, str) else "disconnected"

    async def connect(self) -> None:
        async with self._connect_lock:
            if self._session_key:
                return
            candidate = await self._resolve_device()
            await self._prepare_device(candidate)
            connected = False
            try:
                self._wda = await self._ensure_wda()
                # The reachable WDA endpoint must belong to THIS device before
                # we open a session or send input. WDA reports the product
                # family name ("iPhone"), not the personalized devicectl name
                # ("Dana's iPhone"), and uuid is identifierForVendor — so a
                # *specific* conflicting name is the wrong-device signal;
                # generic family names are accepted.
                await self._verify_wda_device(candidate)
                # A session auto-created by a runner WE launched is ours to
                # adopt; an endpoint discovered via env/probe keeps the
                # foreign-session refusal.
                runner_owned = (
                    self._wda_runner_pid is not None or self._wda_test_process is not None
                )
                self._session_key = await self._wda.open_session(adopt_existing=runner_owned)
                try:
                    await self.get_screen_data(skip_settling=True)
                except WdaBusyError as busy:
                    # WDA and the session are fine; one app's screen hangs.
                    # Tearing WDA down here restarts it on every call and
                    # never helps, so stay connected: open_app and HOME still
                    # work and are the way out.
                    logger.warning(f"Connected, but the first screen read failed: {busy}")
                connected = True
            finally:
                # Any incomplete setup must release a half-started WDA
                # runner, xcodebuild session, or server-side session.
                if not connected:
                    try:
                        await self.disconnect()
                    except (OSError, ValueError, RuntimeError, TimeoutError) as cleanup_error:
                        logger.warning(
                            "Could not release the WDA session after a connection failure: "
                            f"{cleanup_error}",
                        )

    async def disconnect(self) -> None:
        try:
            if self._recorder is not None:
                session = self._recorder.session
                if session is not None:
                    # A failed (non-active) session still owns frames and
                    # conversions — finalize it too so nothing is lost.
                    try:
                        await self._recorder.stop()
                    except (OSError, ValueError, RuntimeError, TimeoutError) as exc:
                        logger.error(
                            f"Physical iOS recording finalization failed during disconnect: {exc}"
                        )
                    if get_active_session(self._device_id) is session:
                        remove_active_session(self._device_id)
        finally:
            async with self._operation_lock:
                self._session_key = None
                self._scale = None
                self._window = None
                self._launched_pids.clear()
                client, self._wda = self._wda, None
                runner_pid, self._wda_runner_pid = self._wda_runner_pid, None
                test_process, self._wda_test_process = self._wda_test_process, None
            # Each cleanup step is isolated so a failure in one never skips
            # the owned WDA session, the xcodebuild child, or the runner pid.
            if client is not None:
                try:
                    await client.close_session()
                except (OSError, ValueError, RuntimeError, TimeoutError) as exc:
                    logger.debug(f"WDA session close failed during disconnect: {exc}")
            if test_process is not None and test_process.returncode is None:
                try:
                    test_process.terminate()
                    await asyncio.wait_for(test_process.wait(), timeout=10.0)
                except (TimeoutError, OSError):
                    # Graceful terminate failed — reap (kill + drain) the
                    # xcodebuild child so it cannot outlive the driver.
                    try:
                        await reap_process(test_process)
                    except (OSError, RuntimeError, TimeoutError) as exc:
                        logger.debug(f"WDA xcodebuild reap failed: {exc}")
            if runner_pid is not None:
                try:
                    await self._terminate_pid(runner_pid)
                except (OSError, RuntimeError, TimeoutError, ValueError) as exc:
                    logger.debug(f"WDA runner termination failed: {exc}")

    async def _verify_wda_device(self, candidate: dict[str, Any]) -> None:
        """Refuse a reachable WDA endpoint that does not belong to this device.

        WDA reports the product family name ("iPhone"), not the personalized
        devicectl name ("Dana's iPhone"), and uuid is identifierForVendor — so
        a *specific* conflicting name is the wrong-device signal; generic
        family names are accepted.
        """
        assert self._wda is not None
        info = await self._wda.device_info()
        expected_name = (candidate.get("name") or "").strip()
        wda_name = str(info.get("name") or "").strip()
        generic_names = {"iphone", "ipad", "ipod touch", "apple watch", "apple tv"}
        name_conflict = (
            expected_name
            and wda_name
            and wda_name.lower() not in generic_names
            and wda_name != expected_name
        )
        if info.get("isSimulator") is not False or name_conflict:
            if info.get("isSimulator") is True:
                remedy = (
                    "A simulator's WebDriverAgent owns that port on this Mac. Release the "
                    "simulator first (mobile_release_device('<simulator UDID>') for a harness "
                    "session, or stop its WebDriverAgentRunner), then retry."
                )
            else:
                remedy = "Point ARTEMIS_IOS_WDA_URL at a WDA server running on the selected device."
            raise RuntimeError(
                f"The WebDriverAgent at {self._wda.base_url} does not report "
                f"the selected physical device {self._device_id} "
                f"(expected name {expected_name!r}, got "
                f"{info.get('name')!r}, isSimulator={info.get('isSimulator')!r}). {remedy}"
            )

    def _require_connected(self) -> None:
        if not self._session_key or self._device_id.strip().lower() == BOOTED_SIMULATOR_ID:
            raise RuntimeError("Connect the physical iOS driver before interacting.")

    def _require_wda(self) -> WdaClient:
        """Bound WDA client for input paths; fails clearly when detached."""
        self._require_connected()
        client = self._wda
        if client is None:
            raise RuntimeError("Connect the physical iOS driver before interacting.")
        return client

    async def _ensure_wda(self) -> WdaClient:
        """Attach to a reachable WDA server, starting one when possible.

        Order: probe known endpoints, then try launching an installed runner
        app directly (newer WDA builds self-host), then — when
        ``ARTEMIS_IOS_WDA_XCTESTRUN`` names a ``.xctestrun`` file from a
        ``build-for-testing`` — spawn ``xcodebuild test-without-building``,
        which is the canonical way to boot the XCTest session that hosts
        WDA's HTTP server.
        """
        candidates = await self._wda_candidates()
        client = await probe_wda(candidates)
        if client is not None:
            return client
        async with _wda_start_lock(self._device_id):
            # Another caller may have started WDA while we waited for the lock.
            client = await probe_wda(candidates)
            if client is not None:
                return client
            return await self._start_and_wait_for_wda(candidates)

    async def _start_and_wait_for_wda(self, candidates: list[str]) -> WdaClient:
        await self._start_wda_server()
        deadline = asyncio.get_running_loop().time() + self.WDA_START_TIMEOUT_SECONDS
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise self._launch_error(
                    f"WebDriverAgent start timed out after {self.WDA_START_TIMEOUT_SECONDS:.0f}s "
                    f"on {self._device_id} (probed {candidates})",
                    timed_out=True,
                )
            client = await probe_wda(candidates, timeout=min(5.0, remaining))
            if client is not None:
                logger.info(f"WebDriverAgent attached at {client.base_url} for {self._device_id}")
                return client
            if self._wda_log_mentions(_DEVICE_LOCKED_MARKERS):
                raise WdaLaunchError(
                    f"iOS device {self._device_id} is locked, so Xcode cannot launch "
                    "WebDriverAgent on it. Unlock the device (and keep it unlocked, e.g. "
                    "Settings > Display & Brightness > Auto-Lock > Never while testing), "
                    f"then retry. [cause: device_locked, needs a person at the phone] "
                    f"Log: {self._wda_log_path}",
                    code="device_locked",
                    needs_human=True,
                )
            if self._wda_test_process is not None and self._wda_test_process.returncode is not None:
                raise self._launch_error(
                    f"xcodebuild hosting WebDriverAgent exited with code "
                    f"{self._wda_test_process.returncode} before its server answered"
                )
            await asyncio.sleep(min(1.0, remaining))

    def _launch_error(self, what: str, *, timed_out: bool = False) -> WdaLaunchError:
        """``what`` plus the first real reason from the xcodebuild log, if any."""
        failure = classify_wda_log(self._wda_log_text(), resign=resign_command())
        if failure is None and timed_out:
            # The build is fine (it ran); the server just never answered.
            return WdaLaunchError(
                f"{what}; xcodebuild log: {self._wda_log_path}. If the device is showing a "
                "passcode prompt to enable UI Automation, enter it on the device first; "
                "otherwise retry once, and read the log if it happens again.",
                code="wda_start_timeout",
            )
        if failure is None:
            return WdaLaunchError(f"{what} (log: {self._wda_log_path}). {self._wda_timeout_hint()}")
        return WdaLaunchError(
            f"{what}: {failure.describe()} Log: {self._wda_log_path}",
            code=failure.code,
            needs_human=failure.needs_human,
        )

    def _wda_log_text(self) -> str:
        if self._wda_log_path is None:
            return ""
        try:
            return self._wda_log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    def _wda_log_mentions(self, markers: tuple[str, ...]) -> bool:
        """Whether the xcodebuild log hosting WDA contains any of ``markers``."""
        if self._wda_log_path is None:
            return False
        try:
            text = self._wda_log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return False
        return any(marker in text for marker in markers)

    async def _wda_candidates(self) -> list[str]:
        """Ordered WDA endpoints to probe for this device."""
        return wda_url_candidates(tunnel_ip=await self._tunnel_ip())

    def _wda_timeout_hint(self) -> str:
        return (
            "If the device is showing a passcode prompt to enable UI Automation, "
            f"enter it on the device first. {_WDA_SETUP_HINT}"
        )

    async def _start_wda_server(self) -> None:
        """Start a WDA server on the device when none answers yet.

        Prefers an XCTest session from ``ARTEMIS_IOS_WDA_XCTESTRUN`` or, failing
        that, the newest device build under ``~/.artemis/wda/device``: a runner
        app launched on its own exits at once unless it self-hosts.
        """
        found = default_device_xctestrun()
        xctestrun = os.environ.get(WDA_XCTESTRUN_ENV) or (str(found) if found else None)
        runner = await self._wda_runner_bundle()
        if xctestrun:
            await self._start_xctest_session(xctestrun)
        elif runner is not None:
            try:
                self._wda_runner_pid = await self._launch_bundle(runner, terminate_existing=False)
                logger.info(
                    f"Launched WebDriverAgent runner {runner} on {self._device_id}; "
                    "waiting for its HTTP server"
                )
            except (OSError, RuntimeError, TimeoutError) as exc:
                # A stale runner may already be hosting the server — the
                # probe loop below still gets a chance to attach.
                logger.debug(f"WDA runner launch failed ({runner}): {exc}")
        if runner is None and not xctestrun:
            raise RuntimeError(
                f"No WebDriverAgent server answers on {self._device_id} and no WDA "
                f"runner is installed. {_WDA_SETUP_HINT}"
            )

    async def _start_xctest_session(self, xctestrun: str) -> None:
        """Hold WDA alive through ``xcodebuild test-without-building``."""
        path = Path(xctestrun).expanduser()
        if path.suffix != ".xctestrun" or not path.is_file():
            raise RuntimeError(
                f"{WDA_XCTESTRUN_ENV} must point at an existing .xctestrun file "
                "produced by 'xcodebuild build-for-testing'."
            )
        if self._wda_test_process is not None and self._wda_test_process.returncode is None:
            return
        # xcodebuild output goes to a per-device log so a failed WDA start
        # (signing, missing runtime, port clash) is diagnosable afterwards.
        log_path = Path(tempfile.gettempdir()) / f"artemis-wda-{self._device_id}.log"
        with log_path.open("wb") as log_file:
            self._wda_test_process = await asyncio.create_subprocess_exec(
                "xcodebuild",
                "test-without-building",
                "-xctestrun",
                str(path),
                "-destination",
                f"id={self._device_id}",
                stdin=asyncio.subprocess.DEVNULL,
                stdout=log_file,
                stderr=asyncio.subprocess.STDOUT,
                env={**os.environ, **self._xctest_env()},
            )
        self._wda_log_path = log_path
        logger.info(
            f"Starting WebDriverAgent via xcodebuild for {self._device_id} (log: {log_path})"
        )

    def _xctest_env(self) -> dict[str, str]:
        """Extra environment for the xcodebuild process hosting WDA.

        Variables prefixed ``TEST_RUNNER_`` are forwarded (prefix stripped)
        into the XCTest runner, e.g. ``TEST_RUNNER_USE_PORT`` sets WDA's port.
        """
        return {}

    async def _tunnel_ip(self) -> str | None:
        """CoreDevice's managed tunnel address for this device, when present."""
        try:
            details = await self._devicectl_json("info", "details")
        except (OSError, RuntimeError, TimeoutError, ValueError):
            return None
        # The tunnel address has moved across Xcode releases: probe every
        # observed shape, canonical first.
        paths = (
            ((details.get("connectionProperties") or {}), "tunnelIPAddress"),
            (
                ((details.get("properties") or {}).get("connection") or {}),
                "tunnelIPAddressString",
            ),
            ((details.get("tunnel") or {}), "ipAddress"),
        )
        for section, key in paths:
            address = section.get(key)
            if isinstance(address, str) and ":" in address:
                return address
        return None

    async def _wda_runner_bundle(self) -> str | None:
        override = os.environ.get(WDA_BUNDLE_ENV)
        try:
            apps = await self._devicectl_json("info", "apps")
        except (OSError, RuntimeError, TimeoutError, ValueError) as exc:
            logger.debug(f"Could not list apps while looking for a WDA runner: {exc}")
            return override or None
        bundles = {
            app.get("bundleIdentifier") or ""
            for app in apps.get("apps", [])
            if isinstance(app, dict)
        }
        if override:
            return override if override in bundles else None
        matches = sorted(b for b in bundles if WDA_RUNNER_PATTERN.search(b))
        return matches[0] if matches else None

    async def _launch_bundle(self, bundle: str, terminate_existing: bool = True) -> int | None:
        arguments = [
            "devicectl",
            "device",
            "process",
            "launch",
            "--device",
            self._device_id,
        ]
        if terminate_existing:
            arguments.append("--terminate-existing")
        arguments += ["--json-output", "-", bundle]
        raw = await run_xcrun(*arguments, timeout=DEVICECTL_LAUNCH_TIMEOUT)
        return self._launched_pid(raw)

    async def _devicectl_json(self, *arguments: str) -> dict[str, Any]:
        """Run a ``devicectl`` info subcommand and return its ``result`` JSON.

        ``--json-output -`` still writes a human table to stdout for ``info``
        subcommands, so the JSON must go to a scratch file.
        """
        with tempfile.TemporaryDirectory(prefix="artemis-devicectl-") as tmp:
            target = Path(tmp) / "out.json"
            await run_xcrun(
                "devicectl",
                "device",
                *arguments,
                "--device",
                self._device_id,
                "--json-output",
                str(target),
                timeout=DEVICECTL_OP_TIMEOUT,
            )
            payload = json.loads(target.read_text(encoding="utf-8"))
        result = payload.get("result")
        return result if isinstance(result, dict) else payload

    # --- Observation ---

    async def _screenshot_png(self) -> bytes:
        """WDA screenshot first (same framebuffer as the hierarchy); devicectl fallback."""
        wda_error: str = "WebDriverAgent is not attached"
        if self._wda is not None:
            try:
                return await self._wda.screenshot_png()
            except WdaBusyError:
                raise
            except (RuntimeError, WdaUnavailableError, OSError) as exc:
                wda_error = f"WebDriverAgent screenshot failed: {exc}"
                logger.debug(f"{wda_error}; falling back to devicectl")
        with tempfile.TemporaryDirectory(prefix="artemis-shot-") as tmp:
            target = Path(tmp) / "shot.png"
            try:
                await devicectl_screenshot(self._device_id, target, timeout=DEVICECTL_OP_TIMEOUT)
            except (RuntimeError, OSError, TimeoutError) as fallback_error:
                # Xcode 26 removed 'devicectl device capture': surface the WDA
                # failure that matters, not the fallback's usage text.
                detail = (str(fallback_error).splitlines() or [repr(fallback_error)])[0]
                raise RuntimeError(
                    f"Could not capture the iOS screen. {wda_error}; the devicectl "
                    f"fallback is unavailable ({detail})."
                ) from fallback_error
            data = target.read_bytes()
        if not data:
            raise RuntimeError("devicectl produced an empty screenshot.")
        return data

    async def _capture(self) -> ScreenData:
        if self._wda is None:
            raise RuntimeError("Connect the physical iOS driver before observing.")
        # Tree first, pixels second. WDA's snapshot waits for the app to go
        # idle, so a screenshot taken before it can show a list mid-fade while
        # the tree has the settled rows; OCR then reads "ghost" rows off the
        # stale frame (QA bug 24). Taken after, the pixels are never older.
        tree = await self._wda.source_json()
        screenshot = await self._screenshot_png()
        with Image.open(BytesIO(screenshot)) as image:
            self._width, self._height = image.size
        win_w, win_h = await self._wda.window_size()
        if win_w <= 0 or win_h <= 0:
            raise RuntimeError("WebDriverAgent reported an unusable window size.")
        if abs(self._width / win_w - self._height / win_h) > 0.05:
            self._scale = None
            raise RuntimeError(
                "The device screenshot and hierarchy disagree on orientation. "
                "Capture again after the rotation settles."
            )
        scale = (self._width / win_w, self._height / win_h)
        if not (0.9 <= scale[0] <= 4.5 and 0.9 <= scale[1] <= 4.5):
            # iOS displays render at 1x-3x; a wildly off scale means the WDA
            # window is not full-screen (e.g. iPad multitasking) and every
            # element bound would be wrong.
            self._scale = None
            raise RuntimeError(
                f"WebDriverAgent window {win_w}x{win_h} does not match the "
                f"{self._width}x{self._height} framebuffer (scale {scale}); "
                "bring the session app full-screen before interacting."
            )
        self._scale = scale
        self._window = (win_w, win_h)
        elements = parse_wda_elements(tree, self._scale, self._width, self._height)
        self._overlay_rects = wda_overlay_rects(tree, self._scale)
        return ScreenData(
            screenshot_bytes=screenshot,
            screenshot_base64=base64.b64encode(screenshot).decode("ascii"),
            ui_elements=elements,
            width=self._width,
            height=self._height,
            platform="ios",
        )

    # --- Input ---

    async def tap(
        self, x: int, y: int, duration_ms: int = 100, times: int = 1, delay_ms: int = 100
    ) -> bool:
        if times < 1 or duration_ms < 0 or delay_ms < 0:
            raise ValueError("Tap count must be positive and durations nonnegative.")
        async with self._operation_lock:
            wda = self._require_wda()
            await self._geometry_unchanged(wda)
            point = self._scaled_point(x, y)
            for index in range(times):
                await wda.tap(*point, hold_ms=duration_ms)
                if index < times - 1:
                    await asyncio.sleep(delay_ms / 1000)
            return True

    async def _geometry_unchanged(self, wda: WdaClient) -> None:
        """Refuse a tap after a rotation without reading the whole tree again.

        A tap only needs the screen geometry the caller's coordinates were
        taken in. The window size answers that in one cheap request; a full
        capture (screenshot plus hierarchy, the slow part on a phone) runs
        only when there is no earlier capture or the size moved.
        """
        window = self._window
        if self._scale is None or window is None or await wda.window_size() != window:
            await self._capture_unchanged("tapping")

    async def hierarchy_elements(self) -> list[dict[str, Any]]:
        """The current tree as elements, without a screenshot: a cheap second look.

        Uses the scale of the latest capture; empty before the first one.
        """
        async with self._operation_lock:
            wda = self._require_wda()
            if self._scale is None:
                return []
            tree = await wda.source_json()
            return parse_wda_elements(tree, self._scale, self._width, self._height)

    async def screenshot_only(self) -> bytes:
        """The current screen as PNG, without the hierarchy read."""
        async with self._operation_lock:
            self._require_wda()
            return await self._screenshot_png()

    async def swipe(
        self, start_x: int, start_y: int, end_x: int, end_y: int, duration_ms: int = 800
    ) -> bool:
        if duration_ms <= 0:
            raise ValueError("Swipe duration must be positive.")
        async with self._operation_lock:
            wda = self._require_wda()
            await self._capture_unchanged("swiping")
            start = self._scaled_point(start_x, self._safe_swipe_y(start_x, start_y))
            end = self._scaled_point(end_x, self._safe_swipe_y(end_x, end_y))
            await wda.swipe(*start, *end, duration_ms)
            return True

    def _safe_swipe_y(self, x: int, y: int) -> int:
        """Move a swipe endpoint off the system edge zones and floating bars.

        A content swipe that starts on the bottom edge (go-home gesture), the
        top edge (Notification Center), or a floating search/tab bar never
        scrolls the content; it is pulled into the clear content area instead.
        """
        if self._scale is None:
            return y
        scale_y = self._scale[1]
        low = round(min(SWIPE_EDGE_MARGIN_TOP_POINTS * scale_y, self._height * 0.08))
        high = (
            self._height
            - 1
            - round(min(SWIPE_EDGE_MARGIN_BOTTOM_POINTS * scale_y, self._height * 0.05))
        )
        clearance = round(SWIPE_OVERLAY_CLEARANCE_POINTS * scale_y)
        middle = self._height // 2
        for o_left, o_top, o_right, o_bottom in self._overlay_rects:
            if not o_left <= x < o_right:
                continue
            if o_top >= middle:  # a bottom bar caps the usable area
                high = min(high, o_top - clearance)
            elif o_bottom <= middle:  # a top bar raises it
                low = max(low, o_bottom + clearance)
        if low >= high:
            return y
        return max(low, min(high, y))

    async def swipe_direction(
        self,
        direction: SwipeDirection | Literal["up", "down", "left", "right"],
        duration_ms: int = 800,
    ) -> bool:
        direction = SwipeDirection(direction).value
        if duration_ms <= 0:
            raise ValueError("Swipe duration must be positive.")
        async with self._operation_lock:
            # Rotation-tolerant like the simulator path: recapture and compute
            # from the *current* size rather than refusing like tap.
            wda = self._require_wda()
            await self._capture()
            sx, sy, ex, ey = self._direction_points(direction, *self.screen_size)
            start = self._scaled_point(sx, self._safe_swipe_y(sx, sy))
            end = self._scaled_point(ex, self._safe_swipe_y(ex, ey))
            await wda.swipe(*start, *end, duration_ms)
            return True

    async def input_text(self, text: str, clear_existing: bool = True) -> bool:
        if clear_existing:
            raise NotImplementedError(
                "Physical iOS typing appends to the focused field; clear it through the "
                "UI or pass clear_existing=False. This matches the simulator behavior."
            )
        async with self._operation_lock:
            wda = self._require_wda()
            await wda.type_text(text)
        return True

    async def press_key(self, key: KeyCode | str | int) -> bool:
        key = key.value if isinstance(key, KeyCode) else str(key).lower()
        async with self._operation_lock:
            wda = self._require_wda()
            if key in ("home", "app_switch"):
                presses = 2 if key == "app_switch" else 1
                for index in range(presses):
                    if not await wda.press_button("home"):
                        if key == "app_switch":
                            # A backend that cannot press Home cannot
                            # double-press for app switching — never report
                            # success from a homescreen fallback.
                            raise NotImplementedError(
                                "App switching is unavailable from this WebDriverAgent backend."
                            )
                        await wda.homescreen()
                    if index < presses - 1:
                        await asyncio.sleep(0.4)
            elif key == "enter":
                await wda.type_text("\n")
            else:
                buttons = {
                    "volume_up": "volumeUp",
                    "volume_down": "volumeDown",
                    "power": "power",
                }
                if key not in buttons or not await wda.press_button(buttons[key]):
                    raise NotImplementedError(
                        f"Key {key!r} is not supported by the physical iOS driver."
                    )
        return True

    async def tap_element(
        self, query: ElementQuery, long_press: bool = False, duration_ms: int = 1000
    ) -> bool:
        async with self._operation_lock:
            wda = self._require_wda()
            data = await self._capture()
            element, center, error = await self.find_element(query, data)
            if error or element is None or center is None:
                return False
            point = self._scaled_point(*center)
            await wda.tap(*point, hold_ms=duration_ms if long_press else 0)
            return True

    # --- App lifecycle via devicectl ---

    async def launch_app(self, package_name: str) -> bool:
        async with self._operation_lock:
            self._require_connected()
            pid = await self._launch_bundle(package_name)
            if pid is not None:
                self._launched_pids[package_name] = pid
        return True

    @staticmethod
    def _launched_pid(raw: bytes) -> int | None:
        """Best-effort process id from devicectl's launch JSON or text output."""
        try:
            payload = json.loads(raw)
        except (ValueError, TypeError):
            payload = None
        if isinstance(payload, dict):
            result = payload.get("result")
            result = result if isinstance(result, dict) else {}
            process = result.get("process")
            candidates = [c for c in (process, result, payload) if isinstance(c, dict)]
            for candidate in candidates:
                pid = candidate.get("processIdentifier") or candidate.get("pid")
                if isinstance(pid, int):
                    return pid
                if isinstance(pid, str) and pid.isdigit():
                    return int(pid)
        match = re.search(r"pid[:= ]+(\d+)", raw.decode(errors="replace"))
        return int(match.group(1)) if match else None

    @staticmethod
    def _normalize_executable(raw: Any) -> str:
        """devicectl reports executables as file:// URLs or paths; normalize."""
        value = raw if isinstance(raw, str) else str(raw or "")
        if isinstance(raw, dict):
            value = str(raw.get("url") or raw.get("path") or "")
        value = value.removeprefix("file://")
        return value.removeprefix("/private") or value

    async def _resolve_pid(self, package_name: str) -> int:
        """Verified live pid: the executable must sit under the app's own URL.

        A cached launch pid only prioritizes among processes already proven to
        belong to the app — a recycled pid must never select a foreign process.
        """
        # ``--include-default-apps`` keeps system apps (Safari, Settings)
        # resolvable; the default view lists developer-installed apps only.
        apps = await self._devicectl_json("info", "apps", "--include-default-apps")
        url_prefix = ""
        for app in apps.get("apps", []):
            if isinstance(app, dict) and app.get("bundleIdentifier") == package_name:
                url_prefix = self._normalize_executable(app.get("url")).rstrip("/")
                break
        if not url_prefix:
            raise ValueError(
                f"{package_name!r} is not installed on {self._device_id} or its app URL "
                "could not be determined; cannot verify a process to terminate."
            )
        processes = await self._devicectl_json("info", "processes")
        candidates: list[int] = []
        for process in processes.get("runningProcesses", []):
            if not isinstance(process, dict):
                continue
            executable = self._normalize_executable(process.get("executable"))
            if executable != url_prefix and not executable.startswith(url_prefix + "/"):
                continue
            pid = process.get("processIdentifier") or process.get("pid") or process.get("processID")
            if isinstance(pid, int):
                candidates.append(pid)
        if not candidates:
            raise ValueError(
                f"No running process found for {package_name!r} on {self._device_id}; "
                "launch it with launch_app before stopping."
            )
        tracked = self._launched_pids.get(package_name)
        if tracked is not None and tracked in candidates:
            return tracked
        return candidates[0]

    async def _terminate_pid(self, pid: int, kill: bool = False) -> None:
        arguments = [
            "devicectl",
            "device",
            "process",
            "terminate",
            "--device",
            self._device_id,
            "--pid",
            str(pid),
        ]
        if kill:
            arguments.append("--kill")
        await run_xcrun(*arguments, timeout=DEVICECTL_OP_TIMEOUT)

    async def stop_app(self, package_name: str) -> bool:
        async with self._operation_lock:
            self._require_connected()
            # Verify the live pid first — a recycled cached pid could belong to
            # a different app now, and terminating it would kill the wrong app.
            pid = await self._resolve_pid(package_name)
            await self._terminate_pid(pid, kill=True)
            self._launched_pids.pop(package_name, None)
        return True

    async def install_app(self, app_path: Path) -> str:
        self._require_connected()
        path = app_path.expanduser().resolve()
        if path.suffix.lower() == ".ipa":
            if not path.is_file():
                raise ValueError("iOS installation requires an existing .ipa file.")
            bundle = self._ipa_bundle_id(path) or path.stem
        elif path.suffix == ".app" and path.is_dir():
            with (path / "Info.plist").open("rb") as stream:
                bundle = plistlib.load(stream).get("CFBundleIdentifier")
            if not isinstance(bundle, str) or not bundle:
                raise ValueError("The .app has no CFBundleIdentifier in Info.plist.")
        else:
            raise ValueError(
                "Physical iOS installation requires a signed .app directory or .ipa built "
                "for a device (arm64) with a valid provisioning profile."
            )
        async with self._operation_lock:
            self._require_connected()
            await run_xcrun(
                "devicectl",
                "device",
                "install",
                "app",
                "--device",
                self._device_id,
                str(path),
                timeout=DEVICECTL_INSTALL_TIMEOUT,
            )
        return bundle

    @staticmethod
    def _ipa_bundle_id(path: Path) -> str | None:
        """Read CFBundleIdentifier from an IPA's embedded app Info.plist."""
        import zipfile

        try:
            with zipfile.ZipFile(path) as archive:
                for name in archive.namelist():
                    if name.startswith("Payload/") and name.endswith(".app/Info.plist"):
                        with archive.open(name) as stream:
                            bundle = plistlib.load(stream).get("CFBundleIdentifier")
                        return bundle if isinstance(bundle, str) and bundle else None
        except (OSError, zipfile.BadZipFile, KeyError, plistlib.InvalidFileException):
            return None
        return None

    async def list_apps(self) -> dict[str, str]:
        async with self._operation_lock:
            self._require_connected()
            # ``info apps`` defaults to developer-installed apps only, which
            # hides Safari and friends; include the system defaults so app
            # resolution can find and launch them.
            apps = await self._devicectl_json("info", "apps", "--include-default-apps")
        result = {}
        for app in apps.get("apps", []):
            bundle = app.get("bundleIdentifier") or app.get("bundleID")
            if not bundle:
                continue
            result[bundle] = app.get("name") or bundle
        return result

    async def open_url(self, url: str) -> bool:
        async with self._operation_lock:
            self._require_connected()
            await run_xcrun(
                "devicectl",
                "device",
                "process",
                "openURL",
                "--device",
                self._device_id,
                url,
                timeout=DEVICECTL_OP_TIMEOUT,
            )
        return True

    async def get_current_package(self) -> str | None:
        async with self._operation_lock:
            self._require_connected()
            if self._wda is not None:
                return await self._wda.active_app()
            return None

    async def execute_shell(self, command: str, timeout_seconds: float = 15.0) -> str:
        raise NotImplementedError("Android shell commands are unavailable on iOS devices.")

    # --- Recording (devicectl screenshot polling) ---

    def _new_recorder(self) -> IosScreenRecorder | PhysicalIosRecorder:
        """Physical recorder: WDA screenshot polling + ffconcat encode.

        ``devicectl device capture`` was removed in Xcode 26, so frames come
        from WebDriverAgent — the same framebuffer every observation uses.
        """
        return PhysicalIosRecorder(self._device_id, frame_source=self._recording_frame)

    async def _recording_frame(self) -> bytes:
        client = self._wda
        if client is None:
            raise RuntimeError("WebDriverAgent is not attached; no frame to record.")
        return await client.screenshot_png()

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

"""iOS Simulator support through WebDriverAgent, for Xcode releases before 27.

``XcodeSimulatorDriver`` relies on Xcode 27's ``DeviceInteraction*`` MCP
tools. On older Xcode (15-26) the same simulator can be driven by
WebDriverAgent (WDA) — the XCUITest HTTP bridge Appium uses — which needs no
code signing on a simulator. This driver combines:

- ``simctl`` for simulator resolution, boot, app lifecycle, URLs, and native
  ``recordVideo`` recording (inherited from ``XcodeSimulatorDriver``);
- WDA for screenshots, the accessibility hierarchy, taps, swipes, text, and
  hardware buttons (inherited from ``PhysicalIosDriver``).

A simulator shares the host's network stack, so WDA listens on
``127.0.0.1:<port>`` directly. Artemis attaches to a running server, or starts
one with ``xcodebuild test-without-building`` from a prebuilt ``.xctestrun``
(``scripts/setup_ios_wda_simulator.sh`` produces it).
"""

import asyncio
from dataclasses import dataclass
from pathlib import Path
import os
import re
import tempfile
from typing import Any, Literal
import zlib

from lunatap.drivers.ios.discovery import WDA_MIN_XCODE, run_xcrun
from lunatap.drivers.ios.physical_driver import WDA_XCTESTRUN_ENV, PhysicalIosDriver
from lunatap.drivers.ios.recording import IosScreenRecorder
from lunatap.drivers.ios.wda import (
    WDA_DEFAULT_PORT,
    WDA_URL_ENV,
    WdaUnavailableError,
    normalize_wda_url,
)
from lunatap.drivers.ios.xcode_driver import XcodeSimulatorDriver
from third_party.mobile_use.utils.logger import get_logger

logger = get_logger(__name__)

WDA_PORT_ENV = "ARTEMIS_IOS_WDA_PORT"
#: Default location written by ``scripts/setup_ios_wda_simulator.sh``.
DEFAULT_SIM_XCTESTRUN_DIR = Path("~/.artemis/wda/simulator").expanduser()
SIMCTL_SCREENSHOT_TIMEOUT = 30.0

_SIM_WDA_SETUP_HINT = (
    "Build WebDriverAgent for simulators once with 'bash scripts/setup_ios_wda_simulator.sh' "
    f"(writes a .xctestrun under {DEFAULT_SIM_XCTESTRUN_DIR}), point "
    f"{WDA_XCTESTRUN_ENV} at a WebDriverAgentRunner .xctestrun built for "
    "'generic/platform=iOS Simulator', or start WDA yourself (Appium, Xcode) and set "
    f"ARTEMIS_IOS_WDA_URL / {WDA_PORT_ENV}."
)


#: Ports tried, in order, when the default one belongs to another simulator.
#: Each simulator starts at its own offset (from its UDID), so it comes back
#: to the same port, and to its own running WebDriverAgent, on the next run.
_FALLBACK_PORT_BASE = WDA_DEFAULT_PORT + 1
_FALLBACK_PORT_SPAN = 400
_FALLBACK_PORT_TRIES = 8
_SIM_PROCESS_RE = re.compile(r"/CoreSimulator/Devices/([0-9A-Fa-f-]{36})/")
_PORT_LOOKUP_TIMEOUT = 5.0


@dataclass(frozen=True)
class PortOwner:
    """Who listens on a local TCP port.

    ``simulator`` carries the UDID of the simulator whose process listens
    (WebDriverAgent's runner lives under ``CoreSimulator/Devices/<UDID>/``);
    ``other`` is any other process; ``unknown`` means the lookup failed.
    """

    state: Literal["free", "simulator", "other", "unknown"]
    udid: str | None = None
    command: str = ""


async def _run_quiet(*argv: str) -> tuple[int, str] | None:
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(process.communicate(), _PORT_LOOKUP_TIMEOUT)
    except (TimeoutError, OSError):
        return None
    return process.returncode or 0, out.decode(errors="replace")


async def port_owner(port: int) -> PortOwner:
    """The process listening on ``127.0.0.1:port``, read from the OS, not from WDA.

    Asking WDA itself is not enough: a WDA whose foreground app hangs never
    answers, and two simulators can share a name.
    """
    listed = await _run_quiet("lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t")
    if listed is None:
        return PortOwner("unknown")
    code, out = listed
    pids = [line.strip() for line in out.splitlines() if line.strip().isdigit()]
    if not pids:
        # lsof exits 1 when nothing matches.
        return PortOwner("free") if code in (0, 1) else PortOwner("unknown")
    shown = await _run_quiet("ps", "-o", "command=", "-p", pids[0])
    if shown is None or not shown[1].strip():
        return PortOwner("unknown")
    command = shown[1].strip()
    match = _SIM_PROCESS_RE.search(command)
    if match:
        return PortOwner("simulator", match.group(1).upper(), command)
    return PortOwner("other", None, command)


def fallback_ports(udid: str) -> list[int]:
    """Ports this simulator tries after the default one, stable across runs."""
    offset = zlib.crc32(udid.upper().encode()) % _FALLBACK_PORT_SPAN
    return [
        _FALLBACK_PORT_BASE + (offset + step) % _FALLBACK_PORT_SPAN
        for step in range(_FALLBACK_PORT_TRIES)
    ]


def default_simulator_xctestrun() -> Path | None:
    """Newest simulator ``.xctestrun`` produced by the setup script, if any."""
    if not DEFAULT_SIM_XCTESTRUN_DIR.is_dir():
        return None
    candidates = [
        path
        for path in DEFAULT_SIM_XCTESTRUN_DIR.rglob("*.xctestrun")
        if "iphonesimulator" in path.name.lower()
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime)


class WdaSimulatorDriver(PhysicalIosDriver):
    """One iOS simulator driven by simctl (lifecycle) and WebDriverAgent (UI)."""

    MIN_XCODE_MAJOR = WDA_MIN_XCODE
    #: First launch of a freshly built runner on a cold simulator can be slow.
    WDA_START_TIMEOUT_SECONDS = 120.0

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
        self._simulator_name: str | None = None
        #: The port chosen for this simulator's WDA, once ``_wda_candidates`` ran.
        self._port: int | None = None

    # --- Resolution and connection: simulator semantics from simctl ---

    async def _resolve_device(self) -> dict[str, Any]:
        candidate = await XcodeSimulatorDriver._resolve_device(self)
        self._simulator_name = candidate.get("name")
        return candidate

    async def _prepare_device(self, candidate: dict[str, Any]) -> None:
        await XcodeSimulatorDriver._prepare_device(self, candidate)

    @staticmethod
    def _wda_port() -> int:
        raw = os.environ.get(WDA_PORT_ENV)
        if not raw:
            return WDA_DEFAULT_PORT
        try:
            port = int(raw)
        except ValueError as error:
            raise ValueError(f"{WDA_PORT_ENV} must be a TCP port number (got {raw!r}).") from error
        if not 0 < port < 65536:
            raise ValueError(f"{WDA_PORT_ENV} must be a TCP port number (got {raw!r}).")
        return port

    async def _wda_candidates(self) -> list[str]:
        # Simulators share the host network: no tunnel, just localhost on a
        # port that is free or already held by this simulator's own WDA.
        port = await self._choose_port()
        candidates: list[str] = []
        for raw in (os.environ.get(WDA_URL_ENV), f"127.0.0.1:{port}"):
            url = normalize_wda_url(raw) if raw else ""
            if url and url not in candidates:
                candidates.append(url)
        return candidates

    async def _choose_port(self) -> int:
        """The configured or default port, unless another process holds it.

        A WDA on the port that runs on a different simulator is never used:
        its taps and screens would be that simulator's. With
        ``ARTEMIS_IOS_WDA_PORT`` set, that is an error; otherwise the
        simulator moves to its own fallback port.
        """
        if self._port is not None:
            return self._port
        mine = self._device_id.upper()
        configured = bool(os.environ.get(WDA_PORT_ENV))
        first = self._wda_port()
        for port in [first] if configured else [first, *fallback_ports(mine)]:
            owner = await port_owner(port)
            if owner.state in ("free", "unknown") or (owner.udid or "").upper() == mine:
                if port != first:
                    logger.info(
                        f"Port {first} is held by {self._describe(await port_owner(first))}; "
                        f"using port {port} for simulator {self._device_id}."
                    )
                self._port = port
                return port
            if configured:
                raise RuntimeError(
                    f"{WDA_PORT_ENV}={port} is held by {self._describe(owner)}, not simulator "
                    f"{self._device_id}. Driving it would act on the wrong device. Unset "
                    f"{WDA_PORT_ENV} to let Lunatap pick a free port, or stop that server."
                )
        raise RuntimeError(
            f"No free WebDriverAgent port for simulator {self._device_id}: {first} and "
            f"{', '.join(map(str, fallback_ports(mine)))} are all in use. Stop unused "
            f"WebDriverAgent servers or set {WDA_PORT_ENV}."
        )

    @staticmethod
    def _describe(owner: PortOwner) -> str:
        if owner.state == "simulator":
            return f"the WebDriverAgent of simulator {owner.udid}"
        if owner.state == "other":
            return f"another process ({owner.command.split()[0] if owner.command else '?'})"
        return "another process"

    async def _verify_wda_device(self, candidate: dict[str, Any]) -> None:
        """The endpoint must be a simulator, and the one we selected.

        WDA reports ``UIDevice.name``, which on a simulator is the simctl
        device name; a different name means another simulator's WDA owns the
        port. Same-named simulators cannot be told apart, so give each
        concurrently used simulator its own ``ARTEMIS_IOS_WDA_PORT``.
        """
        assert self._wda is not None
        info = await self._wda.device_info()
        if info.get("isSimulator") is not True:
            raise RuntimeError(
                f"The WebDriverAgent at {self._wda.base_url} is not running on a simulator "
                f"(isSimulator={info.get('isSimulator')!r}); refusing to drive simulator "
                f"{self._device_id}. Use a different {WDA_PORT_ENV} for the simulator."
            )
        expected = (candidate.get("name") or "").strip()
        reported = str(info.get("name") or "").strip()
        if expected and reported and reported != expected:
            raise RuntimeError(
                f"The WebDriverAgent at {self._wda.base_url} runs on simulator {reported!r}, "
                f"not the selected {expected!r} ({self._device_id}). Stop that server or set "
                f"{WDA_PORT_ENV} to a free port."
            )

    def _xctestrun_path(self) -> str | None:
        configured = os.environ.get(WDA_XCTESTRUN_ENV)
        if configured:
            return configured
        found = default_simulator_xctestrun()
        return str(found) if found is not None else None

    def _xctest_env(self) -> dict[str, str]:
        return {"TEST_RUNNER_USE_PORT": str(self._port or self._wda_port())}

    async def _start_wda_server(self) -> None:
        xctestrun = self._xctestrun_path()
        if not xctestrun:
            raise RuntimeError(
                f"No WebDriverAgent server answers on 127.0.0.1:{self._port or self._wda_port()} for "
                f"simulator {self._device_id}. {_SIM_WDA_SETUP_HINT}"
            )
        await self._start_xctest_session(xctestrun)

    def _wda_timeout_hint(self) -> str:
        return _SIM_WDA_SETUP_HINT

    def _require_connected(self) -> None:
        if not self._session_key:
            raise RuntimeError("Connect the iOS simulator driver before interacting.")

    # --- Observation ---

    async def _screenshot_png(self) -> bytes:
        """WDA screenshot (same framebuffer as the hierarchy); simctl fallback."""
        if self._wda is not None:
            try:
                return await self._wda.screenshot_png()
            except (RuntimeError, WdaUnavailableError, OSError) as exc:
                logger.debug(f"WDA screenshot failed, falling back to simctl: {exc}")
        with tempfile.TemporaryDirectory(prefix="artemis-shot-") as tmp:
            target = Path(tmp) / "shot.png"
            await run_xcrun(
                "simctl",
                "io",
                self._device_id,
                "screenshot",
                "--type=png",
                str(target),
                timeout=SIMCTL_SCREENSHOT_TIMEOUT,
            )
            data = target.read_bytes()
        if not data:
            raise RuntimeError("simctl produced an empty screenshot.")
        return data

    # --- App lifecycle via simctl (same as the native simulator driver) ---

    async def launch_app(self, package_name: str) -> bool:
        return await XcodeSimulatorDriver.launch_app(self, package_name)

    async def stop_app(self, package_name: str) -> bool:
        return await XcodeSimulatorDriver.stop_app(self, package_name)

    async def install_app(self, app_path: Path) -> str:
        return await XcodeSimulatorDriver.install_app(self, app_path)

    async def list_apps(self) -> dict[str, str]:
        return await XcodeSimulatorDriver.list_apps(self)

    async def open_url(self, url: str) -> bool:
        return await XcodeSimulatorDriver.open_url(self, url)

    async def execute_shell(self, command: str, timeout_seconds: float = 15.0) -> str:
        raise NotImplementedError("Android shell commands are unavailable on iOS Simulator.")

    # --- Recording: native simctl recordVideo ---

    def _new_recorder(self) -> IosScreenRecorder:
        return IosScreenRecorder(self._device_id)

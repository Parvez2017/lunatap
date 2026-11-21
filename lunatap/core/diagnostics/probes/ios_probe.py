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

"""iOS Device Readiness Probe.

Required (``is_blocker = True``): Lunatap only drives iOS, so a host without
Xcode, a usable simulator backend, or any simulator/paired device cannot run a
task. The probe reports whether this host could run a task today.
"""

import asyncio

from lunatap.core.diagnostics.probes.base import BaseProbe
from lunatap.core.diagnostics.schema import (
    ProbeAction,
    ProbeCategory,
    ProbeResult,
    ProbeStatus,
)
from lunatap.drivers.ios.discovery import (
    NATIVE_INTERACTION_MIN_XCODE,
    WDA_MIN_XCODE,
    is_physical_ios,
    list_core_devices,
    list_ios_simulators,
    parse_xcode_version,
    physical_ios_ready,
    run_xcrun,
    simctl_available,
    simulator_backend,
)


class IosDeviceProbe(BaseProbe):
    """Reports iOS automation capability: Xcode version, simulators, physical."""

    @property
    def probe_id(self) -> str:
        # Historical id kept stable: persisted reports reference it.
        return "ios_simulators"

    @property
    def category(self) -> ProbeCategory:
        return ProbeCategory.DEVICE

    @property
    def is_blocker(self) -> bool:
        return True

    async def probe(self) -> ProbeResult:
        if not simctl_available():
            return ProbeResult(
                id=self.probe_id,
                category=self.category,
                title="iOS Devices",
                status=ProbeStatus.FAIL,
                is_blocker=self.is_blocker,
                summary="Unavailable",
                description=(
                    "iOS automation requires macOS with Xcode; "
                    "this host cannot enumerate iOS devices."
                ),
                metadata={"platform_supported": False},
            )

        xcode_major: int | None = None
        xcode_version: str | None = None
        try:
            raw = await asyncio.wait_for(run_xcrun("xcodebuild", "-version"), timeout=15.0)
            xcode_version = parse_xcode_version(raw)
            if xcode_version:
                xcode_major = int(xcode_version.split(".")[0])
        except (OSError, RuntimeError, TimeoutError, ValueError):
            xcode_version = None
        xcode_27 = xcode_major is not None and xcode_major >= NATIVE_INTERACTION_MIN_XCODE
        try:
            backend: str | None = simulator_backend()
        except ValueError:
            backend = None
        # Xcode 15-26 drives simulators through WebDriverAgent; the native
        # backend needs Xcode 27's DeviceInteraction tools.
        required_major = NATIVE_INTERACTION_MIN_XCODE if backend == "xcode" else WDA_MIN_XCODE
        xcode_ok = xcode_major is not None and xcode_major >= required_major
        wda_xctestrun: str | None = None
        if backend == "wda":
            from lunatap.drivers.ios.wda_simulator_driver import (
                WDA_XCTESTRUN_ENV,
                default_simulator_xctestrun,
            )
            import os

            found = os.environ.get(WDA_XCTESTRUN_ENV) or default_simulator_xctestrun()
            wda_xctestrun = str(found) if found else None

        simulators = await list_ios_simulators()
        booted = [d for d in (simulators or []) if d.get("state") == "Booted"]
        core_devices = await list_core_devices()
        physical = [d for d in (core_devices or []) if is_physical_ios(d)]
        connected = [d for d in physical if physical_ios_ready(d)]
        metadata = {
            "platform_supported": True,
            "xcode_version": xcode_version,
            "xcode_27_or_newer": xcode_27,
            "simulator_backend": backend,
            "wda_simulator_xctestrun": wda_xctestrun,
            "ios_automation_ready": xcode_ok and backend is not None,
            "simulator_count": len(simulators or []),
            "booted_udids": [d.get("udid") for d in booted],
            "simulators": simulators or [],
            "physical_device_count": len(physical),
            "connected_physical_devices": [
                {
                    "udid": d.get("udid"),
                    "name": d.get("name"),
                    "os_version": d.get("os_version"),
                    "product_type": d.get("product_type"),
                }
                for d in connected
            ],
        }

        if not xcode_ok:
            return ProbeResult(
                id=self.probe_id,
                category=self.category,
                title="iOS Devices",
                status=ProbeStatus.WARN,
                is_blocker=self.is_blocker,
                summary=f"Xcode {required_major}+ Required",
                description=(
                    f"iOS automation ({backend or 'auto'} simulator backend) requires "
                    f"Xcode {required_major} or later"
                    + (f" (found {xcode_version})." if xcode_version else "; none detected.")
                ),
                metadata=metadata,
                actions=[
                    ProbeAction(
                        action_type="hint",
                        label="Select Xcode",
                        payload="sudo xcode-select -s /Applications/Xcode.app/Contents/Developer",
                    ),
                ],
            )

        if backend is None:
            return ProbeResult(
                id=self.probe_id,
                category=self.category,
                title="iOS Devices",
                status=ProbeStatus.WARN,
                is_blocker=self.is_blocker,
                summary="Invalid Simulator Backend",
                description="ARTEMIS_IOS_SIM_BACKEND must be auto, xcode, or wda.",
                metadata=metadata,
            )

        if backend == "wda" and simulators and not wda_xctestrun and not connected:
            return ProbeResult(
                id=self.probe_id,
                category=self.category,
                title="iOS Devices",
                status=ProbeStatus.WARN,
                is_blocker=self.is_blocker,
                summary="WebDriverAgent Build Needed",
                description=(
                    f"Xcode {xcode_version} drives simulators through WebDriverAgent. "
                    "Build it once (no signing needed), or point ARTEMIS_IOS_WDA_URL "
                    "at a WDA server you already run."
                ),
                metadata=metadata,
                actions=[
                    ProbeAction(
                        action_type="hint",
                        label="Build WebDriverAgent",
                        payload="bash scripts/setup_ios_wda_simulator.sh",
                    ),
                ],
            )

        if not simulators and not connected:
            return ProbeResult(
                id=self.probe_id,
                category=self.category,
                title="iOS Devices",
                status=ProbeStatus.WARN,
                is_blocker=self.is_blocker,
                summary="No Devices",
                description=(
                    "Xcode is installed but no available iOS simulators or connected "
                    "physical devices were found. Create a simulator in Xcode > "
                    "Settings > Platforms, or attach a paired iPhone/iPad."
                ),
                metadata=metadata,
            )

        parts = []
        if booted:
            parts.append(f"{len(booted)} simulator(s) booted")
        elif simulators:
            parts.append(f"{len(simulators)} simulator(s) available")
        if connected:
            parts.append(f"{len(connected)} physical device(s) connected")
        summary = ", ".join(parts)
        return ProbeResult(
            id=self.probe_id,
            category=self.category,
            title="iOS Devices",
            status=ProbeStatus.PASS,
            is_blocker=self.is_blocker,
            summary=summary,
            description=(
                f"Xcode {xcode_version} ({backend} simulator backend): {summary}. "
                "Submit a task with platform='ios' and a device UDID to use them."
            ),
            metadata=metadata,
        )


# Backward-compatible name from when the probe only covered simulators.
IosSimulatorProbe = IosDeviceProbe

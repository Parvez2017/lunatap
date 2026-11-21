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

"""Device driver factory: the iOS driver for a target (the mock driver in tests)."""

import os
from typing import TYPE_CHECKING


from lunatap.drivers.base import BaseDeviceDriver
from lunatap.drivers.mock.mock_driver import MockDeviceDriver
from third_party.mobile_use.utils.logger import get_logger

if TYPE_CHECKING:
    from lunatap.context import ArtemisContext

logger = get_logger(__name__)


def ios_driver_class(device_id: str | None) -> type[BaseDeviceDriver]:
    """Pick the driver class for an iOS serial by device reality.

    A serial resolving to a CoreDevice physical entry maps to
    ``PhysicalIosDriver``; simulators, ``booted``, and unknown serials keep a
    simulator driver so its validation and error paths apply. The simulator
    driver is ``XcodeSimulatorDriver`` (Xcode 27 native tools) or
    ``WdaSimulatorDriver`` (WebDriverAgent, Xcode 15+), chosen by
    ``ARTEMIS_IOS_SIM_BACKEND`` (``auto`` picks by the selected Xcode version).
    """
    from lunatap.drivers.ios.discovery import BOOTED_SIMULATOR_ID, simulator_backend

    simulator_driver: type[BaseDeviceDriver]
    if simulator_backend() == "wda":
        from lunatap.drivers.ios.wda_simulator_driver import WdaSimulatorDriver

        simulator_driver = WdaSimulatorDriver
    else:
        from lunatap.drivers.ios.xcode_driver import XcodeSimulatorDriver

        simulator_driver = XcodeSimulatorDriver

    if device_id and device_id.strip().lower() != BOOTED_SIMULATOR_ID:
        from lunatap.drivers.ios.discovery import (
            device_matches_identifier,
            find_physical_ios_device_sync,
            list_ios_simulators_sync,
        )

        simulators = list_ios_simulators_sync()
        if simulators is None or not any(
            device_matches_identifier(device, device_id) for device in simulators
        ):
            if find_physical_ios_device_sync(device_id) is not None:
                from lunatap.drivers.ios.physical_driver import PhysicalIosDriver

                return PhysicalIosDriver
    return simulator_driver


def _create_ios_driver(ctx: "ArtemisContext") -> BaseDeviceDriver:
    """Instantiate the simulator or physical-device driver for an iOS target."""
    if os.environ.get("ARTEMIS_CLOUD_MODE") == "1":
        raise ValueError("Lunatap runs iOS devices locally; cloud mode is not supported.")
    config = getattr(ctx, "agent_config", None)
    workspace_path = getattr(config, "ios_workspace_path", None) if config is not None else None
    driver_class = ios_driver_class(ctx.device.device_id)
    return driver_class(
        device_id=ctx.device.device_id,
        width=ctx.device.device_width,
        height=ctx.device.device_height,
        workspace_path=workspace_path,
    )


def create_driver(ctx: "ArtemisContext") -> BaseDeviceDriver:
    """Instantiates the BaseDeviceDriver for the runtime context (iOS, or mock in tests)."""
    mobile_platform = getattr(ctx.device, "mobile_platform", "ios")
    if mobile_platform == "ios":
        return _create_ios_driver(ctx)
    if mobile_platform == "mock" or os.environ.get("ARTEMIS_MOCK_DRIVER") == "1":
        return MockDeviceDriver(
            device_id=ctx.device.device_id if ctx.device else "mock-device",
            width=ctx.device.device_width if ctx.device else 1080,
            height=ctx.device.device_height if ctx.device else 2400,
        )
    raise ValueError(f"Unsupported mobile platform: {mobile_platform}. Lunatap supports iOS only.")


def get_driver(ctx: "ArtemisContext") -> BaseDeviceDriver:
    """Cached accessor for device driver in the current context."""
    if not hasattr(ctx, "_active_driver") or getattr(ctx, "_active_driver") is None:
        driver = create_driver(ctx)
        setattr(ctx, "_active_driver", driver)
    return getattr(ctx, "_active_driver")

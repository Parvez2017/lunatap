# Copyright 2026 The Lunatap Authors
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

"""Cached device controllers for out-of-task callers (MCP tools, diagnostics).

Moved from the former ``lunatap.mcp.adb_server`` (an Android ADB MCP server);
only its controller cache survives, now iOS-only.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from lunatap.context import ArtemisContext, DeviceContext, DevicePlatform
from lunatap.controllers.unified_controller import UnifiedMobileController
from lunatap.drivers.ios.discovery import BOOTED_SIMULATOR_ID
from lunatap.platform import platform

logger = logging.getLogger(__name__)

# Placeholder screen size until the driver reports the real metrics.
_LAZY_IOS_PLACEHOLDER_WIDTH = 1206
_LAZY_IOS_PLACEHOLDER_HEIGHT = 2622

_CONTROLLERS: dict[str, Any] = {}


def get_controller(device_serial: str | None = None, target_platform: str | None = None):
    """Lazy-load an iOS device controller, cached per device.

    ``device_serial`` is a simulator UDID (or "booted") or a paired physical
    device UDID. Callers must ``await driver.connect()`` before interacting;
    the native session opens lazily on first use. ``target_platform`` is kept
    for call-site compatibility; iOS is the only platform.
    """
    del target_platform
    target_serial = device_serial or os.environ.get("ARTEMIS_DEVICE_ID") or BOOTED_SIMULATOR_ID
    cache_key = f"ios:{target_serial}"
    if cache_key in _CONTROLLERS:
        return _CONTROLLERS[cache_key]
    logger.info("Initializing lazy iOS device controller...")
    ctx = ArtemisContext(
        trace_id="mcp-session",
        device=DeviceContext(
            host_platform=platform.os_type.name,
            mobile_platform=DevicePlatform.IOS,
            device_id=target_serial,
            device_width=_LAZY_IOS_PLACEHOLDER_WIDTH,
            device_height=_LAZY_IOS_PLACEHOLDER_HEIGHT,
        ),
    )
    controller = UnifiedMobileController(ctx=ctx)
    _CONTROLLERS[cache_key] = controller
    return controller

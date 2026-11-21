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

"""Unit tests for Unified Base Device Driver and implementations."""

import base64
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from lunatap.drivers.base import KeyCode, ScreenData, SwipeDirection
from lunatap.drivers.mock.mock_driver import MockDeviceDriver
from third_party.mobile_use.controllers.types import ElementQuery


_ONE_PIXEL_PNG = base64.b64encode(
    base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
    )
).decode("ascii")


@pytest.mark.asyncio
async def test_mock_driver_actions():
    """Verify MockDeviceDriver records and performs all core actions."""
    driver = MockDeviceDriver(device_id="test-mock", width=1080, height=2400)
    assert driver.device_id == "test-mock"
    assert driver.screen_size == (1080, 2400)

    # 1. Screen data
    screen_data = await driver.get_screen_data()
    assert isinstance(screen_data, ScreenData)
    assert screen_data.width == 1080
    assert screen_data.height == 2400
    assert screen_data.screenshot_base64 is not None

    # 2. Tap
    assert await driver.tap(500, 1000) is True
    assert driver.action_history[-1]["action"] == "tap"
    assert driver.action_history[-1]["x"] == 500

    # 3. Swipe
    assert await driver.swipe(100, 200, 300, 400) is True
    assert driver.action_history[-1]["action"] == "swipe"

    # 4. Swipe direction
    assert await driver.swipe_direction(SwipeDirection.UP) is True
    assert driver.action_history[-1]["direction"] == "up"

    # 5. Input text
    assert await driver.input_text("Hello Artemis") is True
    assert driver.action_history[-1]["text"] == "Hello Artemis"

    # 6. Press key
    assert await driver.press_key(KeyCode.HOME) is True
    assert driver.action_history[-1]["key"] == "home"

    # 7. App lifecycle
    assert await driver.launch_app("com.android.settings") is True
    assert await driver.get_current_package() == "com.android.settings"
    assert await driver.stop_app("com.android.settings") is True


@pytest.mark.asyncio
async def test_find_element_prefers_fresh_bounds_over_stale_center():
    """Layout transitions must not leave dynamic element taps at old coordinates."""
    driver = MockDeviceDriver(device_id="test-mock", width=1080, height=2400)
    driver.get_screen_data = AsyncMock(
        return_value=ScreenData(
            screenshot_bytes=base64.b64decode(_ONE_PIXEL_PNG),
            screenshot_base64=_ONE_PIXEL_PNG,
            ui_elements=[
                {
                    "resource_id": "app:id/digit_4",
                    "bounds": "[10,1700][270,1940]",
                    "parsed_bounds": {
                        "left": 10,
                        "top": 1700,
                        "right": 270,
                        "bottom": 1940,
                    },
                    "center": [140, 1600],
                }
            ],
            width=1080,
            height=2400,
        )
    )

    _, center, error = await driver.find_element(ElementQuery(resource_id="app:id/digit_4"))

    assert error is None
    assert center == [140, 1820]

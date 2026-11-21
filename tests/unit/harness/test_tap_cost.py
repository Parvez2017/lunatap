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

"""A tap costs as few screen captures as its safety checks allow."""

import base64

import pytest

from tests.unit.harness.test_session import COLORS, _png


@pytest.mark.asyncio
async def test_a_text_target_is_not_looked_at_twice(started, monkeypatch):
    session, controller, actuator = started
    shots = []

    async def counted():
        shots.append(1)
        return _png(COLORS[controller.screen])

    monkeypatch.setattr(actuator, "take_screenshot", counted)
    outcome = await session.act("tap", target="General")
    assert outcome.ok
    assert shots == []  # the re-read for the text target is the check


@pytest.mark.asyncio
async def test_the_pixel_check_uses_a_screenshot_only_capture(started):
    session, controller, actuator = started
    pngs = []

    async def screenshot_only():
        pngs.append(1)
        return base64.b64decode(_png(COLORS[controller.screen]))

    async def full_capture():
        raise AssertionError("the pre-tap check must not read the hierarchy")

    controller.driver.screenshot_only = screenshot_only
    observation = await session.observe()
    general = next(e for e in observation.elements if e["text"] == "General")
    actuator.take_screenshot = full_capture
    assert (await session.act("tap", target=general["index"])).ok
    assert pngs == [1]

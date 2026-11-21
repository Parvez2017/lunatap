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

"""open_app and HOME stay usable while one app's screen read hangs (QA bug 23)."""

import pytest

from lunatap.drivers.ios.wda import WdaBusyError

STUCK = "WebDriverAgent is up, but reading the screen of com.apple.Preferences timed out after 30s."


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action, kwargs",
    [("open_app", {"app": "com.example.app"}), ("press_key", {"key": "HOME"})],
)
async def test_the_way_out_reports_success_when_only_the_read_hangs(
    started, monkeypatch, action, kwargs
):
    session, controller, actuator = started

    async def stuck():
        raise WdaBusyError(STUCK)

    monkeypatch.setattr(controller, "get_screen_data", stuck)
    outcome = await session.act(action, **kwargs)
    assert outcome.ok
    assert actuator.calls, "the action itself must still be sent"
    assert "could not be read after it" in outcome.message and "Preferences" in outcome.message
    assert session.last_observation is None


@pytest.mark.asyncio
async def test_observe_on_a_stuck_screen_surfaces_the_named_error(started, monkeypatch):
    session, controller, _ = started

    async def stuck():
        raise WdaBusyError(STUCK)

    monkeypatch.setattr(controller, "get_screen_data", stuck)
    with pytest.raises(WdaBusyError, match="com.apple.Preferences"):
        await session.observe()

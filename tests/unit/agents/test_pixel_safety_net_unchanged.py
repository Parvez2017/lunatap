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

"""The pixel Safety Net compares the target region itself before asking a model."""

import base64
import io
from unittest.mock import AsyncMock, MagicMock

import pytest
from PIL import Image, ImageDraw

from lunatap.agents.validator import precondition_pixel as pp
from lunatap.agents.validator.categories import ValidationErrorCategory

TARGET = [300, 900]


def _png(stamp: tuple[int, int] | None = None) -> bytes:
    image = Image.new("RGB", (600, 1200), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((250, 870, 350, 930), fill="blue")  # the button
    if stamp:
        x, y = stamp
        draw.rectangle((x - 40, y - 40, x + 40, y + 40), fill="red")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _b64(png: bytes) -> str:
    return base64.b64encode(png).decode()


def _session(live_png: bytes):
    session = MagicMock()
    session.screenshot_b64 = AsyncMock(return_value=_b64(live_png))
    return session


TAP = {"action": "tap", "coordinates": TARGET}


@pytest.mark.asyncio
async def test_an_unchanged_target_region_passes_without_the_model():
    get_llm = MagicMock()
    passed, category, _ = await pp.validate_action_precondition_pixel(
        MagicMock(), _session(_png()), TAP, _b64(_png()), TARGET, get_llm_fn=get_llm
    )
    assert (passed, category) == (True, ValidationErrorCategory.NONE)
    get_llm.assert_not_called()


@pytest.mark.asyncio
async def test_a_change_elsewhere_on_screen_does_not_count():
    get_llm = MagicMock()
    passed, _, _ = await pp.validate_action_precondition_pixel(
        MagicMock(), _session(_png(stamp=(300, 100))), TAP, _b64(_png()), TARGET, get_llm_fn=get_llm
    )
    assert passed
    get_llm.assert_not_called()


@pytest.mark.asyncio
async def test_a_changed_target_region_asks_the_model(monkeypatch):
    monkeypatch.setattr(
        pp,
        "acomplete_structured",
        AsyncMock(return_value={"is_present": False, "confidence": 0.9, "reasoning": "gone"}),
    )
    get_llm = MagicMock()
    passed, category, reason = await pp.validate_action_precondition_pixel(
        MagicMock(), _session(_png(stamp=TARGET)), TAP, _b64(_png()), TARGET, get_llm_fn=get_llm
    )
    assert (passed, category) == (False, ValidationErrorCategory.PIXEL_TARGET_DISAPPEARED)
    assert "gone" in reason
    get_llm.assert_called_once()

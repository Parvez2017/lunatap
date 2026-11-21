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

"""Apple Vision: local OCR without a Google key, and the pre-tap text check."""

import base64
import io
from unittest.mock import AsyncMock, Mock

import pytest
from PIL import Image

from lunatap.agents.validator import execution_loop
from lunatap.agents.validator.categories import ValidationErrorCategory
from lunatap.drivers.ios import screen_text
from lunatap.drivers.ios.screen_text import TapVerdict, TextBox
from lunatap.mcp.action_executor import McpActionExecutor
from lunatap.mcp.action_types import ActionCode, ActionResult
from lunatap.utils import ocr_api

# A "Settings" row whose tap point is covered by a floating "Search" bar.
COVERED_ROW = {
    "text": "General",
    "class": "Cell",
    "center": [600, 2240],
    "hit_point": [600, 2240],
    "bounds": "[40,2190][1160,2290]",
}
SCREEN_TEXT = [TextBox("Q Search", (110, 2215, 330, 2265), 1.0)]


def _png(width=1206, height=2622) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), "white").save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.fixture
def vision(monkeypatch):
    """Apple Vision switched on, reading ``SCREEN_TEXT`` from any screenshot."""
    monkeypatch.setenv("LUNATAP_LOCAL_OCR", "1")
    monkeypatch.setattr(screen_text, "available", lambda: True)
    reads = []

    def recognize(image, *, fast=False):
        reads.append(image)
        return list(SCREEN_TEXT)

    monkeypatch.setattr(screen_text, "recognize_text", recognize)
    screen_text._RECOGNIZED.clear()
    return reads


# --- Local OCR when no Google Vision key is set ---


@pytest.mark.asyncio
async def test_ocr_falls_back_to_apple_vision_without_a_google_key(vision, monkeypatch):
    monkeypatch.setattr(type(ocr_api.settings), "get_api_key", lambda self, name: None)
    for name in ("OCR_API_KEY", "VISION_API_KEY"):
        monkeypatch.delenv(name, raising=False)

    assert ocr_api.is_ocr_configured()
    results = await ocr_api.perform_ocr(base64.b64encode(_png()).decode())

    assert results == [
        {
            "text": "Q Search",
            "position": [
                {"x": 110, "y": 2215},
                {"x": 330, "y": 2215},
                {"x": 330, "y": 2265},
                {"x": 110, "y": 2265},
            ],
        }
    ]


def test_local_ocr_can_be_switched_off(vision, monkeypatch):
    monkeypatch.setenv("LUNATAP_LOCAL_OCR", "0")
    assert not ocr_api.local_ocr_available()
    assert not screen_text.enabled()


# --- check_element_tap ---


def test_covered_element_is_a_mismatch(vision):
    check = screen_text.check_element_tap(
        _png(),
        label="General",
        kind="XCUIElementTypeCell",
        point=(600, 2240),
        bounds=(40, 2190, 1160, 2290),
    )
    assert check.verdict is TapVerdict.MISMATCH
    assert check.seen == ("Q Search",)


def test_containers_and_unlabelled_elements_are_not_judged(vision):
    png = _png()
    for label, kind in (("General", "ScrollView"), (None, "Button")):
        check = screen_text.check_element_tap(
            png, label=label, kind=kind, point=(600, 2240), bounds=(40, 2190, 1160, 2290)
        )
        assert check.verdict is TapVerdict.UNKNOWN
    assert vision == []  # no OCR spent on them


def test_undecodable_screenshot_is_not_judged(vision):
    check = screen_text.check_element_tap(
        b"not an image", label="General", kind="Cell", point=(1, 1), bounds=(0, 0, 2, 2)
    )
    assert check.verdict is TapVerdict.UNKNOWN


def test_ocr_runs_once_per_screenshot(vision):
    png = _png()
    for _ in range(3):
        screen_text.check_element_tap(
            png, label="General", kind="Cell", point=(600, 2240), bounds=(40, 2190, 1160, 2290)
        )
    assert len(vision) == 1


# --- Flash: the executor refuses a covered index tap once ---


def _flash(tmp_path):
    ctx = Mock()
    ctx.device.device_width = 1206
    ctx.device.device_height = 2622
    actuator = Mock()
    actuator.controller = Mock()
    actuator.extensions.return_value = []
    executor = McpActionExecutor(ctx, actuator=actuator)
    session = Mock()
    session.call = AsyncMock(return_value=ActionResult.success("click", "tapped"))
    session.observe = AsyncMock(return_value=Mock(ok=False))
    executor._session = session
    shot = tmp_path / "screen.png"
    shot.write_bytes(_png())
    state = Mock()
    state.indexed_elements = [dict(COVERED_ROW)]
    state.latest_screenshot = str(shot)
    return executor, session, state


@pytest.mark.asyncio
async def test_flash_refuses_a_covered_tap_then_allows_the_repeat(vision, tmp_path):
    executor, session, state = _flash(tmp_path)

    refused = await executor.execute("click", {"target": 1}, "call-1", state)
    assert refused.status == "error"
    assert refused.metadata["code"] == ActionCode.TARGET_NOT_FOUND.value
    assert "'General' is not visible" in refused.text_summary
    assert "Q Search" in refused.text_summary
    session.call.assert_not_awaited()

    repeated = await executor.execute("click", {"target": 1}, "call-2", state)
    assert repeated.status == "success"
    session.call.assert_awaited_once()


@pytest.mark.asyncio
async def test_flash_taps_when_the_label_is_visible(vision, tmp_path, monkeypatch):
    monkeypatch.setattr(
        screen_text,
        "recognize_text",
        lambda image, fast=False: [TextBox("General", (150, 2215, 335, 2270), 1.0)],
    )
    executor, session, state = _flash(tmp_path)

    result = await executor.execute("click", {"target": 1}, "call-1", state)

    assert result.status == "success"
    session.call.assert_awaited_once()


@pytest.mark.asyncio
async def test_flash_skips_the_check_with_vision_off(vision, tmp_path, monkeypatch):
    monkeypatch.setenv("LUNATAP_LOCAL_OCR", "0")
    executor, session, state = _flash(tmp_path)

    assert (await executor.execute("click", {"target": 1}, "c", state)).status == "success"
    assert vision == []


# --- Pro: a Vision mismatch sends a passing tree check to the pixel check ---


def _pro_tap():
    return {
        "action": "tap",
        "coordinates": [600, 2240],
        "target_text": "General",
        "target_bounds": COVERED_ROW["bounds"],
        "target_class": "Cell",
    }


def _node(pixel_passes: bool):
    node = Mock()
    node._validate_action_precondition = AsyncMock(
        return_value=(True, ValidationErrorCategory.NONE, "")
    )
    pixel = (
        (True, ValidationErrorCategory.NONE, "")
        if pixel_passes
        else (False, ValidationErrorCategory.PIXEL_TARGET_DISAPPEARED, "Covered by the search bar.")
    )
    node._validate_action_precondition_pixel = AsyncMock(return_value=pixel)
    return node


@pytest.mark.asyncio
@pytest.mark.parametrize("pixel_passes", [True, False])
async def test_pro_vision_mismatch_defers_to_the_pixel_check(vision, pixel_passes):
    node = _node(pixel_passes)
    shot = base64.b64encode(_png()).decode()

    passed, _category, reason = await execution_loop._run_precondition_gate(
        node, Mock(), _pro_tap(), Mock(), shot, [600, 2240]
    )

    node._validate_action_precondition_pixel.assert_awaited_once()
    assert passed is pixel_passes
    if not pixel_passes:
        assert "'General' is not visible" in reason


@pytest.mark.asyncio
async def test_pro_skips_the_pixel_check_when_vision_agrees(vision, monkeypatch):
    monkeypatch.setattr(
        screen_text,
        "recognize_text",
        lambda image, fast=False: [TextBox("General", (150, 2215, 335, 2270), 1.0)],
    )
    node = _node(pixel_passes=False)
    shot = base64.b64encode(_png()).decode()

    passed, _, _ = await execution_loop._run_precondition_gate(
        node, Mock(), _pro_tap(), Mock(), shot, [600, 2240]
    )

    assert passed
    node._validate_action_precondition_pixel.assert_not_awaited()

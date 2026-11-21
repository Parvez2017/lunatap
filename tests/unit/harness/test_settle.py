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

"""A screen read mid-transition is read again (QA bug 32)."""

import pytest

from lunatap.controllers.device_controller import ScreenDataResponse
from lunatap.drivers.ios.hierarchy import layout_shift, pixel_element
from tests.unit.harness.test_session import COLORS, SCREENS, _png

W, H = 1320, 2868


def _moved(elements, dy):
    out = []
    for e in elements:
        b = e["parsed_bounds"]
        moved = pixel_element(
            text=e["text"],
            resource_id="",
            class_name=e["class"],
            left=b["left"],
            top=b["top"] + dy,
            right=b["right"],
            bottom=b["bottom"] + dy,
        )
        moved["hit_point"] = [(b["left"] + b["right"]) // 2, (b["top"] + b["bottom"]) // 2 + dy]
        out.append(moved)
    return out


def test_layout_shift_is_the_median_move_of_shared_labels():
    about = SCREENS["about"] + [
        pixel_element(
            text="Model",
            resource_id="",
            class_name="StaticText",
            left=60,
            top=1300,
            right=600,
            bottom=1400,
        )
    ]
    assert layout_shift(about, about) == 0
    assert layout_shift(about, _moved(about, -160)) == 160  # ~62 units of 1000 on a phone
    assert layout_shift(about, SCREENS["general"] + SCREENS["alert"]) is None  # another screen


@pytest.mark.asyncio
async def test_a_reply_read_mid_transition_is_read_again(started):
    session, controller, _ = started
    reads = {"full": 0, "tree": 0}
    real = controller.get_screen_data

    async def get_screen_data():
        reads["full"] += 1
        data = await real()
        if controller.screen == "about" and reads["full"] == 3:
            # The first read after the tap: the push animation is still running.
            return ScreenDataResponse(
                base64=data.base64, elements=_moved(data.elements, -160), width=W, height=H
            )
        return data

    async def hierarchy_elements():
        reads["tree"] += 1
        return list(SCREENS[controller.screen])

    controller.get_screen_data = get_screen_data
    controller.driver.hierarchy_elements = hierarchy_elements
    controller.screen = "general"
    await session.observe()  # read 1
    await session.act("tap", target="About")  # read 2 (text target), 3 (moving), 4 (settled)
    after = session.last_observation
    assert after.title == "About"
    version = next(e for e in after.elements if e["text"] == "iOS Version")
    assert version["bounds"][1] == 1100  # settled, not 160 px too high
    assert reads == {"full": 4, "tree": 2}


@pytest.mark.asyncio
async def test_a_settled_reply_costs_one_tree_read(started):
    session, controller, _ = started
    reads = []

    async def hierarchy_elements():
        reads.append(1)
        return list(SCREENS[controller.screen])

    controller.driver.hierarchy_elements = hierarchy_elements
    await session.observe()
    await session.act("tap", target="General")
    assert reads == [1]
    await session.act("swipe", direction="up")  # the screen did not change: no check
    assert reads == [1]


def _ocr_of(elements, dy=0):
    out = []
    for e in elements:
        b = e["parsed_bounds"]
        out.append(
            {
                "text": e["text"],
                "position": [
                    {"x": b["left"], "y": b["top"] + dy},
                    {"x": b["right"], "y": b["bottom"] + dy},
                ],
            }
        )
    return out


def test_pixel_tree_shift_compares_labels_with_the_screenshot():
    from lunatap.utils.ocr_xml_fusion import pixel_tree_shift

    about = SCREENS["about"]
    assert pixel_tree_shift(about, _ocr_of(about)) == 0
    assert pixel_tree_shift(about, _ocr_of(about, dy=160)) == 160
    assert pixel_tree_shift(about, []) is None


@pytest.mark.asyncio
async def test_a_tree_that_agrees_with_itself_but_not_the_screenshot_is_read_again(
    started, monkeypatch
):
    # Live (QA, Part 10): on the Flight Map tab two tree reads agreed while
    # the page still slid in; the screenshot (taken after the tree) did not.
    session, controller, _ = started
    reads = {"ocr": 0, "tree": 0, "about": 0}

    async def ocr(screenshot_b64, elements, height):
        reads["ocr"] += 1
        if controller.screen == "about":
            reads["about"] += 1
        stale = reads["about"] == 1  # the first look at About is mid-slide
        return _ocr_of(elements, dy=160 if stale else 0)

    async def hierarchy_elements():
        reads["tree"] += 1
        return list(SCREENS[controller.screen])  # the tree itself looks settled

    monkeypatch.setattr(session, "_ocr", ocr)
    controller.driver.hierarchy_elements = hierarchy_elements
    controller.screen = "general"
    await session.observe()
    await session.act("tap", target="About")
    assert reads["about"] == 2  # the stale read, then the re-read
    assert session.last_observation.pixel_shift == 0

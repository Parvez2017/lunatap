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

"""Screenshot text check for tap targets (iOS 26 Settings, 1206x2622 px)."""

import io

import pytest

from lunatap.drivers.ios import screen_text
from lunatap.drivers.ios.screen_text import TapVerdict, TextBox

SCREEN = (1206, 2622)

# What Vision reads on Settings scrolled so "Apps" sits under the search bar.
SETTINGS_TEXT = [
    TextBox("Battery", (150, 2050, 330, 2105), 1.0),
    TextBox("General", (150, 2215, 335, 2270), 1.0),
    TextBox("Q Search", (110, 2440, 330, 2495), 1.0),
]
BATTERY_ROW = (48, 1992, 1158, 2163)


def test_label_visible_at_its_row_matches():
    result = screen_text.check_tap_target(SETTINGS_TEXT, "Battery", (603, 2077), BATTERY_ROW)
    assert result.verdict is TapVerdict.MATCH


def test_row_under_the_search_bar_is_a_mismatch():
    result = screen_text.check_tap_target(
        SETTINGS_TEXT, "Apps", (603, 2481), (48, 2400, 1158, 2571)
    )
    assert result.verdict is TapVerdict.MISMATCH
    assert result.seen == ("Q Search",)
    assert "'Apps' is not visible" in result.describe("Apps")


def test_no_text_near_the_point_is_unknown():
    # Icons, images, or a row scrolled under a bar with no text on it.
    result = screen_text.check_tap_target(SETTINGS_TEXT, "Accessibility", (603, 189))
    assert result.verdict is TapVerdict.UNKNOWN


def test_missing_label_is_unknown():
    assert (
        screen_text.check_tap_target(SETTINGS_TEXT, "", (603, 2077)).verdict is TapVerdict.UNKNOWN
    )
    assert (
        screen_text.check_tap_target(SETTINGS_TEXT, None, (603, 2077)).verdict is TapVerdict.UNKNOWN
    )


@pytest.mark.parametrize(
    ("label", "seen"),
    [
        ("Wi-Fi", "Wi Fi"),  # OCR drops punctuation
        ("General", "General >"),  # chevron read as text
        ("Notifications & Focus", "Notifications Focus"),  # symbol lost
        ("Privacy & Security settings", "Privacy & Security"),  # truncated on screen
    ],
)
def test_matching_tolerates_ocr_noise(label, seen):
    boxes = [TextBox(seen, (150, 500, 600, 560), 1.0)]
    assert screen_text.check_tap_target(boxes, label, (300, 530)).verdict is TapVerdict.MATCH


def test_label_left_of_the_center_counts_when_inside_the_element():
    # A cell's center is often empty space; its label sits at the left edge.
    boxes = [TextBox("Battery", (150, 2050, 330, 2105), 1.0)]
    without_bounds = screen_text.check_tap_target(boxes, "Battery", (900, 2077))
    with_bounds = screen_text.check_tap_target(boxes, "Battery", (900, 2077), BATTERY_ROW)
    assert without_bounds.verdict is TapVerdict.UNKNOWN
    assert with_bounds.verdict is TapVerdict.MATCH


@pytest.mark.parametrize(
    ("kind", "box", "expected"),
    [
        ("Cell", BATTERY_ROW, True),
        ("Button", (900, 100, 1100, 200), True),
        ("Toolbar", (0, 2364, 1206, 2622), False),
        ("Application", (0, 0, 1206, 2622), False),
        ("Image", (0, 0, 1206, 2000), False),  # most of the screen: a backdrop
        ("Button", None, False),
    ],
)
def test_only_leaf_controls_are_checked(kind, box, expected):
    assert screen_text.worth_checking(kind, box, SCREEN) is expected


@pytest.mark.skipif(not screen_text.available(), reason="needs macOS Vision (pyobjc)")
def test_vision_reads_rendered_text_with_top_left_pixel_boxes():
    from PIL import Image, ImageDraw, ImageFont

    image = Image.new("RGB", (800, 400), "white")
    draw = ImageDraw.Draw(image)
    draw.text((60, 260), "General", fill="black", font=ImageFont.load_default(size=72))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")

    boxes = screen_text.recognize_text(buffer.getvalue())
    general = next(box for box in boxes if "General" in box.text)
    left, top, right, bottom = general.box
    # Drawn near the bottom-left: proves the y axis is flipped to top-left origin.
    assert 30 <= left <= 120 and 230 <= top <= 300 and bottom <= 380 and right > left

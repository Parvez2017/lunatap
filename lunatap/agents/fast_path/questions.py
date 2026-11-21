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

"""Every word Clef is asked, in one place (System One request body).

Clef answers two judgments about the current screen; code in ``decider.py``
turns them into an action:

- ``move``: does the plan step need a tap on a listed control, or something
  else (it is off screen, already done, or needs typing/scrolling/reading)?
- ``target``: which listed control carries out the step?

``target`` is asked speculatively in the same request; code reads it only
when ``move`` says to tap. One three-way ``move`` Choice measured better
than two separate yes/no questions (38/40 vs 30/40 on the simulator set).
"""

from __future__ import annotations

import re
from typing import Any

MOVE_TAP = "tap_a_control_on_screen"
MOVE_OFF_SCREEN = "target_not_on_screen"
MOVE_DONE = "step_already_done"
MOVE_OTHER = "needs_typing_scrolling_or_reading"

MOVE_CRITERIA = {
    MOVE_TAP: (
        "A control listed in `screen_elements` carries out `step` with one tap,"
        " e.g. a row, tab, icon, back button or search field with that purpose."
    ),
    MOVE_OFF_SCREEN: (
        "No listed control does `step`: its row or icon is off screen and needs"
        " scrolling or another page."
    ),
    MOVE_DONE: (
        "The screen already is the place `step` leads to, e.g. the step says open"
        " General settings and the General page title is showing. A screen that"
        " merely contains the control is not this."
    ),
    MOVE_OTHER: (
        "`step` is about typing text, scrolling through a list, waiting, or"
        " reading and reporting what is on screen, not about tapping a control."
    ),
}

MOVE_QUESTION = "On the attached iPhone screenshot, what does `step` need next?"
TARGET_QUESTION = (
    "Which element on the attached iPhone screenshot should be tapped to carry out `step`?"
)

_OPTION_RE = re.compile(r"^e(\d+)_")


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")[:40] or "unlabelled"


def element_options(elements: list[dict[str, Any]], width: int, height: int) -> dict[str, str]:
    """Choice options for the indexed elements, keyed ``e<index>_<text>``.

    Option names carry the element text (names are sent to the model; bare
    positions would bias it). Bounds are 0-1000 screen coordinates.
    """
    options: dict[str, str] = {}
    for position, element in enumerate(elements, start=1):
        index = element.get("index", position)
        text = str(element.get("text") or "").strip()
        bounds = element.get("bounds")
        where = ""
        if isinstance(bounds, (list, tuple)) and len(bounds) == 4 and width and height:
            left, top, right, bottom = bounds
            where = (
                f" at [{left * 1000 // width},{top * 1000 // height}]"
                f"[{right * 1000 // width},{bottom * 1000 // height}]"
                " (0-1000 screen coordinates)"
            )
        kind = "Text read from pixels" if element.get("is_ocr") else "Element"
        options[f"e{index}_{_slug(text)}"] = f"{kind} '{text}'{where}"
    return options


def option_index(option: str) -> int | None:
    match = _OPTION_RE.match(option or "")
    return int(match.group(1)) if match else None


def build_request(step: str, options: dict[str, str], screenshot_jpeg_b64: str) -> dict[str, Any]:
    """The System One request body for one Operator turn."""
    return {
        "images": [{"content_type": "image/jpeg", "base64": screenshot_jpeg_b64}],
        "state": {"step": step, "screen_elements": list(options.values())},
        "questions": {
            "move": {"type": "choice", "instructions": MOVE_QUESTION, "criteria": MOVE_CRITERIA},
            "target": {"type": "choice", "instructions": TARGET_QUESTION, "criteria": options},
        },
    }

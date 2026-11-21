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

"""Match a plan step to one on-screen element by its text (UI tree or OCR)."""

from __future__ import annotations

import re
from typing import Any

# A step must start with one of these to be a single tap.
_TAP_VERB_RE = re.compile(
    r"^\s*(open|tap|press|click|select|choose|switch to|go to)\b", re.IGNORECASE
)
# Words that say how to act, not what to act on.
_FILLER = frozenset(
    "open tap press click select choose switch go to the a an on in of for from with"
    " and then button row tab icon app screen page menu item option settings s".split()
)
# Elements covering this much of the screen are containers, not targets.
_CONTAINER_AREA = 0.5
# Element classes (``XCUIElementType`` names) that are tap targets.
_CONTROL_CLASSES = frozenset(
    "Button Cell Icon Link Tab TextField SearchField SecureTextField Switch Toggle"
    " MenuItem SegmentedControl Key".split()
)


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", text.lower()))


def _content_words(text: str) -> set[str]:
    words = _words(text)
    return (words - _FILLER) or (words - {"the", "a", "an", "to", "open", "tap"})


def _area_fraction(element: dict[str, Any], width: int, height: int) -> float:
    bounds = element.get("bounds")
    if not (isinstance(bounds, (list, tuple)) and len(bounds) == 4) or width <= 0 or height <= 0:
        return 0.0
    left, top, right, bottom = bounds
    return max(0, right - left) * max(0, bottom - top) / float(width * height)


def match_step(
    step: str, elements: list[dict[str, Any]], width: int, height: int
) -> dict[str, Any] | None:
    """The single control whose text names ``step``, or ``None``.

    Every word of the element's text must appear in the step, and the element
    must cover the most step words. A tie between elements with different text,
    or the same text in two places, is ambiguous (``None``). The result is a
    control (button, row, icon, field): a matching label taps its row, and
    text with no control around it (a page title) is left to Clef.
    """
    if not _TAP_VERB_RE.match(step):
        return None
    step_words = _content_words(step)
    if not step_words:
        return None
    scored: list[tuple[int, dict[str, Any]]] = []
    for element in elements:
        text = str(element.get("text") or "").strip()
        words = _content_words(text)
        if not words or not words <= _words(step):
            continue
        overlap = len(words & step_words)
        if overlap == 0 or _area_fraction(element, width, height) >= _CONTAINER_AREA:
            continue
        scored.append((overlap, element))
    if not scored:
        return None
    best = max(score for score, _ in scored)
    if best < max(1, len(step_words) - 2):
        # Most of the step is unmatched ("Search the settings for Wi-Fi" vs a
        # "Search" field): that needs judgement, not a word match.
        return None
    winners = [element for score, element in scored if score == best]
    if len({frozenset(_content_words(str(e.get("text") or ""))) for e in winners}) != 1:
        return None
    # Copies of one text are fine only when they are the same control (a row
    # and its label); the same word in two places (a "Search" row and the
    # search field) is ambiguous.
    outer = max(winners, key=lambda e: _area_fraction(e, width, height))
    if not all(_inside(e.get("center"), outer.get("bounds")) for e in winners):
        return None
    # Tap a control, never bare text: a page title that repeats the step
    # ("About" on the About page) means the step may already be done, which
    # is Clef's call. A label inside a row taps the row.
    for element in winners:
        if _is_control(element):
            return element
    return _enclosing_control(winners[0], elements, width, height)


def _is_control(element: dict[str, Any]) -> bool:
    kind = str(element.get("class") or "").split(" ")[0]
    return kind in _CONTROL_CLASSES


def _enclosing_control(
    label: dict[str, Any], elements: list[dict[str, Any]], width: int, height: int
) -> dict[str, Any] | None:
    """The smallest control that contains ``label`` (its row or button)."""
    holders = [
        e
        for e in elements
        if e is not label
        and _is_control(e)
        and _inside(label.get("center"), e.get("bounds"))
        and _area_fraction(e, width, height) < _CONTAINER_AREA
    ]
    return min(holders, key=lambda e: _area_fraction(e, width, height)) if holders else None


def _inside(point: Any, bounds: Any) -> bool:
    if not (isinstance(point, (list, tuple)) and len(point) == 2):
        return False
    if not (isinstance(bounds, (list, tuple)) and len(bounds) == 4):
        return False
    x, y = point
    left, top, right, bottom = bounds
    return left <= x <= right and top <= y <= bottom

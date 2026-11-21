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

"""Settle a tap step in code when the next screen shows its destination.

A plan step such as "Open General settings" is done when a page title named
"General" appears that was not there before the tap. Code can see that in the
element list in milliseconds; asking the LLM to notice it and write ``[x]``
into the plan costs a model round trip per step.
"""

from __future__ import annotations

import re
from typing import Any

from lunatap.agents.fast_path.text_match import (
    _CONTAINER_AREA,
    _TAP_VERB_RE,
    _area_fraction,
    _content_words,
    _words,
)
from lunatap.utils.plan_grammar import STATUS_ACTIVE, STATUS_DONE, STATUS_PENDING, parse_plan

# A step that chains several screens ("Settings > General > About", "open X
# and do Y") is not done when the first of them appears; the LLM settles it.
_CHAIN_RE = re.compile(r">|→|\bthen\b|\band\b", re.IGNORECASE)
# Page titles sit in the top third of the screen: navigation bars near the
# top, an open Home-screen folder's name about a quarter of the way down.
_TITLE_BAND = 0.34
# Above this lies the status bar (clock, battery), never a title.
_STATUS_BAR_BAND = 0.05
_TITLE_CLASSES = frozenset({"NavigationBar", "Application", "Alert", "Sheet"})
_TITLE_TEXT_CLASSES = frozenset(
    {"StaticText", "TextField", "Other", "StaticText [OCR]", "Text [OCR]"}
)


def step_reached(
    step: str,
    tapped_text: str | None,
    before: list[dict[str, Any]],
    after: list[dict[str, Any]],
    width: int,
    height: int,
) -> str | None:
    """The new page title that shows ``step`` reached its screen, or ``None``.

    The title must be new since ``before`` (a title that was already showing
    proves nothing) and must either name the whole step, be (part of) the
    label of the control that was tapped (the folder named "Utilities" opening
    after a tap on the icon labelled "Utilities folder"), or contain that
    label and a word of the step ("My Jobs" after tapping the "Jobs" tab).
    """
    if not step or not _TAP_VERB_RE.match(step) or _CHAIN_RE.search(step):
        return None
    step_words = _content_words(step)
    if not step_words:
        return None
    tapped_words = _content_words(tapped_text or "") if tapped_text else set()
    # Only titles count as 'already showing': a row label lower on the
    # previous screen may carry the same text as the next page's title.
    seen_before = {_identity(e) for e in before if _is_title(e, width, height)}
    for element in after:
        if _identity(element) in seen_before or not _is_title(element, width, height):
            continue
        text = str(element.get("text") or "").strip()
        words = _content_words(text)
        if not words:
            continue
        if words <= _words(step) and (
            step_words <= words or (tapped_words and words <= tapped_words)
        ):
            return text
        # The title carries the tapped label plus words of its own: "My Jobs"
        # after tapping the "Jobs" tab for "Open the Jobs tab".
        if tapped_words and tapped_words <= words and words & step_words:
            return text
    return None


def page_title(elements: list[dict[str, Any]], width: int, height: int) -> str | None:
    """The screen's title: an alert on top, else the navigation bar, else the first title-band text."""
    titled = [
        e for e in elements if _is_title(e, width, height) and str(e.get("text") or "").strip()
    ]
    for wanted in ("Alert", "Sheet", "NavigationBar"):
        for element in titled:
            if element.get("class") == wanted:
                return str(element["text"]).strip()
    # Tree text first. OCR misreads text half under a floating button
    # ("3irth Mark" for "Birth Mark"), so an OCR title is a last resort and
    # says so.
    for element in titled:
        if element.get("class") != "Application" and not _is_ocr(element):
            return str(element["text"]).strip()
    for element in titled:
        if _is_ocr(element):
            return f"{str(element['text']).strip()} (OCR)"
    return None


def _is_ocr(element: dict[str, Any]) -> bool:
    return bool(element.get("is_ocr")) or "[OCR]" in str(element.get("class") or "")


def settle_leaf(content: str, leaf_text: str) -> str | None:
    """``content`` with the active leaf ``leaf_text`` marked done, or ``None``.

    The next pending sibling, if any, is opened so the ledger stays valid
    without a model turn. Milestones are never touched here: closing one
    triggers checkpoints, which stay the LLM's call.
    """
    snapshot = parse_plan(content)
    milestone = snapshot.active_milestone()
    if milestone is None:
        return None
    leaf = snapshot.active_leaf(milestone)
    if leaf is None or leaf.text.strip() != leaf_text.strip():
        return None
    lines = content.splitlines(keepends=True)
    lines[leaf.line_no] = _with_status(lines[leaf.line_no], STATUS_DONE)
    for sibling in snapshot.children_of(milestone):
        if sibling.line_no > leaf.line_no and sibling.indent == leaf.indent:
            if sibling.is_pending:
                lines[sibling.line_no] = _with_status(lines[sibling.line_no], STATUS_ACTIVE)
            break
    return "".join(lines)


def _with_status(line: str, status: str) -> str:
    return re.sub(
        r"\[[" + re.escape(STATUS_PENDING + STATUS_ACTIVE) + r"]\]", f"[{status}]", line, count=1
    )


def _identity(element: dict[str, Any]) -> tuple[str, str]:
    return (str(element.get("text") or "").strip().lower(), str(element.get("class") or ""))


def _is_title(element: dict[str, Any], width: int, height: int) -> bool:
    kind = str(element.get("class") or "")
    if kind in _TITLE_CLASSES:
        return True
    if kind not in _TITLE_TEXT_CLASSES:
        return False
    # A glyph ("<" read by OCR off the back button) is never a title.
    if not any(ch.isalnum() for ch in str(element.get("text") or "")):
        return False
    center = element.get("center")
    if not (isinstance(center, (list, tuple)) and len(center) == 2 and height > 0):
        return False
    return (
        height * _STATUS_BAR_BAND <= center[1] < height * _TITLE_BAND
        and _area_fraction(element, width, height) < _CONTAINER_AREA
    )

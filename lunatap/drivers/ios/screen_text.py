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

"""Check a tap target against the text a person would actually see there.

The accessibility tree can list an element that is drawn underneath something
else (a floating bar, a sheet, a banner). Reading the screenshot catches every
kind of covering, not only the bar types ``wda.OVERLAY_ELEMENT_TYPES`` knows.

Text recognition uses Apple's Vision framework through PyObjC, which ships with
macOS and needs no model download. On hosts without it, ``available()`` is
False and callers should skip the check.
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import io
import os
import re
from collections import OrderedDict
from typing import Any
from dataclasses import dataclass
from enum import Enum

from PIL import Image

from third_party.mobile_use.utils.logger import get_logger

logger = get_logger(__name__)

#: ``LUNATAP_LOCAL_OCR=0`` turns Apple Vision off (OCR and the tap check).
LOCAL_OCR_ENV = "LUNATAP_LOCAL_OCR"

# Points are image pixels with the origin at the top left.
PixelBox = tuple[int, int, int, int]  # left, top, right, bottom


@dataclass(frozen=True)
class TextBox:
    text: str
    box: PixelBox
    confidence: float


class TapVerdict(Enum):
    MATCH = "match"  # the label's text is visible at the tap point
    MISMATCH = "mismatch"  # different text is visible there; the label is not
    UNKNOWN = "unknown"  # nothing to compare (icon, no label, no text nearby)


@dataclass(frozen=True)
class TapCheck:
    verdict: TapVerdict
    seen: tuple[str, ...] = ()  # text found near the tap point

    def describe(self, label: str) -> str:
        if self.verdict is TapVerdict.MISMATCH:
            return f"'{label}' is not visible at the tap point; the screen shows {list(self.seen)}"
        return self.verdict.value


# Elements whose label names a region rather than text drawn on screen.
CONTAINER_KINDS = frozenset(
    {
        "Application",
        "Window",
        "Other",
        "Group",
        "Table",
        "CollectionView",
        "ScrollView",
        "Toolbar",
        "TabBar",
        "NavigationBar",
        "Keyboard",
    }
)
MAX_CHECKED_SCREEN_FRACTION = 0.25


def worth_checking(kind: str, box: PixelBox | None, screen: tuple[int, int]) -> bool:
    """Only leaf controls have a label we can expect to read on screen."""
    if kind in CONTAINER_KINDS or box is None:
        return False
    left, top, right, bottom = box
    area = max(0, right - left) * max(0, bottom - top)
    return area <= MAX_CHECKED_SCREEN_FRACTION * screen[0] * screen[1]


def available() -> bool:
    """True when Apple's Vision framework bindings (PyObjC) are installed."""
    return importlib.util.find_spec("Vision") is not None


def enabled() -> bool:
    """Vision is installed and not switched off with ``LUNATAP_LOCAL_OCR=0``."""
    if os.environ.get(LOCAL_OCR_ENV, "1").strip().lower() in ("0", "false", "no", "off"):
        return False
    return available()


_RECOGNIZED: OrderedDict[str, list[TextBox]] = OrderedDict()
_RECOGNIZED_LIMIT = 8


def recognize_text_cached(image: bytes) -> list[TextBox]:
    """``recognize_text`` for a screenshot, reusing results for the same bytes."""
    key = hashlib.sha256(image).hexdigest()
    if key in _RECOGNIZED:
        _RECOGNIZED.move_to_end(key)
        return _RECOGNIZED[key]
    boxes = recognize_text(image)
    _RECOGNIZED[key] = boxes
    while len(_RECOGNIZED) > _RECOGNIZED_LIMIT:
        _RECOGNIZED.popitem(last=False)
    return boxes


def check_element_tap(
    image: bytes,
    *,
    label: str | None,
    kind: str | None,
    point: tuple[int, int],
    bounds: PixelBox | None,
) -> TapCheck:
    """Vision's verdict on tapping a listed element in this screenshot.

    ``point`` and ``bounds`` are screenshot pixels. Anything that prevents a
    confident answer (Vision off, a container, a decode or OCR error) is
    UNKNOWN, so callers only act on a MISMATCH.
    """
    if not label or not enabled():
        return TapCheck(TapVerdict.UNKNOWN)
    try:
        with Image.open(io.BytesIO(image)) as decoded:
            screen = decoded.size
        if not worth_checking((kind or "").removeprefix("XCUIElementType"), bounds, screen):
            return TapCheck(TapVerdict.UNKNOWN)
        boxes = recognize_text_cached(image)
    except (OSError, ValueError, RuntimeError) as error:
        logger.debug(f"Vision tap check skipped: {error}")
        return TapCheck(TapVerdict.UNKNOWN)
    return check_tap_target(boxes, label, point, bounds)


def parse_box(raw: Any) -> PixelBox | None:
    """Bounds as ``[l, t, r, b]``, ``{"left": ...}`` or ``"[l,t][r,b]"``."""
    if isinstance(raw, dict):
        try:
            return (int(raw["left"]), int(raw["top"]), int(raw["right"]), int(raw["bottom"]))
        except (KeyError, TypeError, ValueError):
            return None
    if isinstance(raw, str):
        numbers = re.findall(r"-?\d+", raw)
        raw = numbers if len(numbers) == 4 else None
    if isinstance(raw, (list, tuple)) and len(raw) == 4:
        try:
            left, top, right, bottom = (int(float(v)) for v in raw)
        except (TypeError, ValueError):
            return None
        return (left, top, right, bottom)
    return None


def recognize_text(png: bytes, *, fast: bool = False) -> list[TextBox]:
    """Return the text lines Vision finds in a PNG/JPEG screenshot."""
    # PyObjC builds these modules' attributes at runtime, so type checkers
    # cannot see them; treat them as untyped.
    Quartz: Any = importlib.import_module("Quartz")
    Vision: Any = importlib.import_module("Vision")
    Foundation: Any = importlib.import_module("Foundation")

    data = Foundation.NSData.dataWithBytes_length_(png, len(png))
    source = Quartz.CGImageSourceCreateWithData(data, None)
    image = Quartz.CGImageSourceCreateImageAtIndex(source, 0, None)
    if image is None:
        raise ValueError("screenshot could not be decoded")
    width = Quartz.CGImageGetWidth(image)
    height = Quartz.CGImageGetHeight(image)

    request = Vision.VNRecognizeTextRequest.alloc().init()
    request.setRecognitionLevel_(
        Vision.VNRequestTextRecognitionLevelFast
        if fast
        else Vision.VNRequestTextRecognitionLevelAccurate
    )
    request.setUsesLanguageCorrection_(False)  # UI labels are not prose
    handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(image, None)
    ok, error = handler.performRequests_error_([request], None)
    if not ok:
        raise RuntimeError(f"Vision text recognition failed: {error}")

    boxes: list[TextBox] = []
    for observation in request.results() or []:
        candidate = observation.topCandidates_(1)
        if not candidate:
            continue
        # Vision boxes are normalized with the origin at the bottom left.
        rect = observation.boundingBox()
        left = rect.origin.x * width
        right = (rect.origin.x + rect.size.width) * width
        top = (1.0 - rect.origin.y - rect.size.height) * height
        bottom = (1.0 - rect.origin.y) * height
        boxes.append(
            TextBox(
                text=str(candidate[0].string()),
                box=(round(left), round(top), round(right), round(bottom)),
                confidence=float(candidate[0].confidence()),
            )
        )
    return boxes


def _words(text: str) -> list[str]:
    return re.findall(r"\w+", text.casefold())


def _label_visible_in(label: str, seen: str) -> bool:
    """True when the label's words appear in the seen text (or vice versa).

    Lenient on purpose: OCR drops punctuation and may merge a label with a
    neighbouring value ("General >" or "Wi-Fi  Home"), and long labels get
    truncated on screen ("Notifications & Fo…").
    """
    label_words, seen_words = _words(label), _words(seen)
    if not label_words or not seen_words:
        return False
    joined_seen = " ".join(seen_words)
    joined_label = " ".join(label_words)
    if joined_label in joined_seen or joined_seen in joined_label:
        return True
    # Most of the label's words are visible (truncation, OCR slips).
    hits = sum(1 for word in label_words if word in seen_words)
    return hits / len(label_words) >= 0.6


def check_tap_target(
    boxes: list[TextBox],
    label: str | None,
    point: tuple[int, int],
    element_box: PixelBox | None = None,
    *,
    slack: int = 24,
) -> TapCheck:
    """Compare an element's label with the text visible where it will be tapped.

    Looks at text whose box contains the tap point, plus text on the same row
    inside the element's own bounds (a cell's label usually sits left of its
    center). ``slack`` widens the tap point vertically, in pixels.
    """
    if not label or not _words(label):
        return TapCheck(TapVerdict.UNKNOWN)
    x, y = point
    near: list[TextBox] = []
    for text_box in boxes:
        left, top, right, bottom = text_box.box
        same_row = top - slack <= y <= bottom + slack
        if not same_row:
            continue
        under_point = left - slack <= x <= right + slack
        inside_element = element_box is not None and (
            left < element_box[2] and right > element_box[0]
        )
        if under_point or inside_element:
            near.append(text_box)
    if not near:
        return TapCheck(TapVerdict.UNKNOWN)
    seen = tuple(text_box.text for text_box in near)
    if any(_label_visible_in(label, text) for text in seen):
        return TapCheck(TapVerdict.MATCH, seen)
    return TapCheck(TapVerdict.MISMATCH, seen)

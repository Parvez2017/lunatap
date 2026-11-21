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

"""Find map pins and other markers the accessibility tree does not list.

A map with 162 pins exposes three accessibility elements (compass, logo,
attribution), so pins are found in the screenshot, locally, with no model.

Pins are found by shape, not only colour (QA item 26: grey pins with a white
ring on satellite imagery were all missed, and fields were reported as
pins). Candidates come from two places: circles (Hough), which catches grey
and white pins, and vivid compact blobs, which catches coloured teardrop
pins. Each candidate is then checked as a pin: a solid inside that stands
out from what is around it. Satellite texture fails that check; a field
has no edge against the next field. Touching pins are reported as one
cluster. The status bar, labelled controls and anything outside the tree's
Map frame are ignored. Each marker carries a confidence, and a scan says when
the background is imagery, where the confidence deserves less trust.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import cv2
import numpy as np

from third_party.mobile_use.utils.logger import get_logger

logger = get_logger(__name__)

#: Downscale so the longest side is this many pixels before detection.
_WORK_SIDE = 1000
#: Vivid-colour thresholds (OpenCV HSV: H 0-179, S and V 0-255).
_MIN_SATURATION = 110
_MIN_VALUE = 90
#: Pin radius as a fraction of the image width.
_MIN_RADIUS = 0.007
_MAX_RADIUS = 0.032
#: The status bar band (clock, battery) as a fraction of the height.
_STATUS_BAR = 0.055
#: A vivid blob must be roughly round: fill of its box, and box aspect.
_MIN_FILL = 0.45
_MAX_ASPECT = 2.2
#: Accepted pin score; stricter over imagery, where texture makes circles.
_MIN_SCORE = 0.35
_MIN_SCORE_IMAGERY = 0.4
#: Under this share of flat pixels the background is photo-like (satellite
#: imagery). Vector maps are mostly flat fills (Apple Maps: 42%); imagery
#: almost never is (a field scene: 5%).
_IMAGERY_FLAT_SHARE = 0.15
#: Tile side (work pixels) for judging texture, and the share of textured
#: tiles above which the scan reports an imagery background.
_TILE = 48
_IMAGERY_TILES = 0.25
#: On imagery a pin needs a round edge: the share of edge strength around
#: it that runs along the circle. Pins measured 0.88-1.0 (Apple satellite
#: cafe pins, Flight's grey and orange pins); Flight satellite fields at most
#: 0.83. A pin straddling a field's edge can fall below it (a known miss).
_IMAGERY_RADIAL_EDGE = 0.855
#: Grey pins (Flight): near-colourless and light, with a ring brighter than
#: the inside (lone pins measured +33, roofs -6 and -63).
_NEUTRAL_MAX_SATURATION = 32
_NEUTRAL_VALUE = (120, 255)  # white rings (other apps) as well as Flight's light grey
_NEUTRAL_MIN_RING = 10.0
#: A pin found at the learned radius must be mostly colourless (a partly
#: see-through pin shows a line drawn under it, live: the job boundary).
_NEUTRAL_DISC_SHARE = 0.6
#: Lone-pin score (0.5 + rim/60) of a clearly ring-shaped pin: rim +25 or more.
_CLEAR_PIN_SCORE = 0.9
#: Pins closer than this many radii apart touch; this many make a cluster.
_TOUCH = 1.35
CLUSTER_MIN = 3
MAX_MARKERS = 120

#: Tree classes whose own icons must not be reported as markers.
_CONTROL_CLASSES = frozenset(
    {
        "Button",
        "Cell",
        "TabBar",
        "NavigationBar",
        "Toolbar",
        "Keyboard",
        "TextField",
        "SearchField",
        "Switch",
        "SegmentedControl",
        "Slider",
        "Icon",
        "AlertButton",
    }
)
_HUE_NAMES = (
    (10, "red"),
    (22, "orange"),
    (34, "yellow"),
    (80, "green"),
    (100, "teal"),
    (130, "blue"),
    (155, "purple"),
    (170, "pink"),
    (180, "red"),
)

Rect = tuple[int, int, int, int]


@dataclass(frozen=True)
class Marker:
    cx: int
    cy: int
    left: int
    top: int
    right: int
    bottom: int
    color: str
    area: int  # full-resolution pixels
    confidence: float = 1.0
    count: int = 1  # pins in this marker; above 1 it is a cluster
    kind: str = "pin"  # pin (a plain disc), icon (a glyph inside: POI, count), cluster
    at_least: bool = False  # the count is a floor: pins hidden under others


@dataclass(frozen=True)
class MarkerScan:
    markers: list[Marker] = field(default_factory=list)
    imagery: bool = False  # photo-like background (satellite): trust confidence less

    @property
    def pins(self) -> int:
        return sum(m.count for m in self.markers)


@dataclass
class _Candidate:
    x: float  # work-scale centre and radius
    y: float
    r: float
    score: float = 0.0
    color: str = ""
    extra: int = 0  # pins hidden in a pack beyond those found one by one
    glyph: bool = False  # a second, contrasting colour inside (a POI symbol, a number)
    packed: bool = False  # found inside an overlapping pack


def _color_name(hsv_pixels: np.ndarray) -> str:
    """The colour of a pin's inside: its saturated pixels' hue, else grey, white or black."""
    vivid = hsv_pixels[hsv_pixels[:, 1] >= 60]
    if len(vivid) < 0.3 * len(hsv_pixels):
        val = float(np.median(hsv_pixels[:, 2]))
        if val < 70:
            return "black"
        return "white" if val > 215 else "grey"
    hue = float(np.median(vivid[:, 0]))
    for limit, name in _HUE_NAMES:
        if hue < limit:
            return name
    return "red"


def _textured_tiles(gray: np.ndarray, valid: np.ndarray) -> np.ndarray:
    """Per-pixel map of photo-like texture, judged per tile.

    A screen often mixes a satellite map with flat UI (a results sheet), so
    texture is decided locally: a tile with few flat pixels is imagery.
    """
    flat = (np.abs(cv2.Laplacian(gray, cv2.CV_32F)) < 2).astype(np.float32)
    textured = np.zeros(gray.shape, bool)
    step = max(16, _TILE)
    for y in range(0, gray.shape[0], step):
        for x in range(0, gray.shape[1], step):
            tile_valid = valid[y : y + step, x : x + step] > 0
            if tile_valid.mean() < 0.1:  # judge edge tiles on their in-map part
                continue
            share = float(flat[y : y + step, x : x + step][tile_valid].mean())
            textured[y : y + step, x : x + step] = share < _IMAGERY_FLAT_SHARE
    return textured


def _circle_candidates(gray: np.ndarray, min_r: int, max_r: int) -> list[_Candidate]:
    circles = cv2.HoughCircles(
        cv2.GaussianBlur(gray, (3, 3), 0),
        cv2.HOUGH_GRADIENT_ALT,
        dp=1.5,
        minDist=max(3, int(min_r * 1.5)),
        param1=150,
        param2=0.75,
        minRadius=min_r,
        maxRadius=max_r,
    )
    if circles is None:
        return []
    return [_Candidate(float(x), float(y), float(r)) for x, y, r in circles[0]]


def _vivid_candidates(hsv: np.ndarray, min_r: int, max_r: int) -> list[_Candidate]:
    mask = cv2.inRange(hsv, (0, _MIN_SATURATION, _MIN_VALUE), (179, 255, 255))
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    count, _, stats, centroids = cv2.connectedComponentsWithStats(mask, connectivity=8)
    found = []
    for index in range(1, count):
        _, _, w, h, area = (int(v) for v in stats[index])
        r = (area / np.pi) ** 0.5
        if not (min_r * 0.6 <= r <= max_r * 1.3):
            continue  # specks, and panels or polygon pieces much bigger than a pin
        if max(w, h) / float(max(1, min(w, h))) > _MAX_ASPECT or area / float(w * h) < _MIN_FILL:
            continue  # long thin outlines and ragged shapes
        cx, cy = centroids[index]
        found.append(_Candidate(float(cx), float(cy), max(r, min(w, h) / 2.0)))
    return found


def _ring(shape: tuple[int, int], c: _Candidate, inner: float, outer: float) -> np.ndarray:
    yy, xx = np.ogrid[: shape[0], : shape[1]]
    d2 = (xx - c.x) ** 2 + (yy - c.y) ** 2
    return (d2 >= (inner * c.r) ** 2) & (d2 <= (outer * c.r) ** 2)


def _score(lab: np.ndarray, hsv: np.ndarray, grad: np.ndarray, c: _Candidate) -> float:
    """How much this looks like a pin: one solid colour with a crisp round edge that stands out."""
    pad = int(c.r * 2.2) + 2
    y0, y1 = max(0, int(c.y) - pad), min(lab.shape[0], int(c.y) + pad + 1)
    x0, x1 = max(0, int(c.x) - pad), min(lab.shape[1], int(c.x) + pad + 1)
    local = _Candidate(c.x - x0, c.y - y0, c.r)
    patch = lab[y0:y1, x0:x1].astype(np.float32)
    shape = patch.shape[:2]
    disc = _ring(shape, local, 0.0, 0.9)
    edge = _ring(shape, local, 0.8, 1.2)
    around = _ring(shape, local, 1.45, 2.1)
    if disc.sum() < 6 or around.sum() < 6 or not edge.any():
        return 0.0
    pixels_in = patch[disc]
    mean_out = patch[around].mean(axis=0)
    mean_edge = patch[edge].mean(axis=0)
    # A pin is drawn in one or two flat colours (fill, plus a glyph such as
    # a cup or a count); two colour groups cover it. Texture varies
    # continuously, so most of a field's pixels sit far from either group.
    criteria = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 10, 1.0)
    _, labels, centres = cv2.kmeans(pixels_in, 2, None, criteria, 1, cv2.KMEANS_PP_CENTERS)
    labels = labels.ravel()
    distance = np.linalg.norm(pixels_in - centres[labels], axis=1)
    dominance = float((distance < 18.0).mean())
    shares = np.bincount(labels, minlength=2) / float(len(labels))
    c.glyph = bool(shares.min() >= 0.12 and float(np.linalg.norm(centres[0] - centres[1])) >= 45.0)
    contrast = max(
        [float(np.linalg.norm(centres[k] - mean_out)) for k in range(2) if shares[k] >= 0.25]
        + [float(np.linalg.norm(mean_edge - mean_out))]
    )
    # Pins are drawn crisply over the map; a field's border is soft.
    sharp = min(1.0, float(grad[y0:y1, x0:x1][edge].mean()) / 60.0)
    busy_around = min(1.0, float(patch[around][:, 0].std()) / 45.0)
    c.color = _color_name(hsv[y0:y1, x0:x1][disc])
    return (
        min(1.0, contrast / 55.0)
        * min(1.0, dominance / 0.6)
        * (0.3 + 0.7 * sharp)
        * (1.0 - 0.4 * busy_around)
    )


def _radial_edge(light: np.ndarray, cx: float, cy: float, r: float) -> float:
    """Share of the edge strength near radius ``r`` that runs along the circle.

    A drawn pin's outline is a circle, so the brightness changes across it
    point away from the centre. A field's border is a ragged line, which
    mostly does not. Full-resolution lightness, full-resolution units.
    """
    pad = int(r * 1.4) + 2
    y0, y1 = max(0, int(cy) - pad), min(light.shape[0], int(cy) + pad + 1)
    x0, x1 = max(0, int(cx) - pad), min(light.shape[1], int(cx) + pad + 1)
    patch = light[y0:y1, x0:x1]
    if patch.size < 16:
        return 0.0
    gx = cv2.Sobel(patch, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(patch, cv2.CV_32F, 0, 1, ksize=3)
    yy, xx = np.mgrid[y0:y1, x0:x1]
    dx, dy = xx - cx, yy - cy
    distance = np.hypot(dx, dy)
    band = (distance >= 0.8 * r) & (distance <= 1.2 * r) & (distance > 0)
    magnitude = np.hypot(gx, gy)[band]
    if magnitude.sum() <= 1e-6:
        return 0.0
    along = np.abs(gx[band] * dx[band] + gy[band] * dy[band]) / (distance[band] * magnitude + 1e-6)
    return float((along * magnitude).sum() / magnitude.sum())


def _ring_contrast(value: np.ndarray, cx: float, cy: float, r: float) -> float:
    """How much brighter the pin's rim is than its inside (full resolution)."""
    pad = int(r) + 2
    y0, y1 = max(0, int(cy) - pad), min(value.shape[0], int(cy) + pad + 1)
    x0, x1 = max(0, int(cx) - pad), min(value.shape[1], int(cx) + pad + 1)
    yy, xx = np.ogrid[y0:y1, x0:x1]
    distance = np.sqrt((xx - cx) ** 2 + (yy - cy) ** 2)
    patch = value[y0:y1, x0:x1]
    rim = patch[(distance >= 0.82 * r) & (distance <= 0.98 * r)]
    inside = patch[distance <= 0.7 * r]
    if rim.size == 0 or inside.size == 0:
        return 0.0
    return float(rim.mean() - inside.mean())


def _pins_at_learned_radius(
    image: np.ndarray,
    hsv_full: np.ndarray,
    valid_full: np.ndarray,
    value: np.ndarray,
    lone: list[_Candidate],
    scale: float,
) -> list[_Candidate]:
    """More lone pins, by circle detection at the radius the found ones have.

    The mask route loses a pin that something shows through (the job
    boundary line under a partly see-through pin). Knowing the radius, a
    circle there is a pin when its disc is mostly colourless and its rim
    brighter than its inside, like the pins already found.
    """
    pin_r = float(np.median([c.r for c in lone])) / scale
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    circles = cv2.HoughCircles(
        cv2.GaussianBlur(gray, (3, 3), 0),
        cv2.HOUGH_GRADIENT,
        dp=1,
        minDist=max(3, int(pin_r)),
        param1=80,
        param2=16,
        minRadius=max(2, int(0.85 * pin_r)),
        maxRadius=int(1.15 * pin_r) + 1,
    )
    if circles is None:
        return []
    colourless = hsv_full[:, :, 1] < _NEUTRAL_MAX_SATURATION
    known = [(c.x / scale, c.y / scale) for c in lone]
    found = []
    for cx, cy, _ in circles[0]:
        x, y = int(cx), int(cy)
        if (
            not (0 <= y < valid_full.shape[0] and 0 <= x < valid_full.shape[1])
            or not valid_full[y, x]
        ):
            continue
        if any(np.hypot(cx - kx, cy - ky) < pin_r for kx, ky in known):
            continue
        y0, y1 = max(0, int(cy - pin_r)), min(colourless.shape[0], int(cy + pin_r) + 1)
        x0, x1 = max(0, int(cx - pin_r)), min(colourless.shape[1], int(cx + pin_r) + 1)
        yy, xx = np.ogrid[y0:y1, x0:x1]
        disc = (xx - cx) ** 2 + (yy - cy) ** 2 <= (0.95 * pin_r) ** 2
        if float(colourless[y0:y1, x0:x1][disc].mean()) < _NEUTRAL_DISC_SHARE:
            continue
        ring = _ring_contrast(value, float(cx), float(cy), pin_r)
        if ring < _NEUTRAL_MIN_RING:
            continue
        known.append((cx, cy))
        found.append(_Candidate(cx * scale, cy * scale, pin_r * scale, min(1.0, 0.4 + ring / 60.0)))
    return found


def _neutral_pins(
    image: np.ndarray,
    hsv_full: np.ndarray,
    valid_full: np.ndarray,
    scale: float,
) -> list[_Candidate]:
    """Grey pins with a light ring, in work coordinates; packed ones counted by circle.

    The colour route never sees them (they are not vivid) and circle
    detection over the whole map cannot separate a pack of overlapping
    ones. Here: a near-colourless mask, opened at pin scale so streets and
    label text drop out; round pin-sized parts with a bright rim are lone
    pins and give the pin radius; bigger parts are packs, whose pins are
    found by circle detection at that radius.
    """
    width = image.shape[1]
    sat, val = hsv_full[:, :, 1], hsv_full[:, :, 2]
    mask = (
        (sat < _NEUTRAL_MAX_SATURATION)
        & (val >= _NEUTRAL_VALUE[0])
        & (val <= _NEUTRAL_VALUE[1])
        & (valid_full > 0)
    ).astype(np.uint8)
    r_open = max(3, int(round(width * 0.0075)))
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * r_open + 1, 2 * r_open + 1))
    opened = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
    count, labels, stats, centroids = cv2.connectedComponentsWithStats(opened, connectivity=8)
    min_r, max_r = width * _MIN_RADIUS, width * _MAX_RADIUS
    value = val.astype(np.float32)

    lone: list[_Candidate] = []
    big: list[int] = []
    for index in range(1, count):
        x, y, w, h, area = (int(v) for v in stats[index])
        contours, _ = cv2.findContours(
            (labels[y : y + h, x : x + w] == index).astype(np.uint8),
            cv2.RETR_EXTERNAL,
            cv2.CHAIN_APPROX_SIMPLE,
        )
        (_, _), enclosing = cv2.minEnclosingCircle(contours[0])
        radius = (area / np.pi) ** 0.5
        roundness = area / (np.pi * enclosing * enclosing + 1e-6)
        cx, cy = (float(v) for v in centroids[index])
        if min_r <= radius <= max_r and roundness >= 0.8:
            ring = _ring_contrast(value, cx, cy, radius)
            if ring >= _NEUTRAL_MIN_RING:
                lone.append(
                    _Candidate(cx * scale, cy * scale, radius * scale, min(1.0, 0.5 + ring / 60.0))
                )
        elif radius > max_r * 0.9 or (radius > min_r and roundness < 0.8):
            big.append(index)
    # Only a clear pin style teaches the radius: two lone pins with a strong
    # rim (Flight's measure +33). Weak look-alikes on a street map do not.
    clear = [c for c in lone if c.score >= _CLEAR_PIN_SCORE]
    if len(clear) >= 2:
        lone += _pins_at_learned_radius(image, hsv_full, valid_full, value, clear, scale)
    if not big or not lone:
        # Packs are only looked for once a lone pin has shown what the pins
        # look like: light-grey blocks on a street map are big and
        # colourless too, and circles can be found in anything that big.
        return lone
    pin_r = float(np.median([c.r for c in lone])) / scale
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    packed: list[_Candidate] = []
    for index in big:
        x, y, w, h, area = (int(v) for v in stats[index])
        if area < 2.0 * np.pi * pin_r * pin_r:
            continue  # a blob a little bigger than a pin, not a pack
        pad = int(pin_r * 1.5)
        x0, y0 = max(0, x - pad), max(0, y - pad)
        x1, y1 = min(image.shape[1], x + w + pad), min(image.shape[0], y + h + pad)
        circles = cv2.HoughCircles(
            cv2.GaussianBlur(gray[y0:y1, x0:x1], (3, 3), 0),
            cv2.HOUGH_GRADIENT,
            dp=1,
            minDist=max(3, int(0.7 * pin_r)),
            param1=80,
            param2=14,
            minRadius=max(2, int(0.8 * pin_r)),
            maxRadius=int(1.2 * pin_r) + 1,
        )
        found = (
            []
            if circles is None
            else [
                (x0 + float(cx), y0 + float(cy))
                for cx, cy, _ in circles[0]
                if labels[min(y1 - 1, y0 + int(cy)), min(x1 - 1, x0 + int(cx))] == index
                and _ring_contrast(value, x0 + float(cx), y0 + float(cy), pin_r) > -8
            ]
        )
        if not found:
            continue  # nothing pin-like in it
        by_area = int(round(area / (np.pi * pin_r * pin_r)))
        part = [
            _Candidate(fx * scale, fy * scale, pin_r * scale, 0.6, packed=True) for fx, fy in found
        ]
        part[0].extra = max(0, by_area - len(found))
        packed.extend(part)
    return lone + packed


def _merge(candidates: list[_Candidate]) -> list[_Candidate]:
    """One candidate per pin: overlapping detections keep the best-scoring one."""
    kept: list[_Candidate] = []
    for cand in sorted(candidates, key=lambda c: -c.score):
        if all(np.hypot(cand.x - k.x, cand.y - k.y) > 0.7 * max(cand.r, k.r) for k in kept):
            kept.append(cand)
    return kept


def _clusters(pins: list[_Candidate]) -> list[list[_Candidate]]:
    parent = list(range(len(pins)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i, a in enumerate(pins):
        for j in range(i + 1, len(pins)):
            b = pins[j]
            if np.hypot(a.x - b.x, a.y - b.y) <= _TOUCH * (a.r + b.r):
                parent[find(i)] = find(j)
    groups: dict[int, list[_Candidate]] = {}
    for i, pin in enumerate(pins):
        groups.setdefault(find(i), []).append(pin)
    return list(groups.values())


def _join_clusters(groups: list[list[_Candidate]]) -> list[list[_Candidate]]:
    """Clusters whose areas overlap are one: a missed pin splits a packed cluster."""

    def box(group: list[_Candidate]) -> tuple[float, float, float, float]:
        r = 2.0 * max(c.r for c in group)  # a pin of slack: a missed pin leaves a gap
        return (
            min(c.x for c in group) - r,
            min(c.y for c in group) - r,
            max(c.x for c in group) + r,
            max(c.y for c in group) + r,
        )

    big = [g for g in groups if len(g) >= CLUSTER_MIN]
    small = [g for g in groups if len(g) < CLUSTER_MIN]
    merged = True
    while merged:
        merged = False
        for i in range(len(big)):
            for j in range(i + 1, len(big)):
                a, b = box(big[i]), box(big[j])
                if a[0] <= b[2] and b[0] <= a[2] and a[1] <= b[3] and b[1] <= a[3]:
                    big[i] = big[i] + big.pop(j)
                    merged = True
                    break
            if merged:
                break
    # A lone detection inside a cluster's area is one of its pins.
    loose = []
    for group in small:
        for pin in group:
            home = next(
                (
                    g
                    for g in big
                    if box(g)[0] <= pin.x <= box(g)[2] and box(g)[1] <= pin.y <= box(g)[3]
                ),
                None,
            )
            if home is not None:
                home.append(pin)
            else:
                loose.append([pin])
    return big + loose


def _inside(point: tuple[int, int], rect: Rect) -> bool:
    return rect[0] <= point[0] <= rect[2] and rect[1] <= point[1] <= rect[3]


def scan_markers(
    png_bytes: bytes,
    *,
    exclude: list[Rect] | None = None,
    text: list[Rect] | None = None,
    region: Rect | None = None,
    max_markers: int = MAX_MARKERS,
) -> MarkerScan:
    """Pins in the screenshot, as markers and clusters, top to bottom then left to right.

    ``exclude`` lists pixel rects that are not map (labelled controls);
    ``text`` lists text boxes from the tree and OCR: a single marker on text
    is a glyph (a "0", the title), but a cluster may run under a map label,
    so clusters are kept. ``region`` is the map's own frame, when known.
    """
    image = cv2.imdecode(np.frombuffer(png_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        return MarkerScan()
    height, width = image.shape[:2]
    scale = min(1.0, _WORK_SIDE / float(max(width, height)))
    work = (
        cv2.resize(image, (int(width * scale), int(height * scale)), interpolation=cv2.INTER_AREA)
        if scale < 1.0
        else image
    )
    gray = cv2.cvtColor(work, cv2.COLOR_BGR2GRAY)
    hsv = cv2.cvtColor(work, cv2.COLOR_BGR2HSV)
    lab = cv2.cvtColor(work, cv2.COLOR_BGR2LAB)
    light = cv2.cvtColor(image, cv2.COLOR_BGR2LAB)[:, :, 0].astype(np.float32)
    grad = cv2.magnitude(
        cv2.Sobel(lab[:, :, 0], cv2.CV_32F, 1, 0, ksize=3),
        cv2.Sobel(lab[:, :, 0], cv2.CV_32F, 0, 1, ksize=3),
    )

    valid = np.zeros(gray.shape, np.uint8)
    top = int(height * _STATUS_BAR)
    if region is not None:
        left, rtop, right, bottom = region
        top = max(top, rtop)
        valid[int(top * scale) : int(bottom * scale), int(left * scale) : int(right * scale)] = 1
    else:
        valid[int(top * scale) :, :] = 1
    textured = _textured_tiles(gray, valid)
    textured_share = float(textured[valid > 0].mean()) if valid.any() else 0.0
    imagery = textured_share >= _IMAGERY_TILES
    logger.debug(f"Marker scan: {textured_share:.0%} of the map looks like imagery")

    min_r = max(2, int(round(work.shape[1] * _MIN_RADIUS)))
    max_r = max(min_r + 2, int(round(work.shape[1] * _MAX_RADIUS)))
    candidates = _circle_candidates(gray, min_r, max_r) + _vivid_candidates(hsv, min_r, max_r)
    pins = []
    for cand in candidates:
        x, y = int(cand.x), int(cand.y)
        if not (0 <= y < valid.shape[0] and 0 <= x < valid.shape[1]) or not valid[y, x]:
            continue
        full = (int(round(cand.x / scale)), int(round(cand.y / scale)))
        if any(_inside(full, rect) for rect in exclude or []):
            continue
        cand.score = _score(lab, hsv, grad, cand)
        # Smooth imagery (Mapbox satellite) leaves many tiles looking flat, so
        # once the map is imagery every candidate must show a round edge.
        on_imagery = imagery or bool(textured[y, x])
        threshold = _MIN_SCORE_IMAGERY if on_imagery else _MIN_SCORE
        if cand.score < threshold:
            continue
        if on_imagery and _radial_edge(light, cand.x / scale, cand.y / scale, cand.r / scale) < (
            _IMAGERY_RADIAL_EDGE
        ):
            continue  # a field's ragged border, not a drawn pin's round one
        pins.append(cand)
    # Grey pins: their own route, the colour and circle routes miss them.
    valid_full = cv2.resize(valid, (width, height), interpolation=cv2.INTER_NEAREST)
    for rect in exclude or []:
        valid_full[max(0, rect[1]) : max(0, rect[3]), max(0, rect[0]) : max(0, rect[2])] = 0
    for cand in _neutral_pins(image, cv2.cvtColor(image, cv2.COLOR_BGR2HSV), valid_full, scale):
        cand.color = "grey"
        pins.append(cand)
    pins = _merge(pins)

    markers = []
    for group in _join_clusters(_clusters(pins)):
        if len(group) >= CLUSTER_MIN:
            hidden = len(group) + sum(c.extra for c in group)
            markers.append(_marker(group, scale, max(_estimated_count(group, lab), hidden)))
        else:
            markers.extend(_marker([pin], scale) for pin in group)
    markers = [
        m
        for m in markers
        if m.count > 1 or not any(_inside((m.cx, m.cy), rect) for rect in text or [])
    ]
    markers.sort(key=lambda m: (m.cy // 40, m.cx))
    return MarkerScan(markers[:max_markers], imagery)


def _estimated_count(group: list[_Candidate], lab: np.ndarray) -> int:
    """Pins in a packed cluster, from the area its pins' colour covers.

    Circle detection misses some pins in a tight pack; the covered area
    does not. Never fewer than the pins actually found.
    """
    r = float(np.median([c.r for c in group]))
    x0 = max(0, int(min(c.x for c in group) - r))
    x1 = min(lab.shape[1], int(max(c.x for c in group) + r) + 1)
    y0 = max(0, int(min(c.y for c in group) - r))
    y1 = min(lab.shape[0], int(max(c.y for c in group) + r) + 1)
    patch = lab[y0:y1, x0:x1].astype(np.float32)
    colours = []
    for c in group:
        yy, xx = int(c.y) - y0, int(c.x) - x0
        if 0 <= yy < patch.shape[0] and 0 <= xx < patch.shape[1]:
            colours.append(patch[yy, xx])
    if not colours:
        return len(group)
    covered = float((np.linalg.norm(patch - np.median(colours, axis=0), axis=2) < 14.0).sum())
    per_pin = np.pi * (0.7 * r) ** 2
    return max(len(group), int(round(covered / per_pin)))


def _marker(group: list[_Candidate], scale: float, count: int | None = None) -> Marker:
    left = min(c.x - c.r for c in group) / scale
    top = min(c.y - c.r for c in group) / scale
    right = max(c.x + c.r for c in group) / scale
    bottom = max(c.y + c.r for c in group) / scale
    # Tap a real pin, the one nearest the group's middle, never the gap between.
    mx, my = np.mean([c.x for c in group]), np.mean([c.y for c in group])
    centre = min(group, key=lambda c: (c.x - mx) ** 2 + (c.y - my) ** 2)
    colors = [c.color for c in group]
    return Marker(
        int(round(centre.x / scale)),
        int(round(centre.y / scale)),
        int(left),
        int(top),
        int(right),
        int(bottom),
        max(set(colors), key=colors.count),
        int(sum(np.pi * c.r * c.r for c in group) / (scale * scale)),
        round(float(np.mean([c.score for c in group])), 2),
        count or len(group),
        "cluster" if len(group) > 1 else ("icon" if group[0].glyph else "pin"),
        any(c.packed or c.extra for c in group),
    )


def detect_markers(
    png_bytes: bytes,
    *,
    exclude: list[Rect] | None = None,
    region: Rect | None = None,
    max_markers: int = MAX_MARKERS,
) -> list[Marker]:
    """The markers alone; see :func:`scan_markers`."""
    return scan_markers(png_bytes, exclude=exclude, region=region, max_markers=max_markers).markers


def control_rects(elements: list[dict[str, Any]]) -> list[Rect]:
    """Pixel rects of labelled controls, whose icons are not markers."""
    rects = []
    for element in elements:
        if element.get("class") not in _CONTROL_CLASSES:
            continue
        if not str(element.get("text") or "").strip():
            continue
        bounds = element.get("bounds")
        if isinstance(bounds, (list, tuple)) and len(bounds) == 4:
            rects.append((int(bounds[0]), int(bounds[1]), int(bounds[2]), int(bounds[3])))
    return rects


#: Element classes that are text on screen, from the tree or OCR.
_TEXT_CLASSES = frozenset(
    {"StaticText", "StaticText [OCR]", "Text [OCR]", "Other [OCR]", "TextField", "TextView"}
)


def text_rects(elements: list[dict[str, Any]]) -> list[Rect]:
    """Pixel boxes of text, whose glyphs ("0", a title) are not pins."""
    rects = []
    for element in elements:
        if element.get("class") not in _TEXT_CLASSES or not str(element.get("text") or "").strip():
            continue
        frame = _pixel_bounds(element)
        if frame is not None:
            rects.append(frame)
    return rects


def _pixel_bounds(element: dict[str, Any]) -> Rect | None:
    bounds = element.get("bounds")
    if isinstance(bounds, (list, tuple)) and len(bounds) == 4:
        return (int(bounds[0]), int(bounds[1]), int(bounds[2]), int(bounds[3]))
    parsed = element.get("parsed_bounds")
    if isinstance(parsed, dict) and {"left", "top", "right", "bottom"} <= parsed.keys():
        return (
            int(parsed["left"]),
            int(parsed["top"]),
            int(parsed["right"]),
            int(parsed["bottom"]),
        )
    return None


def map_region(elements: list[dict[str, Any]]) -> Rect | None:
    """The largest Map element's pixel frame, when the tree has one."""
    frames = [
        frame
        for e in elements
        if str(e.get("class") or "") == "Map" and (frame := _pixel_bounds(e)) is not None
    ]
    if not frames:
        return None
    return max(frames, key=lambda f: (f[2] - f[0]) * (f[3] - f[1]))


def marker_elements(markers: list[Marker], first_index: int) -> list[dict[str, Any]]:
    """Markers as harness elements, numbered from ``first_index``."""
    elements = []
    for offset, marker in enumerate(markers):
        sure = f"{round(marker.confidence * 100)}%"
        if marker.count > 1:
            many = f"{marker.count}+" if marker.at_least else f"~{marker.count}"
            text = f"cluster of {many} {marker.color} pins ({sure})"
        else:
            text = f"{marker.color} {marker.kind} ({sure})"
        elements.append(
            {
                "index": first_index + offset,
                "center": [marker.cx, marker.cy],
                "hit_point": [marker.cx, marker.cy],
                "text": text,
                "bounds": [marker.left, marker.top, marker.right, marker.bottom],
                "class": "Marker",
                "is_ocr": False,
                "confidence": marker.confidence,
                "count": marker.count,
                "kind": marker.kind,
            }
        )
    return elements

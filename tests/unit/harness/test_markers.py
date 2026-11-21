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

"""Map pins the tree does not list, found as vivid blobs in the screenshot."""

import io

import pytest
from PIL import Image, ImageDraw

from lunatap.harness.markers import (
    control_rects,
    detect_markers,
    map_region,
    marker_elements,
    scan_markers,
    text_rects,
)
from tests.unit.harness.marker_scenes import PIN_R, satellite_scene

W, H = 1320, 2868


def _map_png(pins, *, big_rect=None):
    """A muted map (grey with pale roads) with vivid pins drawn on it."""
    image = Image.new("RGB", (W, H), (236, 233, 226))
    draw = ImageDraw.Draw(image)
    for y in range(0, H, 180):  # roads
        draw.line([(0, y), (W, y + 60)], fill=(255, 255, 255), width=14)
    for x in range(0, W, 220):
        draw.line([(x, 0), (x + 40, H)], fill=(214, 211, 200), width=10)
    draw.rectangle([200, 900, 600, 1100], fill=(199, 222, 190))  # a park, pale green
    for cx, cy, color in pins:
        draw.ellipse([cx - 18, cy - 18, cx + 18, cy + 18], fill=color)
        draw.polygon([(cx - 14, cy + 10), (cx + 14, cy + 10), (cx, cy + 40)], fill=color)
    if big_rect:
        draw.rectangle(big_rect, fill=(220, 40, 40))  # a vivid panel, not a pin
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


PINS = [(300, 700, (220, 40, 40)), (760, 1231, (30, 90, 230)), (1000, 1800, (220, 40, 40))]


def test_pins_are_found_with_their_centres_and_colours():
    # Live: a Mapbox map with 162 pins listed three accessibility elements.
    markers = detect_markers(_map_png(PINS))
    assert [(m.color, m.cx, m.cy) for m in markers] == pytest.approx(
        [("red", 300, 707), ("blue", 760, 1238), ("red", 1000, 1807)], abs=8
    ) or len(markers) == 3
    assert [m.color for m in markers] == ["red", "blue", "red"]
    for marker, (cx, cy, _) in zip(markers, PINS, strict=True):
        assert abs(marker.cx - cx) <= 8 and abs(marker.cy - cy) <= 14  # the pin's tail lowers it
        assert marker.left < cx < marker.right and marker.top < cy < marker.bottom


def test_panels_and_pale_areas_are_not_markers():
    markers = detect_markers(_map_png(PINS[:1], big_rect=[100, 2200, 1200, 2700]))
    assert len(markers) == 1 and markers[0].color == "red"


def test_icons_inside_labelled_controls_are_excluded():
    elements = [
        {"class": "Button", "text": "Map", "bounds": [660, 2600, 990, 2800]},
        {"class": "Button", "text": "", "bounds": [0, 0, 200, 200]},  # unlabelled: kept
        {"class": "StaticText", "text": "Jobs", "bounds": [0, 0, W, H]},  # not a control
    ]
    assert control_rects(elements) == [(660, 2600, 990, 2800)]
    pins = PINS + [(825, 2700, (220, 40, 40))]  # a tab-bar icon over the Map button
    markers = detect_markers(_map_png(pins), exclude=control_rects(elements))
    assert len(markers) == 3


def test_markers_become_tappable_elements_after_the_tree():
    elements = marker_elements(detect_markers(_map_png(PINS)), first_index=4)
    assert [e["index"] for e in elements] == [4, 5, 6]
    assert elements[1]["class"] == "Marker" and elements[1]["text"].startswith("blue pin (")
    assert elements[1]["hit_point"] == elements[1]["center"]


def test_garbage_bytes_find_nothing():
    assert detect_markers(b"not a png") == []


# --- QA item 26: a satellite map with grey pins (Flight) ---


def test_grey_pins_on_imagery_are_found_and_fields_are_not():
    # Live: 76 markers, almost all fields, the boundary and the battery;
    # the ~160 grey structure pins were all missed.
    png, truth = satellite_scene()
    scan = scan_markers(png)
    assert scan.imagery
    clusters = [m for m in scan.markers if m.count > 1]
    singles = [m for m in scan.markers if m.count == 1]
    assert len(clusters) == 1 and len(singles) == 3
    cluster = clusters[0]
    assert cluster.color == "grey"
    assert 0.8 * len(truth["cluster"]) <= cluster.count <= 1.25 * len(truth["cluster"])
    xs, ys = zip(*truth["cluster"], strict=True)
    assert cluster.left <= min(xs) and cluster.right >= max(xs)
    assert cluster.top <= min(ys) and cluster.bottom >= max(ys)
    # The tap point is a pin, not the gap between pins.
    assert any(
        abs(cluster.cx - x) <= PIN_R and abs(cluster.cy - y) <= PIN_R for x, y in truth["cluster"]
    )
    found = {(m.color, m.cx // 40, m.cy // 40) for m in singles}
    for x, y in truth["lone"]:
        assert ("grey", x // 40, y // 40) in found or any(
            abs(m.cx - x) <= 8 and abs(m.cy - y) <= 8 for m in singles
        )
    orange = next(m for m in singles if m.color == "orange")
    assert abs(orange.cx - truth["orange"][0]) <= 8 and abs(orange.cy - truth["orange"][1]) <= 8
    assert all(m.cy > 0.055 * 2622 for m in scan.markers)  # the status-bar battery is gone


def test_a_map_frame_from_the_tree_limits_the_search():
    png, truth = satellite_scene()
    region = (0, 1900, 1206, 2622)  # only the lower part is map
    scan = scan_markers(png, region=region)
    assert [(m.color, m.count) for m in scan.markers] == [("grey", 1)]  # the lone pin at y=2000
    assert map_region(
        [
            {"class": "Map", "bounds": [0, 300, 1206, 2400]},
            {"class": "Map", "parsed_bounds": {"left": 0, "top": 0, "right": 100, "bottom": 100}},
            {"class": "Other", "bounds": [0, 0, 1206, 2622]},
        ]
    ) == (0, 300, 1206, 2400)


def test_a_cluster_is_one_element_that_says_how_many():
    png, _ = satellite_scene()
    elements = marker_elements(scan_markers(png).markers, first_index=10)
    cluster = next(e for e in elements if e["count"] > 1)
    assert cluster["text"].startswith("cluster of ") and "grey pins (" in cluster["text"]
    assert cluster["kind"] == "cluster"
    assert 0 < cluster["confidence"] <= 1


def test_an_overlapping_pack_of_grey_pins_is_one_cluster():
    # Live (QA Part 9): Flight's ~100 pins overlap; no cluster was reported.
    png, truth = satellite_scene(packed=True)
    clusters = [m for m in scan_markers(png).markers if m.count > 1]
    assert len(clusters) == 1
    xs, ys = zip(*truth["cluster"], strict=True)
    pack = clusters[0]
    assert pack.left <= min(xs) + PIN_R and pack.right >= max(xs) - PIN_R
    assert pack.count >= 0.4 * len(truth["cluster"])  # a lower bound: hidden pins hide


def test_a_single_marker_on_text_is_dropped_but_a_cluster_under_a_label_is_kept():
    # Live (QA Part 9): the "0" digits of a stats row came back as pins.
    png, truth = satellite_scene()
    lone = truth["lone"][0]
    label_over_lone = (lone[0] - 30, lone[1] - 30, lone[0] + 30, lone[1] + 30)
    xs, ys = zip(*truth["cluster"], strict=True)
    label_over_pack = (min(xs), min(ys), max(xs), min(ys) + 60)  # a road name across the pack
    scan = scan_markers(png, text=[label_over_lone, label_over_pack])
    assert not any(abs(m.cx - lone[0]) < 20 and abs(m.cy - lone[1]) < 20 for m in scan.markers)
    assert any(m.count > 1 for m in scan.markers)
    assert text_rects(
        [
            {"class": "StaticText", "text": "0", "bounds": [10, 20, 30, 40]},
            {"class": "Text [OCR]", "text": "Map", "bounds": [50, 60, 90, 80]},
            {"class": "Button", "text": "Jobs", "bounds": [0, 0, 1, 1]},
            {"class": "StaticText", "text": " ", "bounds": [0, 0, 1, 1]},
        ]
    ) == [(10, 20, 30, 40), (50, 60, 90, 80)]


def test_a_pack_found_circle_by_circle_says_its_count_is_a_floor():
    # Live (QA, Part 10): "~51" for a pack of ~150; many pins hide under others.
    png, _ = satellite_scene(packed=True)
    pack = next(m for m in scan_markers(png).markers if m.count > 1)
    assert pack.at_least and pack.kind == "cluster"
    text = marker_elements([pack], 1)[0]["text"]
    assert text.startswith(f"cluster of {pack.count}+ grey pins")


def test_a_pin_with_a_symbol_inside_is_an_icon():
    from PIL import Image as PILImage

    image = PILImage.new("RGB", (W, H), (236, 233, 226))
    draw = ImageDraw.Draw(image)
    draw.ellipse([300 - 24, 700 - 24, 300 + 24, 700 + 24], fill=(60, 160, 70))  # a park symbol
    draw.rectangle([294, 684, 306, 716], fill=(255, 255, 255))
    draw.ellipse([800 - 24, 700 - 24, 800 + 24, 700 + 24], fill=(220, 40, 40))  # a plain pin
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    kinds = {m.color: m.kind for m in detect_markers(buffer.getvalue())}
    assert kinds == {"green": "icon", "red": "pin"}

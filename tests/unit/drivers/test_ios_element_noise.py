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

"""Elements that are never a target, and what names a screen."""

from lunatap.agents.fast_path.step_done import page_title
from lunatap.drivers.ios import wda
from lunatap.drivers.ios.hierarchy import pixel_element

SCALE = (3.0, 3.0)
W, H = 1206, 2622


def _node(kind, rect, *, label=None, children=()):
    x, y, w, h = rect
    entry = {
        "type": f"XCUIElementType{kind}",
        "rect": {"x": x, "y": y, "width": w, "height": h},
        "isVisible": 1,
        "children": list(children),
    }
    if label is not None:
        entry["label"] = label
    return entry


def test_scroll_indicators_are_not_elements():
    # Live: `Other 'Vertical scroll bar, 1 page'` on every screen, and once
    # even reported as the screen title.
    tree = _node(
        "Application",
        (0, 0, 402, 874),
        label="Jobs",
        children=[
            _node("Cell", (16, 200, 370, 57), label="Test job"),
            _node("Other", (392, 100, 6, 700), label="Vertical scroll bar, 1 page"),
            _node("Other", (0, 860, 402, 6), label="Horizontal scroll bar, 3 pages"),
        ],
    )
    texts = [e["text"] for e in wda.parse_wda_elements(tree, SCALE, W, H)]
    assert texts == ["Jobs", "Test job"]


def _el(text, cls, bounds):
    left, top, right, bottom = bounds
    return pixel_element(
        text=text, resource_id="", class_name=cls, left=left, top=top, right=right, bottom=bottom
    )


def test_an_alert_on_top_is_the_screen():
    elements = [
        _el("My Jobs", "NavigationBar", (0, 200, W, 300)),
        _el("Allow “Flight” to use your location?", "Alert", (150, 900, 1050, 1500)),
    ]
    assert page_title(elements, W, H) == "Allow “Flight” to use your location?"

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

"""OCR adds text the tree lacks; it must not repeat or garble what the tree has."""

from lunatap.utils.ocr_xml_fusion import fuse_ocr_with_xml
from lunatap.utils.visualization import format_minimal_list_with_elements

W, H = 1320, 2868


def _ocr(text, left, top, right, bottom):
    return {"text": text, "position": [{"x": left, "y": top}, {"x": right, "y": bottom}]}


def _row():
    # fuse_ocr_with_xml annotates the nodes in place, so each test gets its own.
    return [
        {"class": "Cell", "bounds": "[0,600][1320,760]", "text": ""},
        {"class": "Button", "bounds": "[60,620][400,740]", "text": ""},  # an icon button
        {"class": "StaticText", "bounds": "[420,640][900,720]", "text": "test 123"},
        {"class": "StaticText", "bounds": "[420,1190][900,1290]", "text": ""},  # custom-drawn
    ]


def test_ocr_that_garbles_a_label_already_in_the_tree_is_dropped():
    # Live (Flight app): a 4-row list came back as 49 elements, a third of
    # them OCR copies like "& test 123" called Button.
    fused = fuse_ocr_with_xml(_row(), [_ocr("& test 123", 425, 645, 895, 715)])
    assert not any("ocr_elements" in node for node in fused)
    assert not any("[OCR]" in str(node.get("class")) for node in fused)


def test_ocr_over_text_the_tree_lacks_is_kept_as_text_not_as_a_button():
    fused = fuse_ocr_with_xml(_row(), [_ocr("go", 100, 650, 360, 710)])
    assert fused[1]["ocr_elements"][0]["text"] == "go"
    _, elements, _ = format_minimal_list_with_elements(fused, W, H)
    ocr = next(e for e in elements if e["is_ocr"])
    assert ocr["text"] == "go" and ocr["class"] == "Text [OCR]"


def test_ocr_elsewhere_with_the_same_words_is_not_a_repeat():
    # Same words, different place: a label the tree has no text for.
    fused = fuse_ocr_with_xml(_row(), [_ocr("test 123", 425, 1200, 895, 1280)])
    _, elements, _ = format_minimal_list_with_elements(fused, W, H)
    assert [e["text"] for e in elements if e["is_ocr"]] == ["test 123"]


def _two_rows():
    # Two list rows; the stale frame puts row 2's text inside row 1.
    return [
        {"class": "Cell", "bounds": "[40,1320][1280,1480]", "text": ""},
        {"class": "StaticText", "bounds": "[110,1360][520,1420]", "text": "Older job"},
        {"class": "Cell", "bounds": "[40,1500][1280,1660]", "text": ""},
        {"class": "StaticText", "bounds": "[110,1540][520,1600]", "text": "Test job"},
    ]


def _ocr_texts(ocr):
    fused = fuse_ocr_with_xml(_two_rows(), [ocr])
    _, elements, _ = format_minimal_list_with_elements(fused, W, H)
    return [e["text"] for e in elements if e["is_ocr"]]


def test_a_row_read_off_a_frame_mid_slide_is_dropped():
    # Live (QA bug 24, Flight "My Jobs"): OCR 'Test job' [82,483] next to the
    # tree's 'Test job' [84,542]: same text, same x, ~55/1000 higher.
    assert _ocr_texts(_ocr("Test job", 112, 1424, 515, 1474)) == []


def test_without_the_guard_the_ghost_would_be_listed(monkeypatch):
    from lunatap.utils import ocr_xml_fusion

    monkeypatch.setattr(ocr_xml_fusion, "_GHOST_MAX_SHIFT", 0.0)
    assert _ocr_texts(_ocr("Test job", 112, 1424, 515, 1474)) == ["Test job"]


def test_a_different_label_near_a_tree_row_is_kept():
    assert _ocr_texts(_ocr("Due today", 112, 1424, 515, 1474)) == ["Due today"]

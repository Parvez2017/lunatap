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

"""Code settles a tap step when the next screen shows its destination."""

import json
from unittest.mock import MagicMock

import pytest

from lunatap.agents.fast_path import decider
from lunatap.agents.fast_path.step_done import step_reached
from lunatap.agents.operator.operator import OperatorNode
from lunatap.agents.operator.prompts import step_updated_task_plan, unwritten_action_streak
from lunatap.config.agent import MemoryTranscriptConfig
from lunatap.context import ArtemisContext
from tests.unit.agents.test_fast_path import GENERAL_PAGE, H, W, _el, _png_b64

SETTINGS_ROOT = [
    _el(1, "Settings", "Application", (0, 0, W, H)),
    _el(2, "Settings", "NavigationBar", (0, 200, W, 300)),
    _el(3, "General", "Cell", (60, 1100, 1260, 1260)),
]
GENERAL = [
    _el(1, "Settings", "Application", (0, 0, W, H)),
    _el(2, "General", "NavigationBar", (0, 200, W, 300)),
    _el(3, "About", "Cell", (60, 1100, 1260, 1260)),
]
KEYBOARD = [
    _el(1, "Settings", "Application", (0, 0, W, H)),
    _el(2, "Keyboard", "NavigationBar", (0, 200, W, 300)),
    _el(3, "Keyboards", "Cell", (60, 1100, 1260, 1260)),
]
# The simulator's Home page and the open folder, as Lunatap indexes them.
HOME = [
    _el(1, "Utilities folder", "Icon", (399, 600, 618, 888)),
    _el(2, "Safari", "Icon", (408, 2532, 624, 2751)),
]
FOLDER_OPEN = [
    _el(1, "Utilities", "TextField", (135, 612, 1185, 804)),
    _el(2, "Shortcuts", "Icon", (240, 957, 474, 1245)),
]


# --- step_reached ---


def test_a_new_screen_title_naming_the_step_completes_it():
    assert (
        step_reached("Open General settings", "General", SETTINGS_ROOT, GENERAL, W, H) == "General"
    )


def test_a_title_that_was_already_there_proves_nothing():
    assert step_reached("Open General settings", "General", GENERAL, GENERAL, W, H) is None


def test_a_step_naming_several_screens_stays_open():
    assert (
        step_reached("Open Settings > General > About", "General", SETTINGS_ROOT, GENERAL, W, H)
        is None
    )


def test_the_tapped_label_becoming_the_title_completes_the_step():
    # Live regression: "Open the Utilities folder to find Settings" was tapped
    # three times because nothing noticed the folder had opened.
    step = "Open the Utilities folder to find Settings"
    assert step_reached(step, "Utilities folder", HOME, FOLDER_OPEN, W, H) == "Utilities"


def test_a_title_that_contains_the_tapped_label_completes_the_step():
    # Live (Flight app): tapping the "Jobs" tab opens a page titled "My Jobs";
    # "Open the Jobs tab" was left as 'tapped' because of the extra word.
    home = [_el(1, "Home", "NavigationBar", (0, 200, W, 300))]
    jobs = [_el(1, "My Jobs", "NavigationBar", (0, 200, W, 300))]
    assert step_reached("Open the Jobs tab", "Jobs", home, jobs, W, H) == "My Jobs"
    # Without knowing what was tapped the extra word still keeps the step open.
    assert step_reached("Open the Jobs tab", None, home, jobs, W, H) is None


def test_a_plain_row_is_not_a_title():
    # "General" is a row on the Settings root, not the page we are on.
    assert step_reached("Open General settings", None, GENERAL, SETTINGS_ROOT, W, H) is None


def test_non_tap_steps_are_never_settled_by_code():
    assert (
        step_reached("Read the iOS version on the About page", None, GENERAL, KEYBOARD, W, H)
        is None
    )


# --- Operator ---

PLAN = (
    "- [x] Open Settings\n"
    "- [/] Open General > Keyboard and list the keyboards\n"
    "  - [/] Open Keyboard settings\n"
    "  - [ ] Open the Keyboards list\n"
)


def _operator(tmp_path, plan):
    notes = tmp_path / "notes"
    notes.mkdir(parents=True)
    (notes / "task_plan.md").write_text(plan, encoding="utf-8")
    ctx = MagicMock(spec=ArtemisContext)
    ctx.execution_setup = None
    ctx.data_engine = MagicMock()
    ctx.data_engine.base_dir = str(tmp_path)
    ctx.data_engine.get_agent_friendly_steps.return_value = []
    node = OperatorNode(ctx, transcript_config=MemoryTranscriptConfig(enabled=False))
    state = MagicMock()
    state.open_incident = None
    state.operator_feedback = None
    state.injected_instruction = None
    state.subagent_calls = []
    state.operator_raw_data = {"width": W, "height": H}
    state.indexed_elements = GENERAL_PAGE
    state.indexed_points = [e["center"] for e in GENERAL_PAGE]
    return node, state


@pytest.fixture
def fast_on(monkeypatch):
    monkeypatch.setenv("LUNATAP_FAST_PATH", "1")
    monkeypatch.setattr(decider, "_clef_credentials", lambda: None)


@pytest.mark.asyncio
async def test_a_fast_tap_followed_by_its_screen_marks_the_leaf_done(tmp_path, fast_on):
    node, state = _operator(tmp_path, PLAN)
    turn = await node._try_fast_path(state, PLAN, GENERAL_PAGE, _png_b64(), W, H)
    assert json.loads(turn["structured_decisions"])[0]["target_text"] == "Keyboard"

    settled = node._settle_previous_tap(state, PLAN, KEYBOARD, W, H)

    assert "  - [x] Open Keyboard settings\n" in settled
    assert "  - [/] Open the Keyboards list\n" in settled  # the next leaf is opened
    assert (tmp_path / "notes" / "task_plan.md").read_text(encoding="utf-8") == settled
    assert settled.count("[/]") == 2  # the milestone stays with the LLM


@pytest.mark.asyncio
async def test_a_tap_that_did_not_take_leaves_the_plan_alone(tmp_path, fast_on):
    node, state = _operator(tmp_path, PLAN)
    await node._try_fast_path(state, PLAN, GENERAL_PAGE, _png_b64(), W, H)

    assert node._settle_previous_tap(state, PLAN, GENERAL_PAGE, W, H) == PLAN
    assert (tmp_path / "notes" / "task_plan.md").read_text(encoding="utf-8") == PLAN


@pytest.mark.asyncio
async def test_the_llms_own_tap_is_settled_the_same_way(tmp_path, fast_on):
    node, state = _operator(tmp_path, PLAN)
    actions = [{"action": "tap", "coordinates": [660, 2088], "target_text": "Keyboard"}]
    node._remember_tap(PLAN, actions, GENERAL_PAGE)

    settled = node._settle_previous_tap(state, PLAN, KEYBOARD, W, H)

    assert "  - [x] Open Keyboard settings\n" in settled


@pytest.mark.asyncio
async def test_a_code_write_resets_the_ledger_streak(tmp_path, fast_on):
    node, state = _operator(tmp_path, PLAN)
    await node._try_fast_path(state, PLAN, GENERAL_PAGE, _png_b64(), W, H)
    node._turn_ledger_streak = 3  # one short of the default stale limit

    settled = node._settle_previous_tap(state, PLAN, KEYBOARD, W, H)

    assert node._fast_path_step(state, settled) == "Open the Keyboards list"
    assert node.code_plan_write == "Open Keyboard settings"


def test_a_recorded_code_plan_write_counts_as_a_plan_write():
    step = {"action_taken": [{"action": "tap"}], "tool_calls": []}
    assert step_updated_task_plan(step) is False
    step["extra_metadata"] = {"code_plan_write": "Open Keyboard settings"}
    assert step_updated_task_plan(step) is True
    assert unwritten_action_streak([step, {"action_taken": [{"action": "tap"}]}]) == 1


def test_the_status_bar_clock_is_not_a_title():
    # Live: the harness reported the Home screen as "8:51 PM".
    from lunatap.agents.fast_path.step_done import page_title

    clock = _el(1, "8:51 PM", "StaticText", (194, 66, 305, 132))
    assert (
        page_title([clock, _el(2, "General", "NavigationBar", (0, 200, W, 300))], W, H) == "General"
    )
    assert page_title([clock], W, H) is None


def test_a_glyph_read_by_ocr_is_not_a_title():
    # Live: the About page was titled "<" (OCR on the back chevron).
    from lunatap.agents.fast_path.step_done import page_title

    chevron = _el(1, "<", "Text [OCR]", (95, 218, 148, 292))
    about = _el(2, "About", "StaticText", (585, 218, 731, 284))
    assert page_title([chevron, about], W, H) == "About"


def test_an_ocr_title_loses_to_tree_text_and_is_marked_when_alone():
    # Live (QA, Flight structure screen): "3irth Mark" was OCR of "Birth
    # Mark" half under the floating back button, reported as the title.
    from lunatap.agents.fast_path.step_done import page_title

    ocr = _el(1, "3irth Mark", "Text [OCR]", (160, 218, 560, 284))
    tree = _el(2, "STRUCTURES", "StaticText", (600, 218, 1000, 284))
    assert page_title([ocr, tree], W, H) == "STRUCTURES"
    assert page_title([ocr], W, H) == "3irth Mark (OCR)"


def test_a_row_label_does_not_hide_the_same_text_as_the_next_title():
    # Live: Settings root lists 'General' as a row (a StaticText label inside
    # the cell); the General page's title is also a StaticText 'General'.
    root = [
        _el(1, "Settings", "NavigationBar", (0, 200, W, 300)),
        _el(2, "General", "Cell", (60, 1100, 1260, 1260)),
        _el(3, "General", "StaticText", (114, 1140, 400, 1220)),
    ]
    general = [
        _el(1, "General", "StaticText", (585, 218, 731, 284)),
        _el(2, "About", "Cell", (60, 400, 1260, 560)),
    ]
    assert step_reached("Open General", "General", root, general, W, H) == "General"

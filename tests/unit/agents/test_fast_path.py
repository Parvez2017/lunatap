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

"""Operator fast path: text match first, then Clef, else the LLM."""

import base64
import io
import json
from unittest.mock import MagicMock

import httpx
import pytest
from PIL import Image

from lunatap.agents.fast_path import FastPath, decider, questions
from lunatap.agents.fast_path.text_match import match_step
from lunatap.agents.operator.operator import OperatorNode
from lunatap.config.agent import MemoryTranscriptConfig
from lunatap.context import ArtemisContext

W, H = 1320, 2868


def _el(index, text, cls, bounds):
    left, top, right, bottom = bounds
    return {
        "index": index,
        "text": text,
        "class": cls,
        "bounds": list(bounds),
        "center": [(left + right) // 2, (top + bottom) // 2],
        "is_ocr": "[OCR]" in cls,
    }


# Settings > General, as Lunatap indexes it on an iPhone 17 Pro Max simulator.
GENERAL_PAGE = [
    _el(1, "Settings", "Application", (0, 0, W, H)),
    _el(2, "<", "Button [OCR]", (100, 225, 146, 288)),
    _el(3, "General", "StaticText", (567, 219, 753, 285)),
    _el(4, "About", "Button", (114, 1146, 378, 1230)),
    _el(5, "Keyboard", "Cell", (60, 2007, 1260, 2169)),
    _el(6, "Keyboard", "StaticText", (114, 2046, 460, 2130)),
    _el(7, "Search", "Button", (60, 1600, 1260, 1700)),
    _el(8, "Search", "SearchField", (95, 2650, 1220, 2770)),
]


def _png_b64():
    buffer = io.BytesIO()
    Image.new("RGB", (W // 4, H // 4), "white").save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode()


# --- text match ---


def test_a_label_and_its_row_tap_the_row():
    assert match_step("Open Keyboard settings", GENERAL_PAGE, W, H)["class"] == "Cell"


def test_a_page_title_is_never_tapped():
    # "General" on the General page is a title: the step may be done (Clef's call).
    assert match_step("Open General settings", GENERAL_PAGE, W, H) is None


def test_the_same_word_in_two_places_is_ambiguous():
    assert match_step("Tap Search", GENERAL_PAGE, W, H) is None


def test_only_tap_steps_are_matched():
    assert match_step("Read the About page and report the version", GENERAL_PAGE, W, H) is None


def test_a_mostly_unmatched_step_is_left_to_judgement():
    assert match_step("Open Keyboard and add the French layout", GENERAL_PAGE, W, H) is None


# --- FastPath.decide ---


def _clef(answers=None, status=200, calls=None):
    def handler(request):
        if calls is not None:
            calls.append(json.loads(request.content))
        if status != 200:
            return httpx.Response(status, json={"errors": ["boom"]})
        return httpx.Response(200, json={"result": {"answers": answers}})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _answers(move, target, sure=0.9):
    return {
        "move": {"choice": move, "probabilities": {move: sure}},
        "target": {"choice": target, "probabilities": {target: 0.8}},
    }


@pytest.fixture
def clef_on(monkeypatch):
    monkeypatch.setenv("LUNATAP_FAST_PATH", "1")
    monkeypatch.setattr(decider, "_clef_credentials", lambda: ("acct", "token"))


@pytest.mark.asyncio
async def test_text_match_taps_without_asking_clef(clef_on):
    calls = []
    fast = FastPath(client=_clef(calls=calls))
    decision = await fast.decide("Open Keyboard settings", GENERAL_PAGE, _png_b64(), W, H)
    assert decision is not None and decision.source == "text"
    assert GENERAL_PAGE[decision.index - 1]["class"] == "Cell"
    assert calls == []


@pytest.mark.asyncio
async def test_clef_picks_the_target_when_text_does_not_match(clef_on):
    calls = []
    fast = FastPath(client=_clef(_answers(questions.MOVE_TAP, "e2_unlabelled"), calls=calls))
    decision = await fast.decide(
        "Go back to the main Settings page", GENERAL_PAGE, _png_b64(), W, H
    )
    assert decision is not None and decision.source == "clef" and decision.index == 2
    request = calls[0]
    assert request["images"][0]["content_type"] == "image/jpeg"
    assert request["state"]["step"] == "Go back to the main Settings page"
    assert "e2_unlabelled" in request["questions"]["target"]["criteria"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "answers",
    [
        _answers(questions.MOVE_DONE, "e3_general"),
        _answers(questions.MOVE_OFF_SCREEN, "e4_about"),
        _answers(questions.MOVE_TAP, "e4_about", sure=0.4),
    ],
    ids=["already-done", "off-screen", "unsure"],
)
async def test_anything_but_a_sure_tap_goes_to_the_llm(clef_on, answers):
    fast = FastPath(client=_clef(answers))
    assert await fast.decide("Open General settings", GENERAL_PAGE, _png_b64(), W, H) is None


@pytest.mark.asyncio
async def test_a_clef_failure_goes_to_the_llm(clef_on):
    fast = FastPath(client=_clef(status=402))
    assert await fast.decide("Go back", GENERAL_PAGE, _png_b64(), W, H) is None


@pytest.mark.asyncio
async def test_a_step_gets_one_fast_tap_per_task(clef_on):
    # Live regression: "Open the Utilities folder" was fast-tapped three times
    # (as "Utilities", then "Utilities folder") although Settings is not there.
    fast = FastPath(client=_clef(_answers(questions.MOVE_TAP, "e5_keyboard")))
    assert await fast.decide("Open Keyboard settings", GENERAL_PAGE, _png_b64(), W, H)
    assert await fast.decide("Open Keyboard settings", GENERAL_PAGE, _png_b64(), W, H) is None
    fast.note_llm_turn()
    assert await fast.decide("Open Keyboard settings", GENERAL_PAGE, _png_b64(), W, H) is None


@pytest.mark.asyncio
async def test_the_llm_takes_a_turn_after_five_fast_ones(clef_on):
    # Code settles sub-goals now, so a run of fast taps can be longer before
    # the LLM must look; five covers Settings > General > About and back.
    fast = FastPath(client=_clef(_answers(questions.MOVE_TAP, "e2_unlabelled")))
    for step in ("Open Keyboard settings", "Open About", "Go back", "Open General", "Open Fonts"):
        assert await fast.decide(step, GENERAL_PAGE, _png_b64(), W, H)
    assert await fast.decide("Go back to Settings", GENERAL_PAGE, _png_b64(), W, H) is None
    fast.note_llm_turn()
    assert await fast.decide("Go back to Settings", GENERAL_PAGE, _png_b64(), W, H)


@pytest.mark.asyncio
async def test_switched_off(monkeypatch):
    monkeypatch.setenv("LUNATAP_FAST_PATH", "0")
    assert await FastPath().decide("Open Keyboard settings", GENERAL_PAGE, None, W, H) is None


# --- Operator integration ---


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
    return node, state, plan


PLAN = "- [x] Open Settings\n- [/] Open General > Keyboard\n  - [/] Open Keyboard settings\n"


@pytest.mark.asyncio
async def test_operator_taps_the_active_leaf_without_the_llm(tmp_path, clef_on):
    node, state, plan = _operator(tmp_path, PLAN)
    turn = await node._try_fast_path(state, plan, GENERAL_PAGE, _png_b64(), W, H)
    assert turn is not None
    action = json.loads(turn["structured_decisions"])[0]
    assert action["action"] == "tap"
    assert action["target_text"] == "Keyboard" and action["target_class"] == "Cell"
    assert turn["operator_raw_thinking"].startswith("[fast path: text]")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("plan", "field"),
    [
        ("- [/] [Loop] Open Keyboard settings for each layout\n", None),
        (PLAN, "operator_feedback"),
        (PLAN, "injected_instruction"),
    ],
    ids=["loop-step", "feedback", "user-instruction"],
)
async def test_operator_keeps_judgement_turns_for_the_llm(tmp_path, clef_on, plan, field):
    node, state, plan = _operator(tmp_path, plan)
    if field:
        setattr(state, field, "something to consider")
    assert await node._try_fast_path(state, plan, GENERAL_PAGE, _png_b64(), W, H) is None


# --- judge: the decision plus the reason the harness can read ---


@pytest.mark.asyncio
async def test_judge_says_why_it_declined(clef_on):
    fast = FastPath(client=_clef(_answers(questions.MOVE_OFF_SCREEN, "e4_about")))
    decision, reason = await fast.judge("Open General settings", GENERAL_PAGE, _png_b64(), W, H)
    assert decision is None and "target_not_on_screen" in reason


@pytest.mark.asyncio
async def test_judge_without_clef_credentials_says_so(monkeypatch):
    monkeypatch.setenv("LUNATAP_FAST_PATH", "1")
    monkeypatch.setattr(decider, "_clef_credentials", lambda: None)
    decision, reason = await FastPath().judge("Go back", GENERAL_PAGE, _png_b64(), W, H)
    assert decision is None and "Clef" in reason


@pytest.mark.asyncio
async def test_harness_mode_switches_both_guards_off(clef_on):
    fast = FastPath(client=_clef(_answers(questions.MOVE_TAP, "e5_keyboard")), harness=True)
    for _ in range(7):
        assert await fast.decide("Open Keyboard settings", GENERAL_PAGE, _png_b64(), W, H)

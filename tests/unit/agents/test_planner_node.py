# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# Modified by the Lunatap Authors in 2026 (forked from google/artemis).

from unittest.mock import Mock, patch

from lunatap.agents.planner.planner import PlannerNode
from lunatap.context import DevicePlatform, ArtemisContext
import pytest


class DummyState:
    def __init__(
        self,
        initial_goal,
        needs_replan=False,
        operator_raw_data=None,
        operator_replan_reason=None,
    ):
        self.initial_goal = initial_goal
        self.needs_replan = needs_replan
        self.operator_raw_data = operator_raw_data
        self.operator_replan_reason = operator_replan_reason


@pytest.fixture
def mock_context():
    ctx = Mock(spec=ArtemisContext)
    ctx.device = Mock()
    # The mock driver supplies the screenshot the planner asks for.
    ctx.device.mobile_platform = "mock"
    ctx.device.device_id = "mock-device"
    ctx.device.device_width = 1080
    ctx.device.device_height = 2400
    ctx.data_engine = None

    return ctx


@pytest.mark.asyncio
async def test_planner_initial_plan(mock_context):
    state = DummyState(
        initial_goal="Test Goal",
        operator_raw_data={"screenshot_b64": "dummy_base64_string"},
    )
    node = PlannerNode(mock_context)

    mock_llm = Mock()
    mock_response = Mock()
    mock_response.content = "- [ ] Subgoal 1"
    mock_response.tool_calls = []

    async def mock_astream(*args, **kwargs):
        yield mock_response

    mock_llm.astream.side_effect = mock_astream
    mock_llm.bind_tools = Mock(return_value=mock_llm)

    with patch("lunatap.agents.planner.planner.get_llm", return_value=mock_llm):
        await node(state)

        assert mock_llm.astream.called
        args, kwargs = mock_llm.astream.call_args
        messages = args[0]

        system_msg = messages[0].content
        assert "## Role" in system_msg

        human_msg_content = messages[1].content
        assert isinstance(human_msg_content, list)
        assert human_msg_content[0]["type"] == "text"
        assert "Goal: Test Goal" in human_msg_content[0]["text"]


def test_validate_plan_format_single_sourced_status_alphabet():
    """Regression: the Planner's format gate is single-sourced from
    plan_grammar — a legal in-progress '[/]' line must never be rejected."""
    from lunatap.agents.planner.planner import validate_plan_format

    valid, err = validate_plan_format(
        "- [x] Done milestone\n- [/] In-progress milestone\n  - [ ] Sub\n- [!] Blocked\n"
    )
    assert valid is True and err == ""

    # Every grammar status char is accepted; unknown ones are still rejected.
    from lunatap.utils.plan_grammar import STATUS_CHARS

    for c in STATUS_CHARS:
        ok, _ = validate_plan_format(f"- [{c}] Milestone\n")
        assert ok is True

    bad, message = validate_plan_format("- [z] Bogus status\n")
    assert bad is False
    assert "Invalid task status" in message

    empty, message = validate_plan_format("# just prose\n")
    assert empty is False
    assert "at least one subgoal" in message

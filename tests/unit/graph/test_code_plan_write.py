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

"""A plan step settled by code is stamped on the recorded step."""

from unittest.mock import patch

import pytest

from lunatap.graph.graph import execution_check_node
from lunatap.graph.state import State
from tests.unit.graph.test_checkpoints import _make_ctx, _make_state, _raw_data, _write_plan


@pytest.mark.asyncio
async def test_a_code_plan_write_is_stamped_on_the_step(tmp_path):
    _write_plan(tmp_path)
    ctx = _make_ctx(tmp_path)
    ctx.data_engine.record_step.return_value = "sid"
    state = _make_state(operator_raw_data=_raw_data(), code_plan_write="Open Keyboard settings")

    with patch("lunatap.graph.graph._get_active_subgoal_hashes", return_value=("h", None)):
        await execution_check_node(state, ctx)

    extra = ctx.data_engine.record_step.call_args.kwargs["extra_metadata"]
    assert extra["code_plan_write"] == "Open Keyboard settings"


def test_the_state_starts_without_a_code_plan_write():
    assert State.initial("goal").code_plan_write is None

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

"""Harness mode: an IDE agent plans and decides; Lunatap observes, acts and checks.

The other mode, handoff, is the autonomous Planner/Operator graph behind
``mobile_run_task``. Harness mode runs no model of its own.
"""

from lunatap.harness.flows import Flow, FlowResult, run_flow
from lunatap.harness.logs import LogCapture
from lunatap.harness.session import (
    ActOutcome,
    DeviceBusy,
    HarnessSession,
    Observation,
    StepOutcome,
)

__all__ = [
    "ActOutcome",
    "DeviceBusy",
    "Flow",
    "FlowResult",
    "HarnessSession",
    "LogCapture",
    "Observation",
    "StepOutcome",
    "run_flow",
]

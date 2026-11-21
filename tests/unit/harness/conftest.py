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

"""A started harness session over the fake device, shared by the harness tests."""

import pytest
import pytest_asyncio

from lunatap.harness import session as session_module
from lunatap.harness.session import HarnessSession
from tests.unit.harness.test_session import FakeActuator, FakeController, FakeLease


@pytest.fixture
def lease(monkeypatch):
    FakeLease.instances = []
    FakeLease.busy = False
    monkeypatch.setattr(session_module, "DeviceExecutionLock", FakeLease)
    return FakeLease


@pytest_asyncio.fixture
async def started(lease):
    controller = FakeController()
    actuator = FakeActuator(controller)
    session = HarnessSession(controller, actuator=actuator)
    await session.start()
    return session, controller, actuator

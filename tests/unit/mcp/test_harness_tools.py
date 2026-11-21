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

"""The harness-mode MCP tools are thin wrappers over HarnessSession."""

import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from lunatap.harness.session import DeviceBusy
from mcp_server.tools import (
    mobile_act,
    mobile_assert,
    mobile_do_step,
    mobile_flow,
    mobile_logs,
    mobile_net,
    mobile_observe,
    mobile_release_device,
    mobile_wait_for,
)
from mcp_server.tools import harness as harness_tools


def test_tool_signatures_and_docs():
    assert "device_serial" in inspect.signature(mobile_observe).parameters
    assert inspect.signature(mobile_observe).parameters["screenshot"].default is False
    act = inspect.signature(mobile_act).parameters
    for name in ("action", "target", "text", "direction", "key", "app", "url", "device_serial"):
        assert name in act
    assert "step" in inspect.signature(mobile_do_step).parameters
    assert "text" in inspect.signature(mobile_wait_for).parameters
    assert "device_serial" in inspect.signature(mobile_release_device).parameters
    # The docstring is the only contract an MCP caller sees.
    assert "mobile_observe" in (mobile_act.__doc__ or "")
    assert "mobile_run_task" in (mobile_observe.__doc__ or "")


@pytest.mark.asyncio
async def test_observe_returns_the_sessions_description():
    session = SimpleNamespace(
        observe=AsyncMock(return_value=SimpleNamespace(describe=lambda: "Screen: Settings"))
    )
    with patch.object(harness_tools, "_get_session", AsyncMock(return_value=session)):
        assert await mobile_observe() == "Screen: Settings"
    session.observe.assert_awaited_once_with(screenshot=False, detect=None)


@pytest.mark.asyncio
async def test_a_busy_device_comes_back_as_an_error_string():
    with patch.object(
        harness_tools, "_get_session", AsyncMock(side_effect=DeviceBusy("SIM-1 is busy"))
    ):
        text = await mobile_act("tap", target=3)
    assert text.startswith("Error:") and "busy" in text


@pytest.mark.asyncio
async def test_release_closes_and_forgets_the_session():
    session = SimpleNamespace(close=AsyncMock())
    harness_tools._sessions["SIM-1"] = session
    text = await mobile_release_device("SIM-1")
    session.close.assert_awaited_once()
    assert "SIM-1" not in harness_tools._sessions and "released" in text.lower()
    assert "nothing" in (await mobile_release_device("SIM-1")).lower()


@pytest.mark.asyncio
async def test_any_serial_for_the_same_phone_finds_the_same_session(monkeypatch):
    # Bug 22: the CoreDevice id of a phone a session already held was
    # reported as "busy, held by its own session".
    udid, core_id = "00008140-00000000000000AB", "11111111-2222-4333-8444-555555555555"
    phone = {"udid": udid, "identifier": core_id, "name": "iPhone (2)"}
    monkeypatch.setattr(
        "lunatap.drivers.ios.discovery.list_ios_simulators_sync", lambda force_refresh=False: []
    )
    monkeypatch.setattr(
        "lunatap.drivers.ios.discovery.find_physical_ios_device_sync",
        lambda identifier: (
            phone if identifier.lower() in (udid.lower(), core_id.lower(), "iphone (2)") else None
        ),
    )
    held = SimpleNamespace(is_simulator=False)
    with patch.dict(harness_tools._sessions, {udid: held}, clear=True):
        assert await harness_tools._get_session(core_id) is held
        assert await harness_tools._get_session("iPhone (2)") is held
        assert await harness_tools._get_session(udid.lower()) is held


@pytest.mark.asyncio
async def test_a_simulator_in_the_way_of_the_phone_names_the_release_call():
    # Live: the simulator's WDA on :8100 answered for the phone and the fix
    # (release the simulator) was left for the QA agent to find.
    sim = SimpleNamespace(is_simulator=True)
    error = RuntimeError(
        "The WebDriverAgent at http://127.0.0.1:8100 does not report the selected physical "
        "device 0000 (expected name 'iPhone (2)', got 'iPhone 17 Pro Max', isSimulator=True)."
    )
    with patch.dict(harness_tools._sessions, {"SIM-1": sim}, clear=True):
        with patch.object(harness_tools, "_get_session", AsyncMock(side_effect=error)):
            reply = await mobile_observe(device_serial="0000")
    assert reply.startswith("Error: The WebDriverAgent")
    assert "call mobile_release_device('SIM-1'), then retry" in reply


@pytest.mark.asyncio
async def test_mobile_logs_reads_through_the_session_and_says_how_to_start():
    assert "since_step" in inspect.signature(mobile_logs).parameters
    session = SimpleNamespace(logs=None, start_logs=AsyncMock(return_value="Log capture running"))
    with patch.object(harness_tools, "_get_session", AsyncMock(return_value=session)):
        assert "mobile_logs(action='start'" in await mobile_logs(action="read")
        reply = await mobile_logs(action="start", bundle_id="Flight.flightFiber")
    assert reply == "OK: Log capture running"
    session.start_logs.assert_awaited_once_with(
        bundle_id="Flight.flightFiber", process=None, mode="stream"
    )


@pytest.mark.asyncio
async def test_mobile_assert_and_mobile_flow_go_through_the_session(tmp_path, monkeypatch):
    from lunatap.harness.session import ActOutcome

    monkeypatch.setenv("LUNATAP_FLOWS_DIR", str(tmp_path))
    session = SimpleNamespace(
        assert_text=AsyncMock(
            return_value=ActOutcome(False, "'x' is not present", "assert_failed")
        ),
        start_recording=lambda name: f"Recording flow '{name}'",
    )
    with patch.object(harness_tools, "_get_session", AsyncMock(return_value=session)):
        assert (await mobile_assert("x")).startswith("FAIL: 'x' is not present")
        assert await mobile_flow("record", name="smoke") == "OK: Recording flow 'smoke'"
        assert (await mobile_flow("list")).endswith(": none")
        assert (await mobile_flow("run", name="nope")).startswith("Error: No flow at")
    session.assert_text.assert_awaited_once_with("x", state="present", timeout_ms=5000)


@pytest.mark.asyncio
async def test_mobile_net_and_marker_detection_go_through_the_session():
    assert "detect" in inspect.signature(mobile_observe).parameters
    observation = SimpleNamespace(describe=lambda: "Screen: Map")
    session = SimpleNamespace(
        net=None,
        observe=AsyncMock(return_value=observation),
        start_net=AsyncMock(return_value="Network capture running"),
    )
    with patch.object(harness_tools, "_get_session", AsyncMock(return_value=session)):
        assert "mobile_net(action='start')" in await mobile_net(action="read")
        assert await mobile_net(action="start", port=9090) == "OK: Network capture running"
        assert await mobile_observe(detect="markers") == "Screen: Map"
    session.start_net.assert_awaited_once_with(port=9090)
    session.observe.assert_awaited_once_with(screenshot=False, detect="markers")

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

"""Checks that can fail, and flows that replay on the next build."""

import json

import pytest
from typer.testing import CliRunner

from lunatap.harness import flows as flows_module
from lunatap.harness.flows import Flow, flow_path, flows_in, run_flow
from lunatap.interfaces.cli.commands import flow as flow_cli
from lunatap.mcp.action_types import ActionResult
from tests.unit.harness.test_session import FakeActuator, FakeController


@pytest.fixture(autouse=True)
def flows_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("LUNATAP_FLOWS_DIR", str(tmp_path / "flows"))
    return tmp_path / "flows"


# --- assert ---


@pytest.mark.asyncio
async def test_assert_passes_and_fails_with_the_screen(started, monkeypatch):
    session, controller, actuator = started
    outcome = await session.assert_text("General")
    assert outcome.ok and outcome.message == "'General' is present on screen."

    async def missing(text, wait_state, timeout_ms):
        return ActionResult.failure("wait_for_text", "timed out")

    monkeypatch.setattr(actuator, "wait_for_text", missing)
    outcome = await session.assert_text("1/8 required captures", timeout_ms=100)
    assert not outcome.ok and outcome.code == "assert_failed"
    assert "not present on screen after 100 ms (title: Settings)" in outcome.message
    assert outcome.observation is not None and outcome.observation.title == "Settings"


# --- record / save / load ---


@pytest.mark.asyncio
async def test_a_session_is_recorded_by_text_and_saved_as_json(started, flows_dir):
    session, controller, _ = started
    session.start_recording("settings-about")
    observation = await session.observe()
    general = next(e for e in observation.elements if e["text"] == "General")
    await session.act("tap", target=general["index"])  # by index here...
    await session.do_step("Open About")
    await session.wait_for("iOS Version", timeout_ms=2000)
    await session.assert_text("26.5")
    await session.act("swipe", direction="up", observe_after=False)

    flow, path, skipped = session.stop_recording()

    assert path == str(flows_dir / "settings-about.json") and skipped == []
    data = json.loads(open(path, encoding="utf-8").read())
    assert data["steps"] == [
        # ...saved by text, with the screen each step reached (bug 19)
        {"kind": "act", "action": "tap", "target": "General", "screen": "General"},
        {"kind": "do_step", "step": "Open About", "screen": "About"},
        {"kind": "wait_for", "text": "iOS Version", "state": "appear", "timeout_ms": 2000},
        {"kind": "assert", "text": "26.5", "state": "present", "timeout_ms": 5000},
        {"kind": "act", "action": "swipe", "direction": "up"},
    ]
    assert "  1. act tap (target='General') -> 'General'" in flow.describe()
    assert Flow.load("settings-about").describe().startswith("Flow 'settings-about': 5 step(s)")
    assert session.recorder is None


def test_flow_names_become_files_and_paths_stay_paths(flows_dir, tmp_path):
    assert flow_path("Sign in / accept") == flows_dir / "Sign-in-accept.json"
    assert flow_path(str(tmp_path / "x.json")) == tmp_path / "x.json"
    (flows_dir).mkdir()
    (flows_dir / "a.json").write_text("{}")
    (flows_dir / "b.json").write_text("{}")
    assert flows_in(str(flows_dir)) == [flows_dir / "a.json", flows_dir / "b.json"]
    assert flows_in("a") == [flows_dir / "a.json"]
    assert flows_in("nope") == []


def test_a_malformed_step_is_rejected():
    with pytest.raises(ValueError, match="kind"):
        Flow.from_json('{"name": "x", "steps": [{"action": "tap"}]}')


# --- run ---


def _flow(*steps):
    return Flow(name="t", steps=[flows_module.FlowStep(s.pop("kind"), s) for s in steps])


@pytest.mark.asyncio
async def test_a_flow_replays_and_passes_its_checks(started):
    session, controller, actuator = started
    result = await run_flow(
        session,
        _flow(
            {"kind": "act", "action": "tap", "target": "General"},
            {"kind": "do_step", "step": "Open About"},
            {"kind": "assert", "text": "26.5", "state": "present"},
        ),
    )
    assert result.passed and result.steps_run == 3
    assert controller.screen == "about"
    assert result.describe().startswith("PASS flow 't': 3/3 step(s)")


@pytest.mark.asyncio
async def test_a_flow_stops_at_the_first_failing_check_with_the_screen(started, monkeypatch):
    session, controller, actuator = started

    async def never(text, wait_state, timeout_ms):
        return ActionResult.failure("wait_for_text", "timed out")

    monkeypatch.setattr(actuator, "wait_for_text", never)
    result = await run_flow(
        session,
        _flow(
            {"kind": "act", "action": "tap", "target": "General"},
            {"kind": "assert", "text": "1/8 required captures"},
            {"kind": "act", "action": "tap", "target": "About"},
        ),
    )
    assert not result.passed and result.steps_run == 2 and result.total == 3
    assert result.failed_step is not None and result.failed_step.kind == "assert"
    assert "Failed at step 2: assert '1/8 required captures' present" in result.describe()
    assert result.observation is not None and result.observation.title == "General"
    assert controller.screen == "general"  # the third step never ran


@pytest.mark.asyncio
async def test_a_moved_label_is_recovered_through_do_step(started):
    # The flow says tap 'general' (recorded lower-case by a QA agent); exact
    # text match fails on nothing, so act falls back to the step decider.
    session, controller, _ = started
    result = await run_flow(session, _flow({"kind": "act", "action": "tap", "target": "Genral"}))
    assert not result.passed  # a typo nobody can resolve stays a failure...
    assert "do_step fallback" in result.message

    controller.screen = "settings"
    await session.observe()
    session._last.elements[1]["text"] = "General settings"  # ...but a label that grew is fine
    result = await run_flow(
        session, _flow({"kind": "act", "action": "tap", "target": "General settings row"})
    )
    assert result.passed, result.describe()


@pytest.mark.asyncio
async def test_replaying_does_not_record_itself(started):
    session, _, _ = started
    session.start_recording("outer")
    await run_flow(session, _flow({"kind": "act", "action": "tap", "target": "General"}))
    flow, _, _ = session.stop_recording(save=False)
    assert flow.steps == []


@pytest.mark.asyncio
async def test_a_check_that_failed_while_recording_is_not_a_flow_step(started, monkeypatch):
    # Bug 20: a FAIL during recording became step 5 and every replay failed there.
    session, _, actuator = started

    async def never(text, wait_state, timeout_ms):
        return ActionResult.failure("wait_for_text", "timed out")

    monkeypatch.setattr(actuator, "wait_for_text", never)
    session.start_recording("x")
    await session.assert_text("1/8 required captures", timeout_ms=10)
    await session.wait_for("Jobs", timeout_ms=10)
    flow, _, skipped = session.stop_recording(save=False)
    assert flow.steps == []
    assert skipped == [
        "assert '1/8 required captures' present (failed while recording)",
        "wait_for 'Jobs' appear (timed out while recording)",
    ]


@pytest.mark.asyncio
async def test_a_replay_that_lands_on_another_screen_fails(started):
    # Bug 19: a pixel tap opened a different pole and the 0/8 check kept passing.
    session, controller, _ = started
    result = await run_flow(
        session,
        _flow(
            {"kind": "act", "action": "tap", "target": "General", "screen": "FF Pole: 1343168"},
            {"kind": "assert", "text": "About"},
        ),
    )
    assert not result.passed and result.steps_run == 1
    assert "expected 'FF Pole: 1343168', got 'General'" in result.message

    step = flows_module.FlowStep("act", {"action": "tap", "target": [760, 1231], "screen": "Map"})
    assert (
        step.describe() == "act tap (target=[760, 1231]) [replays by coordinates: fragile] -> 'Map'"
    )


def test_flows_live_next_to_the_traces_dir_not_in_the_callers_cwd(monkeypatch, tmp_path):
    # Bug 21: flows landed in the app repo the MCP server was started from.
    monkeypatch.delenv("LUNATAP_FLOWS_DIR", raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "lunatap.config.paths.get_default_traces_path", lambda: tmp_path / "lunatap" / "traces"
    )
    assert flows_module.flows_dir() == tmp_path / "lunatap" / "flows"
    assert flows_module.flows_dir() != tmp_path / "flows"


# --- CLI ---


def test_cli_runs_a_folder_of_flows_and_exits_nonzero_on_a_failure(flows_dir, monkeypatch):
    flows_dir.mkdir()
    Flow(
        name="good", steps=[flows_module.FlowStep("act", {"action": "tap", "target": "General"})]
    ).save()
    Flow(name="bad", steps=[flows_module.FlowStep("assert", {"text": "never there"})]).save()

    async def open_session(device_serial):
        from lunatap.harness import session as session_module
        from lunatap.harness.session import HarnessSession
        from tests.unit.harness.test_session import FakeLease

        FakeLease.instances, FakeLease.busy = [], False
        monkeypatch.setattr(session_module, "DeviceExecutionLock", FakeLease)
        controller = FakeController()
        actuator = FakeActuator(controller)

        async def never(text, wait_state, timeout_ms):
            return ActionResult.failure("wait_for_text", "timed out")

        actuator.wait_for_text = never
        session = HarnessSession(controller, actuator=actuator)
        await session.start()
        return session

    monkeypatch.setattr(flow_cli, "_open_session", open_session)
    result = CliRunner().invoke(flow_cli.flow_app, ["run", str(flows_dir), "-d", "SIM-1"])
    assert result.exit_code == 1, result.output
    assert "FAIL flow 'bad'" in result.output and "PASS flow 'good'" in result.output
    assert "1/2 flow(s) passed; failed: bad" in result.output

    result = CliRunner().invoke(flow_cli.flow_app, ["run", str(flows_dir / "good.json")])
    assert result.exit_code == 0, result.output
    assert CliRunner().invoke(flow_cli.flow_app, ["run", "missing"]).exit_code == 2

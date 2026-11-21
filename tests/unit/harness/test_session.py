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

"""Harness mode: the IDE agent observes and acts; code does the rest."""

import asyncio
import base64
import io
import sys
from types import SimpleNamespace

import pytest
import pytest_asyncio
from PIL import Image

from lunatap.controllers.device_controller import ScreenDataResponse
from lunatap.drivers.ios.hierarchy import pixel_element
from lunatap.harness import session as session_module
from lunatap.harness.session import DeviceBusy, HarnessSession
from lunatap.mcp.action_types import ActionResult
from lunatap.runtime.device_lock import DeviceBusyError
from tests.unit.harness.test_logs import TAIL_SCRIPT

W, H = 1320, 2868


def _el(text, cls, bounds):
    left, top, right, bottom = bounds
    element = pixel_element(
        text=text, resource_id="", class_name=cls, left=left, top=top, right=right, bottom=bottom
    )
    element["hit_point"] = [(left + right) // 2, (top + bottom) // 2]
    return element


SCREENS = {
    "settings": [
        _el("Settings", "NavigationBar", (0, 200, W, 300)),
        _el("General", "Cell", (60, 1100, 1260, 1260)),
        _el("Search", "SearchField", (95, 400, 1220, 500)),
        _el("Search", "Button", (60, 1600, 1260, 1700)),
        # A tab-bar button and the label drawn inside it.
        _el("Map", "Button", (660, 2600, 990, 2800)),
        _el("Map", "StaticText", (760, 2700, 890, 2780)),
    ],
    "general": [
        _el("General", "NavigationBar", (0, 200, W, 300)),
        _el("About", "Cell", (60, 1100, 1260, 1260)),
    ],
    "about": [
        _el("About", "NavigationBar", (0, 200, W, 300)),
        _el("iOS Version", "StaticText", (60, 1100, 600, 1200)),
        _el("26.5", "StaticText", (700, 1100, 1260, 1200)),
    ],
    "proxy": [
        _el("Configure Proxy", "NavigationBar", (0, 200, W, 300)),
        # Live (QA 31): OCR read the label 'Port'; the tree has the field 'Port'.
        {**_el("Port", "Text [OCR]", (60, 900, 300, 960)), "is_ocr": True},
        _el("Port", "TextField", (320, 880, 1260, 980)),
        _el("Server", "StaticText", (60, 700, 300, 760)),
        _el("Server", "TextField", (320, 680, 1260, 780)),
    ],
    "jobs": [
        _el("My Jobs", "NavigationBar", (0, 200, W, 300)),
        # Live (QA bug 25): one row Button holding two labels with the same text.
        _el("Test job, IN FIELD, Due today", "Button", (40, 1500, 1280, 1700)),
        _el("Test job", "StaticText", (110, 1530, 520, 1580)),
        _el("Test job", "StaticText", (110, 1620, 520, 1670)),
        # A second row whose label repeats elsewhere, in a different control.
        _el("Draft", "Button", (40, 1800, 1280, 1950)),
        _el("Draft", "StaticText", (110, 2100, 400, 2150)),
    ],
    "alert": [
        _el("Jobs", "NavigationBar", (0, 200, W, 300)),
        _el("Delete this job?", "Alert", (150, 1000, 1170, 1500)),
        _el("Delete this job?", "StaticText", (200, 1050, 1120, 1120)),
    ],
}
SCREENS_ALERT_BUTTONS = ["Cancel", "Delete"]
COLORS = {
    "settings": "white",
    "general": "lightgrey",
    "about": "lightblue",
    "alert": "pink",
    "jobs": "lightyellow",
    "proxy": "lavender",
}
TRANSITIONS = {("settings", "General"): "general", ("general", "About"): "about"}


def _png(color):
    buffer = io.BytesIO()
    Image.new("RGB", (W // 4, H // 4), color).save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode()


class FakeWda:
    """The alert endpoints of WebDriverAgent."""

    def __init__(self, buttons):
        self.buttons = buttons
        self.accepted = []

    async def alert_buttons(self):
        return list(self.buttons)

    async def alert_accept(self, button=None):
        self.accepted.append(button)
        self.buttons = []


class FakeDriver:
    def __init__(self):
        self.connected = False
        self.screen_size = (W, H)

    async def resolve_device(self):
        return "SIM-1"

    async def connect(self):
        self.connected = True

    async def disconnect(self):
        self.connected = False


class FakeController:
    def __init__(self, screen="settings"):
        self.screen = screen
        self.driver = FakeDriver()
        self.ctx = SimpleNamespace(
            device=SimpleNamespace(
                device_id="booted", device_width=W, device_height=H, mobile_platform="ios"
            ),
            data_engine=None,
        )

    async def get_screen_data(self):
        return ScreenDataResponse(
            base64=_png(COLORS[self.screen]), elements=SCREENS[self.screen], width=W, height=H
        )


class FakeActuator:
    """Taps move the fake controller between screens; everything else is recorded."""

    def __init__(self, controller):
        self.controller = controller
        self.calls = []

    async def take_screenshot(self):
        return _png(COLORS[self.controller.screen])

    async def click(self, nx, ny, times=1, delay_ms=100):
        self.calls.append(("click", nx, ny))
        x, y = nx * W / 1000, ny * H / 1000
        for element in SCREENS[self.controller.screen]:
            b = element["parsed_bounds"]
            if b["left"] <= x <= b["right"] and b["top"] <= y <= b["bottom"]:
                key = (self.controller.screen, element["text"])
                if key in TRANSITIONS:
                    self.controller.screen = TRANSITIONS[key]
        return ActionResult.success("click", "Tapped.")

    async def long_press(self, nx, ny, duration_ms=1000):
        self.calls.append(("long_press", nx, ny, duration_ms))
        return ActionResult.success("long_press", "Held.")

    async def input_text(self, text, target=None, clear_exist=True):
        self.calls.append(("input_text", text, target))
        return ActionResult.success("input_text", f"Typed '{text}'.")

    async def swipe(self, start, end, duration_ms=400):
        self.calls.append(("swipe", start, end))
        return ActionResult.success("swipe", "Swiped.")

    async def press_key(self, key):
        self.calls.append(("press_key", key))
        return ActionResult.success("press_key", f"Pressed {key}.")

    async def manage_app(self, action, app_name):
        self.calls.append(("manage_app", action, app_name))
        return ActionResult.success("manage_app", f"{action} {app_name}.")

    async def wait_for_delay(self, time_in_ms):
        self.calls.append(("wait_for_delay", time_in_ms))
        return ActionResult.success("wait_for_delay", "Waited.")

    async def wait_for_text(self, text, wait_state, timeout_ms):
        self.calls.append(("wait_for_text", text, wait_state, timeout_ms))
        return ActionResult.success("wait_for_text", f"'{text}' appeared.")

    async def open_link(self, url):
        self.calls.append(("open_link", url))
        return ActionResult.success("open_link", "Opened.")


class FakeLease:
    instances = []

    def __init__(self, device_id, **kwargs):
        self.device_id = device_id
        self.acquired = False
        self.released = False
        FakeLease.instances.append(self)

    def acquire(self, blocking=True, **kwargs):
        if FakeLease.busy:
            raise DeviceBusyError("Device SIM-1 is busy: Lunatap handoff task running.")
        self.acquired = True

    def release(self):
        self.released = True


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


# --- lifecycle ---


@pytest.mark.asyncio
async def test_start_takes_the_ios_lease_and_connects(started, lease):
    session, controller, _ = started
    assert controller.driver.connected
    assert lease.instances[0].device_id == "SIM-1" and lease.instances[0].acquired
    await session.close()
    assert not controller.driver.connected and lease.instances[0].released


@pytest.mark.asyncio
async def test_a_busy_device_is_refused_with_the_holder_named(lease):
    lease.busy = True
    session = HarnessSession(FakeController(), actuator=FakeActuator(FakeController()))
    with pytest.raises(DeviceBusy, match="handoff task running"):
        await session.start()


# --- observe ---


@pytest.mark.asyncio
async def test_observe_numbers_the_elements_and_names_the_screen(started):
    session, _, _ = started
    observation = await session.observe()
    assert observation.title == "Settings"
    assert [e["text"] for e in observation.elements][:2] == ["Settings", "General"]
    text = observation.describe()
    assert text.startswith("Screen: Settings")
    # Class, text and 0-1000 bounds on one line: what the harness picks from.
    assert "[2] Cell 'General' [45,383][954,439]" in text
    assert "screenshot" not in text.lower()


@pytest.mark.asyncio
async def test_observe_names_the_backend_driving_the_device(started):
    # Live: a simulator's WDA on :8100 answered for the phone; the header
    # should make such a mismatch obvious.
    session, _, _ = started
    observation = await session.observe()
    assert observation.backend == "FakeDriver"
    assert observation.describe().splitlines()[0].endswith("via FakeDriver")
    assert session.is_simulator


@pytest.mark.asyncio
async def test_observe_can_hand_over_the_screenshot_path(started):
    session, _, _ = started
    observation = await session.observe(screenshot=True)
    assert observation.screenshot_path and observation.screenshot_path in observation.describe()


# --- act ---


@pytest.mark.asyncio
async def test_tap_by_index_lands_on_the_element_and_returns_the_new_screen(started):
    session, _, actuator = started
    observation = await session.observe()
    general = next(e for e in observation.elements if e["text"] == "General")

    outcome = await session.act("tap", target=general["index"])

    assert outcome.ok and outcome.screen_changed
    assert outcome.observation.title == "General"
    assert actuator.calls == [("click", 500, 411)]  # the row's centre, 0-1000


@pytest.mark.asyncio
async def test_tap_by_text_resolves_a_unique_label(started):
    session, _, actuator = started
    await session.observe()
    outcome = await session.act("tap", target="general")
    assert outcome.ok and outcome.observation.title == "General"
    assert actuator.calls[0][0] == "click"


@pytest.mark.asyncio
async def test_an_ambiguous_text_target_is_refused_with_the_candidates(started):
    session, _, actuator = started
    await session.observe()
    outcome = await session.act("tap", target="Search")
    assert not outcome.ok and outcome.code == "bad_target"
    assert "SearchField" in outcome.message and "Button" in outcome.message
    assert actuator.calls == []


@pytest.mark.asyncio
async def test_a_label_inside_its_own_button_is_not_ambiguous(started):
    # Live (Flight app): every tab bar has a Button 'Map' with a StaticText
    # 'Map' inside it; the outer control is the one to tap.
    session, _, actuator = started
    await session.observe()
    outcome = await session.act("tap", target="Map", observe_after=False)
    assert outcome.ok, outcome.message
    assert actuator.calls == [("click", 625, 941)]  # the Button's centre, 0-1000


@pytest.mark.asyncio
async def test_same_text_labels_inside_one_row_tap_the_row(started):
    session, controller, actuator = started
    controller.screen = "jobs"
    await session.observe()
    outcome = await session.act("tap", target="Test job", observe_after=False)
    assert outcome.ok, outcome.message
    assert actuator.calls == [("click", 500, 558)]  # the row Button's centre, 0-1000


@pytest.mark.asyncio
async def test_a_tree_element_beats_ocr_text_and_type_prefers_the_field(started):
    session, controller, actuator = started
    controller.screen = "proxy"
    await session.observe()
    assert (await session.act("type", target="Port", text="8888", observe_after=False)).ok
    assert (await session.act("type", target="Server", text="10.0.0.2", observe_after=False)).ok
    port, server = [c for c in actuator.calls if c[0] == "input_text"]
    assert port[2] == (598, 324)  # the TextField's centre, 0-1000
    assert server[2] == (598, 255)


@pytest.mark.asyncio
async def test_same_text_in_different_controls_stays_ambiguous(started):
    session, controller, actuator = started
    controller.screen = "jobs"
    await session.observe()
    outcome = await session.act("tap", target="Draft", observe_after=False)
    assert not outcome.ok and outcome.code == "bad_target"
    assert actuator.calls == []


@pytest.mark.asyncio
async def test_a_tap_needs_an_observation_first(started):
    session, _, actuator = started
    outcome = await session.act("tap", target=2)
    assert not outcome.ok and outcome.code == "bad_target" and "observe" in outcome.message
    assert actuator.calls == []


@pytest.mark.asyncio
async def test_a_tap_is_refused_when_the_screen_changed_since_the_observation(started):
    session, controller, actuator = started
    observation = await session.observe()
    general = next(e for e in observation.elements if e["text"] == "General")
    controller.screen = "about"  # something else happened on the device

    outcome = await session.act("tap", target=general["index"])

    assert not outcome.ok and outcome.code == "screen_changed"
    assert outcome.observation.title == "About"  # a fresh look comes back with the refusal
    assert actuator.calls == []


@pytest.mark.asyncio
async def test_other_actions_reach_the_actuator(started):
    session, _, actuator = started
    await session.observe()
    assert (await session.act("type", text="hello", observe_after=False)).ok
    assert (await session.act("swipe", direction="up", observe_after=False)).ok
    assert (await session.act("press_key", key="HOME", observe_after=False)).ok
    assert (await session.act("open_app", app="Settings", observe_after=False)).ok
    assert (await session.act("wait", duration_ms=300, observe_after=False)).ok
    names = [c[0] for c in actuator.calls]
    assert names == ["input_text", "swipe", "press_key", "manage_app", "wait_for_delay"]
    assert actuator.calls[0] == ("input_text", "hello", None)
    assert actuator.calls[1][1][1] > actuator.calls[1][2][1]  # swipe up moves the finger up
    assert actuator.calls[3] == ("manage_app", "launch", "Settings")


@pytest.mark.asyncio
async def test_an_unknown_action_is_an_error_not_an_exception(started):
    session, _, _ = started
    outcome = await session.act("dance")
    assert not outcome.ok and outcome.code == "bad_action" and "dance" in outcome.message


# --- do_step ---


@pytest.mark.asyncio
async def test_do_step_taps_the_named_row_and_reports_done_when_its_screen_appears(started):
    session, _, actuator = started
    await session.observe()
    outcome = await session.do_step("Open General")
    assert outcome.status == "done" and outcome.tapped == "General"
    assert outcome.observation.title == "General"
    assert len(actuator.calls) == 1


@pytest.mark.asyncio
async def test_do_step_leaves_an_unmatched_step_to_the_harness(started, monkeypatch):
    monkeypatch.setattr(session_module.decider, "_clef_credentials", lambda: None)
    session, _, actuator = started
    await session.observe()
    outcome = await session.do_step("Open Wi-Fi settings")
    assert outcome.status == "undecided" and outcome.tapped is None
    assert "Wi-Fi" in outcome.reason or "no element" in outcome.reason.lower()
    assert actuator.calls == []


@pytest.mark.asyncio
async def test_do_step_can_be_repeated(started):
    # The harness is the judge here, so a second identical step is deliberate.
    session, controller, actuator = started
    await session.observe()
    assert (await session.do_step("Open General")).status == "done"
    controller.screen = "settings"
    await session.observe()
    assert (await session.do_step("Open General")).status == "done"
    assert len(actuator.calls) == 2


@pytest.mark.asyncio
async def test_a_text_target_is_resolved_against_the_screen_as_it_is_now(started):
    # Bug 18: after the app moved on, tap "Map" matched the old screen's title.
    session, controller, actuator = started
    await session.observe()  # Settings
    controller.screen = "general"  # the app moved on without us looking
    outcome = await session.act("tap", target="About")
    assert outcome.ok, outcome.message
    assert outcome.observation is not None and outcome.observation.title == "About"


@pytest.mark.asyncio
async def test_a_passed_wait_leaves_no_stale_tree_for_index_taps(started):
    session, controller, actuator = started
    await session.observe()
    assert (await session.wait_for("General")).ok
    assert session.last_observation is None
    outcome = await session.act("tap", target=2)
    assert not outcome.ok and "observe first" in outcome.message


# --- alerts ---


@pytest.mark.asyncio
async def test_alert_buttons_come_from_wda_and_can_be_pressed_by_tap_or_alert(started):
    # Live: an app's UIAlertController showed no Button in the tree; the only
    # way to dismiss it was an OCR hit on 'OK'.
    session, controller, actuator = started
    controller.screen = "alert"
    controller.driver._wda = FakeWda(SCREENS_ALERT_BUTTONS)

    observation = await session.observe()
    assert observation.title == "Delete this job?"
    assert "AlertButton 'Delete'" in observation.describe()

    outcome = await session.act("tap", target="Delete")
    assert outcome.ok and outcome.message == "Pressed alert button 'Delete'."
    assert controller.driver._wda.accepted == ["Delete"]
    assert actuator.calls == []  # no blind tap at the alert's centre

    controller.driver._wda = FakeWda(["OK"])
    await session.observe()
    outcome = await session.act("alert", text="OK")
    assert outcome.ok and controller.driver._wda.accepted == ["OK"]


@pytest.mark.asyncio
async def test_alerts_need_a_wda_backend(started):
    session, _, _ = started
    outcome = await session.act("alert", text="OK")
    assert not outcome.ok and outcome.code == "failed" and "WebDriverAgent" in outcome.message


# --- wait_for ---


@pytest.mark.asyncio
async def test_a_wait_for_timeout_shows_what_is_on_screen(started, monkeypatch):
    # Live: wait_for("Jobs") timed out because a passkey sheet covered the
    # tab bar, which only a separate observe revealed.
    session, controller, actuator = started

    async def timed_out(text, wait_state, timeout_ms):
        return ActionResult.failure("wait_for_text", f"Timed out waiting for text '{text}'.")

    monkeypatch.setattr(actuator, "wait_for_text", timed_out)
    controller.screen = "about"
    outcome = await session.wait_for("Jobs", timeout_ms=100)
    assert not outcome.ok and outcome.code == "timeout"
    assert outcome.observation is not None and outcome.observation.title == "About"
    assert "Screen: About" in outcome.describe()


@pytest.mark.asyncio
async def test_wait_for_delegates_to_the_actuator(started):
    session, _, actuator = started
    result = await session.wait_for("About", timeout_ms=5000)
    assert result.ok
    assert actuator.calls == [("wait_for_text", "About", "appear", 5000)]


# --- logs ---


@pytest.mark.asyncio
async def test_each_action_names_its_step_and_the_log_lines_it_produced(
    started, tmp_path, monkeypatch
):
    # Live: the main bug only got solved after rebuilding the app three
    # times with hand-added log lines; every step should carry its own.
    session, _, _ = started
    monkeypatch.setattr(session_module, "get_temp_dir", lambda sub=None: tmp_path)
    source = tmp_path / "app.log"
    source.write_text("", encoding="utf-8")
    monkeypatch.setattr(
        session_module,
        "log_command",
        lambda *a, **k: ([sys.executable, "-c", TAIL_SCRIPT, str(source)], "fake stream"),
    )
    status = await session.start_logs(bundle_id="Flight.flightFiber")
    assert "running" in status and "process flightFiber" in status

    await session.observe()
    outcome = await session.act("tap", target="General")
    assert outcome.step == 1 and outcome.log_lines == 0
    assert "mobile_logs(action='read', since_step=1)" in outcome.describe()

    with source.open("a", encoding="utf-8") as fh:
        fh.write("about: version 26.5\n")
    deadline = asyncio.get_running_loop().time() + 5
    while session.logs is not None and session.logs.count_since(0.0) < 1:
        assert asyncio.get_running_loop().time() < deadline
        await asyncio.sleep(0.02)
    outcome = await session.act("tap", target="About")
    assert outcome.step == 2
    assert session.logs is not None and session.logs.read(since_step=1) == ["about: version 26.5"]

    first_file = session.logs.file_path
    assert "stopped" in (await session.stop_logs() or "")
    assert session.logs is None

    # Live (QA, Part 6): the device's one log file kept yesterday's lines.
    await session.start_logs(bundle_id="Flight.flightFiber")
    assert session.logs.file_path != first_file
    await session.stop_logs()
    assert (await session.act("swipe", direction="up", observe_after=False)).step is None


# --- markers and network ---


@pytest.mark.asyncio
async def test_observe_can_add_markers_found_in_the_screenshot(started, monkeypatch):
    from lunatap.harness.markers import Marker, MarkerScan

    session, _, _ = started
    seen = {}

    def fake_scan(png, exclude=None, text=None, region=None):
        seen["region"] = region
        seen["text"] = text
        return MarkerScan(
            [
                Marker(760, 1231, 740, 1210, 780, 1260, "blue", 900, 0.9),
                Marker(300, 1500, 200, 1400, 500, 1700, "grey", 9000, 0.6, count=40),
            ],
            imagery=True,
        )

    monkeypatch.setattr(session_module, "scan_markers", fake_scan)
    observation = await session.observe(detect="markers")
    assert observation.markers.pins == 41
    single, cluster = observation.elements[-2:]
    assert single["class"] == "Marker" and single["text"] == "blue pin (90%)"
    assert cluster["text"] == "cluster of ~40 grey pins (60%)"
    assert cluster["index"] == len(observation.elements)
    head = observation.describe()
    assert "Markers: 2 found in the screenshot (~41 pins, 1 cluster(s)" in head
    assert "satellite imagery" in head
    assert seen["region"] is None  # the fake tree has no Map element
    # Text boxes go along, so a "0" or the page title is never a pin.
    assert any(rect for rect in seen["text"])
    assert (await session.observe()).markers is None


@pytest.mark.asyncio
async def test_each_action_names_the_requests_made_during_it(started, tmp_path, monkeypatch):
    from lunatap.harness import mitm_addon

    session, _, actuator = started
    monkeypatch.setattr(session_module, "get_temp_dir", lambda sub=None: tmp_path)
    monkeypatch.setattr(session_module, "net_command", lambda port: (["true"], "fake proxy"))
    monkeypatch.setattr(session_module, "setup_instructions", lambda *a, **k: "setup: none")
    status = await session.start_net(port=8099)
    assert status.startswith("Network capture") and status.endswith("setup: none")
    # mitmdump gets the log path in its environment; the in-process addon
    # stand-in needs the same.
    assert session.net is not None
    monkeypatch.setenv(mitm_addon.NETLOG_ENV, str(session.net.log_path))

    await session.observe()
    outcome = await session.act("tap", target="General")
    assert outcome.step == 1 and outcome.net_requests == (0, 0)
    from tests.unit.harness.test_network import _flow

    original_click = actuator.click

    async def click_that_calls_the_api(nx, ny, times=1, delay_ms=100):
        result = await original_click(nx, ny, times, delay_ms)
        mitm_addon.response(_flow("GET", "https://api.example.com/about", 500, b"boom"))
        return result

    actuator.click = click_that_calls_the_api
    outcome = await session.act("tap", target="About")
    assert outcome.step == 2 and outcome.net_requests == (1, 1)
    assert "Network: 1 request(s) so far during step 2, 1 failed" in outcome.describe()
    assert session.net is not None and session.net.read(since_step=2, failed_only=True) == [
        "GET https://api.example.com/about -> 500 (250 ms, 4 B) 'boom'"
    ]
    assert "stopped" in (await session.stop_net() or "")

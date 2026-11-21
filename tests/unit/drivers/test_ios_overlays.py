# Copyright 2026 Google LLC
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
#
# Modified by the Lunatap Authors in 2026 (forked from google/artemis).

"""Floating bars, hidden elements, and edge-safe swipes on physical iOS.

The tree mirrors iOS 26 Settings on an iPhone 16 Pro (402x874 points, 3x):
the search field floats in a bottom Toolbar over the scrolling list, so a row
scrolled underneath it is listed by XCTest but a tap on it opens Search.
"""

import asyncio

import pytest

from lunatap.drivers.ios import wda
from lunatap.drivers.ios.physical_driver import PhysicalIosDriver

SCALE = (3.0, 3.0)
WIDTH, HEIGHT = 1206, 2622


def _node(kind, rect, *, label=None, name=None, visible=True, children=()):
    x, y, w, h = rect
    entry = {
        "type": f"XCUIElementType{kind}",
        "rect": {"x": x, "y": y, "width": w, "height": h},
        "isVisible": 1 if visible else 0,
        "children": list(children),
    }
    if label is not None:
        entry["label"] = label
    if name is not None:
        entry["name"] = name
    return entry


def _settings_tree():
    rows = [
        _node("Cell", (16, 664, 370, 57), label="Battery", name="settings.battery"),
        # Center (y=788.5) is under the bar, but its top 24pt are clear.
        _node("Cell", (16, 760, 370, 57), label="VPN", name="settings.vpn"),
        # Scrolled under the bar; XCTest already reports it as not visible.
        _node("Cell", (16, 811, 370, 57), label="General", name="settings.general", visible=False),
        # Under the bar but still reported visible: only geometry can tell.
        _node("Cell", (16, 800, 370, 57), label="Screen Time", name="settings.screentime"),
    ]
    toolbar = _node(
        "Toolbar",
        (0, 788, 402, 86),
        label="Toolbar",
        children=[_node("SearchField", (32, 803, 337, 38), label="Search", name="Search")],
    )
    table = _node("Table", (0, 0, 402, 874), children=rows)
    return _node("Application", (0, 0, 402, 874), label="Settings", children=[table, toolbar])


def _by_text(elements, text):
    return next((element for element in elements if element["text"] == text), None)


def test_overlay_rects_find_the_floating_toolbar_in_pixels():
    assert wda.wda_overlay_rects(_settings_tree(), SCALE) == [(0, 2364, 1206, 2622)]


def test_clear_rows_keep_their_center():
    elements = wda.parse_wda_elements(_settings_tree(), SCALE, WIDTH, HEIGHT)
    assert _by_text(elements, "Battery")["hit_point"] == [603, 2077]


def test_hidden_elements_are_dropped():
    elements = wda.parse_wda_elements(_settings_tree(), SCALE, WIDTH, HEIGHT)
    assert _by_text(elements, "General") is None


def test_row_covered_by_a_visible_bar_is_dropped():
    elements = wda.parse_wda_elements(_settings_tree(), SCALE, WIDTH, HEIGHT)
    assert _by_text(elements, "Screen Time") is None


def test_partly_covered_row_is_tapped_in_its_clear_part():
    elements = wda.parse_wda_elements(_settings_tree(), SCALE, WIDTH, HEIGHT)
    vpn = _by_text(elements, "VPN")
    # Clear band is 2280..2364 px (above the bar); tap its middle, not 2365.
    assert vpn["hit_point"] == [603, 2322]
    assert vpn["parsed_bounds"]["bottom"] == 2451  # reported bounds are unchanged


def test_controls_inside_the_bar_stay_tappable():
    elements = wda.parse_wda_elements(_settings_tree(), SCALE, WIDTH, HEIGHT)
    search = _by_text(elements, "Search")
    assert search is not None
    assert search["hit_point"] == [601, 2466]
    assert _by_text(elements, "Toolbar") is not None


def test_children_of_a_hidden_container_are_still_visited():
    tree = _node(
        "Other",
        (0, 0, 100, 100),
        visible=False,
        children=[_node("Button", (10, 10, 20, 20), label="OK")],
    )
    elements = wda.parse_wda_elements(tree, (1.0, 1.0), 100, 100)
    assert [element["text"] for element in elements] == ["OK"]


@pytest.fixture
def phone_driver():
    driver = PhysicalIosDriver(device_id="00008140-000000000000001C")
    driver._scale = SCALE
    driver._width, driver._height = WIDTH, HEIGHT
    driver._overlay_rects = wda.wda_overlay_rects(_settings_tree(), SCALE)
    return driver


def test_swipe_starting_on_the_toolbar_is_moved_above_it(phone_driver):
    # The agent swiped from the search bar region; that opens Search instead.
    assert phone_driver._safe_swipe_y(603, 2500) == 2364 - 36


def test_swipe_into_the_status_bar_edge_is_moved_down(phone_driver):
    assert phone_driver._safe_swipe_y(603, 10) == 180


def test_swipe_in_the_content_area_is_unchanged(phone_driver):
    assert phone_driver._safe_swipe_y(603, 1200) == 1200


def test_home_edge_is_avoided_without_any_bar(phone_driver):
    phone_driver._overlay_rects = []
    assert phone_driver._safe_swipe_y(603, 2621) == 2621 - 120


def test_swipe_is_untouched_before_any_capture():
    driver = PhysicalIosDriver(device_id="00008140-000000000000001C")
    assert driver._safe_swipe_y(10, 2621) == 2621


@pytest.mark.asyncio
async def test_paired_device_without_tunnel_is_connected_on_demand(monkeypatch):
    driver = PhysicalIosDriver(device_id="00008140-000000000000001C")
    calls = []

    async def fake_devicectl_json(*arguments):
        calls.append(arguments)
        return {"connectionProperties": {"tunnelState": "connected"}}

    monkeypatch.setattr(driver, "_devicectl_json", fake_devicectl_json)
    await driver._prepare_device(
        {"pairing_state": "paired", "connection_state": "disconnected", "name": "iPhone"}
    )
    assert calls == [("info", "details")]


@pytest.mark.asyncio
async def test_unreachable_paired_device_still_fails_clearly(monkeypatch):
    driver = PhysicalIosDriver(device_id="00008140-000000000000001C")

    async def fake_devicectl_json(*arguments):
        raise RuntimeError("device not reachable")

    monkeypatch.setattr(driver, "_devicectl_json", fake_devicectl_json)
    with pytest.raises(RuntimeError, match="is not connected"):
        await driver._prepare_device(
            {"pairing_state": "paired", "connection_state": "disconnected", "name": "iPhone"}
        )


@pytest.mark.asyncio
async def test_locked_device_fails_fast_with_unlock_hint(monkeypatch, tmp_path):
    driver = PhysicalIosDriver(device_id="00008140-000000000000001C")
    log = tmp_path / "wda.log"
    log.write_text(
        "Xcode cannot launch WebDriverAgentRunner on iPhone because the device is locked.\n"
    )

    async def no_candidates():
        return ["http://127.0.0.1:1"]

    async def fake_probe(candidates, timeout=5.0):
        return None

    async def fake_start():
        driver._wda_log_path = log

    monkeypatch.setattr(driver, "_wda_candidates", no_candidates)
    monkeypatch.setattr(driver, "_start_wda_server", fake_start)
    monkeypatch.setattr("lunatap.drivers.ios.physical_driver.probe_wda", fake_probe)
    with pytest.raises(RuntimeError, match="is locked"):
        await driver._ensure_wda()


# --- Recording and screenshots without devicectl capture (Xcode 26+) --------


def _png_bytes(size=(30, 60)):
    import io

    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", size, "white").save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.mark.asyncio
async def test_recorder_takes_frames_from_the_supplied_source(tmp_path, monkeypatch):
    from lunatap.drivers.ios import physical_recording
    from lunatap.drivers.ios.physical_recording import PhysicalIosRecorder

    async def devicectl_must_not_run(*args, **kwargs):
        raise AssertionError("devicectl capture is unavailable on Xcode 26")

    calls = 0

    async def frame_source():
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.01)
        return _png_bytes()

    monkeypatch.setattr(physical_recording, "devicectl_screenshot", devicectl_must_not_run)
    recorder = PhysicalIosRecorder("00008140-000000000000001C", frame_source=frame_source)
    session = await recorder.start(tmp_path)
    assert session.capture_width == 30 and session.capture_height == 60
    assert session.frames and session.frames[0]["path"].read_bytes().startswith(b"\x89PNG")
    session.poll_task.cancel()
    assert calls >= 1


@pytest.mark.asyncio
async def test_physical_driver_records_through_webdriveragent():
    driver = PhysicalIosDriver(device_id="00008140-000000000000001C")

    class _Wda:
        async def screenshot_png(self):
            return b"frame"

    driver._wda = _Wda()
    recorder = driver._new_recorder()
    assert await recorder._frame_source() == b"frame"


@pytest.mark.asyncio
async def test_screenshot_error_names_wda_not_the_devicectl_usage_text(monkeypatch):
    from lunatap.drivers.ios import physical_driver

    driver = PhysicalIosDriver(device_id="00008140-000000000000001C")

    class _Wda:
        async def screenshot_png(self):
            raise RuntimeError("session gone")

    async def broken_devicectl(*args, **kwargs):
        raise RuntimeError("Error: Unknown option '--device'\nUsage: devicectl device ...")

    driver._wda = _Wda()
    monkeypatch.setattr(physical_driver, "devicectl_screenshot", broken_devicectl)
    with pytest.raises(RuntimeError) as raised:
        await driver._screenshot_png()
    message = str(raised.value)
    assert "WebDriverAgent screenshot failed: session gone" in message
    assert "Usage:" not in message


# --- A WebDriverAgent that stops answering mid-read is retried once ----------


@pytest.mark.asyncio
@pytest.mark.parametrize(("method", "retried"), [("GET", True), ("POST", False)])
async def test_unavailable_wda_retries_reads_but_not_writes(monkeypatch, method, retried):
    client = wda.WdaClient("http://wda.test:8100")
    client._session_id = "old"
    calls = []

    def fake_sync_request(req_method, path, payload, timeout):
        calls.append((req_method, path))
        if len(calls) == 1:
            raise wda.WdaUnavailableError("WebDriverAgent did not answer in time")
        return {"ok": True}

    async def fake_reopen(path, suppress_errors=False):
        client._session_id = "new"
        return path.replace("/session/old", "/session/new")

    monkeypatch.setattr(client, "_sync_request", fake_sync_request)
    monkeypatch.setattr(client, "_reopen_session_with_path", fake_reopen)
    if retried:
        assert await client._request(method, "/session/old/source") == {"ok": True}
        assert calls == [(method, "/session/old/source"), (method, "/session/new/source")]
    else:
        with pytest.raises(wda.WdaUnavailableError):
            await client._request(method, "/session/old/actions", {})
        assert len(calls) == 1


@pytest.mark.asyncio
async def test_unavailable_wda_status_fails_fast_without_reopening(monkeypatch):
    # Regression: /status is used to open sessions; retrying it re-entered the
    # reopen lock and hung connect() forever while WDA was still starting.
    client = wda.WdaClient("http://wda.test:8100")

    def unavailable(req_method, path, payload, timeout):
        raise wda.WdaUnavailableError("WebDriverAgent did not answer /status")

    async def reopen_must_not_run(*args, **kwargs):
        raise AssertionError("status probes must not trigger session recovery")

    monkeypatch.setattr(client, "_sync_request", unavailable)
    monkeypatch.setattr(client, "_reopen_session_with_path", reopen_must_not_run)
    assert await asyncio.wait_for(client.status(timeout=0.1), 2) is None

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

"""A hanging screen read is reported as a stuck app, not an unreachable WDA (QA bug 23)."""

import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from lunatap.drivers.ios import physical_driver
from lunatap.drivers.ios.physical_driver import PhysicalIosDriver
from lunatap.drivers.ios.wda import WdaBusyError, WdaClient, WdaUnavailableError


class _HangingWda(BaseHTTPRequestHandler):
    """Answers like WDA, except that the screen read never returns in time."""

    calls: list[str] = []

    def log_message(self, *args):
        pass

    def _reply(self, value):
        body = json.dumps({"value": value, "sessionId": "s1"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        type(self).calls.append(f"GET {self.path}")
        if self.path.startswith("/session/s1/source"):
            time.sleep(1.0)
            return
        if self.path == "/wda/activeAppInfo":
            self._reply({"bundleId": "com.apple.Preferences"})
            return
        self._reply({"ready": True})

    def do_POST(self):
        type(self).calls.append(f"POST {self.path}")
        self._reply({})

    def do_DELETE(self):
        type(self).calls.append(f"DELETE {self.path}")
        self._reply({})


@pytest.fixture
def hanging_wda():
    _HangingWda.calls = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _HangingWda)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


@pytest.mark.asyncio
async def test_a_hanging_source_names_the_app_and_keeps_the_session(hanging_wda):
    client = WdaClient(hanging_wda, timeout=0.3)
    client._session_id = "s1"
    with pytest.raises(WdaBusyError) as caught:
        await client._request("GET", "/session/s1/source?format=json")
    message = str(caught.value)
    assert "is up" in message and "reading the screen of com.apple.Preferences" in message
    assert "open_app" in message and "HOME" in message
    assert "unreachable" not in message
    assert caught.value.bundle_id == "com.apple.Preferences"
    # No recovery: the session is neither deleted nor recreated.
    assert not any(c.startswith(("DELETE", "POST")) for c in _HangingWda.calls)
    assert client.session_id == "s1"


@pytest.mark.asyncio
async def test_a_closed_port_is_still_unreachable():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    client = WdaClient(f"http://127.0.0.1:{port}", timeout=0.3)
    with pytest.raises(WdaUnavailableError) as caught:
        await client._request("GET", "/status")
    assert not isinstance(caught.value, WdaBusyError)
    assert "unreachable" in str(caught.value)


class _SessionOnlyWda:
    async def open_session(self, adopt_existing=False):
        return "s1"


@pytest.mark.asyncio
async def test_connect_survives_a_stuck_first_screen(monkeypatch):
    """Live: each call restarted WDA because connect() tore it down after the read hung."""
    driver = PhysicalIosDriver(device_id="00008140-00000000000000AB")
    torn_down = []

    async def none(*args, **kwargs):
        return None

    async def stuck(*args, **kwargs):
        raise WdaBusyError("WebDriverAgent is up, but reading the screen timed out.")

    async def disconnect():
        torn_down.append(True)

    async def resolve():
        return {"udid": driver._device_id}

    async def ensure():
        return _SessionOnlyWda()

    monkeypatch.setattr(driver, "_resolve_device", resolve)
    monkeypatch.setattr(driver, "_prepare_device", none)
    monkeypatch.setattr(driver, "_ensure_wda", ensure)
    monkeypatch.setattr(driver, "_verify_wda_device", none)
    monkeypatch.setattr(driver, "get_screen_data", stuck)
    monkeypatch.setattr(driver, "disconnect", disconnect)

    await driver.connect()
    assert driver._session_key == "s1"
    assert not torn_down


@pytest.mark.asyncio
async def test_a_busy_screenshot_does_not_fall_back_to_devicectl(monkeypatch):
    driver = PhysicalIosDriver(device_id="00008140-00000000000000AB")

    class _BusyShot:
        async def screenshot_png(self):
            raise WdaBusyError("WebDriverAgent is up, but taking a screenshot timed out.")

    async def fallback(*args, **kwargs):
        raise AssertionError("devicectl fallback must not run for a busy WDA")

    driver._wda = _BusyShot()
    monkeypatch.setattr(physical_driver, "devicectl_screenshot", fallback)
    with pytest.raises(WdaBusyError):
        await driver._screenshot_png()

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

"""Device serials a QA agent is likely to pass, and the errors when they miss."""

import pytest

from lunatap.drivers.ios import physical_driver
from lunatap.drivers.ios.discovery import device_matches_identifier, parse_devicectl_devices
from lunatap.drivers.ios.physical_driver import PhysicalIosDriver, WdaLaunchError
from lunatap.drivers.ios.wda import WdaClient
from lunatap.drivers.ios.xcode_driver import XcodeSimulatorDriver

UDID = "00008140-00000000000000AB"
CORE_ID = "11111111-2222-4333-8444-555555555555"
PAYLOAD = {
    "result": {
        "devices": [
            {
                "identifier": CORE_ID,
                "deviceProperties": {"name": "iPhone (2)", "osVersionNumber": "26.1"},
                "hardwareProperties": {"udid": UDID, "platform": "iOS", "reality": "physical"},
                "connectionProperties": {"tunnelState": "connected", "pairingState": "paired"},
            }
        ]
    }
}


def test_the_corevice_identifier_from_devicectl_is_accepted_as_a_serial():
    # Live: `xcrun devicectl list devices` prints the CoreDevice identifier
    # first; passing it gave "Unavailable iOS simulator UDID".
    (device,) = parse_devicectl_devices(PAYLOAD)
    assert device["identifier"] == CORE_ID
    assert device_matches_identifier(device, CORE_ID.lower())
    assert device_matches_identifier(device, UDID)
    assert device_matches_identifier(device, "iPhone (2)")
    assert not device_matches_identifier(device, "iphone (2)")  # names stay exact


@pytest.mark.asyncio
async def test_an_unknown_serial_lists_what_would_have_matched(monkeypatch):
    async def core_devices(force_refresh=False):
        return parse_devicectl_devices(PAYLOAD)

    monkeypatch.setattr("lunatap.drivers.ios.discovery.list_core_devices", core_devices)
    hint = await XcodeSimulatorDriver._known_devices_hint(
        [{"udid": "SIM-1", "name": "iPhone 17 Pro Max", "state": "Booted"}]
    )
    assert "iPhone 17 Pro Max (SIM-1, Booted)" in hint
    assert f"iPhone (2) ({UDID})" in hint


def test_a_wda_launch_failure_carries_its_cause_and_who_fixes_it(tmp_path, monkeypatch):
    monkeypatch.setattr(physical_driver, "resign_command", lambda: "bash resign.sh")
    driver = PhysicalIosDriver(device_id=UDID)
    log = tmp_path / "wda.log"
    log.write_text(
        "Failed to install embedded profile for com.x.xctrunner : 0xe8008011 "
        "(This provisioning profile has expired.)\n"
    )
    driver._wda_log_path = log

    error = driver._launch_error("xcodebuild hosting WebDriverAgent exited with code 65")

    assert isinstance(error, WdaLaunchError)
    assert error.code == "wda_profile_expired" and error.needs_human is False
    assert "Fix: bash resign.sh" in str(error) and str(log) in str(error)

    log.write_text("Failure Reason: Developer App Certificate is not trusted on this device.\n")
    error = driver._launch_error("exited with code 65")
    assert error.code == "wda_cert_untrusted" and error.needs_human
    assert "VPN & Device Management" in str(error)
    # Nothing recognisable: the old generic hint stays.
    log.write_text("Testing started\n")
    error = driver._launch_error("exited with code 65")
    assert error.code == "wda_launch_failed" and "Build it once" in str(error)


def test_a_wda_start_timeout_says_so_instead_of_the_build_instructions(tmp_path, monkeypatch):
    monkeypatch.setattr(physical_driver, "resign_command", lambda: "bash resign.sh")
    driver = PhysicalIosDriver(device_id=UDID)
    log = tmp_path / "wda.log"
    log.write_text("Testing started\n")
    driver._wda_log_path = log

    error = driver._launch_error("WebDriverAgent start timed out after 45s", timed_out=True)
    assert error.code == "wda_start_timeout"
    assert "xcodebuild log:" in str(error) and "Build it once" not in str(error)

    # Two starts at once: xcodebuild says so, and the fix is a retry, not a rebuild.
    log.write_text("** BUILD INTERRUPTED **\n")
    error = driver._launch_error("exited with code 65")
    assert error.code == "wda_start_interrupted" and "retry once" in str(error)


def test_the_simulators_anchor_app_death_counts_as_a_lost_session():
    # Live: first observe on the simulator → HTTP 404 "stale element
    # reference ... Application 'com.apple.Preferences' is not present ...
    # kAXErrorServerNotFound"; a plain retry worked.
    client = WdaClient("http://127.0.0.1:8100")
    message = (
        "HTTP 404 stale element reference: Application 'com.apple.Preferences' is not "
        "present (kAXErrorServerNotFound)"
    )
    assert client._is_session_loss(RuntimeError(message))
    assert not client._is_session_loss(RuntimeError("stale element reference: Button gone"))

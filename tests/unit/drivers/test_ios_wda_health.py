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

"""WebDriverAgent signing health and launch-failure classification."""

import asyncio
import plistlib
from datetime import datetime, timedelta, timezone, UTC

from lunatap.core.diagnostics.probes.wda_signing_probe import WdaSigningProbe
from lunatap.core.diagnostics.schema import ProbeStatus
from lunatap.drivers.ios import wda_health
from lunatap.drivers.ios.wda_health import (
    classify_wda_log,
    resign_command,
    signing_status,
)

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
RESIGN = (
    "xcodebuild build-for-testing -project WDA.xcodeproj -destination id=0000 DEVELOPMENT_TEAM=T"
)


def _profile(expires: datetime) -> bytes:
    plist = plistlib.dumps(
        {
            "Name": "iOS Team Provisioning Profile: com.example.WebDriverAgentRunner.xctrunner",
            "TeamIdentifier": ["UV8D7H9YGP"],
            "ExpirationDate": expires.replace(tzinfo=None),
            "CreationDate": (expires - timedelta(days=7)).replace(tzinfo=None),
        }
    )
    # A real profile is a CMS envelope around the plist: binary before and after.
    return b"\x30\x82\x1a\x00junk" + plist + b"\x00\x01signature"


def _wda_dir(tmp_path, expires: datetime | None):
    root = tmp_path / "wda"
    if expires is not None:
        app = (
            root
            / "device/DerivedData/Build/Products/Debug-iphoneos/WebDriverAgentRunner-Runner.app"
        )
        app.mkdir(parents=True)
        (app / "embedded.mobileprovision").write_bytes(_profile(expires))
    (root).mkdir(exist_ok=True)
    (root / "build-device.log").write_text(
        "Command line invocation:\n    " + RESIGN + "\n\nBuild settings from command line:\n"
    )
    return root


# --- signing_status ---


def test_a_fresh_profile_is_valid_with_days_left(tmp_path):
    status = signing_status(_wda_dir(tmp_path, NOW + timedelta(days=6)), now=NOW)
    assert status.state == "valid"
    assert status.team_id == "UV8D7H9YGP"
    assert status.expires_at == NOW + timedelta(days=6)
    assert "valid for" in status.describe()


def test_a_profile_expiring_within_two_days_warns(tmp_path):
    status = signing_status(_wda_dir(tmp_path, NOW + timedelta(hours=20)), now=NOW)
    assert status.state == "expiring"
    assert "expires in 20 h" in status.describe()


def test_an_expired_profile_is_reported_with_its_date(tmp_path):
    # Live: the profile expired 2026-11-11 and diagnose still said "ready".
    status = signing_status(_wda_dir(tmp_path, NOW - timedelta(days=9)), now=NOW)
    assert status.state == "expired"
    assert "expired on 2026-11-11" in status.describe()


def test_no_device_build_is_missing_not_an_error(tmp_path):
    status = signing_status(_wda_dir(tmp_path, None), now=NOW)
    assert status.state == "missing" and status.expires_at is None


def test_the_resign_command_is_the_one_from_the_build_log(tmp_path):
    assert resign_command(_wda_dir(tmp_path, None)) == RESIGN
    assert resign_command(tmp_path / "nowhere") == "bash scripts/setup_ios_env.sh"


# --- classify_wda_log ---

EXPIRED_LOG = """
Testing failed:
    WebDriverAgentRunner-Runner.app encountered an error (Failed to install embedded profile for
    com.example.WebDriverAgentRunner.xctrunner : 0xe8008011 (This provisioning profile has expired.))
** TEST EXECUTE FAILED **
"""
UNTRUSTED_LOG = """
error: Failed to launch the runner: The operation couldn't be completed.
    Failure Reason: Developer App Certificate is not trusted on this device.
    Recovery Suggestion: Trust the certificate in Settings.
"""


def test_an_expired_profile_in_the_log_names_the_cause_and_the_resign_command():
    failure = classify_wda_log(EXPIRED_LOG, resign=RESIGN)
    assert failure is not None
    assert failure.code == "wda_profile_expired" and not failure.needs_human
    assert failure.fix == RESIGN
    assert "provisioning profile has expired" in failure.reason
    assert "fix on the Mac" in failure.describe()


def test_an_untrusted_certificate_needs_a_person_at_the_phone():
    # Live: the message told the QA agent to rebuild WDA, which was wrong.
    failure = classify_wda_log(UNTRUSTED_LOG, resign=RESIGN)
    assert failure is not None
    assert failure.code == "wda_cert_untrusted" and failure.needs_human
    assert "VPN & Device Management" in failure.fix
    assert "needs a person at the phone" in failure.describe()


def test_a_locked_device_and_disabled_ui_automation_are_human_steps():
    locked = classify_wda_log("xcodebuild: error: Failed because the device is locked.")
    assert locked is not None and locked.code == "device_locked" and locked.needs_human
    automation = classify_wda_log("Please enable UI Automation in Settings > Developer")
    assert automation is not None and automation.code == "ui_automation_disabled"


def test_an_unknown_failure_surfaces_the_first_failure_reason_line():
    failure = classify_wda_log("stuff\n  Failure Reason: The runner crashed at launch.\nmore")
    assert failure is not None
    assert failure.code == "wda_launch_failed"
    assert failure.reason == "The runner crashed at launch."


def test_a_quiet_log_is_not_classified():
    assert classify_wda_log("Build succeeded\nTesting started") is None
    assert classify_wda_log("") is None


# --- the probe ---


def test_the_probe_fails_on_an_expired_profile_with_the_resign_command(tmp_path, monkeypatch):
    root = _wda_dir(tmp_path, NOW - timedelta(days=1))
    monkeypatch.setattr(wda_health, "DEFAULT_WDA_DIR", root)
    monkeypatch.setattr(
        wda_health, "signing_status", lambda wda_dir=root, now=None: signing_status(root, now=NOW)
    )
    monkeypatch.setattr(wda_health, "resign_command", lambda wda_dir=root: resign_command(root))
    result = asyncio.run(WdaSigningProbe().probe())
    assert result.status is ProbeStatus.FAIL and not result.is_blocker
    assert result.actions[0].action_type == "command" and result.actions[0].payload == RESIGN
    assert result.metadata["state"] == "expired"


def test_the_probe_passes_without_a_device_build(tmp_path, monkeypatch):
    root = _wda_dir(tmp_path, None)
    monkeypatch.setattr(
        wda_health, "signing_status", lambda wda_dir=root, now=None: signing_status(root, now=NOW)
    )
    result = asyncio.run(WdaSigningProbe().probe())
    assert result.status is ProbeStatus.PASS and result.summary == "No Device Build"

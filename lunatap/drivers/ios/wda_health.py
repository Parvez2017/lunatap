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

"""Health of the WebDriverAgent build for physical devices.

Two cheap file reads answer the questions a failed WDA launch leaves open:
is the runner's signing profile still valid (a free Personal Team profile
expires every seven days), and what does the xcodebuild log say the real
reason was. Nothing here talks to a device.
"""

from __future__ import annotations

import plistlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone, UTC
from pathlib import Path

# Where scripts/setup_ios_env.sh builds WebDriverAgent for physical devices.
DEFAULT_WDA_DIR = Path("~/.artemis/wda").expanduser()
RUNNER_APP = "WebDriverAgentRunner-Runner.app"
#: A profile this close to expiry is reported as a warning before it bites.
EXPIRY_WARNING = timedelta(hours=48)
#: The build log the setup script writes; its first command is the one to rerun.
BUILD_LOG = "build-device.log"
FALLBACK_RESIGN_COMMAND = "bash scripts/setup_ios_env.sh"


@dataclass(frozen=True)
class SigningStatus:
    """The signing profile embedded in the device WDA runner."""

    state: str  # "valid" | "expiring" | "expired" | "missing"
    profile_path: str | None
    expires_at: datetime | None
    team_id: str | None
    profile_name: str | None
    checked_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def days_left(self) -> float | None:
        if self.expires_at is None:
            return None
        return (self.expires_at - self.checked_at).total_seconds() / 86400

    def describe(self) -> str:
        if self.state == "missing":
            return "No WebDriverAgent device build found."
        when = self.expires_at.strftime("%Y-%m-%d %H:%M UTC") if self.expires_at else "?"
        team = f" (team {self.team_id})" if self.team_id else ""
        if self.state == "expired":
            return f"WebDriverAgent signing profile expired on {when}{team}."
        days = self.days_left or 0.0
        if self.state == "expiring":
            return f"WebDriverAgent signing profile expires in {days * 24:.0f} h ({when}){team}."
        return f"WebDriverAgent signing profile valid for {days:.1f} days ({when}){team}."


def parse_mobileprovision(raw: bytes) -> dict:
    """The plist inside a ``.mobileprovision`` (a CMS envelope around XML)."""
    start, end = raw.find(b"<?xml"), raw.find(b"</plist>")
    if start < 0 or end < 0:
        raise ValueError("no plist inside the provisioning profile")
    return plistlib.loads(raw[start : end + len(b"</plist>")])


def find_runner_profile(wda_dir: Path = DEFAULT_WDA_DIR) -> Path | None:
    """The embedded profile of the newest device runner build, if any."""
    products = wda_dir / "device" / "DerivedData" / "Build" / "Products"
    if not products.is_dir():
        return None
    candidates = [
        path
        for path in products.glob(f"*-iphoneos/{RUNNER_APP}/embedded.mobileprovision")
        if path.is_file()
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime)


def signing_status(
    wda_dir: Path = DEFAULT_WDA_DIR, *, now: datetime | None = None
) -> SigningStatus:
    """Read the device runner's profile and judge it against ``now``."""
    profile = find_runner_profile(wda_dir)
    if profile is None:
        return SigningStatus("missing", None, None, None, None)
    try:
        plist = parse_mobileprovision(profile.read_bytes())
    except (OSError, ValueError, plistlib.InvalidFileException):
        return SigningStatus("missing", str(profile), None, None, None)
    expires = plist.get("ExpirationDate")
    if not isinstance(expires, datetime):
        return SigningStatus("missing", str(profile), None, None, None)
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=UTC)
    teams = plist.get("TeamIdentifier") or []
    team = str(teams[0]) if isinstance(teams, list) and teams else None
    name = plist.get("Name")
    moment = now or datetime.now(UTC)
    if expires <= moment:
        state = "expired"
    elif expires - moment <= EXPIRY_WARNING:
        state = "expiring"
    else:
        state = "valid"
    return SigningStatus(
        state, str(profile), expires, team, str(name) if name else None, checked_at=moment
    )


def resign_command(wda_dir: Path = DEFAULT_WDA_DIR) -> str:
    """The exact xcodebuild line the setup script ran, or the script itself."""
    log = wda_dir / BUILD_LOG
    try:
        lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return FALLBACK_RESIGN_COMMAND
    for position, line in enumerate(lines):
        if line.strip() == "Command line invocation:" and position + 1 < len(lines):
            command = lines[position + 1].strip()
            if "xcodebuild" in command:
                return command
    return FALLBACK_RESIGN_COMMAND


# --------------------------------------------------------------------------- #
# Why did xcodebuild fail to host WDA?
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class WdaFailure:
    """The first real reason in an xcodebuild log hosting WebDriverAgent."""

    code: str
    reason: str
    fix: str
    needs_human: bool  # a step only someone holding the phone can do

    def describe(self) -> str:
        who = "needs a person at the phone" if self.needs_human else "fix on the Mac"
        return f"{self.reason} [cause: {self.code}, {who}]. Fix: {self.fix}"


TRUST_FIX = (
    "On the phone: Settings > General > VPN & Device Management > tap the developer "
    "app certificate > Trust, then retry."
)
UNLOCK_FIX = (
    "Unlock the phone and keep it unlocked (Settings > Display & Brightness > "
    "Auto-Lock > Never while testing), then retry."
)
UI_AUTOMATION_FIX = (
    "On the phone: Settings > Developer > turn on UI Automation (enter the passcode "
    "if asked), then retry."
)

# (code, markers in the log, fix, needs_human). First hit wins, so the
# specific signing causes come before the generic ones.
_KNOWN: tuple[tuple[str, tuple[str, ...], str | None, bool], ...] = (
    (
        "wda_profile_expired",
        ("provisioning profile has expired", "0xe8008011"),
        None,  # the resign command, filled in at call time
        False,
    ),
    (
        "wda_cert_untrusted",
        ("certificate is not trusted", "developer app certificate", "0xe8008015"),
        TRUST_FIX,
        True,
    ),
    (
        "ui_automation_disabled",
        ("enable ui automation", "ui automation is not enabled", "uiautomation"),
        UI_AUTOMATION_FIX,
        True,
    ),
    (
        "device_locked",
        ("because the device is locked", "the device is locked"),
        UNLOCK_FIX,
        True,
    ),
    (
        "wda_signing_missing",
        (
            "no profiles for",
            "requires a development team",
            "no signing certificate",
            "doesn't match any valid certificate",
        ),
        None,
        False,
    ),
    (
        "wda_start_interrupted",
        ("** build interrupted **", "** test execute interrupted **"),
        "Another WebDriverAgent start was running on this device at the same time; retry once.",
        False,
    ),
    (
        "device_unsupported",
        ("unable to find a destination matching", "is not supported by xcode"),
        "Update Xcode (or its iOS device support files) to a version that supports this "
        "device's iOS version.",
        False,
    ),
)
_REASON_RE = re.compile(
    r"^\s*(?:Failure Reason|Recovery Suggestion|error)\s*:\s*(?P<reason>.+?)\s*$",
    re.IGNORECASE | re.MULTILINE,
)


def classify_wda_log(text: str, *, resign: str | None = None) -> WdaFailure | None:
    """Name the first known failure in an xcodebuild log, or ``None``.

    ``resign`` is the command that re-signs WDA (see ``resign_command``); it is
    the fix for the signing causes.
    """
    if not text:
        return None
    folded = text.casefold()
    for code, markers, fix, needs_human in _KNOWN:
        hit = next((marker for marker in markers if marker in folded), None)
        if hit is None:
            continue
        reason = _line_containing(text, hit) or hit
        return WdaFailure(code, reason, fix or resign or FALLBACK_RESIGN_COMMAND, needs_human)
    match = _REASON_RE.search(text)
    if match:
        return WdaFailure(
            "wda_launch_failed",
            match.group("reason")[:300],
            "Read the log around that line; rerun after fixing what it names.",
            False,
        )
    return None


def _line_containing(text: str, marker: str) -> str | None:
    for line in text.splitlines():
        if marker in line.casefold():
            return line.strip()[:300]
    return None

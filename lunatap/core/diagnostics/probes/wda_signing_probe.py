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

"""Is the WebDriverAgent build for physical iPhones still signed?"""

from __future__ import annotations

import asyncio

from lunatap.core.diagnostics.probes.base import BaseProbe
from lunatap.core.diagnostics.schema import (
    ProbeAction,
    ProbeCategory,
    ProbeResult,
    ProbeStatus,
)
from lunatap.drivers.ios import wda_health

PROBE_ID = "ios_wda_signing"
TITLE = "iOS WebDriverAgent Signing"


class WdaSigningProbe(BaseProbe):
    """Reads the runner's embedded profile; a free team's expires weekly.

    Not a blocker on its own: simulators never need it. ``mobile_diagnose``
    turns an expired profile into a blocker when the caller targets a
    physical device.
    """

    @property
    def probe_id(self) -> str:
        return PROBE_ID

    @property
    def category(self) -> ProbeCategory:
        return ProbeCategory.DEVICE

    @property
    def is_blocker(self) -> bool:
        return False

    async def probe(self) -> ProbeResult:
        status = await asyncio.to_thread(wda_health.signing_status)
        metadata = {
            "state": status.state,
            "profile_path": status.profile_path,
            "expires_at": status.expires_at.isoformat() if status.expires_at else None,
            "days_left": round(status.days_left, 2) if status.days_left is not None else None,
            "team_id": status.team_id,
        }
        if status.state == "missing":
            return ProbeResult(
                id=self.probe_id,
                category=self.category,
                title=TITLE,
                status=ProbeStatus.PASS,
                is_blocker=False,
                summary="No Device Build",
                description=(
                    "No WebDriverAgent device build under ~/.artemis/wda/device; only needed "
                    "for physical iPhones/iPads (scripts/setup_ios_env.sh builds it)."
                ),
                metadata=metadata,
            )
        if status.state == "valid":
            return ProbeResult(
                id=self.probe_id,
                category=self.category,
                title=TITLE,
                status=ProbeStatus.PASS,
                is_blocker=False,
                summary=f"Valid ({status.days_left or 0:.1f} days left)",
                description=status.describe(),
                metadata=metadata,
            )
        resign = await asyncio.to_thread(wda_health.resign_command)
        return ProbeResult(
            id=self.probe_id,
            category=self.category,
            title=TITLE,
            status=ProbeStatus.FAIL if status.state == "expired" else ProbeStatus.WARN,
            is_blocker=False,
            summary="Profile Expired" if status.state == "expired" else "Profile Expiring",
            description=(
                status.describe()
                + " Physical devices cannot run WebDriverAgent until it is re-signed "
                "(the command below is the one that built it)."
            ),
            metadata=metadata,
            actions=[ProbeAction(action_type="command", label="Re-sign WDA", payload=resign)],
        )

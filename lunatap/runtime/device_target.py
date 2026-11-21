# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# Modified by the Lunatap Authors in 2026 (forked from google/artemis).

"""Task-target primitives and platform vocabulary (iOS only).

An :class:`IosTarget` is an immutable execution snapshot, so a queued or
running task never silently moves to another device when the selection
changes in the console.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Any, MutableMapping

#: Environment variable naming a worker's lock scope (read by DeviceExecutionLock).
#: The value is historical; it no longer refers to an ADB endpoint.
LOCK_SCOPE_ENV = "ARTEMIS_ADB_ENDPOINT_ID"


# User-facing platform vocabulary shared by CLI, console, daemon, and MCP.
# Lunatap drives iOS only; "android" is recognised so the error can say so.
SUPPORTED_PLATFORMS: tuple[str, ...] = ("ios",)
DEFAULT_PLATFORM = "ios"
UNSUPPORTED_PLATFORM_MESSAGE = "Lunatap supports iOS only (simulators and iPhones/iPads)."


def normalize_device_platform(
    value: object,
    *,
    default: str = DEFAULT_PLATFORM,
    strict: bool = True,
) -> str:
    """Canonicalize a platform token to ``"ios"``.

    Strips whitespace and case-folds. ``strict=True`` (entry points) raises
    ``ValueError`` on anything else; ``strict=False`` (fail-open internals)
    falls back to *default*. Surfaces adapt the ``ValueError`` to their own
    error type (HTTP 400, Typer error, blocked verdict, ...).
    """
    text = str(value).strip().lower() if value is not None else ""
    if not text:
        return default
    if text in SUPPORTED_PLATFORMS:
        return text
    if strict:
        raise ValueError(f"Unsupported platform '{value}'. {UNSUPPORTED_PLATFORM_MESSAGE}")
    return default


def device_pool_for(platform: object):
    """The device pool for a platform token (lazy imports avoid a cycle).

    Both pools expose the same admission surface (``validate_explicit_serial``,
    ``validate_explicit_serial_async``, ``select_device_async``,
    ``list_devices_async``).
    """
    del platform  # iOS is the only platform; kept for call-site compatibility
    from lunatap.runtime.ios_device_pool import ios_device_pool

    return ios_device_pool


def target_for_platform(
    platform: object,
    serial: str | None = None,
    endpoint: object | None = None,
) -> IosTarget:
    """Build the execution target for a platform; ``lock_scope`` comes with it."""
    del platform, endpoint  # iOS is the only platform
    return IosTarget(serial=serial)


# Execution lock scope shared by every iOS device task. The device UDID
# distinguishes devices inside the scope; "ios" keeps an iOS lock file
# namespaced away from any Android serial of the same text.
IOS_LOCK_SCOPE = "ios"


@dataclass(frozen=True, slots=True)
class IosTarget:
    """An iOS device UDID (simulator or paired physical device)."""

    serial: str | None = None

    @property
    def platform(self) -> str:
        return "ios"

    @property
    def lock_scope(self) -> str:
        return IOS_LOCK_SCOPE

    @property
    def lock_key(self) -> str:
        return f"{self.lock_scope}/{self.serial or 'pending'}"

    def apply_to_environment(
        self,
        environment: MutableMapping[str, str] | None = None,
    ) -> MutableMapping[str, str]:
        target = environment if environment is not None else os.environ
        target[LOCK_SCOPE_ENV] = self.lock_scope
        # An iOS worker never touches ADB: a stale serial must not leak in.
        target.pop("ADB_DEVICE_SERIAL", None)
        # The worker's device id comes from the queue target, not a parent env.
        target.pop("ARTEMIS_DEVICE_ID", None)
        return target

    def to_dict(self) -> dict[str, Any]:
        return {"platform": self.platform, "serial": self.serial}

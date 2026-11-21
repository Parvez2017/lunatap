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

"""Lunatap: an AI agent that tests iOS apps on simulators and iPhones."""

from lunatap.config.env_aliases import apply_env_aliases

# Before any module reads ARTEMIS_* settings (see config/env_aliases.py).
apply_env_aliases()

from lunatap._version import __version__
from lunatap import _legacy_imports

# Old import paths of the modules moved to third_party/ (deprecated).
_legacy_imports.install()

from lunatap.interfaces.sdk.client import ArtemisClient, ConcurrencyMode
from lunatap.interfaces.sdk.task import StreamEvent, StreamEventType, Task
from artemis_client import Capabilities, Device, TaskHandle, TaskResult
from lunatap.sdk.agent import Agent
from lunatap.sdk.builders import Builders
from lunatap.drivers.base import BaseDeviceDriver

__all__ = [
    "__version__",
    "ArtemisClient",
    "Capabilities",
    "ConcurrencyMode",
    "Device",
    "StreamEvent",
    "StreamEventType",
    "Agent",
    "Builders",
    "Task",
    "TaskHandle",
    "TaskResult",
    "BaseDeviceDriver",
]

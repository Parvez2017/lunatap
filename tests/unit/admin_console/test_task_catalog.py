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

"""Console task presets: iOS apps, recommendation and the catalog endpoint."""

import pytest

from apps.admin_console.routers.tasks import get_task_catalog, get_task_presets
from apps.admin_console.services.task_preset_catalog import (
    APP_REGISTRY,
    PRESET_TASK_CATALOG,
)


def test_every_preset_app_is_a_registered_ios_bundle():
    for preset in PRESET_TASK_CATALOG:
        for app in preset.apps:
            assert app.pkg in APP_REGISTRY, (preset.id, app.pkg)
        assert not any("android" in pkg for pkg in preset.required_packages), preset.id


@pytest.mark.asyncio
async def test_catalog_endpoint_returns_tasks_and_registry():
    # Regression: the endpoint called a missing get_app_registry() and
    # model_dump() on dicts, so it always failed.
    catalog = await get_task_catalog()
    assert len(catalog["tasks"]) == len(PRESET_TASK_CATALOG)
    assert catalog["app_registry"]["com.apple.Preferences"]["name"] == "Settings"


@pytest.mark.asyncio
async def test_presets_prefer_installed_apps():
    presets = await get_task_presets(packages="com.apple.Preferences", limit=3)
    assert presets and all(p["is_device_matched"] for p in presets)
    assert all("com.apple.Preferences" in p["required_packages"] for p in presets)

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

from unittest.mock import AsyncMock, MagicMock

from lunatap.context import ArtemisContext
from lunatap.drivers.base import BaseDeviceDriver
from lunatap.tools.base import ArtemisTool
from lunatap.tools.mobile.read_hierarchy import (
    GetUIHierarchy,
    GetUiHierarchy,
    GetUiHierarchyArgs,
    GetUiHierarchyTool,
    get_ui_hierarchy,
    get_ui_hierarchy_tool,
    ui_hierarchy_wrapper,
)
import pytest


@pytest.fixture
def mock_ctx():
    ctx = MagicMock(spec=ArtemisContext)
    ctx.ui_adb_client = MagicMock()
    ctx.ui_adb_client.get_hierarchy.return_value = "<hierarchy><node text='Home'/></hierarchy>"
    return ctx


@pytest.fixture
def mock_driver():
    driver = MagicMock(spec=BaseDeviceDriver)
    screen_data = MagicMock()
    screen_data.ui_hierarchy_xml = "<hierarchy><node text='Settings'/></hierarchy>"
    driver.get_screen_data = AsyncMock(return_value=screen_data)
    return driver


def test_get_ui_hierarchy_tool_subclass():
    """Verify GetUiHierarchyTool is a subclass of ArtemisTool."""
    assert issubclass(GetUiHierarchyTool, ArtemisTool)
    assert issubclass(GetUiHierarchy, ArtemisTool)
    assert issubclass(GetUIHierarchy, ArtemisTool)
    assert isinstance(get_ui_hierarchy, ArtemisTool)
    assert isinstance(get_ui_hierarchy, GetUiHierarchyTool)

    assert get_ui_hierarchy.name == "get_ui_hierarchy"
    assert get_ui_hierarchy.category == "diagnostic"
    assert get_ui_hierarchy.args_schema == GetUiHierarchyArgs

    # GenAI FunctionDeclaration export
    declaration = get_ui_hierarchy.to_genai_declaration()
    assert declaration.name == "get_ui_hierarchy"

    # Wrapper check
    assert ui_hierarchy_wrapper is not None
    assert ui_hierarchy_wrapper.tool_fn_getter == get_ui_hierarchy_tool


@pytest.mark.asyncio
async def test_get_ui_hierarchy_direct_execution_with_driver_screen_data(mock_driver):
    """Verify direct execution with driver's get_screen_data."""
    result = await get_ui_hierarchy.execute(driver=mock_driver)
    mock_driver.get_screen_data.assert_called_once()
    assert "<hierarchy><node text='Settings'/></hierarchy>" in result


@pytest.mark.asyncio
async def test_get_ui_hierarchy_direct_execution_with_driver_get_hierarchy():
    """Verify direct execution with driver's get_ui_hierarchy."""
    driver = MagicMock(spec=BaseDeviceDriver)
    del driver.get_screen_data
    driver.get_ui_hierarchy = AsyncMock(
        return_value="<hierarchy><node text='DirectDriver'/></hierarchy>"
    )
    result = await get_ui_hierarchy.execute(driver=driver)
    driver.get_ui_hierarchy.assert_called_once()
    assert "<hierarchy><node text='DirectDriver'/></hierarchy>" in result


@pytest.mark.asyncio
async def test_get_ui_hierarchy_no_client_or_driver():
    """Verify error handling when no client or driver is provided."""
    result = await get_ui_hierarchy.execute()
    assert "Error retrieving UI hierarchy: No UI Automator client or driver provided." in result

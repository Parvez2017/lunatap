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

"""Unit tests for ToolchainResolver and binary discovery."""

from pathlib import Path
from unittest.mock import patch

from lunatap.toolchain import TOOLS, ToolchainResolver, find_ffmpeg


def test_toolchain_env_override(tmp_path):
    """Verify explicit environment variables override tool path resolution."""
    resolver = ToolchainResolver()
    fake_ffmpeg = tmp_path / "fake_ffmpeg_binary"
    fake_ffmpeg.touch()

    with patch.dict("os.environ", {"ARTEMIS_FFMPEG_PATH": str(fake_ffmpeg)}):
        resolved = resolver.resolve("ffmpeg", force_refresh=True)
        assert resolved == str(Path(fake_ffmpeg).resolve())


def test_toolchain_helper_functions():
    """Verify toolchain discovery helpers return strings; Android tools are gone."""
    ffmpeg = find_ffmpeg()
    assert isinstance(ffmpeg, str) and len(ffmpeg) > 0
    assert "adb" not in TOOLS and "scrcpy" not in TOOLS


def test_toolchain_is_installed_lookup():
    """Verify is_installed check on known and unknown tools."""
    resolver = ToolchainResolver()
    assert resolver.is_installed("non_existent_tool_xyz123") is False

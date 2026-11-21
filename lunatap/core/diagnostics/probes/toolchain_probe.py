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

"""Toolchain and Video Recording Auxiliary Probe."""

from lunatap.core.diagnostics.probes.base import BaseProbe
from lunatap.core.diagnostics.schema import (
    ProbeAction,
    ProbeCategory,
    ProbeResult,
    ProbeStatus,
)
import shutil

from lunatap.utils.video import get_ffmpeg_path


class ToolchainProbe(BaseProbe):
    """Probe checking the video recording toolchain (ffmpeg, ffprobe)."""

    @property
    def probe_id(self) -> str:
        return "toolchain"

    @property
    def category(self) -> ProbeCategory:
        return ProbeCategory.TOOLCHAIN

    @property
    def is_blocker(self) -> bool:
        return False

    async def probe(self) -> ProbeResult:
        # ffmpeg ships with the imageio-ffmpeg dependency; ffprobe (segment
        # metadata for recordings and replays) comes with a system ffmpeg.
        ffmpeg_path = get_ffmpeg_path()
        ffprobe_path = shutil.which("ffprobe")
        metadata = {
            "ffmpeg": bool(ffmpeg_path),
            "ffmpeg_path": ffmpeg_path,
            "ffprobe": ffprobe_path is not None,
            "ffprobe_path": ffprobe_path,
        }
        if ffprobe_path:
            return ProbeResult(
                id=self.probe_id,
                category=self.category,
                title="Video Recording Toolchain",
                status=ProbeStatus.PASS,
                is_blocker=self.is_blocker,
                summary="Ready (FFmpeg + ffprobe)",
                description=(
                    f"FFmpeg ({ffmpeg_path}) and ffprobe ({ffprobe_path}) are available; "
                    "screen recordings and replays are active."
                ),
                metadata=metadata,
            )
        return ProbeResult(
            id=self.probe_id,
            category=self.category,
            title="Video Recording Toolchain",
            status=ProbeStatus.WARN,
            is_blocker=self.is_blocker,
            summary="Missing ffprobe",
            description=(
                "Recording works with the bundled FFmpeg, but ffprobe is missing, so recording "
                "segments and replays lose their duration and timeline metadata."
            ),
            metadata=metadata,
            actions=[
                ProbeAction(
                    action_type="command",
                    label="Install via Homebrew",
                    payload="brew install ffmpeg",
                )
            ],
        )

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

import asyncio
import os
from pathlib import Path
import tempfile
import time
from typing import TYPE_CHECKING
from uuid import uuid4

from lunatap.config.paths import get_temp_dir
from lunatap.context import ArtemisContext
from lunatap.drivers.factory import get_driver
from lunatap.drivers.ios.recording import IosRecordingSession
from lunatap.utils.video import (
    render_timeline_clip,
)
from third_party.mobile_use.controllers.unified_controller import UnifiedMobileControllerBase
from third_party.mobile_use.utils.logger import get_logger
from third_party.mobile_use.utils.video import (
    DEFAULT_MAX_DURATION_SECONDS,
    RecordingSession,
    VideoRecordingResult,
    get_active_session,
    no_active_recording,
    recording_already_active,
    remove_active_session,
    set_active_session,
)

if TYPE_CHECKING:
    from lunatap.drivers.ios.xcode_driver import XcodeSimulatorDriver

logger = get_logger(__name__)


class UnifiedMobileController(UnifiedMobileControllerBase):
    def __init__(self, ctx: ArtemisContext):
        super().__init__(ctx, get_driver(ctx))
        self._segment_cache: dict[tuple[str, int, float, float], VideoRecordingResult] = {}

    async def open_url(self, url: str) -> bool:
        return await self._driver.open_url(url)

    async def erase_text(self, nb_chars: int | None = None) -> bool:
        # No native clear/Backspace key exists on iOS; the driver raises
        # NotImplementedError for unsupported clear operations.
        del nb_chars
        return await self._driver.input_text("", clear_existing=True)

    def _get_device_id(self) -> str:
        if self.ctx and self.ctx.device and self.ctx.device.device_id:
            return self.ctx.device.device_id
        return getattr(self._driver, "device_id", None) or "default"

    async def extract_segment_metadata(
        self,
        start_time: float,
        end_time: float | None = None,
        output_path: Path | None = None,
    ) -> VideoRecordingResult:
        """Get a video segment for a specific time range (relative to video start)."""
        device_id = self._get_device_id()

        # Handle mock driver
        if (
            getattr(self._driver, "is_mock", False)
            or getattr(getattr(self.ctx, "device", None), "mobile_platform", None) == "mock"
            or os.environ.get("ARTEMIS_MOCK_DRIVER") == "1"
        ):
            mock_video = Path("/tmp/mock_recording.mp4")
            return VideoRecordingResult(
                success=True,
                video_path=mock_video,
                message="Mock segment extracted",
            )

        session = get_active_session(device_id)
        if not session:
            return no_active_recording(device_id)

        cache_key = None
        if end_time is not None:
            cache_key = (
                str(session.video_id),
                session.generation,
                round(start_time, 1),
                round(end_time, 1),
            )
            cached_res = self._segment_cache.get(cache_key)
            if (
                cached_res
                and cached_res.success
                and cached_res.video_path
                and cached_res.video_path.exists()
            ):
                logger.info(
                    "Reusing generation-scoped trimmed video segment for range "
                    f"{cache_key[2]}s to {cache_key[3]}s"
                )
                return cached_res

        if isinstance(session, IosRecordingSession):
            result = await self._extract_ios_segment(session, start_time, end_time, output_path)
            if (
                cache_key is not None
                and result.success
                and result.video_path
                and result.video_path.exists()
            ):
                # Sealing bumps the generation; key the entry under the
                # post-seal generation a repeated lookup will observe.
                self._segment_cache[
                    (cache_key[0], session.generation, cache_key[2], cache_key[3])
                ] = result
            return result

        return VideoRecordingResult(
            success=False, message="No iOS recording session is active for this device."
        )

    def _record_recording_failure(self, session: RecordingSession, message: str) -> None:
        if self.ctx and self.ctx.data_engine:
            self.ctx.data_engine.record_video_failure(
                video_id=session.video_id,
                device_id=session.device_id,
                local_video_path=session.local_video_path,
                start_time=session.start_time,
                error=message,
            )

    async def start_video_recording(
        self,
        output_dir: Path | None = None,
        max_duration_seconds: int = DEFAULT_MAX_DURATION_SECONDS,
    ) -> VideoRecordingResult:
        """Start screen recording using the platform's native capture."""
        self._segment_cache.clear()
        if self.ctx.device.mobile_platform == "ios":
            return await self._start_ios_recording(output_dir, max_duration_seconds)
        device_id = self._get_device_id()

        # Check mock driver first
        if (
            getattr(self._driver, "is_mock", False)
            or getattr(getattr(self.ctx, "device", None), "mobile_platform", None) == "mock"
            or os.environ.get("ARTEMIS_MOCK_DRIVER") == "1"
        ):
            await self._driver.start_video_recording(output_dir)
            return VideoRecordingResult(success=True, message="Mock recording started")

        return VideoRecordingResult(
            success=False, message="Screen recording is only available on iOS devices."
        )

    async def stop_video_recording(self) -> VideoRecordingResult:
        """Stop recording and return the finalized MP4 video file."""
        self._segment_cache.clear()
        if self.ctx.device.mobile_platform == "ios":
            return await self._stop_ios_recording()
        device_id = self._get_device_id()

        # Check mock driver first
        if (
            getattr(self._driver, "is_mock", False)
            or getattr(getattr(self.ctx, "device", None), "mobile_platform", None) == "mock"
            or os.environ.get("ARTEMIS_MOCK_DRIVER") == "1"
        ):
            p = await self._driver.stop_video_recording()
            return VideoRecordingResult(
                success=True,
                video_path=Path(p) if p else None,
                message="Mock recording stopped",
            )

        return no_active_recording(device_id)

    def _ios_recording_driver(self) -> "XcodeSimulatorDriver | None":
        """The active driver when it supports native iOS capture, else ``None``."""
        from lunatap.drivers.ios.xcode_driver import XcodeSimulatorDriver

        return self._driver if isinstance(self._driver, XcodeSimulatorDriver) else None

    async def _start_ios_recording(
        self, output_dir: Path | None, max_duration_seconds: int
    ) -> VideoRecordingResult:
        """Start native capture through the iOS driver (simctl or devicectl)."""
        device_id = self._get_device_id()
        if already_active := recording_already_active(device_id):
            return already_active
        driver = self._ios_recording_driver()
        if driver is None:
            return VideoRecordingResult(
                success=False,
                message="iOS recording requires the iOS driver.",
            )
        try:
            await driver.start_video_recording(
                output_dir, max_duration_seconds=max_duration_seconds
            )
        except Exception as exc:
            session = driver.recording_session
            if session is None:
                session = IosRecordingSession(
                    video_id=uuid4(),
                    device_id=device_id,
                    start_time=time.time(),
                    local_video_path=(Path(output_dir) / "recording.mov" if output_dir else None),
                    is_active=False,
                )
            if session.data_engine_start_time is None and self.ctx and self.ctx.data_engine:
                session.data_engine_start_time = (
                    self.ctx.data_engine.session_start_time or session.start_time
                )
            if self.ctx and self.ctx.data_engine:
                self.ctx.data_engine.record_video_start(
                    video_id=session.video_id,
                    device_id=device_id,
                    local_video_path=session.local_video_path or "",
                    start_time=session.start_time,
                )
            self._record_recording_failure(session, f"iOS recording failed to start: {exc}")
            return VideoRecordingResult(
                success=False, message=f"iOS recording failed to start: {exc}"
            )
        session = driver.recording_session
        if session is None:
            return VideoRecordingResult(success=False, message="iOS recorder returned no session")
        session.data_engine_start_time = (
            self.ctx.data_engine.session_start_time
            if (self.ctx and self.ctx.data_engine)
            else session.start_time
        )
        set_active_session(device_id, session)
        if self.ctx and self.ctx.data_engine:
            self.ctx.data_engine.record_video_start(
                video_id=session.video_id,
                device_id=device_id,
                local_video_path=session.local_video_path,
                start_time=session.start_time,
            )
        warning = "; ".join(session.errors) or None
        return VideoRecordingResult(
            success=True,
            message=f"iOS recording started on {device_id}",
            video_id=session.video_id,
            generation=session.generation,
            sealed_until=session.sealed_until,
            source_revision=f"{session.video_id}:{session.generation}:active",
            warning=warning,
        )

    async def _stop_ios_recording(self) -> VideoRecordingResult:
        """Finalize the driver's recorder and report the sealed manifest."""
        device_id = self._get_device_id()
        driver = self._ios_recording_driver()
        session = get_active_session(device_id)
        if session is None:
            session = driver.recording_session if driver is not None else None
        if session is None or session.video_id is None:
            return no_active_recording(device_id)
        if driver is None:
            message = "iOS recording stop requires the Xcode simulator driver."
            self._record_recording_failure(session, message)
            return VideoRecordingResult(success=False, message=message)

        async def _finalize() -> str | None:
            session.is_active = False
            return await driver.stop_video_recording()

        try:
            final_path = await asyncio.shield(_finalize())
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._record_recording_failure(session, f"iOS recording stop failed: {exc}")
            return VideoRecordingResult(success=False, message=f"iOS recording stop failed: {exc}")
        finally:
            remove_active_session(device_id)

        missing = [
            record
            for record in session.segments
            if not (
                record.get("conversion_done")
                and Path(record["output_path"]).exists()
                and Path(record["output_path"]).stat().st_size > 0
            )
        ]
        if final_path is None or not session.segments or missing:
            message = "; ".join(session.errors) or "iOS recording finalized with no valid segments"
            self._record_recording_failure(session, message)
            return VideoRecordingResult(success=False, message=message)

        final_video_path = Path(final_path)
        if self.ctx and self.ctx.data_engine:
            self.ctx.data_engine.record_video_stop(
                video_id=session.video_id,
                device_id=device_id,
                local_video_path=final_video_path,
                start_time=session.start_time,
                end_time=session.start_time + session.sealed_until,
            )
        warning = "; ".join(session.errors) or None
        return VideoRecordingResult(
            success=True,
            message=f"iOS recording stopped, saved {len(session.segments)} segments",
            video_path=final_video_path,
            video_id=session.video_id,
            generation=session.generation,
            sealed_until=session.sealed_until,
            source_revision=f"{session.video_id}:{session.generation}:ready",
            warning=warning,
        )

    async def _extract_ios_segment(
        self,
        session: IosRecordingSession,
        start_time: float,
        end_time: float | None,
        output_path: Path | None,
    ) -> VideoRecordingResult:
        """Render a clip from sealed iOS MP4 segments only.

        The open .mov is never handed to FFmpeg: the driver seals the segment
        at the requested recording-relative end first, then finalized segments
        render through the shared timeline renderer. Recorder restarts leave
        their gaps black rather than stretching adjacent frames.
        """
        device_id = self._get_device_id()
        anchor = session.data_engine_start_time
        shift = (session.start_time - anchor) if anchor is not None else 0.0
        recording_start = float(start_time) - shift
        recording_end = float(end_time) - shift if end_time is not None else None

        driver = self._ios_recording_driver()
        if session.is_active and recording_end is not None and driver is not None:
            await driver.seal_recording_segment(through_time=recording_end)
        if session.conversion_tasks:
            await asyncio.gather(*session.conversion_tasks, return_exceptions=True)

        warning = None
        available_end = session.sealed_until
        if recording_end is None or recording_end > available_end:
            if recording_end is not None:
                warning = (
                    "Requested end clipped to latest sealed iOS capture "
                    f"({available_end + shift:.1f}s of session time)"
                )
            recording_end = available_end

        records = [dict(record) for record in session.segments]
        first_capture = min((float(record["start"]) for record in records), default=None)
        if first_capture is not None and recording_start < first_capture:
            leading = "Requested start preceded the first captured frame; clipped to capture start"
            warning = f"{warning}; {leading}" if warning else leading
            recording_start = first_capture

        if not records or recording_end <= recording_start:
            message = "Requested range does not overlap sealed iOS recording"
            self._record_recording_failure(session, message)
            return VideoRecordingResult(success=False, message=message)

        timeline = [
            {
                "path": str(record["output_path"]),
                "start": float(record["start"]),
                "end": float(record["end"]),
            }
            for record in records
            if record.get("conversion_done")
            and Path(record["output_path"]).exists()
            and float(record["end"]) > recording_start
            and float(record["start"]) < recording_end
        ]
        timeline.sort(key=lambda segment: segment["start"])
        if not timeline:
            message = "No finalized iOS MP4 segments overlap the requested range"
            self._record_recording_failure(session, message)
            return VideoRecordingResult(success=False, message=message)

        trim_output_path = (
            Path(output_path)
            if output_path is not None
            else Path(tempfile.mkdtemp(prefix="video_trimmed_", dir=get_temp_dir("trimmed_videos")))
            / "segment.mp4"
        )
        success = await render_timeline_clip(
            timeline, recording_start, recording_end, trim_output_path
        )
        if not success or not trim_output_path.exists():
            message = "Failed to render clip from sealed iOS segments"
            self._record_recording_failure(session, message)
            return VideoRecordingResult(success=False, message=message)

        actual_start = recording_start + shift
        actual_end = recording_end + shift
        file_size_mb = trim_output_path.stat().st_size / (1024 * 1024)
        return VideoRecordingResult(
            success=True,
            message=(
                f"iOS clip rendered for session range {actual_start:.1f}s to {actual_end:.1f}s"
            ),
            video_path=trim_output_path,
            file_size_mb=round(file_size_mb, 2),
            duration_seconds=round(recording_end - recording_start, 2),
            actual_start_relative_time=actual_start,
            warning=warning,
            video_id=session.video_id,
            generation=session.generation,
            sealed_until=session.sealed_until,
            source_revision=(f"{session.video_id}:{session.generation}:{round(actual_end, 3)}"),
        )

    async def cleanup(self) -> None:
        await self._driver.disconnect()

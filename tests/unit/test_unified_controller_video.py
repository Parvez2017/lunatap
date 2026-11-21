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

"""Unit tests for UnifiedMobileController video recording and playback features."""

import asyncio
import collections
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
import cv2

from lunatap.context import ArtemisContext
from lunatap.controllers.unified_controller import UnifiedMobileController
from lunatap.drivers.mock.mock_driver import MockDeviceDriver
from lunatap.utils.video import (
    extract_frames_at_timestamps,
    get_ffmpeg_path,
    normalize_recording_to_mp4,
    plan_timeline_pieces,
    render_timeline_clip,
)
from third_party.mobile_use.utils.video import (
    RecordingSession,
    extract_audio_from_video,
    get_active_session,
    remove_active_session,
    set_active_session,
)


@pytest.mark.asyncio
async def test_audio_extraction_rejects_silent_video_without_running_ffmpeg(tmp_path):
    silent_video = tmp_path / "silent.mp4"
    silent_video.write_bytes(b"video")
    probe = MagicMock(returncode=0)
    probe.communicate = AsyncMock(return_value=(b"", b""))

    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=probe)) as spawn:
        with pytest.raises(ValueError, match="no audio stream"):
            await extract_audio_from_video(silent_video)

    assert spawn.await_count == 1


def test_targeted_frame_extraction_uses_requested_timestamps():
    fixture = Path(__file__).parents[1] / "tools" / "inputs" / "recording.mp4"
    frames = extract_frames_at_timestamps(
        fixture,
        [0.0, 0.2, 0.4, 0.2, -1.0],
        max_frames=3,
        max_dimension=320,
    )

    assert [timestamp for timestamp, _ in frames] == [0.0, 0.2, 0.4]
    assert all(data.startswith(b"\xff\xd8") for _, data in frames)


@pytest.mark.asyncio
async def test_normalize_recording_handles_resolution_change(tmp_path):
    """A portrait/landscape H.264 track must become one fixed-size MP4."""
    ffmpeg = get_ffmpeg_path()
    portrait = tmp_path / "portrait.mkv"
    landscape = tmp_path / "landscape.mkv"
    dynamic = tmp_path / "dynamic.mkv"
    output = tmp_path / "recording.mp4"

    for size, path in (("108x242", portrait), ("242x108", landscape)):
        subprocess.run(
            [
                ffmpeg,
                "-y",
                "-f",
                "lavfi",
                "-i",
                f"testsrc=size={size}:rate=10:duration=0.5",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                str(path),
            ],
            check=True,
            capture_output=True,
        )

    concat_file = tmp_path / "segments.txt"
    concat_file.write_text(
        f"file '{portrait.as_posix()}'\nfile '{landscape.as_posix()}'\n",
        encoding="utf-8",
    )
    subprocess.run(
        [
            ffmpeg,
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_file),
            "-c",
            "copy",
            str(dynamic),
        ],
        check=True,
        capture_output=True,
    )

    assert await normalize_recording_to_mp4(dynamic, output, 108, 242)

    capture = cv2.VideoCapture(str(output))
    dimensions = set()
    frame_count = 0
    while True:
        ok, frame = capture.read()
        if not ok:
            break
        frame_count += 1
        dimensions.add((frame.shape[1], frame.shape[0]))
    capture.release()

    assert frame_count >= 8
    assert dimensions == {(108, 242)}


def _make_solid_clip(ffmpeg: str, color: str, size: str, duration: float, path) -> None:
    """Encode a single-colour test clip (``size`` is ``WxH``) at 15 fps."""
    subprocess.run(
        [
            ffmpeg,
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"color={color}:size={size}:rate=15:duration={duration}",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(path),
        ],
        check=True,
        capture_output=True,
    )


def _read_frames(path) -> list:
    """Decode every frame of ``path`` with OpenCV, in order."""
    capture = cv2.VideoCapture(str(path))
    frames = []
    while True:
        ok, frame = capture.read()
        if not ok:
            break
        frames.append(frame)
    capture.release()
    return frames


@pytest.mark.asyncio
async def test_analyzer_clip_stays_continuous_across_orientation_segments(tmp_path):
    """The analyzer must still receive one decodable MP4 across a rotation."""
    ffmpeg = get_ffmpeg_path()
    portrait = tmp_path / "recording.mkv"
    landscape = tmp_path / "recording_001.mkv"
    output = tmp_path / "agent_clip.mp4"
    _make_solid_clip(ffmpeg, "red", "108x242", 1.0, portrait)
    _make_solid_clip(ffmpeg, "blue", "242x108", 1.0, landscape)

    assert await render_timeline_clip(
        [
            {"path": portrait, "start": 0.0, "end": 1.0},
            {"path": landscape, "start": 1.0, "end": 2.0},
        ],
        0.25,
        1.75,
        output,
        canvas_width=180,
        canvas_height=320,
    )

    frames = _read_frames(output)
    assert {(frame.shape[1], frame.shape[0]) for frame in frames} == {(180, 320)}
    assert len(frames) >= 20


@pytest.mark.asyncio
async def test_analyzer_clip_keeps_timeline_time_across_restart_gap(tmp_path):
    """After a rotation the clip must not run ahead of the recording timeline.

    Two 1s segments with a 1.5s restart hole between them; the requested 3s
    window must render as 3s of video with black frames where the hole was,
    so ``start_time + frame_offset`` still names the true recording time.
    """
    ffmpeg = get_ffmpeg_path()
    portrait = tmp_path / "recording.mkv"
    landscape = tmp_path / "recording_001.mkv"
    _make_solid_clip(ffmpeg, "red", "108x242", 1.0, portrait)
    _make_solid_clip(ffmpeg, "blue", "242x108", 1.0, landscape)
    segments = [
        {"path": portrait, "start": 0.0, "end": 1.0},
        {"path": landscape, "start": 2.5, "end": 3.5},
    ]

    output = tmp_path / "agent_clip.mp4"
    assert await render_timeline_clip(
        segments, 0.25, 3.25, output, canvas_width=180, canvas_height=320
    )
    frames = _read_frames(output)
    # 3.0s at 15 fps; allow one frame of encoder slack on either side.
    assert 43 <= len(frames) <= 47

    def is_black(frame) -> bool:
        return frame.max() < 40

    def is_blue(frame) -> bool:
        b, g, r = frame[160, 90]
        return b > 150 and r < 80 and g < 80

    # t=0.5s -> red segment, t=1.5s -> restart hole, t=3.0s -> blue segment.
    assert not is_black(frames[7]) and not is_blue(frames[7])
    assert is_black(frames[22])
    assert is_blue(frames[-1])

    # A window opening inside the hole leads with black, then blue at the
    # expected timeline offset (2.5s - 1.5s = 1.0s into the clip).
    output2 = tmp_path / "agent_clip2.mp4"
    assert await render_timeline_clip(
        segments, 1.5, 3.25, output2, canvas_width=180, canvas_height=320
    )
    frames2 = _read_frames(output2)
    assert 24 <= len(frames2) <= 28
    assert is_black(frames2[3])
    assert is_blue(frames2[20])


def test_data_engine_video_lifecycle(tmp_path):
    from lunatap.data_engine.engine import DataEngine
    from lunatap.context import ArtemisContext

    mock_c = MagicMock(spec=ArtemisContext)
    mock_c.execution_setup = MagicMock(traces_path=str(tmp_path / "traces"))
    mock_c.device = None
    engine = DataEngine(mock_c)
    session_id = engine.start_session(goal="Test Video Engine")

    vid = uuid4()
    vpath = tmp_path / "recording.mp4"
    vpath.write_bytes(b"video bytes")

    # Start video recording
    engine.record_video_start(vid, "device-1", vpath)
    rec = engine.storage.get_video_recording(vid)
    assert rec is not None
    assert rec.device_id == "device-1"

    # Stop video recording
    engine.record_video_stop(vid, "device-1", vpath, time.time() - 10.0, time.time())
    sess = engine.storage.get_session(session_id)
    assert sess.video_filepath == str(vpath)

    # Move video path on finalize
    new_vpath = tmp_path / "archived" / "recording.mp4"
    new_vpath.parent.mkdir(parents=True, exist_ok=True)
    new_vpath.write_bytes(b"video bytes")
    engine.update_video_path(new_vpath)

    sess_updated = engine.storage.get_session(session_id)
    assert sess_updated.video_filepath == str(new_vpath)


# --- iOS native recording through the shared controller contract ---


class _FakeIosStderr:
    def __init__(self, lines=None, eof: bool = False):
        self._lines = collections.deque(lines if lines is not None else [b"Recording started\n"])
        self._eof = asyncio.Event()
        if eof:
            self._eof.set()

    async def readline(self):
        if self._lines:
            return self._lines.popleft()
        await self._eof.wait()
        return b""


class _FakeIosRecorderProcess:
    def __init__(self, stderr=None):
        self.stderr = stderr or _FakeIosStderr()
        self.returncode = None
        self.signals = []
        self._exit = asyncio.Event()

    def send_signal(self, sig):
        self.signals.append(sig)
        self.returncode = 0
        self._exit.set()

    def terminate(self):
        self.returncode = -15
        self._exit.set()

    def kill(self):
        self.returncode = -9
        self._exit.set()

    async def wait(self):
        await self._exit.wait()
        return self.returncode


@pytest.fixture
def ios_ctx():
    from lunatap.drivers.ios.xcode_driver import XcodeSimulatorDriver

    ctx = MagicMock(spec=ArtemisContext)
    ctx.device = MagicMock()
    ctx.device.device_id = "sim-udid-1"
    ctx.device.mobile_platform = "ios"
    ctx.data_engine = MagicMock()
    ctx.data_engine.current_session_id = uuid4()
    ctx.data_engine.session_start_time = time.time() - 1.0
    ctx.data_engine.storage = MagicMock()
    driver = XcodeSimulatorDriver(device_id="sim-udid-1")
    driver._session_key = "test-session"
    driver._bridge = SimpleNamespace(
        connected=True, call=AsyncMock(), close=AsyncMock(), start=AsyncMock()
    )
    ctx._active_driver = driver
    return ctx


@pytest.fixture
def ios_recording_seams(monkeypatch):
    """Fake the simctl child process, display probe, and MP4 finalization."""
    from lunatap.drivers.ios import recording as rec
    from lunatap.utils import video as video_utils

    procs: list[_FakeIosRecorderProcess] = []

    async def spawn(*argv, **kwargs):
        proc = _FakeIosRecorderProcess()
        procs.append(proc)
        return proc

    async def fake_finalize(source_path, output_path, width, height, span):
        Path(output_path).write_bytes(b"fake mp4")
        return True

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(rec, "probe_display_dimensions", AsyncMock(return_value=(1206, 2622)))
    monkeypatch.setattr(rec, "finalize_mov_to_mp4", fake_finalize)
    monkeypatch.setattr(
        video_utils,
        "probe_video_segment",
        AsyncMock(return_value={"duration": 1.0, "width": 100, "height": 200}),
    )
    monkeypatch.setattr(rec, "WATCHDOG_INTERVAL_SECONDS", 0.01)
    return procs


@pytest.mark.asyncio
async def test_ios_start_registers_driver_session_and_publishes_de_anchor(
    ios_ctx, ios_recording_seams, tmp_path
):
    remove_active_session("sim-udid-1")
    controller = UnifiedMobileController(ios_ctx)

    res = await controller.start_video_recording(output_dir=tmp_path)

    try:
        assert res.success is True
        session = get_active_session("sim-udid-1")
        assert session is not None
        # Controller and driver share one recording session/process.
        assert session is ios_ctx._active_driver.recording_session
        assert session.data_engine_start_time == ios_ctx.data_engine.session_start_time
        ios_ctx.data_engine.record_video_start.assert_called_once()
        assert res.video_id == session.video_id

        duplicate = await controller.start_video_recording(output_dir=tmp_path)
        assert duplicate.success is False
    finally:
        remove_active_session("sim-udid-1")
        await ios_ctx._active_driver.stop_video_recording()


@pytest.mark.asyncio
async def test_ios_start_failure_publishes_start_and_failure(
    ios_ctx, ios_recording_seams, tmp_path, monkeypatch
):
    from lunatap.drivers.ios import recording as rec

    async def dead_spawn(*argv, **kwargs):
        return _FakeIosRecorderProcess(_FakeIosStderr(lines=[], eof=True))

    monkeypatch.setattr(asyncio, "create_subprocess_exec", dead_spawn)
    remove_active_session("sim-udid-1")
    controller = UnifiedMobileController(ios_ctx)

    res = await controller.start_video_recording(output_dir=tmp_path)

    assert res.success is False
    assert "failed to start" in res.message
    ios_ctx.data_engine.record_video_start.assert_called_once()
    ios_ctx.data_engine.record_video_failure.assert_called_once()
    assert get_active_session("sim-udid-1") is None
    # The driver's failed session is still inspectable.
    assert ios_ctx._active_driver.recording_session is not None


@pytest.mark.asyncio
async def test_ios_stop_records_stop_metadata_and_clears_registry(
    ios_ctx, ios_recording_seams, tmp_path
):
    remove_active_session("sim-udid-1")
    controller = UnifiedMobileController(ios_ctx)
    await controller.start_video_recording(output_dir=tmp_path)

    res = await controller.stop_video_recording()

    assert res.success is True
    assert res.video_path == tmp_path / "recording.mp4"
    assert res.source_revision and res.source_revision.endswith(":ready")
    ios_ctx.data_engine.record_video_stop.assert_called_once()
    assert get_active_session("sim-udid-1") is None


@pytest.mark.asyncio
async def test_ios_stop_failure_reports_unsuccessful_result(
    ios_ctx, ios_recording_seams, tmp_path, monkeypatch
):
    from lunatap.drivers.ios import recording as rec

    monkeypatch.setattr(rec, "finalize_mov_to_mp4", AsyncMock(return_value=False))
    remove_active_session("sim-udid-1")
    controller = UnifiedMobileController(ios_ctx)
    await controller.start_video_recording(output_dir=tmp_path)

    res = await controller.stop_video_recording()

    assert res.success is False
    ios_ctx.data_engine.record_video_failure.assert_called()
    assert get_active_session("sim-udid-1") is None
    # The failed session is retained with actionable errors for reporting.
    retained = ios_ctx._active_driver.recording_session
    assert retained is not None and retained.errors


@pytest.mark.asyncio
async def test_ios_extract_seals_then_renders_only_final_mp4(
    ios_ctx, ios_recording_seams, tmp_path
):
    remove_active_session("sim-udid-1")
    controller = UnifiedMobileController(ios_ctx)
    await controller.start_video_recording(output_dir=tmp_path)
    await asyncio.sleep(0.05)
    session = get_active_session("sim-udid-1")
    seal_spy = AsyncMock(wraps=ios_ctx._active_driver.seal_recording_segment)
    ios_ctx._active_driver.seal_recording_segment = seal_spy

    rendered = {}

    async def fake_render(segments, start, end, output_path, **kwargs):
        rendered["segments"] = segments
        rendered["range"] = (start, end)
        Path(output_path).write_bytes(b"clip")
        return True

    output_path = tmp_path / "clip.mp4"
    shift = session.start_time - session.data_engine_start_time
    with patch(
        "lunatap.controllers.unified_controller.render_timeline_clip",
        AsyncMock(side_effect=fake_render),
    ) as render:
        res = await controller.extract_segment_metadata(
            start_time=shift + 0.0, end_time=shift + 0.02, output_path=output_path
        )
        assert res.success is True
        assert res.video_path == output_path
        seal_spy.assert_awaited_once()
        # Only finalized MP4 segment paths are rendered — never the open .mov.
        assert rendered["segments"]
        assert all(s["path"].endswith(".mp4") for s in rendered["segments"])
        assert all(".mov" not in s["path"] for s in rendered["segments"])

        # Generation-scoped cache: same range reuses the clip without render.
        res2 = await controller.extract_segment_metadata(
            start_time=shift + 0.0, end_time=shift + 0.02, output_path=output_path
        )
        assert res2.success is True
        assert render.await_count == 1

    await controller._driver.stop_video_recording()
    remove_active_session("sim-udid-1")


@pytest.mark.asyncio
async def test_ios_extract_rejects_range_outside_sealed_capture(
    ios_ctx, ios_recording_seams, tmp_path
):
    remove_active_session("sim-udid-1")
    controller = UnifiedMobileController(ios_ctx)
    await controller.start_video_recording(output_dir=tmp_path)
    session = get_active_session("sim-udid-1")
    shift = session.start_time - session.data_engine_start_time

    with patch(
        "lunatap.controllers.unified_controller.render_timeline_clip",
        AsyncMock(return_value=True),
    ) as render:
        res = await controller.extract_segment_metadata(
            start_time=shift + 50.0, end_time=shift + 55.0
        )

    assert res.success is False
    assert "overlap" in res.message
    render.assert_not_awaited()
    await controller._driver.stop_video_recording()
    remove_active_session("sim-udid-1")


def test_timeline_pieces_fill_restart_gaps(tmp_path):
    """Holes between segments (and a window starting in one) become black gaps."""
    first = tmp_path / "recording.mkv"
    second = tmp_path / "recording_001.mkv"
    first.write_bytes(b"x")
    second.write_bytes(b"x")
    segments = [
        {"path": first, "start": 0.0, "end": 10.0},
        {"path": second, "start": 11.5, "end": 20.0},  # a restart took 1.5s
    ]

    pieces = plan_timeline_pieces(segments, 8.0, 14.0)
    assert pieces == [
        ("file", first, 8.0, 10.0),
        ("gap", pytest.approx(1.5)),
        ("file", second, 0.0, pytest.approx(2.5)),
    ]

    # Window opening inside the hole: the clip's t=0 is still the requested start.
    pieces = plan_timeline_pieces(segments, 10.5, 13.0)
    assert pieces == [("gap", pytest.approx(1.0)), ("file", second, 0.0, pytest.approx(1.5))]

    # Sub-epsilon seams are rounding noise, not gaps.
    seam = [
        {"path": first, "start": 0.0, "end": 10.0},
        {"path": second, "start": 10.02, "end": 20.0},
    ]
    assert [p[0] for p in plan_timeline_pieces(seam, 9.0, 11.0)] == ["file", "file"]

    assert plan_timeline_pieces(segments, 10.1, 11.4) == []

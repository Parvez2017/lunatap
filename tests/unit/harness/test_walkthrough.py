# Copyright 2026 Google LLC
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
#
# Modified by the Lunatap Authors in 2026 (forked from google/artemis).

"""Walkthroughs: captioned steps over a screen recording, rendered to a shareable MP4."""

import asyncio
import io
import json
import subprocess
from pathlib import Path

import pytest
from PIL import Image

from lunatap.harness import walkthrough as wt
from lunatap.utils.video import get_ffmpeg_path, probe_video_segment


@pytest.fixture(autouse=True)
def _walkthroughs_in_tmp(tmp_path, monkeypatch):
    monkeypatch.setenv(wt.WALKTHROUGHS_DIR_ENV, str(tmp_path / "walkthroughs"))


def test_captions_read_like_instructions():
    assert wt.step_caption("tap", target="Jobs") == "Tap “Jobs”"
    assert wt.step_caption("tap", target=7) == "Tap"  # an index means nothing to a viewer
    assert wt.step_caption("open_app", app="Settings") == "Open Settings"
    assert wt.step_caption("press_key", key="HOME") == "Go to the Home screen"
    assert wt.step_caption("swipe", direction="down") == "Swipe down"
    assert wt.step_caption("type", text="hello") == "Type “hello”"


def test_each_caption_shows_until_the_next_step():
    # Recording started at t=100 (monotonic); steps at 100.5, 103 and 107.
    windows = wt.caption_windows([100.5, 103.0, 107.0], video_start=100.0, duration=10.0)
    assert windows == [(0.0, 3.0), (3.0, 7.0), (7.0, 10.0)]  # the opening covers frame one
    # A step from before the recording started is clamped to the first frame.
    assert wt.caption_windows([99.0], video_start=100.0, duration=4.0) == [(0.0, 4.0)]


def test_output_is_at_most_1920_tall_with_even_sides():
    assert wt.output_size(1320, 2868) == (884, 1920)
    assert wt.output_size(390, 844) == (390, 844)


def test_a_caption_bar_spans_the_video_width_and_wraps_long_text():
    short = Image.open(io.BytesIO(wt.caption_png("Tap “Jobs”", 884, 1)))
    long = Image.open(io.BytesIO(wt.caption_png("word " * 40, 884, 2)))
    assert short.width == 884 and long.width == 884
    assert long.height > short.height  # wrapped onto more lines


def _test_video(path, seconds=3.0, size="390x844"):
    subprocess.run(
        [
            get_ffmpeg_path(),
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"testsrc=size={size}:rate=30:duration={seconds}",
            "-pix_fmt",
            "yuv420p",
            str(path),
        ],
        check=True,
    )


def _png_b64(color):
    import base64

    return base64.b64encode(_png_bytes(color)).decode()


def _png_bytes(color="white", size=(390, 844)):
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.mark.asyncio
async def test_the_recording_is_rendered_with_captions_and_an_index(tmp_path):
    walk = wt.Walkthrough.create("Open About")
    walk.recording_dir.mkdir()
    segment = walk.recording_dir / "recording.mp4"
    _test_video(segment, seconds=3.0)
    stopped_at = 1000.0
    walk.add("Start on Settings", "Settings", _png_bytes(), at=997.2)
    walk.add("Tap “General”", "General", _png_bytes("lightgrey"), at=998.5)
    walk.add("Check: “iOS Version” is shown", "About", None, at=999.6)

    out = await wt.render_recording(walk, [segment], stopped_at, walk.directory / "w.mp4")
    probe = await probe_video_segment(out)
    # The app keeps its 390x844; the captions get their own band below it.
    _, _, band = wt.frame_layout(390, 844, walk.steps)
    assert probe["width"] == 390 and probe["height"] == 844 + band
    # Recording 997.0-1000.0; first step at 997.2, so nothing is trimmed (lead-in 0.5 s).
    assert 2.5 <= probe["duration"] <= 3.5
    assert sorted(p.name for p in (walk.directory / "captions").iterdir()) == [
        "01.png",
        "02.png",
        "03.png",
    ]

    index = walk.write_index(out)
    text = index.read_text(encoding="utf-8")
    assert "# Open About" in text and "Video: [w.mp4](w.mp4)" in text
    assert "2. **Tap “General”** — General" in text and "screens/02.png" in text
    steps = json.loads((walk.directory / "steps.json").read_text(encoding="utf-8"))
    assert [s["caption"] for s in steps["steps"]][0] == "Start on Settings"


@pytest.mark.asyncio
async def test_dead_time_before_the_first_step_is_trimmed():
    # Live: recorder start-up plus the first look left ~5 s of blank screen.
    walk = wt.Walkthrough.create("Trim")
    walk.recording_dir.mkdir()
    segment = walk.recording_dir / "recording.mp4"
    _test_video(segment, seconds=6.0)
    walk.add("Start", None, _png_bytes(), at=998.0)  # 4 s into a 994-1000 recording
    out = await wt.render_recording(walk, [segment], 1000.0, walk.directory / "w.mp4")
    assert (await probe_video_segment(out))["duration"] == pytest.approx(2.5, abs=0.3)


@pytest.mark.asyncio
async def test_without_a_recording_the_screenshots_become_a_slideshow():
    walk = wt.Walkthrough.create("No recorder")
    walk.add("Start", "Home", _png_bytes(), at=1.0)
    walk.add("Open Settings", "Settings", _png_bytes("lightblue"), at=2.0)
    out = await wt.render_slideshow(walk, walk.directory / "walkthrough.mp4")
    probe = await probe_video_segment(out)
    assert probe["height"] > 844  # app plus the caption band
    assert probe["duration"] == pytest.approx(2 * wt.SLIDE_SECONDS, abs=0.6)


# --- the harness session ---


class _Recorder:
    """Stands in for the driver's screen recorder: writes a real MP4 on stop."""

    def __init__(self, fail=False):
        self.fail = fail
        self.dir = None

    async def start_video_recording(self, output_dir=None, **_):
        if self.fail:
            raise RuntimeError("simctl io recordVideo is unavailable")
        self.dir = output_dir
        output_dir.mkdir(parents=True, exist_ok=True)

    async def stop_video_recording(self):
        path = self.dir / "recording.mp4"
        await asyncio.to_thread(_test_video, path, 2.0)
        return str(path)


@pytest.mark.asyncio
async def test_harness_actions_become_steps_and_stop_writes_the_video(started):
    session, controller, _ = started
    recorder = _Recorder()
    controller.driver.start_video_recording = recorder.start_video_recording
    controller.driver.stop_video_recording = recorder.stop_video_recording

    status = await session.start_walkthrough("Find the iOS version")
    assert "1 step(s), recording" in status
    assert (await session.act("tap", target="General")).ok
    step = await session.do_step("About")
    assert step.status in ("done", "tapped")
    assert (await session.assert_text("iOS Version")).ok

    captions = [s.caption for s in session.walkthrough.steps]
    assert captions == [
        "Find the iOS version",  # the walkthrough's name opens it
        "Tap “General”",
        "About",  # do_step's tap carries the step's own words
        "Check: “iOS Version” is shown",
    ]
    assert session.walkthrough.recaption("Open About")
    assert session.walkthrough.steps[-1].caption == "Open About"

    summary = await session.stop_walkthrough()
    assert session.walkthrough is None
    assert "walkthrough.mp4" in summary and "Note:" not in summary
    folder = next(p for p in summary.splitlines() if p.startswith("Folder: "))
    video = Path(folder.removeprefix("Folder: file://")) / "walkthrough.mp4"
    assert (await probe_video_segment(video))["duration"] > 1.5


@pytest.mark.asyncio
async def test_a_failed_recorder_still_gives_a_slideshow(started):
    session, controller, _ = started
    recorder = _Recorder(fail=True)
    controller.driver.start_video_recording = recorder.start_video_recording

    status = await session.start_walkthrough("Steps only")
    assert "no recording (simctl io recordVideo is unavailable)" in status
    await session.act("tap", target="General")
    summary = await session.stop_walkthrough()
    assert "Video: file://" in summary and "used a slideshow" in summary


@pytest.mark.asyncio
async def test_closing_the_session_finishes_a_running_walkthrough(started):
    session, controller, _ = started
    controller.driver.start_video_recording = _Recorder(fail=True).start_video_recording
    await session.start_walkthrough("Closed early")
    folder = session.walkthrough.directory
    await session.close()
    assert (folder / "walkthrough.mp4").exists() and (folder / "steps.md").exists()


def test_still_stretches_are_cut_to_a_few_seconds():
    # Live (QA, Part 9): a 102 s video ended in ~30 s of "Wait" over the map.
    windows = [(0.0, 2.0), (2.0, 31.0), (31.0, 33.0), (33.0, 90.0)]
    spans = wt.kept_spans(windows, duration=90.0)
    assert spans == [(0.0, 2.0), (2.0, 8.0), (31.0, 33.0), (33.0, 36.0)]
    assert sum(b - a for a, b in spans) == 13.0


def test_captions_never_cover_the_app():
    steps = [wt.WalkStep(1, "Tap “Jobs”", None, 0.0), wt.WalkStep(2, "word " * 30, None, 1.0)]
    width, height, band = wt.frame_layout(1206, 2622, steps)
    assert height + band <= wt.MAX_HEIGHT and width % 2 == 0 and height % 2 == 0
    tallest = max(
        Image.open(io.BytesIO(wt.caption_png(s.caption, width, s.number))).height for s in steps
    )
    assert band >= tallest


@pytest.mark.asyncio
async def test_waits_and_actions_that_change_nothing_are_not_steps(started):
    session, controller, _ = started
    controller.driver.start_video_recording = _Recorder(fail=True).start_video_recording
    await session.start_walkthrough("Swipe around")
    await session.act("wait", duration_ms=10)
    await session.act("swipe", direction="up")  # the fake screen does not move
    await session.act("tap", target="General")
    assert [s.caption for s in session.walkthrough.steps] == ["Swipe around", "Tap “General”"]
    await session.stop_walkthrough()


@pytest.mark.asyncio
async def test_a_long_wait_is_trimmed_in_the_rendered_video():
    walk = wt.Walkthrough.create("Trim waits")
    walk.recording_dir.mkdir()
    segment = walk.recording_dir / "recording.mp4"
    _test_video(segment, seconds=20.0)
    # Recording 980-1000; steps at 980.2, 981 and 982, then 18 s of nothing.
    for i, at in enumerate((980.2, 981.0, 982.0)):
        walk.add(f"Step {i + 1}", None, _png_bytes(), at=at)
    out = await wt.render_recording(walk, [segment], 1000.0, walk.directory / "w.mp4")
    assert (await probe_video_segment(out))["duration"] == pytest.approx(0.8 + 1.0 + 3.0, abs=0.4)


def test_the_last_step_is_held_until_its_screen_settled():
    # Live (QA, Part 10): the video ended on the map still loading.
    windows = [(0.0, 2.0), (2.0, 30.0)]
    assert wt.kept_spans(windows, 30.0) == [(0.0, 2.0), (2.0, 5.0)]
    assert wt.kept_spans(windows, 30.0, settled=9.5) == [(0.0, 2.0), (2.0, 10.5)]
    assert wt.kept_spans(windows, 30.0, settled=29.0) == [(0.0, 2.0), (2.0, 14.0)]  # capped


@pytest.mark.asyncio
async def test_stop_waits_for_the_screen_to_stop_changing(started, monkeypatch):
    session, controller, _ = started
    shots = iter(["lightblue", "lightgrey", "white", "white", "white", "white"])
    seen = []

    async def screenshot_b64():
        color = next(shots, "white")
        seen.append(color)
        return _png_b64(color)

    monkeypatch.setattr(session, "_screenshot_b64", screenshot_b64)
    walk_until = await session._wait_until_still(since=0.0, min_seconds=0.0, max_seconds=10.0)
    assert seen[:4] == ["lightblue", "lightgrey", "white", "white"]  # stops at two alike
    assert walk_until > 0

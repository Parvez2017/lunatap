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

"""App walkthroughs: a screen recording plus one captioned screenshot per step.

While a walkthrough runs, every harness action becomes a step: its caption
("Tap “Jobs”", or the plan step's own words), the screen it landed on, and a
screenshot. ``stop`` turns the recording into ``walkthrough.mp4`` with each
step's caption shown while that step plays, and writes the screenshots and a
``steps.md`` next to it. Without a usable recording (the recorder failed to
start, or produced nothing), the video is a slideshow of the step
screenshots instead, so there is always something to share.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from lunatap.utils.video import get_ffmpeg_path, probe_video_segment
from third_party.mobile_use.utils.logger import get_logger

logger = get_logger(__name__)

WALKTHROUGHS_DIR_ENV = "LUNATAP_WALKTHROUGHS_DIR"
#: Shareable output: tall phone video capped at this height, H.264, no audio.
MAX_HEIGHT = 1920
#: Video kept before the first step, after trimming the recorder's start-up.
LEAD_IN_SECONDS = 0.5
#: The longest a step stays on screen, and the ending after the last step;
#: longer stretches are a still screen while the agent waits or thinks.
STEP_MAX_SECONDS = 6.0
TAIL_SECONDS = 3.0
#: The last step waits for its screen to settle (a map loading), up to this.
TAIL_MAX_SECONDS = 12.0
#: How long each screenshot shows in a slideshow (no recording).
SLIDE_SECONDS = 2.5
_FFMPEG_TIMEOUT_S = 600.0
_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def walkthroughs_dir() -> Path:
    """``$LUNATAP_WALKTHROUGHS_DIR``, else ``walkthroughs/`` next to Lunatap's traces dir."""
    override = os.environ.get(WALKTHROUGHS_DIR_ENV)
    if override:
        return Path(override).expanduser()
    from lunatap.config.paths import get_default_traces_path

    return get_default_traces_path().parent / "walkthroughs"


def step_caption(
    action: str,
    target: Any = None,
    text: str | None = None,
    direction: str | None = None,
    key: str | None = None,
    app: str | None = None,
    url: str | None = None,
) -> str:
    """A viewer's description of one harness action."""
    label = target if isinstance(target, str) and not target.strip().isdigit() else None
    if action == "tap":
        return f"Tap “{label}”" if label else "Tap"
    if action == "long_press":
        return f"Press and hold “{label}”" if label else "Press and hold"
    if action == "type":
        return f"Type “{text}”" if text else "Type"
    if action == "swipe":
        return f"Swipe {direction or 'up'}"
    if action == "press_key":
        return "Go to the Home screen" if (key or "").upper() == "HOME" else f"Press {key}"
    if action == "open_app":
        return f"Open {app}"
    if action == "stop_app":
        return f"Close {app}"
    if action == "open_link":
        return f"Open {url}"
    if action == "alert":
        return f"Choose “{text}”" if text else "Accept the alert"
    if action == "wait":
        return "Wait"
    return action


@dataclass
class WalkStep:
    number: int
    caption: str
    title: str | None
    at: float  # time.monotonic() when the step began
    screenshot: str | None = None  # path relative to the walkthrough dir


@dataclass
class Walkthrough:
    """One walkthrough in progress: where it is written and the steps so far."""

    name: str
    directory: Path
    started_at: float = field(default_factory=time.monotonic)
    steps: list[WalkStep] = field(default_factory=list)
    recording: bool = False
    recording_note: str | None = None
    settled_at: float | None = None  # monotonic: when the last screen stopped changing

    @classmethod
    def create(cls, name: str, root: Path | None = None) -> Walkthrough:
        slug = _NAME_RE.sub("-", name.strip()).strip("-") or "walkthrough"
        stamp = time.strftime("%Y%m%d-%H%M%S")
        directory = (root or walkthroughs_dir()) / f"{slug}-{stamp}"
        (directory / "screens").mkdir(parents=True, exist_ok=True)
        return cls(name=name.strip() or slug, directory=directory)

    @property
    def recording_dir(self) -> Path:
        return self.directory / "recording"

    def add(
        self, caption: str, title: str | None, screenshot_png: bytes | None, at: float
    ) -> WalkStep:
        number = len(self.steps) + 1
        relative = None
        if screenshot_png:
            relative = f"screens/{number:02d}.png"
            (self.directory / relative).write_bytes(screenshot_png)
        step = WalkStep(number, caption, title, at, relative)
        self.steps.append(step)
        return step

    def recaption(self, text: str, since: float | None = None) -> bool:
        """Give the newest step (taken after ``since``, when given) a caption."""
        if not self.steps or (since is not None and self.steps[-1].at < since):
            return False
        self.steps[-1].caption = text.strip()
        return True

    def status(self) -> str:
        recording = "recording" if self.recording else f"no recording ({self.recording_note})"
        return (
            f"Walkthrough '{self.name}': {len(self.steps)} step(s), {recording}, "
            f"folder {self.directory}"
        )

    def write_index(self, video: Path | None) -> Path:
        """``steps.json`` for tools and ``steps.md`` for people."""
        data = {
            "name": self.name,
            "video": video.name if video else None,
            "steps": [asdict(s) | {"at": round(s.at - self.started_at, 2)} for s in self.steps],
        }
        (self.directory / "steps.json").write_text(json.dumps(data, indent=2), encoding="utf-8")
        lines = [f"# {self.name}", ""]
        if video is not None:
            lines += [f"Video: [{video.name}]({video.name})", ""]
        for step in self.steps:
            where = f" — {step.title}" if step.title else ""
            lines.append(f"{step.number}. **{step.caption}**{where}")
            if step.screenshot:
                lines += ["", f"   ![Step {step.number}]({step.screenshot})"]
            lines.append("")
        path = self.directory / "steps.md"
        path.write_text("\n".join(lines), encoding="utf-8")
        return path


# --- captions ---


def caption_png(text: str, width: int, number: int | None = None) -> bytes:
    """A caption bar as RGBA PNG: white text on a dark rounded band, ``width`` wide."""
    margin = max(16, width // 30)
    size = max(18, width // 22)
    font = ImageFont.load_default(size=size)
    label = f"{number}. {text}" if number is not None else text
    lines = _wrap(label, font, width - 4 * margin)
    line_h = int(size * 1.3)
    height = line_h * len(lines) + 2 * margin
    image = Image.new("RGBA", (width, height + margin), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle(
        (margin, 0, width - margin, height), radius=margin, fill=(15, 15, 20, 215)
    )
    y = margin
    for line in lines:
        draw.text((2 * margin, y), line, font=font, fill=(255, 255, 255, 255))
        y += line_h
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def _wrap(text: str, font: Any, max_width: int) -> list[str]:
    words, lines, current = text.split(), [], ""
    for word in words:
        trial = f"{current} {word}".strip()
        if current and font.getlength(trial) > max_width:
            lines.append(current)
            current = word
        else:
            current = trial
    if current:
        lines.append(current)
    return lines[:3] or [""]


def caption_windows(
    starts: list[float], video_start: float, duration: float
) -> list[tuple[float, float]]:
    """Each step's caption shows from its start until the next step, in video seconds."""
    times = [min(max(0.0, s - video_start), duration) for s in starts]
    if times:
        times[0] = 0.0  # the opening caption covers the video's first frame
    windows = []
    for i, begin in enumerate(times):
        end = times[i + 1] if i + 1 < len(times) else duration
        windows.append((round(begin, 2), round(max(begin, end), 2)))
    return windows


def output_size(width: int, height: int) -> tuple[int, int]:
    """Scaled to at most ``MAX_HEIGHT`` tall, both sides even (H.264 needs it)."""
    out_h = min(height, MAX_HEIGHT)
    out_w = round(width * out_h / height)
    return out_w - out_w % 2, out_h - out_h % 2


# --- rendering ---


async def _ffmpeg(arguments: list[str]) -> None:
    process = await asyncio.create_subprocess_exec(
        get_ffmpeg_path(),
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        *arguments,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(process.communicate(), _FFMPEG_TIMEOUT_S)
    except TimeoutError:
        process.kill()
        await process.wait()
        raise RuntimeError("ffmpeg timed out while rendering the walkthrough") from None
    if process.returncode != 0:
        detail = stderr.decode(errors="replace").strip()[-800:]
        raise RuntimeError(f"ffmpeg failed while rendering the walkthrough: {detail}")


_ENCODE = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p"]
_SHARE = ["-movflags", "+faststart", "-an"]


def _band(width: int, steps: list[WalkStep]) -> int:
    """Height of the caption band under the app: the tallest caption, even."""
    tallest = max(
        (Image.open(io.BytesIO(caption_png(s.caption, width, s.number))).height for s in steps),
        default=0,
    )
    band = tallest + max(16, width // 30)
    return band + band % 2


def frame_layout(src_w: int, src_h: int, steps: list[WalkStep]) -> tuple[int, int, int]:
    """(width, app height, caption band height), at most ``MAX_HEIGHT`` tall in all.

    Captions sit in a band under the app, never over it: on the app they hid
    its tab bar, the very thing a walkthrough step taps (QA, Part 9).
    """
    width, height = output_size(src_w, src_h)
    band = _band(width, steps)
    if height + band > MAX_HEIGHT:
        height = MAX_HEIGHT - band
        width = round(src_w * height / src_h)
        width -= width % 2
        height -= height % 2
        band = _band(width, steps)
    return width, height, band


def kept_spans(
    windows: list[tuple[float, float]], duration: float, settled: float | None = None
) -> list[tuple[float, float]]:
    """The part of each step's window the video keeps: its first seconds.

    A step's screen settles in a few seconds; the rest of its window is the
    agent waiting or thinking over a still screen (a 30 s "Wait" live). The
    last step is kept until its screen stopped changing (``settled``, video
    seconds) plus a moment, so the video does not end on a map still loading.
    """
    spans = []
    for i, (begin, end) in enumerate(windows):
        last = i == len(windows) - 1
        cap = TAIL_SECONDS if last else STEP_MAX_SECONDS
        if last and settled is not None:
            cap = min(TAIL_MAX_SECONDS, max(cap, settled - begin + 1.0))
        stop = min(end if not last else duration, begin + cap)
        if stop > begin:
            spans.append((round(begin, 2), round(stop, 2)))
    return spans


async def render_recording(
    walk: Walkthrough, segments: list[Path], stopped_at: float, out: Path
) -> Path:
    """The recording, scaled for sharing, still stretches cut, captions in a band below.

    The recording ran until ``stopped_at`` (monotonic), so it started
    ``duration`` seconds before that: steps are placed from the end, which
    holds whatever the recorder's own start-up delay was.
    """
    probes = [await probe_video_segment(s) for s in segments]
    duration = sum(float(p.get("duration") or 0.0) for p in probes)
    if duration <= 0 or not probes[0].get("width") or not probes[0].get("height"):
        raise RuntimeError("the recording is empty or unreadable")
    width, height, band = frame_layout(
        int(probes[0]["width"]), int(probes[0]["height"]), walk.steps
    )
    video_start = stopped_at - duration
    # Recorder start-up and the first look leave seconds of nothing before
    # the first step; the video starts just before it.
    lead = 0.0
    if walk.steps:
        lead = min(max(0.0, walk.steps[0].at - video_start - LEAD_IN_SECONDS), duration - 0.5)
        lead = max(0.0, lead)
    windows = caption_windows([s.at for s in walk.steps], video_start + lead, duration - lead)
    shown = [(s, w) for s, w in zip(walk.steps, windows, strict=True) if w[1] > w[0]]
    settled = None if walk.settled_at is None else walk.settled_at - (video_start + lead)
    spans = kept_spans([w for _, w in shown], duration - lead, settled)
    captions_dir = walk.directory / "captions"
    captions_dir.mkdir(exist_ok=True)

    inputs: list[str] = []
    for segment in segments:
        inputs += ["-i", str(segment)]
    chains = []
    fit = (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30"
    )
    for i in range(len(segments)):
        chains.append(f"[{i}:v]{fit}[s{i}]")
    joined = "".join(f"[s{i}]" for i in range(len(segments)))
    keep = "+".join(f"between(t,{a},{b})" for a, b in spans) or "1"
    chains.append(
        f"{joined}concat=n={len(segments)}:v=1:a=0,trim=start={lead:.2f},setpts=PTS-STARTPTS,"
        f"select='{keep}',setpts=N/(30*TB),"
        f"pad={width}:{height + band}:0:0:color=0x0f0f14[v0]"
    )
    label = "v0"
    clock = 0.0
    for j, ((step, _), (begin, end)) in enumerate(zip(shown, spans, strict=False)):
        png = captions_dir / f"{step.number:02d}.png"
        png.write_bytes(caption_png(step.caption, width, step.number))
        inputs += ["-i", str(png)]
        index = len(segments) + j
        start_at, clock = clock, clock + (end - begin)
        chains.append(
            f"[{label}][{index}:v]overlay=0:{height}+({band}-h)/2:"
            f"enable='between(t,{start_at:.2f},{clock:.2f})'[c{j}]"
        )
        label = f"c{j}"
    await _ffmpeg(
        [*inputs, "-filter_complex", ";".join(chains), "-map", f"[{label}]", *_ENCODE, *_SHARE]
        + [str(out)]
    )
    return out


async def render_slideshow(walk: Walkthrough, out: Path) -> Path:
    """No recording: the step screenshots as a video, captions in a band below."""
    shots = [s for s in walk.steps if s.screenshot]
    if not shots:
        raise RuntimeError("no step has a screenshot, so there is nothing to show")
    frames_dir = walk.directory / "frames"
    frames_dir.mkdir(exist_ok=True)
    first = Image.open(walk.directory / shots[0].screenshot)
    width, height, band = frame_layout(*first.size, shots)
    lines = ["ffconcat version 1.0"]
    for step in shots:
        frame = Image.open(walk.directory / step.screenshot).convert("RGB")
        frame.thumbnail((width, height))
        canvas = Image.new("RGB", (width, height + band), (15, 15, 20))
        canvas.paste(frame, ((width - frame.width) // 2, (height - frame.height) // 2))
        bar = Image.open(io.BytesIO(caption_png(step.caption, width, step.number)))
        canvas.paste(bar, (0, height + (band - bar.height) // 2), bar)
        path = frames_dir / f"{step.number:02d}.png"
        canvas.save(path)
        lines += [f"file '{path.name}'", f"duration {SLIDE_SECONDS}"]
    playlist = frames_dir / "slides.ffconcat"
    playlist.write_text("\n".join(lines) + "\n", encoding="utf-8")
    await _ffmpeg(
        ["-f", "concat", "-safe", "0", "-i", str(playlist), "-vf", "fps=30"]
        + ["-t", f"{SLIDE_SECONDS * len(shots):.2f}", *_ENCODE, *_SHARE]
        + [str(out)]
    )
    return out


def recording_segments(directory: Path) -> list[Path]:
    """The recorder's finished MP4 segments, in order."""
    return sorted(p for p in directory.glob("*.mp4") if ".part" not in p.name)

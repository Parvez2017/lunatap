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

"""Device log capture for a harness session.

A screen alone does not say why an app misbehaved. ``LogCapture`` runs the
platform's log stream in the background, keeps the lines in a ring buffer
(and a file), and marks where each harness step happened, so a failing
step can be read next to the lines the app wrote while it ran.

Sources (``log_command``):

- simulator: ``xcrun simctl spawn <udid> log stream`` filtered to the app;
- physical device, any build: ``pymobiledevice3 syslog live`` when installed;
- physical device, dev-signed build: ``xcrun devicectl device process launch
  --console`` (``mode="launch"``), which relaunches the app with its stdout
  attached; TestFlight and App Store builds cannot be attached this way.
"""

from __future__ import annotations

import asyncio
import re
import shutil
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from third_party.mobile_use.utils.logger import get_logger

logger = get_logger(__name__)

MAX_LINES = 20_000
PYMOBILEDEVICE3_HINT = (
    "Install pymobiledevice3 for physical-device logs (`uv tool install pymobiledevice3`, "
    "then `sudo pymobiledevice3 remote tunneld` on iOS 17+), or use mode='launch' for a "
    "dev-signed build."
)


#: Lines no QA question is about: everything the ExternalAccessory SDK
#: logs (its 5 s heartbeat, manager set-up, foreground checks) and the iAP
#: daemon chatter (IAPApp..., IAPDHasLaunched, IAP2DHasLaunched), all seen
#: live on the Flight app.
DEFAULT_NOISE = r"\[#?ExternalAccessory\]|\bIAPApp|\bIAP2?D?HasLaunched"


@dataclass(frozen=True)
class LogLine:
    at: float  # time.monotonic()
    text: str


@dataclass(frozen=True)
class StepMark:
    step: int
    label: str
    at: float


def process_name(bundle_id: str | None, process: str | None) -> str | None:
    """The process name to filter on: given, or the bundle id's last part."""
    if process:
        return process.strip()
    if bundle_id and "." in bundle_id:
        return bundle_id.rsplit(".", 1)[-1]
    return bundle_id or None


def log_command(
    device_id: str,
    *,
    is_simulator: bool,
    bundle_id: str | None = None,
    process: str | None = None,
    mode: str = "stream",
) -> tuple[list[str] | None, str]:
    """The command that streams the device log, or ``(None, why)``."""
    name = process_name(bundle_id, process)
    if is_simulator:
        clauses = []
        if name:
            clauses.append(f'process == "{name}"')
        if bundle_id:
            clauses.append(f'subsystem BEGINSWITH "{bundle_id}"')
        command = ["xcrun", "simctl", "spawn", device_id, "log", "stream", "--style", "compact"]
        command += ["--level", "info"]
        if clauses:
            command += ["--predicate", " OR ".join(f"({c})" for c in clauses)]
        return command, "simctl log stream"
    if mode == "launch":
        if not bundle_id:
            return None, "mode='launch' needs the app's bundle id."
        return [
            "xcrun",
            "devicectl",
            "device",
            "process",
            "launch",
            "--console",
            "--terminate-existing",
            "--device",
            device_id,
            bundle_id,
        ], "devicectl --console (relaunches the app; dev-signed builds only)"
    if shutil.which("pymobiledevice3"):
        return ["pymobiledevice3", "syslog", "live", "--udid", device_id], "pymobiledevice3 syslog"
    return None, f"No log source for a physical device on this Mac. {PYMOBILEDEVICE3_HINT}"


def capture_availability() -> dict[str, str | None]:
    """What `mobile_logs` can use on this Mac, for diagnose."""
    return {
        "simulator": "simctl log stream" if shutil.which("xcrun") else None,
        "physical": "pymobiledevice3 syslog" if shutil.which("pymobiledevice3") else None,
        "physical_dev_signed": "devicectl --console" if shutil.which("xcrun") else None,
        "hint": None if shutil.which("pymobiledevice3") else PYMOBILEDEVICE3_HINT,
    }


class LogCapture:
    """One background log stream with step markers."""

    def __init__(
        self,
        command: list[str],
        *,
        source: str,
        process: str | None = None,
        file_path: Path | None = None,
        max_lines: int = MAX_LINES,
    ) -> None:
        self.command = command
        self.source = source
        self.process = process
        self.file_path = file_path
        self._lines: deque[LogLine] = deque(maxlen=max_lines)
        self._marks: list[StepMark] = []
        self._process: asyncio.subprocess.Process | None = None
        self._reader: asyncio.Task[None] | None = None
        self._file = None
        self.started_at: float | None = None
        self.exit_code: int | None = None

    # --- lifecycle ---

    async def start(self) -> None:
        if self.file_path is not None:
            self.file_path.parent.mkdir(parents=True, exist_ok=True)
            self._file = self.file_path.open("a", encoding="utf-8")
        self._process = await asyncio.create_subprocess_exec(
            *self.command,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        self.started_at = time.monotonic()
        self._reader = asyncio.create_task(self._pump())

    async def _pump(self) -> None:
        assert self._process is not None and self._process.stdout is not None
        stream = self._process.stdout
        while True:
            raw = await stream.readline()
            if not raw:
                break
            # devicectl's console output carries \r mid-line, not just at the
            # end; drop every one so the file and read() match.
            text = raw.decode("utf-8", errors="replace").rstrip("\r\n").replace("\r", "")
            self._lines.append(LogLine(time.monotonic(), text))
            if self._file is not None:
                self._file.write(text + "\n")
                self._file.flush()
        self.exit_code = await self._process.wait()

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.returncode is None

    async def stop(self) -> None:
        if self._process is not None and self._process.returncode is None:
            self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), timeout=5.0)
            except TimeoutError:
                self._process.kill()
                await self._process.wait()
        if self._reader is not None:
            try:
                await asyncio.wait_for(self._reader, timeout=5.0)
            except (TimeoutError, asyncio.CancelledError):
                self._reader.cancel()
        if self._file is not None:
            self._file.close()
            self._file = None

    # --- marks and reads ---

    def mark(self, step: int, label: str) -> StepMark:
        """Note where harness step ``step`` started."""
        mark = StepMark(step, label, time.monotonic())
        self._marks.append(mark)
        return mark

    def count_since(self, at: float) -> int:
        return sum(1 for line in self._lines if line.at >= at)

    def read(
        self,
        *,
        since_step: int | None = None,
        last_seconds: float | None = None,
        grep: str | None = None,
        exclude: str | None = None,
        max_lines: int = 200,
    ) -> list[str]:
        """The newest matching lines (oldest first), at most ``max_lines``.

        ``grep`` keeps only matching lines; ``exclude`` drops matching ones
        on top of :data:`DEFAULT_NOISE` (accessory heartbeats). Pass
        ``exclude=""`` to see every line.
        """
        start = 0.0
        end: float | None = None
        if since_step is not None:
            marks = {m.step: m for m in self._marks}
            mark = marks.get(since_step)
            if mark is None:
                return [f"(no step {since_step} was marked; steps: {sorted(marks)})"]
            start = mark.at
            later = [m.at for m in self._marks if m.step == since_step + 1]
            end = later[0] if later else None
        if last_seconds is not None:
            start = max(start, time.monotonic() - last_seconds)
        pattern = re.compile(grep, re.IGNORECASE) if grep else None
        if exclude == "":
            noise = None
        elif exclude:
            noise = re.compile(f"(?:{exclude})|(?:{DEFAULT_NOISE})", re.IGNORECASE)
        else:
            noise = re.compile(DEFAULT_NOISE, re.IGNORECASE)
        chosen = [
            line.text
            for line in self._lines
            if line.at >= start
            and (end is None or line.at < end)
            and (pattern is None or pattern.search(line.text))
            and (noise is None or not noise.search(line.text))
        ]
        return chosen[-max_lines:]

    def status(self) -> str:
        state = "running" if self.running else f"stopped (exit {self.exit_code})"
        marks = len(self._marks)
        return (
            f"Log capture {state}: {self.source}"
            + (f", process {self.process}" if self.process else "")
            + f", {len(self._lines)} lines buffered, {marks} step marks"
            + (f", file {self.file_path}" if self.file_path else "")
        )

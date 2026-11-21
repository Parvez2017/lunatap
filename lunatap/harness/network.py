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

"""Network capture for a harness session, through mitmproxy.

``NetCapture`` runs ``mitmdump`` with ``mitm_addon.py``, which writes one
JSON line per completed request, and reads that file back per harness
step. The device must be pointed at the proxy once (a person's job: Wi-Fi
proxy plus a trusted CA), which ``setup_instructions`` spells out.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from urllib.parse import urlsplit
import shutil
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from lunatap.harness.mitm_addon import NETLOG_ENV
from third_party.mobile_use.utils.logger import get_logger

logger = get_logger(__name__)

DEFAULT_PORT = 8080
INSTALL_HINT = "Install mitmproxy for network capture: `uv tool install mitmproxy` (or `brew install mitmproxy`)."
ADDON = Path(__file__).with_name("mitm_addon.py")
CA_CERT = Path("~/.mitmproxy/mitmproxy-ca-cert.pem").expanduser()


@dataclass(frozen=True)
class NetMark:
    step: int
    label: str
    at: float  # time.time(), to compare with the addon's timestamps


@dataclass
class Request:
    ts: float
    method: str
    url: str
    status: int
    ms: int
    content_type: str = ""
    size: int = 0
    body: str | None = None

    def describe(self) -> str:
        line = f"{self.method} {self.url} -> {self.status} ({self.ms} ms"
        if self.size:
            line += f", {self.size} B"
        line += ")"
        if self.body:
            line += f" {self.body!r}"
        return line


def net_command(port: int = DEFAULT_PORT) -> tuple[list[str] | None, str]:
    """The mitmdump command, or ``(None, why)`` when it is not installed."""
    mitmdump = shutil.which("mitmdump")
    if not mitmdump:
        return None, f"mitmdump is not installed. {INSTALL_HINT}"
    return [
        mitmdump,
        "-q",
        "--listen-host",
        "0.0.0.0",
        "-p",
        str(port),
        "-s",
        str(ADDON),
        "--set",
        "flow_detail=0",
    ], f"mitmdump on :{port}"


def lan_ip() -> str | None:
    """This Mac's address on the LAN, the proxy host a phone must use."""
    try:
        output = subprocess.run(
            ["ipconfig", "getifaddr", "en0"], capture_output=True, text=True, timeout=5
        ).stdout.strip()
        if output:
            return output
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("8.8.8.8", 80))
            return probe.getsockname()[0]
    except OSError:
        return None


def setup_instructions(port: int, *, is_simulator: bool, device_id: str) -> str:
    """What a person has to do once so the device's traffic reaches the proxy."""
    host = lan_ip() or "<this Mac's LAN IP>"
    if is_simulator:
        return (
            "Simulator setup (one time; the simulator uses the Mac's network settings):\n"
            f"  1. networksetup -setwebproxy Wi-Fi 127.0.0.1 {port}\n"
            f"  2. networksetup -setsecurewebproxy Wi-Fi 127.0.0.1 {port}\n"
            f"  3. xcrun simctl keychain {device_id} add-root-cert {CA_CERT}\n"
            "  4. relaunch the app under test.\n"
            "  Afterwards: networksetup -setwebproxystate Wi-Fi off; "
            "networksetup -setsecurewebproxystate Wi-Fi off."
        )
    return (
        "Phone setup (one time, by a person holding it), in this order:\n"
        f"  1. Settings > Wi-Fi > (i) on the network > Configure Proxy > Manual: "
        f"server {host}, port {port}. The proxy must be on first: mitm.it only serves the "
        "certificate through it.\n"
        "  2. In Safari (not Chrome or another browser: iOS only offers to install a profile "
        "downloaded in Safari) open http://mitm.it and install the iOS profile.\n"
        "  3. Settings > General > VPN & Device Management > install the mitmproxy profile "
        "(this needs the passcode).\n"
        "  4. Settings > General > About > Certificate Trust Settings > enable mitmproxy. "
        "Until this is on, the phone shows 'No Internet Connection' for HTTPS: that is "
        "expected, not broken.\n"
        "  5. relaunch the app under test. Apps that pin certificates will not connect "
        "through the proxy.\n"
        "  Afterwards: set Configure Proxy back to Off BEFORE stopping the capture, or the "
        "phone loses internet."
    )


_ID_SEGMENT = re.compile(
    r"^(?:\d+|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|[0-9a-f]{16,}|[A-Za-z0-9_-]{24,})$",
    re.IGNORECASE,
)


def endpoint(url: str) -> str:
    """``host/path`` with ids collapsed: /structures/1234/photos -> /structures/{id}/photos."""
    parts = urlsplit(url)
    segments = ["{id}" if _ID_SEGMENT.match(seg) else seg for seg in parts.path.split("/")]
    return parts.netloc + "/".join(segments)


def summarize(requests: list[Request]) -> list[str]:
    """One line per endpoint: count, statuses, the slowest; most called first.

    Live (QA, Part 7): this view found 211 photo calls in one prefetch and a
    401 on every launch, which a list of 342 lines hid.
    """
    groups: dict[tuple[str, str], list[Request]] = {}
    for r in requests:
        groups.setdefault((r.method, endpoint(r.url)), []).append(r)
    lines = []
    for (method, where), items in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        statuses: dict[int, int] = {}
        for r in items:
            statuses[r.status] = statuses.get(r.status, 0) + 1
        codes = ", ".join(
            f"{code}×{n}" if n > 1 else str(code) for code, n in sorted(statuses.items())
        )
        lines.append(
            f"{len(items)}× {method} {where} -> {codes} (max {max(r.ms for r in items)} ms)"
        )
    return lines


class NetCapture:
    """One mitmdump process and its request log, with step marks."""

    def __init__(self, command: list[str], *, source: str, log_path: Path) -> None:
        self.command = command
        self.source = source
        self.log_path = log_path
        self._process: asyncio.subprocess.Process | None = None
        self._marks: list[NetMark] = []
        self.started_at: float | None = None

    async def start(self) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_path.write_text("", encoding="utf-8")
        self._process = await asyncio.create_subprocess_exec(
            *self.command,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            env={**os.environ, NETLOG_ENV: str(self.log_path)},
        )
        self.started_at = time.time()

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

    def mark(self, step: int, label: str) -> NetMark:
        mark = NetMark(step, label, time.time())
        self._marks.append(mark)
        return mark

    def requests(self) -> list[Request]:
        """Every request recorded so far."""
        try:
            lines = self.log_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        out = []
        for line in lines:
            try:
                data = json.loads(line)
                out.append(
                    Request(
                        float(data["ts"]),
                        str(data["method"]),
                        str(data["url"]),
                        int(data["status"]),
                        int(data.get("ms") or 0),
                        str(data.get("content_type") or ""),
                        int(data.get("size") or 0),
                        data.get("body"),
                    )
                )
            except (ValueError, KeyError, TypeError):
                continue
        return out

    def count_since(self, at: float) -> tuple[int, int]:
        """(requests, failed requests) completed since ``at``."""
        since = [r for r in self.requests() if r.ts >= at]
        return len(since), sum(1 for r in since if r.status >= 400)

    def read(
        self,
        *,
        since_step: int | None = None,
        last_seconds: float | None = None,
        grep: str | None = None,
        host: str | None = None,
        failed_only: bool = False,
        summary: bool = False,
        max_lines: int = 100,
    ) -> list[str]:
        """Requests as lines; ``summary`` groups them by endpoint instead.

        ``host`` is a regex on the host alone, to keep the app's own calls
        and drop other apps' traffic (a browser updater, connectivity checks).
        """
        start, end = 0.0, None
        if since_step is not None:
            marks = {m.step: m for m in self._marks}
            mark = marks.get(since_step)
            if mark is None:
                return [f"(no step {since_step} was marked; steps: {sorted(marks)})"]
            start = mark.at
            later = [m.at for m in self._marks if m.step == since_step + 1]
            end = later[0] if later else None
        if last_seconds is not None:
            start = max(start, time.time() - last_seconds)
        pattern = re.compile(grep, re.IGNORECASE) if grep else None
        host_pattern = re.compile(host, re.IGNORECASE) if host else None
        picked = [
            r
            for r in self.requests()
            if r.ts >= start
            and (end is None or r.ts < end)
            and (not failed_only or r.status >= 400)
            and (pattern is None or pattern.search(r.url))
            and (host_pattern is None or host_pattern.search(urlsplit(r.url).netloc))
        ]
        if summary:
            return summarize(picked)[:max_lines]
        return [r.describe() for r in picked][-max_lines:]

    def status(self) -> str:
        state = "running" if self.running else "stopped"
        if not self.running and self.requests():
            last = max(r.ts for r in self.requests())
            if time.time() - last < 5:
                state += " while requests were still arriving (is the proxy still on?)"
        total = len(self.requests())
        return (
            f"Network capture {state}: {self.source}, {total} request(s) recorded, "
            f"{len(self._marks)} step marks, file {self.log_path}"
        )

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

"""Device log capture lined up with harness steps."""

import asyncio
import sys

import pytest

from lunatap.harness import logs as logs_module
from lunatap.harness.logs import LogCapture, LogLine, log_command, process_name

# A stand-in for `log stream`: prints a line whenever a file grows.
TAIL_SCRIPT = """
import sys, time
path = sys.argv[1]
seen = 0
while True:
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except FileNotFoundError:
        lines = []
    for line in lines[seen:]:
        print(line, flush=True)
    seen = len(lines)
    time.sleep(0.02)
"""


async def _wait_for_lines(capture, count, timeout=5.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while capture.count_since(0.0) < count:
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"only {capture.count_since(0.0)} lines arrived")
        await asyncio.sleep(0.02)


@pytest.mark.asyncio
async def test_lines_are_kept_per_step_and_written_to_a_file(tmp_path):
    source = tmp_path / "app.log"
    source.write_text("", encoding="utf-8")
    out = tmp_path / "harness.log"
    capture = LogCapture(
        [sys.executable, "-c", TAIL_SCRIPT, str(source)], source="fake stream", file_path=out
    )
    await capture.start()
    try:
        with source.open("a", encoding="utf-8") as fh:
            fh.write("boot ok\n")
        await _wait_for_lines(capture, 1)
        mark = capture.mark(1, "tap Jobs")
        with source.open("a", encoding="utf-8") as fh:
            fh.write("GET /fiber/jobs/1/structures -> 403 Accept the job first.\r\n")  # CRLF
            fh.write("[ExternalAccessory] Returning connectedAccessories count 0\n")
            fh.write("IAPDHasLaunched: 1\n")
            fh.write("upload done\n")
        await _wait_for_lines(capture, 5)
        capture.mark(2, "tap Map")
        with source.open("a", encoding="utf-8") as fh:
            fh.write("map loaded\n")
        await _wait_for_lines(capture, 6)

        assert capture.count_since(mark.at) == 5
        # Accessory heartbeats and iAP chatter are dropped by default.
        during_step_1 = capture.read(since_step=1)
        assert during_step_1 == [
            "GET /fiber/jobs/1/structures -> 403 Accept the job first.",
            "upload done",
        ]
        assert len(capture.read(since_step=1, exclude="")) == 4
        assert capture.read(since_step=1, grep="403|error") == [during_step_1[0]]
        assert capture.read(since_step=1, exclude="upload") == [during_step_1[0]]
        assert capture.read(since_step=2) == ["map loaded"]
        assert capture.read(since_step=9) == ["(no step 9 was marked; steps: [1, 2])"]
        assert capture.read(last_seconds=60, max_lines=2) == [
            "upload done",
            "map loaded",
        ]
        assert "running" in capture.status() and "2 step marks" in capture.status()
    finally:
        await capture.stop()
    assert not capture.running and "stopped" in capture.status()
    assert out.read_text(encoding="utf-8").splitlines()[0] == "boot ok"


@pytest.mark.asyncio
async def test_a_carriage_return_inside_a_line_is_dropped_in_memory_and_on_disk(tmp_path):
    # Live (QA): the raw log file kept \r in 48 of 88 lines from devicectl.
    out = tmp_path / "harness.log"
    script = (
        "import sys; sys.stdout.write('Launching 50%\\rLaunched app\\r\\n'); sys.stdout.flush()"
    )
    capture = LogCapture([sys.executable, "-c", script], source="fake", file_path=out)
    await capture.start()
    try:
        await _wait_for_lines(capture, 1)
    finally:
        await capture.stop()
    assert capture.read(exclude="") == ["Launching 50%Launched app"]
    assert out.read_bytes() == b"Launching 50%Launched app\n"


def test_the_simulator_stream_filters_on_the_apps_process_and_subsystem():
    command, source = log_command(
        "SIM-1", is_simulator=True, bundle_id="Flight.flightFiber", process=None
    )
    assert command is not None and source == "simctl log stream"
    assert command[:6] == ["xcrun", "simctl", "spawn", "SIM-1", "log", "stream"]
    predicate = command[command.index("--predicate") + 1]
    assert '(process == "flightFiber")' in predicate
    assert '(subsystem BEGINSWITH "Flight.flightFiber")' in predicate
    assert process_name("Flight.flightFiber", None) == "flightFiber"
    assert process_name(None, " Flight ") == "Flight"


def test_a_physical_device_needs_pymobiledevice3_or_a_dev_signed_launch(monkeypatch):
    monkeypatch.setattr(logs_module.shutil, "which", lambda name: None)
    command, why = log_command("0000", is_simulator=False, bundle_id="com.x.app")
    assert command is None and "pymobiledevice3" in why

    command, source = log_command("0000", is_simulator=False, bundle_id="com.x.app", mode="launch")
    assert command is not None and "--console" in command and command[-1] == "com.x.app"
    assert "relaunches the app" in source
    command, why = log_command("0000", is_simulator=False, mode="launch")
    assert command is None and "bundle id" in why

    monkeypatch.setattr(logs_module.shutil, "which", lambda name: "/usr/local/bin/" + name)
    command, source = log_command("0000", is_simulator=False, bundle_id="com.x.app")
    assert command is not None and command[:3] == ["pymobiledevice3", "syslog", "live"]


def test_the_default_filter_drops_every_accessory_and_iap_line_seen_live():
    # Live (QA, Part 6): these still showed after the first filter.
    capture = LogCapture(["true"], source="fake")
    noise = [
        "IAP2DHasLaunched: kIAP2AvailableNotification posted",
        "[#ExternalAccessory] -[EAAccessoryManager _initFromSingletonCreationMethod]",
        "[#ExternalAccessory] -[EAAccessoryManager _applicationWillEnterForeground:]",
        "[#ExternalAccessory] -[EAAccessoryManager _checkForConnectedAccessories:]",
        "[ExternalAccessory] Returning connectedAccessories count 0",
        "IAPAppConnectedAccessories changed",
        "IAPDHasLaunched: 1",
    ]
    kept = ["GET /fiber/jobs -> 200", "Accessory list screen opened"]
    capture._lines.extend(LogLine(1.0, text) for text in noise + kept)
    assert capture.read() == kept
    assert len(capture.read(exclude="")) == len(noise) + len(kept)

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

"""Network capture through mitmproxy, lined up with harness steps."""

import json
import sys
import time
from types import SimpleNamespace

import pytest

from lunatap.harness import mitm_addon, network
from lunatap.harness.network import NetCapture, net_command, setup_instructions


def _flow(method, url, status, body=b"", started=None, ended=None):
    now = time.time()
    return SimpleNamespace(
        request=SimpleNamespace(
            method=method, pretty_url=url, url=url, timestamp_start=started or now - 0.25
        ),
        response=SimpleNamespace(
            status_code=status,
            headers={"content-type": "application/json"},
            content=body,
            timestamp_end=ended or now,
        ),
    )


def test_the_addon_writes_one_line_per_response_with_error_bodies(tmp_path, monkeypatch):
    # Live: "Couldn't load this job's poles" was a 403 {"detail": "Accept the
    # job first."} the app swallowed; this is the line that would have shown it.
    log = tmp_path / "net.jsonl"
    monkeypatch.setenv(mitm_addon.NETLOG_ENV, str(log))
    mitm_addon.response(
        _flow(
            "GET",
            "https://api.example.com/fiber/jobs/1/structures",
            403,
            b'{"detail": "Accept the job first."}',
        )
    )
    mitm_addon.response(_flow("GET", "https://api.example.com/fiber/jobs", 200, b"[]" * 50))
    entries = [json.loads(line) for line in log.read_text().splitlines()]
    assert (
        entries[0]["status"] == 403 and entries[0]["body"] == '{"detail": "Accept the job first."}'
    )
    assert entries[0]["method"] == "GET" and 200 <= entries[0]["ms"] <= 400
    assert "body" not in entries[1] and entries[1]["size"] == 100


def test_requests_are_read_per_step_and_failed_only(tmp_path, monkeypatch):
    log = tmp_path / "net.jsonl"
    monkeypatch.setenv(mitm_addon.NETLOG_ENV, str(log))
    capture = NetCapture(["true"], source="fake", log_path=log)
    log.write_text("")
    mitm_addon.response(_flow("GET", "https://api.example.com/me", 200))
    time.sleep(0.01)
    mark = capture.mark(1, "tap Jobs")
    time.sleep(0.01)
    mitm_addon.response(_flow("GET", "https://api.example.com/fiber/jobs", 200))
    mitm_addon.response(
        _flow("GET", "https://api.example.com/fiber/jobs/1/structures", 403, b'{"detail":"no"}')
    )
    time.sleep(0.01)
    capture.mark(2, "tap Map")
    time.sleep(0.01)
    mitm_addon.response(_flow("GET", "https://tiles.example.com/1/2/3.png", 200))

    assert capture.count_since(mark.at) == (3, 1)
    during_1 = capture.read(since_step=1)
    assert during_1 == [
        "GET https://api.example.com/fiber/jobs -> 200 (250 ms)",
        "GET https://api.example.com/fiber/jobs/1/structures -> 403 (250 ms, 15 B) "
        '\'{"detail":"no"}\'',
    ]
    assert capture.read(since_step=1, failed_only=True) == during_1[1:]
    assert capture.read(since_step=2, grep="tiles") == [
        "GET https://tiles.example.com/1/2/3.png -> 200 (250 ms)"
    ]
    assert capture.read(since_step=7) == ["(no step 7 was marked; steps: [1, 2])"]
    assert "4 request(s) recorded, 2 step marks" in capture.status()


def test_without_mitmdump_the_start_says_how_to_install_it(monkeypatch):
    monkeypatch.setattr(network.shutil, "which", lambda name: None)
    command, why = net_command(8080)
    assert command is None and "uv tool install mitmproxy" in why
    monkeypatch.setattr(network.shutil, "which", lambda name: "/opt/bin/mitmdump")
    command, source = net_command(8081)
    assert command is not None and command[:1] == ["/opt/bin/mitmdump"] and "8081" in command
    assert command[command.index("-s") + 1].endswith("mitm_addon.py")


def test_setup_instructions_name_the_human_steps(monkeypatch):
    monkeypatch.setattr(network, "lan_ip", lambda: "192.168.0.101")
    phone = setup_instructions(8080, is_simulator=False, device_id="0000")
    assert "server 192.168.0.101, port 8080" in phone and "mitm.it" in phone
    assert "Certificate Trust Settings" in phone
    sim = setup_instructions(8080, is_simulator=True, device_id="SIM-1")
    assert "networksetup -setwebproxy Wi-Fi 127.0.0.1 8080" in sim
    assert "simctl keychain SIM-1 add-root-cert" in sim


@pytest.mark.asyncio
async def test_the_capture_runs_and_stops_its_process(tmp_path):
    capture = NetCapture(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        source="fake",
        log_path=tmp_path / "n.jsonl",
    )
    await capture.start()
    assert capture.running
    await capture.stop()
    assert not capture.running and "stopped" in capture.status()


def test_host_filter_and_summary_view(tmp_path, monkeypatch):
    # Live (QA, Part 7): 342 requests, other apps mixed in; a by-endpoint
    # view found 211 photo calls in one prefetch and a 401 on every launch.
    log = tmp_path / "net.jsonl"
    monkeypatch.setenv(mitm_addon.NETLOG_ENV, str(log))
    capture = NetCapture(["true"], source="fake", log_path=log)
    log.write_text("")
    for structure in (11, 12, 13):
        mitm_addon.response(
            _flow("GET", f"https://api.example.com/fiber/structures/{structure}/photos", 200)
        )
    mitm_addon.response(_flow("GET", "https://api.roboflow.com/coreml/model?api_key=x", 401))
    mitm_addon.response(_flow("GET", "https://api.roboflow.com/coreml/model?api_key=x", 401))
    mitm_addon.response(_flow("GET", "https://connectivitycheck.gstatic.com/generate_204", 204))

    mine = capture.read(host=r"example\.com|roboflow")
    assert len(mine) == 5 and not any("gstatic" in line for line in mine)
    assert capture.read(summary=True, host=r"example\.com|roboflow") == [
        "3× GET api.example.com/fiber/structures/{id}/photos -> 200×3 (max 250 ms)",
        "2× GET api.roboflow.com/coreml/model -> 401×2 (max 250 ms)",
    ]
    assert (
        network.endpoint("https://a.io/jobs/8f14e45f-ceea-467f-a0c5-8a1b2c3d4e5f/photos?x=1")
        == "a.io/jobs/{id}/photos"
    )


def test_phone_setup_names_the_traps(monkeypatch):
    monkeypatch.setattr(network, "lan_ip", lambda: "192.168.0.101")
    phone = setup_instructions(8080, is_simulator=False, device_id="0000")
    assert "Safari (not Chrome" in phone
    assert "The proxy must be on first" in phone
    assert "No Internet Connection" in phone
    assert "BEFORE stopping" in phone

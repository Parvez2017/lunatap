# Copyright 2026 The Lunatap Authors
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

"""A simulator never attaches to another simulator's WebDriverAgent.

Live: with simulator A's WDA on 8100, a task for simulator B attached to it,
read A's screen and timed out on A's hanging app.
"""

import pytest

from lunatap.drivers.ios import wda_simulator_driver
from lunatap.drivers.ios.wda_simulator_driver import (
    PortOwner,
    WdaSimulatorDriver,
    fallback_ports,
    port_owner,
)

MINE = "D4499A27-A89A-4C42-96FE-2538DA8B5A61"
OTHER = "4D51683C-9F97-4F44-8984-5A9E869956D7"
RUNNER = (
    "/Users/me/Library/Developer/CoreSimulator/Devices/{udid}/data/Containers/Bundle/"
    "Application/23170E09/WebDriverAgentRunner-Runner.app/WebDriverAgentRunner-Runner"
)


@pytest.fixture(autouse=True)
def _no_port_env(monkeypatch):
    for key in ("ARTEMIS_IOS_WDA_PORT", "ARTEMIS_IOS_WDA_URL"):
        monkeypatch.delenv(key, raising=False)


def owners(monkeypatch, table):
    seen = []

    async def lookup(port):
        seen.append(port)
        return table.get(port, PortOwner("free"))

    monkeypatch.setattr(wda_simulator_driver, "port_owner", lookup)
    return seen


@pytest.mark.asyncio
async def test_another_simulators_wda_on_8100_moves_us_to_our_own_port(monkeypatch):
    owners(monkeypatch, {8100: PortOwner("simulator", OTHER)})
    driver = WdaSimulatorDriver(MINE)
    own = fallback_ports(MINE)[0]
    assert await driver._wda_candidates() == [f"http://127.0.0.1:{own}"]
    # The WDA we start listens there too.
    assert driver._xctest_env() == {"TEST_RUNNER_USE_PORT": str(own)}


@pytest.mark.asyncio
async def test_our_own_wda_on_8100_is_reused(monkeypatch):
    owners(monkeypatch, {8100: PortOwner("simulator", MINE.lower())})
    assert await WdaSimulatorDriver(MINE)._wda_candidates() == ["http://127.0.0.1:8100"]


@pytest.mark.asyncio
async def test_a_non_simulator_listener_is_skipped(monkeypatch):
    """e.g. iproxy forwarding 8100 to a real iPhone."""
    owners(monkeypatch, {8100: PortOwner("other", None, "iproxy 8100 8100")})
    candidates = await WdaSimulatorDriver(MINE)._wda_candidates()
    assert candidates == [f"http://127.0.0.1:{fallback_ports(MINE)[0]}"]


@pytest.mark.asyncio
async def test_an_unknown_owner_keeps_the_old_behaviour(monkeypatch):
    owners(monkeypatch, {8100: PortOwner("unknown")})
    assert await WdaSimulatorDriver(MINE)._wda_candidates() == ["http://127.0.0.1:8100"]


@pytest.mark.asyncio
async def test_a_busy_fallback_port_tries_the_next(monkeypatch):
    first, second = fallback_ports(MINE)[:2]
    owners(
        monkeypatch,
        {8100: PortOwner("simulator", OTHER), first: PortOwner("other", None, "python3")},
    )
    assert await WdaSimulatorDriver(MINE)._wda_candidates() == [f"http://127.0.0.1:{second}"]


@pytest.mark.asyncio
async def test_a_configured_port_held_by_another_simulator_is_an_error(monkeypatch):
    monkeypatch.setenv("ARTEMIS_IOS_WDA_PORT", "8105")
    owners(monkeypatch, {8105: PortOwner("simulator", OTHER)})
    with pytest.raises(RuntimeError) as caught:
        await WdaSimulatorDriver(MINE)._wda_candidates()
    message = str(caught.value)
    assert "8105" in message and OTHER in message and "wrong device" in message


@pytest.mark.asyncio
async def test_no_free_port_is_an_error(monkeypatch):
    busy = PortOwner("simulator", OTHER)
    owners(monkeypatch, {p: busy for p in [8100, *fallback_ports(MINE)]})
    with pytest.raises(RuntimeError, match="No free WebDriverAgent port"):
        await WdaSimulatorDriver(MINE)._wda_candidates()


@pytest.mark.asyncio
async def test_the_choice_is_made_once_per_driver(monkeypatch):
    seen = owners(monkeypatch, {})
    driver = WdaSimulatorDriver(MINE)
    await driver._wda_candidates()
    await driver._wda_candidates()
    assert seen == [8100]


def test_fallback_ports_are_stable_distinct_and_never_the_default():
    ports = fallback_ports(MINE)
    assert ports == fallback_ports(MINE.lower())
    assert len(set(ports)) == len(ports)
    assert 8100 not in ports and all(8101 <= p < 8501 for p in ports)
    assert ports != fallback_ports(OTHER)


def fake_commands(monkeypatch, replies):
    async def run(*argv):
        return replies.get(argv[0])

    monkeypatch.setattr(wda_simulator_driver, "_run_quiet", run)


@pytest.mark.asyncio
async def test_port_owner_reads_the_simulator_udid_from_the_process_path(monkeypatch):
    fake_commands(monkeypatch, {"lsof": (0, "36276\n"), "ps": (0, RUNNER.format(udid=OTHER))})
    owner = await port_owner(8100)
    assert owner.state == "simulator" and owner.udid == OTHER


@pytest.mark.asyncio
async def test_port_owner_free_other_and_unknown(monkeypatch):
    fake_commands(monkeypatch, {"lsof": (1, "")})
    assert (await port_owner(8100)).state == "free"
    fake_commands(monkeypatch, {"lsof": (0, "123\n"), "ps": (0, "/usr/local/bin/iproxy 8100")})
    assert (await port_owner(8100)).state == "other"
    fake_commands(monkeypatch, {"lsof": None})
    assert (await port_owner(8100)).state == "unknown"

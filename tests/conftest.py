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

"""Repository-wide pytest fixtures and classification helpers.

The default test paths contain only deterministic tests.  Tests under the
integration and end-to-end trees remain directly runnable, and receive stable
markers here so callers can select them without relying on filename patterns.
"""

from pathlib import Path

import pytest

from lunatap.drivers.mock.mock_driver import MockDeviceDriver


@pytest.fixture
def mock_driver():
    """Provide an isolated mock mobile driver."""
    return MockDeviceDriver(device_id="fixture-mock-device", width=1080, height=2400)


@pytest.fixture(autouse=True)
def _deterministic_ios_simulator_backend(monkeypatch):
    """Pin the iOS simulator backend so unit tests ignore the host's Xcode.

    ``auto`` picks WebDriverAgent below Xcode 27; tests exercising backend
    selection set ``ARTEMIS_IOS_SIM_BACKEND`` (or delete it) themselves.
    """
    monkeypatch.setenv("ARTEMIS_IOS_SIM_BACKEND", "xcode")


@pytest.fixture(autouse=True)
def _no_host_ios_enumeration(monkeypatch):
    """Keep device admission off the host's real simulators and paired phones.

    Enumeration answers "unknown" (None), so admission fails open as it does
    when ``xcrun`` is unavailable. Tests of admission patch these themselves.
    """

    import importlib

    # ``lunatap.runtime`` exports an ``ios_device_pool`` instance that shadows
    # the module name, so patch the module object itself.
    pool_module = importlib.import_module("lunatap.runtime.ios_device_pool")

    async def _unknown():
        return None

    monkeypatch.setattr(pool_module, "list_ios_simulators", _unknown)
    monkeypatch.setattr(pool_module, "list_core_devices", _unknown)
    monkeypatch.setattr(pool_module, "list_ios_simulators_sync", lambda: None)
    monkeypatch.setattr(pool_module, "list_core_devices_sync", lambda: None)


@pytest.fixture(autouse=True)
def _stock_llm_selection(monkeypatch):
    """Ignore a developer's model choice in .env, so tests see config/artemis.jsonc.

    Tests of the preset logic set these variables themselves.
    """
    for name in ("LUNATAP_LLM_PRESET", "LUNATAP_LLM_MODEL", "LUNATAP_LLM_FALLBACK_MODEL"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _placeholder_gemini_key(monkeypatch):
    """Let agent tests build a Gemini client without a real key, and never use one.

    Gemini is the fallback provider, and its client refuses to construct
    without a key, so a shell without one failed ~80 agent tests. No test
    calls the API; tests of key handling delete these variables themselves.
    """
    from pydantic import SecretStr

    from lunatap.config.settings import settings

    monkeypatch.setenv("GOOGLE_API_KEY", "dummy-for-tests")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    # The settings object read its key at import; agents check it, not the env.
    monkeypatch.setattr(settings, "GOOGLE_API_KEY", SecretStr("dummy-for-tests"))


@pytest.fixture(autouse=True)
def _no_local_ocr(monkeypatch):
    """Keep Apple Vision OCR off, so results do not depend on the host OS.

    Tests of the local OCR path set ``LUNATAP_LOCAL_OCR`` themselves.
    """
    monkeypatch.setenv("LUNATAP_LOCAL_OCR", "0")


@pytest.fixture(autouse=True)
def _no_operator_fast_path(monkeypatch):
    """Keep the Operator on its LLM path, so tests never call Cloudflare.

    Tests of the fast path set ``LUNATAP_FAST_PATH`` themselves.
    """
    monkeypatch.setenv("LUNATAP_FAST_PATH", "0")


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Attach test-layer markers according to the owning test directory."""
    for item in items:
        parts = Path(str(item.path)).parts
        if "integration" in parts:
            item.add_marker(pytest.mark.integration)
        if "e2e" in parts:
            item.add_marker(pytest.mark.e2e)

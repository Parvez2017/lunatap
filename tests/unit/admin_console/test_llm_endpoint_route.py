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

"""Console setup of OpenAI or an OpenAI-compatible endpoint (/api/system/llm-endpoint)."""

import os

import pytest
from httpx import ASGITransport, AsyncClient

from apps.admin_console.server import app

ENV_NAMES = (
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "LUNATAP_LLM_PRESET",
    "LUNATAP_LLM_MODEL",
    "LUNATAP_LLM_FALLBACK_MODEL",
)


@pytest.fixture
def env_file(tmp_path, monkeypatch):
    import importlib

    settings_module = importlib.import_module("lunatap.config.settings")
    from lunatap.config import settings
    from lunatap.core.diagnostics import readiness_engine

    path = tmp_path / ".env"
    path.write_text("# keep me\nOPENAI_BASE_URL=http://old\nOTHER=1\n", encoding="utf-8")
    monkeypatch.setattr(settings_module, "get_env_file", lambda: path)
    for name in ENV_NAMES:
        monkeypatch.setenv(name, "placeholder")  # registers the original for restore
        monkeypatch.delenv(name)
    monkeypatch.setattr(settings, "OPENAI_API_KEY", None)
    monkeypatch.setattr(settings, "OPENAI_BASE_URL", None)
    monkeypatch.setattr(readiness_engine, "invalidate_cache", lambda: None)

    async def _report(force_refresh=False):
        return {"probes": []}

    monkeypatch.setattr(readiness_engine, "run_all", _report)
    return path


@pytest.fixture
def validator(monkeypatch):
    calls = []

    async def _validate(provider, api_key, base_url=None, timeout=12.0):
        calls.append((provider, api_key, base_url))
        return api_key != "bad", "ok" if api_key != "bad" else "401 invalid key"

    import lunatap.utils.credentials_validator as module

    monkeypatch.setattr(module, "validate_api_key", _validate)
    return calls


async def _post(body):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://localhost") as client:
        return await client.post("/api/system/llm-endpoint", json=body)


@pytest.mark.asyncio
async def test_compatible_endpoint_selects_preset_model_and_base_url(env_file, validator):
    res = await _post(
        {
            "api_key": "sk-or-123",
            "base_url": "https://openrouter.ai/api/v1/",
            "model": "openai/gpt-5.4-mini",
        }
    )

    assert res.status_code == 200, res.text
    assert validator == [("openai", "sk-or-123", "https://openrouter.ai/api/v1")]
    assert os.environ["LUNATAP_LLM_PRESET"] == "openai-compatible"
    assert os.environ["OPENAI_BASE_URL"] == "https://openrouter.ai/api/v1"
    assert os.environ["LUNATAP_LLM_MODEL"] == "openai/gpt-5.4-mini"
    text = env_file.read_text(encoding="utf-8")
    assert "OPENAI_BASE_URL=https://openrouter.ai/api/v1" in text
    assert "LUNATAP_LLM_PRESET=openai-compatible" in text
    assert "OPENAI_API_KEY=sk-or-123" in text
    assert "# keep me" in text and "OTHER=1" in text
    assert "sk-or-123" not in res.text


@pytest.mark.asyncio
async def test_plain_openai_clears_a_previous_base_url(env_file, validator):
    res = await _post({"api_key": "sk-123"})

    assert res.status_code == 200, res.text
    assert os.environ["LUNATAP_LLM_PRESET"] == "openai"
    assert "OPENAI_BASE_URL" not in os.environ
    assert "OPENAI_BASE_URL" not in env_file.read_text(encoding="utf-8")
    assert res.json()["endpoint"]["base_url"] is None


@pytest.mark.asyncio
async def test_base_url_without_model_is_rejected_before_any_request(env_file, validator):
    res = await _post({"api_key": "sk-123", "base_url": "https://openrouter.ai/api/v1"})

    assert res.status_code == 400
    assert "model" in res.json()["detail"]
    assert validator == []


@pytest.mark.asyncio
async def test_invalid_key_saves_nothing(env_file, validator):
    before = env_file.read_text(encoding="utf-8")
    res = await _post({"api_key": "bad", "base_url": "https://x.example/v1", "model": "m"})

    assert res.status_code == 400
    assert "401 invalid key" in res.json()["detail"]
    assert env_file.read_text(encoding="utf-8") == before
    assert "LUNATAP_LLM_PRESET" not in os.environ

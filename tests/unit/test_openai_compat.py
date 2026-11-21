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

"""Running every agent on OpenAI or an OpenAI-compatible server, with no Google key."""

from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from lunatap.config import llm as llm_config
from lunatap.config.settings import settings
from lunatap.llm.router import ModelEndpoint, ModelFactory, ModelProvider
from lunatap.services import llm as llm_service
from third_party.mobile_use.utils.file import load_jsonc

CONFIG = Path(__file__).resolve().parents[2] / "config" / "artemis.jsonc"


@pytest.fixture
def stock_config():
    with open(CONFIG, encoding="utf-8") as handle:
        return load_jsonc(handle)


@pytest.fixture
def no_google(monkeypatch):
    monkeypatch.setattr(settings, "GOOGLE_API_KEY", None)
    for name in ("LUNATAP_LLM_PRESET", "LUNATAP_LLM_MODEL", "LUNATAP_LLM_FALLBACK_MODEL"):
        monkeypatch.delenv(name, raising=False)


def _parse(config_dict):
    return llm_config.LLMConfig.model_validate(llm_config._expand_default_into_nodes(config_dict))


def test_openai_preset_reaches_every_node_without_a_google_key(
    stock_config, no_google, monkeypatch
):
    monkeypatch.setenv("LUNATAP_LLM_PRESET", "openai")
    config = _parse(stock_config)
    nodes = [config.operator, config.planner, config.summarizer, config.explorer, config.checker]
    nodes += [config.utils.hopper, config.utils.object_detector, config.utils.video_analyzer]
    for node in nodes:
        assert node.provider == "openai", node
        assert "gemini" not in node.model
        assert node.fallback.provider == "openai" and "gemini" not in node.fallback.model
    # Non-model settings of a pinned node survive: only the Gemini pin is dropped.
    assert config.planner.thinking_level == "high"


@pytest.mark.parametrize("judge", ["validator_pixel_safety_net", "planner_validation"])
@pytest.mark.parametrize("preset", ["openai", "openai-compatible"])
def test_judge_nodes_follow_a_non_google_preset(
    stock_config, no_google, monkeypatch, judge, preset
):
    # Regression: both judges fell back to a hard-coded Gemini model, so with
    # no Google key the Pro safety net and planner validation always failed.
    monkeypatch.setenv("LUNATAP_LLM_PRESET", preset)
    monkeypatch.setenv("LUNATAP_LLM_MODEL", "gpt-5.4-mini")
    node = _parse(stock_config).get_agent(judge)
    assert node.provider != "google" and "gemini" not in node.model
    assert node.fallback.provider != "google" and "gemini" not in node.fallback.model
    assert node.temperature == 0.0


def test_judge_nodes_keep_the_light_gemini_default_with_a_google_key(
    stock_config, no_google, monkeypatch
):
    monkeypatch.setattr(settings, "GOOGLE_API_KEY", SecretStr("real-looking-key"))
    monkeypatch.setenv("LUNATAP_LLM_PRESET", "openai")
    assert _parse(stock_config).get_agent("planner_validation").model == "gemini-3.5-flash-lite"


def test_gemini_pins_stay_when_a_google_key_is_available(stock_config, no_google, monkeypatch):
    monkeypatch.setattr(settings, "GOOGLE_API_KEY", SecretStr("real-looking-key"))
    monkeypatch.setenv("LUNATAP_LLM_PRESET", "openai")
    config = _parse(stock_config)
    assert config.operator.provider == "openai"
    assert config.utils.hopper.model == "gemini-3.5-flash-lite"
    assert config.utils.hopper.fallback.provider == "google"


def test_use_preset_in_the_config_file(stock_config, no_google):
    stock_config["use_preset"] = "openai"
    assert _parse(stock_config).operator.model == "gpt-5.4-mini"


def test_compatible_server_takes_its_model_from_the_environment(
    stock_config, no_google, monkeypatch
):
    monkeypatch.setenv("LUNATAP_LLM_PRESET", "openai-compatible")
    monkeypatch.setenv("LUNATAP_LLM_MODEL", "qwen3-vl-32b")
    config = _parse(stock_config)
    assert (config.operator.provider, config.operator.model) == ("custom", "qwen3-vl-32b")
    assert config.operator.fallback.model == "qwen3-vl-32b"


def test_compatible_server_without_a_model_explains_what_to_set(
    stock_config, no_google, monkeypatch
):
    monkeypatch.setenv("LUNATAP_LLM_PRESET", "openai-compatible")
    with pytest.raises(ValueError, match="LUNATAP_LLM_MODEL"):
        _parse(stock_config)


def test_unknown_preset_lists_the_available_ones(stock_config, no_google, monkeypatch):
    monkeypatch.setenv("LUNATAP_LLM_PRESET", "nope")
    with pytest.raises(ValueError, match="openai-compatible"):
        _parse(stock_config)


def test_stock_config_is_unchanged_without_a_preset(stock_config, no_google):
    config = _parse(stock_config)
    assert config.operator.provider == "google"
    assert config.utils.hopper.model == "gemini-3.5-flash-lite"


# --- Settings that name only a model (step_summarizer.model, chunking.model) --


@pytest.fixture
def openai_ctx(stock_config, no_google, monkeypatch):
    monkeypatch.setenv("LUNATAP_LLM_PRESET", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    return SimpleNamespace(llm_config=_parse(stock_config))


def _model_name(model):
    inner = getattr(model, "_model", None) or getattr(model, "base_model", None) or model
    while hasattr(inner, "bound"):
        inner = inner.bound
    return getattr(inner, "model_name", None) or getattr(inner, "model", None)


def test_bare_gemini_name_follows_a_non_google_role(openai_ctx):
    model = llm_service.get_llm_for_model(None, "gemini-3.5-flash-lite")
    assert _model_name(model) == "gpt-5.4-mini"


def test_bare_non_gemini_name_replaces_the_model_on_the_role_provider(openai_ctx):
    model = llm_service.get_llm_for_model(None, "gpt-5.4-nano")
    assert _model_name(model) == "gpt-5.4-nano"


def test_provider_prefix_picks_the_provider(openai_ctx):
    model = llm_service.get_llm_for_model(None, "openai/gpt-5.4-nano")
    assert type(model).__name__ == "ChatOpenAI" and _model_name(model) == "gpt-5.4-nano"


def test_unknown_prefix_is_part_of_the_model_name():
    assert llm_service._split_model_ref("meta-llama/llama-4") == (None, "meta-llama/llama-4")


def test_google_role_keeps_the_gemini_path(stock_config, no_google, monkeypatch):
    calls = []
    monkeypatch.setattr(
        llm_service, "get_google_llm", lambda model_name, temperature: calls.append(model_name)
    )
    llm_service.get_llm_for_model(SimpleNamespace(llm_config=_parse(stock_config)), "gemini-x")
    assert calls == ["gemini-x"]


# --- Local OpenAI-compatible servers -------------------------------------------


def test_ollama_defaults_to_its_own_port_and_never_gets_the_openai_key(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-real-openai-key")
    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    model = ModelFactory.create_model(
        ModelEndpoint(provider=ModelProvider.OLLAMA, model_name="qwen3-vl")
    )
    assert str(model.openai_api_base).startswith("http://localhost:11434/v1")
    assert model.openai_api_key.get_secret_value() == "ollama"


def test_custom_provider_uses_openai_base_url(monkeypatch):
    monkeypatch.setenv("OPENAI_BASE_URL", "http://gpu-box:8000/v1")
    model = ModelFactory.create_model(
        ModelEndpoint(provider=ModelProvider.CUSTOM, model_name="qwen3-vl-32b")
    )
    assert str(model.openai_api_base) == "http://gpu-box:8000/v1"


# --- Flash on OpenAI must call a tool every turn ---------------------------------


@pytest.mark.parametrize(
    ("provider", "expected"),
    [
        (ModelProvider.OPENAI, {"tool_choice": "required"}),
        (ModelProvider.GOOGLE, {}),
        (ModelProvider.OLLAMA, {}),
    ],
)
def test_tool_choice_is_required_only_for_openai(provider, expected):
    llm = SimpleNamespace(endpoint=ModelEndpoint(provider=provider, model_name="m"))
    assert llm_service.tool_choice_kwargs(llm) == expected


def test_tool_choice_is_left_alone_for_an_unwrapped_model():
    assert llm_service.tool_choice_kwargs(object()) == {}

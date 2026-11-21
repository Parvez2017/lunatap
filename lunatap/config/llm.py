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

"""LLM provider, model hierarchy, fallback chaining, and configuration loaders."""

import os
from pathlib import Path
from typing import Any


from lunatap.config.constants import (
    LLM_CONFIG_FILENAME,
    AgentNode,
)
from lunatap.config.paths import ROOT_DIR, get_config_path
from lunatap.utils.cython_compat import CyFunctionDetector
from third_party.mobile_use.config import llm as base_llm_config
from third_party.mobile_use.config.llm import (
    LLM,
    AgentNodeWithFallback,
    LLMConfigBase,
    LLMConfigUtils,
    LLMUtilsNodeWithFallback,
    LLMWithFallback,
    validate_vertex_ai_credentials,
)
from third_party.mobile_use.utils.file import load_jsonc
from third_party.mobile_use.utils.logger import get_logger

logger = get_logger(__name__)

__all__ = [
    "LLM",
    "AgentNodeWithFallback",
    "LLMUtilsNodeWithFallback",
    "CyFunctionDetector",
    "LLMConfig",
    "LLMConfigUtils",
    "LLMWithFallback",
    "deep_merge_llm_config",
    "get_default_llm_config",
    "initialize_llm_config",
    "lightweight_judge_default",
    "load_llm_config_override",
    "parse_llm_config",
    "validate_vertex_ai_credentials",
]


def lightweight_judge_default() -> "LLMWithFallback":
    """Factory default for the lightweight judge nodes (pixel safety net and
    planner validation): a flash-lite model at temperature 0."""
    return LLMWithFallback(
        provider="google",
        model="gemini-3.5-flash-lite",
        temperature=0.0,
        fallback=LLM(
            provider="google",
            model="gemini-3.1-flash-lite",
            temperature=0.0,
        ),
    )


def _at_temperature_zero(node: "LLMWithFallback") -> "LLMWithFallback":
    """A copy of ``node`` (and its fallback) for deterministic judging."""
    fallback = node.fallback.model_copy(update={"temperature": 0.0}) if node.fallback else None
    return node.model_copy(update={"temperature": 0.0, "fallback": fallback})


class LLMConfig(LLMConfigBase):
    """Comprehensive LLM configuration mapping every node to primary/fallback models."""

    summarizer: LLMWithFallback
    operator: LLMWithFallback
    operator_summarizer: LLMWithFallback
    log_reader_sub_agent: LLMWithFallback
    log_analyzer: LLMWithFallback
    diagnoser: LLMWithFallback
    checker: LLMWithFallback
    planner_avatar: LLMWithFallback
    history_analyzer_expert: LLMWithFallback
    diagnoser_expert: LLMWithFallback
    explorer: LLMWithFallback
    history_analyzer: LLMWithFallback | None = None
    validator_pixel_safety_net: LLMWithFallback | None = None
    planner_validation: LLMWithFallback | None = None
    output_analyzer: LLMWithFallback | None = None

    def validate_providers(self) -> None:
        """Validate credentials across all configured agent nodes."""
        super().validate_providers()
        self.summarizer.validate_provider("Summarizer")
        self.operator.validate_provider("Operator")
        self.operator_summarizer.validate_provider("OperatorSummarizer")
        self.log_reader_sub_agent.validate_provider("LogReaderSubAgent")
        self.log_analyzer.validate_provider("LogAnalyzer")
        self.diagnoser.validate_provider("Diagnoser")
        self.checker.validate_provider("Checker")
        self.planner_avatar.validate_provider("PlannerAvatar")
        self.history_analyzer_expert.validate_provider("HistoryAnalyzerExpert")
        self.diagnoser_expert.validate_provider("DiagnoserExpert")
        self.explorer.validate_provider("Explorer")
        if self.history_analyzer:
            self.history_analyzer.validate_provider("HistoryAnalyzer")
        if self.validator_pixel_safety_net:
            self.validator_pixel_safety_net.validate_provider("ValidatorPixelSafetyNet")
        if self.planner_validation:
            self.planner_validation.validate_provider("PlannerValidation")
        if self.output_analyzer:
            self.output_analyzer.validate_provider("OutputAnalyzer")

    def get_agent(self, item: AgentNode) -> LLMWithFallback:
        """Retrieve model configuration for a specific agent node with sensible defaults."""
        val = getattr(self, item)
        if val is None:
            if item == "history_analyzer":
                return self.operator
            elif item in ("validator_pixel_safety_net", "planner_validation"):
                # Both are cheap, high-frequency judges: the pixel safety net
                # runs before actions, the planner validator after every
                # milestone edit. They share one lightweight Gemini default,
                # which can't run on a non-Google setup with no Google key; there
                # they use the configured model at temperature 0.
                if _is_google(self.operator.provider) or _google_credentials_available():
                    return lightweight_judge_default()
                return _at_temperature_zero(self.operator)
            elif item == "output_analyzer":
                return self.log_analyzer
        return val


#: Environment overrides for the model every node inherits. They let one ``.env``
#: line switch the whole agent to OpenAI or any OpenAI-compatible server.
ENV_LLM_PRESET = "LUNATAP_LLM_PRESET"
ENV_LLM_MODEL = "LUNATAP_LLM_MODEL"
ENV_LLM_FALLBACK_MODEL = "LUNATAP_LLM_FALLBACK_MODEL"

_GOOGLE_PROVIDERS = frozenset({"google", "gemini", "vertexai", "vertex"})


def _is_google(provider: Any) -> bool:
    return str(provider or "google").strip().lower() in _GOOGLE_PROVIDERS


def _google_credentials_available() -> bool:
    from lunatap.config.settings import settings

    key = getattr(settings, "GOOGLE_API_KEY", None)
    return bool(key and key.get_secret_value().strip())


def _select_default_model(config_dict: dict) -> dict:
    """The model config every node inherits: preset, then environment overrides.

    ``use_preset`` in the config file (or ``LUNATAP_LLM_PRESET``) replaces
    ``default`` with a named entry from ``presets``. ``LUNATAP_LLM_MODEL`` and
    ``LUNATAP_LLM_FALLBACK_MODEL`` then replace the model names, so a single
    preset serves any server that speaks the same API.
    """
    default_cfg = dict(
        config_dict.get(
            "default",
            {
                "provider": "google",
                "model": "gemini-3.8-flash",
                "fallback": {"provider": "google", "model": "gemini-3.7-flash"},
            },
        )
    )
    preset_name = (os.environ.get(ENV_LLM_PRESET) or config_dict.get("use_preset") or "").strip()
    if preset_name:
        presets = config_dict.get("presets") or {}
        if preset_name not in presets:
            raise ValueError(
                f"LLM preset {preset_name!r} is not defined in 'presets' "
                f"(available: {', '.join(sorted(presets)) or 'none'})"
            )
        default_cfg = dict(presets[preset_name])
        logger.info(f"Using LLM preset '{preset_name}': {default_cfg.get('provider')}")

    model = (os.environ.get(ENV_LLM_MODEL) or "").strip()
    fallback_model = (os.environ.get(ENV_LLM_FALLBACK_MODEL) or "").strip()
    if model:
        default_cfg["model"] = model
    if model or fallback_model:
        fallback = dict(default_cfg.get("fallback") or {"provider": default_cfg.get("provider")})
        fallback["model"] = fallback_model or model
        default_cfg["fallback"] = fallback
    if not str(default_cfg.get("model") or "").strip():
        raise ValueError(
            f"No model name for provider {default_cfg.get('provider')!r}: "
            f"set {ENV_LLM_MODEL} (for example in .env)"
        )
    return default_cfg


def _pins_google(override: dict, inherited_provider: Any) -> bool:
    """True when a node override pins a Google/Gemini model.

    An explicit Google provider counts, and so does a Gemini model name with no
    provider (it would otherwise be sent to the inherited, non-Google provider).
    """
    if "provider" in override:
        return _is_google(override["provider"])
    model = override.get("model")
    return (
        isinstance(model, str) and "gemini" in model.lower() and not _is_google(inherited_provider)
    )


def _drop_unusable_google_pins(override: dict, default_cfg: dict) -> dict:
    """Let a node pinned to Gemini follow a non-Google default when Google can't run.

    The stock config pins some nodes (hopper, object_detector) to Gemini models.
    With an OpenAI(-compatible) default and no Google key, those pins can only
    fail, so the node inherits the default model instead. Everything else in
    the override (thinking level, grounding, ...) is kept.
    """
    if _is_google(default_cfg.get("provider")) or _google_credentials_available():
        return override
    cleaned = dict(override)
    if _pins_google(cleaned, default_cfg.get("provider")):
        cleaned.pop("provider", None)
        cleaned.pop("model", None)
    fallback = cleaned.get("fallback")
    if isinstance(fallback, dict) and _pins_google(
        fallback, (default_cfg.get("fallback") or {}).get("provider", default_cfg.get("provider"))
    ):
        cleaned.pop("fallback")
    return cleaned


def _expand_default_into_nodes(config_dict: dict) -> dict:
    """Expand unified config format with 'default' and 'nodes' into full LLMConfig schema."""
    if "planner" in config_dict and "utils" in config_dict:
        return config_dict

    default_model_cfg = _select_default_model(config_dict)

    nodes_override = {
        name: _drop_unusable_google_pins(override, default_model_cfg)
        for name, override in (config_dict.get("nodes") or {}).items()
        if isinstance(override, dict)
    }

    all_agent_nodes = [
        "planner",
        "summarizer",
        "operator",
        "operator_summarizer",
        "log_reader_sub_agent",
        "log_analyzer",
        "diagnoser",
        "checker",
        "planner_avatar",
        "history_analyzer_expert",
        "diagnoser_expert",
        "explorer",
    ]

    all_utils_nodes = [
        "outputter",
        "hopper",
        "video_analyzer",
        "object_detector",
    ]

    result: dict[str, Any] = {}
    for node in all_agent_nodes:
        node_cfg = dict(default_model_cfg)
        if node in nodes_override:
            for k, v in nodes_override[node].items():
                if isinstance(v, dict) and isinstance(node_cfg.get(k), dict):
                    node_cfg[k] = {**node_cfg[k], **v}
                else:
                    node_cfg[k] = v
        result[node] = node_cfg

    utils_dict: dict[str, Any] = {}
    for util in all_utils_nodes:
        util_cfg = dict(default_model_cfg)
        if util in nodes_override:
            for k, v in nodes_override[util].items():
                if isinstance(v, dict) and isinstance(util_cfg.get(k), dict):
                    util_cfg[k] = {**util_cfg[k], **v}
                else:
                    util_cfg[k] = v
        utils_dict[util] = util_cfg
    result["utils"] = utils_dict

    return result


def parse_llm_config() -> LLMConfig:
    """Parse and instantiate LLMConfig from artemis.jsonc or llm-config.json."""
    config_path = None
    for candidate in ("artemis.jsonc", "artemis.json", LLM_CONFIG_FILENAME):
        try:
            config_path = get_config_path(candidate)
            break
        except FileNotFoundError:
            continue

    if not config_path:
        config_path = get_config_path(LLM_CONFIG_FILENAME, ROOT_DIR / LLM_CONFIG_FILENAME)

    try:
        with open(config_path, encoding="utf-8") as f:
            config_dict = load_jsonc(f)
            expanded_dict = _expand_default_into_nodes(config_dict)
            return LLMConfig.model_validate(expanded_dict)
    except Exception as e:
        logger.error(f"Failed to load or parse llm config: {config_path}. Error: {e}")
        raise


def initialize_llm_config() -> LLMConfig:
    """Parse and validate credentials for LLMConfig."""
    return base_llm_config.initialize_llm_config(parse_llm_config)


def get_default_llm_config() -> LLMConfig:
    """Returns default LLMConfig parsed from standard configuration file."""
    return parse_llm_config()


def deep_merge_llm_config(base: LLMConfig, overrides: dict) -> LLMConfig:
    """Recursively merge dictionary overrides into an existing LLMConfig object."""
    base_dict = base.model_dump()

    def merge(d1: dict, d2: dict) -> None:
        for k, v in d2.items():
            if k in d1 and isinstance(d1[k], dict) and isinstance(v, dict):
                merge(d1[k], v)
            else:
                d1[k] = v

    merge(base_dict, overrides)
    return LLMConfig.model_validate(base_dict)


def load_llm_config_override(path: Path | str) -> LLMConfig:
    """Load custom LLM configuration JSON/JSONC overrides on top of default configuration."""
    resolved_path = Path(path)
    if not resolved_path.exists():
        try:
            resolved_path = get_config_path(str(path))
        except OSError:
            pass
    return base_llm_config.load_llm_config_override(
        resolved_path, get_default_llm_config, deep_merge_llm_config
    )

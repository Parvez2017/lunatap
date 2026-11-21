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

"""LUNATAP_* environment variables stand in for the inherited ARTEMIS_* names."""

from lunatap.config.env_aliases import apply_env_aliases


def test_new_name_sets_the_legacy_name():
    env = {"LUNATAP_IOS_SIM_BACKEND": "wda"}
    assert apply_env_aliases(env) == ["ARTEMIS_IOS_SIM_BACKEND"]
    assert env["ARTEMIS_IOS_SIM_BACKEND"] == "wda"


def test_new_name_wins_over_an_old_value():
    env = {"LUNATAP_IOS_WDA_PORT": "8101", "ARTEMIS_IOS_WDA_PORT": "8100"}
    apply_env_aliases(env)
    assert env["ARTEMIS_IOS_WDA_PORT"] == "8101"


def test_old_names_alone_keep_working():
    env = {"ARTEMIS_IOS_SIM_BACKEND": "xcode"}
    assert apply_env_aliases(env) == []
    assert env == {"ARTEMIS_IOS_SIM_BACKEND": "xcode"}


def test_llm_selection_is_not_copied():
    env = {"LUNATAP_LLM_PRESET": "openai"}
    assert apply_env_aliases(env) == []
    assert "ARTEMIS_LLM_PRESET" not in env

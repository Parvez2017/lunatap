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

"""``LUNATAP_*`` environment variables, the public names of the ``ARTEMIS_*`` ones.

The code inherited from Artemis reads ``ARTEMIS_*`` names. Rather than rename
all of them at once, each ``LUNATAP_X`` is copied onto ``ARTEMIS_X`` at
startup, so users and docs can use the new names while old ``.env`` files keep
working. When both are set, the ``LUNATAP_`` value wins.

Variables that only exist under the new prefix (``LUNATAP_LLM_*``) are read
directly and are not copied.
"""

from __future__ import annotations

import os
from collections.abc import MutableMapping

NEW_PREFIX = "LUNATAP_"
LEGACY_PREFIX = "ARTEMIS_"
#: New-only variables, read under their own names.
NATIVE_PREFIXES = ("LUNATAP_LLM_",)


def apply_env_aliases(environ: MutableMapping[str, str] | None = None) -> list[str]:
    """Copy every ``LUNATAP_X`` onto ``ARTEMIS_X``; returns the legacy names set."""
    env = os.environ if environ is None else environ
    applied = []
    for name, value in list(env.items()):
        if not name.startswith(NEW_PREFIX) or name.startswith(NATIVE_PREFIXES):
            continue
        legacy = LEGACY_PREFIX + name[len(NEW_PREFIX) :]
        if env.get(legacy) != value:
            env[legacy] = value
            applied.append(legacy)
    return applied

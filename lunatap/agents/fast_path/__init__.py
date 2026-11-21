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

"""Cheap Operator decisions: OCR/element text match first, then Clef, then the LLM.

Most Operator turns only need "which element does the current plan step
mean?". The fast path answers that without the full Operator prompt:

1. **Text match** (free, instant): exactly one on-screen element whose text
   names the step, e.g. step "Open Camera settings" and a "Camera" row.
2. **Clef** (Cloudflare's decision model, about 0.6 s and 1.5k tokens): picks
   the element from the indexed list and screenshot, and says whether the step
   needs a tap at all.
3. Anything else (not on screen, already done, typing, scrolling, an unsure
   answer) returns ``None`` and the normal LLM Operator turn runs.
"""

from lunatap.agents.fast_path.decider import FastDecision, FastPath, fast_path_enabled

__all__ = ["FastDecision", "FastPath", "fast_path_enabled"]

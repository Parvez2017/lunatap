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

"""Image media-type detection for LLM message payloads.

Screenshots flow through the codebase as raw bytes or base64 strings and are
embedded in OpenAI-style ``image_url`` data URIs and provider ``Part`` objects.
Historically every site hardcoded ``image/jpeg`` while the bytes were actually
PNG, which strict OpenAI-compatible endpoints reject. Sniff the magic bytes
instead of trusting the label.
"""

import base64

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_JPEG_MAGIC = b"\xff\xd8"


def image_mime_type(data: bytes | str) -> str:
    """Return ``image/png`` or ``image/jpeg`` for raw or base64 image bytes."""
    if isinstance(data, str):
        # The base64 alphabet can't hold the magic bytes; decode just enough
        # to sniff the header.
        if data.startswith("data:"):
            data = data.rsplit(",", 1)[-1]
        chunk = data[:20]
        chunk += "=" * (-len(chunk) % 4)
        data = base64.b64decode(chunk)
    if data[:2] == _JPEG_MAGIC:
        return "image/jpeg"
    if data[:8] == _PNG_MAGIC:
        return "image/png"
    # Every screenshot path in the project emits PNG; keep PNG as the honest
    # default for unrecognized-but-image bytes.
    return "image/png"


def image_data_uri(data: bytes | str) -> str:
    """Return a ``data:image/<mime>;base64,<b64>`` URI with the correct type."""
    if isinstance(data, bytes):
        mime = image_mime_type(data)
        data = base64.b64encode(data).decode("utf-8")
    else:
        mime = image_mime_type(data)
    return f"data:{mime};base64,{data}"

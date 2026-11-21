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

"""mitmproxy addon: one JSON line per completed request.

Loaded by ``mitmdump -s lunatap/harness/mitm_addon.py``; the output path
comes from ``LUNATAP_NETLOG``. It has no mitmproxy import so it can be unit
tested with a plain object shaped like an ``HTTPFlow``.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

NETLOG_ENV = "LUNATAP_NETLOG"
#: How much of an error response body is kept (the server's own reason).
BODY_EXCERPT = 300


def record(flow: Any) -> dict[str, Any]:
    """The entry for one flow: what a QA person needs next to the screen."""
    request, response = flow.request, flow.response
    started = float(getattr(request, "timestamp_start", None) or time.time())
    ended = float(getattr(response, "timestamp_end", None) or time.time())
    status = int(response.status_code)
    entry: dict[str, Any] = {
        "ts": ended,
        "method": str(request.method),
        "url": str(getattr(request, "pretty_url", None) or request.url),
        "status": status,
        "ms": int((ended - started) * 1000),
        "content_type": str((response.headers or {}).get("content-type", ""))[:80],
        "size": len(getattr(response, "content", b"") or b""),
    }
    if status >= 400:
        body = getattr(response, "content", b"") or b""
        entry["body"] = body[:BODY_EXCERPT].decode("utf-8", errors="replace")
    return entry


def response(flow: Any) -> None:
    """mitmproxy hook: append the entry to the net log."""
    path = os.environ.get(NETLOG_ENV)
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record(flow), ensure_ascii=False) + "\n")

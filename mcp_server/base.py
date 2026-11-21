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

"""Shared FastMCP instance definition for the mobile automation MCP server."""

import logging

from mcp.server.fastmcp import FastMCP

logger = logging.getLogger(__name__)

try:
    from mcp.server.fastmcp.server import Settings as FastMCPSettings

    FastMCPSettings.model_rebuild()
except Exception as exc:
    # Version-compat shim: older/newer FastMCP releases may not expose
    # Settings or need the rebuild; the server works without it.
    logger.debug("FastMCP Settings.model_rebuild skipped: %s", exc, exc_info=True)

# Define the shared FastMCP instance for external IDE and agent clients.
mcp = FastMCP(
    "lunatap",
    instructions=(
        "Lunatap drives iOS apps on simulators and paired iPhones/iPads in two modes. "
        "Harness mode: you plan and decide; call mobile_observe (numbered elements), "
        "mobile_act (one action, returns the new screen), mobile_do_step (one plan step settled "
        "in code), mobile_wait_for, mobile_assert (a check that can fail), mobile_logs (the "
        "app's device log per step), mobile_net (its network calls per step, via mitmproxy) and "
        "mobile_flow (record this session, replay it on the next build), then "
        "mobile_release_device when done. "
        "Handoff mode: mobile_run_task launches Lunatap's own autonomous agent, "
        "mobile_manage_task to check status or steer execution, "
        "mobile_get_device_state to inspect real-time device screen/hierarchy, "
        "mobile_inspect_trace to inspect detailed execution steps and visual action overlays, "
        "and mobile_diagnose whenever a tool errors, no device is found, or the user says "
        "Lunatap is not working: it checks the environment and returns ordered fix steps."
    ),
)

# Tool registration is intentionally NOT done here: mcp_server/__init__.py
# imports mcp_server.tools (which imports this module), so importing this
# module from anywhere already registers every tool. Importing tools here as
# well would create a circular import (base -> tools -> base).

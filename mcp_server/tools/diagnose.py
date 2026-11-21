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

"""MCP Tool: mobile_diagnose.

Runs the same readiness probes as the web console's device wizard and the
``lunatap doctor`` CLI, adds the MCP-host probe, and renders the outcome as an
action list an AI coding assistant can execute without reading Lunatap source.
On request it also verifies the configured API keys against the providers and
drives the selected iOS device end to end (screenshot + UI hierarchy) to prove
a task could actually start.
"""

from __future__ import annotations


import asyncio
import os
from pathlib import Path
import re
from typing import Any

from mcp_server.base import mcp
from mcp_server.utils import env_utils
from lunatap.core.diagnostics import readiness_engine
from lunatap.core.diagnostics.device_smoke import smoke_test_device
from lunatap.core.diagnostics.readiness import (
    base_verdict,
    collect_readiness,
    sort_by_fix_order,
)
from lunatap.core.diagnostics.schema import ProbeResult, ProbeStatus, SystemReadinessReport
from lunatap.runtime import DeviceExecutionLock, trace_store
from lunatap.runtime.device_target import normalize_device_platform
from lunatap.utils.credentials_validator import validate_api_key
from third_party.mobile_use.utils.logger import get_logger

logger = get_logger(__name__)

#: Upper bound for one diagnosis phase. Device probes shell out to ``xcrun``,
#: so a wedged simulator service must surface as a verdict, not a hung tool. The optional extras (credential verification, device smoke test) run
#: concurrently under a second budget of the same size.
DIAGNOSIS_TIMEOUT_SECONDS = 40.0

#: Per-provider budget for a live API key verification request.
CREDENTIAL_CHECK_TIMEOUT_SECONDS = 12.0

#: Providers the credentials probe lists by their *endpoint URL* rather than a
#: key (``OPENAI_BASE_URL`` / ``OLLAMA_BASE_URL`` / ``VLLM_BASE_URL``). Their
#: "raw_key" is that URL: verification must call the endpoint, not treat the
#: URL as an API key.
_ENDPOINT_PROVIDERS = frozenset({"custom", "ollama", "vllm"})

#: Metadata keys that carry credential material. The console UI needs them to
#: prefill its settings form; an MCP caller is an LLM context and must never
#: see them.
_SECRET_METADATA_KEYS = frozenset(
    {"raw_key", "key", "api_keys", "current_key", "current_gemini_key"}
)

_ERROR_MARKERS = ("traceback", "error", "exception", "failed", "critical")

#: rich/typer write colour escapes into the tee'd stderr log; the model must not see them.
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


# --------------------------------------------------------------------------- #
# Rendering helpers
# --------------------------------------------------------------------------- #


def _scrub(value: Any) -> Any:
    """Drop secrets and bulky inventories from probe metadata before it reaches the model."""
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            if key in _SECRET_METADATA_KEYS:
                continue
            if key == "installed_packages" and isinstance(item, list):
                cleaned["installed_package_count"] = len(item)
                continue
            cleaned[key] = _scrub(item)
        return cleaned
    if isinstance(value, list):
        return [_scrub(item) for item in value]
    return value


def _find(results: list[ProbeResult], probe_id: str) -> ProbeResult | None:
    return next((r for r in results if r.id == probe_id), None)


def _render_check(result: ProbeResult) -> dict[str, Any]:
    """Passing checks are one line each; only problems carry detail, fixes and facts."""
    check: dict[str, Any] = {
        "id": result.id,
        "title": result.title,
        "status": result.status.value,
        "required": result.is_blocker,
        "summary": result.summary,
    }
    if result.status is ProbeStatus.PASS:
        return check
    check["category"] = result.category.value
    check["detail"] = result.description
    check["fix"] = [
        {"type": action.action_type, "label": action.label, "payload": action.payload}
        for action in result.actions
    ]
    check["facts"] = _scrub(result.metadata)
    return check


def _compact_host(metadata: dict[str, Any]) -> dict[str, Any]:
    daemon = metadata.get("daemon") or {}
    host: dict[str, Any] = {
        key: metadata.get(key)
        for key in (
            "server_python",
            "runner_python",
            "interpreter_matches_venv",
            "env_file",
            "env_file_exists",
            "traces_dir",
        )
    }
    if "mcp_client" in metadata:
        host["mcp_client"] = _scrub(metadata["mcp_client"])
    host["daemon"] = {
        key: daemon.get(key)
        for key in ("port", "reachable", "port_held_by_other_process", "log_path")
    }
    return host


def _render_action_lines(action: Any) -> list[str]:
    """Turn one ProbeAction into next_steps lines the assistant can act on verbatim.

    Commands chained with ``&&`` become consecutive ``Run:`` lines (PowerShell
    5.1 cannot parse ``&&``).
    """
    payload = action.payload.strip()
    if action.action_type == "link":
        return [f"  Docs: {payload}"]
    if action.action_type != "command":
        return [f"  Guidance: {payload}"]
    return [
        f"  Run: {command}"
        for command in (part.strip() for part in payload.split("&&"))
        if command and command != "lunatap init"  # interactive; covered by other guidance
    ]


def _credential_steps(env_file: str | None) -> list[str]:
    location = env_file or "the project's .env file"
    return [
        "  Ask the user to add a provider key (for example GEMINI_API_KEY=...) to "
        f"{location}, or to the MCP server's env block in the IDE's MCP config. "
        "Never ask them to paste the key into the chat.",
        "  `lunatap init` is interactive and cannot run from a tool call; edit the env file instead.",
        "  Keys are read when the server starts: restart the MCP server (reload MCP servers in "
        "the IDE) after adding one.",
    ]


# --------------------------------------------------------------------------- #
# Device lock / task state
# --------------------------------------------------------------------------- #


def _normalize_serial(value: Any) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "_", str(value or "").strip())


def _task_state() -> dict[str, list[dict[str, Any]]]:
    """Compact view of who holds or waits for a device, from the cross-process lock files."""
    active: list[dict[str, Any]] = []
    queued: list[dict[str, Any]] = []
    try:
        seen: set[Any] = set()
        for owner in DeviceExecutionLock.get_active_owners().values():
            token = getattr(owner, "token", id(owner))
            if token in seen:
                continue
            seen.add(token)
            active.append(
                {
                    "device": getattr(owner, "device_id", None),
                    "session_id": getattr(owner, "session_id", None),
                    "pid": getattr(owner, "pid", None),
                    "description": getattr(owner, "description", None),
                    "ingress": getattr(owner, "ingress", None),
                    "started_at": getattr(owner, "acquired_at", None),
                }
            )
    except Exception as exc:
        logger.debug(f"Could not read active device locks: {exc}")
    try:
        for ticket in DeviceExecutionLock.get_queued_tasks():
            queued.append(
                {
                    "device": ticket.get("device_id"),
                    "session_id": ticket.get("session_id"),
                    "pid": ticket.get("pid"),
                    "description": ticket.get("goal"),
                    "ingress": ticket.get("ingress"),
                    "created_at": ticket.get("created_at"),
                }
            )
    except Exception as exc:
        logger.debug(f"Could not read queued device tasks: {exc}")
    return {"active": active, "queued": queued}


def _busy_step(tasks: dict[str, list[dict[str, Any]]], serial: str | None) -> str | None:
    if not serial:
        return None
    wanted = _normalize_serial(serial)
    for entry in tasks.get("active", []):
        if _normalize_serial(entry.get("device")) != wanted:
            continue
        task_id = entry.get("session_id") or "unknown"
        description = entry.get("description") or "no description"
        return (
            f"Device '{serial}' is busy with task {task_id} (pid {entry.get('pid')}): "
            f"{description}. Wait for it to finish, or stop it with "
            f'mobile_manage_task(action="stop", trace_id="{task_id}"). A new task on this '
            "device queues behind it."
        )
    return None


# --------------------------------------------------------------------------- #
# Emulator launch
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# Credential verification
# --------------------------------------------------------------------------- #


async def _verify_credentials(cred_result: ProbeResult | None) -> list[dict[str, Any]]:
    """Check every configured key against its provider; the keys never leave this function."""
    if cred_result is None:
        return []
    metadata = cred_result.metadata
    api_keys = metadata.get("api_keys") or {}
    targets: list[tuple[str, str, str]] = []
    for entry in metadata.get("providers") or []:
        provider = str(entry.get("provider") or "").strip()
        raw_key = entry.get("raw_key") or api_keys.get(provider) or ""
        if not provider or not raw_key:
            continue
        targets.append((provider, str(entry.get("label") or provider), str(raw_key)))
    if api_keys.get("ocr"):
        targets.append(("ocr", "Vision OCR", str(api_keys["ocr"])))
    if not targets:
        return []

    outcomes = await asyncio.gather(
        *(
            validate_api_key(
                provider,
                "EMPTY",
                base_url=key,
                timeout=CREDENTIAL_CHECK_TIMEOUT_SECONDS,
            )
            if provider in _ENDPOINT_PROVIDERS
            else validate_api_key(provider, key, timeout=CREDENTIAL_CHECK_TIMEOUT_SECONDS)
            for provider, _label, key in targets
        ),
        return_exceptions=True,
    )
    verified: list[dict[str, Any]] = []
    for (provider, label, key), outcome in zip(targets, outcomes):
        if isinstance(outcome, BaseException):
            valid, message = False, f"verification raised {outcome.__class__.__name__}: {outcome}"
        else:
            valid, message = bool(outcome[0]), str(outcome[1])
        verified.append(
            {
                "provider": provider,
                "label": label,
                "valid": valid,
                "message": message.replace(key, "***"),
            }
        )
    return verified


def _primary_credential(credentials: list[dict[str, Any]] | None) -> dict[str, Any] | None:
    """The provider the task runner will use: first configured LLM provider (OCR is auxiliary)."""
    for entry in credentials or []:
        if entry.get("provider") != "ocr":
            return entry
    return None


def _credential_verification_steps(
    credentials: list[dict[str, Any]] | None, env_file: str | None
) -> tuple[list[str], bool]:
    """Lines for failed verifications; second item says whether a restart is needed."""
    if not credentials:
        return [], False
    steps: list[str] = []
    primary = _primary_credential(credentials)
    needs_restart = False
    for entry in credentials:
        if entry.get("valid"):
            continue
        tag = "REQUIRED" if entry is primary else "OPTIONAL"
        steps.append(
            f"[{tag}] {entry.get('label')} API key ({entry.get('provider')}) failed live "
            f"verification: {entry.get('message')}"
        )
        if entry is primary:
            steps.extend(_credential_steps(env_file))
            needs_restart = True
    return steps, needs_restart


# --------------------------------------------------------------------------- #
# Device smoke test
# --------------------------------------------------------------------------- #


def _probe_unavailable(serial: str | None, error: str) -> dict[str, Any]:
    return {
        "ok": False,
        "serial": serial,
        "elapsed_seconds": 0.0,
        "screenshot_bytes": None,
        "element_count": None,
        "error": error,
        "fix": [],
    }


async def _device_smoke_test(device_serial: str | None, platform: str = "ios") -> dict[str, Any]:
    """Drive the device end to end (screenshot + UI hierarchy) through the shared smoke test."""
    return await smoke_test_device(device_serial, platform=platform)


def _ios_requested_device(
    ios_result: ProbeResult | None, requested_device: str | None
) -> dict[str, Any] | None:
    """Canonical iOS selection from probe metadata, or None when ambiguous.

    Mirrors the driver/pool contract: UDID compare is case-insensitive, names
    match exactly, exactly one match is required, and physical hardware is
    never picked implicitly. Returns the selected entry as
    ``{serial, name, state, is_emulator}``.
    """
    metadata = ios_result.metadata if ios_result else {}
    usable_sims = [
        d
        for d in (metadata.get("simulators") or [])
        if d.get("udid") and d.get("state") in ("Booted", "Shutdown")
    ]
    physical = [d for d in (metadata.get("connected_physical_devices") or []) if d.get("udid")]
    booted = [d for d in usable_sims if d.get("state") == "Booted"]

    def _entry(device: dict[str, Any], *, is_emulator: bool) -> dict[str, Any]:
        state = device.get("state") or ""
        return {
            "serial": str(device.get("udid")),
            "name": device.get("name"),
            "state": ("device" if not is_emulator or state == "Booted" else state or "unknown"),
            "is_emulator": is_emulator,
        }

    if requested_device:
        if requested_device.strip().lower() == "booted":
            return _entry(booted[0], is_emulator=True) if len(booted) == 1 else None
        needle = requested_device.lower()
        matches = [
            (d, True)
            for d in usable_sims
            if str(d.get("udid")).lower() == needle or d.get("name") == requested_device
        ]
        matches += [
            (d, False)
            for d in physical
            if str(d.get("udid")).lower() == needle or d.get("name") == requested_device
        ]
        if len(matches) != 1:
            return None
        device, is_emulator = matches[0]
        return _entry(device, is_emulator=is_emulator)
    if len(booted) == 1:
        return _entry(booted[0], is_emulator=True)
    if len(usable_sims) == 1:
        return _entry(usable_sims[0], is_emulator=True)
    return None


async def _run_device_probe(
    ios_result: ProbeResult | None, requested_device: str | None
) -> dict[str, Any]:
    selected = _ios_requested_device(ios_result, requested_device)
    if selected is None:
        if requested_device:
            return _probe_unavailable(
                requested_device,
                f"requested iOS device '{requested_device}' does not resolve to exactly "
                "one usable iOS device; pass the simulator or paired physical device "
                "UDID listed in the ios_simulators check facts",
            )
        return _probe_unavailable(
            None,
            "no single unambiguous iOS device is available; pass device_serial with an "
            "explicit simulator or paired physical device UDID",
        )
    try:
        return await _device_smoke_test(selected["serial"], platform="ios")
    except Exception as exc:
        return _probe_unavailable(
            selected["serial"], f"iOS smoke test raised {exc.__class__.__name__}: {exc}"
        )


def _device_probe_steps(device_probe: dict[str, Any] | None) -> list[str]:
    if device_probe is None or device_probe.get("ok"):
        return []
    serial = device_probe.get("serial") or "the attached device"
    steps = [f"[REQUIRED] Device probe failed on {serial}: {device_probe.get('error')}"]
    steps.extend(f"  Guidance: {fix}" for fix in device_probe.get("fix") or [])
    return steps


# --------------------------------------------------------------------------- #
# Accessibility helper (UI hierarchy backend)
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# next_steps
# --------------------------------------------------------------------------- #


def _ios_device_steps(
    ios_result: ProbeResult | None,
    *,
    requested_device: str | None,
    tasks: dict[str, list[dict[str, Any]]],
) -> list[str]:
    """Device guidance for iOS: missing/ambiguous targets, busy devices, choices."""
    steps: list[str] = []
    if ios_result is None:
        return steps
    metadata = ios_result.metadata or {}
    simulators = [d for d in (metadata.get("simulators") or []) if d.get("udid")]
    physical = [d for d in (metadata.get("connected_physical_devices") or []) if d.get("udid")]
    ready = [d for d in simulators if d.get("state") == "Booted"] + physical
    selected = _ios_requested_device(ios_result, requested_device)

    def _listing(devices: list[dict[str, Any]]) -> str:
        return ", ".join(f"{d.get('name')} ({d.get('udid')})" for d in devices) or "none"

    if requested_device and selected is None:
        steps.append(
            f"[REQUIRED] Requested iOS device '{requested_device}' did not match exactly one "
            f"device. Simulators: {_listing(simulators)}. Physical: {_listing(physical)}. "
            "Pass a UDID from `xcrun simctl list devices` or `xcrun devicectl list devices`."
        )

    target = selected["serial"] if selected else None
    busy = _busy_step(tasks, target)
    if busy:
        steps.append(busy)

    booted = [d for d in simulators if d.get("state") == "Booted"]
    if not requested_device and len(booted) > 1:
        # Physical devices are never picked implicitly, so only several booted
        # simulators make the default target ambiguous.
        steps.append(
            "Several simulators are booted: ask the user which one to use and pass its UDID as "
            "device_serial. Booted: "
            + _listing(booted)
            + "."
            + (f" Physical (needs its UDID): {_listing(physical)}." if physical else "")
        )
    elif not requested_device and selected is None and not ready:
        steps.append(
            "No iOS simulator is booted and no iPhone/iPad is connected. Pass a simulator UDID "
            "as device_serial (Lunatap boots it), or ask the user to connect a device."
        )
    return steps


def _skipped_physical_hint(ios_result: ProbeResult | None, requested_device: str | None) -> str:
    """When a simulator was picked by default, name the phones that were not."""
    if requested_device or ios_result is None:
        return ""
    metadata = ios_result.metadata or {}
    physical = [d for d in (metadata.get("connected_physical_devices") or []) if d.get("udid")]
    selected = _ios_requested_device(ios_result, None)
    if not physical or selected is None:
        return ""
    listing = ", ".join(f"{d.get('name')} ({d.get('udid')})" for d in physical)
    return (
        f" This report is for the simulator {selected.get('name')} ({selected.get('serial')}); "
        f"to check a physical device instead, pass its UDID as device_serial: {listing}."
    )


def _next_steps(
    results: list[ProbeResult],
    *,
    fixes_applied: list[dict[str, Any]],
    requested_device: str | None,
    env_file: str | None,
    credentials: list[dict[str, Any]] | None,
    tasks: dict[str, list[dict[str, Any]]],
    device_probe: dict[str, Any] | None,
) -> list[str]:
    steps: list[str] = []
    needs_restart = False
    credential_lines, credential_restart = _credential_verification_steps(credentials, env_file)

    for result in sort_by_fix_order(results):
        if result.status is not ProbeStatus.PASS:
            required = result.is_blocker or (
                result.id == "ios_wda_signing" and _wda_signing_blocks(results, requested_device)
            )
            tag = "REQUIRED" if required else "OPTIONAL"
            steps.append(f"[{tag}] {result.title}: {result.description}")
            if result.id == "gemini_api_key" and result.status is ProbeStatus.FAIL:
                steps.extend(_credential_steps(env_file))
                needs_restart = True
            else:
                for action in result.actions:
                    steps.extend(_render_action_lines(action))
            if result.id in ("system_config", "integration_host", "python_runtime"):
                needs_restart = True

        if result.id == "gemini_api_key":
            steps.extend(credential_lines)
            needs_restart = needs_restart or credential_restart

    steps.extend(
        _ios_device_steps(
            _find(results, "ios_simulators"), requested_device=requested_device, tasks=tasks
        )
    )
    steps.extend(_device_probe_steps(device_probe))

    for fix in fixes_applied:
        outcome = "applied" if fix.get("success") else "did not help"
        steps.append(f"Auto-fix {fix['fix']} {outcome}: {fix.get('message', '')}".rstrip())

    if not steps:
        steps.append(
            "Environment is ready. Drive the device yourself with mobile_observe / mobile_act "
            "(harness mode) or delegate a task with mobile_run_task (handoff mode); pass "
            "device_serial when more than one device is attached."
            + _skipped_physical_hint(_find(results, "ios_simulators"), requested_device)
        )
        return steps

    if needs_restart:
        steps.append(
            "After changing the env file, config, or MCP server config: restart the MCP server "
            "(reload MCP servers in the IDE), then call mobile_diagnose again."
        )
    else:
        steps.append("After each fix, call mobile_diagnose again until verdict is 'ready'.")
    return steps


# --------------------------------------------------------------------------- #
# Logs
# --------------------------------------------------------------------------- #


_RECORD_START_RE = re.compile(r"^\[\d\d/\d\d/\d\d \d\d:\d\d:\d\d\]")


def _log_records(text: str) -> list[str]:
    """Join the console log's wrapped lines back into one record each.

    rich wraps a record at the terminal width and indents the continuation
    lines, so a single pydantic error arrives as a dozen fragments.
    """
    records: list[str] = []
    for raw in text.splitlines():
        line = _ANSI_RE.sub("", raw).rstrip()
        if not line.strip():
            continue
        continuation = line[:1].isspace() and not _RECORD_START_RE.match(line)
        if continuation and records:
            records[-1] = f"{records[-1]} {line.strip()}"
        else:
            records.append(line.strip())
    return records


def _tail_errors(path: str, *, max_lines: int = 12, max_chars: int = 400) -> list[str]:
    """Return the last error-looking records of a log without loading the whole file."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as handle:
            handle.seek(max(0, size - 64 * 1024))
            text = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    matches = [
        re.sub(r"\s+", " ", record)[:max_chars]
        for record in _log_records(text)
        if any(marker in record.lower() for marker in _ERROR_MARKERS)
    ]
    return matches[-max_lines:]


def _last_failed_task(limit: int = 30) -> dict[str, Any] | None:
    """Locate the most recent failed trace and surface its error tail inline."""
    traces_dir = Path(trace_store.TRACES_DIR)
    try:
        candidates = [p for p in traces_dir.iterdir() if p.is_dir()]
    except OSError:
        return None

    def _mtime(path: Path) -> float:
        try:
            return path.stat().st_mtime
        except OSError:
            return 0.0

    candidates.sort(key=_mtime, reverse=True)
    for trace_dir in candidates[:limit]:
        status = trace_store.read_status(trace_dir.name)
        if not status or status.get("status") != "failed":
            continue
        stderr_log = trace_store.get_trace_stderr_log_path(trace_dir.name)
        return {
            "trace_id": trace_dir.name,
            "error": (status.get("error") or "")[:600],
            "device_serial": status.get("device_serial"),
            "stderr_log": stderr_log,
            "stdout_log": trace_store.get_trace_stdout_log_path(trace_dir.name),
            "recent_errors": _tail_errors(stderr_log),
        }
    return None


def _collect_logs(host_metadata: dict[str, Any]) -> dict[str, Any]:
    from lunatap.harness.logs import capture_availability

    root = env_utils.get_project_root()
    stderr_log = os.path.join(root, "scratch", "mcp_stderr.log")
    return {
        # What mobile_logs can stream from a device on this Mac.
        "device_log_capture": capture_availability(),
        "mcp_stderr_log": stderr_log,
        "mcp_launch_log": os.path.join(root, "scratch", "mcp_launch_debug.log"),
        "daemon_log": (host_metadata.get("daemon") or {}).get("log_path"),
        "recent_mcp_errors": _tail_errors(stderr_log),
        "last_failed_task": _last_failed_task(),
    }


# --------------------------------------------------------------------------- #
# Self-heal
# --------------------------------------------------------------------------- #


def _public_fix_result(name: str, result: dict[str, Any]) -> dict[str, Any]:
    return {
        "fix": name,
        "success": bool(result.get("success")),
        "message": str(result.get("message") or ""),
        "skipped": bool(result.get("skipped")),
    }


def _cleanup_stale_locks() -> dict[str, Any] | None:
    """Drop lock files / queue tickets of dead processes; reported only when something changed."""
    try:
        removed = int(DeviceExecutionLock.cleanup_stale_locks())
    except Exception as exc:
        return {
            "fix": "cleanup_stale_locks",
            "success": False,
            "skipped": False,
            "message": f"cleanup raised {exc.__class__.__name__}: {exc}",
        }
    if removed <= 0:
        return None
    return {
        "fix": "cleanup_stale_locks",
        "success": True,
        "skipped": False,
        "message": f"removed {removed} stale device lock(s) / queue ticket(s)",
    }


async def _apply_safe_fixes(report: SystemReadinessReport) -> list[dict[str, Any]]:
    """Run the self-heals the console exposes as buttons, only when they cannot disrupt a task."""
    del report  # only lock cleanup is safe to automate on iOS today
    applied: list[dict[str, Any]] = []
    cleanup = _cleanup_stale_locks()
    if cleanup is not None:
        applied.append(cleanup)
    return applied


# --------------------------------------------------------------------------- #
# Collection / verdict
# --------------------------------------------------------------------------- #


async def _none() -> None:
    return None


async def _run_extras(
    results: list[ProbeResult],
    *,
    requested_device: str | None,
    verify_credentials: bool,
    probe_device: bool,
) -> dict[str, Any]:
    """Optional, slower work: live key checks, device smoke test, lock state."""
    credentials, device_probe = await asyncio.gather(
        _verify_credentials(_find(results, "gemini_api_key")) if verify_credentials else _none(),
        _run_device_probe(_find(results, "ios_simulators"), requested_device)
        if probe_device
        else _none(),
    )
    return {"credentials": credentials, "device_probe": device_probe, "tasks": _task_state()}


def _requested_physical_device(
    results: list[ProbeResult], requested_device: str | None
) -> dict[str, Any] | None:
    """The selected device when it is a physical iPhone/iPad, else None."""
    selected = _ios_requested_device(_find(results, "ios_simulators"), requested_device)
    if selected is None or selected.get("is_emulator"):
        return None
    return selected


def _wda_signing_blocks(results: list[ProbeResult], requested_device: str | None) -> bool:
    """An expired WebDriverAgent profile blocks a run on a physical device."""
    signing = _find(results, "ios_wda_signing")
    if signing is None or signing.status is not ProbeStatus.FAIL:
        return False
    return _requested_physical_device(results, requested_device) is not None


def _requested_device_ready(results: list[ProbeResult], requested_device: str | None) -> bool:
    """A caller-named device counts as a blocker: it must resolve to exactly one device."""
    if not requested_device:
        return True
    ios_result = _find(results, "ios_simulators")
    if ios_result is None:
        return False
    return _ios_requested_device(ios_result, requested_device) is not None


def _verdict(
    results: list[ProbeResult],
    *,
    requested_device: str | None,
    credentials: list[dict[str, Any]] | None,
    device_probe: dict[str, Any] | None,
) -> str:
    verdict = base_verdict(results)
    ios_result = _find(results, "ios_simulators")
    if verdict == "blocked" or ios_result is None or ios_result.status is not ProbeStatus.PASS:
        return "blocked"
    if not _requested_device_ready(results, requested_device):
        return "blocked"
    if _wda_signing_blocks(results, requested_device):
        return "blocked"
    primary = _primary_credential(credentials)
    if primary is not None and not primary.get("valid"):
        return "blocked"
    if device_probe is not None and not device_probe.get("ok"):
        return "blocked"
    if any(not entry.get("valid") for entry in credentials or []):
        return "degraded"
    return verdict


def _summary(
    results: list[ProbeResult],
    verdict: str,
    *,
    credentials: list[dict[str, Any]] | None,
    device_probe: dict[str, Any] | None,
) -> str:
    blockers = [r for r in results if r.is_blocker]
    passed = [r for r in blockers if r.status is ProbeStatus.PASS]
    line = f"{len(passed)}/{len(blockers)} required checks pass ({verdict})."
    failing = [
        f"{r.title}: {r.summary}"
        for r in sort_by_fix_order(results)
        if r.status is not ProbeStatus.PASS
    ]
    failing.extend(
        f"{entry.get('label')} key verification: {entry.get('message')}"
        for entry in credentials or []
        if not entry.get("valid")
    )
    if device_probe is not None and not device_probe.get("ok"):
        failing.append(f"Device probe: {device_probe.get('error')}")
    if failing:
        line += " Attention: " + "; ".join(failing) + "."
    return line


def _timeout_response(fixes_applied: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "verdict": "blocked",
        "summary": (
            f"Diagnosis did not finish within {DIAGNOSIS_TIMEOUT_SECONDS:.0f}s; "
            "`xcrun simctl`, `xcrun devicectl`, the device, or the toolchain scan is hung."
        ),
        "next_steps": [
            "Run: xcrun simctl list devices",
            "If that hangs too, ask the user to quit Simulator (or replug the iPhone), then "
            "call mobile_diagnose again.",
        ],
        "checks": [],
        "host": {},
        "device": None,
        "tasks": _task_state(),
        "credentials": None,
        "device_probe": None,
        "fixes_applied": fixes_applied,
        "logs": _collect_logs({}),
    }


@mcp.tool()
async def mobile_diagnose(
    attempt_fix: bool = False,
    device_serial: str | None = None,
    verify_credentials: bool = False,
    probe_device: bool = False,
    platform: str | None = None,
) -> dict[str, Any]:
    """Diagnoses why Lunatap cannot run tasks from this IDE and returns the fixes.

    Call this FIRST whenever another Lunatap tool errors, a task fails to
    start, no device is found, or the user says Lunatap "does not work".
    It checks, in fix order: Python runtime, config file, the MCP host
    (interpreter vs project venv, .env location, traces directory, daemon
    port), LLM credentials, iOS devices (Xcode, simulators, paired
    iPhones/iPads), and the optional video toolchain. A plain call takes a
    few seconds; the optional extras cost more (see Args).

    Returns a dict:
      - `verdict`: "ready" | "degraded" (optional pieces missing) | "blocked".
      - `summary`: one line with the pass count and what needs attention.
      - `next_steps`: ordered instructions. `[REQUIRED]`/`[OPTIONAL]` lines
        name a problem; the indented lines under them are the fix. `Run:`
        lines are single, local, non-destructive shell commands you may
        execute yourself, one per line (ask before installing software).
        `Guidance:` lines need the user, explain a setting, or tell you
        which mobile_diagnose call to make next. Follow them top to bottom,
        then call this tool again until `verdict` is "ready".
      - `checks`: per-check status. Passing checks are one line (id, title,
        status, required, summary); failing ones add detail, fix, facts.
        Secrets are never included.
      - `host`: how the MCP server was launched: server_python,
        runner_python, interpreter_matches_venv, env_file, env_file_exists,
        traces_dir, mcp_client (when known), daemon {port, reachable,
        port_held_by_other_process, log_path}.
      - `device`: the iOS device a task would use ({serial, model, platform,
        is_emulator}) or null when none is unambiguous.
      - `tasks`: {"active": [...], "queued": [...]} Lunatap tasks holding or
        waiting for a device (device, session_id, pid, description, ingress,
        started_at/created_at). Stop a stuck one with
        mobile_manage_task(action="stop", trace_id=<session_id>).
      - `credentials`: null unless verify_credentials; else
        [{provider, label, valid, message}] per configured key.
      - `device_probe`: null unless probe_device; else {ok, serial,
        elapsed_seconds, screenshot_bytes, element_count, error, fix}.
      - `fixes_applied`: what `attempt_fix` did ({fix, success, skipped, message}).
      - `logs`: paths to the MCP server logs and the daemon log, recent
        error lines, and the most recent failed task with its error, log
        paths and `recent_errors` (tail of its stderr log).

    Args:
        attempt_fix: When true, removes device locks left by dead processes,
          then re-runs the checks. Nothing else is changed.
        device_serial: Optional simulator or paired physical device UDID (or
          an exact device name) the user wants to use; the report then states
          whether it resolves to exactly one device and whether it is busy.
        verify_credentials: When true, checks every configured API key
          against its provider over the network (about 12 seconds worst
          case, keys stay on the server). Use it when tasks fail with
          authentication, quota or model errors although the key check
          passes. An invalid primary key makes the verdict "blocked".
        probe_device: When true, drives the selected device end to end
          (screenshot + UI hierarchy, about 20 seconds; WebDriverAgent or
          Xcode's device tools) to prove a task can really start. Use it when
          the checks pass but tasks still fail on the device. A failed probe
          makes the verdict "blocked" and lists the fix.
        platform: "ios", the only supported platform (the default).
    """
    fixes_applied: list[dict[str, Any]] = []
    try:
        normalize_device_platform(platform)
    except ValueError as exc:
        return {"verdict": "blocked", "error": str(exc)}
    requested_device = device_serial.strip() if device_serial and device_serial.strip() else None
    try:
        report, host = await asyncio.wait_for(
            collect_readiness(), timeout=DIAGNOSIS_TIMEOUT_SECONDS
        )
        if attempt_fix:
            fixes_applied = await asyncio.wait_for(
                _apply_safe_fixes(report), timeout=DIAGNOSIS_TIMEOUT_SECONDS
            )
        results = [*report.probes, host]
        extras = await asyncio.wait_for(
            _run_extras(
                results,
                requested_device=requested_device,
                verify_credentials=verify_credentials,
                probe_device=probe_device,
            ),
            timeout=DIAGNOSIS_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        return _timeout_response(fixes_applied)

    credentials: list[dict[str, Any]] | None = extras["credentials"]
    device_probe: dict[str, Any] | None = extras["device_probe"]
    verdict = _verdict(
        results,
        requested_device=requested_device,
        credentials=credentials,
        device_probe=device_probe,
    )
    selected = _ios_requested_device(_find(results, "ios_simulators"), requested_device)
    device = (
        {
            "serial": selected["serial"],
            "model": selected["name"],
            "platform": "ios",
            "is_emulator": selected["is_emulator"],
        }
        if selected
        else None
    )
    env_file = host.metadata.get("env_file")
    return {
        "verdict": verdict,
        "summary": _summary(results, verdict, credentials=credentials, device_probe=device_probe),
        "next_steps": _next_steps(
            results,
            fixes_applied=fixes_applied,
            requested_device=requested_device,
            env_file=env_file,
            credentials=credentials,
            tasks=extras["tasks"],
            device_probe=device_probe,
        ),
        "checks": [_render_check(r) for r in sort_by_fix_order(results)],
        "host": _compact_host(host.metadata),
        "device": device,
        "tasks": extras["tasks"],
        "credentials": credentials,
        "device_probe": device_probe,
        "fixes_applied": fixes_applied,
        "logs": _collect_logs(host.metadata),
    }

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

"""Unit tests for the mobile_diagnose MCP tool."""

import asyncio
from contextlib import ExitStack
import inspect
import shutil
import tempfile
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from lunatap.core.diagnostics.probes.host_probe import IntegrationHostProbe
from lunatap.core.diagnostics.schema import (
    DeviceInfo,
    ProbeAction,
    ProbeCategory,
    ProbeResult,
    ProbeStatus,
    SystemReadinessReport,
)
from lunatap.runtime import trace_store
from lunatap.runtime.device_lock import DeviceLockOwner
from mcp_server.tools import diagnose
from mcp_server.tools.diagnose import mobile_diagnose


@pytest.fixture
def temp_trace_env(monkeypatch):
    temp_dir = tempfile.mkdtemp()
    monkeypatch.setattr(trace_store, "TRACES_DIR", temp_dir)
    yield temp_dir
    shutil.rmtree(temp_dir, ignore_errors=True)


def _probe(
    probe_id: str,
    status: ProbeStatus,
    *,
    blocker: bool = True,
    category: ProbeCategory = ProbeCategory.RUNTIME,
    summary: str = "",
    description: str = "",
    metadata: dict | None = None,
    actions: list[ProbeAction] | None = None,
) -> ProbeResult:
    return ProbeResult(
        id=probe_id,
        category=category,
        title=probe_id.replace("_", " ").title(),
        status=status,
        is_blocker=blocker,
        summary=summary or status.value,
        description=description or f"{probe_id} is {status.value}",
        metadata=metadata or {},
        actions=actions or [],
    )


def _credentials_metadata() -> dict:
    return {
        "providers": [
            {
                "provider": "google",
                "label": "Gemini",
                "masked": "AIza...abcd",
                "raw_key": "SECRET-GOOGLE",
                "key": "SECRET-GOOGLE",
            },
            {
                "provider": "openai",
                "label": "ChatGPT",
                "masked": "sk-...wxyz",
                "raw_key": "SECRET-OPENAI",
                "key": "SECRET-OPENAI",
            },
        ],
        "api_keys": {
            "google": "SECRET-GOOGLE",
            "gemini": "SECRET-GOOGLE",
            "openai": "SECRET-OPENAI",
            "ocr": "SECRET-OCR",
        },
        "current_key": "SECRET-GOOGLE",
        "current_gemini_key": "SECRET-GOOGLE",
        "has_ocr_key": True,
    }


def _healthy_probes() -> list[ProbeResult]:
    return [
        _probe("python_runtime", ProbeStatus.PASS),
        _probe("system_config", ProbeStatus.PASS),
        _probe("toolchain", ProbeStatus.PASS, blocker=False, category=ProbeCategory.TOOLCHAIN),
        _probe(
            "gemini_api_key",
            ProbeStatus.PASS,
            category=ProbeCategory.CREDENTIALS,
            metadata=_credentials_metadata(),
        ),
        _probe(
            "vision_ocr_key", ProbeStatus.PASS, blocker=False, category=ProbeCategory.CREDENTIALS
        ),
        _ios_probe(),
    ]


def _host(status: ProbeStatus = ProbeStatus.PASS, **metadata) -> ProbeResult:
    base = {
        "os": "win32",
        "project_root": "/proj",
        "server_python": "/proj/.venv/bin/python",
        "runner_python": "/proj/.venv/bin/python",
        "venv_python": "/proj/.venv/bin/python",
        "venv_exists": True,
        "interpreter_matches_venv": True,
        "env_file": "/proj/.env",
        "env_file_exists": True,
        "traces_dir": "/proj/traces",
        "traces_dir_writable": True,
        "traces_dir_error": None,
        "daemon": {
            "host": "127.0.0.1",
            "port": 8000,
            "standalone_forced": False,
            "reachable": False,
            "port_in_use": False,
            "port_held_by_other_process": False,
            "log_path": "/logs/daemon.log",
        },
    }
    base.update(metadata)
    # Mirrors the real probe: a WARN there degrades, it does not block.
    return _probe("integration_host", status, blocker=status is not ProbeStatus.WARN, metadata=base)


def _report(probes: list[ProbeResult], active_device: DeviceInfo | None = None):
    blockers = [p for p in probes if p.is_blocker]
    passed = [p for p in blockers if p.status is ProbeStatus.PASS]
    return SystemReadinessReport(
        overall_ready=len(blockers) == len(passed),
        blocker_count=len(blockers),
        passed_blocker_count=len(passed),
        probes=probes,
        active_device=active_device,
        os_type="linux",
        timestamp=time.time(),
    )


def _owner(device_id: str = "pixel-1", session_id: str = "trace-123") -> DeviceLockOwner:
    return DeviceLockOwner(
        pid=777,
        process_created_at=1.0,
        token="tok-1",
        device_id=device_id,
        description="Open settings and toggle wifi",
        acquired_at="2026-09-09T10:00:00+00:00",
        session_id=session_id,
        ingress="mcp",
        lock_scope=None,
    )


def _run(
    probes,
    host=None,
    *,
    report=None,
    run_all=None,
    active_owners=None,
    queued=None,
    cleanup=0,
    validate=None,
    smoke=None,
    **kwargs,
):
    """Run the tool with every side-effecting collaborator stubbed."""
    with ExitStack() as stack:
        stack.enter_context(
            patch.object(
                diagnose.readiness_engine,
                "run_all",
                run_all or AsyncMock(return_value=report or _report(probes)),
            )
        )
        stack.enter_context(
            patch.object(IntegrationHostProbe, "probe", AsyncMock(return_value=host or _host()))
        )
        stack.enter_context(
            patch.object(
                diagnose.DeviceExecutionLock,
                "get_active_owners",
                MagicMock(return_value=active_owners or {}),
            )
        )
        stack.enter_context(
            patch.object(
                diagnose.DeviceExecutionLock,
                "get_queued_tasks",
                MagicMock(return_value=queued or []),
            )
        )
        stack.enter_context(
            patch.object(
                diagnose.DeviceExecutionLock,
                "cleanup_stale_locks",
                cleanup if isinstance(cleanup, MagicMock) else MagicMock(return_value=cleanup),
            )
        )
        stack.enter_context(
            patch.object(
                diagnose,
                "validate_api_key",
                validate or AsyncMock(return_value=(True, "verified")),
            )
        )
        stack.enter_context(
            patch.object(
                diagnose,
                "_device_smoke_test",
                smoke or AsyncMock(return_value=_smoke_ok()),
            )
        )
        return asyncio.run(mobile_diagnose(**kwargs))


def _smoke_ok(serial: str = "pixel-1") -> dict:
    return {
        "ok": True,
        "serial": serial,
        "elapsed_seconds": 3.2,
        "screenshot_bytes": 120_000,
        "element_count": 42,
        "error": None,
        "fix": [],
    }


# --------------------------------------------------------------------------- #
# Schema / shape
# --------------------------------------------------------------------------- #


def test_tool_signature_and_registration():
    sig = inspect.signature(mobile_diagnose)
    assert sig.parameters["attempt_fix"].default is False
    assert sig.parameters["device_serial"].default is None
    assert "launch_avd" not in sig.parameters  # Android-only, removed
    assert sig.parameters["verify_credentials"].default is False
    assert sig.parameters["probe_device"].default is False
    doc = mobile_diagnose.__doc__ or ""
    for field in (
        "next_steps",
        "checks",
        "host",
        "device",
        "tasks",
        "credentials",
        "device_probe",
        "fixes_applied",
        "logs",
        "verify_credentials",
        "probe_device",
    ):
        assert field in doc

    from mcp_server.base import mcp

    tool_names = {t.name for t in asyncio.run(mcp.list_tools())}
    assert "mobile_diagnose" in tool_names


def test_ready_environment_reports_ready_with_slim_shape(temp_trace_env):
    result = _run(_healthy_probes())

    assert result["verdict"] == "ready"
    assert "overall_ready" not in result
    assert "active_device" not in result
    (ready,) = result["next_steps"]
    assert ready.startswith("Environment is ready. Drive the device yourself with mobile_observe")
    assert "mobile_run_task (handoff mode)" in ready
    assert "SECRET" not in repr(result)

    # Passing checks are one line each: no detail / fix / facts.
    for check in result["checks"]:
        assert check["status"] == "pass"
        assert set(check) == {"id", "title", "status", "required", "summary"}

    # Fix order: runtime first, then host, credentials, device, toolchain.
    assert [c["id"] for c in result["checks"]][:5] == [
        "python_runtime",
        "system_config",
        "integration_host",
        "gemini_api_key",
        "ios_simulators",
    ]
    assert result["device"] == {
        "serial": "SIM-AAAA",
        "model": "Sim Booted",
        "platform": "ios",
        "is_emulator": True,
    }
    assert result["host"] == {
        "server_python": "/proj/.venv/bin/python",
        "runner_python": "/proj/.venv/bin/python",
        "interpreter_matches_venv": True,
        "env_file": "/proj/.env",
        "env_file_exists": True,
        "traces_dir": "/proj/traces",
        "daemon": {
            "port": 8000,
            "reachable": False,
            "port_held_by_other_process": False,
            "log_path": "/logs/daemon.log",
        },
    }
    assert "emulator" not in result
    assert result["credentials"] is None
    assert result["device_probe"] is None
    assert result["tasks"] == {"active": [], "queued": []}
    assert result["logs"]["mcp_stderr_log"].endswith("mcp_stderr.log")
    assert result["logs"]["daemon_log"] == "/logs/daemon.log"


def test_host_mcp_client_is_forwarded_when_probe_reports_it(temp_trace_env):
    result = _run(_healthy_probes(), host=_host(mcp_client={"name": "cursor", "key": "SECRET"}))
    assert result["host"]["mcp_client"] == {"name": "cursor"}
    assert "SECRET" not in repr(result)


def test_failing_checks_keep_scrubbed_facts_and_fix_list(temp_trace_env):
    probes = _healthy_probes()
    probes[3].status = ProbeStatus.WARN
    probes[5].status = ProbeStatus.WARN
    result = _run(probes)

    creds = next(c for c in result["checks"] if c["id"] == "gemini_api_key")
    assert creds["category"] == "auth"
    assert creds["facts"]["providers"] == [
        {"provider": "google", "label": "Gemini", "masked": "AIza...abcd"},
        {"provider": "openai", "label": "ChatGPT", "masked": "sk-...wxyz"},
    ]
    assert "api_keys" not in creds["facts"]
    assert creds["fix"] == []
    assert "SECRET" not in repr(result)

    ios = next(c for c in result["checks"] if c["id"] == "ios_simulators")
    assert ios["status"] == "warn"
    assert [sim["udid"] for sim in ios["facts"]["simulators"]] == ["SIM-AAAA", "SIM-BBBB"]


def test_optional_toolchain_failure_is_degraded(temp_trace_env):
    probes = _healthy_probes()
    probes[2] = _probe(
        "toolchain",
        ProbeStatus.FAIL,
        blocker=False,
        category=ProbeCategory.TOOLCHAIN,
        summary="Missing scrcpy",
        description="scrcpy is not installed; video capture is disabled.",
        actions=[
            ProbeAction(action_type="command", label="Install", payload="brew install scrcpy")
        ],
    )
    result = _run(probes)

    assert result["verdict"] == "degraded"
    assert result["next_steps"][0] == (
        "[OPTIONAL] Toolchain: scrcpy is not installed; video capture is disabled."
    )
    assert "  Run: brew install scrcpy" in result["next_steps"]


# --------------------------------------------------------------------------- #
# Command hygiene
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# Credentials guidance
# --------------------------------------------------------------------------- #


def test_missing_credentials_point_to_env_file_not_chat(temp_trace_env):
    probes = _healthy_probes()
    probes[3] = _probe(
        "gemini_api_key",
        ProbeStatus.FAIL,
        category=ProbeCategory.CREDENTIALS,
        summary="Key Missing",
        actions=[
            ProbeAction(action_type="command", label="Init", payload="lunatap init"),
            ProbeAction(action_type="link", label="Key", payload="https://aistudio.google.com"),
        ],
    )
    result = _run(probes, host=_host(env_file="/home/u/artemis/.env"))

    steps = result["next_steps"]
    assert result["verdict"] == "blocked"
    assert not any(s.strip() == "Run: lunatap init" for s in steps)
    assert any("/home/u/artemis/.env" in s and "Never ask them to paste" in s for s in steps)
    assert any("interactive" in s for s in steps)
    assert steps[-1].startswith("After changing the env file")


def test_host_problems_come_before_credentials_and_request_restart(temp_trace_env):
    """A host WARN (interpreter mismatch, missing venv, squatted port) is a
    degradation the runner works around: the verdict is degraded, never
    blocked, so a pip/conda checkout is not stuck in a diagnose loop."""
    host = _host(ProbeStatus.WARN, summary="Host Warning")
    host.actions = [
        ProbeAction(
            action_type="command", label="Regen", payload="uv run lunatap mcp --install claude"
        )
    ]
    result = _run(_healthy_probes(), host=host)

    steps = result["next_steps"]
    assert result["verdict"] == "degraded"
    assert steps[0].startswith("[OPTIONAL] Integration Host")
    assert "  Run: uv run lunatap mcp --install claude" in steps
    assert "restart the MCP server" in steps[-1]


def test_unwritable_traces_dir_blocks(temp_trace_env):
    host = _host(ProbeStatus.FAIL, summary="Host Misconfigured", traces_dir_writable=False)
    result = _run(_healthy_probes(), host=host)
    assert result["verdict"] == "blocked"
    assert result["next_steps"][0].startswith("[REQUIRED] Integration Host")


# --------------------------------------------------------------------------- #
# Requested device
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# launch_avd
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# verify_credentials
# --------------------------------------------------------------------------- #


def test_verify_credentials_reports_every_provider_without_leaking_keys(temp_trace_env):
    validate = AsyncMock(return_value=(True, "verified"))
    result = _run(_healthy_probes(), validate=validate, verify_credentials=True)

    assert result["verdict"] == "ready"
    assert result["credentials"] == [
        {"provider": "google", "label": "Gemini", "valid": True, "message": "verified"},
        {"provider": "openai", "label": "ChatGPT", "valid": True, "message": "verified"},
        {"provider": "ocr", "label": "Vision OCR", "valid": True, "message": "verified"},
    ]
    called = {call.args[0]: call.args[1] for call in validate.await_args_list}
    assert called == {"google": "SECRET-GOOGLE", "openai": "SECRET-OPENAI", "ocr": "SECRET-OCR"}
    assert all(call.kwargs["timeout"] == 12.0 for call in validate.await_args_list)
    assert "SECRET" not in repr(result)


def test_verify_credentials_calls_endpoint_providers_by_url(temp_trace_env):
    """A custom / Ollama / vLLM entry carries its base URL as the "key": the
    check must hit that endpoint (no auth header) instead of validating the
    URL as if it were an API key."""
    validate = AsyncMock(return_value=(True, "verified"))
    cred = _probe(
        "gemini_api_key",
        ProbeStatus.PASS,
        category=ProbeCategory.CREDENTIALS,
        metadata={
            "providers": [
                {
                    "provider": "ollama",
                    "label": "Local Ollama",
                    "raw_key": "http://localhost:11434/v1",
                },
                {"provider": "google", "label": "Gemini", "raw_key": "SECRET-GOOGLE"},
            ],
            "api_keys": {},
        },
    )
    probes = [p for p in _healthy_probes() if p.id != "gemini_api_key"] + [cred]
    _run(probes, validate=validate, verify_credentials=True)

    calls = {call.args[0]: call for call in validate.await_args_list}
    assert calls["ollama"].args[1] == "EMPTY"
    assert calls["ollama"].kwargs["base_url"] == "http://localhost:11434/v1"
    assert calls["google"].args[1] == "SECRET-GOOGLE"
    assert "base_url" not in calls["google"].kwargs


def test_invalid_primary_credential_blocks_and_redacts_message(temp_trace_env):
    async def _validate(provider, api_key, timeout=12.0):
        if provider == "google":
            return False, f"Gemini API verification failed (400): key {api_key} invalid"
        if provider == "ocr":
            raise RuntimeError("boom SECRET-OCR")
        return True, "ok"

    result = _run(
        _healthy_probes(),
        host=_host(env_file="/home/u/.env"),
        validate=AsyncMock(side_effect=_validate),
        verify_credentials=True,
    )

    assert result["verdict"] == "blocked"
    assert "SECRET" not in repr(result)
    google, openai, ocr = result["credentials"]
    assert google["valid"] is False and "***" in google["message"]
    assert openai["valid"] is True
    assert ocr["valid"] is False and ocr["message"].startswith("verification raised RuntimeError")
    steps = result["next_steps"]
    required = next(s for s in steps if s.startswith("[REQUIRED] Gemini API key (google)"))
    assert "failed live verification" in required
    assert steps[steps.index(required) + 1].startswith("  Ask the user to add a provider key")
    assert any("/home/u/.env" in s for s in steps)
    assert any(s.startswith("[OPTIONAL] Vision OCR API key (ocr)") for s in steps)
    assert steps[-1].startswith("After changing the env file")
    assert "Gemini key verification" in result["summary"]


def test_invalid_secondary_credential_only_degrades(temp_trace_env):
    async def _validate(provider, api_key, timeout=12.0):
        return (provider != "openai", "ok" if provider != "openai" else "401 unauthorized")

    result = _run(
        _healthy_probes(), validate=AsyncMock(side_effect=_validate), verify_credentials=True
    )
    assert result["verdict"] == "degraded"
    assert any(s.startswith("[OPTIONAL] ChatGPT API key (openai)") for s in result["next_steps"])


# --------------------------------------------------------------------------- #
# Task / lock state
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# attempt_fix
# --------------------------------------------------------------------------- #


def test_attempt_fix_records_stale_lock_cleanup_only_when_something_was_removed(temp_trace_env):
    run_all = AsyncMock(return_value=_report(_healthy_probes()))

    result = _run(_healthy_probes(), run_all=run_all, cleanup=0, attempt_fix=True)
    assert result["fixes_applied"] == []
    assert run_all.await_count == 1

    result = _run(_healthy_probes(), run_all=run_all, cleanup=2, attempt_fix=True)
    assert result["fixes_applied"] == [
        {
            "fix": "cleanup_stale_locks",
            "success": True,
            "skipped": False,
            "message": "removed 2 stale device lock(s) / queue ticket(s)",
        }
    ]
    assert run_all.await_count == 2  # lock cleanup does not change probe results: no re-run
    assert any(s.startswith("Auto-fix cleanup_stale_locks applied") for s in result["next_steps"])

    failing = MagicMock(side_effect=PermissionError("locked dir"))
    result = _run(_healthy_probes(), cleanup=failing, attempt_fix=True)
    assert result["fixes_applied"] == [
        {
            "fix": "cleanup_stale_locks",
            "success": False,
            "skipped": False,
            "message": "cleanup raised PermissionError: locked dir",
        }
    ]

    # Without attempt_fix the cleanup is never attempted.
    failing.reset_mock()
    _run(_healthy_probes(), cleanup=failing)
    failing.assert_not_called()


# --------------------------------------------------------------------------- #
# probe_device
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# Timeout / logs
# --------------------------------------------------------------------------- #


def test_hung_probe_yields_blocked_verdict_instead_of_hanging(temp_trace_env, monkeypatch):
    async def _never():
        await asyncio.sleep(10)

    monkeypatch.setattr(diagnose, "DIAGNOSIS_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(diagnose, "collect_readiness", _never)
    with (
        patch.object(diagnose.DeviceExecutionLock, "get_active_owners", return_value={}),
        patch.object(diagnose.DeviceExecutionLock, "get_queued_tasks", return_value=[]),
    ):
        result = asyncio.run(mobile_diagnose())

    assert result["verdict"] == "blocked"
    assert result["checks"] == []
    assert result["next_steps"][0] == "Run: xcrun simctl list devices"
    assert "hung" in result["summary"]
    for field in ("device", "credentials", "device_probe"):
        assert result[field] is None
    assert result["tasks"] == {"active": [], "queued": []}


def test_hung_device_probe_still_times_out(temp_trace_env, monkeypatch):
    async def _hang(serial):
        await asyncio.sleep(10)

    monkeypatch.setattr(diagnose, "DIAGNOSIS_TIMEOUT_SECONDS", 0.05)
    result = _run(_healthy_probes(), smoke=AsyncMock(side_effect=_hang), probe_device=True)
    assert result["verdict"] == "blocked"
    assert "hung" in result["summary"]


def test_logs_surface_recent_errors_and_last_failed_task(temp_trace_env, monkeypatch):
    monkeypatch.setattr(diagnose.env_utils, "get_project_root", lambda: temp_trace_env)
    scratch = diagnose.Path(temp_trace_env) / "scratch"
    scratch.mkdir()
    (scratch / "mcp_stderr.log").write_text(
        "info: booted\n\x1b[33mTraceback (most recent call last):\x1b[0m\nModuleNotFoundError: No module named 'x'\nall good\n",
        encoding="utf-8",
    )
    trace_store.init_trace("ok-trace", "fine", "Flash", None)
    trace_store.update_trace_status("ok-trace", "completed")
    trace_store.init_trace("bad-trace", "broken", "Flash", None)
    trace_store.update_trace_status("bad-trace", "failed", error="adb: device offline")
    with open(trace_store.get_trace_stderr_log_path("bad-trace"), "w", encoding="utf-8") as fh:
        fh.write("starting runner\n")
        fh.write("ERROR adb: device offline\n")
        fh.write("Traceback (most recent call last):\n")
        fh.write("RuntimeError: device offline\n")

    result = _run(_healthy_probes())

    logs = result["logs"]
    assert logs["recent_mcp_errors"] == [
        "Traceback (most recent call last):",
        "ModuleNotFoundError: No module named 'x'",
    ]
    failed = logs["last_failed_task"]
    assert failed["trace_id"] == "bad-trace"
    assert failed["error"] == "adb: device offline"
    assert failed["stderr_log"].endswith("stderr.log")
    assert failed["recent_errors"] == [
        "ERROR adb: device offline",
        "Traceback (most recent call last):",
        "RuntimeError: device offline",
    ]


# --------------------------------------------------------------------------- #
# Accessibility helper (UI hierarchy backend)
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# iOS device selection
# --------------------------------------------------------------------------- #

_IOS_SIM_BOOTED = {"udid": "SIM-AAAA", "name": "Sim Booted", "state": "Booted"}
_IOS_SIM_SHUT = {"udid": "SIM-BBBB", "name": "Sim Off", "state": "Shutdown"}
_IOS_PHYS = {"udid": "PHYS-0001", "name": "Office Phone", "state": "device"}


def _ios_probe(simulators=None, physical=None, status=ProbeStatus.PASS):
    return _probe(
        "ios_simulators",
        status,
        category=ProbeCategory.DEVICE,
        metadata={
            "xcode_27_or_newer": True,
            "simulators": ([_IOS_SIM_BOOTED, _IOS_SIM_SHUT] if simulators is None else simulators),
            "connected_physical_devices": ([_IOS_PHYS] if physical is None else physical),
        },
    )


def test_ios_requested_device_matches_udid_case_insensitively():
    selected = diagnose._ios_requested_device(_ios_probe(), "sim-aaaa")
    assert selected == {
        "serial": "SIM-AAAA",
        "name": "Sim Booted",
        "state": "device",
        "is_emulator": True,
    }


def test_ios_requested_device_matches_exact_sim_and_physical_names():
    sim = diagnose._ios_requested_device(_ios_probe(), "Sim Off")
    assert sim["serial"] == "SIM-BBBB" and sim["is_emulator"] is True
    phys = diagnose._ios_requested_device(_ios_probe(), "Office Phone")
    assert phys["serial"] == "PHYS-0001" and phys["is_emulator"] is False
    # Name matching is exact — case differences do not bind.
    assert diagnose._ios_requested_device(_ios_probe(), "sim off") is None


def test_ios_requested_device_explicit_booted_needs_exactly_one():
    assert diagnose._ios_requested_device(_ios_probe(), "booted")["serial"] == "SIM-AAAA"
    two_booted = [_IOS_SIM_BOOTED, {**_IOS_SIM_SHUT, "state": "Booted"}]
    assert diagnose._ios_requested_device(_ios_probe(simulators=two_booted), "booted") is None
    none_booted = [dict(_IOS_SIM_SHUT)]
    assert diagnose._ios_requested_device(_ios_probe(simulators=none_booted), "booted") is None


def test_ios_requested_device_ambiguous_names_return_none():
    twins = [
        {**_IOS_SIM_BOOTED, "name": "Twin"},
        {**_IOS_SIM_SHUT, "name": "Twin"},
    ]
    assert diagnose._ios_requested_device(_ios_probe(simulators=twins), "Twin") is None


def test_ios_requested_device_never_autopicks_hardware():
    # No request: exactly one booted sim wins; a sole shutdown sim is next.
    assert diagnose._ios_requested_device(_ios_probe(), None)["serial"] == "SIM-AAAA"
    only_off = diagnose._ios_requested_device(_ios_probe(simulators=[_IOS_SIM_SHUT]), None)
    assert only_off["serial"] == "SIM-BBBB"
    # Physical-only inventory is never auto-selected.
    assert diagnose._ios_requested_device(_ios_probe(simulators=[]), None) is None


def test_ios_probe_serial_routes_canonical_udid(monkeypatch):
    """_run_device_probe must smoke the canonical UDID, not the raw request."""
    smoke = AsyncMock(return_value=_smoke_ok("SIM-AAAA"))
    monkeypatch.setattr(diagnose, "_device_smoke_test", smoke)
    result = asyncio.run(diagnose._run_device_probe(_ios_probe(), "sim-aaaa"))
    smoke.assert_awaited_once_with("SIM-AAAA", platform="ios")
    assert result["ok"] is True


def test_ios_probe_without_canonical_target_fails_with_udid_guidance(monkeypatch):
    smoke = AsyncMock()
    monkeypatch.setattr(diagnose, "_device_smoke_test", smoke)
    result = asyncio.run(diagnose._run_device_probe(_ios_probe(), "ghost"))
    assert result["ok"] is False
    assert "UDID" in result["error"]
    smoke.assert_not_awaited()
    # No request and ambiguous inventory -> same guidance shape.
    twins = [_IOS_SIM_BOOTED, {**_IOS_SIM_SHUT, "state": "Booted"}]
    result = asyncio.run(diagnose._run_device_probe(_ios_probe(simulators=twins), None))
    assert result["ok"] is False
    assert "device_serial" in result["error"]
    smoke.assert_not_awaited()


def test_requested_device_ready_ios_uses_the_same_selection():
    results = [_ios_probe()]
    assert diagnose._requested_device_ready(results, "SIM-AAAA")
    assert diagnose._requested_device_ready(results, "Office Phone")
    assert not diagnose._requested_device_ready(results, "ghost")
    assert diagnose._requested_device_ready(results, None)


def test_ios_diagnose_reports_the_selected_ios_device(temp_trace_env):
    """The iOS `device` payload describes the selected iOS target, not ADB's."""
    probes = _healthy_probes() + [_ios_probe()]
    result = _run(probes)
    device = result["device"]
    assert device == {
        "serial": "SIM-AAAA",
        "model": "Sim Booted",
        "platform": "ios",
        "is_emulator": True,
    }


def test_ios_diagnose_smoke_uses_selected_udid(temp_trace_env):
    probes = _healthy_probes() + [_ios_probe()]
    smoke = AsyncMock(return_value=_smoke_ok("SIM-AAAA"))
    result = _run(probes, platform="ios", probe_device=True, smoke=smoke)
    smoke.assert_awaited_once_with("SIM-AAAA", platform="ios")
    assert result["device_probe"]["ok"] is True


# --------------------------------------------------------------------------- #
# iOS device targeting, busy devices and the device probe
# --------------------------------------------------------------------------- #

_IOS_SIM_TWO = {"udid": "SIM-CCCC", "name": "Sim Two", "state": "Booted"}


def _two_booted_probes() -> list[ProbeResult]:
    return _healthy_probes()[:-1] + [_ios_probe(simulators=[_IOS_SIM_BOOTED, _IOS_SIM_TWO])]


def test_requested_ios_device_missing_blocks_with_the_known_devices(temp_trace_env):
    result = _run(_healthy_probes(), device_serial="SIM-ZZZZ")
    assert result["verdict"] == "blocked"
    step = result["next_steps"][0]
    assert step.startswith("[REQUIRED] Requested iOS device 'SIM-ZZZZ' did not match")
    assert "Sim Booted (SIM-AAAA)" in step and "Office Phone (PHYS-0001)" in step
    assert not any(s.startswith("Environment is ready") for s in result["next_steps"])


def test_requested_ios_device_present_stays_ready(temp_trace_env):
    result = _run(_healthy_probes(), device_serial="sim-aaaa")
    assert result["verdict"] == "ready"
    assert result["device"]["serial"] == "SIM-AAAA"


def test_several_booted_simulators_ask_the_user_to_choose(temp_trace_env):
    result = _run(_two_booted_probes())
    assert result["verdict"] == "ready"
    choose = next(s for s in result["next_steps"] if s.startswith("Several simulators are booted"))
    assert "SIM-AAAA" in choose and "SIM-CCCC" in choose
    assert "Office Phone (PHYS-0001)" in choose  # a phone always needs its UDID


def test_a_connected_phone_next_to_one_booted_simulator_is_not_ambiguous(temp_trace_env):
    result = _run(_healthy_probes())
    assert not any(s.startswith("Several") for s in result["next_steps"])


def test_tasks_surface_active_and_queued_and_busy_device_step(temp_trace_env):
    queued_ticket = {
        "session_id": "trace-456",
        "goal": "Queued goal",
        "device_id": "SIM-AAAA",
        "device_serial": "SIM-AAAA",
        "adb_endpoint_id": None,
        "pid": 888,
        "token": "tok-2",
        "ingress": "cli",
        "status": "pending",
        "created_at": 1700000000.0,
        "start_time": 1700000000.0,
    }
    result = _run(
        _healthy_probes(), active_owners={"SIM-AAAA": _owner("SIM-AAAA")}, queued=[queued_ticket]
    )

    assert [task["device"] for task in result["tasks"]["active"]] == ["SIM-AAAA"]
    assert [task["session_id"] for task in result["tasks"]["queued"]] == ["trace-456"]
    assert result["verdict"] == "ready"
    busy = next(s for s in result["next_steps"] if s.startswith("Device 'SIM-AAAA' is busy"))
    assert 'mobile_manage_task(action="stop", trace_id="trace-123")' in busy


def test_busy_step_only_for_the_targeted_device(temp_trace_env):
    owners = {"SIM-CCCC": _owner("SIM-CCCC")}
    result = _run(_two_booted_probes(), active_owners=owners, device_serial="SIM-AAAA")
    assert not any("is busy" in s for s in result["next_steps"])

    result = _run(_two_booted_probes(), active_owners=owners, device_serial="SIM-CCCC")
    assert any(s.startswith("Device 'SIM-CCCC' is busy") for s in result["next_steps"])

    # Two booted simulators and no requested device: nothing to single out.
    result = _run(_two_booted_probes(), active_owners=owners)
    assert not any("is busy" in s for s in result["next_steps"])


def test_probe_device_ok_keeps_verdict_ready(temp_trace_env):
    smoke = AsyncMock(return_value=_smoke_ok("SIM-AAAA"))
    result = _run(_healthy_probes(), probe_device=True, smoke=smoke)
    smoke.assert_awaited_once_with("SIM-AAAA", platform="ios")
    assert result["verdict"] == "ready"
    assert result["device_probe"]["ok"] is True


def test_probe_device_failure_blocks_with_fix_guidance(temp_trace_env):
    smoke = AsyncMock(
        return_value={
            "ok": False,
            "serial": "SIM-AAAA",
            "error": "WebDriverAgent did not start",
            "fix": ["Run scripts/setup_ios_wda_simulator.sh"],
        }
    )
    result = _run(_healthy_probes(), probe_device=True, smoke=smoke)
    assert result["verdict"] == "blocked"
    assert result["next_steps"][:2] == [
        "[REQUIRED] Device probe failed on SIM-AAAA: WebDriverAgent did not start",
        "  Guidance: Run scripts/setup_ios_wda_simulator.sh",
    ]


def test_probe_device_is_skipped_without_an_unambiguous_device(temp_trace_env):
    smoke = AsyncMock(return_value=_smoke_ok("SIM-AAAA"))
    result = _run(_two_booted_probes(), probe_device=True, smoke=smoke)
    smoke.assert_not_awaited()
    assert result["device_probe"]["ok"] is False
    assert "UDID" in result["device_probe"]["error"]


# --------------------------------------------------------------------------- #
# WebDriverAgent signing (physical devices)
# --------------------------------------------------------------------------- #

_RESIGN = "xcodebuild build-for-testing -project WDA.xcodeproj DEVELOPMENT_TEAM=T"


def _signing_probe(status=ProbeStatus.FAIL):
    return _probe(
        "ios_wda_signing",
        status,
        blocker=False,
        category=ProbeCategory.DEVICE,
        description="WebDriverAgent signing profile expired on 2026-11-11 (team T).",
        actions=[ProbeAction(action_type="command", label="Re-sign WDA", payload=_RESIGN)],
    )


def test_expired_wda_profile_blocks_a_physical_device_with_the_resign_command(temp_trace_env):
    # Live: diagnose said "ready, 5/5" and the next open_app failed on the
    # expired profile; the fix was in build-device.log all along.
    result = _run(_healthy_probes() + [_signing_probe()], device_serial="PHYS-0001")

    assert result["verdict"] == "blocked"
    steps = result["next_steps"]
    required = next(s for s in steps if s.startswith("[REQUIRED] Ios Wda Signing"))
    assert "expired on 2026-11-11" in required
    assert f"  Run: {_RESIGN}" in steps


def test_expired_wda_profile_is_only_optional_for_a_simulator(temp_trace_env):
    result = _run(_healthy_probes() + [_signing_probe()], device_serial="SIM-AAAA")
    assert result["verdict"] == "degraded"  # an optional check failing, as for the toolchain
    assert any(s.startswith("[OPTIONAL] Ios Wda Signing") for s in result["next_steps"])


def test_default_simulator_report_names_the_physical_device_it_skipped(temp_trace_env):
    result = _run(_healthy_probes())
    (ready,) = result["next_steps"]
    assert ready.startswith("Environment is ready.")
    assert "simulator Sim Booted (SIM-AAAA)" in ready and "Office Phone (PHYS-0001)" in ready


def test_wrapped_console_records_are_joined_before_filtering(tmp_path):
    # rich wraps one pydantic error over a dozen indented lines.
    log = tmp_path / "mcp_stderr.log"
    log.write_text(
        "[11/07/26 04:35:45] INFO     Processing request of type            server.py:733\n"
        "                             ListToolsRequest                                   \n"
        "[11/07/26 04:40:47] WARNING  Failed to validate request: 31       session.py:383\n"
        "                             validation errors for ClientRequest                \n"
        "                             PingRequest.method                                 \n"
        "                               Input should be 'ping'                           \n"
        "                             [type=literal_error,                               \n"
        "                             https://errors.pydantic.dev/2.13/v/l               \n"
        "                             iteral_error                                       \n"
        "[11/07/26 04:41:00] INFO     Processing request of type            server.py:733\n",
        encoding="utf-8",
    )
    errors = diagnose._tail_errors(str(log))
    assert len(errors) == 1
    assert errors[0].startswith("[11/07/26 04:40:47] WARNING Failed to validate request: 31")
    assert "PingRequest.method Input should be 'ping' [type=literal_error," in errors[0]

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

import asyncio
import contextlib
import inspect
import os
import re
import threading

import uuid
from io import BytesIO
from pathlib import Path

from dotenv import load_dotenv
from google import genai
from langchain_google_genai import ChatGoogleGenerativeAI
from PIL import Image

from lunatap.config import (
    CheckerConfig,
    run_tuning_for_profile,
    settings,
)
from lunatap.context import (
    ArtemisContext,
    DeviceContext,
    DevicePlatform,
    ExecutionSetup,
)
from lunatap.data_engine.engine import DataEngine
from lunatap.graph.state import State
from lunatap.runtime import DeviceExecutionLock, trace_store
from lunatap.runtime.device_target import IOS_LOCK_SCOPE
from lunatap.runtime.cancel_requests import watch_for_cancel_request
from lunatap.sdk.run_outcome import attach_test_summary, resolve_trace_suffix
from lunatap.sdk.types.agent import AgentConfig
from lunatap.utils.startup_progress import publish_startup_progress
from third_party.mobile_use.sdk.types.exceptions import AgentError, AgentNotInitializedError
from third_party.mobile_use.sdk.types.task import Task
from third_party.mobile_use.sdk.agent import AgentBase, TOutput
from third_party.mobile_use.utils.logger import get_logger

logger = get_logger(__name__)

load_dotenv()


__all__ = ["Agent", "TOutput", "attach_test_summary", "resolve_trace_suffix", "run_tuning_summary"]


def run_tuning_summary(config: AgentConfig, profile: str | None) -> dict[str, str] | None:
    """Per-run Pro tuning (verification level, explorer mode) for an SDK config.

    The SDK config carries the Checker switches as flat flags; fold them back
    into a :class:`CheckerConfig` so the ladder classification stays single-
    sourced in ``lunatap.config``. Returns ``None`` for Flash runs.
    """
    return run_tuning_for_profile(
        profile,
        checker=CheckerConfig(
            enabled=not config.disable_checker,
            midway_checks=not config.disable_midway_checks,
            final_check=not config.disable_final_check,
            assert_failure_policy=config.assert_failure_policy,
        ),
        explorer=config.explorer,
        explorer_versions=dict(config.explorer_versions or {}),
    )


class Agent(AgentBase):
    """Artemis SDK agent."""

    def __init__(
        self,
        *,
        config: AgentConfig | None = None,
        device_id: str | None = None,
        device_serial: str | None = None,
        concurrency_mode: str | None = None,
        max_concurrency: int | None = None,
        session_id: str | None = None,
    ):
        raw_sid = (
            session_id or os.getenv("ARTEMIS_SESSION_ID") or os.getenv("ARTEMIS_CLOUD_SESSION_ID")
        )
        self._session_id: str | None = str(raw_sid).strip() if raw_sid else None
        target_dev = device_serial or device_id
        if config is None:
            from lunatap.sdk.builders import Builders

            builder = Builders.AgentConfig
            if target_dev:
                builder.for_device(DevicePlatform.IOS, target_dev)
            if concurrency_mode:
                builder.with_concurrency_mode(concurrency_mode)
            if max_concurrency is not None:
                builder.with_max_concurrency(max_concurrency)
            self._config = builder.build()
        else:
            self._config = config
            updates = {}
            if target_dev:
                updates["device_id"] = target_dev
                updates["device_platform"] = config.device_platform or DevicePlatform.IOS
            if concurrency_mode:
                updates["concurrency_mode"] = str(concurrency_mode).strip().lower()
            if max_concurrency is not None:
                updates["max_concurrency"] = max_concurrency
            if updates:
                self._config = self._config.model_copy(update=updates)

        self._tasks = []
        self._tmp_traces_dir = Path(settings.TRACES_PATH)
        self._initialized = False
        self._task_lock = asyncio.Lock()
        self._ios_driver = None

    async def _init_internal(
        self,
        api_key: str | None = None,
        retry_count: int = 5,
        retry_wait_seconds: int = 5,
    ):
        if self._config.device_platform not in (None, DevicePlatform.IOS):
            raise AgentError(
                f"Unsupported platform '{self._config.device_platform}'. Lunatap supports iOS only."
            )
        if os.environ.get("ARTEMIS_CLOUD_MODE") == "1":
            raise AgentError("Lunatap runs iOS devices locally; cloud mode is not supported.")
        if self._initialized:
            return True
        from lunatap.drivers.factory import ios_driver_class
        from lunatap.drivers.ios.discovery import BOOTED_SIMULATOR_ID

        publish_startup_progress(
            "device_check", "Checking the iOS device", session_id=self._session_id
        )
        # The picker runs simctl/devicectl subprocesses — keep them off the
        # event loop so init timeouts and progress stays responsive.
        driver_class = await asyncio.to_thread(
            ios_driver_class, self._config.device_id or BOOTED_SIMULATOR_ID
        )
        driver = driver_class(
            device_id=self._config.device_id or BOOTED_SIMULATOR_ID,
            workspace_path=getattr(self._config, "ios_workspace_path", None),
        )
        self._ios_driver = driver
        try:
            # Resolve the device without booting it or opening a native UI
            # session. Mutating setup waits for run_task's execution lease.
            await driver.resolve_device()
        except (OSError, ValueError, RuntimeError, TimeoutError, asyncio.CancelledError):
            await driver.disconnect()
            self._ios_driver = None
            raise
        width, height = driver.screen_size
        self._device_context = DeviceContext(
            host_platform="DARWIN",
            mobile_platform=DevicePlatform.IOS,
            device_id=driver.device_id,
            device_width=width,
            device_height=height,
        )
        # Android read-only ADB probes have no iOS equivalent yet; native
        # simctl recording is supported and honors the configured flag.
        self._config = self._config.model_copy(update={"disable_device_probes": True})
        publish_startup_progress("device_ready", "iOS device selected", session_id=self._session_id)
        asyncio.create_task(self._prewarm_llm_connections(api_key))
        self._initialized = True
        return True

    @contextlib.asynccontextmanager
    async def _ios_operation(self):
        """Serialize one public iOS SDK call behind the device execution lease.

        Called from ``run_task`` itself (internal app installation), the helper
        reuses the lease and native session the task already holds. Called
        publicly, it acquires the same FIFO device lease a task would, then
        opens a short-lived native session that is always closed on exit.
        """
        driver = self._ios_driver
        if not self._initialized or driver is None:
            raise AgentNotInitializedError()
        if asyncio.current_task() is getattr(self, "_current_task", None):
            yield driver
            return
        async with self._task_lock:
            if not self._initialized or self._ios_driver is not driver:
                raise AgentNotInitializedError()
            device_lock = DeviceExecutionLock(
                driver.device_id,
                description="Artemis iOS device operation",
                concurrency_mode=getattr(self._config, "concurrency_mode", "per_device"),
                max_concurrency=getattr(self._config, "max_concurrency", None),
                session_id=self._session_id,
                ingress="sdk",
                lock_scope=IOS_LOCK_SCOPE,
            )
            queue_cancel_event = threading.Event()
            acquire_task = asyncio.create_task(
                asyncio.to_thread(device_lock.acquire, cancel_event=queue_cancel_event)
            )
            connect_attempted = False
            try:
                try:
                    await asyncio.shield(acquire_task)
                except asyncio.CancelledError:
                    queue_cancel_event.set()
                    try:
                        await asyncio.shield(acquire_task)
                    except (OSError, ValueError, RuntimeError, TimeoutError) as exc:
                        # Draining the lock acquisition after cancellation is best effort.
                        logger.debug(
                            f"Device lock acquisition drain after cancel failed: {exc}",
                            exc_info=True,
                        )
                    raise
                connect_attempted = True
                await driver.connect()
                (
                    self._device_context.device_width,
                    self._device_context.device_height,
                ) = driver.screen_size
                yield driver
            finally:
                try:
                    if connect_attempted:
                        try:
                            await driver.disconnect()
                        except (OSError, ValueError, RuntimeError, TimeoutError) as exc:
                            logger.debug(
                                f"iOS driver disconnect after SDK operation failed: {exc}",
                                exc_info=True,
                            )
                finally:
                    await asyncio.to_thread(device_lock.release)

    async def _install_app_internal(self, app_path: str | Path) -> str | None:
        async with self._ios_operation() as driver:
            return await driver.install_app(Path(app_path))

    async def _prepare_app_lock(self, task: Task, context: ArtemisContext):
        if task.request.locked_app_package:
            raise AgentError(
                "iOS app locking is unavailable because foreground ownership cannot be verified."
            )

    async def get_screenshot(self):
        async with self._ios_operation() as driver:
            data = await driver.get_screen_data()
            with Image.open(BytesIO(data.screenshot_bytes)) as image:
                return image.copy()

    async def _prewarm_llm_connections(self, api_key: str | None = None):
        """Pre-warms the HTTP2/gRPC connection pools for both Native GenAI and LangChain clients in the background."""
        if os.environ.get("ARTEMIS_FAKE_LLM") == "1":
            logger.info("ARTEMIS_FAKE_LLM=1 — skipping real LLM connection pre-warming.")
            publish_startup_progress(
                "model_ready", "Model connection is ready (fake LLM)", session_id=self._session_id
            )
            return
        try:
            from lunatap.config.llm import get_default_llm_config

            operator_provider = str(get_default_llm_config().operator.provider)
        except (ValueError, OSError) as exc:
            logger.debug(f"Could not read the operator provider for pre-warming: {exc}")
            operator_provider = "google"
        if operator_provider not in ("google", "vertexai"):
            # Only Gemini connections are pre-warmed; other providers connect on first use.
            publish_startup_progress(
                "model_ready",
                "Model connection will initialize on first use",
                session_id=self._session_id,
            )
            return
        publish_startup_progress(
            "model_warmup", "Warming the model connection", session_id=self._session_id
        )
        logger.info("Starting background pre-warming of Gemini API connection pools...")
        try:
            key = api_key
            if not key and settings.GOOGLE_API_KEY:
                key = settings.GOOGLE_API_KEY.get_secret_value()

            if not key:
                logger.warning("Skipping LLM pre-warming: No API key available.")
                publish_startup_progress(
                    "model_ready",
                    "Model connection will initialize on first use",
                    session_id=self._session_id,
                )
                return

            # 1. Pre-warm Native SDK client
            client = genai.Client(api_key=key)

            # 2. Pre-warm LangChain client
            chat = ChatGoogleGenerativeAI(model="gemini-3.8-flash", google_api_key=key)

            # Fire both calls concurrently in the background
            await asyncio.gather(
                client.aio.models.count_tokens(model="gemini-3.8-flash", contents="ping"),
                chat.ainvoke("ping"),
                return_exceptions=True,
            )
            logger.success("Gemini API connection pools successfully pre-warmed.")
            publish_startup_progress(
                "model_ready", "Model connection is ready", session_id=self._session_id
            )
        except Exception as e:
            logger.warning(f"Failed to pre-warm LLM connections: {e}")
            publish_startup_progress(
                "model_ready",
                "Model connection will initialize on first use",
                session_id=self._session_id,
            )

    async def _watch_external_cancel(self, task_name: str) -> None:
        """Cancel the running task when another process drops a cancel marker.

        The admin console (and other ingresses) cannot deliver a signal to a
        worker portably, so they write a marker keyed by session id / pid.
        Reacting here routes the request through the same cancellation path
        as Ctrl+C: the recording is stopped and remuxed, the trace folder is
        compiled and renamed, and the device lease is released.
        """

        def _on_cancel() -> None:
            logger.warning(
                f"[{task_name}] External cancel request received; stopping the task gracefully."
            )
            self.stop_current_task()

        try:
            await watch_for_cancel_request(_on_cancel, session_id=self._session_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug(f"[{task_name}] Cancel watcher stopped: {exc}")

    async def clean(self, force: bool = False):
        driver = getattr(self, "_ios_driver", None)
        if driver is not None:
            await driver.disconnect()
            self._ios_driver = None
        if not self._initialized and not force:
            return
        self._initialized = False
        logger.info("✅ Lunatap agent stopped.")

    async def _ensure_device_unlocked(self) -> None:
        """Reconnect the iOS driver before a task; a previous task may have closed it."""
        driver = getattr(self, "_ios_driver", None)
        if driver is None:
            raise AgentError("The iOS device driver is not initialized.")
        await driver.connect()
        self._device_context.device_width, self._device_context.device_height = driver.screen_size

    async def _prepare_device_environment(self, context: ArtemisContext):
        """No device environment preparation is needed on iOS."""
        return

    def _prepare_tracing(self, task: Task, context: ArtemisContext):
        """Prepare tracing and data engine setup."""
        driver = getattr(self, "_ios_driver", None)
        if driver is not None:
            from lunatap.mcp.actuators.ios import IosActuator

            context._active_driver = driver
            context.actuator = IosActuator(context)
        task_name = self._prepare_trace_paths(task)

        context.execution_setup = ExecutionSetup(
            traces_path=self._tmp_traces_dir,
            trace_name=task_name,
            enable_remote_tracing=task.request.enable_remote_tracing
            if task.request.record_trace
            else False,
            video_recording_tools_enabled=self._config.video_recording_tools_enabled,
            disable_checker=self._config.disable_checker,
            disable_midway_checks=self._config.disable_midway_checks,
            disable_final_check=self._config.disable_final_check,
            checker_max_iterations=self._config.checker_max_iterations,
            final_check_max_attempts=self._config.final_check_max_attempts,
            checkpoint_max_repairs=self._config.checkpoint_max_repairs,
            max_concurrent_checkpoints=self._config.max_concurrent_checkpoints,
            checkpoint_timeout=self._config.checkpoint_timeout,
            settlement_timeout=self._config.settlement_timeout,
            assert_failure_policy=self._config.assert_failure_policy,
            disable_device_probes=self._config.disable_device_probes,
            disable_planner_validation=self._config.disable_planner_validation,
            enable_committee=self._config.enable_committee,
            committee_debate_rounds=self._config.committee_debate_rounds,
            disable_outputter=self._config.disable_outputter,
            outputter=self._config.outputter,
            explorer=self._config.explorer,
            explorer_versions=self._config.explorer_versions,
        )

        context.data_engine = DataEngine(ctx=context)
        device_data = context.device.model_dump() if context.device else {}
        if task.request.profile:
            device_data["profile"] = task.request.profile
        run_tuning = run_tuning_summary(self._config, task.request.profile)
        if run_tuning:
            device_data["run_tuning"] = run_tuning

        target_sid = (
            self._session_id
            or os.getenv("ARTEMIS_SESSION_ID")
            or os.getenv("ARTEMIS_CLOUD_SESSION_ID")
            or getattr(task, "id", None)
            or getattr(getattr(task, "request", None), "task_name", None)
        )
        sess_uuid = None
        if target_sid:
            try:
                sess_uuid = uuid.UUID(str(target_sid))
            except Exception:
                sess_uuid = str(target_sid)

        context.data_engine.start_session(
            goal=task.request.goal,
            device_info=device_data,
            session_id=sess_uuid,
        )

    async def _finalize_tracing_safely(self, task: Task, context: ArtemisContext):
        """Finalize optional trace artifacts without changing task semantics."""
        try:
            await self._finalize_tracing(task=task, context=context)
        except Exception as exc:
            logger.error(
                f"[{task.get_name()}] Trace artifact finalization failed after task status "
                f"was resolved as '{task.status}': {exc}",
                exc_info=True,
            )

    def _get_graph_state(self, task: Task):
        return State.initial(task.request.goal)

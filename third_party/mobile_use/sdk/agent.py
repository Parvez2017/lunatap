# Copyright 2025-2026 Minitap, Inc.
# Modifications Copyright 2026 Google LLC
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
#
# Originally from mobile-use (https://github.com/minitap-ai/mobile-use).
# See third_party/mobile_use/METADATA for the upstream source and local modifications.

"""Base SDK agent: device/client setup, task creation and the task run loop.

Artemis extends this class in ``lunatap.sdk.agent.Agent`` with tracing setup, device
environment preparation, cancellation watching and UI hierarchy backend reporting.
"""

import asyncio
import os

try:
    from datetime import UTC, datetime
except ImportError:
    from datetime import datetime

    UTC = UTC
import shutil
import threading
import uuid
from pathlib import Path
from platform import system
from shutil import which
from types import NoneType
from typing import Any, TypeVar, overload

from PIL import Image
from pydantic import BaseModel

from lunatap.agents.flash.runner import FlashRunner
from lunatap.agents.outputter.outputter import outputter
from lunatap.config import (
    OutputConfig,
    record_events,
)
from lunatap.constants import RECURSION_LIMIT
from lunatap.context import (
    ArtemisContext,
    DeviceContext,
    DevicePlatform,
    ExecutionSetup,
)
from lunatap.controllers.controller_factory import get_controller
from lunatap.data_engine.trace import DataEngineCallbackHandler
from lunatap.graph.graph import get_graph
from lunatap.graph.state import State
from lunatap.runtime import DeviceExecutionLock
from lunatap.runtime.device_target import IOS_LOCK_SCOPE
from lunatap.sdk.run_outcome import attach_test_summary, resolve_trace_suffix
from lunatap.sdk.types.agent import AgentConfig
from lunatap.utils.startup_progress import publish_startup_progress
from third_party.mobile_use.sdk.builders.task_request_builder import TaskRequestBuilder
from third_party.mobile_use.sdk.types.exceptions import (
    AgentError,
    AgentNotInitializedError,
    AgentProfileNotFoundError,
    AgentTaskRequestError,
)
from third_party.mobile_use.sdk.types.task import (
    AgentProfile,
    Task,
    TaskRequest,
)
from third_party.mobile_use.utils.logger import get_logger
from third_party.mobile_use.utils.media import (
    create_gif_from_trace_folder,
    create_steps_json_from_trace_folder,
    remove_images_from_trace_folder,
    remove_steps_json_from_trace_folder,
)

logger = get_logger(__name__)

TOutput = TypeVar("TOutput", bound=BaseModel | None)


class AgentBase:
    _config: AgentConfig
    _tasks: list[Task] = []
    _tmp_traces_dir: Path
    _initialized: bool = False
    _device_context: DeviceContext
    _current_task: asyncio.Task | None = None
    _task_lock: asyncio.Lock
    _cloud_mobile_id: str | None = None

    async def init(
        self,
        api_key: str | None = None,
        device_id: str | None = None,
        device_serial: str | None = None,
        retry_count: int = 5,
        retry_wait_seconds: int = 5,
    ):
        target_dev = device_serial or device_id
        if target_dev:
            self._config = self._config.model_copy(
                update={
                    "device_id": target_dev,
                    "device_platform": self._config.device_platform or DevicePlatform.IOS,
                }
            )

        return await self._init_internal(
            api_key=api_key,
            retry_count=retry_count,
            retry_wait_seconds=retry_wait_seconds,
        )

    async def _init_internal(
        self,
        api_key: str | None = None,
        retry_count: int = 5,
        retry_wait_seconds: int = 5,
    ):
        # Device setup is platform-specific: lunatap.sdk.agent.Agent implements iOS.
        raise AgentError("Lunatap supports iOS only; use lunatap.sdk.Agent.")

    async def install_app(self, app_path: str | Path) -> str | None:
        """Install an app on the connected device.

        Args:
            app_path: Path to a signed iOS .app bundle or .ipa to install.

        Returns:
            None.

        Raises:
            AgentNotInitializedError: If the agent is not initialized
            AgentError: If installation fails or platform is unsupported
            FileNotFoundError: If the app file/folder doesn't exist
        """
        return await self._install_app_internal(app_path)

    async def _install_app_internal(self, app_path: str | Path) -> str | None:
        # lunatap.sdk.agent.Agent installs .app/.ipa bundles on iOS devices.
        raise AgentError("Lunatap supports iOS only; use lunatap.sdk.Agent.")

    def new_task(self, goal: str):
        """Create a new task request builder.

        Args:
            goal: Natural language description of what to accomplish

        Returns:
            TaskRequestBuilder that can be configured with:
            - .with_output_format() for structured output
            - .with_output_description() for output description
            - .with_locked_app_package() to restrict execution to a specific app
            - .using_profile() to specify an LLM profile
            - .with_max_steps() to set maximum execution steps
            - .with_trace_recording() to enable trace recording
            - .with_name() to set a custom task name
        """
        return TaskRequestBuilder[None].from_common(
            goal=goal,
            common=self._config.task_request_defaults,
        )

    @overload
    async def run_task(
        self,
        *,
        goal: str,
        output: type[TOutput],
        profile: str | AgentProfile | None = None,
        name: str | None = None,
        locked_app_package: str | None = None,
        app_path: str | Path | None = None,
    ) -> TOutput | None: ...

    @overload
    async def run_task(
        self,
        *,
        goal: str,
        output: str,
        profile: str | AgentProfile | None = None,
        name: str | None = None,
        locked_app_package: str | None = None,
        app_path: str | Path | None = None,
    ) -> str | dict | None: ...

    @overload
    async def run_task(
        self,
        *,
        goal: str,
        output=None,
        profile: str | AgentProfile | None = None,
        name: str | None = None,
        locked_app_package: str | None = None,
        app_path: str | Path | None = None,
    ) -> str | None: ...

    @overload
    async def run_task(
        self,
        *,
        request: TaskRequest[None],
        locked_app_package: str | None = None,
        app_path: str | Path | None = None,
    ) -> str | dict | None: ...

    @overload
    async def run_task(
        self,
        *,
        request: TaskRequest[TOutput],
        locked_app_package: str | None = None,
        app_path: str | Path | None = None,
    ) -> TOutput | None: ...

    async def run_task(
        self,
        *,
        goal: str | None = None,
        output: type[TOutput] | str | None = None,
        profile: str | AgentProfile | None = None,
        locked_app_package: str | None = None,
        name: str | None = None,
        app_path: str | Path | None = None,
        request: TaskRequest[TOutput] | None = None,
    ) -> str | dict | TOutput | None:

        # Normal local execution path
        if request is not None:
            if locked_app_package is not None:
                if request.locked_app_package:
                    logger.warning(
                        "Locked app package specified both in the request and"
                        " as a parameter. Using the parameter value."
                    )
                request.locked_app_package = locked_app_package
            # Handle app_path parameter override
            if app_path is not None:
                if request.app_path:
                    logger.warning(
                        "App path specified both in the request and as a"
                        " parameter. Using the parameter value."
                    )
                request.app_path = Path(app_path) if isinstance(app_path, str) else app_path
            return await self._run_task(request=request)
        if goal is None:
            raise AgentTaskRequestError("Goal is required")
        task_request = self.new_task(goal=goal)
        if output is not None:
            if isinstance(output, str):
                task_request.with_output_description(description=output)
            elif output is not NoneType:
                task_request.with_output_format(output_format=output)
        if profile is not None:
            task_request.using_profile(profile=profile)
        if name is not None:
            task_request.with_name(name=name)
        if locked_app_package is not None:
            task_request.with_locked_app_package(package_name=locked_app_package)
        if app_path is not None:
            task_request.with_app_path(app_path=app_path)
        return await self._run_task(task_request.build())

    async def _run_task(
        self,
        request: TaskRequest[TOutput],
    ) -> str | dict | TOutput | None:
        if not self._initialized:
            raise AgentNotInitializedError()

        if request.profile:
            agent_profile = self._config.agent_profiles.get(request.profile)
            if agent_profile is None:
                if request.profile.lower() in (
                    "flash",
                    "pro",
                    "ultra",
                    "default",
                ):
                    agent_profile = self._config.default_profile
                else:
                    raise AgentProfileNotFoundError(request.profile)
        else:
            agent_profile = self._config.default_profile

        logger.info(str(agent_profile))

        on_status_changed = None
        task_id = str(
            self._session_id
            or os.getenv("ARTEMIS_SESSION_ID")
            or os.getenv("ARTEMIS_CLOUD_SESSION_ID")
            or uuid.uuid4()
        )

        task = Task(
            id=task_id,
            device=self._device_context,
            status="pending",
            request=request,
            created_at=datetime.now(),
            on_status_changed=on_status_changed,
        )
        self._tasks.append(task)
        task_name = task.get_name()

        context = ArtemisContext(
            device=self._device_context,
            llm_config=agent_profile.llm_config,
            agent_config=self._config,
        )
        if context.device.mobile_platform == DevicePlatform.IOS:
            context._active_driver = getattr(self, "_ios_driver", None)

        output_config = None
        if request.output_description or request.output_format:
            output_config = OutputConfig(
                output_description=request.output_description,
                structured_output=request.output_format,  # type: ignore
            )
            logger.info(str(output_config))

        logger.info(f"[{task_name}] Starting graph with goal: `{request.goal}`")
        state = self._get_graph_state(task=task)
        graph_input = state.model_dump()
        datetime.now(UTC)

        async def _execute_task_logic():
            last_state: State | None = None
            last_state_snapshot: dict | None = None
            output = None
            effective_mode = getattr(self._config, "concurrency_mode", "per_device")
            effective_max = getattr(self._config, "max_concurrency", None)
            sess_id = (
                self._session_id
                or os.getenv("ARTEMIS_SESSION_ID")
                or os.getenv("ARTEMIS_CLOUD_SESSION_ID")
                or getattr(task, "id", None)
                or getattr(getattr(task, "request", None), "task_name", None)
            )
            # iOS simulators lock under the "ios" scope so a UDID can never
            # collide with an Android serial carrying the same text.
            lock_scope = (
                IOS_LOCK_SCOPE if context.device.mobile_platform == DevicePlatform.IOS else None
            )
            active_owner = DeviceExecutionLock.get_active_owner(
                self._device_context.device_id, lock_scope=lock_scope
            )
            already_held = (
                active_owner is not None
                and active_owner.pid == os.getpid()
                and (
                    (sess_id and str(active_owner.session_id) == str(sess_id))
                    or active_owner.token == self._device_context.device_id
                )
            )
            device_lock = (
                None
                if already_held
                else DeviceExecutionLock(
                    self._device_context.device_id,
                    description=f"{request.goal[:120]}",
                    concurrency_mode=effective_mode,
                    max_concurrency=effective_max,
                    session_id=str(sess_id) if sess_id else None,
                    ingress=os.getenv("ARTEMIS_TASK_INGRESS") or "agent",
                    lock_scope=lock_scope,
                )
            )
            try:
                if device_lock is not None and os.environ.get("ARTEMIS_CLOUD_MODE") != "1":
                    queue_cancel_event = threading.Event()
                    acquire_task = asyncio.create_task(
                        asyncio.to_thread(
                            device_lock.acquire,
                            cancel_event=queue_cancel_event,
                        )
                    )
                    try:
                        await asyncio.shield(acquire_task)
                    except asyncio.CancelledError:
                        queue_cancel_event.set()
                        try:
                            await asyncio.shield(acquire_task)
                        except Exception as exc:  # pylint: disable=broad-exception-caught
                            # Draining the lock acquisition after cancellation is best effort.
                            logger.debug(
                                f"Device lock acquisition drain after cancel failed: {exc}",
                                exc_info=True,
                            )
                        raise
                # All device mutation and UI initialization happens only after
                # this task reaches the head of the one global FIFO queue.
                # Session creation must be inside the same boundary as well:
                # DataEngine uses a shared database, so a queued task must not
                # publish a new active session while the current task is still
                # finishing.
                await self._ensure_device_unlocked()
                self._prepare_tracing(task=task, context=context)
                self._prepare_output_files(task=task)
                publish_startup_progress(
                    "environment", "Preparing the device environment", session_id=str(sess_id)
                )
                await self._prepare_app_installation(task=task)
                await self._prepare_device_environment(context=context)
                await self._prepare_app_lock(task=task, context=context)
                async with context:
                    recording_started = False
                    if self._config.video_recording_tools_enabled:
                        try:
                            controller = get_controller(context)

                            output_dir = None
                            if context.execution_setup and context.execution_setup.traces_path:
                                output_dir = (
                                    self._tmp_traces_dir / context.execution_setup.trace_name
                                ).resolve()

                            logger.info(f"[{task_name}] Starting automated screen recording...")
                            start_res = await controller.start_video_recording(
                                output_dir=output_dir
                            )
                            if start_res and start_res.success:
                                recording_started = True
                            else:
                                logger.warning(
                                    f"[{task_name}] Failed to start screen"
                                    f" recording: {start_res.message if start_res else 'unknown'}"
                                )
                        except Exception as e:
                            logger.error(f"[{task_name}] Failed to start screen recording: {e}")

                    publish_startup_progress(
                        "environment_ready", "Device environment is ready", session_id=str(sess_id)
                    )
                    try:
                        if request.profile and request.profile.lower() == "flash":
                            logger.info(f"[{task_name}] Invoking FlashRunner reactive loop...")
                            await task.set_status(
                                status="running",
                                message="Invoking FlashRunner...",
                            )

                            # An explicit max_steps caps the reactive loop; the
                            # default leaves the cap to agent.flash.max_turns
                            # (0 = unlimited).
                            max_turns = (
                                request.max_steps if request.max_steps != RECURSION_LIMIT else None
                            )
                            runner = FlashRunner(context, goal=request.goal, max_turns=max_turns)
                            flash_result = await runner.run(state)

                            output = flash_result
                            last_state_snapshot = state.model_dump()

                            status = flash_result.get("status")
                            if status == "completed":
                                logger.info(f"✅ Automation '{task_name}' is success ✅")
                                await task.finalize(content=output, state=last_state_snapshot)
                                if context.data_engine:
                                    context.data_engine.end_session("completed")
                            else:
                                err = (
                                    f"[{task_name}] FlashRunner failed:"
                                    f" {flash_result.get('explanation')}"
                                )
                                logger.warning(err)
                                await task.finalize(
                                    content=output,
                                    state=last_state_snapshot,
                                    error=err,
                                )
                                if context.data_engine:
                                    context.data_engine.end_session("failed")
                            return output
                        else:
                            logger.info(f"[{task_name}] Invoking graph with input: {graph_input}")
                            await task.set_status(status="running", message="Invoking graph...")
                            async for chunk in (await get_graph(context)).astream(
                                input=graph_input,
                                config={
                                    "recursion_limit": task.request.max_steps,
                                    "callbacks": (
                                        (
                                            list(self._config.graph_config_callbacks)
                                            if self._config.graph_config_callbacks is not None
                                            else []
                                        )
                                        + [DataEngineCallbackHandler(context)]
                                        if context.data_engine
                                        else self._config.graph_config_callbacks
                                    ),
                                },
                                stream_mode=[
                                    "messages",
                                    "custom",
                                    "updates",
                                    "values",
                                ],
                            ):
                                stream_mode, payload = chunk
                                if stream_mode == "values":
                                    last_state_snapshot = payload  # type: ignore
                                    last_state = State(**last_state_snapshot)  # type: ignore

                            if not last_state:
                                err = f"[{task_name}] No result received from graph"
                                logger.warning(err)
                                await task.finalize(
                                    content=output,
                                    state=last_state_snapshot,
                                    error=err,
                                )
                                if context.data_engine:
                                    context.data_engine.end_session("failed")
                                return None

                            output = await self._extract_output(
                                task_name=task_name,
                                ctx=context,
                                request=request,
                                output_config=output_config,
                                state=last_state,
                            )
                            # Run outcome (goal axis x test axis) drives the
                            # wrap-up instead of an unconditional success path.
                            run_outcome = getattr(last_state, "run_outcome", None)
                            context.run_outcome = run_outcome
                            output = attach_test_summary(output, run_outcome)
                            task_status_axis = (
                                run_outcome.get("task_status") if run_outcome else None
                            )
                            tests_failed = (
                                int((run_outcome.get("tests") or {}).get("failed", 0))
                                if run_outcome
                                else 0
                            )
                            if task_status_axis == "blocked":
                                err = (
                                    f"[{task_name}] Task ended blocked: verify"
                                    " criteria unmet after exhausting the final"
                                    " check budget."
                                )
                                logger.warning(err)
                                await task.finalize(
                                    content=output,
                                    state=last_state_snapshot,
                                    error=err,
                                )
                                if context.data_engine:
                                    context.data_engine.end_session("failed")
                                return output

                            if tests_failed > 0:
                                logger.warning(
                                    f"[{task_name}] Task completed, but"
                                    f" {tests_failed} assertion(s) failed —"
                                    " see the test summary."
                                )
                            else:
                                logger.info(f"✅ Automation '{task_name}' is success ✅")
                            await task.finalize(content=output, state=last_state_snapshot)
                            if context.data_engine:
                                context.data_engine.end_session("completed")

                            return output
                    finally:
                        if recording_started:

                            async def _safe_stop_recording():
                                try:
                                    logger.info(
                                        f"[{task_name}] Stopping automated screen recording..."
                                    )
                                    controller = get_controller(context)
                                    return await controller.stop_video_recording()
                                except Exception as e:
                                    logger.error(
                                        f"[{task_name}] Error stopping screen recording: {e}"
                                    )
                                    return None

                            stop_task = asyncio.create_task(_safe_stop_recording())
                            try:
                                stop_result = await asyncio.shield(stop_task)
                            except (asyncio.CancelledError, BaseException):
                                try:
                                    stop_result = await asyncio.wait_for(stop_task, timeout=15.0)
                                except Exception:
                                    stop_result = None

                            if stop_result and stop_result.success and stop_result.video_path:
                                logger.info(
                                    f"[{task_name}] Screen recording saved"
                                    f" to: {stop_result.video_path}"
                                )
                            elif stop_result:
                                logger.warning(
                                    f"[{task_name}] Failed to save screen"
                                    f" recording: {stop_result.message}"
                                )
            except asyncio.CancelledError:
                err = f"[{task_name}] Task cancelled"
                logger.warning(err)
                await task.finalize(
                    content=output,
                    state=last_state_snapshot,
                    error=err,
                    cancelled=True,
                )
                if context.data_engine:
                    context.data_engine.end_session("cancelled")

                raise
            except Exception as e:
                err = f"[{task_name}] Error running automation: {e}"
                logger.error(err)
                await task.finalize(
                    content=output,
                    state=last_state_snapshot,
                    error=err,
                )
                if context.data_engine:
                    context.data_engine.end_session("failed")

                raise
            finally:
                # The native simulator transport is task-owned even when setup
                # fails before entering the context manager.
                if context.device.mobile_platform == DevicePlatform.IOS:
                    await context.disconnect_driver()
                try:
                    await self._finalize_tracing_safely(task=task, context=context)
                finally:
                    if device_lock is not None:
                        await asyncio.to_thread(device_lock.release)

        async with self._task_lock:
            if self._current_task and not self._current_task.done():
                logger.warning(
                    "Another automation task is already running. "
                    "Stopping it before starting the new one."
                )
                self.stop_current_task()
                try:
                    await self._current_task
                except asyncio.CancelledError:
                    pass

            cancel_watcher: asyncio.Task | None = None
            try:
                self._current_task = asyncio.create_task(_execute_task_logic())
                cancel_watcher = asyncio.create_task(self._watch_external_cancel(task_name))
                return await self._current_task
            finally:
                self._current_task = None
                if cancel_watcher is not None and not cancel_watcher.done():
                    cancel_watcher.cancel()
                    try:
                        await cancel_watcher
                    except asyncio.CancelledError:
                        pass

    def stop_current_task(self):
        """Requests cancellation of the currently running automation task."""
        if self._current_task and not self._current_task.done():
            logger.info("Requesting to stop the current automation task...")
            was_cancelled = self._current_task.cancel()
            if was_cancelled:
                logger.success("Cancellation request for the current task was sent.")
            else:
                logger.warning(
                    "Could not send cancellation request for the current task "
                    "(it may already be completing)."
                )
        else:
            logger.info("No active automation task to stop.")

    async def get_screenshot(self) -> Image.Image:
        """Capture a screenshot from the mobile device (implemented by lunatap.sdk.Agent)."""
        if not self._initialized:
            raise AgentNotInitializedError()
        raise AgentError("Lunatap supports iOS only; use lunatap.sdk.Agent.")

    async def _prepare_app_installation(self, task: Task) -> str | None:
        """Install app if app_path is specified in the task request.

        Returns:
            None.
        """
        if not task.request.app_path:
            return None

        task_name = task.get_name()
        logger.info(f"[{task_name}] Installing app from: {task.request.app_path}")

        await self.install_app(task.request.app_path)

        return None

    async def _prepare_app_lock(self, task: Task, context: ArtemisContext):
        """App locking needs foreground-app verification, which iOS does not expose."""
        if task.request.locked_app_package:
            raise AgentError(
                "iOS app locking is unavailable because foreground ownership cannot be verified."
            )

    def _prepare_trace_paths(self, task: Task) -> str:
        """Create the temp (and, when recording, output) trace directories; return the task name."""
        task_name = task.get_name()
        temp_trace_path = Path(self._tmp_traces_dir / task_name).resolve()
        temp_trace_path.mkdir(parents=True, exist_ok=True)

        if task.request.record_trace:
            traces_output_path = Path(task.request.trace_path).resolve()
            logger.info(f"[{task_name}] 📂 Traces output path: {traces_output_path}")
            logger.info(f"[{task_name}] 📄📂 Traces temp path: {temp_trace_path}")
            traces_output_path.mkdir(parents=True, exist_ok=True)
        return task_name

    async def _finalize_tracing(self, task: Task, context: ArtemisContext):
        if context.data_engine:
            session_status = "completed"
            if task.status == "failed":
                session_status = "failed"
            elif task.status == "cancelled":
                session_status = "cancelled"
            context.data_engine.end_session(status=session_status)

        exec_setup_ctx = context.execution_setup
        if not exec_setup_ctx:
            return

        if exec_setup_ctx.traces_path is None or exec_setup_ctx.trace_name is None:
            return

        task_name = task.get_name()
        temp_trace_path = (self._tmp_traces_dir / exec_setup_ctx.trace_name).resolve()

        if not task.request.record_trace:
            try:
                if temp_trace_path.exists():
                    shutil.rmtree(temp_trace_path)
                    logger.info(f"[{task_name}] Cleaned up temporary trace folder.")
            except Exception as e:
                logger.error(f"[{task_name}] Failed to clean up temp trace folder: {e}")
            return

        status = resolve_trace_suffix(task.status, getattr(context, "run_outcome", None))
        ts = task.created_at.strftime("%Y-%m-%dT%H-%M-%S")
        new_name = f"{exec_setup_ctx.trace_name}{status}_{ts}"

        traces_output_path = Path(task.request.trace_path).resolve()

        logger.info(f"[{task_name}] Compiling trace FROM FOLDER: " + str(temp_trace_path))
        create_gif_from_trace_folder(temp_trace_path)
        create_steps_json_from_trace_folder(temp_trace_path)

        logger.info(f"[{task_name}] Video created, removing dust...")
        remove_images_from_trace_folder(temp_trace_path)
        remove_steps_json_from_trace_folder(temp_trace_path)
        logger.info(f"[{task_name}] 📽️ Trace compiled, moving to output path 📽️")

        output_folder_path = temp_trace_path.rename(traces_output_path / new_name).resolve()
        logger.info(f"[{task_name}] 📂✅ Traces located in: {output_folder_path}")

        new_video_path = output_folder_path / "recording.mp4"
        if new_video_path.exists() and context.data_engine:
            context.data_engine.update_video_path(new_video_path)

    def _prepare_output_files(self, task: Task):
        if task.request.llm_output_path:
            _validate_and_prepare_file(file_path=task.request.llm_output_path)

    async def _extract_output(
        self,
        task_name: str,
        ctx: ArtemisContext,
        request: TaskRequest[TOutput],
        output_config: OutputConfig | None,
        state: State,
    ) -> str | dict | TOutput | None:
        exec_setup = getattr(ctx, "execution_setup", None)
        outputter_cfg = getattr(exec_setup, "outputter", None) or (
            getattr(ctx.agent_config, "outputter", None) if ctx and ctx.agent_config else None
        )
        outputter_enabled = getattr(outputter_cfg, "enabled", True) if outputter_cfg else True
        force_synthesis = (
            getattr(outputter_cfg, "force_synthesis", False) if outputter_cfg else False
        )
        if exec_setup and getattr(exec_setup, "disable_outputter", False):
            outputter_enabled = False

        should_run = outputter_enabled and (
            (
                output_config
                and output_config.needs_structured_format(default_enabled=outputter_enabled)
            )
            or force_synthesis
            or (output_config and output_config.enable_outputter is True)
        )
        if output_config and output_config.enable_outputter is False:
            should_run = False

        if should_run:
            logger.info(f"[{task_name}] Generating structured output via Outputter...")
            try:
                structured_output = await outputter(
                    ctx=ctx,
                    output_config=output_config or OutputConfig(),
                    graph_output=state,
                )
                logger.info(f"[{task_name}] Structured output: {structured_output}")
                record_events(
                    output_path=request.llm_output_path,
                    events=structured_output,
                )
                if request.output_format is not None and request.output_format is not NoneType:
                    return request.output_format.model_validate(structured_output)
                return structured_output
            except Exception as e:
                logger.error(f"[{task_name}] Failed to generate structured output: {e}")
                return None
        return None


def _validate_and_prepare_file(file_path: Path):
    path_obj = Path(file_path)
    if path_obj.exists() and path_obj.is_dir():
        raise AgentTaskRequestError(f"Error: Path '{file_path}' is a directory, not a file.")
    try:
        path_obj.parent.mkdir(parents=True, exist_ok=True)
        path_obj.touch(exist_ok=True)
    except OSError as e:
        raise AgentTaskRequestError(f"Error creating file '{file_path}': {e}")

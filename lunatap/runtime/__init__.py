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

"""Lunatap runtime: device locks, the iOS device pool, daemon and server lifecycle."""

from lunatap.runtime.device_lock import (
    ConcurrencyMode,
    DeviceBusyError,
    DeviceExecutionLock,
)
from lunatap.runtime.device_target import IOS_LOCK_SCOPE, LOCK_SCOPE_ENV, IosTarget
from lunatap.runtime.ios_device_pool import DeviceStatus, IosDevicePool, ios_device_pool
from lunatap.runtime.process_probe import pid_is_alive
from lunatap.runtime.daemon_client import (
    ensure_daemon_running,
    get_daemon_session,
    get_daemon_status,
    is_artemis_daemon,
    is_daemon_running,
    stop_task_on_daemon,
    submit_batch_to_daemon,
    submit_task_to_daemon,
    wait_for_daemon_task,
)
from lunatap.runtime.server_lifecycle import (
    clear_server_info,
    find_server_pids,
    get_server_status,
    is_port_in_use,
    read_server_info,
    stop_server,
    write_server_info,
)
from lunatap.runtime.supervisor import ProcessSupervisor, process_supervisor
from lunatap.runtime.cancel_requests import (
    clear_cancel_request,
    is_cancel_requested,
    request_cancel,
    watch_for_cancel_request,
)

__all__ = [
    "IOS_LOCK_SCOPE",
    "LOCK_SCOPE_ENV",
    "ConcurrencyMode",
    "DeviceBusyError",
    "DeviceExecutionLock",
    "DeviceStatus",
    "IosDevicePool",
    "IosTarget",
    "clear_cancel_request",
    "clear_server_info",
    "ensure_daemon_running",
    "find_server_pids",
    "get_daemon_session",
    "get_daemon_status",
    "get_server_status",
    "is_artemis_daemon",
    "is_daemon_running",
    "ios_device_pool",
    "is_cancel_requested",
    "is_port_in_use",
    "pid_is_alive",
    "ProcessSupervisor",
    "process_supervisor",
    "read_server_info",
    "request_cancel",
    "stop_server",
    "stop_task_on_daemon",
    "submit_batch_to_daemon",
    "submit_task_to_daemon",
    "wait_for_daemon_task",
    "watch_for_cancel_request",
    "write_server_info",
]

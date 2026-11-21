/**
 * Copyright 2026 Google LLC
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

export type ProbeStatus = 'pass' | 'warn' | 'fail' | 'skipped';

export type ProbeCategory = 'device' | 'auth' | 'toolchain' | 'runtime';

export interface ProbeAction {
  action_type: 'command' | 'hint' | 'link';
  label: string;
  payload: string;
}

export interface DeviceInfo {
  /** Device UDID (simulator or paired iPhone/iPad). */
  serial: string;
  /** 'device' when booted / connected; otherwise the raw simctl or CoreDevice state. */
  state: string;
  model: string | null;
  product: string | null;
  screen_resolution?: string | null;
  is_locked?: boolean | null;
  /** True for an iOS Simulator, false for a physical iPhone/iPad. */
  is_emulator: boolean;
  /** True while a task holds this device's execution lock. */
  is_busy?: boolean;
  active_task_desc?: string | null;
  active_session_id?: string | null;
  installed_packages?: string[];
  platform?: 'ios';
}

export interface DeviceListResponse {
  devices: DeviceInfo[];
}

export interface ProbeResult {
  id: string;
  category: ProbeCategory;
  title: string;
  status: ProbeStatus;
  is_blocker: boolean;
  summary: string;
  description: string;
  metadata: Record<string, any>;
  actions: ProbeAction[];
}

export interface SystemReadinessReport {
  overall_ready: boolean;
  blocker_count: number;
  passed_blocker_count: number;
  probes: ProbeResult[];
  active_device: DeviceInfo | null;
  os_type?: 'linux' | 'darwin' | 'windows' | string;
  timestamp: number;
}

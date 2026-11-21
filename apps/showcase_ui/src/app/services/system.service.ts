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

import { Injectable, signal, computed, inject, DestroyRef, NgZone } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Observable, finalize, shareReplay, tap } from 'rxjs';
import {
  SystemReadinessReport,
  DeviceInfo,
  DeviceListResponse
} from '../core/models/system.model';

@Injectable({
  providedIn: 'root'
})
export class SystemService {
  private http = inject(HttpClient);

  // Core reactive signals
  public readinessReport = signal<SystemReadinessReport | null>(null);
  public hasReadinessReport = computed(() => this.readinessReport() !== null);
  public isLoading = signal<boolean>(false);
  public isRefreshingDevices = signal<boolean>(false);
  public lastCheckedTime = signal<Date | null>(null);

  // Probe lookups
  public pythonProbe = computed(() => this.probes().find(p => p.id === 'python_runtime') || null);
  public configProbe = computed(() => this.probes().find(p => p.id === 'system_config') || null);
  public toolchainProbe = computed(() => this.probes().find(p => p.id === 'toolchain') || null);
  public iosProbe = computed(() => this.probes().find(p => p.id === 'ios_simulators') || null);
  public llmProbe = computed(() => this.probes().find(p => p.id === 'gemini_api_key' || p.id === 'llm_api_key') || null);
  public geminiProbe = computed(() => this.llmProbe());
  public ocrProbe = computed(() => this.probes().find(p => p.id === 'vision_ocr_key' || p.id === 'ocr_api_key') || null);

  /** Xcode can drive iOS devices: the right Xcode plus a usable simulator backend. */
  public isIosToolingReady = computed(() => {
    const meta = this.iosProbe()?.metadata;
    return (meta?.['ios_automation_ready'] ?? meta?.['xcode_27_or_newer']) === true;
  });

  // Grouped readiness helpers for the 3-step onboarding flow
  public isEnvironmentReady = computed(() => {
    const pyOk = this.pythonProbe()?.status === 'pass';
    const cfgOk = this.configProbe()?.status === 'pass';
    // iOS automation needs macOS with Xcode (27+ natively, 15+ via WebDriverAgent).
    return pyOk && cfgOk && this.osType() === 'darwin' && this.isIosToolingReady();
  });

  public isSkipCredentialsCheck = signal<boolean>(false);

  public isCredentialsReady = computed(() => {
    if (this.isSkipCredentialsCheck()) {
      return true;
    }
    const llm = this.llmProbe();
    return llm?.status === 'pass';
  });

  /** A selected simulator or paired iPhone/iPad satisfies the device step. */
  public isDeviceReady = computed(() => this.selectedIosDevice() != null);

  // Step-level counting matching the 3-step onboarding guide
  public totalStepCount = computed(() => 3);
  public passedStepCount = computed(() => {
    let count = 0;
    if (this.isEnvironmentReady()) count++;
    if (this.isCredentialsReady()) count++;
    if (this.isDeviceReady()) count++;
    return count;
  });

  // Computed helper signals
  public isReady = computed(() => {
    return this.isEnvironmentReady() && this.isCredentialsReady() && this.isDeviceReady();
  });
  public blockerCount = computed(() => this.totalStepCount());
  public passedBlockerCount = computed(() => this.passedStepCount());
  public probes = computed(() => this.readinessReport()?.probes ?? []);
  public osType = computed<'linux' | 'darwin' | 'windows'>(() => {
    const raw = this.readinessReport()?.os_type;
    if (raw === 'windows' || raw === 'win32') return 'windows';
    if (raw === 'darwin' || raw === 'macos' || raw === 'mac') return 'darwin';
    return 'linux';
  });

  public skipCredentialsCheck(): void {
    this.isSkipCredentialsCheck.set(true);
  }

  public setSkipCredentialsCheck(skip: boolean): void {
    this.isSkipCredentialsCheck.set(skip);
  }

  /** Device list from `GET /api/devices` (null until first fetched). */
  public apiDevices = signal<DeviceInfo[] | null>(null);

  /** iOS devices reported by the readiness probe (simulators + paired devices). */
  public probeDevices = computed<DeviceInfo[]>(() => {
    const devices: DeviceInfo[] = [];
    const iosMeta = this.iosProbe()?.metadata;
    if (iosMeta && Array.isArray(iosMeta['simulators'])) {
      for (const s of iosMeta['simulators'] as any[]) {
        if (!s?.udid) continue;
        devices.push({
          serial: s.udid,
          state: s.state === 'Booted' ? 'device' : (s.state || 'unknown'),
          model: s.name ?? null,
          product: String(s.runtime ?? '')
            .replace('com.apple.CoreSimulator.SimRuntime.', '')
            .replace(/^([A-Za-z]+)-(.+)$/, (_m: string, name: string, ver: string) => `${name} ${ver.replace(/-/g, '.')}`) || null,
          is_emulator: true,
          platform: 'ios',
        });
      }
    }
    if (iosMeta && Array.isArray(iosMeta['connected_physical_devices'])) {
      for (const p of iosMeta['connected_physical_devices'] as any[]) {
        if (!p?.udid) continue;
        devices.push({
          serial: p.udid,
          state: 'device',
          model: p.name ?? null,
          product: p.os_version ? `iOS ${p.os_version}` : 'iOS physical device',
          is_emulator: false,
          platform: 'ios',
        });
      }
    }
    return devices;
  });

  /**
   * Every selectable iOS device. Prefers the `/api/devices` listing (which
   * carries busy/lock state) and falls back to the readiness probe metadata.
   */
  public connectedDevices = computed<DeviceInfo[]>(() => {
    const fromApi = this.apiDevices();
    const probed = this.probeDevices();
    if (fromApi === null) {
      return probed;
    }
    const seen = new Set(fromApi.map(d => d.serial));
    return [
      ...fromApi.map(d => ({ ...d, platform: 'ios' as const })),
      ...probed.filter(d => !seen.has(d.serial)),
    ];
  });

  public iosDevices = computed<DeviceInfo[]>(() => this.connectedDevices());

  // Selection lives client-side: the run payload carries the chosen UDID.
  public selectedIosDevice = signal<DeviceInfo | null>(null);
  public selectedDeviceSerial = computed<string | null>(
    () => this.selectedIosDevice()?.serial ?? null
  );

  private pollingTimer: any = null;
  private readinessRequest$: Observable<SystemReadinessReport> | null = null;
  private zone = inject(NgZone);
  private lastAppliedReportJson: string | null = null;
  private onVisibilityChange = () => {
    if (typeof document !== 'undefined' && !document.hidden) {
      this.fetchReadiness(true).subscribe({ error: () => {} });
    }
  };

  constructor() {
    this.startAutoPolling(3000);
    if (typeof document !== 'undefined') {
      document.addEventListener('visibilitychange', this.onVisibilityChange);
    }
    inject(DestroyRef).onDestroy(() => {
      this.stopAutoPolling();
      if (typeof document !== 'undefined') {
        document.removeEventListener('visibilitychange', this.onVisibilityChange);
      }
    });
  }

  /**
   * Start periodic auto-polling for system readiness changes. The interval
   * runs outside the Angular zone (a tick alone must not trigger change
   * detection) and pauses while the tab is hidden; a visibilitychange listener
   * refreshes immediately when the tab becomes visible again.
   */
  public startAutoPolling(intervalMs: number = 3000): void {
    if (this.pollingTimer) {
      clearInterval(this.pollingTimer);
    }
    this.zone.runOutsideAngular(() => {
      this.pollingTimer = setInterval(() => {
        if (typeof document !== 'undefined' && document.hidden) {
          return;
        }
        // Perform silent background check without disturbing UI loading state
        this.fetchReadiness(true).subscribe({
          error: () => {} // Silent catch
        });
      }, intervalMs);
    });
  }

  /**
   * Stop auto-polling
   */
  public stopAutoPolling(): void {
    if (this.pollingTimer) {
      clearInterval(this.pollingTimer);
      this.pollingTimer = null;
    }
  }

  /**
   * Fetch latest system readiness report from backend
   * @param silent If true, updates signals silently without triggering global isLoading spinner
   */
  public fetchReadiness(
    silent: boolean = false,
    forceRefresh: boolean = false
  ): Observable<SystemReadinessReport> {
    if (!silent) {
      this.isLoading.set(true);
    }

    // Share one request across initialization, focus events, polling, and manual
    // refreshes. This prevents a slow probe from creating an unbounded queue.
    if (this.readinessRequest$) {
      return this.readinessRequest$;
    }

    const request$ = this.http.get<SystemReadinessReport>('/api/system/readiness', {
      params: forceRefresh ? { force: true } : {}
    }).pipe(
      tap({
        next: (report) => {
          this.applyReadinessReport(report);
        },
        error: (err) => {
          console.error('Failed to fetch system readiness:', err);
        }
      }),
      finalize(() => {
        if (this.readinessRequest$ === request$) {
          this.readinessRequest$ = null;
        }
        this.isLoading.set(false);
      }),
      shareReplay({ bufferSize: 1, refCount: false })
    );
    this.readinessRequest$ = request$;
    return request$;
  }

  private applyReadinessReport(report: SystemReadinessReport): void {
    const current = this.readinessReport();
    if (current && report.timestamp < current.timestamp) {
      return;
    }
    this.lastCheckedTime.set(new Date(report.timestamp * 1000));
    // A poll usually returns an identical report; setting a new object anyway
    // would cascade through every probe computed for no visible change.
    const serialized = JSON.stringify({ ...report, timestamp: 0 });
    if (serialized === this.lastAppliedReportJson) {
      return;
    }
    this.lastAppliedReportJson = serialized;
    this.readinessReport.set(report);
  }

  /**
   * Fetch the iOS simulators and paired iPhones/iPads (`GET /api/devices`).
   */
  public fetchDevices(): Observable<DeviceListResponse> {
    this.isRefreshingDevices.set(true);
    return this.http.get<DeviceListResponse>('/api/devices').pipe(
      tap({
        next: (res) => {
          const devices = Array.isArray(res?.devices) ? res.devices : [];
          this.apiDevices.set(devices.filter(d => !d.platform || d.platform === 'ios'));
          // Keep the selected device's details (boot state, busy flag) current.
          const selected = this.selectedIosDevice();
          if (selected) {
            const fresh = devices.find(d => d.serial === selected.serial);
            if (fresh) {
              this.selectedIosDevice.set({ ...fresh, platform: 'ios' });
            }
          }
        },
        error: (err) => {
          console.error('Failed to fetch iOS devices:', err);
        }
      }),
      finalize(() => this.isRefreshingDevices.set(false))
    );
  }

  /**
   * Select an iOS simulator or paired iPhone/iPad (by UDID) as the run target.
   */
  public selectDevice(serial: string): Observable<any> {
    // Selection is client-side state (the run payload carries the binding);
    // apply it optimistically since UDID validation can take seconds and the
    // error path below reverts it if the server rejects the pick.
    const findDevice = () =>
      this.connectedDevices().find(d => d.serial === serial) ?? null;
    this.selectedIosDevice.set(findDevice());
    this.isLoading.set(true);
    return this.http.post<any>('/api/system/devices/select', { serial, platform: 'ios' }).pipe(
      tap({
        next: (res) => {
          this.selectedIosDevice.set(
            res?.status === 'success' ? findDevice() ?? this.selectedIosDevice() : null
          );
          this.isLoading.set(false);
        },
        error: (err) => {
          console.error('Failed to select iOS device:', err);
          this.selectedIosDevice.set(null);
          this.isLoading.set(false);
        }
      })
    );
  }

  public currentApiKey = computed<string>(() => {
    return (this.llmProbe()?.metadata?.['current_key'] as string) || '';
  });

  public apiKeysMap = computed<Record<string, string>>(() => {
    return (this.llmProbe()?.metadata?.['api_keys'] as Record<string, string>) || {};
  });

  public modelConfigEnv = signal<ModelConfigEnvResponse | null>(null);

  /**
   * Fetch current model configuration (artemis.jsonc) and environment (.env) status
   */
  public fetchModelConfigEnv(): Observable<ModelConfigEnvResponse> {
    return this.http.get<ModelConfigEnvResponse>('/api/system/model-config-env').pipe(
      tap({
        next: (data) => {
          this.modelConfigEnv.set(data);
        },
        error: (err) => {
          console.error('Failed to fetch model config and env:', err);
        }
      })
    );
  }

  /**
   * Test and verify an API key against live provider endpoint without persisting
   */
  public testApiKey(provider: string, apiKey: string, baseUrl?: string): Observable<{ valid: boolean; provider: string; message: string }> {
    return this.http.post<{ valid: boolean; provider: string; message: string }>('/api/system/credentials/test', {
      provider,
      api_key: apiKey,
      base_url: baseUrl
    });
  }

  /** OpenAI(-compatible) endpoint settings in effect (no secrets). */
  public fetchLlmEndpoint(): Observable<LlmEndpointState> {
    return this.http.get<LlmEndpointState>('/api/system/llm-endpoint');
  }

  /**
   * Verify a key against OpenAI or an OpenAI-compatible base URL and use it for
   * every agent. A base URL selects the openai-compatible preset and needs a model.
   */
  public updateLlmEndpoint(body: { api_key: string; base_url?: string | null; model?: string | null }): Observable<any> {
    return this.http.post<any>('/api/system/llm-endpoint', body).pipe(
      tap({
        next: (res) => {
          if (res?.report) {
            this.applyReadinessReport(res.report);
          }
          this.fetchModelConfigEnv().subscribe();
        },
      })
    );
  }

  /**
   * Update, verify, and configure API key for an LLM provider or Vision OCR
   */
  public updateApiKey(provider: string, apiKey: string, persistToEnv: boolean = true): Observable<any> {
    return this.http.post<any>('/api/system/credentials', {
      provider,
      api_key: apiKey,
      persist_to_env: persistToEnv
    }).pipe(
      tap({
        next: (res) => {
          if (res?.report) {
            this.applyReadinessReport(res.report);
          }
          // Refresh model config & env after updating key
          this.fetchModelConfigEnv().subscribe();
        },
        error: (err) => {
          console.error(`Failed to update credentials for ${provider}:`, err);
        }
      })
    );
  }
}

export interface ModelConfigEnvResponse {
  config_path: string;
  config_filename: string;
  config_content: string;
  default_model: {
    provider?: string;
    model?: string;
    thinking_level?: string;
    fallback?: {
      provider?: string;
      model?: string;
      thinking_level?: string;
    };
  };
  presets: Record<string, {
    provider: string;
    model: string;
    fallback?: { provider: string; model: string };
  }>;
  env_path: string;
  env_filename: string;
  env_vars: Array<{
    name: string;
    provider: string;
    is_set: boolean;
    preview: string | null;
    description: string;
  }>;
}

export interface LlmEndpointState {
  preset: string | null;
  base_url: string | null;
  model: string | null;
  fallback_model: string | null;
}

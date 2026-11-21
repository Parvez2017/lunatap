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

import { Component, signal, computed, effect, inject, OnInit, OnDestroy, ChangeDetectionStrategy } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { Router, RouterLink } from '@angular/router';
import { AgentService } from '../../services/agent.service';
import { SystemService } from '../../services/system.service';
import { DeviceInfo } from '../../core/models/system.model';
import {
  AppReference,
  SmartSuggestion,
  SuggestionCategory
} from '../../core/data/smart-tasks.data';
import { TaskRecommendationService } from '../../core/services/task-recommendation.service';
import {
  DEFAULT_EXPLORER_MODE,
  DEFAULT_VERIFICATION_LEVEL,
  EXPLORER_MODES,
  ExplorerModeId,
  TuningLevel,
  VERIFICATION_LEVELS,
  VerificationLevelId,
  levelIndex,
  notchPercent
} from '../../core/models/pro-tuning.model';

export type TuningKind = 'verify' | 'explore';

/** One 2x2 px square of the "maxed out" dither texture drawn over a slider rail. */
export interface DitherPixel {
  /** Horizontal position as a percentage of the rail width. */
  x: number;
  /** Row offset in px (rail is 6 px tall, three 2 px rows). */
  y: number;
  /** Resting opacity; squares near the thumb are stronger. */
  opacity: number;
  /** Animation delay in ms so the texture spreads leftwards from the thumb. */
  delay: number;
}

/**
 * Seeded pixels keep the slider texture stable across renders.
 * Delays increase with distance from the thumb to animate from right to left.
 */
function buildDitherPixels(count = 260, seed = 7): DitherPixel[] {
  let state = seed >>> 0;
  const rand = (): number => {
    state = (Math.imul(state, 1664525) + 1013904223) >>> 0;
    return state / 0x100000000;
  };
  const pixels: DitherPixel[] = [];
  for (let i = 0; i < count; i++) {
    const x = rand() * 100;
    const y = Math.floor(rand() * 3) * 2;
    pixels.push({
      x: Math.round(x * 10) / 10,
      y,
      opacity: Math.round((0.35 + 0.4 * rand()) * 100) / 100,
      // 8 ms per percent: the front takes ~0.8 s to reach the left end.
      delay: Math.round((100 - x) * 8)
    });
  }
  return pixels;
}

/** Shared view model for the verification and screen-reading sliders. */
export interface TuningSliderVm {
  kind: TuningKind;
  name: string;
  /** One-word meaning of each end of the track, e.g. ["Off", "Strict"]. */
  ends: readonly [string, string];
  ladder: readonly TuningLevel[];
  index: number;
  level: TuningLevel;
  /** 0..1 position of the thumb along the track. */
  fraction: number;
}

export type { AppReference, SmartSuggestion, SuggestionCategory };

/** Setup guide tabs: boot a simulator, or pair a physical iPhone/iPad. */
type IosGuideTab = 'simulator' | 'physical';


type ModelSetupMode = 'openai' | 'gemini' | 'custom';

@Component({
  selector: 'app-home',
  standalone: true,
  imports: [FormsModule],
  templateUrl: './home.component.html',
  changeDetection: ChangeDetectionStrategy.Eager,
  styleUrl: './home.component.scss'
})
export class HomeComponent implements OnInit, OnDestroy {
  public agentService = inject(AgentService);
  public systemService = inject(SystemService);
  public taskRecService = inject(TaskRecommendationService);
  private router = inject(Router);

  // High-level navigation mode: 'diagnostics' (System Setup Guide) vs 'launcher' (Task Execution)
  public activeTab = signal<'diagnostics' | 'launcher'>('launcher');

  // Interactive guide sub-tab inside the iOS device section
  public activeIosGuideTab = signal<IosGuideTab>('simulator');

  // Model setup choice. OpenAI is the default; its key form also covers Gemini.
  public modelSetupMode = signal<ModelSetupMode>('openai');
  private modelSetupModeTouched = false;

  /** Provider id the shared key form saves to ('openai' or 'google'). */
  public llmKeyProvider = computed<'openai' | 'google'>(() =>
    this.modelSetupMode() === 'gemini' ? 'google' : 'openai'
  );
  // OpenAI-compatible endpoint (OpenRouter, Gemini, Claude, local servers).
  public llmBaseUrl = signal<string>('');
  public llmModel = signal<string>('');
  public showCompatibleEndpoint = signal<boolean>(false);
  private savedEndpoint = signal<{ base_url: string; model: string }>({ base_url: '', model: '' });
  public readonly compatibleEndpointExamples = [
    { label: 'OpenRouter', url: 'https://openrouter.ai/api/v1', model: 'openai/gpt-5.4-mini' },
    { label: 'Gemini', url: 'https://generativelanguage.googleapis.com/v1beta/openai/', model: 'gemini-3.8-flash' },
    { label: 'Claude', url: 'https://api.anthropic.com/v1/', model: 'claude-sonnet-5-5' },
    { label: 'LM Studio', url: 'http://localhost:1234/v1', model: '' },
  ];

  public llmProviderLabel = computed<string>(() =>
    this.llmKeyProvider() === 'google' ? 'Gemini' : 'OpenAI'
  );
  public showOcrConfig = signal<boolean>(false);
  public showFullConfigFile = signal<boolean>(false);

  // Model & Environment configuration from backend
  public modelConfigEnv = computed(() => this.systemService.modelConfigEnv());

  // LLM API key form state (OpenAI or Gemini)
  public llmKeyInput = signal<string>('');
  public showLlmKey = signal<boolean>(false);
  public isSavingLlmKey = signal<boolean>(false);
  public isTestingLlmKey = signal<boolean>(false);
  public llmSaveMessage = signal<string | null>(null);
  public llmSaveError = signal<string | null>(null);
  public isLlmKeyEdited = signal<boolean>(false);

  // Vision OCR API Key State
  public ocrKeyInput = signal<string>('');
  public showOcrKey = signal<boolean>(false);
  public isSavingOcrKey = signal<boolean>(false);
  public isTestingOcrKey = signal<boolean>(false);
  public ocrSaveMessage = signal<string | null>(null);
  public ocrSaveError = signal<string | null>(null);
  public isOcrKeyEdited = signal<boolean>(false);

  // Clipboard copy state tracker for interactive feedback
  public copiedId = signal<string | null>(null);

  // Diagnostic re-check state
  public isRefreshingDiagnostics = signal<boolean>(false);

  public showConnectionMethods = signal<boolean>(false);

  // Task execution parameters
  public selectedProfile = signal<'flash' | 'pro'>('flash');
  public taskGoal = signal<string>('');
  public isSubmitting = signal<boolean>(false);
  public errorMessage = signal<string | null>(null);

  // Pro Mode Outputter & Structured Output Configuration
  public expectedOutput = signal<string>('');
  public enableOutputter = signal<boolean>(true);
  public showOutputterDrawer = signal<boolean>(false);

  // Slider indexes map to the API ids in VERIFICATION_LEVELS / EXPLORER_MODES.
  public readonly verificationLevels = VERIFICATION_LEVELS;
  public readonly explorerModes = EXPLORER_MODES;
  public verificationIndex = signal<number>(
    levelIndex(VERIFICATION_LEVELS, DEFAULT_VERIFICATION_LEVEL, DEFAULT_VERIFICATION_LEVEL)
  );
  public explorerIndex = signal<number>(
    levelIndex(EXPLORER_MODES, DEFAULT_EXPLORER_MODE, DEFAULT_EXPLORER_MODE)
  );
  /** Effective defaults from the backend config (`GET /api/run/defaults`). */
  private tuningDefaults = signal<{ verification: VerificationLevelId; explorer: ExplorerModeId }>({
    verification: DEFAULT_VERIFICATION_LEVEL,
    explorer: DEFAULT_EXPLORER_MODE
  });
  /** True once the user moved a slider; backend defaults then stop overriding it. */
  private tuningTouched = signal<boolean>(false);
  /** Which slider's hover card is open (while hovering, dragging, or focused). */
  public activeTuningTip = signal<TuningKind | null>(null);
  /** Pixel cloud drawn over a rail once its slider reaches the last notch. */
  public readonly ditherPixels: readonly DitherPixel[] = buildDitherPixels();

  public verificationLevel = computed<TuningLevel<VerificationLevelId>>(
    () => VERIFICATION_LEVELS[this.verificationIndex()]
  );
  public explorerMode = computed<TuningLevel<ExplorerModeId>>(
    () => EXPLORER_MODES[this.explorerIndex()]
  );
  public isTuningDefault = computed<boolean>(() => {
    const d = this.tuningDefaults();
    return this.verificationLevel().id === d.verification && this.explorerMode().id === d.explorer;
  });
  public tuningSliders = computed<TuningSliderVm[]>(() => {
    const vi = this.verificationIndex();
    const ei = this.explorerIndex();
    return [
      {
        kind: 'verify',
        name: 'Result check',
        ends: ['Off', 'Strict'],
        ladder: VERIFICATION_LEVELS,
        index: vi,
        level: VERIFICATION_LEVELS[vi],
        fraction: notchPercent(vi, VERIFICATION_LEVELS.length) / 100
      },
      {
        kind: 'explore',
        name: 'Screen reading',
        ends: ['Faster', 'Sharper'],
        ladder: EXPLORER_MODES,
        index: ei,
        level: EXPLORER_MODES[ei],
        fraction: notchPercent(ei, EXPLORER_MODES.length) / 100
      }
    ];
  });

  public notchFraction(index: number, count: number): number {
    return notchPercent(index, count) / 100;
  }

  public setTuningIndex(kind: TuningKind, raw: number | string): void {
    const idx = Math.round(Number(raw));
    if (!Number.isFinite(idx)) return;
    const ladder = kind === 'verify' ? VERIFICATION_LEVELS : EXPLORER_MODES;
    const clamped = Math.min(Math.max(idx, 0), ladder.length - 1);
    this.tuningTouched.set(true);
    if (kind === 'verify') {
      this.verificationIndex.set(clamped);
    } else {
      this.explorerIndex.set(clamped);
    }
    // Keyboard nudges and drags should keep the explanation visible.
    this.activeTuningTip.set(kind);
  }

  public showTuningTip(kind: TuningKind): void {
    this.activeTuningTip.set(kind);
  }

  public hideTuningTip(kind: TuningKind): void {
    if (this.activeTuningTip() === kind) {
      this.activeTuningTip.set(null);
    }
  }

  public resetTuning(): void {
    const d = this.tuningDefaults();
    this.verificationIndex.set(levelIndex(VERIFICATION_LEVELS, d.verification, DEFAULT_VERIFICATION_LEVEL));
    this.explorerIndex.set(levelIndex(EXPLORER_MODES, d.explorer, DEFAULT_EXPLORER_MODE));
    this.tuningTouched.set(false);
  }

  /** Pull the effective config defaults so the sliders start where artemis.jsonc is. */
  private loadProTuningDefaults(): void {
    this.agentService.getProTuningDefaults().subscribe({
      next: (res) => {
        const vIdx = levelIndex(VERIFICATION_LEVELS, res?.verification_level, DEFAULT_VERIFICATION_LEVEL);
        const eIdx = levelIndex(EXPLORER_MODES, res?.explorer_mode, DEFAULT_EXPLORER_MODE);
        this.tuningDefaults.set({
          verification: VERIFICATION_LEVELS[vIdx].id,
          explorer: EXPLORER_MODES[eIdx].id
        });
        if (!this.tuningTouched()) {
          this.verificationIndex.set(vIdx);
          this.explorerIndex.set(eIdx);
        }
      },
      // Defaults are a convenience; the built-in ladder defaults already apply.
      error: () => undefined
    });
  }

  public toggleOutputterDrawer(): void {
    this.showOutputterDrawer.update((v) => !v);
  }

  public applyOutputPreset(preset: string): void {
    if (this.expectedOutput() === preset) {
      this.expectedOutput.set('');
    } else {
      this.expectedOutput.set(preset);
      this.showOutputterDrawer.set(true);
    }
  }

  // Smart intent detection for model recommendation
  public isIntentSuggestingPro = computed<boolean>(() => {
    const text = this.taskGoal().toLowerCase();
    if (!text.trim()) return false;
    const keywords = [
      'monitor', 'polling', 'poll', 'wait until', 'loop', 'keep watching',
      'crash', 'troubleshoot', 'diagnose', 'debug', 'investigate',
      'compare', 'extract', 'summarize', 'report',
      '监控', '轮询', '等待', '一直', '直到', '崩溃', '闪退', '排查', '分析日志', '对比', '总结'
    ];
    return keywords.some(k => text.includes(k));
  });

  public showIntentSuggestion = computed<boolean>(() => {
    return this.isIntentSuggestingPro() && this.selectedProfile() === 'flash';
  });

  // Computed helper states delegating to SystemService
  public isReady = computed(() => this.systemService.isReady());
  public hasReadinessReport = computed(() => this.systemService.hasReadinessReport());
  public isLoading = computed(() => this.systemService.isLoading());
  public isRefreshingDevices = computed(() => this.systemService.isRefreshingDevices());

  // Probes
  public pythonProbe = computed(() => this.systemService.pythonProbe());
  public configProbe = computed(() => this.systemService.configProbe());
  public llmProbe = computed(() => this.systemService.llmProbe());
  public geminiProbe = computed(() => this.systemService.geminiProbe());
  public ocrProbe = computed(() => this.systemService.ocrProbe());
  public toolchainProbe = computed(() => this.systemService.toolchainProbe());

  // Step-level readiness
  public isEnvironmentReady = computed(() => this.systemService.isEnvironmentReady());
  public isCredentialsReady = computed(() => this.systemService.isCredentialsReady());
  public isSkipCredentialsCheck = computed(() => this.systemService.isSkipCredentialsCheck());
  public isDeviceReady = computed(() => this.systemService.isDeviceReady());

  // Device information
  public connectedDevices = computed(() => this.systemService.connectedDevices());
  public selectedDeviceSerial = computed(() => this.systemService.selectedDeviceSerial());
  public iosDevices = computed(() => this.systemService.iosDevices());
  public selectedIosDevice = computed(() => this.systemService.selectedIosDevice());
  public iosProbe = computed(() => this.systemService.iosProbe());
  public isIosToolingReady = computed(() => this.systemService.isIosToolingReady());
  public xcodeVersion = computed<string | null>(
    () => (this.iosProbe()?.metadata?.['xcode_version'] as string) || null
  );
  public simulatorBackend = computed<string | null>(
    () => (this.iosProbe()?.metadata?.['simulator_backend'] as string) || null
  );
  public totalStepCount = computed(() => this.systemService.totalStepCount());
  public passedStepCount = computed(() => this.systemService.passedStepCount());
  public blockerCount = computed(() => this.systemService.blockerCount());
  public passedBlockerCount = computed(() => this.systemService.passedBlockerCount());

  // Configured LLM providers from probe metadata
  public configuredLlmProviders = computed<any[]>(() => {
    const meta = this.llmProbe()?.metadata;
    if (meta && Array.isArray(meta['providers'])) {
      return meta['providers'];
    }
    return [];
  });

  // iOS automation runs on macOS only, so setup commands are macOS commands.
  public readonly oneClickSetupCmd = 'bash scripts/install_deps.sh';
  public readonly toolchainInstallCmd = 'brew install ffmpeg';
  public readonly xcodeSelectCmd = 'sudo xcode-select -s /Applications/Xcode.app/Contents/Developer';
  public readonly iosEnvCheckCmd = 'bash scripts/setup_ios_env.sh';
  public readonly wdaSimulatorBuildCmd = 'bash scripts/setup_ios_wda_simulator.sh';
  public readonly listSimulatorsCmd = 'xcrun simctl list devices available';
  public readonly bootSimulatorCmd = 'xcrun simctl boot <UDID>';
  public readonly openSimulatorCmd = 'open -a Simulator';
  public readonly listPhysicalDevicesCmd = 'xcrun devicectl list devices';

  // Flag indicating whether Google Cloud Vision OCR is configured
  public isOcrConfigured = computed<boolean>(() => {
    const meta = this.ocrProbe()?.metadata;
    return meta?.['configured'] === true;
  });

  public currentApiKey = computed<string>(() => this.systemService.currentApiKey());
  public apiKeysMap = computed<Record<string, string>>(() => this.systemService.apiKeysMap());

  public savedLlmKey = computed<string>(() => {
    const keys = this.apiKeysMap();
    return keys[this.llmKeyProvider()] || '';
  });

  public isEndpointModified = computed<boolean>(() =>
    this.llmKeyProvider() === 'openai' &&
    (this.llmBaseUrl().trim() !== this.savedEndpoint().base_url ||
      this.llmModel().trim() !== this.savedEndpoint().model)
  );

  public isLlmKeyModified = computed<boolean>(() => {
    if (this.isEndpointModified()) return true;
    return this.llmKeyInput().trim() !== this.savedLlmKey().trim();
  });

  public savedOcrKey = computed<string>(() => {
    const keys = this.apiKeysMap();
    return keys['ocr'] || '';
  });

  public isOcrModified = computed<boolean>(() => {
    return this.ocrKeyInput().trim() !== this.savedOcrKey().trim();
  });

  // Rich Smart Suggestions Library (Device-Aware, Flash vs Pro Tailored)
  public readonly allSuggestions = this.taskRecService.allTasks;

  // Suggestion category filter & shuffle state
  public selectedCategory = signal<SuggestionCategory>('all');
  public shuffleOffset = signal<number>(0);

  // Set of installed app bundle ids from the selected device
  public installedPackages = computed<Set<string>>(() => {
    const pkgs = this.selectedIosDevice()?.installed_packages;
    if (pkgs && Array.isArray(pkgs)) {
      return new Set(pkgs);
    }
    return new Set();
  });

  public filteredSuggestions = computed<SmartSuggestion[]>(() => {
    return this.taskRecService.filterAndRankTasks(
      this.installedPackages(),
      this.selectedCategory(),
      this.shuffleOffset()
    );
  });




  private focusListener = () => {
    // Silently re-check environment when user returns to the browser tab
    this.systemService.fetchReadiness().subscribe();
    this.systemService.fetchDevices().subscribe({ error: () => {} });
  };

  constructor() {
    effect(() => {
      const keys = this.systemService.apiKeysMap();
      // Open on Gemini only when that is the one key configured.
      if (!this.modelSetupModeTouched && !keys['openai'] && keys['google']) {
        this.modelSetupMode.set('gemini');
      }
      const llmKey = keys[this.llmKeyProvider()] || '';
      const ocrKey = keys['ocr'] || '';
      if (!this.isLlmKeyEdited()) {
        this.llmKeyInput.set(llmKey);
      }
      if (!this.isOcrKeyEdited()) {
        this.ocrKeyInput.set(ocrKey);
      }
    });
  }

  ngOnInit(): void {

    this.loadProTuningDefaults();
    this.loadLlmEndpoint();
    // Initial fetch of system readiness & model configuration
    this.systemService.fetchReadiness().subscribe();
    this.systemService.fetchModelConfigEnv().subscribe();
    this.systemService.fetchDevices().subscribe({ error: () => {} });

    window.addEventListener('focus', this.focusListener);
  }

  ngOnDestroy(): void {
    window.removeEventListener('focus', this.focusListener);
  }

  public setTab(tab: 'diagnostics' | 'launcher'): void {
    this.activeTab.set(tab);
    if (tab === 'diagnostics') {
      this.systemService.fetchReadiness().subscribe();
      this.systemService.fetchModelConfigEnv().subscribe();
    }
  }

  public setIosGuideTab(tab: IosGuideTab): void {
    this.activeIosGuideTab.set(tab);
  }

  public setModelSetupMode(mode: ModelSetupMode): void {
    this.modelSetupModeTouched = true;
    if (mode !== this.modelSetupMode()) {
      this.isLlmKeyEdited.set(false);
      this.llmSaveError.set(null);
      this.llmSaveMessage.set(null);
    }
    this.modelSetupMode.set(mode);
    if (mode === 'custom') {
      this.systemService.setSkipCredentialsCheck(true);
      this.systemService.fetchModelConfigEnv().subscribe();
    } else {
      this.systemService.setSkipCredentialsCheck(false);
    }
  }

  public toggleFullConfigFile(): void {
    this.showFullConfigFile.update(v => !v);
  }

  public toggleLlmKeyVisibility(): void {
    this.showLlmKey.update(v => !v);
  }

  public toggleOcrKeyVisibility(): void {
    this.showOcrKey.update(v => !v);
  }

  public toggleOcrConfig(): void {
    this.showOcrConfig.update(v => !v);
  }

  public onLlmKeyChange(val: string): void {
    this.llmKeyInput.set(val);
    this.isLlmKeyEdited.set(true);
    this.llmSaveError.set(null);
    this.llmSaveMessage.set(null);
  }

  public onOcrKeyChange(val: string): void {
    this.ocrKeyInput.set(val);
    this.isOcrKeyEdited.set(true);
    this.ocrSaveError.set(null);
    this.ocrSaveMessage.set(null);
  }

  private loadLlmEndpoint(): void {
    this.systemService.fetchLlmEndpoint().subscribe({
      next: (state) => this.applyLlmEndpoint(state),
      error: () => {},
    });
  }

  private applyLlmEndpoint(state: { base_url: string | null; model: string | null }): void {
    const baseUrl = state?.base_url || '';
    const model = state?.model || '';
    this.savedEndpoint.set({ base_url: baseUrl, model });
    this.llmBaseUrl.set(baseUrl);
    this.llmModel.set(model);
    if (baseUrl) {
      this.showCompatibleEndpoint.set(true);
    }
  }

  public toggleCompatibleEndpoint(): void {
    this.showCompatibleEndpoint.update(v => !v);
  }

  public onLlmBaseUrlChange(value: string): void {
    this.llmBaseUrl.set(value);
    this.llmSaveError.set(null);
    this.llmSaveMessage.set(null);
  }

  public onLlmModelChange(value: string): void {
    this.llmModel.set(value);
    this.llmSaveError.set(null);
    this.llmSaveMessage.set(null);
  }

  public useEndpointExample(example: { url: string; model: string }): void {
    this.onLlmBaseUrlChange(example.url);
    if (example.model) {
      this.onLlmModelChange(example.model);
    }
  }

  public saveLlmKey(): void {
    const key = this.llmKeyInput().trim();
    if (!key) return;
    this.isSavingLlmKey.set(true);
    this.llmSaveError.set(null);
    this.llmSaveMessage.set(null);

    const save$ = this.llmKeyProvider() === 'openai'
      ? this.systemService.updateLlmEndpoint({
          api_key: key,
          base_url: this.llmBaseUrl().trim() || null,
          model: this.llmModel().trim() || null,
        })
      : this.systemService.updateApiKey(this.llmKeyProvider(), key, true);
    save$.subscribe({
      next: (res) => {
        this.isSavingLlmKey.set(false);
        this.isLlmKeyEdited.set(false);
        if (res?.endpoint) {
          this.applyLlmEndpoint(res.endpoint);
        }
        this.llmSaveMessage.set(res?.message || `✓ ${this.llmProviderLabel()} API key verified & saved successfully.`);
        setTimeout(() => this.llmSaveMessage.set(null), 5000);
      },
      error: (err) => {
        this.isSavingLlmKey.set(false);
        this.llmSaveError.set(err?.error?.detail || err?.message || `Failed to update ${this.llmProviderLabel()} API key.`);
      }
    });
  }

  public clearLlmKey(): void {
    this.llmKeyInput.set('');
    this.isLlmKeyEdited.set(false);
    this.llmSaveError.set(null);
    this.llmSaveMessage.set(null);

    if (this.savedLlmKey().trim()) {
      this.isSavingLlmKey.set(true);
      this.systemService.updateApiKey(this.llmKeyProvider(), '', true).subscribe({
        next: (res) => {
          this.isSavingLlmKey.set(false);
          this.llmSaveMessage.set(res?.message || `✓ ${this.llmProviderLabel()} API key cleared.`);
          setTimeout(() => this.llmSaveMessage.set(null), 5000);
        },
        error: (err) => {
          this.isSavingLlmKey.set(false);
          this.llmSaveError.set(err?.error?.detail || err?.message || `Failed to clear ${this.llmProviderLabel()} API key.`);
        }
      });
    } else {
      this.llmSaveMessage.set(`✓ ${this.llmProviderLabel()} API key cleared.`);
      setTimeout(() => this.llmSaveMessage.set(null), 3000);
    }
  }

  public saveOcrKey(): void {
    const key = this.ocrKeyInput().trim();
    if (!key) return;
    this.isSavingOcrKey.set(true);
    this.ocrSaveError.set(null);
    this.ocrSaveMessage.set(null);

    this.systemService.updateApiKey('ocr', key, true).subscribe({
      next: (res) => {
        this.isSavingOcrKey.set(false);
        this.isOcrKeyEdited.set(false);
        this.ocrSaveMessage.set(res?.message || '✓ Vision OCR API key verified & saved.');
        setTimeout(() => this.ocrSaveMessage.set(null), 5000);
      },
      error: (err) => {
        this.isSavingOcrKey.set(false);
        this.ocrSaveError.set(err?.error?.detail || err?.message || 'Failed to update Vision OCR key.');
      }
    });
  }

  public clearOcrKey(): void {
    this.ocrKeyInput.set('');
    this.isOcrKeyEdited.set(false);
    this.ocrSaveError.set(null);
    this.ocrSaveMessage.set(null);

    if (this.savedOcrKey().trim()) {
      this.isSavingOcrKey.set(true);
      this.systemService.updateApiKey('ocr', '', true).subscribe({
        next: (res) => {
          this.isSavingOcrKey.set(false);
          this.ocrSaveMessage.set(res?.message || '✓ Vision OCR API key cleared.');
          setTimeout(() => this.ocrSaveMessage.set(null), 5000);
        },
        error: (err) => {
          this.isSavingOcrKey.set(false);
          this.ocrSaveError.set(err?.error?.detail || err?.message || 'Failed to clear Vision OCR key.');
        }
      });
    } else {
      this.ocrSaveMessage.set('✓ Vision OCR API key cleared.');
      setTimeout(() => this.ocrSaveMessage.set(null), 3000);
    }
  }

  public testLlmKey(): void {
    const key = this.llmKeyInput().trim();
    if (!key) return;
    this.isTestingLlmKey.set(true);
    this.llmSaveError.set(null);
    this.llmSaveMessage.set(null);

    this.systemService.testApiKey(
      this.llmKeyProvider(),
      key,
      this.llmKeyProvider() === 'openai' ? this.llmBaseUrl().trim() || undefined : undefined
    ).subscribe({
      next: (res) => {
        this.isTestingLlmKey.set(false);
        if (res?.valid) {
          this.llmSaveMessage.set(res?.message || `✓ ${this.llmProviderLabel()} API key is valid!`);
        } else {
          this.llmSaveError.set(res?.message || `${this.llmProviderLabel()} API key verification failed.`);
        }
        setTimeout(() => this.llmSaveMessage.set(null), 5000);
      },
      error: (err) => {
        this.isTestingLlmKey.set(false);
        this.llmSaveError.set(err?.error?.detail || err?.message || `${this.llmProviderLabel()} API key test failed.`);
      }
    });
  }

  public testOcrKey(): void {
    const key = this.ocrKeyInput().trim();
    if (!key) return;
    this.isTestingOcrKey.set(true);
    this.ocrSaveError.set(null);
    this.ocrSaveMessage.set(null);

    this.systemService.testApiKey('ocr', key).subscribe({
      next: (res) => {
        this.isTestingOcrKey.set(false);
        if (res?.valid) {
          this.ocrSaveMessage.set(res?.message || '✓ Vision OCR API key is valid!');
        } else {
          this.ocrSaveError.set(res?.message || 'Vision OCR API key verification failed.');
        }
        setTimeout(() => this.ocrSaveMessage.set(null), 5000);
      },
      error: (err) => {
        this.isTestingOcrKey.set(false);
        this.ocrSaveError.set(err?.error?.detail || err?.message || 'Vision OCR API key test failed.');
      }
    });
  }

  public getProviderDisplayName(tab: string): string {
    switch (tab) {
      case 'gemini': return 'Gemini';
      case 'ocr': return 'Vision OCR';
      default: return tab;
    }
  }

  public getProviderEnvVar(tab: string): string {
    switch (tab) {
      case 'gemini': return 'GEMINI_API_KEY';
      case 'ocr': return 'GOOGLE_VISION_API_KEY';
      default: return 'API_KEY';
    }
  }

  public getProviderHint(tab: string): string {
    switch (tab) {
      case 'gemini':
        return 'For a quick start, Google Gemini provides a free API key. Lunatap also supports other models (OpenAI, Claude, OpenRouter, etc.)—you can configure your own API keys directly in .env or your environment.';
      case 'ocr':
        return 'Google Cloud Vision API key for on-screen OCR text detection and UI grounding.';
      default:
        return 'Configure your API key or use environment definitions.';
    }
  }

  public skipCredentialsCheck(): void {
    this.systemService.skipCredentialsCheck();
  }

  public getApiKeyPlaceholder(tab: string): string {
    switch (tab) {
      case 'gemini': return 'Enter Gemini API Key (e.g. AIzaSy...)';
      case 'ocr': return 'Enter Vision OCR API Key (e.g. AIzaSy...)';
      default: return 'Enter API Key...';
    }
  }

  public isTabProviderActive(tab: string): boolean {
    if (tab === 'ocr') {
      return this.isOcrConfigured();
    }
    const targetProvider = tab === 'gemini' ? 'google' : tab;
    const providers = this.configuredLlmProviders();
    return providers.some(p => p.provider === targetProvider);
  }

  public refreshReadiness(): void {
    this.isRefreshingDiagnostics.set(true);
    this.systemService.fetchReadiness(false, true).subscribe({
      next: () => {
        this.systemService.fetchModelConfigEnv().subscribe({
          next: () => {
            setTimeout(() => this.isRefreshingDiagnostics.set(false), 450);
          },
          error: () => this.isRefreshingDiagnostics.set(false)
        });
      },
      error: () => this.isRefreshingDiagnostics.set(false)
    });
  }

  /** Re-list simulators and paired devices, and re-run the readiness probes. */
  public refreshDevices(): void {
    this.systemService.fetchDevices().subscribe({ error: () => {} });
    this.systemService.fetchReadiness(true, true).subscribe({ error: () => {} });
  }

  public toggleConnectionMethods(): void {
    this.showConnectionMethods.update(value => !value);
  }

  public selectTargetDevice(dev: DeviceInfo): void {
    this.systemService.selectDevice(dev.serial).subscribe({ error: () => {} });
  }

  public copyToClipboard(text: string, id: string): void {
    if (navigator.clipboard) {
      navigator.clipboard.writeText(text).then(() => {
        this.copiedId.set(id);
        setTimeout(() => {
          if (this.copiedId() === id) {
            this.copiedId.set(null);
          }
        }, 2000);
      });
    }
  }

  public setProfile(profile: 'flash' | 'pro'): void {
    this.selectedProfile.set(profile);
  }

  public setCategory(cat: SuggestionCategory): void {
    this.selectedCategory.set(cat);
  }

  public shuffleSuggestions(): void {
    this.shuffleOffset.update(v => v + 3);
  }

  public getAppNamesDisplay(apps: AppReference[]): string {
    return apps.map(a => a.name).join(' + ');
  }

  public applySuggestion(item: SmartSuggestion): void {
    this.taskGoal.set(item.goal);
    this.selectedProfile.set(item.profile);
    this.errorMessage.set(null);
  }

  public applyQuickPrompt(promptGoal: string, profile?: 'flash' | 'pro'): void {
    this.taskGoal.set(promptGoal);
    if (profile) {
      this.selectedProfile.set(profile);
    }
    this.errorMessage.set(null);
  }


  public proceedToLauncher(): void {
    this.activeTab.set('launcher');
  }

  public runTask(): void {
    const goal = this.taskGoal().trim();
    if (!goal) {
      this.errorMessage.set('Please enter a task goal before running.');
      return;
    }

    if (!this.isReady()) {
      this.errorMessage.set('System prerequisites are not satisfied. Please review System Setup first.');
      this.activeTab.set('diagnostics');
      return;
    }

    this.isSubmitting.set(true);
    this.errorMessage.set(null);

    this.agentService
      .runTask(
        goal,
        this.selectedProfile(),
        this.selectedProfile() === 'pro' && this.expectedOutput().trim()
          ? this.expectedOutput().trim()
          : undefined,
        this.selectedProfile() === 'pro' ? this.enableOutputter() : undefined,
        this.selectedProfile() === 'pro'
          ? { verificationLevel: this.verificationLevel().id, explorerMode: this.explorerMode().id }
          : undefined
      )
      .subscribe({
        next: () => {
          this.isSubmitting.set(false);
          this.router.navigate(['/workspace']);
        },
        error: (err) => {
          console.error('Failed to submit task from home page:', err);
          this.isSubmitting.set(false);
          this.errorMessage.set(
            err?.error?.detail || 'Failed to submit task. Please check server connection.'
          );
        }
      });
  }
}

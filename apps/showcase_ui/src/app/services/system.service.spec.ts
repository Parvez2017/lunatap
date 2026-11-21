import { provideHttpClient, withXhr } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';

import { SystemReadinessReport } from '../core/models/system.model';
import { SystemService } from './system.service';

describe('SystemService readiness polling', () => {
  let service: SystemService;
  let http: HttpTestingController;

  const report = (timestamp: number): SystemReadinessReport => ({
    overall_ready: true,
    blocker_count: 3,
    passed_blocker_count: 3,
    probes: [],
    active_device: null,
    os_type: 'windows',
    timestamp
  });

  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [
        SystemService,
        provideHttpClient(withXhr()),
        provideHttpClientTesting()
      ]
    });
    service = TestBed.inject(SystemService);
    http = TestBed.inject(HttpTestingController);
    service.stopAutoPolling();
  });

  afterEach(() => http.verify());

  it('shares one HTTP request across overlapping readiness callers', () => {
    let firstTimestamp = 0;
    let secondTimestamp = 0;

    service.fetchReadiness().subscribe(value => firstTimestamp = value.timestamp);
    service.fetchReadiness(true).subscribe(value => secondTimestamp = value.timestamp);

    const request = http.expectOne('/api/system/readiness');
    request.flush(report(10));

    expect(firstTimestamp).toBe(10);
    expect(secondTimestamp).toBe(10);
    expect(service.readinessReport()?.timestamp).toBe(10);
    expect(service.isLoading()).toBeFalse();
  });

  it('does not let an older snapshot replace a newer report', () => {
    service.fetchReadiness().subscribe();
    http.expectOne('/api/system/readiness').flush(report(20));

    service.fetchReadiness().subscribe();
    http.expectOne('/api/system/readiness').flush(report(10));

    expect(service.readinessReport()?.timestamp).toBe(20);
  });

  it('requests a forced backend refresh for a manual re-check', () => {
    service.fetchReadiness(false, true).subscribe();

    const request = http.expectOne(req =>
      req.url === '/api/system/readiness' && req.params.get('force') === 'true'
    );
    expect(request.request.params.get('force')).toBe('true');
    request.flush(report(30));
  });

  it('lists iOS simulators from the readiness probe with platform tags', () => {
    service.fetchReadiness().subscribe();
    http.expectOne('/api/system/readiness').flush({
      ...report(60),
      probes: [
        {
          id: 'ios_simulators',
          category: 'device',
          title: 'iOS Simulator',
          status: 'pass',
          is_blocker: false,
          summary: '1 Booted',
          description: '',
          metadata: {
            simulators: [
              {
                udid: 'E1D9F1D1-04E5-4E95-801F-830B854FD3E2',
                name: 'iPhone 18 Pro',
                state: 'Booted',
                runtime: 'com.apple.CoreSimulator.SimRuntime.iOS-27-0',
                isAvailable: true
              },
              {
                udid: 'AAAA1111-2222-3333-4444-555566667777',
                name: 'iPhone 17',
                state: 'Shutdown',
                runtime: 'com.apple.CoreSimulator.SimRuntime.iOS-26-0',
                isAvailable: true
              }
            ]
          },
          actions: []
        }
      ]
    });

    const devices = service.connectedDevices();
    expect(devices.length).toBe(2);
    expect(devices.every(d => d.platform === 'ios')).toBeTrue();
    const booted = devices.find(d => d.serial === 'E1D9F1D1-04E5-4E95-801F-830B854FD3E2');
    expect(booted?.platform).toBe('ios');
    expect(booted?.model).toBe('iPhone 18 Pro');
    expect(booted?.state).toBe('device');
    expect(booted?.product).toBe('iOS 27.0');
    expect(booted?.is_emulator).toBeTrue();
    const shutdown = devices.find(d => d.serial === 'AAAA1111-2222-3333-4444-555566667777');
    expect(shutdown?.state).toBe('Shutdown');
  });

  it('selects an iOS simulator by posting its platform and tracks it locally', () => {
    service.fetchReadiness().subscribe();
    http.expectOne('/api/system/readiness').flush({
      ...report(70),
      probes: [
        {
          id: 'ios_simulators',
          category: 'device',
          title: 'iOS Simulator',
          status: 'pass',
          is_blocker: false,
          summary: '1 Booted',
          description: '',
          metadata: {
            simulators: [
              {
                udid: 'UDID-1',
                name: 'iPhone 18 Pro',
                state: 'Booted',
                runtime: 'com.apple.CoreSimulator.SimRuntime.iOS-27-0',
                isAvailable: true
              }
            ]
          },
          actions: []
        }
      ]
    });

    service.selectDevice('UDID-1').subscribe();
    const request = http.expectOne('/api/system/devices/select');
    expect(request.request.body).toEqual({ serial: 'UDID-1', platform: 'ios' });
    request.flush({ status: 'success', selected_serial: 'UDID-1', platform: 'ios' });

    expect(service.selectedIosDevice()?.serial).toBe('UDID-1');
    expect(service.selectedDeviceSerial()).toBe('UDID-1');
    expect(service.isDeviceReady()).toBeTrue();
  });

  const iosProbeWith = (metadata: any) => ({
    id: 'ios_simulators',
    category: 'device',
    title: 'iOS Devices',
    status: 'pass',
    is_blocker: false,
    summary: 'iOS ready',
    description: '',
    metadata,
    actions: []
  });

  const envProbe = (id: string, status = 'pass') => ({
    id,
    category: 'runtime',
    title: id,
    status,
    is_blocker: true,
    summary: status,
    description: '',
    metadata: {},
    actions: []
  });

  it('merges paired physical iOS devices alongside simulators', () => {
    service.fetchReadiness().subscribe();
    http.expectOne('/api/system/readiness').flush({
      ...report(90),
      probes: [
        iosProbeWith({
          xcode_27_or_newer: true,
          simulators: [
            {
              udid: 'SIM-1',
              name: 'iPhone 18 Pro',
              state: 'Booted',
              runtime: 'com.apple.CoreSimulator.SimRuntime.iOS-27-0',
              isAvailable: true
            }
          ],
          connected_physical_devices: [
            {
              udid: 'PHYS-1',
              name: 'Office iPhone',
              os_version: '27.1',
              product_type: 'iPhone17,2'
            },
            { name: 'No UDID — skipped', os_version: '27.0' }
          ]
        })
      ]
    });

    const devices = service.connectedDevices();
    const physical = devices.find(d => d.serial === 'PHYS-1');
    expect(devices.filter(d => d.platform === 'ios').length).toBe(2);
    expect(physical?.platform).toBe('ios');
    expect(physical?.is_emulator).toBeFalse();
    expect(physical?.state).toBe('device');
    expect(physical?.product).toBe('iOS 27.1');
  });

  it('selects a physical iOS device by serial and posts its platform', () => {
    service.fetchReadiness().subscribe();
    http.expectOne('/api/system/readiness').flush({
      ...report(91),
      probes: [
        iosProbeWith({
          xcode_27_or_newer: true,
          connected_physical_devices: [
            { udid: 'PHYS-9', name: 'Office iPhone', os_version: '27.0' }
          ]
        })
      ]
    });

    service.selectDevice('PHYS-9').subscribe();
    const request = http.expectOne('/api/system/devices/select');
    expect(request.request.body).toEqual({ serial: 'PHYS-9', platform: 'ios' });
    request.flush({ status: 'success', selected_serial: 'PHYS-9', platform: 'ios' });

    expect(service.selectedIosDevice()?.serial).toBe('PHYS-9');
    expect(service.selectedIosDevice()?.is_emulator).toBeFalse();
    expect(service.isDeviceReady()).toBeTrue();
  });

  it('reverts the optimistic iOS selection when the server rejects it', () => {
    service.fetchReadiness().subscribe();
    http.expectOne('/api/system/readiness').flush({
      ...report(92),
      probes: [
        iosProbeWith({
          xcode_27_or_newer: true,
          simulators: [
            {
              udid: 'SIM-2',
              name: 'iPhone 18 Pro',
              state: 'Booted',
              runtime: 'r',
              isAvailable: true
            }
          ]
        })
      ]
    });

    service.selectDevice('SIM-2').subscribe({ error: () => {} });
    expect(service.selectedIosDevice()?.serial).toBe('SIM-2'); // optimistic
    http
      .expectOne('/api/system/devices/select')
      .flush('boom', { status: 500, statusText: 'Internal Server Error' });
    expect(service.selectedIosDevice()).toBeNull();
  });

  it('gates environment readiness on macOS + Xcode iOS tooling', () => {
    service.fetchReadiness().subscribe();
    http.expectOne('/api/system/readiness').flush({
      ...report(93),
      os_type: 'darwin',
      probes: [
        envProbe('python_runtime'),
        envProbe('system_config'),
        iosProbeWith({
          xcode_27_or_newer: true,
          simulators: [
            { udid: 'SIM-3', name: 'Sim', state: 'Booted', runtime: 'r', isAvailable: true }
          ]
        })
      ]
    });
    expect(service.isEnvironmentReady()).toBeTrue();
    expect(service.isDeviceReady()).toBeFalse(); // no device selected yet

    service.selectDevice('SIM-3').subscribe();
    http.expectOne('/api/system/devices/select').flush({ status: 'success' });

    expect(service.isDeviceReady()).toBeTrue();
    expect(service.isReady()).toBeFalse(); // credentials still missing
  });

  it('keeps the environment not-ready when Xcode is too old', () => {
    service.fetchReadiness().subscribe();
    http.expectOne('/api/system/readiness').flush({
      ...report(94),
      os_type: 'darwin',
      probes: [
        envProbe('python_runtime'),
        envProbe('system_config'),
        iosProbeWith({
          xcode_27_or_newer: false,
          simulators: [
            { udid: 'SIM-4', name: 'Sim', state: 'Booted', runtime: 'r', isAvailable: true }
          ]
        })
      ]
    });

    service.selectDevice('SIM-4').subscribe();
    http.expectOne('/api/system/devices/select').flush({ status: 'success' });

    expect(service.isEnvironmentReady()).toBeFalse();
  });


  it('stays not-ready off macOS even with Xcode metadata', () => {
    service.fetchReadiness().subscribe();
    http.expectOne('/api/system/readiness').flush({
      ...report(96),
      os_type: 'linux',
      probes: [
        envProbe('python_runtime'),
        envProbe('system_config'),
        iosProbeWith({ ios_automation_ready: true })
      ]
    });
    expect(service.isEnvironmentReady()).toBeFalse();
  });

  it('lists iOS devices from /api/devices ahead of probe metadata', () => {
    service.fetchReadiness().subscribe();
    http.expectOne('/api/system/readiness').flush({
      ...report(97),
      probes: [
        iosProbeWith({
          simulators: [
            { udid: 'SIM-A', name: 'iPhone 17', state: 'Shutdown', runtime: 'r', isAvailable: true },
            { udid: 'SIM-B', name: 'iPad Air', state: 'Shutdown', runtime: 'r', isAvailable: true }
          ]
        })
      ]
    });

    service.fetchDevices().subscribe();
    http.expectOne('/api/devices').flush({
      devices: [
        {
          serial: 'SIM-A',
          state: 'device',
          model: 'iPhone 17',
          product: 'iOS 26 0',
          is_emulator: true,
          is_busy: true,
          platform: 'ios'
        }
      ]
    });

    const devices = service.connectedDevices();
    expect(devices.map(d => d.serial)).toEqual(['SIM-A', 'SIM-B']);
    expect(devices[0].state).toBe('device');
    expect(devices[0].is_busy).toBeTrue();
    expect(service.isRefreshingDevices()).toBeFalse();
  });

  it('posts platform ios when selecting a device', () => {
    service.apiDevices.set([
      { serial: 'PHYS-2', state: 'device', model: 'iPhone', product: 'iOS 27.0 physical', is_emulator: false, platform: 'ios' }
    ]);
    service.selectDevice('PHYS-2').subscribe();
    const request = http.expectOne('/api/system/devices/select');
    expect(request.request.body).toEqual({ serial: 'PHYS-2', platform: 'ios' });
    request.flush({ status: 'success', selected_serial: 'PHYS-2', platform: 'ios' });
    expect(service.selectedIosDevice()?.is_emulator).toBeFalse();
  });
});

import { StartupProgressEvent } from '../../services/agent.service';
import { buildStartupWorkItems } from './agent-stream.component';

describe('startup Work block', () => {
  it('shows only the device preparation operations', () => {
    const events: StartupProgressEvent[] = [
      { stage: 'submitting', message: 'Submitting the task', timestamp: 100 },
      { stage: 'queued', message: 'Task received and queued', timestamp: 100.9 },
      { stage: 'launching', message: 'Starting the execution process', timestamp: 101 },
      { stage: 'configuration', message: 'Loading the run configuration', timestamp: 103.1 },
      { stage: 'device_check', message: 'Checking the iOS device', timestamp: 103.2 },
      { stage: 'device_ready', message: 'iOS device selected', timestamp: 123 },
      { stage: 'model_warmup', message: 'Warming the model connection', timestamp: 123 },
      { stage: 'environment', message: 'Preparing the device environment', timestamp: 128 },
      { stage: 'environment_ready', message: 'Device environment is ready', timestamp: 131 }
    ];

    const items = buildStartupWorkItems(events, 131, false, true);

    expect(items.map((item) => item.stage)).toEqual([
      'device_ready',
      'environment_ready'
    ]);
    expect(items.map((item) => item.elapsed)).toEqual(['20s', '3.0s']);
    expect(items.every((item) => !item.isActive)).toBeTrue();
  });

  it('keeps the current preparation operation live without exposing first-action text', () => {
    const events: StartupProgressEvent[] = [
      { stage: 'device_check', message: 'Checking the iOS device', timestamp: 10 }
    ];

    const [item] = buildStartupWorkItems(events, 11.4, false, true);

    expect(item.message).toBe('Checking the iOS device');
    expect(item.elapsed).toBe('1.4s');
    expect(item.isActive).toBeTrue();
  });

  it('infers device completion from the next stage and uses the generic line', () => {
    const events: StartupProgressEvent[] = [
      { stage: 'device_check', message: 'Checking the iOS device', timestamp: 10 },
      { stage: 'environment', message: 'Preparing the device environment', timestamp: 14 }
    ];

    const [item] = buildStartupWorkItems(events, 20, false, true);

    expect(item.message).toBe('iOS device connected');
    expect(item.elapsed).toBe('4.0s');
    expect(item.isActive).toBeFalse();
  });

  it('uses old first_response events only as a hidden completion boundary', () => {
    const events: StartupProgressEvent[] = [
      { stage: 'environment', message: 'Preparing the device environment', timestamp: 20 },
      { stage: 'first_response', message: 'Generating the first action', timestamp: 23 }
    ];

    const [item] = buildStartupWorkItems(events, 24, false, true);

    expect(item.message).toBe('Device environment is ready');
    expect(item.elapsed).toBe('3.0s');
    expect(item.isActive).toBeFalse();
  });
});

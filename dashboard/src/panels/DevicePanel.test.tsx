// MON-05: 고장 격리 원인은 원인 코드에서 계산한다
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import { DataCtx } from '../data/context';
import { LAYOUT } from '../data/mock/fixtures';
import { SCENES } from '../data/mock/story';
import { createMockSource } from '../data/mock/mockSource';
import type { ReasonCode } from '../model/types';
import { DevicePanel } from './DevicePanel';

function renderFault(cause: ReasonCode) {
  const snapshot = SCENES.fault();
  snapshot.devices = snapshot.devices.map((d) => (d.nodeId === 'locker-2' ? { ...d, fault: { cause, action: 'open', at: '14:29:40' } } : d));
  render(
    <DataCtx.Provider value={{ source: createMockSource({ scene: 'fault' }), layout: LAYOUT, snapshot, personName: (id) => id, deviceName: (id) => id }}>
      <DevicePanel nodeId="locker-2" onClose={() => {}} />
    </DataCtx.Provider>
  );
}

afterEach(cleanup);

describe('DevicePanel FAULT', () => {
  it('FR12B 결과 불명이면 결과를 몰라서 격리했다고 쓴다', () => {
    renderFault('EXEC_UNKNOWN');
    expect(screen.getByText('열기 명령의 결과를 몰라서 후보에서 빼고 격리했어요')).toBeTruthy();
  });
  it('FR12B 실행 실패면 기기가 실패를 알렸다고 쓴다', () => {
    renderFault('EXEC_FAILED');
    expect(screen.getByText('기기가 열기 명령 실패를 알려서 후보에서 빼고 격리했어요')).toBeTruthy();
  });
});

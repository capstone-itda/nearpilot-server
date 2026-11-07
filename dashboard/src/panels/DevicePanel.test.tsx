// MON-05: 고장 격리 원인은 원인 코드에서 계산한다
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import { DataCtx } from '../data/context';
import { DECLARATIONS, PLACES } from '../data/mock/fixtures';
import { SCENES } from '../data/mock/story';
import { createMockSource } from '../data/mock/mockSource';
import type { PlaceInfo, ReasonCode } from '../model/types';
import { DevicePanel } from './DevicePanel';

function renderFault(cause: ReasonCode) {
  const snapshot = SCENES.fault();
  snapshot.devices = snapshot.devices.map((d) => (d.nodeId === 'locker-2' ? { ...d, fault: { cause, action: 'open', at: '14:29:40' } } : d));
  render(
    <DataCtx.Provider value={{ source: createMockSource({ scene: 'fault' }), places: PLACES, snapshot, personName: (id) => id, deviceName: (id) => id }}>
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

describe('DevicePanel 여러 장소', () => {
  it('FR12A 라운지가 주 장소일 때도 옆방 기기 무드등의 이름·방식·동작이 맞다', () => {
    const names = new Map(PLACES.flatMap((p) => p.devices).map((d) => [d.nodeId, d.name]));
    render(
      <DataCtx.Provider value={{ source: createMockSource({ scene: 'side-room' }), places: PLACES, snapshot: SCENES['side-room'](), personName: (id) => id, deviceName: (id) => names.get(id) ?? id }}>
        <DevicePanel nodeId="mood-1" onClose={() => {}} />
      </DataCtx.Provider>
    );
    expect(screen.getByRole('heading', { name: /무드등/ })).toBeTruthy();
    expect(screen.getByText('공용형 · shared')).toBeTruthy();
    expect(screen.getByText('켜기 · 끄기')).toBeTruthy();
  });
});

describe('DevicePanel 기기 선언', () => {
  const FAN_PLACE: PlaceInfo = {
    placeId: 'lab', placeName: '실험실', modelVersion: 'm', calibrationVersion: 'c',
    devices: [{
      nodeId: 'fan-1', name: '선풍기', deviceKind: 'fan', occupancy: 'shared',
      vendor: { name: 'acme', model: 'f1', firmware: '1.0.3' },
      actions: [{ name: 'spin', description: '팬을 돌린다' }, { name: 'stop', description: '팬을 멈춘다', role: 'deactivate' }],
      descriptorHash: '0123456789abcdef'.repeat(4)
    }]
  };
  const show = (places: PlaceInfo[], nodeId: string, scene: 'idle' | 'fault' = 'idle') => render(
    <DataCtx.Provider value={{ source: createMockSource({ scene }), places, snapshot: SCENES[scene](), personName: (id) => id, deviceName: (id) => id }}>
      <DevicePanel nodeId={nodeId} onClose={() => {}} />
    </DataCtx.Provider>
  );

  it('FR12A 제작사·모델·펌웨어, 동작과 역할, 선언 해시 전체를 보인다', () => {
    show([FAN_PLACE], 'fan-1');
    expect(screen.getByText('fan · fan')).toBeTruthy();
    expect(screen.getByText('acme · f1 · 1.0.3')).toBeTruthy();
    expect(screen.getByText('spin')).toBeTruthy();  // 동작 이름은 선언 그대로
    expect(screen.getByText('spin · stop')).toBeTruthy();  // 기기 절의 동작 행: 모르는 동작은 이름 그대로
    expect(screen.getByText('팬을 돌린다')).toBeTruthy();
    expect(screen.getByText('팬을 멈춘다 · 끄기 역할')).toBeTruthy();
    expect(screen.getByText('0123456789abcdef'.repeat(4))).toBeTruthy();  // 줄이지 않는다 (FR-13)
  });
  it('FR12A 사물함은 열기 · 닫기이고 닫기는 재조작 역할이다', () => {
    show(PLACES, 'locker-1');
    expect(screen.getByText('열기 · 닫기')).toBeTruthy();
    expect(screen.getByText('사물함을 잠근다. 반납이 아니다 · 재조작')).toBeTruthy();
    expect(screen.getByText(DECLARATIONS.locker.descriptorHash)).toBeTruthy();
  });
  it('FR12A 고장 격리된 기기도 선언을 보인다', () => {
    show(PLACES, 'locker-2', 'fault');
    expect(screen.getByText('기기 선언')).toBeTruthy();
    expect(screen.getByText(DECLARATIONS.locker.descriptorHash)).toBeTruthy();
  });
});

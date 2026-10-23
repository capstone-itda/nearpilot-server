// MON-04: 요청 상세는 주 장소가 아닌 장소의 기기도 이름과 기능으로 보인다
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import { DataCtx } from '../data/context';
import { PLACES } from '../data/mock/fixtures';
import { createMockSource } from '../data/mock/mockSource';
import { SCENES } from '../data/mock/story';
import type { RequestProgress } from '../model/types';
import { RequestPanel } from './RequestPanel';

afterEach(cleanup);

const names = new Map(PLACES.flatMap((p) => p.devices).map((d) => [d.nodeId, d.name]));
const ask = (r: Partial<RequestProgress>): RequestProgress => ({
  id: 'r-side', personId: 'p-a', tool: 'request_use', target: { kind: 'explicit', nodeId: 'mood-1' }, action: 'on', at: '14:43:48',
  steps: [{ result: 'pass' }, { result: 'pass' }, { result: 'pass' }, { result: 'stop' }, { result: 'skip' }, { result: 'skip' }, { result: 'skip' }],
  verdict: 'ASK', reason: 'LOW_CONFIDENCE', ...r
});

function renderRequest(r: RequestProgress) {
  render(
    <DataCtx.Provider value={{ source: createMockSource({ scene: 'side-room' }), places: PLACES, snapshot: SCENES['side-room'](), personName: (id) => id, deviceName: (id) => names.get(id) ?? id }}>
      <RequestPanel r={r} onClose={() => {}} />
    </DataCtx.Provider>
  );
}

describe('RequestPanel 여러 장소', () => {
  it('FR12A 옆방 기기의 ASK는 후보 막대에 무드등을 보이고, 기능 이름으로 묻는다', () => {
    renderRequest(ask({ nodeId: 'mood-1', posteriors: { 'mood-1': 0.58, 'light-1': 0.31 }, threshold: 0.75 }));
    const bars = document.querySelector('[data-component="ProbBars"]')!;
    expect(bars.textContent).toContain('무드등');
    expect(bars.textContent).toContain('조명 1');
    expect(screen.getByText('어느 조명 앞일까')).toBeTruthy();
  });

  it('FR12A 판정이 고른 옆방 기기의 이름으로 실행 결과를 말한다', () => {
    renderRequest(ask({ target: { kind: 'nearest', capability: 'light' }, verdict: 'OK', reason: 'OK', nodeId: 'mood-1', execution: 'succeeded' }));
    expect(screen.getByText('무드등 켜기')).toBeTruthy();
    expect(screen.getByText('무드등이 켜졌어요')).toBeTruthy();
  });
});

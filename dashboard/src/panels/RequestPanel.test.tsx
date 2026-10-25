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

describe('RequestPanel 실행 결과', () => {
  const executed = (execution: RequestProgress['execution']) =>
    ask({ target: { kind: 'nearest', capability: 'light' }, verdict: 'OK', reason: 'OK', nodeId: 'mood-1', execution });

  it('FR12A 기기가 실행 전에 거절했으면 격리하지 않았다고 말한다. 고장 격리라고 쓰지 않는다', () => {
    renderRequest(executed('rejected'));
    expect(screen.getByText('기기가 실행 전에 거절했어요 · 격리하지 않았어요 (rejected)')).toBeTruthy();
    expect(document.body.textContent).not.toContain('고장 격리');
  });
  it.each([
    ['succeeded', '무드등이 켜졌어요'],
    ['failed', '기기가 실패를 알려 고장 격리했어요 (EXEC_FAILED)'],
    ['unknown', '응답이 없어 결과를 몰라 고장 격리했어요 (EXEC_UNKNOWN)'],
    ['rejected', '기기가 실행 전에 거절했어요 · 격리하지 않았어요 (rejected)'],
    ['sent', '명령 보냄 · 결과 기다림'],
    [undefined, '허가 · 명령 보낼 차례']
  ] as const)('FR12A 실행 결과가 %s이면 결과 줄이 같은 말을 한다', (execution, text) => {
    renderRequest(executed(execution));
    const result = screen.getByText('결과').nextElementSibling;
    expect(result?.textContent).toBe(text);
  });
});

describe('RequestPanel 승인 신청의 끝', () => {
  const needApproval = (r: Partial<RequestProgress>): RequestProgress => ({
    id: 'r-appr', personId: 'p-b', tool: 'request_use', target: { kind: 'nearest', capability: 'light' }, action: 'on', at: '14:30:58',
    steps: [{ result: 'pass' }, { result: 'pass' }, { result: 'pass' }, { result: 'pass' }, { result: 'stop' }, { result: 'skip' }, { result: 'skip' }],
    verdict: 'NEED_APPROVAL', reason: 'APPROVAL_REQUIRED', nodeId: 'light-1', accessRequestedAt: '14:31:00', ...r
  });
  const TEXTS = ['소유자가 승인 · 다시 요청하면 실행', '소유자가 거절 · 실행 안 함', '소유자 응답 없이 신청 만료 · 실행 안 함'];

  it.each([
    ['approved', TEXTS[0]],
    ['denied', TEXTS[1]],
    ['expired', TEXTS[2]],
    // 끝난 까닭을 모르는 요청(조회 REST)은 만료로 읽는다
    [undefined, TEXTS[2]]
  ] as const)('FR12A 승인 신청이 끝난 까닭이 %s이면 승인 진행의 마지막 줄이 그 까닭을 말한다', (accessEnd, text) => {
    renderRequest(needApproval({ accessExpiredAt: '14:31:30', ...(accessEnd ? { accessEnd } : {}) }));
    const timeline = document.querySelector('[data-component="Timeline"]')!;
    expect(timeline.querySelector('.np-tl__item:last-child .np-tl__text')?.textContent).toBe(text);
    expect(timeline.querySelector('.np-tl__item:last-child .np-tl__time')?.textContent).toBe('14:31:30');
    for (const other of TEXTS.filter((t) => t !== text)) expect(screen.queryByText(other)).toBeNull();
  });
});

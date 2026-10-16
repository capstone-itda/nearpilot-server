// FR-12B 판정 게이지: 도구 호출 뒤의 판정 결과를 게이지가 찰 때까지 늦게 넘긴다
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { StreamEvent } from '../model/events';
import { JUDGE_GAUGE_MS, paceJudging } from './pace';

const call: StreamEvent = { type: 'mcp_call', at: '14:00:01.000', requestId: 'r-1', personId: 'p-a', host: 'claude', tool: 'request_use', target: { kind: 'nearest', capability: 'locker' }, action: 'open' };
const step: StreamEvent = { type: 'step', at: '14:00:01.020', requestId: 'r-1', step: 0, result: 'pass' };
const verdict: StreamEvent = { type: 'verdict', at: '14:00:01.200', requestId: 'r-1', verdict: 'DENY', reason: 'POLICY_DENIED' };
const rssi: StreamEvent = { type: 'rssi', at: '14:00:01.100', personId: 'p-a', rssi: { 'locker-1': -50 } };

afterEach(() => vi.useRealTimers());

describe('paceJudging', () => {
  it('FR12B 도구 호출 뒤의 단계와 판정은 게이지가 찬 뒤 순서대로 넘기고, RSSI는 바로 넘긴다', () => {
    vi.useFakeTimers();
    const got: string[][] = [];
    const p = paceJudging((x) => got.push(x.map((e) => e.type)));
    p.push([call]);
    p.push([step]);
    p.push([rssi]);
    p.push([verdict]);
    expect(got).toEqual([['mcp_call'], ['rssi']]);
    vi.advanceTimersByTime(JUDGE_GAUGE_MS);
    expect(got).toEqual([['mcp_call'], ['rssi'], ['step', 'verdict']]);
  });
  it('FR12B 같은 묶음에 판정까지 있으면(다시 연결해 받은 상태) 늦추지 않는다', () => {
    const got: string[][] = [];
    paceJudging((x) => got.push(x.map((e) => e.type))).push([call, step, verdict]);
    expect(got).toEqual([['mcp_call', 'step', 'verdict']]);
  });
});

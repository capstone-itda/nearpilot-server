// FR-12B: 이벤트를 차례로 쌓아 모니터 상태를 만든다
import { describe, expect, it } from 'vitest';
import type { StreamEvent } from '../model/events';
import { FINISHED_HOLD_SEC, initialState, reduceAll } from './reduce';
import { PLACE } from './mock/fixtures';

const s0 = () => reduceAll(initialState(PLACE), [
  { type: 'beacon', at: '14:00:00.000', personId: 'p-a', pseudonym: '방문자 A', letter: 'A', role: 'visitor', seen: true },
  { type: 'rssi', at: '14:00:00.000', personId: 'p-a', rssi: { 'locker-1': -52 } }
]);
const call: StreamEvent = { type: 'mcp_call', at: '14:00:01.000', requestId: 'r-1', personId: 'p-a', host: 'claude', tool: 'request_use', target: { kind: 'nearest', capability: 'locker' }, action: 'open' };

describe('reduce', () => {
  it('FR12B 비콘과 RSSI가 사람을 만든다', () => {
    expect(s0().people).toEqual([{ id: 'p-a', pseudonym: '방문자 A', letter: 'A', role: 'visitor', beaconSeen: true, rssi: { 'locker-1': -52 } }]);
    const gone = reduceAll(s0(), [{ type: 'beacon', at: '14:00:02.000', personId: 'p-a', pseudonym: '방문자 A', letter: 'A', role: 'visitor', seen: false }]);
    expect(gone.people[0]).toMatchObject({ beaconSeen: false, rssi: {} });
  });

  it('FR12B 도구 호출이 오면 판정 전 요청이 생기고 단계는 기다림이다', () => {
    const s = reduceAll(s0(), [call]);
    expect(s.request).toMatchObject({ id: 'r-1', at: '14:00:01' });
    expect(s.request!.verdict).toBeUndefined();
    expect(s.request!.steps.every((x) => x.result === 'pending')).toBe(true);
    expect(s.lastReceivedAt).toBe('14:00:01');
  });

  it('FR12B 단계, 판정, 명령, 실행 결과, 응답이 차례로 요청을 바꾼다', () => {
    let s = reduceAll(s0(), [call,
      { type: 'step', at: '14:00:01.020', requestId: 'r-1', step: 0, result: 'pass', detail: '새 요청' },
      { type: 'step', at: '14:00:01.040', requestId: 'r-1', step: 1, result: 'pass' },
      { type: 'posterior', at: '14:00:01.050', requestId: 'r-1', posteriors: { 'locker-1': 0.97 }, threshold: 0.95 }]);
    expect(s.request!.steps.map((x) => x.result)).toEqual(['pass', 'pass', 'pending', 'pending', 'pending', 'pending', 'pending']);
    expect(s.request).toMatchObject({ posteriors: { 'locker-1': 0.97 }, threshold: 0.95 });

    s = reduceAll(s, [{ type: 'verdict', at: '14:00:01.200', requestId: 'r-1', verdict: 'OK', reason: 'OK', nodeId: 'locker-1' }]);
    // 판정이 나면 오지 않은 단계는 확인하지 않은 단계다
    expect(s.request!.steps.slice(2).every((x) => x.result === 'skip')).toBe(true);
    expect(s.request).toMatchObject({ verdict: 'OK', nodeId: 'locker-1' });

    s = reduceAll(s, [
      { type: 'transition', at: '14:00:01.210', nodeId: 'locker-1', to: 'RESERVED' },
      { type: 'command', at: '14:00:01.220', requestId: 'r-1', nodeId: 'locker-1', action: 'open' }]);
    expect(s.request!.execution).toBe('sent');
    expect(s.devices.find((d) => d.nodeId === 'locker-1')!.state).toBe('RESERVED');

    s = reduceAll(s, [
      { type: 'exec_result', at: '14:00:02.000', requestId: 'r-1', nodeId: 'locker-1', result: 'succeeded' },
      { type: 'transition', at: '14:00:02.010', nodeId: 'locker-1', to: 'ACTIVE' },
      { type: 'mcp_result', at: '14:00:02.020', requestId: 'r-1' }]);
    expect(s.request).toMatchObject({ execution: 'succeeded', respondedAt: '14:00:02' });
    expect(s.devices.find((d) => d.nodeId === 'locker-1')!.state).toBe('ACTIVE');
  });

  it('FR12B 다른 요청 번호의 이벤트는 지금 요청을 바꾸지 않는다', () => {
    const s = reduceAll(s0(), [call, { type: 'step', at: '14:00:01.020', requestId: 'r-x', step: 0, result: 'stop' }]);
    expect(s.request!.steps[0].result).toBe('pending');
  });

  it('FR12B 고장 격리 전이는 원인을 붙이고, 사용 가능으로 돌아가면 지운다', () => {
    let s = reduceAll(s0(), [{ type: 'transition', at: '14:00:03.000', nodeId: 'locker-2', to: 'FAULT', fault: { cause: 'EXEC_UNKNOWN', action: 'open' } }]);
    expect(s.devices.find((d) => d.nodeId === 'locker-2')!.fault).toEqual({ cause: 'EXEC_UNKNOWN', action: 'open', at: '14:00:03' });
    s = reduceAll(s, [{ type: 'transition', at: '14:00:04.000', nodeId: 'locker-2', to: 'AVAILABLE' }]);
    expect(s.devices.find((d) => d.nodeId === 'locker-2')!.fault).toBeUndefined();
  });

  it('FR12B 승인 신청은 소유자 알림을 켜고 만료는 끈다', () => {
    const base = reduceAll(s0(), [call, { type: 'verdict', at: '14:00:01.200', requestId: 'r-1', verdict: 'NEED_APPROVAL', reason: 'APPROVAL_REQUIRED', nodeId: 'locker-1' },
      { type: 'mcp_result', at: '14:00:01.300', requestId: 'r-1' }]);
    const asked = reduceAll(base, [{ type: 'approval', at: '14:00:05.000', requestId: 'r-1', nodeId: 'locker-1', personId: 'p-a', kind: 'requested' }]);
    expect(asked.ownerNotified).toBe(true);
    expect(asked.request!.accessRequestedAt).toBe('14:00:05');
    const expired = reduceAll(asked, [{ type: 'approval', at: '14:10:05.000', requestId: 'r-1', nodeId: 'locker-1', personId: 'p-a', kind: 'expired' }]);
    expect(expired.ownerNotified).toBe(false);
    expect(expired.request!.accessExpiredAt).toBe('14:10:05');
  });

  it('FR12B 끝난 요청은 잠시 남았다가 내려가고, 승인을 기다리는 요청은 남는다', () => {
    const done = reduceAll(s0(), [call, { type: 'verdict', at: '14:00:01.200', requestId: 'r-1', verdict: 'DENY', reason: 'POLICY_DENIED' },
      { type: 'mcp_result', at: '14:00:01.300', requestId: 'r-1' }]);
    const rssiAt = (t: string): StreamEvent => ({ type: 'rssi', at: t, personId: 'p-a', rssi: { 'locker-1': -52 } });
    expect(reduceAll(done, [rssiAt(`14:00:${String(1 + FINISHED_HOLD_SEC).padStart(2, '0')}.000`)]).request).toBeDefined();
    expect(reduceAll(done, [rssiAt(`14:00:${String(2 + FINISHED_HOLD_SEC).padStart(2, '0')}.000`)]).request).toBeUndefined();

    const waiting = reduceAll(s0(), [call, { type: 'verdict', at: '14:00:01.200', requestId: 'r-1', verdict: 'NEED_APPROVAL', reason: 'APPROVAL_REQUIRED' },
      { type: 'mcp_result', at: '14:00:01.300', requestId: 'r-1' },
      { type: 'approval', at: '14:00:05.000', requestId: 'r-1', nodeId: 'locker-1', personId: 'p-a', kind: 'requested' }]);
    expect(reduceAll(waiting, [rssiAt('14:05:00.000')]).request).toBeDefined();
  });

  it('FR12B 연결 상태는 링크 이벤트가 바꾸고 수신 시각은 그대로 둔다', () => {
    const s = reduceAll(s0(), [{ type: 'link', at: '14:00:09.000', connected: false, reconnectAttempts: 2 }]);
    expect(s).toMatchObject({ connected: false, reconnectAttempts: 2, lastReceivedAt: '14:00:00' });
  });
});

// FR-12B: 이벤트를 차례로 쌓아 모니터 상태를 만든다
import { describe, expect, it } from 'vitest';
import type { StreamEvent } from '../model/events';
import { FINISHED_HOLD_SEC, initialState, reduceAll } from './reduce';
import { LOUNGE, PLACES } from './mock/fixtures';

const s0 = () => reduceAll(initialState([LOUNGE]), [
  { type: 'beacon', at: '14:00:00.000', personId: 'p-a', pseudonym: '방문자 A', letter: 'A', role: 'visitor', seen: true },
  { type: 'rssi', at: '14:00:00.000', personId: 'p-a', rssi: { 'locker-1': -52 } }
]);
const call: StreamEvent = { type: 'mcp_call', at: '14:00:01.000', requestId: 'r-1', personId: 'p-a', tool: 'request_use', target: { kind: 'nearest', capability: 'locker' }, action: 'open' };

describe('reduce', () => {
  it('FR12B 비콘과 RSSI가 사람을 만든다', () => {
    expect(s0().people).toEqual([{ id: 'p-a', pseudonym: '방문자 A', letter: 'A', role: 'visitor', beaconSeen: true, rssi: { 'locker-1': -52 }, near: 'locker-1', nearCandidate: undefined }]);
    const gone = reduceAll(s0(), [{ type: 'beacon', at: '14:00:02.000', personId: 'p-a', pseudonym: '방문자 A', letter: 'A', role: 'visitor', seen: false }]);
    expect(gone.people[0]).toMatchObject({ beaconSeen: false, rssi: {}, near: undefined });
  });

  it('FR12B 처음 상태는 모든 장소의 기기를 사용 가능으로 둔다', () => {
    const s = initialState(PLACES);
    expect(s.devices.map((d) => d.nodeId)).toEqual(PLACES.flatMap((p) => p.devices.map((d) => d.nodeId)));
    expect(s.devices.every((d) => d.state === 'AVAILABLE')).toBe(true);
  });

  it('FR12B 무대에 세우는 기기는 장소와 상관없이 신호가 가장 센 기기다', () => {
    const s = reduceAll(initialState(PLACES), [
      { type: 'beacon', at: '14:00:00.000', personId: 'p-a', pseudonym: '방문자 A', letter: 'A', role: 'visitor', seen: true },
      { type: 'rssi', at: '14:00:00.000', personId: 'p-a', rssi: { 'locker-1': -70, 'mood-1': -50 } }
    ]);
    expect(s.people[0].near).toBe('mood-1');
  });

  it('FR12B 무대에 세우는 기기는 다른 기기 신호가 4dB 이상 센 상태가 2초 이어질 때만 바뀐다', () => {
    const at = (t: string, rssi: Record<string, number>): StreamEvent => ({ type: 'rssi', at: t, personId: 'p-a', rssi });
    const near = (events: StreamEvent[]) => reduceAll(s0(), events).people[0].near;
    // 3dB 차이는 흔들림으로 본다
    expect(near([at('14:00:01.000', { 'locker-1': -55, 'locker-2': -52 }), at('14:00:05.000', { 'locker-1': -55, 'locker-2': -52 })])).toBe('locker-1');
    // 4dB 이상이어도 2초가 안 되면 그대로
    expect(near([at('14:00:01.000', { 'locker-1': -56, 'locker-2': -50 }), at('14:00:02.500', { 'locker-1': -56, 'locker-2': -50 })])).toBe('locker-1');
    // 2초 이어지면 옮긴다
    expect(near([at('14:00:01.000', { 'locker-1': -56, 'locker-2': -50 }), at('14:00:03.000', { 'locker-1': -56, 'locker-2': -50 })])).toBe('locker-2');
    // 중간에 차이가 줄면 처음부터 다시 잰다
    expect(near([at('14:00:01.000', { 'locker-1': -56, 'locker-2': -50 }), at('14:00:02.000', { 'locker-1': -53, 'locker-2': -52 }),
      at('14:00:03.500', { 'locker-1': -56, 'locker-2': -50 })])).toBe('locker-1');
  });

  it('FR12B 지금 기기의 신호가 한 묶음에서 빠져도 바로 옮기지 않고, 다른 기기가 2초 이어 가장 세면 옮긴다', () => {
    const at = (t: string, rssi: Record<string, number>): StreamEvent => ({ type: 'rssi', at: t, personId: 'p-a', rssi });
    const near = (events: StreamEvent[]) => reduceAll(s0(), events).people[0].near;
    // 한 묶음만 빠지고 돌아오면 그대로
    expect(near([at('14:00:01.000', { 'mood-1': -78 }), at('14:00:01.500', { 'locker-1': -52, 'mood-1': -78 })])).toBe('locker-1');
    expect(near([at('14:00:01.000', { 'mood-1': -78 })])).toBe('locker-1');
    // 빠진 채 2초 이어지면 옮긴다
    expect(near([at('14:00:01.000', { 'mood-1': -78 }), at('14:00:03.000', { 'mood-1': -78 })])).toBe('mood-1');
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

describe('reduce 감사', () => {
  it('FR12B 감사 로그 줄이 오면 마지막 줄 ID가 바뀐다', () => {
    const s = reduceAll(s0(), [{ type: 'audit', at: '14:00:05.000', row: { id: 'a9', at: '14:00:05', step: '1', text: '비콘 수신 없음', code: 'BEACON_NOT_SEEN' } }]);
    expect(s.lastAuditId).toBe('a9');
  });
});

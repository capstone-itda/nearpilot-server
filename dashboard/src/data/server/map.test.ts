import { afterAll, beforeAll, describe, expect, it, vi } from 'vitest';
import type { StreamEvent } from '../../model/events';
import type { AuditRow } from '../../model/types';
import { approvalsOf, auditOf, healthOf, historyOf, mapEvent, mapExecution, placesOf, rentalOf, requestOf, serverPeriod, trustOf, usesOf } from './map';

// 시각은 이 PC의 지역 시각으로 바뀐다. 시험은 서울(UTC+9)로 고정한다.
beforeAll(() => { vi.stubEnv('TZ', 'Asia/Seoul'); });
afterAll(() => { vi.unstubAllEnvs(); });

/** 서버가 보내는 시각 (UTC 05:28:10.123 = 서울 14:28:10.123) */
const AT = '2026-10-24T05:28:10.123+00:00';
const AT_MS = '14:28:10.123';

const AUDIT_RAW = { id: 'a-1', at: AT, step: '3', nodeId: 'locker-1', text: '근접 통과', code: 'OK' };
const AUDIT_ROW: AuditRow = { id: 'a-1', at: '14:28:10', step: '3', nodeId: 'locker-1', text: '근접 통과', code: 'OK' };

/** architecture.md §5 "WebSocket" 형식 표의 키 그대로 쓴 서버 이벤트와, 화면 모델로 옮긴 결과 */
const EVENTS: [name: string, raw: Record<string, unknown>, mapped: StreamEvent][] = [
  ['beacon(방문자)',
    { type: 'beacon', at: AT, personId: 'p1', pseudonym: '방문자 A', letter: 'A', role: 'visitor', seen: true },
    { type: 'beacon', at: AT_MS, personId: 'p1', pseudonym: '방문자 A', letter: 'A', role: 'visitor', seen: true }],
  ['beacon(소유자는 letter 없음)',
    { type: 'beacon', at: AT, personId: 'p9', pseudonym: '소유자', role: 'owner', seen: false },
    { type: 'beacon', at: AT_MS, personId: 'p9', pseudonym: '소유자', role: 'owner', seen: false }],
  ['rssi',
    { type: 'rssi', at: AT, personId: 'p1', rssi: { 'locker-1': -58, 'light-1': -71 } },
    { type: 'rssi', at: AT_MS, personId: 'p1', rssi: { 'locker-1': -58, 'light-1': -71 } }],
  ['mcp_call',
    { type: 'mcp_call', at: AT, requestId: 'r1', personId: 'p1', tool: 'request_use', target: { kind: 'nearest', capability: 'locker' }, action: 'open' },
    { type: 'mcp_call', at: AT_MS, requestId: 'r1', personId: 'p1', tool: 'request_use', target: { kind: 'nearest', capability: 'locker' }, action: 'open' }],
  ['step("3"은 3번 칸)',
    { type: 'step', at: AT, requestId: 'r1', step: '3', result: 'pass', detail: '후보 locker-1 0.91' },
    { type: 'step', at: AT_MS, requestId: 'r1', step: 3, result: 'pass', detail: '후보 locker-1 0.91' }],
  ['step(detail 없음)',
    { type: 'step', at: AT, requestId: 'r1', step: '0', result: 'stop' },
    { type: 'step', at: AT_MS, requestId: 'r1', step: 0, result: 'stop' }],
  ['posterior',
    { type: 'posterior', at: AT, requestId: 'r1', posteriors: { 'locker-1': 0.91, 'light-1': 0.05 }, threshold: 0.8 },
    { type: 'posterior', at: AT_MS, requestId: 'r1', posteriors: { 'locker-1': 0.91, 'light-1': 0.05 }, threshold: 0.8 }],
  ['verdict',
    { type: 'verdict', at: AT, requestId: 'r1', verdict: 'OK', reason: 'OK', nodeId: 'locker-1' },
    { type: 'verdict', at: AT_MS, requestId: 'r1', verdict: 'OK', reason: 'OK', nodeId: 'locker-1' }],
  ['verdict(nodeId 없음)',
    { type: 'verdict', at: AT, requestId: 'r1', verdict: 'ASK', reason: 'MOVE_CLOSER' },
    { type: 'verdict', at: AT_MS, requestId: 'r1', verdict: 'ASK', reason: 'MOVE_CLOSER' }],
  ['command',
    { type: 'command', at: AT, requestId: 'r1', nodeId: 'locker-1', action: 'open' },
    { type: 'command', at: AT_MS, requestId: 'r1', nodeId: 'locker-1', action: 'open' }],
  ['exec_result',
    { type: 'exec_result', at: AT, requestId: 'r1', nodeId: 'locker-1', result: 'unknown' },
    { type: 'exec_result', at: AT_MS, requestId: 'r1', nodeId: 'locker-1', result: 'unknown' }],
  ['mcp_result',
    { type: 'mcp_result', at: AT, requestId: 'r1' },
    { type: 'mcp_result', at: AT_MS, requestId: 'r1' }],
  ['transition',
    { type: 'transition', at: AT, nodeId: 'locker-1', to: 'ACTIVE' },
    { type: 'transition', at: AT_MS, nodeId: 'locker-1', to: 'ACTIVE' }],
  ['transition(초과·고장 원인)',
    { type: 'transition', at: AT, nodeId: 'locker-1', to: 'FAULT', overdue: true, fault: { cause: 'EXEC_UNKNOWN', action: 'open' } },
    { type: 'transition', at: AT_MS, nodeId: 'locker-1', to: 'FAULT', overdue: true, fault: { cause: 'EXEC_UNKNOWN', action: 'open' } }],
  ['approval(신청)',
    { type: 'approval', at: AT, requestId: 'r1', nodeId: 'locker-2', personId: 'p1', kind: 'requested' },
    { type: 'approval', at: AT_MS, requestId: 'r1', nodeId: 'locker-2', personId: 'p1', kind: 'requested' }],
  ['approval(승인)',
    { type: 'approval', at: AT, requestId: 'r1', nodeId: 'locker-2', personId: 'p1', kind: 'approved' },
    { type: 'approval', at: AT_MS, requestId: 'r1', nodeId: 'locker-2', personId: 'p1', kind: 'approved' }],
  ['approval(거부)',
    { type: 'approval', at: AT, requestId: 'r1', nodeId: 'locker-2', personId: 'p1', kind: 'denied' },
    { type: 'approval', at: AT_MS, requestId: 'r1', nodeId: 'locker-2', personId: 'p1', kind: 'denied' }],
  ['approval(만료)',
    { type: 'approval', at: AT, requestId: 'r1', nodeId: 'locker-2', personId: 'p1', kind: 'expired' },
    { type: 'approval', at: AT_MS, requestId: 'r1', nodeId: 'locker-2', personId: 'p1', kind: 'expired' }],
  ['audit',
    { type: 'audit', at: AT, row: AUDIT_RAW },
    { type: 'audit', at: AT_MS, row: AUDIT_ROW }]
];

describe('서버 어댑터 스트림 이벤트 변환', () => {
  it.each(EVENTS)('FR12B %s 이벤트를 화면 모델로 옮긴다 (서버 시각은 지역 시각 HH:MM:SS.mmm)', (_name, raw, mapped) => {
    expect(mapEvent(raw)).toEqual(mapped);
  });

  it('FR12B 승인 이벤트의 끝 셋(approved·denied·expired)과 신청(requested)을 모두 받는다', () => {
    const kinds = ['requested', 'approved', 'denied', 'expired'].map((kind) => {
      const e = mapEvent({ type: 'approval', at: AT, requestId: 'r1', nodeId: 'locker-2', personId: 'p1', kind });
      return e?.type === 'approval' ? e.kind : undefined;
    });
    expect(kinds).toEqual(['requested', 'approved', 'denied', 'expired']);
    expect(mapEvent({ type: 'approval', at: AT, requestId: 'r1', nodeId: 'locker-2', personId: 'p1', kind: 'withdrawn' })).toBeUndefined();
  });

  it('FR12B step은 "0"~"6" 문자열만 받아 칸 번호로 바꾼다', () => {
    for (const s of ['0', '1', '2', '3', '4', '5', '6']) {
      expect(mapEvent({ type: 'step', at: AT, requestId: 'r1', step: s, result: 'pass' })).toMatchObject({ step: Number(s) });
    }
    for (const s of ['7', 'auth', 'exec', 3, null, '']) {
      expect(mapEvent({ type: 'step', at: AT, requestId: 'r1', step: s, result: 'pass' }), String(s)).toBeUndefined();
    }
  });

  it('FR12B 방문자의 request_use가 아닌 도구 호출이나 대상·동작이 없는 호출은 버린다', () => {
    const base = { type: 'mcp_call', at: AT, requestId: 'r1', personId: 'p1', tool: 'request_use', target: { kind: 'any' }, action: 'on' };
    expect(mapEvent(base)).toMatchObject({ type: 'mcp_call', target: { kind: 'any' }, action: 'on' });
    expect(mapEvent({ ...base, tool: 'check_status' })).toBeUndefined();
    expect(mapEvent({ ...base, tool: 'request_access' })).toBeUndefined();
    const { target: _target, ...noTarget } = base;
    expect(mapEvent(noTarget)).toBeUndefined();
    const { action: _action, ...noAction } = base;
    expect(mapEvent(noAction)).toBeUndefined();
  });

  it('FR12B 모르는 이벤트 종류는 undefined다 (프로토타입 이름도 종류로 보지 않는다)', () => {
    for (const type of ['future_event', 'link', 'mock_phone', '__proto__', 'constructor', 'toString', '', 7, null]) {
      expect(mapEvent({ type, at: AT }), String(type)).toBeUndefined();
    }
    for (const raw of [null, undefined, 'beacon', 42, [], [{ type: 'beacon' }]]) expect(mapEvent(raw)).toBeUndefined();
  });

  it('FR12B 형식이 틀린 이벤트는 undefined다: 필수 키 없음, 값 종류 어긋남, 시각 형식 어긋남', () => {
    const ok = { type: 'verdict', at: AT, requestId: 'r1', verdict: 'OK', reason: 'OK' };
    expect(mapEvent(ok)).toBeDefined();
    const { requestId: _requestId, ...noRequestId } = ok;
    expect(mapEvent(noRequestId)).toBeUndefined();
    expect(mapEvent({ ...ok, verdict: 'MAYBE' })).toBeUndefined();
    expect(mapEvent({ ...ok, verdict: 7 })).toBeUndefined();
    expect(mapEvent({ ...ok, at: '14:28:10' })).toBeUndefined();
    expect(mapEvent({ ...ok, at: '2026-10-24T05:28:10.123' })).toBeUndefined();
    expect(mapEvent({ ...ok, at: undefined })).toBeUndefined();
    expect(mapEvent({ type: 'rssi', at: AT, personId: 'p1', rssi: { 'locker-1': 'strong' } })).toBeUndefined();
    expect(mapEvent({ type: 'rssi', at: AT, personId: 'p1', rssi: { 'locker-1': Number.NaN } })).toBeUndefined();
    expect(mapEvent({ type: 'beacon', at: AT, personId: 'p1', pseudonym: '방문자 A', role: 'admin', seen: true })).toBeUndefined();
    expect(mapEvent({ type: 'transition', at: AT, nodeId: 'locker-1', to: 'BROKEN' })).toBeUndefined();
    expect(mapEvent({ type: 'audit', at: AT, row: { ...AUDIT_RAW, at: 'now' } })).toBeUndefined();
    expect(mapEvent({ type: 'audit', at: AT })).toBeUndefined();
  });

  it('FR12B 형식 표에 없는 키는 옮기지 않는다 (사용자 ID·토큰이 섞여 와도 화면 모델에 없다)', () => {
    const e = mapEvent({ type: 'beacon', at: AT, personId: 'p1', pseudonym: '방문자 A', letter: 'A', role: 'visitor', seen: true, userId: 'u-1', token: 'np_secret' });
    expect(e).toEqual({ type: 'beacon', at: AT_MS, personId: 'p1', pseudonym: '방문자 A', letter: 'A', role: 'visitor', seen: true });
    expect(JSON.stringify(e)).not.toMatch(/u-1|np_secret/);
  });

  it('FR12B 선택 키가 null이면 키를 뺀다', () => {
    const e = mapEvent({ type: 'verdict', at: AT, requestId: 'r1', verdict: 'ASK', reason: 'MOVE_CLOSER', nodeId: null });
    expect(e).toEqual({ type: 'verdict', at: AT_MS, requestId: 'r1', verdict: 'ASK', reason: 'MOVE_CLOSER' });
    expect(e).not.toHaveProperty('nodeId');
  });
});

describe('서버 어댑터 실행 상태', () => {
  it('FR12B 서버의 실행 상태를 화면 모델의 실행으로 옮긴다', () => {
    expect(mapExecution('pending')).toBe('sent');
    expect(mapExecution('in_progress')).toBe('sent');
    expect(mapExecution('succeeded')).toBe('succeeded');
    expect(mapExecution('failed')).toBe('failed');
    expect(mapExecution('unknown')).toBe('unknown');
  });
  it('FR12B 기기가 실행 전에 거절한 상태(rejected)는 실패(failed)가 아니라 거절(rejected)로 옮긴다', () => {
    expect(mapExecution('rejected')).toBe('rejected');
    expect(mapExecution('rejected')).not.toBe(mapExecution('failed'));
  });
  it('FR12B 확실한 미전송(not_sent)과 모르는 상태는 실행 없음이다', () => {
    expect(mapExecution('not_sent')).toBeUndefined();
    expect(mapExecution('teleported')).toBeUndefined();
    expect(mapExecution('')).toBeUndefined();
  });
});

describe('서버 어댑터 이력 기간', () => {
  it('FR12A 화면의 "오늘"은 today, 시각 구간은 HH:MM~HH:MM이다', () => {
    expect(serverPeriod('오늘')).toBe('today');
    expect(serverPeriod('14:26~14:29')).toBe('14:26~14:29');
    expect(serverPeriod('오늘 14:26~14:29')).toBe('14:26~14:29');
    expect(serverPeriod('9:05 ~ 10:00')).toBe('09:05~10:00');
  });
  it('FR12A 서버 값(today)과 그 밖의 값은 그대로 보낸다', () => {
    expect(serverPeriod('today')).toBe('today');
    expect(serverPeriod('어제')).toBe('어제');
  });
});

describe('서버 어댑터 조회 응답 변환', () => {
  const REQUEST = {
    id: 'r1', personId: 'p1', tool: 'request_use', target: { kind: 'nearest', capability: 'locker' }, action: 'open', at: AT,
    verdict: 'OK', reason: 'OK', nodeId: 'locker-1', posteriors: { 'locker-1': 0.91 }, threshold: 0.8, execution: 'succeeded',
    accessRequestedAt: '2026-10-24T05:28:12.000+00:00', accessExpiredAt: '2026-10-24T05:28:40.000+00:00', respondedAt: '2026-10-24T05:28:41.500+00:00',
    steps: [
      { result: 'pass' }, { result: 'pass' }, { result: 'pass' }, { result: 'pass', detail: '0.91' },
      { result: 'pass' }, { result: 'stop', detail: '사용 중' }, { result: 'skip' }
    ]
  };

  it('FR12A 요청 한 건: 시각은 HH:MM:SS, 실행 상태는 화면 모델의 값이다', () => {
    expect(requestOf(REQUEST)).toEqual({
      id: 'r1', personId: 'p1', tool: 'request_use', target: { kind: 'nearest', capability: 'locker' }, action: 'open', at: '14:28:10',
      verdict: 'OK', reason: 'OK', nodeId: 'locker-1', posteriors: { 'locker-1': 0.91 }, threshold: 0.8, execution: 'succeeded',
      accessRequestedAt: '14:28:12', accessExpiredAt: '14:28:40', respondedAt: '14:28:41',
      steps: REQUEST.steps
    });
  });
  it('FR12A 요청 한 건: 서버의 pending·in_progress는 sent, rejected는 rejected, not_sent는 키 없음이다', () => {
    expect(requestOf({ ...REQUEST, execution: 'pending' }).execution).toBe('sent');
    expect(requestOf({ ...REQUEST, execution: 'in_progress' }).execution).toBe('sent');
    expect(requestOf({ ...REQUEST, execution: 'rejected' }).execution).toBe('rejected');
    expect(requestOf({ ...REQUEST, execution: 'failed' }).execution).toBe('failed');
    expect(requestOf({ ...REQUEST, execution: 'unknown' }).execution).toBe('unknown');
    expect(requestOf({ ...REQUEST, execution: 'not_sent' })).not.toHaveProperty('execution');
  });
  it('FR12A 요청 한 건: 판정 전 요청은 선택 키 없이도 읽는다', () => {
    const { verdict: _v, reason: _r, nodeId: _n, posteriors: _p, threshold: _t, execution: _e, accessRequestedAt: _a, accessExpiredAt: _x, respondedAt: _d, ...early } = REQUEST;
    const r = requestOf({ ...early, steps: Array.from({ length: 7 }, () => ({ result: 'pending' })) });
    expect(r.verdict).toBeUndefined();
    expect(r.steps).toHaveLength(7);
  });
  it('FR12A 요청 한 건: 단계가 일곱 칸이 아니거나 값이 틀리면 형식 오류다. 오류 문구에 값은 없고 경로만 있다', () => {
    expect(() => requestOf({ ...REQUEST, steps: REQUEST.steps.slice(0, 6) })).toThrow('서버 응답 형식이 맞지 않아요 · request.steps');
    expect(() => requestOf({ ...REQUEST, execution: 'teleported' })).toThrow('request.execution');
    expect(() => requestOf({ ...REQUEST, personId: 7 })).toThrow('request.personId');
    expect(() => requestOf({ ...REQUEST, id: undefined })).toThrow('request.id');
    expect(() => requestOf({ ...REQUEST, at: 'yesterday' })).toThrow('request.at');
    expect(() => requestOf({ ...REQUEST, tool: 'secret-np_token-value' })).toThrowError(new Error('서버 응답 형식이 맞지 않아요 · request.tool'));
  });

  it('FR12A 신뢰도: 평가 리포트가 없으면 evaluation 키가 없다', () => {
    const t = trustOf({
      live: { since: AT, decisions: 12, doubleOccupancy: 0, latencyMedianSec: null, latencyP95Sec: null, counts: { OK: 8, BUSY: 1, ASK: 2, DENY: 0, NEED_APPROVAL: 1 } }
    });
    expect(t).toEqual({
      live: { since: '14:28', decisions: 12, doubleOccupancy: 0, latencyMedianSec: null, latencyP95Sec: null, counts: { OK: 8, BUSY: 1, ASK: 2, DENY: 0, NEED_APPROVAL: 1 } }
    });
    expect(t).not.toHaveProperty('evaluation');
  });
  it('FR12A 신뢰도: 평가 리포트에 혼동 행렬이 없으면 confusion 키만 없다. 지표의 null은 그대로다', () => {
    const live = { since: AT, decisions: 1, doubleOccupancy: 0, latencyMedianSec: 0.4, latencyP95Sec: 1.2, counts: { OK: 1, BUSY: 0, ASK: 0, DENY: 0, NEED_APPROVAL: 0 } };
    const t = trustOf({
      live,
      evaluation: { date: '2026-10-20', metrics: [{ id: 1, name: '정확도', pass: null, label: '측정 전', sample: '0건' }, { id: 2, name: '재현율', pass: true, label: '통과', sample: '40건', sampleNote: '보정 후' }] }
    });
    expect(t.live.latencyMedianSec).toBe(0.4);
    expect(t.evaluation).toEqual({
      date: '2026-10-20',
      metrics: [{ id: 1, name: '정확도', pass: null, label: '측정 전', sample: '0건' }, { id: 2, name: '재현율', pass: true, label: '통과', sample: '40건', sampleNote: '보정 후' }]
    });
    expect(t.evaluation).not.toHaveProperty('confusion');
  });
  it('FR12A 신뢰도: 평가 날짜는 날짜 문자열 그대로, 혼동 행렬은 그대로 옮긴다', () => {
    const live = { since: AT, decisions: 0, doubleOccupancy: 0, latencyMedianSec: null, latencyP95Sec: null, counts: { OK: 0, BUSY: 0, ASK: 0, DENY: 0, NEED_APPROVAL: 0 } };
    const t = trustOf({ live, evaluation: { date: '2026-10-20', metrics: [], confusion: { labels: ['1', '2', 'none'], values: [[90, 5, 5], [4, 92, 4], [0, 1, 99]] } } });
    expect(t.evaluation?.date).toBe('2026-10-20');
    expect(t.evaluation?.confusion).toEqual({ labels: ['1', '2', 'none'], values: [[90, 5, 5], [4, 92, 4], [0, 1, 99]] });
  });
  it('FR12A 신뢰도: 판정 수 다섯 개가 모두 있어야 한다', () => {
    expect(() => trustOf({ live: { since: AT, decisions: 0, doubleOccupancy: 0, latencyMedianSec: null, latencyP95Sec: null, counts: { OK: 0, BUSY: 0, ASK: 0, DENY: 0 } } })).toThrow('trust.live.counts.NEED_APPROVAL');
  });

  it('FR12A 장소 목록은 그대로 옮긴다', () => {
    const raw = {
      weakRssiDbm: -75,
      places: [{ placeId: 'lounge', placeName: '공용 라운지', modelVersion: 'm-3', calibrationVersion: 'c-7', devices: [{ nodeId: 'locker-1', name: '사물함 1', capability: 'locker', occupancy: 'rental' }] }]
    };
    expect(placesOf(raw)).toEqual(raw);
    expect(() => placesOf({ weakRssiDbm: -75 })).toThrow('places.places');
    expect(() => placesOf({ ...raw, places: [{ ...raw.places[0], devices: [{ nodeId: 'x', name: 'x', capability: 'toaster', occupancy: 'rental' }] }] })).toThrow('capability');
  });
  it('FR12A 점유 목록: 시작 시각은 HH:MM이고 초과 분은 정수로 내린다', () => {
    const rows = usesOf([
      { nodeId: 'locker-1', personId: 'p1', occupancy: 'rental', session: 'ACTIVE', sessionNote: '사용 중', startedAt: AT, overdueMinutes: 3.7 },
      { nodeId: 'light-1', personId: 'p2', occupancy: 'shared', session: 'RESERVED', startedAt: '2026-10-24T05:30:00.000+00:00' }
    ]);
    expect(rows).toEqual([
      { nodeId: 'locker-1', personId: 'p1', occupancy: 'rental', session: 'ACTIVE', sessionNote: '사용 중', startedAt: '14:28', overdueMinutes: 3 },
      { nodeId: 'light-1', personId: 'p2', occupancy: 'shared', session: 'RESERVED', startedAt: '14:30' }
    ]);
    expect(() => usesOf([{ nodeId: 'locker-1', personId: 'p1', occupancy: 'rental', session: 'FAULT', startedAt: AT }])).toThrow('uses[0].session');
  });
  it('FR12A 승인 목록: 신청·유효 시각은 HH:MM, 끝난 승인은 끝난 사유를 가진다', () => {
    const a = approvalsOf({
      pending: [{ personId: 'p1', nodeId: 'locker-2', mode: 'one_time', requestedAt: AT }],
      active: [{ personId: 'p2', nodeId: 'locker-3', mode: 'time_window', remainingMinutes: 12, validUntil: '2026-10-24T06:00:00.000+00:00', uses: 2 }],
      ended: [{ personId: 'p3', nodeId: 'locker-2', endReason: 'APPROVAL_REJECTED' }]
    });
    expect(a).toEqual({
      pending: [{ personId: 'p1', nodeId: 'locker-2', mode: 'one_time', requestedAt: '14:28' }],
      active: [{ personId: 'p2', nodeId: 'locker-3', mode: 'time_window', remainingMinutes: 12, validUntil: '15:00', uses: 2 }],
      ended: [{ personId: 'p3', nodeId: 'locker-2', endReason: 'APPROVAL_REJECTED' }]
    });
    expect(() => approvalsOf({ pending: [], active: [] })).toThrow('approvals.ended');
  });
  it('FR12A 감사 로그와 이력: 감사 행은 HH:MM:SS, 이력은 HH:MM이다', () => {
    expect(auditOf([AUDIT_RAW])).toEqual([AUDIT_ROW]);
    const h = historyOf([{ requestId: 'r1', at: AT, personId: 'p1', target: { kind: 'explicit', nodeId: 'locker-1' }, action: 'open', verdict: 'OK', outcome: '열림', nodeId: 'locker-1', code: 'OK' }]);
    expect(h).toEqual([{ requestId: 'r1', at: '14:28', personId: 'p1', target: { kind: 'explicit', nodeId: 'locker-1' }, action: 'open', verdict: 'OK', outcome: '열림', nodeId: 'locker-1', code: 'OK' }]);
  });
  it('FR12A 대여 한 건: 시작은 HH:MM, 타임라인은 HH:MM:SS, 초과 분은 정수로 내린다', () => {
    const r = rentalOf({
      nodeId: 'locker-1', personId: 'p1', startedAt: AT, overdueMinutes: 2.9,
      timeline: [{ at: AT, text: '열림' }, { at: '2026-10-24T05:31:00.000+00:00', text: '지금', now: true }]
    });
    expect(r).toEqual({ nodeId: 'locker-1', personId: 'p1', startedAt: '14:28', overdueMinutes: 2, timeline: [{ at: '14:28:10', text: '열림' }, { at: '14:31:00', text: '지금', now: true }] });
  });
  it('FR12A 점검: 비콘 마지막 수신 초는 정수로 내리고 null은 그대로다', () => {
    const h = healthOf({
      devices: [{ nodeId: 'locker-1', connected: true, trust: 'trusted' }],
      beacons: [{ personId: 'p1', label: '방문자 A', lastSeenSec: 4.8 }, { personId: 'p2', label: '방문자 B', lastSeenSec: null, note: '수신 없음' }],
      server: { database: true, mqtt: false, model: 'm-3' }
    });
    expect(h.beacons).toEqual([{ personId: 'p1', label: '방문자 A', lastSeenSec: 4 }, { personId: 'p2', label: '방문자 B', lastSeenSec: null, note: '수신 없음' }]);
    expect(h.server).toEqual({ database: true, mqtt: false, model: 'm-3' });
    expect(() => healthOf({ devices: [{ nodeId: 'x', connected: true, trust: 'banned' }], beacons: [], server: { database: true, mqtt: true, model: 'm' } })).toThrow('health.devices[0].trust');
  });
  it('FR12A 응답이 객체나 배열이 아니면 형식 오류다', () => {
    expect(() => placesOf(null)).toThrow('서버 응답 형식이 맞지 않아요 · places');
    expect(() => usesOf({})).toThrow('서버 응답 형식이 맞지 않아요 · uses');
    expect(() => auditOf('[]')).toThrow('서버 응답 형식이 맞지 않아요 · audit');
  });
});

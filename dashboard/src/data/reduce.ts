// FR-12B 이벤트를 차례로 쌓아 모니터 상태를 만든다. 화면은 이 상태만 보고 그린다.
import type { StreamEvent } from '../model/events';
import type { MonitorState, PlaceInfo, RequestProgress } from '../model/types';

/** HH:MM:SS.mmm → HH:MM:SS */
export const clockOf = (at: string) => at.slice(0, 8);
const secOf = (t: string) => { const [h, m, x = '0'] = t.split(':'); return Number(h) * 3600 + Number(m) * 60 + Number(x); };

/** 끝난 요청을 무대와 레일에 남겨 두는 시간 (초). 표시용 값이다. 그 뒤에는 대기 화면으로 돌아간다. */
export const FINISHED_HOLD_SEC = 15;

/** 요청이 끝난 시각. AI에 응답했고, 소유자 응답을 기다리는 승인 신청이 없으면 끝났다. */
export function finishedAt(r: RequestProgress): string | undefined {
  if (r.accessExpiredAt) return r.accessExpiredAt;
  if (r.accessRequestedAt) return undefined;
  return r.respondedAt;
}

/** 연결 직후의 빈 상태: 장소의 기기는 모두 사용 가능, 사람은 비콘 이벤트가 오면 생긴다 */
export function initialState(place: PlaceInfo): MonitorState {
  return {
    connected: true, lastReceivedAt: '', reconnectAttempts: 0,
    devices: place.devices.map((d) => ({ nodeId: d.nodeId, state: 'AVAILABLE', online: true, trust: 'trusted', overdue: false })),
    people: [], ownerNotified: false
  };
}

/** 진행 중인 요청에 딸린 이벤트만 그 요청을 바꾼다 */
function withRequest(s: MonitorState, requestId: string, f: (r: RequestProgress) => RequestProgress): MonitorState {
  return s.request?.id === requestId ? { ...s, request: f(s.request) } : s;
}

export function reduce(s0: MonitorState, e: StreamEvent): MonitorState {
  const s1 = apply(s0, e);
  // 끝나고 일정 시간이 지난 요청은 내린다
  const end = s1.request && finishedAt(s1.request);
  return end && s1.lastReceivedAt && secOf(s1.lastReceivedAt) - secOf(end) > FINISHED_HOLD_SEC ? { ...s1, request: undefined } : s1;
}

function apply(s0: MonitorState, e: StreamEvent): MonitorState {
  const s: MonitorState = { ...s0, lastReceivedAt: clockOf(e.at) };
  switch (e.type) {
    case 'beacon': {
      const known = s.people.some((p) => p.id === e.personId);
      const person = { id: e.personId, pseudonym: e.pseudonym, letter: e.letter, role: e.role, beaconSeen: e.seen };
      return {
        ...s,
        people: known
          ? s.people.map((p) => (p.id === e.personId ? { ...p, ...person, rssi: e.seen ? p.rssi : {} } : p))
          : [...s.people, { ...person, rssi: {} }]
      };
    }
    case 'rssi':
      return { ...s, people: s.people.map((p) => (p.id === e.personId ? { ...p, rssi: e.rssi } : p)) };
    case 'mcp_call':
      return {
        ...s,
        request: {
          id: e.requestId, personId: e.personId, host: e.host, tool: e.tool, target: e.target, action: e.action,
          at: clockOf(e.at), steps: Array.from({ length: 7 }, () => ({ result: 'pending' as const }))
        }
      };
    case 'step':
      return withRequest(s, e.requestId, (r) => ({ ...r, steps: r.steps.map((x, i) => (i === e.step ? { result: e.result, detail: e.detail } : x)) }));
    case 'posterior':
      return withRequest(s, e.requestId, (r) => ({ ...r, posteriors: e.posteriors, threshold: e.threshold }));
    case 'verdict':
      // 판정이 나면 오지 않은 단계는 확인하지 않은 단계다
      return withRequest(s, e.requestId, (r) => ({
        ...r, verdict: e.verdict, reason: e.reason, nodeId: e.nodeId,
        steps: r.steps.map((x) => (x.result === 'pending' ? { result: 'skip' as const } : x))
      }));
    case 'command':
      return withRequest(s, e.requestId, (r) => ({ ...r, execution: 'sent' }));
    case 'exec_result':
      return withRequest(s, e.requestId, (r) => ({ ...r, execution: e.result }));
    case 'mcp_result':
      return withRequest(s, e.requestId, (r) => ({ ...r, respondedAt: clockOf(e.at) }));
    case 'transition':
      return {
        ...s,
        devices: s.devices.map((d) => (d.nodeId !== e.nodeId ? d : {
          ...d, state: e.to,
          overdue: e.to === 'AVAILABLE' ? false : e.overdue ?? d.overdue,
          fault: e.to === 'FAULT' && e.fault ? { ...e.fault, at: clockOf(e.at) } : undefined
        }))
      };
    case 'approval': {
      const t = withRequest(s, e.requestId, (r) => (e.kind === 'requested' ? { ...r, accessRequestedAt: clockOf(e.at) } : { ...r, accessExpiredAt: clockOf(e.at) }));
      return { ...t, ownerNotified: e.kind === 'requested' };
    }
    case 'link':
      return { ...s0, connected: e.connected, reconnectAttempts: e.reconnectAttempts };
    case 'mock_phone': {
      const phones = s.mockPhones ?? [];
      return { ...s0, mockPhones: phones.some((m) => m.personId === e.screen.personId) ? phones.map((m) => (m.personId === e.screen.personId ? e.screen : m)) : [...phones, e.screen] };
    }
    case 'audit':
      // 감사 로그는 패널이 조회로 읽는다. 수신 시각만 바뀐다.
      return s;
  }
}

export const reduceAll = (s: MonitorState, events: StreamEvent[]) => events.reduce(reduce, s);

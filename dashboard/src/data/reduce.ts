// FR-12B 이벤트를 차례로 쌓아 모니터 상태를 만든다. 화면은 이 상태만 보고 그린다.
import type { StreamEvent } from '../model/events';
import type { MonitorState, Person, PlaceInfo, RequestProgress } from '../model/types';

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

/**
 * 무대에서 사람을 다른 기기 앞으로 옮기는 기준: 새 기기의 신호가 지금 기기보다 이만큼(dB) 세고, 그 상태가 이 시간(초) 이어질 때.
 * 표시용 값이라 서버 설정이 아니다. 판정에는 쓰지 않는다 — RSSI가 흔들려도 점이 두 기기 앞을 오가지 않게 한다.
 */
export const NEAR_MARGIN_DB = 4;
export const NEAR_DWELL_SEC = 2;

function nearOf(p: Person, rssi: Record<string, number>, at: string): Pick<Person, 'near' | 'nearCandidate'> {
  const best = Object.entries(rssi).sort((a, b) => b[1] - a[1])[0]?.[0];
  const cur = p.near;
  // 빈 묶음은 지금 기기를 그대로 둔다 (비콘이 끊기면 따로 지운다). 지우면 다음 묶음에서 2초 규칙 없이 다른 기기로 정한다
  if (!best) return { near: cur, nearCandidate: undefined };
  // 처음 받았으면 바로 정한다
  if (!cur) return { near: best, nearCandidate: undefined };
  // 지금 기기의 신호가 한 묶음에서 빠지면 4dB 비교 없이 2초 규칙만 쓴다 (빠짐 하나로 장소가 바뀌지 않게)
  if (rssi[cur] !== undefined && (best === cur || rssi[best] - rssi[cur] < NEAR_MARGIN_DB)) return { near: cur, nearCandidate: undefined };
  const c = p.nearCandidate?.nodeId === best ? p.nearCandidate : { nodeId: best, since: at };
  return secOf(at) - secOf(c.since) >= NEAR_DWELL_SEC ? { near: best, nearCandidate: undefined } : { near: cur, nearCandidate: c };
}

/** 연결 직후의 빈 상태: 모든 장소의 기기는 모두 사용 가능, 사람은 비콘 이벤트가 오면 생긴다 */
export function initialState(places: PlaceInfo[]): MonitorState {
  return {
    connected: true, lastReceivedAt: '', reconnectAttempts: 0,
    devices: places.flatMap((p) => p.devices).map((d) => ({ nodeId: d.nodeId, state: 'AVAILABLE', online: true, trust: 'trusted', overdue: false })),
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
          ? s.people.map((p) => (p.id === e.personId ? { ...p, ...person, ...(e.seen ? {} : { rssi: {}, near: undefined, nearCandidate: undefined }) } : p))
          : [...s.people, { ...person, rssi: {} }]
      };
    }
    case 'rssi':
      return { ...s, people: s.people.map((p) => (p.id === e.personId ? { ...p, rssi: e.rssi, ...nearOf(p, e.rssi, e.at) } : p)) };
    case 'mcp_call':
      return {
        ...s,
        request: {
          id: e.requestId, personId: e.personId, tool: e.tool, target: e.target, action: e.action,
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
      // 승인·거부·만료는 모두 승인 신청의 끝이다. 소유자 알림을 끝내고, 끝난 까닭과 요청을 내리는 시각을 잰다
      const t = withRequest(s, e.requestId, (r) => (e.kind === 'requested' ? { ...r, accessRequestedAt: clockOf(e.at) } : { ...r, accessExpiredAt: clockOf(e.at), accessEnd: e.kind }));
      return { ...t, ownerNotified: e.kind === 'requested' };
    }
    case 'link': {
      // 끊겼다 다시 연결되면 끊기기 전의 진행 중 요청과 소유자 알림을 지운다. 이어서 오는 지금 상태가 아직 진행 중인 것만 다시 세운다
      // (끊긴 동안 끝난 요청과 승인 신청이 남지 않는다). 사람과 기기는 지금 상태가 덮으므로 두고, 처음 연결은 앞 상태가 이미 연결이라 지우지 않는다
      const back = !s0.connected && e.connected;
      return { ...s0, connected: e.connected, reconnectAttempts: e.reconnectAttempts, ...(back ? { request: undefined, ownerNotified: false } : {}) };
    }
    case 'mock_phone': {
      const phones = s.mockPhones ?? [];
      return { ...s0, mockPhones: phones.some((m) => m.personId === e.screen.personId) ? phones.map((m) => (m.personId === e.screen.personId ? e.screen : m)) : [...phones, e.screen] };
    }
    case 'audit':
      // 감사 로그 줄은 패널이 조회로 읽는다. 새 줄이 왔다는 것만 둔다.
      return { ...s, lastAuditId: e.row.id };
  }
}

export const reduceAll = (s: MonitorState, events: StreamEvent[]) => events.reduce(reduce, s);

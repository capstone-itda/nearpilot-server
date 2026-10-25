// 목 이벤트 이야기: 서버가 FR-12B 스트림으로 보낼 이벤트를 시각순으로 적는다.
// 13:21 방문자 C가 사물함 3 대여(14:21:05부터 대여 초과). 14:20 C의 요청 거부, 14:22 B의 BUSY,
// 14:26 A의 ASK, 14:28 A가 사물함 1을 엶, 14:29 B의 사물함 2가 결과 불명으로 FAULT,
// 14:30 B의 조명 1 승인 필요 → 14:31 승인 신청 → 14:41 응답 없이 만료.
// 14:42 C가 라운지로 돌아옴, 14:43 A가 옆방으로 옮겨 무드등을 켬(OK).
// 고정 장면은 그 시각까지의 이벤트를 한꺼번에 보낸 결과다. 조회 데이터(fixtures.ts)의 시각과 같다.
import type { StreamEvent } from '../../model/events';
import type { Action, MockPhoneScreen, MonitorState, Person, ReasonCode, RequestProgress, Verdict } from '../../model/types';
import { initialState, reduceAll } from '../reduce';
import { AUDIT, PLACES } from './fixtures';

/** HH:MM:SS(.mmm) → 초 */
export const sec = (t: string) => { const [h, m, s = '0'] = t.split(':'); return Number(h) * 3600 + Number(m) * 60 + Number(s); };
/** 초 → HH:MM:SS.mmm */
export const stamp = (v: number) => {
  const ms = Math.round(v * 1000);
  const t = Math.floor(ms / 1000);
  const two = (n: number) => String(n).padStart(2, '0');
  return `${two(Math.floor(t / 3600))}:${two(Math.floor(t / 60) % 60)}:${two(t % 60)}.${String(ms % 1000).padStart(3, '0')}`;
};

type Rssi = Record<string, number>;
/** 서 있는 사람은 가장 센 기기가 분명하다 (흔들림으로 바뀌지 않게) */
const RSSI_IDLE_A: Rssi = { 'locker-1': -74, 'locker-2': -75, 'locker-3': -77, 'speaker-1': -62, 'light-1': -71, 'light-2': -73, 'light-3': -76 };
const RSSI_ASK_A: Rssi = { 'locker-1': -61, 'locker-2': -63, 'locker-3': -75, 'speaker-1': -82, 'light-1': -84, 'light-2': -86, 'light-3': -88 };
const RSSI_FLOW_A: Rssi = { 'locker-1': -52, 'locker-2': -67, 'locker-3': -74, 'speaker-1': -77, 'light-1': -83, 'light-2': -84, 'light-3': -86 };
const RSSI_IDLE_B: Rssi = { 'locker-1': -77, 'locker-2': -76, 'locker-3': -66, 'speaker-1': -83, 'light-1': -77, 'light-2': -78, 'light-3': -72 };
const RSSI_FAULT_B: Rssi = { 'locker-1': -74, 'locker-2': -58, 'locker-3': -70, 'speaker-1': -83, 'light-1': -77, 'light-2': -77, 'light-3': -84 };
const RSSI_APPROVAL_B: Rssi = { 'locker-1': -72, 'locker-2': -75, 'locker-3': -77, 'speaker-1': -70, 'light-1': -57, 'light-2': -71, 'light-3': -83 };
/** 옆방으로 옮긴 A: 라운지 신호는 사라지고 무드등만 잡힌다 */
const RSSI_SIDE_A: Rssi = { 'mood-1': -49 };
/** 라운지로 돌아온 C: 대여 중인 사물함 3 앞 */
const RSSI_C: Rssi = { 'locker-1': -78, 'locker-2': -74, 'locker-3': -60, 'speaker-1': -80, 'light-1': -79, 'light-2': -82, 'light-3': -84 };

const PEOPLE: (Pick<Person, 'pseudonym' | 'letter' | 'role'> & { id: string })[] = [
  { id: 'p-a', pseudonym: '방문자 A', letter: 'A', role: 'visitor' },
  { id: 'p-b', pseudonym: '방문자 B', letter: 'B', role: 'visitor' },
  { id: 'p-c', pseudonym: '방문자 C', letter: 'C', role: 'visitor' },
  { id: 'p-owner', pseudonym: '소유자', role: 'owner' }
];

/**
 * 사람별 비콘이 잡히는 구간과 신호 세기 기준점. 사이 시각은 선형으로 잇는다.
 * 기준점 사이는 같은 기기들의 값이어야 한다. A는 라운지를 떠나 비콘이 끊긴 사이에 신호 기기가 옆방으로 바뀐다.
 */
const TRACKS: Record<string, { windows: [string, string][]; keys: [string, Rssi][] }> = {
  'p-a': {
    windows: [['14:05:00', '14:43:40'], ['14:43:44', '99:00:00']],
    keys: [['14:05:00', RSSI_IDLE_A], ['14:25:50', RSSI_IDLE_A], ['14:26:10', RSSI_ASK_A], ['14:27:45', RSSI_ASK_A], ['14:28:05', RSSI_FLOW_A],
      ['14:43:40', RSSI_FLOW_A], ['14:43:44', RSSI_SIDE_A]]
  },
  'p-b': {
    windows: [['14:05:00', '14:23:00'], ['14:28:30', '99:00:00']],
    keys: [['14:05:00', RSSI_IDLE_B], ['14:28:30', RSSI_IDLE_B], ['14:29:30', RSSI_FAULT_B], ['14:29:50', RSSI_FAULT_B], ['14:30:50', RSSI_APPROVAL_B]]
  },
  'p-c': { windows: [['14:42:30', '99:00:00']], keys: [['14:42:30', RSSI_C]] }
};

/** 같은 입력에 같은 값을 주는 ±1 dB 흔들림 (0.5초마다 바뀜) */
function jitter(personId: string, nodeId: string, t: number): number {
  let h = Math.floor(t * 2) * 2654435761;
  for (const c of personId + nodeId) h = Math.imul(h ^ c.charCodeAt(0), 2246822519);
  h ^= h >>> 13;
  return (((h >>> 0) % 1000) / 1000) * 2 - 1;
}

const seenAt = (personId: string, t: number) => !!TRACKS[personId]?.windows.some(([a, b]) => sec(a) <= t && t < sec(b));

/** 시각 t의 RSSI 이벤트 (비콘이 잡히는 사람만) */
export function rssiEvents(t: number): StreamEvent[] {
  return Object.entries(TRACKS).filter(([id]) => seenAt(id, t)).map(([personId, tr]) => {
    let a = tr.keys[0], b = tr.keys[0];
    for (let i = 0; i < tr.keys.length; i++) if (sec(tr.keys[i][0]) <= t) { a = tr.keys[i]; b = tr.keys[i + 1] ?? tr.keys[i]; }
    const span = sec(b[0]) - sec(a[0]);
    const k = span > 0 ? Math.min(1, (t - sec(a[0])) / span) : 0;
    const rssi = Object.fromEntries(Object.keys(a[1]).map((id) => [id, Math.round(a[1][id] + (b[1][id] - a[1][id]) * k + jitter(personId, id, t))]));
    return { type: 'rssi' as const, at: stamp(t), personId, rssi };
  });
}

const beacon = (t: number, id: string, seen: boolean): StreamEvent => {
  const p = PEOPLE.find((x) => x.id === id)!;
  return { type: 'beacon', at: stamp(t), personId: id, pseudonym: p.pseudonym, letter: p.letter, role: p.role, seen };
};
const phone = (t: number, screen: MockPhoneScreen): StreamEvent => ({ type: 'mock_phone', at: stamp(t), screen });

interface RequestScript {
  id: string; at: string; personId: string; target: RequestProgress['target']; action: Action;
  /** 단계 ⓪부터 차례로. 마지막이 stop이면 거기서 멈춘다 */
  steps: [('pass' | 'stop'), string?][];
  posterior?: { at: number; posteriors: Record<string, number>; threshold: number };
  verdict: Verdict; reason: ReasonCode; nodeId?: string;
  /** OK의 실행: 결과 시각과 결과 */
  exec?: { at: string; result: 'succeeded' | 'failed' | 'unknown' };
  said: string; reply: string;
}

/** 요청 하나의 이벤트. 실제 판정처럼 0.2초 안에 단계와 판정이 지나간다. */
function requestEvents(r: RequestScript): StreamEvent[] {
  const t0 = sec(r.at);
  const out: StreamEvent[] = [
    phone(t0 - 0.6, { personId: r.personId, scene: 'chat', said: r.said, reply: '서버 응답을 기다리는 중…' }),
    { type: 'mcp_call', at: stamp(t0), requestId: r.id, personId: r.personId, tool: 'request_use', target: r.target, action: r.action }
  ];
  r.steps.forEach(([result, detail], i) => {
    if (r.posterior && r.posterior.at === i) out.push({ type: 'posterior', at: stamp(t0 + 0.02 * i + 0.01), requestId: r.id, posteriors: r.posterior.posteriors, threshold: r.posterior.threshold });
    out.push({ type: 'step', at: stamp(t0 + 0.02 * (i + 1)), requestId: r.id, step: i, result, detail });
  });
  const tv = t0 + 0.2;
  out.push({ type: 'verdict', at: stamp(tv), requestId: r.id, verdict: r.verdict, reason: r.reason, nodeId: r.nodeId });
  let tr = tv + 0.02;
  if (r.verdict === 'OK' && r.nodeId && r.exec) {
    // 판정과 예약은 한 트랜잭션이고, 실행 결과와 전이도 한 번에 온다 (같은 시각이라 같은 묶음)
    out.push({ type: 'transition', at: stamp(tv), nodeId: r.nodeId, to: 'RESERVED' });
    out.push({ type: 'command', at: stamp(tv + 0.01), requestId: r.id, nodeId: r.nodeId, action: r.action });
    const te = sec(r.exec.at);
    out.push({ type: 'exec_result', at: stamp(te), requestId: r.id, nodeId: r.nodeId, result: r.exec.result });
    out.push(r.exec.result === 'succeeded'
      ? { type: 'transition', at: stamp(te), nodeId: r.nodeId, to: 'ACTIVE' }
      : { type: 'transition', at: stamp(te), nodeId: r.nodeId, to: 'FAULT', fault: { cause: r.exec.result === 'failed' ? 'EXEC_FAILED' : 'EXEC_UNKNOWN', action: r.action } });
    tr = te + 0.02;
  }
  out.push({ type: 'mcp_result', at: stamp(tr), requestId: r.id });
  out.push(phone(tr + 0.4, { personId: r.personId, scene: 'chat', said: r.said, reply: r.reply }));
  return out;
}

const LOCKER = { kind: 'nearest' as const, capability: 'locker' as const };
const ALL_PASS = (details: string[]): RequestScript['steps'] => details.map((d) => ['pass', d]);

const REQUEST_SCRIPTS: RequestScript[] = [
  { id: 'r-1420', at: '14:20:41', personId: 'p-c', target: { kind: 'nearest', capability: 'speaker' }, action: 'on',
    steps: [['pass', '새 요청'], ['stop']], verdict: 'DENY', reason: 'BEACON_NOT_SEEN', said: '스피커 켜 줘', reply: '폰 신호가 잡히지 않아 켤 수 없어요.' },
  { id: 'r-1422', at: '14:22:05', personId: 'p-b', target: LOCKER, action: 'open',
    steps: [...ALL_PASS(['새 요청', '비콘 최근 수신', '사물함 3대', 'P(locker-3) 0.96 ≥ 0.95 (설정값)', '열림 정책']), ['stop']],
    posterior: { at: 3, posteriors: { 'locker-3': 0.96, 'locker-2': 0.03, 'locker-1': 0.01 }, threshold: 0.95 },
    verdict: 'BUSY', reason: 'DEVICE_BUSY', nodeId: 'locker-3', said: '내 앞 사물함 열어 줘', reply: '사물함 3은 다른 사람이 쓰고 있어요.' },
  { id: 'r-1426', at: '14:26:12', personId: 'p-a', target: LOCKER, action: 'open',
    steps: [['pass', '새 요청'], ['pass', '비콘 최근 수신'], ['pass', '사물함 3대'], ['stop']],
    posterior: { at: 3, posteriors: { 'locker-1': 0.58, 'locker-2': 0.31, 'locker-3': 0.02 }, threshold: 0.95 },
    verdict: 'ASK', reason: 'LOW_CONFIDENCE', said: '사물함 열어 줘', reply: '어느 사물함 앞인지 확실하지 않아요. 사물함 1과 2 중 어느 쪽인가요?' },
  { id: 'r-1428', at: '14:28:10', personId: 'p-a', target: LOCKER, action: 'open',
    steps: ALL_PASS(['새 요청', '비콘 최근 수신', '사물함 3대', 'P(locker-1) 0.97 ≥ 0.95 (설정값)', '열림 정책', '비어 있음', '다른 점유 없음']),
    posterior: { at: 3, posteriors: { 'locker-1': 0.97, 'locker-2': 0.02, 'locker-3': 0.01 }, threshold: 0.95 },
    verdict: 'OK', reason: 'OK', nodeId: 'locker-1', exec: { at: '14:28:11', result: 'succeeded' }, said: '내 앞 사물함 열어 줘', reply: '사물함 1을 열었어요.' },
  { id: 'r-1429', at: '14:29:37', personId: 'p-b', target: LOCKER, action: 'open',
    steps: ALL_PASS(['새 요청', '비콘 최근 수신', '사물함 3대', 'P(locker-2) 0.96 ≥ 0.95 (설정값)', '열림 정책', '비어 있음', '다른 점유 없음']),
    posterior: { at: 3, posteriors: { 'locker-2': 0.96, 'locker-3': 0.03, 'locker-1': 0.01 }, threshold: 0.95 },
    verdict: 'OK', reason: 'OK', nodeId: 'locker-2', exec: { at: '14:29:40', result: 'unknown' }, said: '내 앞 사물함 열어 줘',
    reply: '사물함 2가 열렸는지 확인되지 않아 사용을 멈췄어요. 소유자에게 알려 주세요.' },
  { id: 'r-1430', at: '14:30:58', personId: 'p-b', target: { kind: 'nearest', capability: 'light' }, action: 'on',
    steps: [...ALL_PASS(['새 요청', '비콘 최근 수신', '조명 3대', 'P(light-1) 0.91 ≥ 0.75 (설정값)']), ['stop']],
    posterior: { at: 3, posteriors: { 'light-1': 0.91, 'light-2': 0.06, 'light-3': 0.03 }, threshold: 0.75 },
    verdict: 'NEED_APPROVAL', reason: 'APPROVAL_REQUIRED', nodeId: 'light-1', said: '조명 켜 줘', reply: '조명 1은 소유자 승인이 필요해요. 승인을 신청할게요.' },
  // 옆방의 장소는 따로라 후보도 그 장소의 기기다
  { id: 'r-1443', at: '14:43:48', personId: 'p-a', target: { kind: 'nearest', capability: 'light' }, action: 'on',
    steps: ALL_PASS(['새 요청', '비콘 최근 수신', '조명 1대', 'P(mood-1) 0.98 ≥ 0.75 (설정값)', '켜기 정책']),
    posterior: { at: 3, posteriors: { 'mood-1': 0.98 }, threshold: 0.75 },
    verdict: 'OK', reason: 'OK', nodeId: 'mood-1', exec: { at: '14:43:49', result: 'succeeded' }, said: '조명 켜 줘', reply: '무드등을 켰어요.' }
];

/** 승인 신청과 만료 (대기 10분은 목 값, 실제 값은 설정 approval_pending_ttl_sec) */
const APPROVAL_EVENTS: StreamEvent[] = [
  // 소유자 폰에 알림이 뜬 화면이 승인 신청 이벤트와 같은 묶음에 오게 먼저 둔다
  phone(sec('14:30:59.9'), { personId: 'p-owner', scene: 'notice', notice: { title: '조명 켜기 승인 요청', body: '방문자 B가 조명 1을 켜려고 해요.', clock: '14:31' } }),
  { type: 'approval', at: '14:31:00.000', requestId: 'r-1430', nodeId: 'light-1', personId: 'p-b', kind: 'requested' },
  phone(sec('14:31:00.2'), { personId: 'p-b', scene: 'chat', said: '조명 켜 줘', reply: '조명 1은 소유자 승인이 필요해요. 승인을 요청했어요.' }),
  { type: 'approval', at: '14:41:00.000', requestId: 'r-1430', nodeId: 'light-1', personId: 'p-b', kind: 'expired' },
  phone(sec('14:41:00.1'), { personId: 'p-owner', scene: 'idle' }),
  phone(sec('14:41:00.2'), { personId: 'p-b', scene: 'chat', said: '조명 켜 줘', reply: '소유자가 답하지 않아 승인 요청이 만료됐어요.' })
];

const START = sec('14:05:00');

/** 이야기의 사건 이벤트 (RSSI 제외). 시각순 */
export const STORY: StreamEvent[] = (() => {
  const t = START;
  const out: StreamEvent[] = [
    // 연결 직후 서버가 지금 상태를 다시 보낸다
    ...PEOPLE.map((p) => beacon(t, p.id, seenAt(p.id, t))),
    ...PEOPLE.map((p) => phone(t, { personId: p.id, scene: 'idle' })),
    { type: 'transition', at: stamp(t), nodeId: 'locker-3', to: 'ACTIVE' },
    { type: 'transition', at: '14:21:05.000', nodeId: 'locker-3', to: 'ACTIVE', overdue: true },
    // 비콘이 잡히고 끊기는 시각
    ...Object.entries(TRACKS).flatMap(([id, tr]) => tr.windows.flatMap(([a, b]) => [
      ...(sec(a) > t ? [beacon(sec(a), id, true)] : []),
      ...(sec(b) < sec('99:00:00') ? [beacon(sec(b), id, false), phone(sec(b) + 0.1, { personId: id, scene: 'idle' })] : [])
    ])),
    ...AUDIT.map((row): StreamEvent => ({ type: 'audit', at: `${row.at}.250`, row })),
    ...REQUEST_SCRIPTS.flatMap(requestEvents),
    ...APPROVAL_EVENTS
  ];
  // 같은 시각이면 적은 순서를 지킨다
  return out.map((e, i) => [e, i] as const).sort((a, b) => sec(a[0].at) - sec(b[0].at) || a[1] - b[1]).map(([e]) => e);
})();

/** (from, to] 구간의 사건 이벤트 */
export const storyBetween = (from: number, to: number) => STORY.filter((e) => sec(e.at) > from && sec(e.at) <= to);
/** 연결 직후: 시각 t까지의 사건과 그 시각의 RSSI */
export const storyUntil = (t: number): StreamEvent[] => [...STORY.filter((e) => sec(e.at) <= t), ...rssiEvents(t)];

/** 고정 장면의 시각 */
export const SCENE_TIME = {
  /** MON-01 대기 */
  idle: '14:05:00',
  /** CMN-02 연결 끊김 (마지막 수신 14:25:40) */
  offline: '14:25:40',
  /** MON-04 ASK: A가 사물함 1과 2 사이 */
  ask: '14:26:13',
  /** MON-01·02 판정 중: A의 요청이 도착해 ① 비콘까지 통과 */
  judging: '14:28:10.01',
  /** MON-01·02·03 요청 진행: A의 사물함 1 열기 명령을 보내고 결과를 기다림 */
  flow: '14:28:10.5',
  /** 요청 진행의 끝: 사물함 1이 열리고 AI에 결과를 돌려줌 */
  done: '14:28:12',
  /** MON-01·05 고장 격리: B의 사물함 2 결과 불명 */
  fault: '14:29:41',
  /** MON-01·04 승인 대기: B의 조명 1, 소유자에게 알림 */
  approval: '14:31:02',
  /** 패널 화면의 바탕: 승인 신청이 만료되고 조용해진 뒤 */
  later: '14:41:30',
  /** 옆방: A가 옮겨 간 옆방에서 무드등 켜기 명령을 보내고 결과를 기다림. C는 라운지에 있다 */
  'side-room': '14:43:48.5'
} as const;
export type SceneName = keyof typeof SCENE_TIME;
/** 재생 순서 (시각순) */
export const PLAY_ORDER: SceneName[] = ['idle', 'ask', 'flow', 'done', 'fault', 'approval', 'side-room'];

/** 그 장면 시각까지 이벤트를 쌓은 모니터 상태 (테스트와 고정 장면) */
export const stateAt = (t: number): MonitorState => reduceAll(initialState(PLACES), storyUntil(t));
export const SCENES = Object.fromEntries(Object.entries(SCENE_TIME).map(([k, v]) => [k, () => {
  const s = stateAt(sec(v));
  return k === 'offline' ? { ...s, connected: false, reconnectAttempts: 3 } : s;
}])) as Record<SceneName, () => MonitorState>;

/** 판정이 끝난 요청 하나 (조회 REST의 판정 단계별 결과) */
export function finishedRequest(id: string): RequestProgress | undefined {
  let found: RequestProgress | undefined;
  let s = initialState(PLACES);
  for (const e of STORY) {
    s = reduceAll(s, [e]);
    if (s.request?.id === id) found = s.request;
  }
  return found;
}
/** 요청이 처음 이야기에 나타나는 시각 */
export const requestStart = (id: string) => REQUEST_SCRIPTS.find((r) => r.id === id)?.at;

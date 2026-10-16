// 목 데이터: 승인한 화면 설계 목업의 예시 시각표.
// 13:21 방문자 C가 사물함 3 대여(14:21:05부터 대여 초과). 14:12 방문자 A가 조명 1 시간 승인(15:12까지).
// 14:26 A의 ASK, 14:28 A가 사물함 1, 14:29 B의 사물함 2가 결과 불명으로 FAULT, 14:30 B의 조명 1 승인 필요.
import type {
  Approvals, AuditRow, DeviceStatus, FloorLayout, Health, HistoryRow, Person,
  RentalInfo, RequestProgress, Snapshot, TrustReport, UseRow
} from '../../model/types';

/** 목업 평면도 좌표(SVG)를 m로 바꾼다. 방 8.4 m × 5.4 m = SVG 1010 × 650, 원점 (330, 90). */
const m = (x: number, y: number) => ({ x: ((x - 330) * 8.4) / 1010, y: ((y - 90) * 5.4) / 650 });

export const LAYOUT: FloorLayout = {
  placeName: '공용 라운지',
  widthM: 8.4,
  depthM: 5.4,
  modelVersion: 'v3',
  calibrationVersion: 'cal-2',
  weakRssiDbm: -80,
  // NFR-10 시연 장비: 사물함 3, LED 조명 3, 스피커 1
  devices: [
    { nodeId: 'locker-1', name: '사물함 1', capability: 'locker', occupancy: 'rental', body: m(450, 112), front: m(450, 260), wall: 'top' },
    { nodeId: 'locker-2', name: '사물함 2', capability: 'locker', occupancy: 'rental', body: m(610, 112), front: m(610, 260), wall: 'top' },
    { nodeId: 'locker-3', name: '사물함 3', capability: 'locker', occupancy: 'rental', body: m(770, 112), front: m(770, 260), wall: 'top' },
    { nodeId: 'speaker-1', name: '스피커', capability: 'speaker', occupancy: 'shared', body: m(430, 719), front: m(430, 560), wall: 'bottom' },
    { nodeId: 'light-1', name: '조명 1', capability: 'light', occupancy: 'shared', body: m(555, 719), front: m(555, 560), wall: 'bottom' },
    { nodeId: 'light-2', name: '조명 2', capability: 'light', occupancy: 'shared', body: m(680, 719), front: m(680, 560), wall: 'bottom' },
    { nodeId: 'light-3', name: '조명 3', capability: 'light', occupancy: 'shared', body: m(805, 719), front: m(805, 560), wall: 'bottom' }
  ]
};

const dev = (over: Partial<Record<string, Partial<DeviceStatus>>> = {}): DeviceStatus[] =>
  LAYOUT.devices.map((d) => ({ nodeId: d.nodeId, state: 'AVAILABLE', online: true, trust: 'trusted', overdue: false, ...over[d.nodeId] }));

const RSSI_IDLE_A = { 'locker-1': -69, 'locker-2': -69, 'locker-3': -71, 'speaker-1': -70, 'light-1': -70, 'light-2': -70, 'light-3': -72 };
const RSSI_IDLE_B = { 'locker-1': -79, 'locker-2': -76, 'locker-3': -73, 'speaker-1': -81, 'light-1': -79, 'light-2': -78, 'light-3': -76 };

type PersonOver = Partial<Pick<Person, 'position' | 'beaconSeen' | 'rssi'>>;
const people = (a: PersonOver = {}, b: PersonOver = {}): Person[] => [
  { id: 'p-a', pseudonym: '방문자 A', letter: 'A', role: 'visitor', beaconSeen: true, position: m(1040, 420), rssi: RSSI_IDLE_A, ...a },
  { id: 'p-b', pseudonym: '방문자 B', letter: 'B', role: 'visitor', beaconSeen: true, position: m(1150, 300), rssi: RSSI_IDLE_B, ...b },
  { id: 'p-c', pseudonym: '방문자 C', letter: 'C', role: 'visitor', beaconSeen: false, rssi: {} },
  { id: 'p-owner', pseudonym: '소유자', role: 'owner', beaconSeen: false, rssi: {} }
];

const ALL_PASS = (details: string[]): RequestProgress['steps'] => details.map((detail) => ({ result: 'pass', detail }));

const base = (): Snapshot => ({
  connected: true, lastReceivedAt: '14:31:02', reconnectAttempts: 0,
  devices: dev(), people: people(), ownerNotified: false
});

export const SCENES = {
  /** MON-01 대기 */
  idle: (): Snapshot => ({
    ...base(), lastReceivedAt: '14:05:00',
    devices: dev({ 'locker-3': { state: 'ACTIVE' } }),
    mockPhone: { personId: 'p-a', scene: 'idle' }
  }),
  /** MON-04 ASK: A가 사물함 1과 2 사이 */
  ask: (): Snapshot => ({
    ...base(), lastReceivedAt: '14:26:12',
    devices: dev({ 'locker-3': { state: 'ACTIVE', overdue: true } }),
    people: people({ position: m(530, 345), rssi: { 'locker-1': -61, 'locker-2': -63, 'locker-3': -75, 'speaker-1': -82, 'light-1': -84, 'light-2': -86, 'light-3': -88 } }, { position: undefined, beaconSeen: false }),
    request: {
      id: 'r-1426', personId: 'p-a', host: 'claude', tool: 'request_use', target: { kind: 'nearest', capability: 'locker' }, action: 'open',
      at: '14:26:12', verdict: 'ASK', reason: 'LOW_CONFIDENCE',
      steps: [{ result: 'pass', detail: '새 요청' }, { result: 'pass', detail: '비콘 최근 수신' }, { result: 'pass', detail: '사물함 3대' }, { result: 'stop' }, { result: 'skip' }, { result: 'skip' }, { result: 'skip' }],
      posteriors: { 'locker-1': 0.58, 'locker-2': 0.31, 'locker-3': 0.02 }, threshold: 0.95
    },
    mockPhone: { personId: 'p-a', scene: 'chat', said: '사물함 열어 줘', reply: '어느 사물함 앞인지 확실하지 않아요. 열고 싶은 사물함 앞으로 조금 더 가까이 가 주세요.' }
  }),
  /** MON-01·02·03 요청 진행: A의 사물함 1 열기 */
  flow: (): Snapshot => ({
    ...base(), lastReceivedAt: '14:28:10',
    devices: dev({ 'locker-1': { state: 'RESERVED' }, 'locker-3': { state: 'ACTIVE', overdue: true } }),
    people: people({ position: m(450, 260), rssi: { 'locker-1': -52, 'locker-2': -67, 'locker-3': -74, 'speaker-1': -79, 'light-1': -81, 'light-2': -84, 'light-3': -86 } }, { position: undefined, beaconSeen: false }),
    request: {
      id: 'r-1428', personId: 'p-a', host: 'claude', tool: 'request_use', target: { kind: 'nearest', capability: 'locker' }, action: 'open',
      at: '14:28:10', verdict: 'OK', reason: 'OK', nodeId: 'locker-1', execution: 'sent',
      steps: ALL_PASS(['새 요청', '비콘 최근 수신', '사물함 3대', 'P(locker-1) 0.97 ≥ 0.95 (설정값)', '열림 정책', '비어 있음', '다른 점유 없음']),
      posteriors: { 'locker-1': 0.97, 'locker-2': 0.02, 'locker-3': 0.01 }, threshold: 0.95
    },
    mockPhone: { personId: 'p-a', scene: 'chat', said: '내 앞 사물함 열어 줘', reply: '서버 응답을 기다리는 중…' }
  }),
  /** MON-01·05 고장 격리: B의 사물함 2 결과 불명 */
  fault: (): Snapshot => ({
    ...base(), lastReceivedAt: '14:29:40',
    devices: dev({ 'locker-1': { state: 'ACTIVE' }, 'locker-2': { state: 'FAULT', fault: { cause: 'EXEC_UNKNOWN', action: 'open', at: '14:29:40' } }, 'locker-3': { state: 'ACTIVE', overdue: true } }),
    people: people({ position: undefined, beaconSeen: false }, { position: m(610, 260) }),
    request: {
      id: 'r-1429', personId: 'p-b', host: 'gemini', tool: 'request_use', target: { kind: 'nearest', capability: 'locker' }, action: 'open',
      at: '14:29:37', verdict: 'OK', reason: 'OK', nodeId: 'locker-2', execution: 'unknown',
      steps: ALL_PASS(['새 요청', '비콘 최근 수신', '사물함 3대', 'P(locker-2) 0.96 ≥ 0.95 (설정값)', '열림 정책', '비어 있음', '다른 점유 없음'])
    },
    mockPhone: { personId: 'p-b', scene: 'chat', said: '내 앞 사물함 열어 줘', reply: '사물함 2가 열렸는지 확인되지 않아 사용을 멈췄어요. 직원에게 알려 주세요.' }
  }),
  /** MON-01·04 승인 대기: B의 조명 1 켜기 */
  approval: (): Snapshot => ({
    ...base(), lastReceivedAt: '14:31:02',
    devices: dev({ 'locker-1': { state: 'ACTIVE' }, 'locker-2': { state: 'FAULT', fault: { cause: 'EXEC_UNKNOWN', action: 'open', at: '14:29:40' } }, 'locker-3': { state: 'ACTIVE', overdue: true } }),
    people: people({}, { position: m(555, 560) }),
    request: {
      id: 'r-1430', personId: 'p-b', host: 'chatgpt', tool: 'request_use', target: { kind: 'nearest', capability: 'light' }, action: 'on',
      at: '14:30:58', verdict: 'NEED_APPROVAL', reason: 'APPROVAL_REQUIRED', nodeId: 'light-1', accessRequestedAt: '14:31:00',
      steps: [{ result: 'pass', detail: '새 요청' }, { result: 'pass', detail: '비콘 최근 수신' }, { result: 'pass', detail: '조명 3대' }, { result: 'pass', detail: 'P(light-1) 0.91 ≥ 0.75 (설정값)' }, { result: 'stop' }, { result: 'skip' }, { result: 'skip' }]
    },
    ownerNotified: true,
    mockPhone: { personId: 'p-owner', scene: 'notice', notice: { title: '조명 켜기 승인 요청', body: '방문자 B가 조명 1을 켜려고 해요.', clock: '14:31' } }
  }),
  /** 패널 화면의 바탕: 요청이 끝난 뒤 */
  later: (): Snapshot => ({
    ...base(),
    devices: dev({ 'locker-1': { state: 'ACTIVE' }, 'locker-2': { state: 'FAULT', fault: { cause: 'EXEC_UNKNOWN', action: 'open', at: '14:29:40' } }, 'locker-3': { state: 'ACTIVE', overdue: true } }),
    mockPhone: { personId: 'p-a', scene: 'idle' }
  }),
  /** CMN-02 연결 끊김 */
  offline: (): Snapshot => ({
    ...base(), connected: false, lastReceivedAt: '14:25:40', reconnectAttempts: 3,
    devices: dev({ 'locker-3': { state: 'ACTIVE', overdue: true } }),
    mockPhone: { personId: 'p-a', scene: 'idle' }
  })
};
export type SceneName = keyof typeof SCENES;
/** 재생 순서 (시각순) */
export const PLAY_ORDER: SceneName[] = ['idle', 'ask', 'flow', 'fault', 'approval', 'later'];

/** 이력에서 요청을 누르면 여는 판정 상세 */
export const REQUESTS: Record<string, () => RequestProgress> = {
  'r-1426': () => SCENES.ask().request!,
  'r-1428': () => ({ ...SCENES.flow().request!, execution: 'succeeded' }),
  'r-1429': () => SCENES.fault().request!,
  'r-1430': () => SCENES.approval().request!,
  'r-1422': () => ({
    id: 'r-1422', personId: 'p-b', host: 'gemini', tool: 'request_use', target: { kind: 'nearest', capability: 'locker' }, action: 'open',
    at: '14:22:05', verdict: 'BUSY', reason: 'DEVICE_BUSY', nodeId: 'locker-3',
    steps: [...ALL_PASS(['새 요청', '비콘 최근 수신', '사물함 3대', 'P(locker-3) 0.96 ≥ 0.95 (설정값)', '열림 정책']), { result: 'stop' }, { result: 'skip' }]
  }),
  'r-1420': () => ({
    id: 'r-1420', personId: 'p-c', host: 'claude', tool: 'request_use', target: { kind: 'nearest', capability: 'speaker' }, action: 'on',
    at: '14:20:41', verdict: 'DENY', reason: 'BEACON_NOT_SEEN',
    steps: [{ result: 'pass', detail: '새 요청' }, { result: 'stop' }, { result: 'skip' }, { result: 'skip' }, { result: 'skip' }, { result: 'skip' }, { result: 'skip' }]
  })
};
export const USES: UseRow[] = [
  { nodeId: 'locker-3', personId: 'p-c', occupancy: 'rental', session: 'ACTIVE', startedAt: '13:21', overdueMinutes: 9 },
  { nodeId: 'locker-1', personId: 'p-a', occupancy: 'rental', session: 'ACTIVE', startedAt: '14:28' },
  { nodeId: 'locker-2', personId: 'p-b', occupancy: 'rental', session: 'RESERVED', sessionNote: '실행 미확인 · 고장으로 보존', startedAt: '14:29' }
];

export const APPROVALS: Approvals = {
  pending: [{ nodeId: 'light-1', personId: 'p-b', requestedAt: '14:31', remainingMinutes: 9 }],
  active: [{ nodeId: 'light-1', personId: 'p-a', mode: 'time_window', validUntil: '15:12', uses: 3 }],
  ended: [
    { nodeId: 'light-1', personId: 'p-c', endReason: 'APPROVAL_REJECTED' },
    { nodeId: 'light-2', personId: 'p-c', endReason: 'APPROVAL_REQUEST_EXPIRED' },
    { nodeId: 'locker-1', personId: 'p-a', mode: 'one_time', endReason: 'APPROVAL_EXHAUSTED' }
  ]
};

export const AUDIT: AuditRow[] = [
  { id: 'a8', at: '14:30:58', step: '4', nodeId: 'light-1', text: '승인 필요', code: 'APPROVAL_REQUIRED' },
  { id: 'a7', at: '14:30:58', step: '3', nodeId: 'light-1', text: '근접 통과 0.91', code: 'OK' },
  { id: 'a6', at: '14:29:40', step: 'fault', nodeId: 'locker-2', text: '결과 불명으로 격리', code: 'EXEC_UNKNOWN' },
  { id: 'a5', at: '14:28:11', step: 'exec', nodeId: 'locker-1', text: '열림 완료', code: 'OK' },
  { id: 'a4', at: '14:26:12', step: '3', text: '어느 사물함 앞인지 확신 부족', code: 'LOW_CONFIDENCE' },
  { id: 'a3', at: '14:22:05', step: '5', nodeId: 'locker-3', text: '다른 사람이 점유 중', code: 'DEVICE_BUSY' },
  { id: 'a2', at: '14:20:41', step: '1', text: '비콘 수신 없음', code: 'BEACON_NOT_SEEN' },
  { id: 'a1', at: '14:20:03', step: 'auth', text: '토큰 확인 실패', code: 'INVALID_TOKEN' }
];

export const HISTORY: HistoryRow[] = [
  { requestId: 'r-1430', at: '14:30', personId: 'p-b', host: 'chatgpt', target: { kind: 'nearest', capability: 'light' }, action: 'on', verdict: 'NEED_APPROVAL', nodeId: 'light-1', outcome: '실행 안 함 · 승인 신청', code: 'APPROVAL_REQUIRED' },
  { requestId: 'r-1429', at: '14:29', personId: 'p-b', host: 'gemini', target: { kind: 'nearest', capability: 'locker' }, action: 'open', verdict: 'OK', nodeId: 'locker-2', outcome: '결과 불명 → 고장 격리', code: 'EXEC_UNKNOWN' },
  { requestId: 'r-1428', at: '14:28', personId: 'p-a', host: 'claude', target: { kind: 'nearest', capability: 'locker' }, action: 'open', verdict: 'OK', nodeId: 'locker-1', outcome: '열림' },
  { requestId: 'r-1426', at: '14:26', personId: 'p-a', host: 'claude', target: { kind: 'nearest', capability: 'locker' }, action: 'open', verdict: 'ASK', outcome: '다시 질문', code: 'LOW_CONFIDENCE' },
  { requestId: 'r-1422', at: '14:22', personId: 'p-b', host: 'gemini', target: { kind: 'nearest', capability: 'locker' }, action: 'open', verdict: 'BUSY', nodeId: 'locker-3', outcome: '실행 안 함', code: 'DEVICE_BUSY' },
  { requestId: 'r-1420', at: '14:20', personId: 'p-c', host: 'claude', target: { kind: 'nearest', capability: 'speaker' }, action: 'on', verdict: 'DENY', outcome: '실행 안 함', code: 'BEACON_NOT_SEEN' }
];

export const TRUST: TrustReport = {
  live: { since: '14:00', decisions: 42, doubleOccupancy: 0, latencyMedianSec: 0.21, latencyP95Sec: 0.48, counts: { OK: 27, ASK: 6, NEED_APPROVAL: 4, BUSY: 2, DENY: 3 } },
  evaluation: {
    date: '2026-10-10',
    metrics: [
      { id: 1, name: '기기 선택', pass: true, label: '통과', sample: '100' },
      { id: 2, name: '재질문', pass: null, label: '기준 미결', sample: '—' },
      { id: 3, name: '점유·중복', pass: true, label: '통과', sample: '2,100', sampleNote: '부하 시험 동시 수 미결' },
      { id: 4, name: '다중 사용자', pass: true, label: '통과', sample: '150' },
      { id: 5, name: '정책·발견·차단', pass: true, label: '통과', sample: '180' },
      { id: 6, name: '종료·복구', pass: false, label: '✕ 미통과', sample: '120' },
      { id: 7, name: '지연·호환성', pass: true, label: '통과', sample: '250' },
      { id: 8, name: '근접 모델', pass: null, label: '기준 일부 미결', sample: '5회차' },
      { id: 9, name: '전역 할당', pass: null, label: '미측정', sample: '—' }
    ],
    confusion: (() => {
      const labels = ['1', '2', '3', '스', '조1', '조2', '조3', '없음'];
      const d = [96, 96, 97, 95, 96, 95, 97, 95];
      return { labels, values: d.map((v, i) => labels.map((_, j) => (i === j ? v : j === (i + 1) % 8 ? 100 - v : 0))) };
    })()
  }
};

export const RENTALS: Record<string, RentalInfo> = {
  'locker-3': { nodeId: 'locker-3', personId: 'p-c', startedAt: '13:21', overdueMinutes: 9, timeline: [
    { at: '13:21:04', text: '예약 · RESERVED' }, { at: '13:21:05', text: '열림 · ACTIVE' }, { at: '14:21', text: '대여 시간 초과', now: true }
  ] },
  'locker-1': { nodeId: 'locker-1', personId: 'p-a', startedAt: '14:28', overdueMinutes: 0, timeline: [
    { at: '14:28:10', text: '예약 · RESERVED' }, { at: '14:28:11', text: '열림 · ACTIVE', now: true }
  ] }
};

export const HEALTH: Health = {
  devices: LAYOUT.devices.map((d) => ({ nodeId: d.nodeId, connected: true, trust: 'trusted' })),
  beacons: [
    { personId: 'p-a', label: 'minor 101', lastSeenSec: 1 },
    { personId: 'p-b', label: 'minor 102', lastSeenSec: 1, note: '스피커 쪽 신호 약함' },
    { personId: 'p-owner', label: '—', lastSeenSec: null, note: '비콘 없음 · 승인만 함' }
  ],
  hosts: [{ host: 'claude', lastToolsList: '14:02' }, { host: 'chatgpt', lastToolsList: '14:03' }, { host: 'gemini', lastToolsList: '14:03' }],
  server: { database: true, mqtt: true, model: 'v3 · cal-2' }
};

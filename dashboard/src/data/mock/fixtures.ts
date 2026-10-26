// 목 조회 데이터. 스트림 이벤트 이야기는 story.ts에 있고, 여기 시각과 같다.
// 13:21 방문자 C가 사물함 3 대여(14:21:05부터 대여 초과). 14:12 방문자 A가 조명 1 시간 승인(15:12까지).
// 14:26 A의 ASK, 14:28 A가 사물함 1, 14:29 B의 사물함 2가 결과 불명으로 FAULT, 14:30 B의 조명 1 승인 필요.
// 14:43 A가 옆방으로 옮겨 무드등을 켬 (승인 없이 OK).
import { resolveArrangement, withArrangement } from '../../view/arrange';
import type {
  ApprovalRow, Approvals, AuditRow, FloorLayout, Health, HistoryRow, PlaceInfo, RentalInfo, TrustReport, UseRow
} from '../../model/types';

/** 배치 화면에서 약한 신호로 표시할 기준 (dBm). 응답 전체에 하나다. */
export const WEAK_RSSI_DBM = -80;

export const LOUNGE: PlaceInfo = {
  placeId: 'lounge',
  placeName: '공용 라운지',
  modelVersion: 'v3',
  calibrationVersion: 'cal-2',
  // NFR-10 시연 장비: 사물함 3, LED 조명 3, 스피커 1
  devices: [
    { nodeId: 'locker-1', name: '사물함 1', capability: 'locker', occupancy: 'rental' },
    { nodeId: 'locker-2', name: '사물함 2', capability: 'locker', occupancy: 'rental' },
    { nodeId: 'locker-3', name: '사물함 3', capability: 'locker', occupancy: 'rental' },
    { nodeId: 'speaker-1', name: '스피커', capability: 'speaker', occupancy: 'shared' },
    { nodeId: 'light-1', name: '조명 1', capability: 'light', occupancy: 'shared' },
    { nodeId: 'light-2', name: '조명 2', capability: 'light', occupancy: 'shared' },
    { nodeId: 'light-3', name: '조명 3', capability: 'light', occupancy: 'shared' }
  ]
};
/** NFR-10 시연의 두 번째 장소: 무드등 하나. 장소마다 근접 모델과 보정이 따로라 버전이 다르다. */
export const SIDE_ROOM: PlaceInfo = {
  placeId: 'side-room',
  placeName: '옆방',
  modelVersion: 'v2',
  calibrationVersion: 'cal-1',
  devices: [{ nodeId: 'mood-1', name: '무드등', capability: 'light', occupancy: 'shared' }]
};
export const PLACES: PlaceInfo[] = [LOUNGE, SIDE_ROOM];
/** 라운지에 기본 표시 배치를 입힌 것 (테스트용) */
export const LAYOUT: FloorLayout = withArrangement(LOUNGE, resolveArrangement(LOUNGE.devices), WEAK_RSSI_DBM);

/** 목 전용: 이 시각부터 조회 결과에 나타난다. 조회는 마지막으로 보낸 이벤트 시각까지만 보여 준다. */
export type Since<T> = T & { since: string };
/** 대여 시간이 끝나는 시각 (목 전용, 실제 값은 설정 rental_duration_sec) */
export const RENTAL_DUE: Record<string, string> = { 'locker-3': '14:21:05' };

export const USES: Since<UseRow>[] = [
  { nodeId: 'locker-3', personId: 'p-c', occupancy: 'rental', session: 'ACTIVE', startedAt: '13:21', since: '13:21:04' },
  { nodeId: 'locker-1', personId: 'p-a', occupancy: 'rental', session: 'ACTIVE', startedAt: '14:28', since: '14:28:10' },
  { nodeId: 'locker-2', personId: 'p-b', occupancy: 'rental', session: 'RESERVED', sessionNote: '실행 미확인 · 고장으로 보존', startedAt: '14:29', since: '14:29:37' }
];

/** 승인 신청 대기 시간 (분). 목 전용 값이고 실제 값은 설정에서 받는다. */
export const APPROVAL_WAIT_MIN = 10;

export const APPROVALS: { [K in keyof Approvals]: Since<ApprovalRow>[] } = {
  pending: [{ nodeId: 'light-1', personId: 'p-b', requestedAt: '14:31', remainingMinutes: 9, since: '14:31:00' }],
  active: [{ nodeId: 'light-1', personId: 'p-a', mode: 'time_window', validUntil: '15:02', uses: 1, since: '14:02:00' }],
  ended: [
    { nodeId: 'light-1', personId: 'p-c', endReason: 'APPROVAL_REJECTED', since: '13:40:00' },
    { nodeId: 'light-2', personId: 'p-c', endReason: 'APPROVAL_REQUEST_EXPIRED', since: '13:52:00' },
    { nodeId: 'light-2', personId: 'p-a', mode: 'one_time', endReason: 'APPROVAL_EXHAUSTED', since: '13:58:00' }
  ]
};

export const AUDIT: AuditRow[] = [
  { id: 'a9', at: '14:43:49', step: 'exec', nodeId: 'mood-1', text: '제어 신호 출력 완료', code: 'OK' },
  { id: 'a8', at: '14:30:58', step: '4', nodeId: 'light-1', text: '승인 필요', code: 'APPROVAL_REQUIRED' },
  { id: 'a7', at: '14:30:58', step: '3', nodeId: 'light-1', text: '근접 통과 0.91', code: 'OK' },
  { id: 'a6', at: '14:29:40', step: 'fault', nodeId: 'locker-2', text: '결과 불명으로 격리', code: 'EXEC_UNKNOWN' },
  { id: 'a5', at: '14:28:11', step: 'exec', nodeId: 'locker-1', text: '제어 신호 출력 완료', code: 'OK' },
  { id: 'a4', at: '14:26:12', step: '3', text: '어느 사물함 앞인지 확신 부족', code: 'LOW_CONFIDENCE' },
  { id: 'a3', at: '14:22:05', step: '5', nodeId: 'locker-3', text: '다른 사람이 점유 중', code: 'DEVICE_BUSY' },
  { id: 'a2', at: '14:20:41', step: '1', text: '비콘 수신 없음', code: 'BEACON_NOT_SEEN' },
  { id: 'a1', at: '14:20:03', step: 'auth', text: '토큰 확인 실패', code: 'INVALID_TOKEN' }
];

export const HISTORY: Since<HistoryRow>[] = [
  { requestId: 'r-1443', at: '14:43', personId: 'p-a', target: { kind: 'nearest', capability: 'light' }, action: 'on', verdict: 'OK', nodeId: 'mood-1', outcome: '제어 신호 출력 완료', since: '14:43:49' },
  { requestId: 'r-1430', at: '14:30', personId: 'p-b', target: { kind: 'nearest', capability: 'light' }, action: 'on', verdict: 'NEED_APPROVAL', nodeId: 'light-1', outcome: '실행 안 함', code: 'APPROVAL_REQUIRED', since: '14:30:58' },
  { requestId: 'r-1429', at: '14:29', personId: 'p-b', target: { kind: 'nearest', capability: 'locker' }, action: 'open', verdict: 'OK', nodeId: 'locker-2', outcome: '결과 불명 → 고장 격리', code: 'EXEC_UNKNOWN', since: '14:29:40' },
  { requestId: 'r-1428', at: '14:28', personId: 'p-a', target: { kind: 'nearest', capability: 'locker' }, action: 'open', verdict: 'OK', nodeId: 'locker-1', outcome: '제어 신호 출력 완료', since: '14:28:11' },
  { requestId: 'r-1426', at: '14:26', personId: 'p-a', target: { kind: 'nearest', capability: 'locker' }, action: 'open', verdict: 'ASK', outcome: '다시 질문', code: 'LOW_CONFIDENCE', since: '14:26:12' },
  { requestId: 'r-1422', at: '14:22', personId: 'p-b', target: { kind: 'nearest', capability: 'locker' }, action: 'open', verdict: 'BUSY', nodeId: 'locker-3', outcome: '실행 안 함', code: 'DEVICE_BUSY', since: '14:22:05' },
  { requestId: 'r-1420', at: '14:20', personId: 'p-c', target: { kind: 'nearest', capability: 'speaker' }, action: 'on', verdict: 'DENY', outcome: '실행 안 함', code: 'BEACON_NOT_SEEN', since: '14:20:41' },
  { requestId: 'r-1403', at: '14:03', personId: 'p-a', target: { kind: 'nearest', capability: 'light' }, action: 'on', verdict: 'OK', nodeId: 'light-1', outcome: '제어 신호 출력 완료 · 시간 승인 사용', since: '14:03:00' },
  { requestId: 'r-1401', at: '14:01', personId: 'p-a', target: { kind: 'nearest', capability: 'light' }, action: 'on', verdict: 'NEED_APPROVAL', nodeId: 'light-1', outcome: '실행 안 함 · 시간 승인됨', code: 'APPROVAL_REQUIRED', since: '14:01:00' },
  { requestId: 'r-1358', at: '13:58', personId: 'p-a', target: { kind: 'nearest', capability: 'light' }, action: 'on', verdict: 'OK', nodeId: 'light-2', outcome: '제어 신호 출력 완료 · 1회 승인 소진', since: '13:58:00' },
  { requestId: 'r-1347', at: '13:47', personId: 'p-a', target: { kind: 'nearest', capability: 'light' }, action: 'on', verdict: 'NEED_APPROVAL', nodeId: 'light-2', outcome: '실행 안 함 · 1회 승인됨', code: 'APPROVAL_REQUIRED', since: '13:47:00' },
  { requestId: 'r-1342', at: '13:42', personId: 'p-c', target: { kind: 'nearest', capability: 'light' }, action: 'on', verdict: 'NEED_APPROVAL', nodeId: 'light-2', outcome: '실행 안 함 · 승인 신청 만료', code: 'APPROVAL_REQUIRED', since: '13:42:00' },
  { requestId: 'r-1339', at: '13:39', personId: 'p-c', target: { kind: 'nearest', capability: 'light' }, action: 'on', verdict: 'NEED_APPROVAL', nodeId: 'light-1', outcome: '실행 안 함 · 소유자 거절', code: 'APPROVAL_REQUIRED', since: '13:39:00' },
  { requestId: 'r-1321', at: '13:21', personId: 'p-c', target: { kind: 'nearest', capability: 'locker' }, action: 'open', verdict: 'OK', nodeId: 'locker-3', outcome: '제어 신호 출력 완료 · 대여 시작', since: '13:21:05' }
];

/** 실시간 집계의 바탕. 목 어댑터가 지금까지 보인 HISTORY를 센다. 이력 "오늘"과 같은 수가 된다. */
export const TRUST_BASE: TrustReport['live'] = { since: '14:00', decisions: 0, doubleOccupancy: 0, latencyMedianSec: 0.21, latencyP95Sec: 0.48, counts: { OK: 0, ASK: 0, NEED_APPROVAL: 0, BUSY: 0, DENY: 0 } };

export const TRUST: TrustReport = {
  live: TRUST_BASE,
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
  'locker-3': { nodeId: 'locker-3', personId: 'p-c', startedAt: '13:21', overdueMinutes: 0, timeline: [
    { at: '13:21:04', text: '예약 · RESERVED' }, { at: '13:21:05', text: '제어 신호 출력 완료 · ACTIVE' }, { at: '14:21:05', text: '대여 시간 초과' }
  ] },
  'locker-2': { nodeId: 'locker-2', personId: 'p-b', startedAt: '14:29', overdueMinutes: 0, timeline: [
    { at: '14:29:37', text: '예약 · RESERVED' }, { at: '14:29:40', text: '결과 불명 · 고장 격리' }
  ] },
  'locker-1': { nodeId: 'locker-1', personId: 'p-a', startedAt: '14:28', overdueMinutes: 0, timeline: [
    { at: '14:28:10', text: '예약 · RESERVED' }, { at: '14:28:11', text: '제어 신호 출력 완료 · ACTIVE' }
  ] }
};

export const HEALTH: Health = {
  devices: PLACES.flatMap((p) => p.devices).map((d) => ({ nodeId: d.nodeId, connected: true, trust: 'trusted' })),
  beacons: [
    { personId: 'p-a', label: 'minor 101', lastSeenSec: 1 },
    { personId: 'p-b', label: 'minor 102', lastSeenSec: 1, note: '스피커 쪽 신호 약함' },
    { personId: 'p-c', label: 'minor 103', lastSeenSec: 1 },
    { personId: 'p-owner', label: '—', lastSeenSec: null, note: '비콘 없음 · 승인만 함' }
  ],
  server: { database: true, mqtt: true, model: PLACES.map((p) => `${p.modelVersion} · ${p.calibrationVersion}`).join(' / ') }
};

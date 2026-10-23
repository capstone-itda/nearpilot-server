// 화면이 쓰는 값. 이름과 값은 server/src/nearpilot/shared/enums.py를 따른다.
// 서버 메시지 형식은 architecture.md §5에 있다 (FR-12A, FR-12B). 스트림 이벤트는 events.ts, 조회 결과는 아래 형태로 어댑터가 바꿔 넘긴다.

export type Verdict = 'OK' | 'BUSY' | 'ASK' | 'DENY' | 'NEED_APPROVAL';
export type DeviceState = 'AVAILABLE' | 'RESERVED' | 'ACTIVE' | 'CLOSING' | 'FAULT';
export type Capability = 'locker' | 'light' | 'speaker';
export type Occupancy = 'rental' | 'shared';
export type Action = 'open' | 'on' | 'off';
export type TargetKind = 'nearest' | 'explicit' | 'any';
export type TrustState = 'discovered' | 'trusted' | 'revoked';
export type AuditStep = 'auth' | '0' | '1' | '2' | '3' | '4' | '5' | '6' | 'exec' | 'close' | 'fault' | 'announce' | 'admin';
export type ApprovalMode = 'one_time' | 'time_window';
export type ReasonCode =
  | 'OK' | 'INVALID_TOKEN' | 'INVALID_INPUT' | 'REQUEST_ID_CONFLICT' | 'CAPACITY_EXCEEDED'
  | 'BEACON_NOT_SEEN' | 'NO_CANDIDATE' | 'TARGET_UNAVAILABLE' | 'LOW_CONFIDENCE' | 'MOVE_CLOSER'
  | 'APPROVAL_REQUIRED' | 'POLICY_DENIED' | 'APPROVAL_PENDING' | 'APPROVAL_REJECTED' | 'APPROVAL_EXPIRED'
  | 'APPROVAL_REQUEST_EXPIRED' | 'APPROVAL_EXHAUSTED' | 'ACTION_NOT_ALLOWED' | 'DEVICE_BUSY' | 'USER_BUSY'
  | 'NOT_ASSIGNED' | 'USE_NOT_FOUND' | 'RECHECK_FAILED' | 'INTERNAL_ERROR' | 'COMMAND_IN_PROGRESS'
  | 'EXEC_FAILED' | 'EXEC_UNKNOWN' | 'RESERVATION_EXPIRED' | 'NODE_NOT_CONNECTED' | 'COMMAND_NOT_SENT';

/** 조회 REST의 기기 한 대 (현재 상태의 기기 목록) */
export interface DeviceInfo {
  nodeId: string;
  name: string;
  capability: Capability;
  occupancy: Occupancy;
}

/** 조회 REST의 장소 하나. 좌표는 없다. */
export interface PlaceInfo {
  placeId: string;
  placeName: string;
  modelVersion: string;
  calibrationVersion: string;
  devices: DeviceInfo[];
}

/** 조회 REST의 장소 목록 (GET /dashboard/places). 약한 신호 기준은 응답 전체에 하나다. */
export interface PlacesInfo {
  /** 배치 화면에서 약한 신호로 표시할 기준 (dBm). 설정에서 받는다. 판정에는 쓰지 않는다. */
  weakRssiDbm: number;
  places: PlaceInfo[];
}

/** 무대 위 표시 자리 (SVG 단위). 실제 위치가 아니고 이 PC에서 정한다. */
export interface Spot { x: number; y: number }

export interface LayoutDevice extends DeviceInfo { spot: Spot }
/** 장소 정보에 이 PC의 표시 배치를 입힌 것 */
export interface FloorLayout extends Omit<PlaceInfo, 'devices'> { weakRssiDbm: number; devices: LayoutDevice[] }

export interface DeviceStatus {
  nodeId: string;
  state: DeviceState;
  online: boolean;
  trust: TrustState;
  /** 대여형 점유의 대여 시간 초과 여부 (FR-20) */
  overdue: boolean;
  /** FAULT의 원인과 결과를 모르는 명령 (현재 상태 조회) */
  fault?: { cause: ReasonCode; action?: Action; at: string };
}

export interface Person {
  id: string;
  /** 화면에 보이는 가명 (D10) */
  pseudonym: string;
  /** 무대 표시 글자 (A, B, …). 소유자는 없다. */
  letter?: string;
  role: 'visitor' | 'owner';
  /** 비콘을 최근에 받았는지 */
  beaconSeen: boolean;
  /** 기기별 비콘 신호 세기 (dBm) */
  rssi: Record<string, number>;
  /** 무대에서 이 사람을 세우는 기기. 신호가 흔들려도 바로 바꾸지 않는다 (reduce.ts의 NEAR_MARGIN_DB, NEAR_DWELL_SEC) */
  near?: string;
  /** near보다 신호가 센 기기와 그렇게 된 시각 */
  nearCandidate?: { nodeId: string; since: string };
}

/** 판정 단계 ⓪~⑥ 하나의 결과. pending은 아직 이벤트가 오지 않은 단계 */
export interface StepOutcome {
  result: 'pending' | 'pass' | 'stop' | 'skip';
  detail?: string;
}

export interface RequestProgress {
  id: string;
  personId: string;
  tool: 'request_use';
  target: { kind: TargetKind; capability?: Capability; nodeId?: string };
  action: Action;
  /** 요청 시각 (HH:MM:SS) */
  at: string;
  /** 판정. 판정 이벤트가 오기 전에는 없다 */
  verdict?: Verdict;
  reason?: ReasonCode;
  /** ⓪~⑥ 일곱 칸. 오지 않은 단계는 pending, 판정 뒤 멈춘 단계 뒤는 skip */
  steps: StepOutcome[];
  /** 판정이 고른 기기 */
  nodeId?: string;
  /** 후보별 사후확률 (FR-12B) */
  posteriors?: Record<string, number>;
  /** 이 요청에 적용한 근접 임계값 (설정값) */
  threshold?: number;
  /** 실행: 보냄(결과 대기), 완료, 결과 불명 */
  execution?: 'sent' | 'succeeded' | 'failed' | 'unknown';
  /** NEED_APPROVAL 뒤 AI가 request_access로 신청한 시각 */
  accessRequestedAt?: string;
  /** 승인 신청이 소유자 응답 없이 만료된 시각 */
  accessExpiredAt?: string;
  /** 서버가 AI 호스트에 결과를 돌려준 시각 */
  respondedAt?: string;
}

/** 판정이 난 요청 (조회 REST의 판정 단계별 결과) */
export type DecidedRequest = RequestProgress & { verdict: Verdict; reason: ReasonCode };
export const isDecided = (r: RequestProgress): r is DecidedRequest => r.verdict !== undefined;

/** 폰 화면을 흉내 낸 목 데이터. 실제 화면은 미러링 영상이다. */
export interface MockPhoneScreen {
  personId: string;
  scene: 'idle' | 'chat' | 'notice';
  said?: string;
  reply?: string;
  notice?: { title: string; body: string; clock: string };
}

/** 대시보드가 받은 이벤트를 차례로 쌓아 만든 모니터 상태 (FR-12B). data/reduce.ts가 만든다. */
export interface MonitorState {
  connected: boolean;
  /** 마지막 이벤트의 서버 시각 (HH:MM:SS) */
  lastReceivedAt: string;
  reconnectAttempts: number;
  devices: DeviceStatus[];
  people: Person[];
  /** 마지막으로 받은 감사 로그 줄의 ID. 감사 패널이 다시 조회할 때를 안다 */
  lastAuditId?: string;
  /** 진행 중이거나 막 끝난 요청. 끝나고 FINISHED_HOLD_SEC(data/reduce.ts)가 지나면 내린다 */
  request?: RequestProgress;
  /** 소유자 폰에 승인 알림을 보낸 중이면 true */
  ownerNotified: boolean;
  /** 목 어댑터 전용: 사람별 가짜 폰 화면 (mock_phone 이벤트). 서버 어댑터는 비워 두고 미러링 영상을 쓴다. */
  mockPhones?: MockPhoneScreen[];
}

// ── 조회 결과 (FR-12A 대시보드 조회 REST) ────────────────────────────────

export interface UseRow {
  nodeId: string;
  personId: string;
  occupancy: Occupancy;
  session: 'RESERVED' | 'ACTIVE' | 'CLOSING';
  sessionNote?: string;
  startedAt: string;
  overdueMinutes?: number;
}

export interface ApprovalRow {
  nodeId: string;
  personId: string;
  mode?: ApprovalMode;
  requestedAt?: string;
  remainingMinutes?: number;
  validUntil?: string;
  uses?: number;
  endReason?: 'APPROVAL_REJECTED' | 'APPROVAL_REQUEST_EXPIRED' | 'APPROVAL_EXHAUSTED' | 'APPROVAL_EXPIRED';
}
export interface Approvals { pending: ApprovalRow[]; active: ApprovalRow[]; ended: ApprovalRow[] }

export interface AuditRow {
  id: string;
  at: string;
  step: AuditStep;
  nodeId?: string;
  text: string;
  code: ReasonCode;
}

export interface HistoryRow {
  requestId: string;
  at: string;
  personId: string;
  target: { kind: TargetKind; capability?: Capability; nodeId?: string };
  action: Action;
  verdict: Verdict;
  nodeId?: string;
  outcome: string;
  code?: ReasonCode;
}
export interface HistoryQuery { period: string; verdict?: Verdict; nodeId?: string }

export interface TrustReport {
  live: { since: string; decisions: number; doubleOccupancy: number; latencyMedianSec: number | null; latencyP95Sec: number | null; counts: Record<Verdict, number> };
  evaluation: {
    date: string;
    metrics: { id: number; name: string; pass: boolean | null; label: string; sample: string; sampleNote?: string }[];
    /** 혼동 행렬 (%) 행 = 실제, 열 = 고름. 마지막 칸은 none */
    confusion: { labels: string[]; values: number[][] };
  };
}

export interface RentalInfo { nodeId: string; personId: string; startedAt: string; overdueMinutes: number; timeline: { at: string; text: string; now?: boolean }[] }

export interface Health {
  devices: { nodeId: string; connected: boolean; trust: TrustState }[];
  beacons: { personId: string; label: string; lastSeenSec: number | null; note?: string }[];
  server: { database: boolean; mqtt: boolean; model: string };
}

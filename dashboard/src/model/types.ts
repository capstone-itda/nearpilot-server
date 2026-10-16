// 데이터 소스가 화면에 주는 값. 이름과 값은 server/src/nearpilot/shared/enums.py를 따른다.
// 서버 메시지 형식은 미결이다 (FR-12A, FR-12B). 서버 어댑터가 이 형태로 바꿔 넘긴다.

export type Verdict = 'OK' | 'BUSY' | 'ASK' | 'DENY' | 'NEED_APPROVAL';
export type DeviceState = 'AVAILABLE' | 'RESERVED' | 'ACTIVE' | 'CLOSING' | 'FAULT';
export type Capability = 'locker' | 'light' | 'speaker';
export type Occupancy = 'rental' | 'shared';
export type Action = 'open' | 'on' | 'off';
export type TargetKind = 'nearest' | 'explicit' | 'any';
export type HostKind = 'claude' | 'chatgpt' | 'gemini';
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

/** 평면 위의 점. 단위는 m, 원점은 방의 왼쪽 위 모서리. */
export interface Point { x: number; y: number }

/** 근접 모델 파일의 배치 (D4: 편집하지 않는다) */
export interface FloorLayout {
  placeName: string;
  widthM: number;
  depthM: number;
  modelVersion: string;
  calibrationVersion: string;
  /** 배치·신호 점검에서 약한 신호로 표시할 기준 (dBm). 설정에서 받는다. 판정에는 쓰지 않는다. */
  weakRssiDbm: number;
  devices: LayoutDevice[];
}
export interface LayoutDevice {
  nodeId: string;
  name: string;
  capability: Capability;
  occupancy: Occupancy;
  /** 본체 위치 */
  body: Point;
  /** 기기 앞 위치 (근접 판정의 기준점) */
  front: Point;
  /** 벽: 위쪽 벽 기기는 이름표가 아래, 아래쪽 벽 기기는 위 */
  wall: 'top' | 'bottom';
}

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
  /** 추정 위치. 없으면 무대에 그리지 않는다. */
  position?: Point;
  /** 기기별 비콘 신호 세기 (dBm) */
  rssi: Record<string, number>;
}

/** 판정 단계 ⓪~⑥ 하나의 결과 */
export interface StepOutcome {
  result: 'pass' | 'stop' | 'skip';
  detail?: string;
}

export interface RequestProgress {
  id: string;
  personId: string;
  host: HostKind;
  tool: 'request_use';
  target: { kind: TargetKind; capability?: Capability; nodeId?: string };
  action: Action;
  /** 요청 시각 (HH:MM:SS) */
  at: string;
  verdict: Verdict;
  reason: ReasonCode;
  /** ⓪~⑥ 일곱 칸. 멈춘 단계 뒤는 skip */
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
}

/** 폰 화면을 흉내 낸 목 데이터. 실제 화면은 미러링 영상이다. */
export interface MockPhoneScreen {
  personId: string;
  scene: 'idle' | 'chat' | 'notice';
  said?: string;
  reply?: string;
  notice?: { title: string; body: string; clock: string };
}

/** 모니터링 스트림이 유지하는 장소 상태 (FR-12B) */
export interface Snapshot {
  connected: boolean;
  /** 마지막 수신 시각 (HH:MM:SS) */
  lastReceivedAt: string;
  reconnectAttempts: number;
  devices: DeviceStatus[];
  people: Person[];
  request?: RequestProgress;
  /** 소유자 폰에 승인 알림을 보낸 중이면 true */
  ownerNotified: boolean;
  mockPhone?: MockPhoneScreen;
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
  host: HostKind;
  target: { kind: TargetKind; capability?: Capability; nodeId?: string };
  action: Action;
  verdict: Verdict;
  nodeId?: string;
  outcome: string;
  code?: ReasonCode;
}
export interface HistoryQuery { period: string; verdict?: Verdict; nodeId?: string; host?: HostKind }

export interface TrustReport {
  live: { since: string; decisions: number; doubleOccupancy: number; latencyMedianSec: number; latencyP95Sec: number; counts: Record<Verdict, number> };
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
  hosts: { host: HostKind; lastToolsList?: string }[];
  server: { database: boolean; mqtt: boolean; model: string };
}

// 서버 값(architecture.md §5 "대시보드 조회", "WebSocket")을 화면 모델(model/types.ts, model/events.ts)로 옮긴다.
// 서버가 형식 표의 키만 보내므로 표의 키만 읽는다. 형식이 표와 맞지 않으면 이벤트는 그 이벤트만 버리고, 조회는 오류로 알린다.
import type { StreamEvent } from '../../model/events';
import type {
  Action, Approvals, AuditRow, AuditStep, Capability, DeviceState, Health, HistoryRow, Occupancy, PlacesInfo, Person,
  ReasonCode, RentalInfo, RequestProgress, StepOutcome, TargetKind, TrustReport, TrustState, UseRow, Verdict
} from '../../model/types';
import { localTime, type TimeFormat } from './time';

/** 서버 값이 형식 표와 맞지 않다. 메시지에는 경로만 쓴다 — 값에 사용자 정보가 있을 수 있다 */
class BadFormat extends Error {}

/** 값을 검사해서 화면 모델의 값으로 바꾼다. 어긋나면 BadFormat(경로)을 던진다 */
type Check<T> = (v: unknown, path: string) => T;
type Rec = Record<string, unknown>;

const bad = (path: string): never => { throw new BadFormat(path); };
const isRec = (v: unknown): v is Rec => typeof v === 'object' && v !== null && !Array.isArray(v);

const str: Check<string> = (v, p) => (typeof v === 'string' ? v : bad(p));
const num: Check<number> = (v, p) => (typeof v === 'number' && Number.isFinite(v) ? v : bad(p));
const bool: Check<boolean> = (v, p) => (typeof v === 'boolean' ? v : bad(p));
const lit = <T extends string>(...allowed: readonly T[]): Check<T> => (v, p) => (typeof v === 'string' && (allowed as readonly string[]).includes(v) ? (v as T) : bad(p));
const nul = <T,>(item: Check<T>): Check<T | null> => (v, p) => (v === null ? null : item(v, p));
const arr = <T,>(item: Check<T>, size?: number): Check<T[]> => (v, p) => (Array.isArray(v) && (size === undefined || v.length === size) ? v.map((x, i) => item(x, `${p}[${i}]`)) : bad(p));
/** 기기 ID → 값 */
const rec = <T,>(item: Check<T>): Check<Record<string, T>> => (v, p) => (isRec(v) ? Object.fromEntries(Object.entries(v).map(([k, x]) => [k, item(x, p)])) : bad(p));
/** 서버 시각 → 이 PC의 지역 시각 (time.ts의 필드별 형식) */
const time = (format: TimeFormat): Check<string> => (v, p) => localTime(v, format) ?? bad(p);
/** 분·초는 정수로 내린다. 서버는 흐른 시간을 소수로 주고 화면은 정수 분·초를 쓴다 (목 데이터와 같다) */
const whole: Check<number> = (v, p) => Math.floor(num(v, p));

/**
 * 객체. required의 키는 꼭 있어야 하고, optional의 키는 없으면 뺀다 (값이 undefined로 바뀌어도 뺀다).
 * 표에 없는 키는 옮기지 않는다.
 */
function obj<T, O = {}>(required: { [K in keyof T]: Check<T[K]> }, optional?: { [K in keyof O]: Check<O[K] | undefined> }): Check<T & Partial<O>> {
  return (v, p) => {
    if (!isRec(v)) return bad(p);
    const out: Rec = {};
    for (const [k, check] of Object.entries(required) as [string, Check<unknown>][]) {
      if (v[k] === undefined) bad(`${p}.${k}`);
      out[k] = check(v[k], `${p}.${k}`);
    }
    for (const [k, check] of Object.entries(optional ?? {}) as [string, Check<unknown>][]) {
      if (v[k] === undefined || v[k] === null) continue;
      const x = check(v[k], `${p}.${k}`);
      if (x !== undefined) out[k] = x;
    }
    return out as T & Partial<O>;
  };
}

// ── 값 종류 ──────────────────────────────────────────────────────────

const VERDICT = lit<Verdict>('OK', 'BUSY', 'ASK', 'DENY', 'NEED_APPROVAL');
const DEVICE_STATE = lit<DeviceState>('AVAILABLE', 'RESERVED', 'ACTIVE', 'CLOSING', 'FAULT');
const ACTION = lit<Action>('open', 'on', 'off');
const CAPABILITY = lit<Capability>('locker', 'light', 'speaker');
const OCCUPANCY = lit<Occupancy>('rental', 'shared');
const TRUST_STATE = lit<TrustState>('discovered', 'trusted', 'revoked');
/** 서버가 shared/enums.py의 값만 보낸다. 화면이 모르는 코드는 코드 그대로 보인다 */
const reason = str as Check<ReasonCode>;
const auditStep = str as Check<AuditStep>;
/** 판정 단계 ⓪~⑥: 서버는 "0"~"6" 문자열로 보내고 화면은 칸 번호로 쓴다 */
const stepNo: Check<number> = (v, p) => Number(lit('0', '1', '2', '3', '4', '5', '6')(v, p));

const TARGET: Check<RequestProgress['target']> = obj({ kind: lit<TargetKind>('nearest', 'explicit', 'any') }, { capability: CAPABILITY, nodeId: str });
const AUDIT_ROW: Check<AuditRow> = obj({ id: str, at: time('HH:MM:SS'), step: auditStep, text: str, code: reason }, { nodeId: str });

// ── 모니터링 스트림 이벤트 (architecture.md §5 "WebSocket" 형식 표) ──────────────

type Kind<K extends StreamEvent['type']> = Extract<StreamEvent, { type: K }>;

/** 이벤트 한 종류. type을 붙이고, at을 HH:MM:SS.mmm으로 바꾸고, 표의 키를 옮긴다 */
function event<K extends StreamEvent['type']>(type: K, body: Check<Omit<Kind<K>, 'type' | 'at'>>): Check<Kind<K>> {
  return (v, p) => {
    const fields = body(v, p);
    return { type, at: time('HH:MM:SS.mmm')((v as Rec).at, `${p}.at`), ...fields } as unknown as Kind<K>;
  };
}

const EVENTS = {
  beacon: event('beacon', obj({ personId: str, pseudonym: str, role: lit<Person['role']>('visitor', 'owner'), seen: bool }, { letter: str })),
  rssi: event('rssi', obj({ personId: str, rssi: rec(num) })),
  // 대시보드는 방문자의 request_use 흐름만 그린다. 서버가 다른 도구의 호출을 보내거나 대상·동작이 없으면 이 이벤트는 버린다
  mcp_call: event('mcp_call', obj({ requestId: str, personId: str, tool: lit<'request_use'>('request_use'), target: TARGET, action: ACTION })),
  step: event('step', obj({ requestId: str, step: stepNo, result: lit<'pass' | 'stop'>('pass', 'stop') }, { detail: str })),
  posterior: event('posterior', obj({ requestId: str, posteriors: rec(num), threshold: num })),
  verdict: event('verdict', obj({ requestId: str, verdict: VERDICT, reason }, { nodeId: str })),
  command: event('command', obj({ requestId: str, nodeId: str, action: ACTION })),
  exec_result: event('exec_result', obj({ requestId: str, nodeId: str, result: lit<'succeeded' | 'failed' | 'unknown'>('succeeded', 'failed', 'unknown') })),
  mcp_result: event('mcp_result', obj({ requestId: str })),
  transition: event('transition', obj({ nodeId: str, to: DEVICE_STATE }, { overdue: bool, fault: obj({ cause: reason }, { action: ACTION }) })),
  approval: event('approval', obj({ requestId: str, nodeId: str, personId: str, kind: lit<'requested' | 'approved' | 'denied' | 'expired'>('requested', 'approved', 'denied', 'expired') })),
  audit: event('audit', obj({ row: AUDIT_ROW }))
};

/** 스트림 이벤트 하나를 화면 모델로 바꾼다. 모르는 종류이거나 형식이 틀리면 undefined (그 이벤트만 버린다) */
export function mapEvent(raw: unknown): StreamEvent | undefined {
  const type = isRec(raw) ? raw.type : undefined;
  if (typeof type !== 'string' || !Object.hasOwn(EVENTS, type)) return undefined;
  try {
    return EVENTS[type as keyof typeof EVENTS](raw, type);
  } catch (e) {
    if (e instanceof BadFormat) return undefined;
    throw e;
  }
}

/**
 * 서버의 실행 상태(ExecutionStatus)를 화면 모델의 실행으로 옮긴다.
 * pending·in_progress는 명령을 보내고 결과를 기다리는 중(sent), rejected(검증된 DEVICE_BUSY)는 실패(failed),
 * not_sent(확실한 미전송)는 실행이 없으니 undefined다.
 */
export function mapExecution(status: string): RequestProgress['execution'] | undefined {
  switch (status) {
    case 'pending': case 'in_progress': return 'sent';
    case 'rejected': return 'failed';
    case 'succeeded': case 'failed': case 'unknown': return status;
    default: return undefined;
  }
}
const EXECUTION: Check<RequestProgress['execution'] | undefined> = (v, p) =>
  mapExecution(lit('pending', 'in_progress', 'succeeded', 'failed', 'unknown', 'not_sent', 'rejected')(v, p));

/**
 * 이력 기간을 서버 값으로 바꾼다. 화면의 "오늘"은 `today`, "14:26~14:29"(앞에 "오늘 "이 있어도, 시가 한 자리여도)는 `HH:MM~HH:MM`이다.
 * 그 밖은 그대로 보내고 서버가 400으로 알린다.
 */
export function serverPeriod(period: string): string {
  const m = /(\d{1,2}):(\d{2})\s*~\s*(\d{1,2}):(\d{2})/.exec(period);
  if (m) return `${m[1].padStart(2, '0')}:${m[2]}~${m[3].padStart(2, '0')}:${m[4]}`;
  return period.trim() === '오늘' ? 'today' : period;
}

// ── 조회 응답 (architecture.md §5 "대시보드 조회" 형식 표) ───────────────────────

const PLACES: Check<PlacesInfo> = obj({
  weakRssiDbm: num,
  places: arr(obj({
    placeId: str, placeName: str, modelVersion: str, calibrationVersion: str,
    devices: arr(obj({ nodeId: str, name: str, capability: CAPABILITY, occupancy: OCCUPANCY }))
  }))
});

const USES: Check<UseRow[]> = arr(obj(
  { nodeId: str, personId: str, occupancy: OCCUPANCY, session: lit<UseRow['session']>('RESERVED', 'ACTIVE', 'CLOSING'), startedAt: time('HH:MM') },
  { sessionNote: str, overdueMinutes: whole }
));

const APPROVAL_ROW = obj({ nodeId: str, personId: str }, {
  mode: lit<'one_time' | 'time_window'>('one_time', 'time_window'), requestedAt: time('HH:MM'), remainingMinutes: num, validUntil: time('HH:MM'), uses: num,
  endReason: lit<'APPROVAL_REJECTED' | 'APPROVAL_REQUEST_EXPIRED' | 'APPROVAL_EXHAUSTED' | 'APPROVAL_EXPIRED'>('APPROVAL_REJECTED', 'APPROVAL_REQUEST_EXPIRED', 'APPROVAL_EXHAUSTED', 'APPROVAL_EXPIRED')
});
const APPROVALS: Check<Approvals> = obj({ pending: arr(APPROVAL_ROW), active: arr(APPROVAL_ROW), ended: arr(APPROVAL_ROW) });

const AUDIT: Check<AuditRow[]> = arr(AUDIT_ROW);

const HISTORY: Check<HistoryRow[]> = arr(obj(
  { requestId: str, at: time('HH:MM'), personId: str, target: TARGET, action: ACTION, verdict: VERDICT, outcome: str },
  { nodeId: str, code: reason }
));

const STEP_OUTCOME: Check<StepOutcome> = obj({ result: lit<StepOutcome['result']>('pending', 'pass', 'stop', 'skip') }, { detail: str });
const REQUEST: Check<RequestProgress> = obj(
  { id: str, personId: str, tool: lit<'request_use'>('request_use'), target: TARGET, action: ACTION, at: time('HH:MM:SS'), steps: arr(STEP_OUTCOME, 7) },
  {
    verdict: VERDICT, reason, nodeId: str, posteriors: rec(num), threshold: num, execution: EXECUTION,
    accessRequestedAt: time('HH:MM:SS'), accessExpiredAt: time('HH:MM:SS'), respondedAt: time('HH:MM:SS')
  }
);

const LIVE = obj({
  since: time('HH:MM'), decisions: num, doubleOccupancy: num, latencyMedianSec: nul(num), latencyP95Sec: nul(num),
  counts: obj({ OK: num, BUSY: num, ASK: num, DENY: num, NEED_APPROVAL: num })
});
const METRIC = obj({ id: num, name: str, pass: nul(bool), label: str, sample: str }, { sampleNote: str });
const CONFUSION = obj({ labels: arr(str), values: arr(arr(num)) });
const EVALUATION = obj({ date: str, metrics: arr(METRIC) }, { confusion: CONFUSION });
/** evaluation은 평가 리포트가 없으면, confusion은 리포트에 혼동 행렬이 없으면 빠진다 */
const TRUST: Check<TrustReport> = obj({ live: LIVE }, { evaluation: EVALUATION });

const RENTAL: Check<RentalInfo> = obj({
  nodeId: str, personId: str, startedAt: time('HH:MM'), overdueMinutes: whole,
  timeline: arr(obj({ at: time('HH:MM:SS'), text: str }, { now: bool }))
});

const HEALTH: Check<Health> = obj({
  devices: arr(obj({ nodeId: str, connected: bool, trust: TRUST_STATE })),
  beacons: arr(obj({ personId: str, label: str, lastSeenSec: nul(whole) }, { note: str })),
  server: obj({ database: bool, mqtt: bool, model: str })
});

/** 조회 응답 하나를 화면 모델로 바꾸는 함수. 형식이 틀리면 Error를 던진다 (useQuery가 패널에 보인다) */
function parse<T>(check: Check<T>, kind: string): (raw: unknown) => T {
  return (raw) => {
    try {
      return check(raw, kind);
    } catch (e) {
      throw e instanceof BadFormat ? new Error(`서버 응답 형식이 맞지 않아요 · ${e.message}`) : e;
    }
  };
}

export const placesOf = parse(PLACES, 'places');
export const usesOf = parse(USES, 'uses');
export const approvalsOf = parse(APPROVALS, 'approvals');
export const auditOf = parse(AUDIT, 'audit');
export const historyOf = parse(HISTORY, 'history');
export const requestOf = parse(REQUEST, 'request');
export const trustOf = parse(TRUST, 'trust');
export const rentalOf = parse(RENTAL, 'rental');
export const healthOf = parse(HEALTH, 'health');

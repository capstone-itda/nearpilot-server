// 목 실시간 흐름: 장면 이야기(14:26 ASK → 14:28 열기 → 14:29 고장 → 14:31 승인)를 시각 순서대로 흘려보낸다.
// 비콘 신호는 사람이 움직이는 대로 계속 바뀌고, 판정·실행 결과는 그 시각에 도착한다.
// 화면은 받은 스냅숏으로만 계산하므로 서버 스트림을 붙여도 화면 코드는 그대로다.
import type { Person, Snapshot } from '../../model/types';
import { RSSI_APPROVAL_B, RSSI_ASK_A, RSSI_FAULT_B, RSSI_FLOW_A, RSSI_IDLE_A, RSSI_IDLE_B, SCENES } from './fixtures';

type Rssi = Record<string, number>;
/** HH:MM:SS → 초 */
export const sec = (t: string) => { const [h, m, s = '0'] = t.split(':'); return Number(h) * 3600 + Number(m) * 60 + Number(s); };
const clock = (v: number) => {
  const t = Math.floor(v);
  return [Math.floor(t / 3600), Math.floor(t / 60) % 60, t % 60].map((n) => String(n).padStart(2, '0')).join(':');
};

/** 실제 경과(초) → 장소 시각. 조용한 구간은 빨리 감고, 판정이 오가는 구간은 실제 속도로 보인다. */
const BEATS: [string, string, number][] = [
  ['14:25:50', '14:26:12', 8],   // A가 들어와 사물함 쪽으로 걸어감 → ASK
  ['14:26:12', '14:26:20', 6],
  ['14:26:20', '14:28:05', 8],   // A가 사물함 1 앞으로 옮김
  ['14:28:05', '14:28:20', 12],  // 다시 요청 → 열기 명령 → 열림
  ['14:28:20', '14:29:30', 8],   // B가 들어와 사물함 2 앞으로
  ['14:29:30', '14:29:44', 12],  // 열기 명령 → 결과 응답 없음 → 고장 격리
  ['14:29:44', '14:30:50', 7],   // B가 조명 1 쪽으로
  ['14:30:50', '14:31:04', 12]   // 승인 필요 → 승인 신청 → 소유자 알림
];
export const LIVE_START = sec(BEATS[0][0]);
export const LIVE_LENGTH_SEC = BEATS.reduce((n, b) => n + b[2], 0);

export function liveTime(elapsedSec: number): number {
  let left = elapsedSec;
  for (const [from, to, dur] of BEATS) {
    if (left <= dur) return sec(from) + ((sec(to) - sec(from)) * left) / dur;
    left -= dur;
  }
  // 이야기가 끝나면 실제 속도로 계속 간다
  return sec(BEATS[BEATS.length - 1][1]) + left;
}

/** 사람별 신호 세기 기준점. 사이 시각은 선형으로 잇는다. 기준점 밖에서는 비콘을 받지 않는다. */
const FAR: Rssi = { 'locker-1': -86, 'locker-2': -87, 'locker-3': -88, 'speaker-1': -84, 'light-1': -85, 'light-2': -86, 'light-3': -87 };
const TRACKS: Record<string, { from: string; keys: [string, Rssi][] }> = {
  'p-a': { from: '14:25:50', keys: [['14:25:50', FAR], ['14:26:10', RSSI_ASK_A], ['14:26:25', RSSI_ASK_A], ['14:28:00', RSSI_FLOW_A], ['14:28:40', RSSI_FLOW_A], ['14:30:00', RSSI_IDLE_A]] },
  'p-b': { from: '14:28:30', keys: [['14:28:30', RSSI_IDLE_B], ['14:29:30', RSSI_FAULT_B], ['14:29:50', RSSI_FAULT_B], ['14:30:50', RSSI_APPROVAL_B]] }
};

/** 같은 입력에 같은 값을 주는 ±1.5 dB 흔들림 (0.5초마다 바뀜) */
function jitter(personId: string, nodeId: string, t: number): number {
  let h = Math.floor(t * 2) * 2654435761;
  for (const c of personId + nodeId) h = Math.imul(h ^ c.charCodeAt(0), 2246822519);
  h ^= h >>> 13;
  return ((((h >>> 0) % 1000) / 1000) * 3 - 1.5);
}

function rssiAt(personId: string, t: number): Rssi | undefined {
  const tr = TRACKS[personId];
  if (!tr || t < sec(tr.from)) return undefined;
  const keys = tr.keys;
  let a = keys[0], b = keys[0];
  for (let i = 0; i < keys.length; i++) {
    if (sec(keys[i][0]) <= t) { a = keys[i]; b = keys[i + 1] ?? keys[i]; }
  }
  const span = sec(b[0]) - sec(a[0]);
  const k = span > 0 ? Math.min(1, (t - sec(a[0])) / span) : 0;
  return Object.fromEntries(Object.keys(a[1]).map((id) => [id, Math.round(a[1][id] + (b[1][id] - a[1][id]) * k + jitter(personId, id, t))]));
}

/** 판정·실행 상태: 그 시각까지 도착한 마지막 사건 */
function eventsAt(t: number): Snapshot {
  if (t < sec('14:26:12')) return { ...SCENES.ask(), request: undefined, mockPhones: SCENES.idle().mockPhones };
  if (t < sec('14:28:10')) return SCENES.ask();
  if (t < sec('14:28:12')) return SCENES.flow();
  if (t < sec('14:29:37')) return SCENES.done();
  if (t < sec('14:29:40')) {
    // 열기 명령을 보내고 결과를 기다리는 중
    const f = SCENES.fault();
    return {
      ...f,
      devices: f.devices.map((d) => (d.nodeId === 'locker-2' ? { ...d, state: 'RESERVED', fault: undefined } : d)),
      request: { ...f.request!, execution: 'sent' },
      mockPhones: f.mockPhones!.map((m) => (m.personId === 'p-b' ? { ...m, reply: '서버 응답을 기다리는 중…' } : m))
    };
  }
  if (t < sec('14:30:58')) return SCENES.fault();
  if (t < sec('14:31:00')) {
    // 승인 필요 판정만 도착하고, AI가 아직 승인을 신청하지 않음
    const s = SCENES.approval();
    return {
      ...s, ownerNotified: false, request: { ...s.request!, accessRequestedAt: undefined },
      mockPhones: SCENES.fault().mockPhones!.map((m) => (m.personId === 'p-b' ? { personId: 'p-b', scene: 'chat', said: '조명 켜 줘', reply: '서버 응답을 기다리는 중…' } : m))
    };
  }
  return SCENES.approval();
}

/** 장소 시각 t의 스냅숏 */
export function liveSnapshot(t: number): Snapshot {
  const s = eventsAt(t);
  const people: Person[] = s.people.map((p) => {
    const r = rssiAt(p.id, t);
    return p.role !== 'visitor' || TRACKS[p.id] === undefined ? p : { ...p, beaconSeen: !!r, rssi: r ?? {} };
  });
  return { ...s, people, lastReceivedAt: clock(t) };
}

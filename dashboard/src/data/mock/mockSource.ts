// 목 어댑터: 서버 없이 화면을 그린다. 서버 어댑터는 경로와 형식이 정해진 뒤 같은 인터페이스로 만든다.
import type { DataSource } from '../source';
import { liveSnapshot, liveTime } from './live';
import type { HistoryQuery, Snapshot, UseRow } from '../../model/types';
import {
  APPROVAL_WAIT_MIN, APPROVALS, AUDIT, HEALTH, HISTORY, PLACE, PLAY_ORDER, RENTAL_DUE, RENTALS, REQUESTS, SCENES, TRUST, TRUST_BASE, USES,
  type SceneName, type Since
} from './fixtures';

/** HH:MM 또는 HH:MM:SS → 초 */
const sec = (t: string) => { const [h, m, s = '0'] = t.split(':'); return Number(h) * 3600 + Number(m) * 60 + Number(s); };
/** 목 전용 since 값을 떼어 낸다 */
const strip = <T,>({ since: _since, ...row }: Since<T>) => row as unknown as T;

export interface MockOptions {
  /** 고정할 장면. 'play'면 PLAY_ORDER를 차례로 재생하고, 'live'면 신호와 판정이 시각에 따라 흐른다. */
  scene: SceneName | 'play' | 'live';
  /** 재생 간격 (ms) */
  intervalMs?: number;
}

const copy = <T,>(v: T): Promise<T> => Promise.resolve(structuredClone(v));

export function isSceneName(v: string | null): v is SceneName {
  return !!v && Object.hasOwn(SCENES, v);
}

/** "오늘"은 전부, "14:20~14:31"처럼 시각 범위를 쓰면 그 안의 기록만 */
function inPeriod(at: string, period: string): boolean {
  const m = /(\d{1,2}):(\d{2})\s*~\s*(\d{1,2}):(\d{2})/.exec(period);
  if (!m) return true;
  const t = (h: string, mm: string) => Number(h) * 60 + Number(mm);
  const [h, mm] = at.split(':');
  return t(m[1], m[2]) <= t(h, mm) && t(h, mm) <= t(m[3], m[4]);
}

export function createMockSource(opt: MockOptions): DataSource {
  const listeners = new Set<(s: Snapshot) => void>();
  let index = 0;
  let current: Snapshot = opt.scene === 'live' ? liveSnapshot(liveTime(0)) : opt.scene === 'play' ? SCENES[PLAY_ORDER[0]]() : SCENES[opt.scene]();
  let timer: ReturnType<typeof setInterval> | undefined;

  const emit = () => listeners.forEach((l) => l(current));
  const seen = (t: string) => sec(t) <= sec(current.lastReceivedAt);
  /** 대여 시간을 넘긴 분. 넘기지 않았으면 0 */
  const overdue = (nodeId: string) => {
    const due = RENTAL_DUE[nodeId];
    const st = current.devices.find((d) => d.nodeId === nodeId)?.state;
    if (!due || !st || st === 'AVAILABLE' || !seen(due)) return 0;
    return Math.floor((sec(current.lastReceivedAt) - sec(due)) / 60);
  };
  const startLive = () => {
    if (timer) return;
    const started = Date.now();
    timer = setInterval(() => {
      current = liveSnapshot(liveTime((Date.now() - started) / 1000));
      emit();
    }, opt.intervalMs ?? 500);
  };
  const startPlay = () => {
    if (opt.scene === 'live') { startLive(); return; }
    if (opt.scene !== 'play' || timer || index === PLAY_ORDER.length - 1) return;
    timer = setInterval(() => {
      // 한 번만 재생한다. 처음으로 돌아가면 복구 없이 고장 격리가 풀린 것처럼 보인다.
      index += 1;
      current = SCENES[PLAY_ORDER[index]]();
      emit();
      if (index === PLAY_ORDER.length - 1) { clearInterval(timer); timer = undefined; }
    }, opt.intervalMs ?? 6000);
  };

  return {
    subscribe(listener) {
      listeners.add(listener);
      listener(current);
      startPlay();
      return () => {
        listeners.delete(listener);
        if (!listeners.size && timer) { clearInterval(timer); timer = undefined; }
      };
    },
    reconnect() {
      if (current.connected) return;
      current = SCENES.later();
      emit();
    },
    place: () => copy(PLACE),
    // 조회는 지금 스냅숏의 수신 시각에 서버가 줄 결과를 흉내 낸다
    uses: () => copy(USES.filter((u) => {
      const st = current.devices.find((d) => d.nodeId === u.nodeId)?.state;
      return st && st !== 'AVAILABLE' && seen(u.since);
    }).map((u): UseRow => {
      const st = current.devices.find((d) => d.nodeId === u.nodeId)!.state;
      const over = overdue(u.nodeId);
      return { ...strip<UseRow>(u), session: st === 'FAULT' ? u.session : st as UseRow['session'], ...(over ? { overdueMinutes: over } : {}) };
    })),
    // 기다리는 승인의 남은 시간은 신청 시각부터 줄고, 다 되면 대기 만료로 끝난다
    approvals: () => {
      const left = (since: string) => Math.floor(APPROVAL_WAIT_MIN - (sec(current.lastReceivedAt) - sec(since)) / 60);
      const pending = APPROVALS.pending.filter((a) => seen(a.since));
      return copy({
        pending: pending.filter((a) => left(a.since) > 0).map((a) => ({ ...strip(a), remainingMinutes: left(a.since) })),
        active: APPROVALS.active.filter((a) => seen(a.since)).map(strip),
        ended: [
          ...pending.filter((a) => left(a.since) <= 0).map(({ nodeId, personId }) => ({ nodeId, personId, endReason: 'APPROVAL_REQUEST_EXPIRED' as const })),
          ...APPROVALS.ended.filter((a) => seen(a.since)).map(strip)
        ]
      });
    },
    audit: () => copy(AUDIT.filter((a) => seen(a.at))),
    history: (q: HistoryQuery) => copy(HISTORY.filter((h) => seen(h.since) &&
      inPeriod(h.at, q.period) && (!q.verdict || h.verdict === q.verdict) && (!q.nodeId || h.nodeId === q.nodeId) && (!q.host || h.host === q.host)).map(strip)),
    // 진행 중인 요청은 스냅숏 그대로, 끝난 요청은 이력에 보인 뒤부터 준다
    request: (id: string) => {
      if (current.request?.id === id) return copy(current.request);
      const h = HISTORY.find((r) => r.requestId === id);
      return REQUESTS[id] && h && seen(h.since) ? copy(REQUESTS[id]()) : Promise.reject(new Error(`요청 ${id} 없음`));
    },
    // 집계는 이력 앞의 기준치에 지금까지 보인 이력을 더한다
    trust: () => {
      const rows = HISTORY.filter((h) => seen(h.since));
      const counts = { ...TRUST_BASE.counts };
      for (const h of rows) counts[h.verdict] += 1;
      const decisions = TRUST_BASE.decisions + rows.length;
      const latency = decisions ? {} : { latencyMedianSec: null, latencyP95Sec: null };
      return copy({ ...TRUST, live: { ...TRUST_BASE, decisions, counts, ...latency } });
    },
    rental: (id: string) => {
      const r = RENTALS[id];
      if (!r) return Promise.reject(new Error(`대여 ${id} 없음`));
      const timeline = r.timeline.filter((t) => seen(t.at));
      return copy({ ...r, overdueMinutes: overdue(id), timeline: timeline.map((t, i) => ({ ...t, now: i === timeline.length - 1 })) });
    },
    // 비콘 수신은 지금 스냅숏을 따른다
    health: () => copy({ ...HEALTH, beacons: HEALTH.beacons.map((b) => {
      const p = current.people.find((x) => x.id === b.personId);
      return p?.role === 'visitor' && !p.beaconSeen ? { ...b, lastSeenSec: null, note: '최근 신호 없음' } : b;
    }) })
  };
}

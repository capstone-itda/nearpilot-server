// 목 어댑터: 서버 없이 화면을 그린다. 서버 어댑터는 경로와 형식이 정해진 뒤 같은 인터페이스로 만든다.
// 스트림은 story.ts의 이벤트를 보낸다. 조회는 마지막으로 보낸 이벤트 시각까지의 기록만 준다.
import type { DataSource } from '../source';
import type { StreamEvent } from '../../model/events';
import type { HistoryQuery, MonitorState, UseRow } from '../../model/types';
import { FINISHED_HOLD_SEC, initialState, reduceAll } from '../reduce';
import {
  APPROVAL_WAIT_MIN, APPROVALS, AUDIT, HEALTH, HISTORY, PLACE, RENTAL_DUE, RENTALS, TRUST, TRUST_BASE, USES, type Since
} from './fixtures';
import { PLAY_ORDER, SCENE_TIME, STORY, finishedRequest, rssiEvents, sec, stamp, storyBetween, storyUntil, type SceneName } from './story';

/** 목 전용 since 값을 떼어 낸다 */
const strip = <T,>({ since: _since, ...row }: Since<T>) => row as unknown as T;

export interface MockOptions {
  /** 고정할 장면. 'play'면 PLAY_ORDER의 장면 사이 이벤트를 차례로 보내고, 'live'면 이벤트가 시각 순서대로 흐른다. */
  scene: SceneName | 'play' | 'live';
  /** play의 장면 간격 (ms) */
  intervalMs?: number;
}

const copy = <T,>(v: T): Promise<T> => Promise.resolve(structuredClone(v));

export function isSceneName(v: string | null): v is SceneName {
  return !!v && Object.hasOwn(SCENE_TIME, v);
}

/** "오늘"은 전부, "14:20~14:31"처럼 시각 범위를 쓰면 그 안의 기록만 */
function inPeriod(at: string, period: string): boolean {
  const m = /(\d{1,2}):(\d{2})\s*~\s*(\d{1,2}):(\d{2})/.exec(period);
  if (!m) return true;
  const t = (h: string, mm: string) => Number(h) * 60 + Number(mm);
  const [h, mm] = at.split(':');
  return t(m[1], m[2]) <= t(h, mm) && t(h, mm) <= t(m[3], m[4]);
}

/** live 시작 시각: A가 사물함 쪽으로 걸어가기 직전 */
export const LIVE_START = sec('14:25:50');

/**
 * live의 실제 경과(초) → 이야기 시각. 요청 하나의 이벤트는 0.2초 안에 몰려 있어서 하나씩 약 0.9초 간격으로 늘려 보이고,
 * 이벤트 사이의 긴 조용한 구간은 빨리 감는다. 마지막 이벤트 뒤에는 실제 속도로 간다.
 */
export function liveClock(start = LIVE_START): (elapsedSec: number) => number {
  // 요청이 끝나 화면에서 내려가는 때도 마디로 둬서 결과가 보이는 시간을 너무 빨리 감지 않는다
  const holdEnds = STORY.filter((e) => e.type === 'mcp_result' || (e.type === 'approval' && e.kind === 'expired')).map((e) => sec(e.at) + FINISHED_HOLD_SEC + 1);
  const times = [...new Set([...STORY.map((e) => sec(e.at)), ...holdEnds].filter((t) => t > start))].sort((a, b) => a - b);
  const knots: [number, number][] = [[0, start]];
  let real = 0;
  let prev = start;
  for (const t of times) {
    const g = t - prev;
    real += g < 0.5 ? 0.9 : g <= 5 ? g : Math.min(8, 4 + g / 60);
    knots.push([real, t]);
    prev = t;
  }
  return (e) => {
    for (let i = 1; i < knots.length; i++) {
      const [r1, t1] = knots[i];
      if (e <= r1) {
        const [r0, t0] = knots[i - 1];
        return t0 + ((t1 - t0) * (e - r0)) / (r1 - r0);
      }
    }
    const [rl, tl] = knots[knots.length - 1];
    return tl + (e - rl);
  };
}

export function createMockSource(opt: MockOptions): DataSource {
  const listeners = new Set<(events: StreamEvent[]) => void>();
  const firstScene: SceneName = opt.scene === 'play' ? PLAY_ORDER[0] : opt.scene === 'live' ? 'idle' : opt.scene;
  /** 마지막으로 보낸 이벤트의 이야기 시각 */
  let now = opt.scene === 'live' ? LIVE_START : sec(SCENE_TIME[firstScene]);
  let link: StreamEvent | undefined = opt.scene === 'offline' ? { type: 'link', at: stamp(now), connected: false, reconnectAttempts: 3 } : undefined;
  /** 연결 직후 서버가 다시 보내는 지금 상태 */
  const replay = () => [...storyUntil(now), ...(link ? [link] : [])];
  /** 조회 결과를 그 시각에 맞추려고 어댑터도 같은 상태를 쌓는다 */
  let current: MonitorState = reduceAll(initialState(PLACE), replay());
  let index = 0;
  let timer: ReturnType<typeof setInterval> | undefined;
  let liveStarted: number | undefined;

  const emit = (events: StreamEvent[]) => {
    if (!events.length) return;
    current = reduceAll(current, events);
    listeners.forEach((l) => l(events));
  };
  /** 이야기 시각 t까지 나아간다 */
  const advance = (t: number, withRssi: boolean, extra: StreamEvent[] = []) => {
    const events = [...storyBetween(now, t), ...(withRssi ? rssiEvents(t) : []), ...extra];
    now = t;
    emit(events);
  };

  const seen = (t: string) => sec(t) <= now;
  /** 대여 시간을 넘긴 분. 넘기지 않았으면 0 */
  const overdue = (nodeId: string) => {
    const due = RENTAL_DUE[nodeId];
    const st = current.devices.find((d) => d.nodeId === nodeId)?.state;
    if (!due || !st || st === 'AVAILABLE' || !seen(due)) return 0;
    return Math.floor((now - sec(due)) / 60);
  };

  const start = () => {
    if (timer) return;
    if (opt.scene === 'live') {
      liveStarted ??= Date.now();
      const clock = liveClock();
      let tick = 0;
      timer = setInterval(() => {
        tick += 1;
        // 사건 이벤트는 0.1초마다 확인하고, RSSI는 0.5초마다 보낸다
        advance(Math.max(now, clock((Date.now() - liveStarted!) / 1000)), tick % 5 === 0);
      }, 100);
      return;
    }
    if (opt.scene !== 'play' || index === PLAY_ORDER.length - 1) return;
    timer = setInterval(() => {
      // 한 번만 재생한다. 처음으로 돌아가면 복구 없이 고장 격리가 풀린 것처럼 보인다.
      index += 1;
      advance(sec(SCENE_TIME[PLAY_ORDER[index]]), true);
      if (index === PLAY_ORDER.length - 1) { clearInterval(timer); timer = undefined; }
    }, opt.intervalMs ?? 6000);
  };

  return {
    mock: true,
    subscribe(listener) {
      listeners.add(listener);
      listener(replay());
      start();
      return () => {
        listeners.delete(listener);
        if (!listeners.size && timer) { clearInterval(timer); timer = undefined; }
      };
    },
    reconnect() {
      if (current.connected) return;
      // 끊긴 동안 쌓인 이벤트를 받고 연결이 돌아온다
      const t = sec(SCENE_TIME.later);
      link = { type: 'link', at: stamp(t), connected: true, reconnectAttempts: 0 };
      advance(t, true, [link]);
    },
    place: () => copy(PLACE),
    // 조회는 마지막으로 보낸 이벤트 시각에 서버가 줄 결과를 흉내 낸다
    uses: () => copy(USES.filter((u) => {
      const st = current.devices.find((d) => d.nodeId === u.nodeId)?.state;
      return st && st !== 'AVAILABLE' && seen(u.since);
    }).map((u): UseRow => {
      const st = current.devices.find((d) => d.nodeId === u.nodeId)!.state;
      const over = overdue(u.nodeId);
      const { sessionNote, ...row } = strip<UseRow>(u);
      // 고장 격리 메모는 기기가 실제로 FAULT일 때만 붙인다
      return { ...row, session: st === 'FAULT' ? u.session : st as UseRow['session'], ...(st === 'FAULT' && sessionNote ? { sessionNote } : {}), ...(over ? { overdueMinutes: over } : {}) };
    })),
    // 기다리는 승인의 남은 시간은 신청 시각부터 줄고, 다 되면 대기 만료로 끝난다
    approvals: () => {
      const left = (since: string) => Math.floor(APPROVAL_WAIT_MIN - (now - sec(since)) / 60);
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
    // 진행 중인 요청은 지금 상태 그대로, 끝난 요청은 이력에 보인 뒤부터 준다
    request: (id: string) => {
      if (current.request?.id === id) return copy(current.request);
      const h = HISTORY.find((r) => r.requestId === id);
      const r = h && seen(h.since) ? finishedRequest(id) : undefined;
      return r ? copy(r) : Promise.reject(new Error(`요청 ${id} 없음`));
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
    // 비콘 수신은 지금 상태를 따른다
    health: () => copy({ ...HEALTH, beacons: HEALTH.beacons.map((b) => {
      const p = current.people.find((x) => x.id === b.personId);
      return p?.role === 'visitor' && !p.beaconSeen ? { ...b, lastSeenSec: null, note: '최근 신호 없음' } : b;
    }) })
  };
}

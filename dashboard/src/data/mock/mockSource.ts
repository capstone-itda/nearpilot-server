// 목 어댑터: 서버 없이 화면을 그린다. 서버 어댑터는 따로 같은 인터페이스로 만든다.
// 스트림은 story.ts의 이벤트를 보낸다. 조회는 마지막으로 보낸 이벤트 시각까지의 기록만 준다.
import type { DataSource } from '../source';
import type { StreamEvent } from '../../model/events';
import type { HistoryQuery, HistoryRow, MonitorState, RequestProgress, UseRow } from '../../model/types';
import { FINISHED_HOLD_SEC, initialState, reduceAll } from '../reduce';
import {
  APPROVAL_WAIT_MIN, APPROVALS, AUDIT, HEALTH, HISTORY, PLACES, RENTAL_DUE, RENTALS, TRUST, TRUST_BASE, USES, WEAK_RSSI_DBM, type Since
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
 * live의 실제 경과(초) → 이야기 시각. 요청 하나의 판정 이벤트는 실제처럼 0.2초 안에 보내고,
 * 명령 뒤 결과를 기다리는 1초 안쪽의 간격은 2초로 늘려 명령 선이 보이게 하고,
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
    // 판정처럼 빠른 이벤트는 실제 간격대로 보낸다 (결과는 판정 게이지가 찬 뒤에 보인다)
    real += g < 0.5 ? g : g <= 5 ? Math.max(g, 2) : Math.min(8, 4 + g / 60);
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

/** 요청별 AI 응답(mcp_result) 시각. 이력은 응답한 뒤에 생긴다. */
const RESPONDED: Record<string, string> = Object.fromEntries(STORY.flatMap((e) => (e.type === 'mcp_result' ? [[e.requestId, e.at]] : [])));
/** 감사 로그 줄이 스트림에 온 시각 */
const AUDIT_AT: Record<string, string> = Object.fromEntries(STORY.flatMap((e) => (e.type === 'audit' ? [[e.row.id, e.at]] : [])));

/** 스트림 연결 전(14:05 전)의 요청은 이야기에 이벤트가 없다. 이력 줄에서 끝난 요청 상세를 만든다. */
function earlierRequest(h: HistoryRow): RequestProgress {
  const shared = PLACES.flatMap((p) => p.devices).find((d) => d.nodeId === h.nodeId)?.occupancy === 'shared';
  const steps: RequestProgress['steps'] = Array.from({ length: 7 }, (_, i) =>
    h.verdict === 'NEED_APPROVAL' ? { result: i < 4 ? 'pass' : i === 4 ? 'stop' : 'skip' }
      : { result: shared && i >= 5 ? 'skip' : 'pass' });
  return {
    id: h.requestId, personId: h.personId, tool: 'request_use', target: h.target, action: h.action, at: `${h.at}:00`,
    verdict: h.verdict, reason: h.code ?? 'OK', nodeId: h.nodeId, steps,
    ...(h.verdict === 'OK' ? { execution: 'succeeded' as const } : {}), respondedAt: `${h.at}:00`
  };
}

/** 승인 신청 이벤트가 오면 이력의 결과에 덧붙인다 */
function withApproval(h: HistoryRow, now: number): HistoryRow {
  const last = STORY.filter((e) => e.type === 'approval' && e.requestId === h.requestId && sec(e.at) <= now).at(-1);
  if (!last || last.type !== 'approval') return h;
  return { ...h, outcome: `${h.outcome} · ${last.kind === 'requested' ? '승인 신청' : '승인 신청 만료'}` };
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
  let current: MonitorState = reduceAll(initialState(PLACES), replay());
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
  /** 판정 기록(이력, 감사)은 스트림에서 그 이벤트가 온 뒤부터 조회된다 */
  const answered = (h: { requestId: string; since: string }) => sec(RESPONDED[h.requestId] ?? h.since) <= now;
  const audited = (a: { id: string; at: string }) => sec(AUDIT_AT[a.id] ?? a.at) <= now;
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
    places: () => copy({ weakRssiDbm: WEAK_RSSI_DBM, places: PLACES }),
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
      // 만료 이벤트가 오는 때(신청 + 대기 시간)까지 기다림에 둔다. 남은 분은 올림
      const left = (since: string) => Math.ceil(APPROVAL_WAIT_MIN - (now - sec(since)) / 60);
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
    audit: () => copy(AUDIT.filter(audited)),
    history: (q: HistoryQuery) => copy(HISTORY.filter((h) => answered(h) &&
      inPeriod(h.at, q.period) && (!q.verdict || h.verdict === q.verdict) && (!q.nodeId || h.nodeId === q.nodeId)).map((h) => withApproval(strip<HistoryRow>(h), now))),
    // 진행 중인 요청은 지금 상태 그대로, 끝난 요청은 이력에 보인 뒤부터 준다
    request: (id: string) => {
      if (current.request?.id === id) return copy(current.request);
      const h = HISTORY.find((r) => r.requestId === id);
      const r = h && answered(h) ? finishedRequest(id) ?? earlierRequest(strip<HistoryRow>(h)) : undefined;
      return r ? copy(r) : Promise.reject(new Error(`요청 ${id} 없음`));
    },
    // 집계는 이력 앞의 기준치에 시연 세션 시작(since) 뒤로 지금까지 보인 이력을 더한다
    trust: () => {
      const rows = HISTORY.filter((h) => answered(h) && h.at >= TRUST_BASE.since);
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

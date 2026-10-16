// 목 어댑터: 서버 없이 화면을 그린다. 서버 어댑터는 경로와 형식이 정해진 뒤 같은 인터페이스로 만든다.
import type { DataSource } from '../source';
import type { HistoryQuery, Snapshot } from '../../model/types';
import {
  APPROVALS, AUDIT, HEALTH, HISTORY, LAYOUT, PLAY_ORDER, RENTALS, REQUESTS, SCENES, TRUST, USES,
  type SceneName
} from './fixtures';

export interface MockOptions {
  /** 고정할 장면. 'play'면 PLAY_ORDER를 차례로 재생한다. */
  scene: SceneName | 'play';
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
  let current: Snapshot = opt.scene === 'play' ? SCENES[PLAY_ORDER[0]]() : SCENES[opt.scene]();
  let timer: ReturnType<typeof setInterval> | undefined;

  const emit = () => listeners.forEach((l) => l(current));
  const startPlay = () => {
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
      current = { ...SCENES.later(), lastReceivedAt: current.lastReceivedAt };
      emit();
    },
    layout: () => copy(LAYOUT),
    uses: () => copy(USES),
    approvals: () => copy(APPROVALS),
    audit: () => copy(AUDIT),
    history: (q: HistoryQuery) => copy(HISTORY.filter((h) =>
      inPeriod(h.at, q.period) && (!q.verdict || h.verdict === q.verdict) && (!q.nodeId || h.nodeId === q.nodeId) && (!q.host || h.host === q.host))),
    request: (id: string) => (REQUESTS[id] ? copy(REQUESTS[id]()) : Promise.reject(new Error(`요청 ${id} 없음`))),
    trust: () => copy(TRUST),
    rental: (id: string) => (RENTALS[id] ? copy(RENTALS[id]) : Promise.reject(new Error(`대여 ${id} 없음`))),
    health: () => copy(HEALTH)
  };
}

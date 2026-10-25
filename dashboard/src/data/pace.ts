// FR-12B 판정 게이지: 실제 판정은 1초 안에 끝난다. 도구 호출이 오면 게이지를 채우는 동안 판정 결과를 잠시 늦게 넘긴다.
import type { StreamEvent } from '../model/events';

/**
 * 판정 결과를 늦추는 시간 (ms): 요청 점이 서버에 닿는 0.6초(Stage의 ARRIVE_SEC) + 판정 게이지 1초.
 * 표시용 값이라 서버 설정이 아니다. CSS의 np-gauge 애니메이션과 같다.
 */
export const JUDGE_GAUGE_MS = 1600;

/** 사건 순서와 상관없는 이벤트는 늦추지 않는다 */
const passes = (e: StreamEvent) => e.type === 'rssi' || e.type === 'beacon' || e.type === 'link';

/**
 * 도구 호출 뒤의 이벤트를 게이지가 찰 때까지 모아 두었다가 순서대로 넘긴다.
 * 같은 묶음에 판정까지 들어 있으면(다시 연결해 받은 지금 상태) 늦추지 않는다.
 * 다시 연결되면 모아 둔 이벤트를 버린다 — 끊기기 전의 이벤트이고, 아직 진행 중인 것은 지금 상태가 다시 보낸다.
 */
export function paceJudging(listener: (events: StreamEvent[]) => void, gaugeMs = JUDGE_GAUGE_MS) {
  let held: StreamEvent[] = [];
  let timer: ReturnType<typeof setTimeout> | undefined;

  const push = (events: StreamEvent[]) => {
    if (timer && events.some((e) => e.type === 'link' && e.connected)) {
      clearTimeout(timer);
      timer = undefined;
      held = [];
    }
    if (timer) {
      const now = events.filter(passes);
      held.push(...events.filter((e) => !passes(e)));
      if (now.length) listener(now);
      return;
    }
    const k = events.findIndex((e) => e.type === 'mcp_call' && !events.some((v) => v.type === 'verdict' && v.requestId === e.requestId));
    if (k === -1) { listener(events); return; }
    const rest = events.slice(k + 1);
    listener([...events.slice(0, k + 1), ...rest.filter(passes)]);
    held = rest.filter((e) => !passes(e));
    timer = setTimeout(() => {
      timer = undefined;
      const next = held;
      held = [];
      if (next.length) push(next);
    }, gaugeMs);
  };

  return { push, stop: () => clearTimeout(timer) };
}

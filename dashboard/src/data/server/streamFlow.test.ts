// 서버 스트림 이벤트가 어댑터(mapEvent)와 앱이 쌓는 길(판정 게이지 → 리듀서 → 레일)을 지나 화면 상태가 되는 흐름
// 서버 이벤트는 docs/architecture.md §5 "WebSocket" 형식 표의 키 그대로다
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { MonitorState } from '../../model/types';
import { railView } from '../../view/rail';
import { LOUNGE } from '../mock/fixtures';
import { JUDGE_GAUGE_MS, paceJudging } from '../pace';
import { initialState, reduceAll } from '../reduce';
import { mapEvent } from './map';

afterEach(() => { vi.useRealTimers(); });

const AT = '2026-10-24T05:28:10.123+00:00';
const BEACON = { type: 'beacon', at: AT, personId: 'p1', pseudonym: '방문자 A', letter: 'A', role: 'visitor', seen: true };
const CALL = { type: 'mcp_call', at: AT, requestId: 'r1', personId: 'p1', tool: 'request_use', target: { kind: 'nearest', capability: 'locker' }, action: 'open' };
const VERDICT_OK = { type: 'verdict', at: AT, requestId: 'r1', verdict: 'OK', reason: 'OK', nodeId: 'locker-1' };
const VERDICT_APPROVAL = { type: 'verdict', at: AT, requestId: 'r1', verdict: 'NEED_APPROVAL', reason: 'APPROVAL_REQUIRED', nodeId: 'locker-1' };
const COMMAND = { type: 'command', at: AT, requestId: 'r1', nodeId: 'locker-1', action: 'open' };
const MCP_RESULT = { type: 'mcp_result', at: AT, requestId: 'r1' };
const exec = (result: string) => ({ type: 'exec_result', at: AT, requestId: 'r1', nodeId: 'locker-1', result });
const approval = (kind: string) => ({ type: 'approval', at: AT, requestId: 'r1', nodeId: 'locker-1', personId: 'p1', kind });
/** 지금 상태가 보내는 기기 전이 */
const NOW_ACTIVE = { type: 'transition', at: AT, nodeId: 'locker-1', to: 'ACTIVE' };

/** 앱이 구독한 묶음을 쌓는 길(App.tsx): 서버 메시지 → mapEvent(serverSource) → 판정 게이지(paceJudging) → reduceAll */
function session() {
  let state: MonitorState = initialState([LOUNGE]);
  const pacer = paceJudging((events) => { state = reduceAll(state, events); });
  /** 연결 이벤트는 서버 이벤트가 아니라 어댑터가 만든다. 다시 연결하면 첫 메시지(지금 상태)보다 먼저 온다 */
  const link = (connected: boolean) => pacer.push([{ type: 'link', at: '14:28:12.000', connected, reconnectAttempts: connected ? 0 : 1 }]);
  return {
    get state() { return state; },
    rail: () => railView(state, [LOUNGE], 'p1'),
    /** 서버 메시지 하나(한 묶음) */
    message: (events: unknown[]) => pacer.push(events.flatMap((e) => mapEvent(e) ?? [])),
    disconnect: () => link(false),
    reconnect: () => link(true)
  };
}

describe('서버 스트림의 실행 결과', () => {
  it('FR12B 기기가 실행 전에 거절하면(exec_result rejected) 요청의 실행이 rejected이고 레일 5칸이 거절 문구다', () => {
    const s = session();
    s.message([BEACON, CALL, VERDICT_OK, COMMAND]);
    expect(s.rail().steps[3]).toMatchObject({ status: 'current', mark: '실행 중' });
    expect(s.rail().steps[4]).toEqual({ status: 'todo' });

    s.message([exec('rejected')]);
    expect(s.state.request?.execution).toBe('rejected');
    expect(s.rail().steps[3]).toMatchObject({ status: 'done' });
    expect(s.rail().steps[4]).toEqual({ status: 'current', tone: 'busy', mark: '거절됨', detail: '기기가 실행 전에 거절', code: 'rejected' });
    // 거절은 고장 격리가 아니다
    expect(s.state.devices.some((d) => d.state === 'FAULT')).toBe(false);
  });

  it.each([['succeeded', '완료'], ['failed', '고장 격리'], ['rejected', '거절됨'], ['unknown', '고장 격리']])(
    'FR12B 스트림 exec_result %s → 레일 5칸 "%s"',
    (result, mark) => {
      const s = session();
      s.message([BEACON, CALL, VERDICT_OK, COMMAND, exec(result)]);
      expect(s.state.request?.execution).toBe(result);
      expect(s.rail().steps[4].mark).toBe(mark);
    }
  );
});

describe('서버 스트림 다시 연결', () => {
  it('FR12B 명령을 보낸 뒤 끊겼다 다시 연결했는데 지금 상태에 그 요청이 없으면 요청이 사라지고 레일은 실행 중이 아니다', () => {
    const s = session();
    s.message([BEACON, CALL, VERDICT_OK, COMMAND]);
    expect(s.rail().steps[3].mark).toBe('실행 중');

    // 끊긴 동안 요청이 끝났다. 지금 상태에는 기기 전이와 비콘만 있다
    s.disconnect();
    s.reconnect();
    s.message([NOW_ACTIVE, BEACON]);
    expect(s.state.connected).toBe(true);
    expect(s.state.request).toBeUndefined();
    expect(s.rail()).toMatchObject({ mode: 'guide', title: '요청 대기' });
    // 사람과 기기는 지금 상태가 덮는다
    expect(s.state.devices.find((d) => d.nodeId === 'locker-1')?.state).toBe('ACTIVE');
    expect(s.state.people.map((p) => p.pseudonym)).toEqual(['방문자 A']);
  });

  it('FR12B 승인 신청 중에 끊겼다 다시 연결하면 소유자 알림을 지우고, 지금 상태에 대기 중인 승인이 있으면 다시 켠다', () => {
    const s = session();
    s.message([BEACON, CALL, VERDICT_APPROVAL, MCP_RESULT, approval('requested')]);
    expect(s.state.ownerNotified).toBe(true);
    expect(s.rail().steps[4].mark).toBe('승인 기다림');

    // 끊긴 동안 소유자가 승인했다. 지금 상태에는 대기 중인 승인이 없다
    s.disconnect();
    s.reconnect();
    expect(s.state.ownerNotified).toBe(false);
    s.message([NOW_ACTIVE]);
    expect(s.state.ownerNotified).toBe(false);
    expect(s.state.request).toBeUndefined();

    // 끊긴 동안 승인이 나지 않았다면 지금 상태가 대기 중인 승인을 다시 보낸다
    const waiting = session();
    waiting.message([BEACON, CALL, VERDICT_APPROVAL, MCP_RESULT, approval('requested')]);
    waiting.disconnect();
    waiting.reconnect();
    waiting.message([NOW_ACTIVE, approval('requested')]);
    expect(waiting.state.ownerNotified).toBe(true);
  });

  it('FR12B 지금 상태가 아직 진행 중인 요청을 다시 보내면 그 요청이 다시 선다', () => {
    const s = session();
    s.message([BEACON, CALL, VERDICT_OK, COMMAND]);
    s.disconnect();
    s.reconnect();
    expect(s.state.request).toBeUndefined();

    // 서버가 진행 중인 요청의 이벤트를 처음부터 다시 보낸다 (명령 결과를 아직 받지 못했다)
    s.message([NOW_ACTIVE, BEACON, CALL, VERDICT_OK, COMMAND]);
    expect(s.state.request).toMatchObject({ id: 'r1', verdict: 'OK', execution: 'sent' });
    expect(s.rail().steps[3].mark).toBe('실행 중');
  });

  it('FR12B 끊기기만 했을 때는 마지막 화면을 그대로 둔다. 요청을 지우는 것은 다시 연결할 때다', () => {
    const s = session();
    s.message([BEACON, CALL, VERDICT_OK, COMMAND]);
    s.disconnect();
    expect(s.state.connected).toBe(false);
    expect(s.state.request?.id).toBe('r1');
    expect(s.rail()).toMatchObject({ mode: 'live', title: '방문자 A · 사물함 열기' });
  });

  it('FR12B 판정 게이지가 이벤트를 붙든 동안 끊겼다 다시 연결해도, 연결 이벤트는 바로 넘어가 끊기기 전의 요청을 지우고 이어서 온 지금 상태는 지워지지 않는다', () => {
    vi.useFakeTimers();
    const s = session();
    s.message([BEACON, CALL]);
    // 판정 전: 게이지가 차는 동안 뒤 이벤트를 붙든다
    s.message([VERDICT_OK, COMMAND]);
    expect(s.state.request?.verdict).toBeUndefined();

    s.disconnect();
    expect(s.state.connected).toBe(false);
    s.reconnect();
    expect(s.state.request).toBeUndefined();

    // 지금 상태: 같은 요청이 아직 진행 중이다. 게이지가 붙든 채라 게이지가 찬 뒤에 넘어온다. 붙들었던 옛 이벤트가 먼저 넘어오지만 지운 요청에는 닿지 않는다
    s.message([NOW_ACTIVE, CALL, VERDICT_OK, COMMAND]);
    vi.advanceTimersByTime(JUDGE_GAUGE_MS);
    expect(s.state.request).toMatchObject({ id: 'r1', verdict: 'OK', execution: 'sent' });
    expect(s.rail().steps[3].mark).toBe('실행 중');
  });

  it('FR12B 판정 게이지가 붙들었던 옛 이벤트가 나중에 넘어와도, 지운 요청은 되살아나지 않는다', () => {
    vi.useFakeTimers();
    const s = session();
    s.message([BEACON, CALL]);
    s.message([VERDICT_OK, COMMAND, exec('succeeded'), MCP_RESULT]);

    s.disconnect();
    s.reconnect();
    // 지금 상태에는 그 요청이 없다
    s.message([NOW_ACTIVE]);
    vi.advanceTimersByTime(JUDGE_GAUGE_MS);
    expect(s.state.request).toBeUndefined();
    expect(s.state.devices.find((d) => d.nodeId === 'locker-1')?.state).toBe('ACTIVE');
  });
});

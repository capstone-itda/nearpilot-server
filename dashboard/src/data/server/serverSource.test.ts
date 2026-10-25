// 서버 어댑터: 가짜 fetch·WebSocket·타이머로 조회 REST(FR-12A)와 모니터링 스트림(FR-12B)을 시험한다
import { afterAll, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import type { StreamEvent } from '../../model/events';
import { createServerSource, type ServerOptions } from './serverSource';

beforeAll(() => { vi.stubEnv('TZ', 'Asia/Seoul'); });
afterAll(() => { vi.unstubAllEnvs(); });

const ORIGIN = 'http://dash.test:5173';
const TOKEN = 'np_owner_tok-123';
/** 서버 시각 (UTC 05:28:10.123 = 서울 14:28:10.123) */
const AT = '2026-10-24T05:28:10.123+00:00';

// ── 가짜 ────────────────────────────────────────────────────────────

/** 브라우저 WebSocket 대역. 시험이 열림·메시지·끊김을 직접 일으킨다 */
class FakeSocket {
  static all: FakeSocket[] = [];
  onopen: (() => void) | null = null;
  onmessage: ((e: { data: unknown }) => void) | null = null;
  onclose: ((e: { code: number }) => void) | null = null;
  sent: string[] = [];
  closed = false;
  closeCode: number | undefined;
  constructor(public url: string) { FakeSocket.all.push(this); }
  send(data: string) { this.sent.push(data); }
  close(code?: number) { this.closed = true; this.closeCode = code; }
  open() { this.onopen?.(); }
  message(data: unknown) { this.onmessage?.({ data: typeof data === 'string' ? data : JSON.stringify(data) }); }
  drop(code = 1006) { this.onclose?.({ code }); }
}
const last = () => FakeSocket.all[FakeSocket.all.length - 1];

/** setTimeout 대역. 예약한 시간을 모두 기록하고 시험이 직접 돌린다 */
function fakeTimers() {
  let next = 0;
  const pending = new Map<number, () => void>();
  const delays: number[] = [];
  return {
    delays,
    get pending() { return pending.size; },
    setTimeout: ((fn: () => void, ms: number) => { next += 1; pending.set(next, fn); delays.push(ms); return next; }) as unknown as typeof setTimeout,
    clearTimeout: ((id: number) => { pending.delete(id); }) as unknown as typeof clearTimeout,
    flush() { const run = [...pending.values()]; pending.clear(); run.forEach((fn) => fn()); }
  };
}

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });

interface Call { url: URL; init: RequestInit }

const BODIES: Record<string, unknown> = {
  '/dashboard/places': { weakRssiDbm: -75, places: [{ placeId: 'lounge', placeName: '공용 라운지', modelVersion: 'm-3', calibrationVersion: 'c-7', devices: [{ nodeId: 'locker-1', name: '사물함 1', capability: 'locker', occupancy: 'rental' }] }] },
  '/dashboard/uses': [{ nodeId: 'locker-1', personId: 'p1', occupancy: 'rental', session: 'ACTIVE', startedAt: AT }],
  '/dashboard/approvals': { pending: [], active: [], ended: [] },
  '/dashboard/audit': [{ id: 'a-1', at: AT, step: '3', text: '근접 통과', code: 'OK' }],
  '/dashboard/history': [],
  '/dashboard/trust': { live: { since: AT, decisions: 0, doubleOccupancy: 0, latencyMedianSec: null, latencyP95Sec: null, counts: { OK: 0, BUSY: 0, ASK: 0, DENY: 0, NEED_APPROVAL: 0 } } },
  '/dashboard/health': { devices: [], beacons: [], server: { database: true, mqtt: true, model: 'm-3' } }
};
const REQUEST_BODY = { id: 'r 1/x', personId: 'p1', tool: 'request_use', target: { kind: 'any' }, action: 'on', at: AT, steps: Array.from({ length: 7 }, () => ({ result: 'pending' })) };
const RENTAL_BODY = { nodeId: 'locker 1', personId: 'p1', startedAt: AT, overdueMinutes: 0, timeline: [] };

/** 조회 대역. 호출을 기록하고, 경로에 맞는 본문을 주거나 handler가 답을 정한다 */
function fakeFetch(handler?: (url: URL, init: RequestInit) => Response | Promise<Response>) {
  const calls: Call[] = [];
  const f = (async (input: string, init: RequestInit) => {
    const url = new URL(input);
    calls.push({ url, init });
    if (handler) return handler(url, init);
    if (url.pathname.startsWith('/dashboard/requests/')) return json(REQUEST_BODY);
    if (url.pathname.startsWith('/dashboard/rentals/')) return json(RENTAL_BODY);
    return json(BODIES[url.pathname]);
  }) as unknown as typeof fetch;
  return { f, calls };
}

function setup(over: Partial<ServerOptions> = {}) {
  FakeSocket.all = [];
  const timers = fakeTimers();
  const fetched = fakeFetch();
  const rejected: string[] = [];
  const source = createServerSource({
    origin: ORIGIN,
    token: TOKEN,
    onRejected: (why) => rejected.push(why),
    fetch: fetched.f,
    WebSocket: FakeSocket as unknown as typeof WebSocket,
    setTimeout: timers.setTimeout,
    clearTimeout: timers.clearTimeout,
    ...over
  });
  return { source, timers, rejected, calls: fetched.calls };
}

/** 구독하고 받은 묶음을 모은다 */
function listen(source: ReturnType<typeof setup>['source']) {
  const bundles: StreamEvent[][] = [];
  const off = source.subscribe((events) => bundles.push(events));
  return { bundles, off };
}
const links = (bundles: StreamEvent[][]) => bundles.flat().filter((e): e is Extract<StreamEvent, { type: 'link' }> => e.type === 'link');

const BEACON = { type: 'beacon', at: AT, personId: 'p1', pseudonym: '방문자 A', letter: 'A', role: 'visitor', seen: true };
const RSSI = { type: 'rssi', at: AT, personId: 'p1', rssi: { 'locker-1': -58 } };

beforeEach(() => { FakeSocket.all = []; });

// ── 조회 REST ───────────────────────────────────────────────────────

describe('서버 어댑터 조회 REST', () => {
  it('FR12A 조회 9개가 경로와 질의 인자를 맞게 쓰고, 메서드는 모두 GET이며 Authorization 헤더를 단다', async () => {
    const { source, calls } = setup();
    await source.places();
    await source.uses();
    await source.approvals();
    await source.audit();
    await source.history({ period: '오늘' });
    await source.history({ period: '오늘 14:26~14:29', verdict: 'NEED_APPROVAL', nodeId: 'locker-2' });
    await source.request('r 1/x');
    await source.trust();
    await source.rental('locker 1');
    await source.health();
    const seen = calls.map((c) => [c.url.pathname, Object.fromEntries(c.url.searchParams)]);
    expect(seen).toEqual([
      ['/dashboard/places', {}],
      ['/dashboard/uses', {}],
      ['/dashboard/approvals', {}],
      ['/dashboard/audit', {}],
      ['/dashboard/history', { period: 'today' }],
      ['/dashboard/history', { period: '14:26~14:29', verdict: 'NEED_APPROVAL', nodeId: 'locker-2' }],
      ['/dashboard/requests/r%201%2Fx', {}],
      ['/dashboard/trust', {}],
      ['/dashboard/rentals/locker%201', {}],
      ['/dashboard/health', {}]
    ]);
    for (const c of calls) {
      expect(c.url.origin).toBe(ORIGIN);
      expect(c.init.method).toBe('GET');
      expect(c.init.body).toBeUndefined();
      expect(c.init.headers).toEqual({ Authorization: `Bearer ${TOKEN}` });
      // 토큰은 주소에 쓰지 않는다
      expect(c.url.toString()).not.toContain(TOKEN);
    }
  });

  it('FR12A origin을 주지 않으면 대시보드를 연 주소를 쓴다', async () => {
    const { source, calls } = setup({ origin: undefined });
    await source.health();
    expect(calls[0].url.origin).toBe(location.origin);
  });

  it('FR12A 응답의 시각을 필드별 형식의 지역 시각으로 바꾼다', async () => {
    const { source } = setup();
    expect(await source.audit()).toEqual([{ id: 'a-1', at: '14:28:10', step: '3', text: '근접 통과', code: 'OK' }]);
    expect((await source.uses())[0].startedAt).toBe('14:28');
    expect((await source.trust()).live.since).toBe('14:28');
    expect((await source.request('r 1/x')).at).toBe('14:28:10');
  });

  it('FR12A 401이면 invalid, 403이면 not_owner로 onRejected를 한 번 부르고 오류로 알린다', async () => {
    for (const [status, why] of [[401, 'invalid'], [403, 'not_owner']] as const) {
      const { source, rejected } = setup({ fetch: fakeFetch(() => json({ error: status === 401 ? 'INVALID_TOKEN' : 'POLICY_DENIED' }, status)).f });
      await expect(source.uses()).rejects.toThrow('토큰이 거부됐어요');
      expect(rejected).toEqual([why]);
    }
  });

  it('FR12A 500이면 상태와 사유 코드를 담은 Error를 던진다. 오류 문구에 토큰이 없다', async () => {
    const { source, rejected } = setup({ fetch: fakeFetch(() => json({ error: 'INTERNAL_ERROR' }, 500)).f });
    const err = await source.places().then(() => undefined, (e: Error) => e);
    expect(err).toBeInstanceOf(Error);
    expect(err?.message).toBe('서버 오류 500 · INTERNAL_ERROR');
    expect(err?.message).not.toContain(TOKEN);
    expect(rejected).toEqual([]);
  });

  it('FR12A 본문이 JSON이 아닌 오류 응답은 상태만 알린다', async () => {
    const { source } = setup({ fetch: fakeFetch(() => new Response('<html>bad gateway</html>', { status: 502 })).f });
    await expect(source.places()).rejects.toThrow(new Error('서버 오류 502'));
  });

  it('FR12A 연결하지 못하면 원문 대신 정해진 문구를 던진다. 브라우저 오류 원문에 토큰이 있어도 쓰지 않는다', async () => {
    const { source } = setup({ fetch: (async () => { throw new TypeError(`Failed to fetch (Authorization: Bearer ${TOKEN})`); }) as unknown as typeof fetch });
    const err = await source.health().then(() => undefined, (e: Error) => e);
    expect(err?.message).toBe('서버에 연결하지 못했어요');
    expect(err?.message).not.toContain(TOKEN);
  });

  it('FR12A 요청·대여가 404이면 화면 문구로 알린다. 다른 조회의 404는 서버 오류다', async () => {
    const { source } = setup({ fetch: fakeFetch(() => json({ error: 'USE_NOT_FOUND' }, 404)).f });
    await expect(source.request('r-9')).rejects.toThrow(new Error('요청 r-9 없음'));
    await expect(source.rental('locker-9')).rejects.toThrow(new Error('대여 locker-9 없음'));
    await expect(source.trust()).rejects.toThrow(new Error('서버 오류 404 · USE_NOT_FOUND'));
  });

  it('FR12A 응답이 JSON이 아니거나 형식이 표와 다르면 Error다. 값은 문구에 쓰지 않는다', async () => {
    const broken = setup({ fetch: fakeFetch(() => new Response('not json', { status: 200 })).f });
    await expect(broken.source.audit()).rejects.toThrow(new Error('서버 응답을 읽지 못했어요'));
    const wrong = setup({ fetch: fakeFetch(() => json({ weakRssiDbm: 'np_owner_tok-123', places: [] })).f });
    await expect(wrong.source.places()).rejects.toThrow(new Error('서버 응답 형식이 맞지 않아요 · places.weakRssiDbm'));
  });

  it('FR12A FR12B 오류가 나도 토큰을 console에 쓰지 않는다', async () => {
    const spies = (['log', 'info', 'warn', 'error', 'debug'] as const).map((m) => vi.spyOn(console, m).mockImplementation(() => undefined));
    const bad = setup({ fetch: fakeFetch(() => json({ error: 'INTERNAL_ERROR' }, 500)).f });
    await bad.source.places().catch(() => undefined);
    const down = setup({ fetch: (async () => { throw new TypeError(`Failed to fetch ${TOKEN}`); }) as unknown as typeof fetch });
    await down.source.uses().catch(() => undefined);
    const denied = setup({ fetch: fakeFetch(() => json({ error: 'INVALID_TOKEN' }, 401)).f });
    await denied.source.audit().catch(() => undefined);
    const live = setup();
    listen(live.source);
    last().open();
    last().message({ v: 1, events: [{ type: 'future_event', at: AT }, 'broken'] });
    last().message('not json{');
    last().message({ v: 2, events: [] });
    const closed = setup();
    listen(closed.source);
    last().open();
    last().drop(4401);
    const logged = spies.flatMap((s) => s.mock.calls).flat().map(String).join('\n');
    expect(logged).not.toContain(TOKEN);
    spies.forEach((s) => s.mockRestore());
  });

  it('FR12A 데이터 소스에는 구독, 다시 연결, 조회 9개만 있고 목 데이터 표시(mock)는 없다', () => {
    const { source } = setup();
    expect(Object.keys(source).sort()).toEqual(['approvals', 'audit', 'health', 'history', 'places', 'reconnect', 'rental', 'request', 'subscribe', 'trust', 'uses']);
    expect(source.mock).toBeUndefined();
  });
});

// ── 모니터링 스트림 ──────────────────────────────────────────────────

describe('서버 어댑터 스트림 연결', () => {
  it('FR12B /dashboard/stream에 ws로 붙고, 열리면 인증 메시지를 가장 먼저 보낸다. 토큰은 주소에 없다', () => {
    const { source } = setup();
    listen(source);
    expect(FakeSocket.all).toHaveLength(1);
    expect(last().url).toBe('ws://dash.test:5173/dashboard/stream');
    expect(last().url).not.toContain(TOKEN);
    expect(last().sent).toEqual([]);
    last().open();
    expect(last().sent).toEqual([JSON.stringify({ type: 'auth', token: TOKEN })]);
    last().message({ v: 1, events: [BEACON] });
    // 인증 말고는 아무것도 보내지 않는다
    expect(last().sent).toHaveLength(1);
  });

  it('FR12B https로 연 대시보드는 wss로 붙는다', () => {
    const { source } = setup({ origin: 'https://dash.example' });
    listen(source);
    expect(last().url).toBe('wss://dash.example/dashboard/stream');
  });

  it('FR12B 열리면 link connected, 끊기면 link disconnected로 알린다 (시각은 HH:MM:SS.mmm)', () => {
    const { source } = setup();
    const { bundles } = listen(source);
    last().open();
    last().drop(1006);
    expect(bundles).toEqual([
      [{ type: 'link', at: expect.stringMatching(/^\d{2}:\d{2}:\d{2}\.\d{3}$/), connected: true, reconnectAttempts: 0 }],
      [{ type: 'link', at: expect.stringMatching(/^\d{2}:\d{2}:\d{2}\.\d{3}$/), connected: false, reconnectAttempts: 1 }]
    ]);
  });

  it('FR12B 서버 메시지 하나가 리스너에 한 번, 한 묶음으로 간다. 이벤트는 화면 모델로 바뀐다', () => {
    const { source } = setup();
    const { bundles } = listen(source);
    last().open();
    bundles.length = 0;
    last().message({ v: 1, events: [BEACON, RSSI] });
    last().message({ v: 1, events: [{ type: 'mcp_result', at: AT, requestId: 'r1' }] });
    expect(bundles).toEqual([
      [
        { type: 'beacon', at: '14:28:10.123', personId: 'p1', pseudonym: '방문자 A', letter: 'A', role: 'visitor', seen: true },
        { type: 'rssi', at: '14:28:10.123', personId: 'p1', rssi: { 'locker-1': -58 } }
      ],
      [{ type: 'mcp_result', at: '14:28:10.123', requestId: 'r1' }]
    ]);
  });

  it('FR12B 지금 상태가 비어 있어도 첫 묶음을 넘긴다', () => {
    const { source } = setup();
    const { bundles } = listen(source);
    last().open();
    bundles.length = 0;
    last().message({ v: 1, events: [] });
    expect(bundles).toEqual([[]]);
  });

  it('FR12B 모르는 이벤트와 형식이 틀린 이벤트는 그 이벤트만 버린다', () => {
    const { source } = setup();
    const { bundles } = listen(source);
    last().open();
    bundles.length = 0;
    last().message({ v: 1, events: [BEACON, { type: 'future_event', at: AT }, { type: 'rssi', at: AT, personId: 'p1', rssi: 'weak' }, null, 7, RSSI] });
    expect(bundles).toHaveLength(1);
    expect(bundles[0].map((e) => e.type)).toEqual(['beacon', 'rssi']);
  });

  it('FR12B JSON이 아니거나 모양이 틀린 메시지는 버리고 연결은 그대로다', () => {
    const { source, timers } = setup();
    const { bundles } = listen(source);
    last().open();
    bundles.length = 0;
    for (const bad of ['not json{', '"text"', '7', 'null', '[]', '{}', '{"v":1}', '{"v":1,"events":"x"}', '{"events":[]}', new ArrayBuffer(4), undefined]) {
      last().message(bad);
      last().onmessage?.({ data: bad });
    }
    expect(bundles).toEqual([]);
    expect(last().closed).toBe(false);
    expect(timers.pending).toBe(0);
  });

  it('FR12B v가 1이 아니면 읽지 않고 연결을 끊고 다시 연결하지 않는다', () => {
    const { source, timers, rejected } = setup();
    const { bundles } = listen(source);
    last().open();
    bundles.length = 0;
    const old = last();
    old.message({ v: 2, events: [BEACON] });
    expect(old.closed).toBe(true);
    expect(old.closeCode).toBe(1000);
    expect(bundles.flat().map((e) => e.type)).toEqual(['link']);
    expect(links(bundles)[0]).toMatchObject({ connected: false });
    // 끊은 소켓의 늦은 닫힘 이벤트로 다시 연결하지 않는다
    old.drop(1000);
    expect(timers.pending).toBe(0);
    expect(FakeSocket.all).toHaveLength(1);
    expect(rejected).toEqual([]);
  });

  it('FR12B v 불일치로 멈춘 뒤 사람이 누른 reconnect()는 새 소켓으로 연결하고, 열리면 인증 메시지를 가장 먼저 보낸다', () => {
    const { source, timers } = setup();
    const { bundles } = listen(source);
    last().open();
    const old = last();
    old.message({ v: 2, events: [BEACON] });
    // 저절로 다시 연결하지 않는다
    expect(FakeSocket.all).toHaveLength(1);
    expect(timers.pending).toBe(0);
    bundles.length = 0;

    source.reconnect();
    expect(FakeSocket.all).toHaveLength(2);
    const fresh = last();
    expect(fresh).not.toBe(old);
    expect(fresh.url).toBe('ws://dash.test:5173/dashboard/stream');
    expect(fresh.sent).toEqual([]);
    fresh.open();
    expect(fresh.sent).toEqual([JSON.stringify({ type: 'auth', token: TOKEN })]);
    expect(links(bundles)).toEqual([{ type: 'link', at: expect.any(String), connected: true, reconnectAttempts: 0 }]);
    // 끊은 소켓의 늦은 닫힘으로 새 연결이 흔들리지 않고, 새 연결의 메시지를 이어서 받는다
    old.drop(1000);
    expect(timers.pending).toBe(0);
    expect(FakeSocket.all).toHaveLength(2);
    fresh.message({ v: 1, events: [BEACON] });
    expect(bundles.at(-1)?.map((e) => e.type)).toEqual(['beacon']);
  });

  it('FR12B v 불일치가 거듭나도 reconnect()를 누를 때마다 새로 연결한다. 열려 있는 연결과 해제한 연결은 건드리지 않는다', () => {
    const { source, timers } = setup();
    const { off } = listen(source);
    last().open();
    // 열려 있는 연결은 그대로 둔다
    source.reconnect();
    expect(FakeSocket.all).toHaveLength(1);
    for (let n = 2; n <= 4; n += 1) {
      last().message({ v: 9, events: [] });
      expect(timers.pending).toBe(0);
      source.reconnect();
      expect(FakeSocket.all).toHaveLength(n);
      last().open();
      // 누르지 않았는데 한 번 더 열리지 않는다
      source.reconnect();
      expect(FakeSocket.all).toHaveLength(n);
    }
    // 멈춘 채 해제하면 reconnect()로 다시 열리지 않는다
    last().message({ v: 9, events: [] });
    off();
    source.reconnect();
    expect(FakeSocket.all).toHaveLength(4);
  });

  it('FR12B 4401·4403으로 멈춘 연결은 reconnect()로도 다시 열지 않는다 (토큰을 지우고 입력 화면으로 갔다)', () => {
    for (const [code, why] of [[4401, 'invalid'], [4403, 'not_owner']] as const) {
      const { source, timers, rejected } = setup();
      listen(source);
      last().open();
      last().drop(code);
      source.reconnect();
      expect(FakeSocket.all, String(code)).toHaveLength(1);
      expect(timers.pending, String(code)).toBe(0);
      expect(rejected).toEqual([why]);
    }
  });

  it('FR12B 4401은 invalid, 4403은 not_owner로 onRejected를 부르고 다시 연결하지 않는다', () => {
    for (const [code, why] of [[4401, 'invalid'], [4403, 'not_owner']] as const) {
      const { source, timers, rejected } = setup();
      const { bundles } = listen(source);
      last().drop(code);
      expect(rejected).toEqual([why]);
      expect(timers.pending).toBe(0);
      expect(timers.delays).toEqual([]);
      expect(FakeSocket.all).toHaveLength(1);
      expect(links(bundles)).toEqual([{ type: 'link', at: expect.any(String), connected: false, reconnectAttempts: 0 }]);
    }
  });

  it.each([1000, 1006, 1011, 1013, 4408])('FR12B 닫기 코드 %i는 1, 2, 4, 8초… 최대 10초로 다시 연결한다', (code) => {
    const { source, timers, rejected } = setup();
    const { bundles } = listen(source);
    last().drop(code);
    for (let n = 1; n < 6; n += 1) {
      timers.flush();
      expect(FakeSocket.all).toHaveLength(n + 1);
      last().drop(code);
    }
    expect(timers.delays).toEqual([1000, 2000, 4000, 8000, 10_000, 10_000]);
    expect(links(bundles).map((l) => [l.connected, l.reconnectAttempts])).toEqual([[false, 1], [false, 2], [false, 3], [false, 4], [false, 5], [false, 6]]);
    expect(rejected).toEqual([]);
  });

  it('FR12B 연결이 열리면 기다리는 시간이 1초로 돌아간다. 다시 연결한 소켓도 인증 메시지를 먼저 보낸다', () => {
    const { source, timers } = setup();
    const { bundles } = listen(source);
    last().drop(1006);
    timers.flush();
    last().drop(1006);
    timers.flush();
    expect(timers.delays).toEqual([1000, 2000]);
    last().open();
    expect(last().sent).toEqual([JSON.stringify({ type: 'auth', token: TOKEN })]);
    expect(links(bundles).at(-1)).toMatchObject({ connected: true, reconnectAttempts: 0 });
    last().drop(1011);
    expect(timers.delays).toEqual([1000, 2000, 1000]);
  });

  it('FR12B 다시 연결한 뒤 서버가 보내는 지금 상태 묶음을 이어서 받는다', () => {
    const { source, timers } = setup();
    const { bundles } = listen(source);
    last().open();
    last().drop(1013);
    timers.flush();
    last().open();
    bundles.length = 0;
    last().message({ v: 1, events: [BEACON] });
    expect(bundles).toEqual([[{ type: 'beacon', at: '14:28:10.123', personId: 'p1', pseudonym: '방문자 A', letter: 'A', role: 'visitor', seen: true }]]);
  });

  it('FR12B reconnect()는 기다리는 재연결을 바로 한다. 연결이 열려 있으면 아무것도 하지 않는다', () => {
    const { source, timers } = setup();
    listen(source);
    last().open();
    source.reconnect();
    expect(FakeSocket.all).toHaveLength(1);
    last().drop(1006);
    expect(timers.pending).toBe(1);
    source.reconnect();
    expect(timers.pending).toBe(0);
    expect(FakeSocket.all).toHaveLength(2);
    // 바로 다시 연결해도 열리기 전까지는 기다리는 시간이 이어서 늘어난다
    last().drop(1006);
    expect(timers.delays).toEqual([1000, 2000]);
  });

  it('FR12B 해제하면 소켓을 닫고, 닫힘·메시지 같은 늦은 이벤트로 다시 연결하거나 리스너를 부르지 않는다', () => {
    const { source, timers } = setup();
    const { bundles, off } = listen(source);
    last().open();
    bundles.length = 0;
    const sock = last();
    off();
    expect(sock.closed).toBe(true);
    sock.message({ v: 1, events: [BEACON] });
    sock.drop(1006);
    sock.open();
    expect(bundles).toEqual([]);
    expect(timers.pending).toBe(0);
    expect(FakeSocket.all).toHaveLength(1);
    expect(sock.sent).toHaveLength(1);
  });

  it('FR12B 다시 연결을 기다리는 중에 해제하면 재연결 타이머가 남지 않는다', () => {
    const { source, timers } = setup();
    const { off } = listen(source);
    last().open();
    last().drop(1006);
    expect(timers.pending).toBe(1);
    off();
    expect(timers.pending).toBe(0);
    timers.flush();
    source.reconnect();
    expect(FakeSocket.all).toHaveLength(1);
  });

  it('FR12B 구독마다 연결이 하나다: 해제한 연결은 닫히고 새 구독은 다른 연결로 받는다 (StrictMode의 두 번 구독)', () => {
    const { source } = setup();
    const first = listen(source);
    const firstSocket = last();
    first.off();
    const second = listen(source);
    const secondSocket = last();
    expect(FakeSocket.all).toHaveLength(2);
    expect(firstSocket.closed).toBe(true);
    expect(secondSocket.closed).toBe(false);
    secondSocket.open();
    firstSocket.message({ v: 1, events: [RSSI] });
    secondSocket.message({ v: 1, events: [BEACON] });
    expect(first.bundles).toEqual([]);
    expect(second.bundles.flat().map((e) => e.type)).toEqual(['link', 'beacon']);
  });

  it('FR12B reconnect()는 열려 있는 모든 구독의 기다리는 재연결을 한다', () => {
    const { source, timers } = setup();
    listen(source);
    listen(source);
    FakeSocket.all.forEach((s) => s.drop(1006));
    expect(timers.pending).toBe(2);
    source.reconnect();
    expect(timers.pending).toBe(0);
    expect(FakeSocket.all).toHaveLength(4);
  });

  it('FR12B 리스너가 받는 것에 토큰이 없다', () => {
    const { source } = setup();
    const { bundles } = listen(source);
    last().open();
    last().message({ v: 1, events: [BEACON, { type: 'beacon', at: AT, personId: 'p2', pseudonym: '방문자 B', role: 'visitor', seen: false, token: TOKEN }] });
    last().drop(1006);
    expect(JSON.stringify(bundles)).not.toContain(TOKEN);
  });
});

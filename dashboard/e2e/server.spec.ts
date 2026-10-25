// FR-12A FR-12B: ?source=server — 토큰을 넣으면 가짜 서버(경로 가로채기)의 지금 상태와 이벤트로 무대·레일·패널이 그려지고, 토큰은 주소와 화면에 없다
// 가짜 서버의 메시지는 docs/architecture.md §5 "대시보드 조회", "WebSocket"의 키 그대로다 (서버 api/ws/schema.py)
import { expect, test, type Page, type Request, type WebSocketRoute } from '@playwright/test';
import { inspect } from './inspect';

// 서버 시각은 UTC로 오고 화면은 이 PC의 지역 시각을 쓴다. 시험은 서울(UTC+9)로 고정한다.
test.use({ timezoneId: 'Asia/Seoul' });

const TOKEN = 'np_owner_e2e-7f3a91c4';
const CLEAN = { pageScroll: [], innerScroll: [], textOverflow: [], ellipsized: [], clipped: [], smallTargets: [], inlineStyle: 0, smallFont: [], overlaps: [], lowContrast: [], missingComponent: 0 };

/** 서버가 보내는 시각 (UTC 05:mm:ss = 서울 14:mm:ss) */
const at = (min: number, sec: number, ms = 0) => `2026-10-24T05:${String(min).padStart(2, '0')}:${String(sec).padStart(2, '0')}.${String(ms).padStart(3, '0')}+00:00`;

const PLACES = {
  weakRssiDbm: -75,
  places: [{
    placeId: 'lounge', placeName: '공용 라운지', modelVersion: 'm-3', calibrationVersion: 'c-7',
    devices: [
      { nodeId: 'locker-1', name: '사물함 1', capability: 'locker', occupancy: 'rental' },
      { nodeId: 'locker-2', name: '사물함 2', capability: 'locker', occupancy: 'rental' },
      { nodeId: 'light-1', name: '조명 1', capability: 'light', occupancy: 'shared' }
    ]
  }]
};
const USE_B = { nodeId: 'locker-2', personId: 'p-2', occupancy: 'rental', session: 'ACTIVE', startedAt: at(20, 0) };
const USE_A = { nodeId: 'locker-1', personId: 'p-1', occupancy: 'rental', session: 'ACTIVE', startedAt: at(28, 11, 120) };
const AUDIT_OLD = { id: 'a-1', at: at(28, 9), step: '3', nodeId: 'locker-1', text: '근접 통과', code: 'OK' };
const AUDIT_EXEC = { id: 'a-2', at: at(28, 11, 160), step: 'exec', nodeId: 'locker-1', text: '열림 명령 성공', code: 'OK' };
const TRUST = { live: { since: at(0, 0), decisions: 1, doubleOccupancy: 0, latencyMedianSec: 0.21, latencyP95Sec: null, counts: { OK: 1, BUSY: 0, ASK: 0, DENY: 0, NEED_APPROVAL: 0 } } };

/** 연결 직후 서버가 보내는 지금 상태: 방문자 둘과 이미 사용 중인 사물함 2 */
const NOW = [
  { type: 'beacon', at: at(28, 0), personId: 'p-1', pseudonym: '방문자 A', letter: 'A', role: 'visitor', seen: true },
  { type: 'rssi', at: at(28, 0), personId: 'p-1', rssi: { 'locker-1': -52, 'locker-2': -72, 'light-1': -80 } },
  { type: 'beacon', at: at(28, 0), personId: 'p-2', pseudonym: '방문자 B', letter: 'B', role: 'visitor', seen: true },
  { type: 'rssi', at: at(28, 0), personId: 'p-2', rssi: { 'locker-1': -74, 'locker-2': -50, 'light-1': -78 } },
  { type: 'transition', at: at(20, 0), nodeId: 'locker-2', to: 'ACTIVE' },
  { type: 'audit', at: at(28, 9), row: AUDIT_OLD }
];

/** 사물함 열림 흐름: 요청이 닿고(방문자 A, 가까운 사물함 열기), 판정 단계를 지나, 허가, 명령, 실행 완료, 응답 */
const CALL = { type: 'mcp_call', at: at(28, 10, 100), requestId: 'req-1', personId: 'p-1', tool: 'request_use', target: { kind: 'nearest', capability: 'locker' }, action: 'open' };
const JUDGING = [
  ...['0', '1', '2', '3', '4', '5', '6'].map((n, i) => ({ type: 'step', at: at(28, 10, 150 + i * 20), requestId: 'req-1', step: n, result: 'pass' })),
  { type: 'posterior', at: at(28, 10, 300), requestId: 'req-1', posteriors: { 'locker-1': 0.93, 'locker-2': 0.04 }, threshold: 0.8 },
  { type: 'verdict', at: at(28, 10, 400), requestId: 'req-1', verdict: 'OK', reason: 'OK', nodeId: 'locker-1' },
  { type: 'command', at: at(28, 10, 450), requestId: 'req-1', nodeId: 'locker-1', action: 'open' }
];
const DONE = [
  { type: 'exec_result', at: at(28, 11, 100), requestId: 'req-1', nodeId: 'locker-1', result: 'succeeded' },
  { type: 'transition', at: at(28, 11, 120), nodeId: 'locker-1', to: 'ACTIVE' },
  { type: 'mcp_result', at: at(28, 11, 150), requestId: 'req-1' },
  { type: 'audit', at: at(28, 11, 160), row: AUDIT_EXEC }
];

interface Options {
  /** 모든 조회에 이 상태로 답한다 */
  restStatus?: 401 | 403;
  /** 인증 메시지를 받으면 이 코드로 스트림을 닫는다 */
  streamClose?: number;
}

/** 가짜 서버. 조회는 page.route, 스트림은 page.routeWebSocket으로 받는다 */
async function fakeServer(page: Page, options: Options = {}) {
  let socket: WebSocketRoute | undefined;
  const fake = {
    options,
    /** 받은 조회: 경로(질의 포함)와 Authorization 헤더 */
    rest: [] as { path: string; auth: string | undefined }[],
    /** 스트림 연결 수와 연결마다 받은 메시지 */
    connections: 0,
    streamIn: [] as unknown[],
    /** 서버에 있는 점유와 감사 로그 (시험이 흐름에 맞춰 바꾼다) */
    uses: [USE_B] as unknown[],
    audit: [AUDIT_OLD] as unknown[],
    /** 장소 조회가 이렇게 실패한다 (시험이 바꾼다). error는 서버 오류(500), abort는 서버에 닿지 못함 */
    placesFailure: undefined as 'error' | 'abort' | undefined,
    /** 열려 있는 스트림에 이벤트 한 묶음을 보낸다 */
    send(events: unknown[]) { socket!.send(JSON.stringify({ v: 1, events })); },
    /** 열려 있는 스트림을 서버가 닫는다 */
    close(code: number) { socket!.close({ code, reason: 'test' }); }
  };

  await page.route('**/dashboard/**', async (route) => {
    const url = new URL(route.request().url());
    const auth = route.request().headers().authorization;
    fake.rest.push({ path: url.pathname + url.search, auth });
    if (options.restStatus) return route.fulfill({ status: options.restStatus, json: { error: options.restStatus === 401 ? 'INVALID_TOKEN' : 'POLICY_DENIED' } });
    if (auth !== `Bearer ${TOKEN}`) return route.fulfill({ status: 401, json: { error: 'INVALID_TOKEN' } });
    if (url.pathname === '/dashboard/places' && fake.placesFailure === 'abort') return route.abort('connectionrefused');
    if (url.pathname === '/dashboard/places' && fake.placesFailure === 'error') return route.fulfill({ status: 500, json: { error: 'INTERNAL_ERROR' } });
    const bodies: Record<string, unknown> = {
      '/dashboard/places': PLACES, '/dashboard/uses': fake.uses, '/dashboard/approvals': { pending: [], active: [], ended: [] },
      '/dashboard/audit': fake.audit, '/dashboard/history': [], '/dashboard/trust': TRUST,
      '/dashboard/health': { devices: [], beacons: [], server: { database: true, mqtt: true, model: 'm-3' } }
    };
    return url.pathname in bodies ? route.fulfill({ json: bodies[url.pathname] }) : route.fulfill({ status: 404, json: { error: 'USE_NOT_FOUND' } });
  });

  await page.routeWebSocket('**/dashboard/stream', (ws) => {
    socket = ws;
    fake.connections += 1;
    ws.onMessage((message) => {
      const msg = JSON.parse(String(message)) as { type?: string; token?: string };
      fake.streamIn.push(msg);
      if (options.streamClose) { ws.close({ code: options.streamClose, reason: 'test' }); return; }
      if (msg.type === 'auth' && msg.token === TOKEN) fake.send(NOW);
      else ws.close({ code: 4401, reason: 'invalid token' });
    });
  });
  return fake;
}

const gate = (page: Page) => page.locator('[data-component="TokenGate"]');
const enterToken = async (page: Page, token = TOKEN) => {
  await gate(page).getByLabel('토큰').fill(token);
  await gate(page).getByRole('button', { name: '연결' }).click();
};
const stage = (page: Page) => page.locator('[data-component="Stage"]');
const device = (page: Page, name: string) => page.locator('[data-component="StageDevice"]').filter({ hasText: name });

const writes: string[] = [];
test.beforeEach(async ({ page }) => {
  writes.length = 0;
  page.on('request', (r: Request) => { if (r.method() !== 'GET') writes.push(`${r.method()} ${r.url()}`); });
  await page.setViewportSize({ width: 1920, height: 1080 });
});
test.afterEach(() => { expect(writes, 'GET 말고 다른 요청이 없어야 한다').toEqual([]); });

test('FR12A FR12B ?source=server는 토큰 입력 화면부터 연다. 토큰을 넣기 전에는 서버에 아무것도 부르지 않는다', async ({ page }) => {
  const fake = await fakeServer(page);
  await page.goto('/?source=server&theme=dark');
  await expect(gate(page)).toBeVisible();
  await expect(gate(page).locator('input')).toHaveAttribute('type', 'password');
  await expect(gate(page).getByRole('alert')).toHaveCount(0);
  await expect(page.locator('[data-component="TopBar"]')).toHaveCount(0);
  expect(fake.rest).toEqual([]);
  expect(fake.connections).toBe(0);
});

for (const theme of ['dark', 'light']) {
  test(`FR12A FR12B 토큰 입력 화면은 ${theme} 테마에서 넘침·겹침·작은 누르는 자리·대비 문제가 없다`, async ({ page }) => {
    await fakeServer(page);
    for (const [w, h] of [[1920, 1080], [1280, 720]]) {
      await page.setViewportSize({ width: w, height: h });
      await page.goto(`/?source=server&theme=${theme}`);
      await expect(gate(page)).toBeVisible();
      await page.evaluate(() => document.fonts.ready);
      const r = await inspect(page);
      expect(r, `${w}×${h}`).toEqual({ ...r, ...CLEAN });
    }
  });
}

test('FR12A FR12B 토큰을 넣으면 가짜 서버의 지금 상태와 사물함 열림 흐름이 무대·레일·패널에 그려지고, 토큰은 주소와 화면에 없다', async ({ page }) => {
  const fake = await fakeServer(page);
  const logs: string[] = [];
  const crashes: string[] = [];
  page.on('console', (m) => logs.push(m.text()));
  page.on('pageerror', (e) => crashes.push(e.message));
  await page.goto('/?source=server&theme=dark');
  await enterToken(page);

  // 지금 상태: 기기 셋과 사람 둘이 무대에 서고, 사용 중인 사물함 2가 보인다
  await expect(stage(page)).toBeVisible();
  await expect(page.locator('.np-topbar__live')).toContainText('실시간 연결됨');
  // 마지막으로 받은 이벤트의 서버 시각(UTC 05:28:09)이 서울 시각으로 보인다
  await expect(page.locator('.np-topbar__recv')).toHaveText('수신 14:28:09');
  await expect(page.locator('[data-component="StageDevice"]')).toHaveCount(3);
  await expect(device(page, '사물함 2').locator('.np-stage__device-status')).toHaveText('사용 중');
  await expect(device(page, '사물함 1').locator('.np-stage__device-status')).toHaveText('사용 가능');
  await expect(page.locator('[data-component="StageVisitor"]').filter({ hasText: '방문자 A' })).toHaveAttribute('data-on', 'true');
  await expect(page.locator('[data-component="StageVisitor"]').filter({ hasText: '방문자 B' })).toHaveAttribute('data-on', 'true');
  await expect(page.locator('.np-rail__title')).toHaveText('요청 대기');
  // 서버 데이터에는 목 데이터 표시가 없다
  await expect(page.locator('[data-component="TopBar"]')).not.toContainText('목 데이터');
  await expect(page.locator('[data-component="OfflineBanner"]')).toHaveCount(0);

  // 사물함 열림 흐름
  fake.send([CALL]);
  fake.send(JUDGING);
  await expect(page.locator('.np-rail__title')).toHaveText('방문자 A · 사물함 열기');
  fake.uses = [USE_A, USE_B];
  fake.audit = [AUDIT_EXEC, AUDIT_OLD];
  fake.send(DONE);
  const lastStep = page.locator('[data-component="FlowStep"]').nth(4);
  await expect(lastStep.locator('.np-step__mark')).toHaveText('완료', { timeout: 15_000 });
  await expect(lastStep.locator('.np-step__detail')).toContainText('열렸어요');
  await expect(device(page, '사물함 1').locator('.np-stage__device-status')).toHaveText('사용 중');
  await expect(page.locator('.np-topbar__recv')).toHaveText('수신 14:28:11');

  // 패널: 점유·대여 현황, 감사 로그, 신뢰도 지표
  const side = page.locator('[data-component="SideMenu"]');
  const panel = page.locator('[data-component="Panel"]');
  await side.getByRole('button', { name: '점유·대여 현황' }).click();
  const uses = panel.getByRole('table', { name: '점유·대여 목록' }).locator('tbody tr');
  await expect(uses).toHaveCount(2);
  await expect(uses.filter({ hasText: '사물함 1' })).toContainText('방문자 A');
  // 시작 시각은 서울 시각 HH:MM이다
  await expect(uses.filter({ hasText: '사물함 1' })).toContainText('14:28');
  await expect(uses.filter({ hasText: '사물함 2' })).toContainText('방문자 B');
  await expect(uses.filter({ hasText: '사물함 2' })).toContainText('14:20');

  await side.getByRole('button', { name: '감사 로그' }).click();
  const audit = panel.getByRole('table', { name: '감사 로그' }).locator('tbody tr');
  await expect(audit).toHaveCount(2);
  await expect(audit.first()).toContainText('14:28:11');
  await expect(audit.first()).toContainText('열림 명령 성공');
  await expect(audit.last()).toContainText('14:28:09');

  await side.getByRole('button', { name: '신뢰도 지표' }).click();
  await expect(panel).toContainText('MON-10');
  await expect(panel.getByText('평가 리포트 없음')).toBeVisible();
  await expect(panel.getByRole('table', { name: '인수 기준 지표' })).toHaveCount(0);
  // 평가 리포트가 없는 신뢰도 패널도 넘침·겹침·대비 문제가 없다 (도착 강조가 끝난 뒤에 잰다)
  await page.evaluate(() => Promise.all(document.getAnimations().filter((a) => a.effect?.getTiming().iterations !== Infinity).map((a) => a.finished.catch(() => undefined))));
  const looks = await inspect(page);
  expect(looks).toEqual({ ...looks, ...CLEAN });

  // 인증 메시지가 스트림의 첫 메시지이고 보낸 것의 전부다. 조회에는 Authorization 헤더만 쓴다
  expect(fake.connections).toBe(1);
  expect(fake.streamIn).toEqual([{ type: 'auth', token: TOKEN }]);
  expect(fake.rest.length).toBeGreaterThan(0);
  expect(fake.rest.every((r) => r.auth === `Bearer ${TOKEN}`)).toBe(true);
  expect(fake.rest.map((r) => r.path)).toContain('/dashboard/places');
  expect(fake.rest.map((r) => r.path)).toContain('/dashboard/trust');

  // 토큰은 주소, 화면 글자, DOM, 장기 저장소 어디에도 없다
  expect(page.url()).not.toContain(TOKEN);
  expect(fake.rest.some((r) => r.path.includes(TOKEN))).toBe(false);
  expect(await page.locator('body').innerText()).not.toContain(TOKEN);
  expect(await page.content()).not.toContain(TOKEN);
  expect(await page.evaluate(() => JSON.stringify({ ...localStorage }))).not.toContain(TOKEN);
  expect(logs.join('\n')).not.toContain(TOKEN);
  expect(crashes).toEqual([]);
});

test('FR12A FR12B 같은 탭에서 다시 열면 저장한 토큰으로 입력 화면 없이 연결한다', async ({ page }) => {
  const fake = await fakeServer(page);
  await page.goto('/?source=server');
  await enterToken(page);
  await expect(stage(page)).toBeVisible();
  await page.reload();
  await expect(stage(page)).toBeVisible();
  await expect(gate(page)).toHaveCount(0);
  expect(fake.connections).toBe(2);
  expect(fake.streamIn).toEqual([{ type: 'auth', token: TOKEN }, { type: 'auth', token: TOKEN }]);
});

/**
 * 시계를 직접 다룬다: 페이지를 열기 전에 가짜 시계를 깔고, 연결이 끝난 뒤 멈춘다.
 * 멈춘 시계에서는 자동 재연결(첫 대기 1초)이 실제 시간에 밀려 시험이 보려는 순서보다 먼저 일어나지 않는다.
 */
async function connectedWithFrozenClock(page: Page) {
  await page.clock.install();
  await page.goto('/?source=server');
  await enterToken(page);
  await expect(stage(page)).toBeVisible();
  await page.clock.pauseAt(Date.now() + 1000);
}

/** 페이지가 만든 WebSocket 수를 페이지 안에서 센다. fakeServer 다음에 불러야 가짜 서버의 대역을 감싼다 */
async function countSockets(page: Page) {
  await page.addInitScript(() => {
    let count = 0;
    Object.defineProperty(window, '__sockets', { get: () => count });
    window.WebSocket = new Proxy(window.WebSocket, { construct(target, args, newTarget) { count += 1; return Reflect.construct(target, args, newTarget); } });
  });
  return () => page.evaluate(() => (window as unknown as { __sockets: number }).__sockets);
}

test('FR12B 스트림이 끊기면 끊김을 알리고, 다시 연결 버튼을 누르면 기다리지 않고 바로 다시 연결해 지금 상태를 다시 받는다', async ({ page }) => {
  const fake = await fakeServer(page);
  await connectedWithFrozenClock(page);
  await expect(device(page, '사물함 2').locator('.np-stage__device-status')).toHaveText('사용 중');

  fake.close(1013);
  const banner = page.locator('[data-component="OfflineBanner"]');
  await expect(banner).toContainText('서버와 연결이 끊겼어요');
  await expect(page.locator('[data-component="TopBar"]')).not.toContainText('목 데이터');
  // 시계가 멈춰 있으니 자동 재연결은 아직이다. 누르면 바로 다시 연결한다
  expect(fake.connections).toBe(1);
  await banner.getByRole('button', { name: '다시 연결' }).click();
  await expect(banner).toHaveCount(0);
  expect(fake.connections).toBe(2);
  expect(fake.streamIn).toEqual([{ type: 'auth', token: TOKEN }, { type: 'auth', token: TOKEN }]);
  // 다시 연결하면 서버가 보낸 지금 상태로 이어 간다
  await expect(device(page, '사물함 2').locator('.np-stage__device-status')).toHaveText('사용 중');
});

test('FR12B 스트림이 끊기면 끊김을 알리고, 버튼을 누르지 않아도 첫 재연결 대기(1초)가 지나면 다시 연결한다. 끊기기 전에 진행 중이던 요청이 지금 상태에 없으면 사라진다', async ({ page }) => {
  const fake = await fakeServer(page);
  await connectedWithFrozenClock(page);
  // 사물함 열림 요청이 진행 중이다: 판정이 났고 명령을 보냈다 (실행 결과는 아직 없다). 판정까지 한 묶음이라 판정 게이지가 늦추지 않는다
  fake.send([CALL, ...JUDGING]);
  const railTitle = page.locator('.np-rail__title');
  await expect(railTitle).toHaveText('방문자 A · 사물함 열기');
  await expect(page.locator('[data-component="FlowStep"]').nth(3).locator('.np-step__mark')).toHaveText('실행 중');

  fake.close(1013);
  const banner = page.locator('[data-component="OfflineBanner"]');
  await expect(banner).toContainText('서버와 연결이 끊겼어요');
  // 끊기기만 했을 때는 마지막 화면을 그대로 둔다. 시계가 멈춰 있으니 자동 재연결도 아직이다
  await expect(railTitle).toHaveText('방문자 A · 사물함 열기');
  expect(fake.connections).toBe(1);

  // 첫 재연결 대기(1초)가 지나면 다시 연결한다. 서버가 보낸 지금 상태에는 그 요청이 없으니 사라진다
  await page.clock.runFor(1000);
  await expect(banner).toHaveCount(0);
  expect(fake.connections).toBe(2);
  expect(fake.streamIn).toEqual([{ type: 'auth', token: TOKEN }, { type: 'auth', token: TOKEN }]);
  await expect(railTitle).toHaveText('요청 대기');
  await expect(device(page, '사물함 2').locator('.np-stage__device-status')).toHaveText('사용 중');
});

test('FR12B 스트림이 4401로 닫히면 다시 연결하지 않고 토큰을 지운 뒤 이유와 함께 입력 화면으로 돌아간다', async ({ page }) => {
  const fake = await fakeServer(page, { streamClose: 4401 });
  const sockets = await countSockets(page);
  await page.clock.install();
  await page.goto('/?source=server');
  await enterToken(page);
  await expect(gate(page).getByRole('alert')).toContainText('토큰이 올바르지 않아요');
  await expect(stage(page)).toHaveCount(0);
  expect(await page.evaluate(() => sessionStorage.length)).toBe(0);
  expect(page.url()).not.toContain(TOKEN);
  // 다시 연결하지 않는다. 재연결을 예약했다면 첫 대기(1초)가 지나는 이때 소켓이 늘어난다 (센 수가 1인 것으로 센 자리가 맞는지 먼저 본다)
  expect(await sockets()).toBe(1);
  await page.clock.runFor(10_000);
  expect(await sockets()).toBe(1);
  expect(fake.connections).toBe(1);
  await expect(gate(page).locator('input')).toHaveValue('');

  // 올바른 토큰을 다시 넣으면 연결된다
  fake.options.streamClose = undefined;
  await enterToken(page);
  await expect(stage(page)).toBeVisible();
  await expect(gate(page)).toHaveCount(0);
  expect(fake.connections).toBe(2);
});

test('FR12B 스트림이 4403으로 닫히면 소유자 토큰이 아니라는 이유를 보인다', async ({ page }) => {
  await fakeServer(page, { streamClose: 4403 });
  await page.goto('/?source=server');
  await enterToken(page);
  await expect(gate(page).getByRole('alert')).toContainText('소유자 토큰이 아니에요');
  expect(await page.evaluate(() => sessionStorage.length)).toBe(0);
});

const placesError = (page: Page) => page.locator('[data-component="PlacesError"]');
const placesCalls = (fake: Awaited<ReturnType<typeof fakeServer>>) => fake.rest.filter((r) => r.path === '/dashboard/places').length;

for (const [name, failure, reason] of [
  ['서버 오류(500)', 'error', '서버 오류 500 · INTERNAL_ERROR'],
  ['서버에 닿지 못함(연결 실패)', 'abort', '서버에 연결하지 못했어요']
] as const) {
  test(`FR12A 처음 장소 조회가 ${name}이면 불러오는 중에 멈추지 않고 실패 이유와 다시 시도를 보인다. 누르면 다시 조회해 화면을 그린다`, async ({ page }) => {
    const fake = await fakeServer(page);
    fake.placesFailure = failure;
    const logs: string[] = [];
    page.on('console', (m) => logs.push(m.text()));
    await page.goto('/?source=server&theme=dark');
    await enterToken(page);

    await expect(placesError(page)).toBeVisible();
    await expect(placesError(page).getByRole('heading')).toHaveText('장소를 불러오지 못했어요');
    await expect(placesError(page).getByRole('alert')).toHaveText(reason);
    await expect(page.getByText('불러오는 중…')).toHaveCount(0);
    await expect(stage(page)).toHaveCount(0);
    // 장소를 받기 전에는 스트림을 열지 않는다. 토큰 입력 화면으로 돌아가지도 않는다
    expect(fake.connections).toBe(0);
    await expect(gate(page)).toHaveCount(0);
    expect(await page.evaluate(() => sessionStorage.length)).toBeGreaterThan(0);
    // 토큰은 주소·화면·DOM·콘솔에 없다
    expect(page.url()).not.toContain(TOKEN);
    expect(await page.locator('body').innerText()).not.toContain(TOKEN);
    expect(await page.content()).not.toContain(TOKEN);
    expect(logs.join('\n')).not.toContain(TOKEN);

    // 서버가 돌아오면 다시 시도로 장소를 다시 조회해 화면을 그린다
    fake.placesFailure = undefined;
    const before = placesCalls(fake);
    await placesError(page).getByRole('button', { name: '다시 시도' }).click();
    await expect(stage(page)).toBeVisible();
    await expect(placesError(page)).toHaveCount(0);
    expect(placesCalls(fake)).toBe(before + 1);
    await expect(page.locator('[data-component="StageDevice"]')).toHaveCount(3);
    expect(fake.connections).toBe(1);
    expect(page.url()).not.toContain(TOKEN);
  });
}

test('FR12A 다시 시도해도 장소 조회가 실패하면 실패 화면에 머물고, 서버가 돌아온 뒤 누르면 그린다', async ({ page }) => {
  const fake = await fakeServer(page);
  fake.placesFailure = 'error';
  await page.goto('/?source=server');
  await enterToken(page);
  await expect(placesError(page).getByRole('alert')).toHaveText('서버 오류 500 · INTERNAL_ERROR');
  const first = placesCalls(fake);
  // 서버가 이번에는 닿지 못한다: 새 이유로 바뀐다
  fake.placesFailure = 'abort';
  await placesError(page).getByRole('button', { name: '다시 시도' }).click();
  await expect(placesError(page).getByRole('alert')).toHaveText('서버에 연결하지 못했어요');
  expect(placesCalls(fake)).toBe(first + 1);
  expect(fake.connections).toBe(0);
  fake.placesFailure = undefined;
  await placesError(page).getByRole('button', { name: '다시 시도' }).click();
  await expect(stage(page)).toBeVisible();
});

for (const theme of ['dark', 'light']) {
  test(`FR12A 장소 조회 실패 화면은 ${theme} 테마에서 넘침·겹침·작은 누르는 자리·대비 문제가 없다`, async ({ page }) => {
    const fake = await fakeServer(page);
    fake.placesFailure = 'error';
    await page.goto(`/?source=server&theme=${theme}`);
    await enterToken(page);
    await expect(placesError(page)).toBeVisible();
    await page.evaluate(() => document.fonts.ready);
    for (const [w, h] of [[1920, 1080], [1280, 720]]) {
      await page.setViewportSize({ width: w, height: h });
      const r = await inspect(page);
      expect(r, `${w}×${h}`).toEqual({ ...r, ...CLEAN });
    }
  });
}

test('FR12A 조회가 401이면 토큰을 지우고 입력 화면으로 돌아간다. 403이면 소유자 토큰이 아니라고 알린다', async ({ page }) => {
  const fake = await fakeServer(page, { restStatus: 401 });
  await page.goto('/?source=server');
  await enterToken(page);
  await expect(gate(page).getByRole('alert')).toContainText('토큰이 올바르지 않아요');
  expect(await page.evaluate(() => sessionStorage.length)).toBe(0);
  // 거부된 토큰으로는 스트림도 열지 않는다
  expect(fake.connections).toBe(0);

  fake.options.restStatus = 403;
  await enterToken(page, 'np_visitor_e2e-other');
  await expect(gate(page).getByRole('alert')).toContainText('소유자 토큰이 아니에요');
  expect(page.url()).not.toContain('np_visitor_e2e-other');
});

// FR-12A · D1 · D2 · D7: 패널은 무대 위에 겹쳐 열리고 ×로 닫히며, 화면 동작은 서버에 쓰지 않는다
import { expect, test, type Page, type Request } from '@playwright/test';
import { ready } from './inspect';

const box = (page: Page, sel: string) => page.locator(sel).evaluate((e) => { const b = e.getBoundingClientRect(); return [b.x, b.y, b.width, b.height]; });
const writes: string[] = [];
const track = (page: Page) => page.on('request', (r: Request) => { if (r.method() !== 'GET') writes.push(`${r.method()} ${r.url()}`); });

test.beforeEach(async ({ page }) => { writes.length = 0; track(page); await page.setViewportSize({ width: 1920, height: 1080 }); });
test.afterEach(() => { expect(writes, 'GET 말고 다른 요청이 없어야 한다').toEqual([]); });

const PANELS: [string, string][] = [['점유·대여 현황', 'MON-06'], ['승인 현황', 'MON-07'], ['감사 로그', 'MON-08'], ['요청·판정 이력', 'MON-09'], ['신뢰도 지표', 'MON-10']];

test('FR12A D1 메뉴로 패널을 열고 ×로 닫는다. 무대 크기와 위치는 그대로다', async ({ page }) => {
  await ready(page, 'scene=later');
  const before = await box(page, '.np-monitor > .np-stage');
  for (const [label, id] of PANELS) {
    await page.getByRole('button', { name: '메뉴' }).click();
    await expect(page.locator('[data-component="PanelMenu"]')).toHaveAttribute('data-open', 'true');
    await page.getByRole('menuitem', { name: label }).click();
    await expect(page.locator('[data-component="PanelMenu"]')).toHaveAttribute('data-open', 'false');
    const panel = page.locator('[data-component="Panel"]');
    await expect(panel).toContainText(id);
    expect(await box(page, '.np-monitor > .np-stage')).toEqual(before);
    await page.getByRole('button', { name: '패널 닫기' }).click();
    await expect(panel).toHaveCount(0);
  }
});

test('FR12A 이력에서 사람을 누르면 그 요청의 판정 단계를 연다', async ({ page }) => {
  await ready(page, 'scene=later&panel=history');
  await page.getByRole('row', { name: /14:22/ }).getByRole('button').click();
  const panel = page.locator('[data-component="Panel"]');
  await expect(panel).toContainText('MON-04');
  await expect(panel).toContainText('바쁨');
  await expect(panel.locator('.np-steps__item[data-result="fail"]')).toHaveCount(1);
});

test('FR12A 이력 찾기는 판정으로 거른다', async ({ page }) => {
  await ready(page, 'scene=later&panel=history');
  await page.locator('label', { hasText: '판정' }).locator('select').selectOption('OK');
  await page.getByRole('button', { name: '찾기' }).click();
  await expect(page.getByRole('table', { name: '요청·판정 이력' }).locator('tbody tr')).toHaveCount(5);
});

test('FR12A 이력은 한 쪽에 6건씩 최근 것부터 보인다', async ({ page }) => {
  await ready(page, 'scene=later&panel=history');
  const rows = page.getByRole('table', { name: '요청·판정 이력' }).locator('tbody tr');
  await expect(rows).toHaveCount(6);
  await expect(rows.first()).toContainText('14:30');
  await page.getByRole('button', { name: '이전' }).click();
  await expect(rows).toHaveCount(6);
  await page.getByRole('button', { name: '이전' }).click();
  await expect(rows).toHaveCount(1);
  await expect(rows.last()).toContainText('13:21');
});

test('FR12A 감사 로그는 단계로 거른다', async ({ page }) => {
  await ready(page, 'scene=later&panel=audit');
  await page.getByRole('button', { name: 'fault', exact: true }).click();
  const rows = page.getByRole('table', { name: '감사 로그' }).locator('tbody tr');
  await expect(rows).toHaveCount(1);
  await expect(rows).toContainText('EXEC_UNKNOWN');
});

test('FR12A 점유 현황에서 기기를 누르면 기기 상세를 연다', async ({ page }) => {
  await ready(page, 'scene=later&panel=uses');
  await page.getByRole('button', { name: /사물함 3/ }).click();
  const panel = page.locator('[data-component="Panel"]');
  await expect(panel).toContainText('MON-05');
  await expect(panel).toContainText('20분 초과');
});

test('FR12A D2 고장 격리 상세에는 복구 버튼이 없다', async ({ page }) => {
  await ready(page, 'scene=fault');
  const panel = page.locator('[data-component="Panel"]');
  await expect(panel).toContainText('스크립트');
  await expect(panel.getByRole('button')).toHaveCount(1); // 닫기만
});

test('FR12B D1 ASK·승인 필요·고장 격리는 상세 패널이 자동으로 열린다', async ({ page }) => {
  for (const [scene, text] of [['ask', 'ASK'], ['approval', 'NEED_APPROVAL'], ['fault', 'FAULT']]) {
    await ready(page, `scene=${scene}`);
    await expect(page.locator('[data-component="Panel"] .np-panel__title')).toContainText(text);
  }
  await ready(page, 'scene=flow');
  await expect(page.locator('[data-component="Panel"]')).toHaveCount(0);
});

test('FR12A D7 설정 화면을 오가고 자동 전환 스위치는 이 PC에만 저장한다', async ({ page }) => {
  await ready(page, 'scene=later');
  await page.getByRole('button', { name: '메뉴' }).click();
  await page.getByRole('menuitem', { name: '폰 연결' }).click();
  const sw = page.getByRole('switch', { name: /요청한 사람/ });
  await expect(sw).toHaveAttribute('aria-checked', 'true');
  await sw.click();
  await expect(sw).toHaveAttribute('aria-checked', 'false');
  expect(await page.evaluate(() => JSON.parse(localStorage.getItem('nearpilot-dashboard')!).follow.followRequester)).toBe(false);
  await page.getByRole('button', { name: /^점검/ }).click();
  await expect(page.locator('[data-component="ReadyBanner"]')).toContainText('확인할 것 3건');
  await expect(page.locator('.np-crow[data-state="bad"]')).toContainText('사물함 2');
  await page.getByRole('button', { name: '모니터로 돌아가기' }).click();
  await expect(page.locator('[data-component="MonitorLayout"]')).toHaveCount(1);
});

test('FR12A D7 테마를 바꾸고 다시 열어도 유지한다', async ({ page }) => {
  await ready(page, 'scene=idle');
  await expect(page.locator('.np-app')).toHaveAttribute('data-theme', 'dark');
  await page.getByRole('button', { name: '라이트 모드로 바꾸기' }).click();
  await expect(page.locator('.np-app')).toHaveAttribute('data-theme', 'light');
  await ready(page, 'scene=idle');
  await expect(page.locator('.np-app')).toHaveAttribute('data-theme', 'light');
});

test('FR12B 연결 끊김에서 다시 연결하면 실시간 화면으로 돌아온다', async ({ page }) => {
  await ready(page, 'scene=offline');
  await expect(page.getByRole('status').filter({ hasText: '연결 끊김' })).toBeVisible();
  await expect(page.locator('[data-component="OfflineBanner"]')).toContainText('마지막 수신 14:25:40');
  await page.getByRole('button', { name: '다시 연결' }).click();
  await expect(page.locator('[data-component="OfflineBanner"]')).toHaveCount(0);
  await expect(page.getByText('실시간 연결됨')).toBeVisible();
});

test('FR12B 재생 모드는 장면을 차례로 바꾼다', async ({ page }) => {
  await page.clock.install();
  await ready(page, 'scene=play');
  await expect(page.locator('.np-rail__title')).toHaveText('요청을 기다리고 있어요');
  await page.clock.runFor(6100);
  await expect(page.locator('.np-rail__title')).toHaveText('방문자 A · 사물함 열기');
  await expect(page.locator('.np-panel__title')).toContainText('ASK');
  // 다음 요청으로 바뀌면 이전 요청의 패널은 닫힌다
  await page.clock.runFor(6100);
  await expect(page.locator('.np-rail__title')).toHaveText('방문자 A · 사물함 열기');
  await expect(page.locator('.np-rail__meta')).toContainText('14:28:10');
  await expect(page.locator('[data-component="FlowRail"] [data-status="current"]')).toContainText('확인 중');
  await expect(page.locator('[data-component="Panel"]')).toHaveCount(0);
});

// D4: 무대 배치는 설정에서 끌어다 놓아 정하고, 이 PC에만 저장한다
const device = (page: Page, name: string) => page.locator(`[data-component="StageDevice"][aria-label^="${name} 옮기기"]`);
const spotOf = async (page: Page, name: string) => device(page, name).getAttribute('transform');
/** 무대 SVG 좌표를 화면 좌표로 */
const toScreen = (page: Page, x: number, y: number) => page.locator('.np-stage__svg').evaluate((svg: SVGSVGElement, p) => {
  const m = svg.getScreenCTM()!;
  const q = new DOMPoint(p.x, p.y).matrixTransform(m);
  return { x: q.x, y: q.y };
}, { x, y });

test('FR12A D4 배치 화면에서 기기를 끌어 옮기면 이 PC에 저장되고 모니터 무대도 따라간다', async ({ page }) => {
  await ready(page, 'scene=later&view=layout');
  expect(await spotOf(page, '사물함 1')).toBe('translate(456 120)');
  const from = await toScreen(page, 456, 120);
  const to = await toScreen(page, 1032, 120);
  await page.mouse.move(from.x, from.y);
  await page.mouse.down();
  await page.mouse.move(to.x, to.y, { steps: 8 });
  await page.mouse.up();
  expect(await spotOf(page, '사물함 1')).toBe('translate(1032 120)');

  // 겹치는 자리에는 놓지 않는다
  const busy = await toScreen(page, 792, 120);
  const back = await toScreen(page, 1032, 120);
  await page.mouse.move(back.x, back.y);
  await page.mouse.down();
  await page.mouse.move(busy.x, busy.y, { steps: 8 });
  await page.mouse.up();
  expect(await spotOf(page, '사물함 1')).toBe('translate(1032 120)');

  // 화살표 키로 한 칸씩
  await device(page, '사물함 2').focus();
  await page.keyboard.press('ArrowDown');
  expect(await spotOf(page, '사물함 2')).toBe('translate(624 144)');
  // 이웃 기기에 막히면 그 너머의 첫 빈자리로 건너뛴다 (사물함 3, 사물함 1 너머)
  await page.keyboard.press('ArrowRight');
  expect(await spotOf(page, '사물함 2')).toBe('translate(1200 144)');

  await ready(page, 'scene=later');
  await expect(page.locator('[data-component="StageDevice"]').filter({ hasText: '사물함 1' })).toHaveAttribute('transform', 'translate(1032 120)');

  await ready(page, 'scene=later&view=layout');
  await page.getByRole('button', { name: '기본 배치로' }).click();
  expect(await spotOf(page, '사물함 1')).toBe('translate(456 120)');
  expect(writes).toEqual([]);
});

test('FR12A D4 배치를 파일로 내보내고 다시 불러온다', async ({ page }) => {
  await ready(page, 'scene=later&view=layout');
  const [download] = await Promise.all([page.waitForEvent('download'), page.getByRole('button', { name: '파일로 내보내기' }).click()]);
  const file = JSON.parse(await (await download.createReadStream()).toArray().then((c) => Buffer.concat(c).toString('utf8')));
  expect(file).toMatchObject({ kind: 'nearpilot-arrangement', version: 1, place: '공용 라운지' });
  file.devices['locker-1'] = { x: 1176, y: 120 };
  await page.locator('input[type="file"]').setInputFiles({ name: 'arr.json', mimeType: 'application/json', buffer: Buffer.from(JSON.stringify(file)) });
  await expect(page.getByRole('status').filter({ hasText: '자리를 불러왔어요' })).toBeVisible();
  expect(await spotOf(page, '사물함 1')).toBe('translate(1176 120)');
  await page.locator('input[type="file"]').setInputFiles({ name: 'bad.json', mimeType: 'application/json', buffer: Buffer.from('{') });
  await expect(page.getByRole('status').filter({ hasText: 'JSON 파일이 아니에요' })).toBeVisible();
  // 한 대라도 놓을 수 없는 자리면 아무것도 바꾸지 않는다
  file.devices['locker-1'] = { x: 456, y: 120 };
  file.devices['locker-2'] = { x: 456, y: 120 };
  await page.locator('input[type="file"]').setInputFiles({ name: 'clash.json', mimeType: 'application/json', buffer: Buffer.from(JSON.stringify(file)) });
  await expect(page.getByRole('status').filter({ hasText: '배치는 그대로예요' })).toBeVisible();
  expect(await spotOf(page, '사물함 1')).toBe('translate(1176 120)');
});

test('FR12A 점검은 장면의 비콘 수신과 연결 끊김을 따르고, 빈 감사 로그는 비었다고 말한다', async ({ page }) => {
  // B의 비콘은 14:23~14:28:30에 잡히지 않는다
  await ready(page, 'scene=fault&view=check');
  await expect(page.locator('.np-crow').filter({ hasText: '방문자 B' })).not.toContainText('최근 신호 없음');
  await ready(page, 'scene=offline&view=check');
  await expect(page.locator('.np-crow').filter({ hasText: '방문자 B' })).toContainText('최근 신호 없음');
  await expect(page.getByRole('status').filter({ hasText: '서버와 연결이 끊겼어요' })).toBeVisible();
  await ready(page, 'scene=idle&panel=audit');
  await expect(page.getByText('아직 기록이 없어요')).toBeVisible();
  await expect(page.locator('.np-table__empty')).toHaveText('없어요');
});

test('FR12B 실시간 흐름 모드는 이벤트가 도착하는 순서대로 화면을 바꾼다', async ({ page }) => {
  await page.clock.install();
  await ready(page, 'scene=live');
  await expect(page.locator('.np-rail__title')).toHaveText('요청을 기다리고 있어요');
  await expect(page.locator('.np-topbar__mock')).toHaveText('목 데이터');
  // 무대 장면과 레일 3단계 표시를 0.2초마다 적는다
  const seen: string[] = [];
  const stage = page.locator('[data-component="Stage"]');
  const labels = () => page.locator('.np-stage__signal-label').allTextContents();
  // ASK 결과가 보이는 동안 신호 세기가 계속 들어와 바뀐다
  let askLabels: string[] | undefined;
  let rssiChanged = false;
  for (let i = 0; i < 200 && !seen.includes('flow'); i++) {
    await page.clock.runFor(200);
    const scene = await stage.getAttribute('data-scene');
    if (seen.at(-1) !== scene) seen.push(scene!);
    if (scene === 'ask') {
      const now = await labels();
      if (!askLabels) askLabels = now;
      else if (now.join() !== askLabels.join()) rssiChanged = true;
    }
  }
  expect(rssiChanged).toBe(true);
  // 자동으로 열린 ASK 상세는 다음 요청이 오면 닫힌다
  await expect(page.locator('[data-component="Panel"]')).toHaveCount(0);
  // 레일에서 진행 중인 요청의 상세를 연다
  await page.getByRole('button', { name: '요청 상세' }).click();
  await expect(page.locator('.np-panel__title')).toContainText('사물함 열기');
  // 대기 → 판정 중 → ASK → (내려감) 대기 → 판정 중 → 실행
  expect(seen).toEqual(['idle', 'judging', 'ask', 'idle', 'judging', 'flow']);
  await expect(page.locator('.np-rail__meta')).toContainText('14:28:10');
  await page.clock.runFor(60000);
  await expect(page.locator('.np-rail__title')).toHaveText('방문자 B · 조명 켜기');
  await expect(page.locator('.np-topbar__recv')).toContainText('수신 14:3');
  expect(writes).toEqual([]);
});

test('FR12B 판정 중 요청 상세는 단계가 도착하는 대로 채워진다', async ({ page }) => {
  await ready(page, 'scene=judging');
  await page.getByRole('button', { name: '요청 상세' }).click();
  await expect(page.locator('[data-component="Panel"] [data-component="VerdictTag"]')).toContainText('판정 중');
  await expect(page.locator('.np-steps__item[data-result="pending"]')).toHaveCount(5);
});

test('FR12B 흐름 중에 메뉴로 연 목록 패널은 자동 패널로 바뀌지 않는다', async ({ page }) => {
  await page.clock.install();
  await ready(page, 'scene=live&panel=audit');
  await expect(page.locator('.np-panel__title')).toHaveText('감사 로그');
  await page.clock.runFor(90000);
  await expect(page.locator('.np-rail__title')).toHaveText('방문자 B · 조명 켜기');
  await expect(page.locator('.np-panel__title')).toHaveText('감사 로그');
});

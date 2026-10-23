// FR-12A · D1 · D2 · D7: 패널은 무대 위에 겹쳐 열리고 ×로 닫히며, 화면 동작은 서버에 쓰지 않는다
import { expect, test, type Page, type Request } from '@playwright/test';
import { ready } from './inspect';
import { iso } from '../src/view/iso';

const box = (page: Page, sel: string) => page.locator(sel).evaluate((e) => { const b = e.getBoundingClientRect(); return [b.x, b.y, b.width, b.height]; });
const writes: string[] = [];
const track = (page: Page) => page.on('request', (r: Request) => { if (r.method() !== 'GET') writes.push(`${r.method()} ${r.url()}`); });

test.beforeEach(async ({ page }) => { writes.length = 0; track(page); await page.setViewportSize({ width: 1920, height: 1080 }); });
test.afterEach(() => { expect(writes, 'GET 말고 다른 요청이 없어야 한다').toEqual([]); });

const PANELS: [string, string][] = [['점유·대여 현황', 'MON-06'], ['승인 현황', 'MON-07'], ['감사 로그', 'MON-08'], ['요청·판정 이력', 'MON-09'], ['신뢰도 지표', 'MON-10']];

test('FR12A D1 사이드 메뉴로 패널을 열고, 같은 항목이나 ×로 닫는다. 무대 크기와 위치는 그대로다', async ({ page }) => {
  await ready(page, 'scene=later');
  const before = await box(page, '.np-monitor > .np-stage');
  const side = page.locator('[data-component="SideMenu"]');
  const panel = page.locator('[data-component="Panel"]');
  for (const [label, id] of PANELS) {
    await side.getByRole('button', { name: label }).click();
    await expect(panel).toContainText(id);
    await expect(side.getByRole('button', { name: label })).toHaveAttribute('aria-current', 'page');
    expect(await box(page, '.np-monitor > .np-stage')).toEqual(before);
    await page.getByRole('button', { name: '패널 닫기' }).click();
    await expect(panel).toHaveCount(0);
    await expect(side.locator('[aria-current="page"]')).toHaveCount(0);
  }
  // 같은 항목을 다시 누르면 닫히고, 다른 항목을 누르면 바로 바뀐다
  await side.getByRole('button', { name: '감사 로그' }).click();
  await side.getByRole('button', { name: '요청·판정 이력' }).click();
  await expect(panel).toContainText('MON-09');
  await side.getByRole('button', { name: '요청·판정 이력' }).click();
  await expect(panel).toHaveCount(0);
  // 패널이 사이드 메뉴를 가리지 않는다
  await side.getByRole('button', { name: '신뢰도 지표' }).click();
  const [px, , pw] = await box(page, '[data-component="Panel"]');
  const [sx] = await box(page, '[data-component="SideMenu"]');
  expect(px + pw).toBeLessThanOrEqual(sx + 0.5);
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
  await page.getByRole('button', { name: '설정' }).click();
  await expect(page.getByRole('tab', { name: /^배치/ })).toHaveAttribute('aria-selected', 'true');
  await page.getByRole('tab', { name: /^폰 연결/ }).click();
  const sw = page.getByRole('switch', { name: /요청한 사람/ });
  await expect(sw).toHaveAttribute('aria-checked', 'true');
  await sw.click();
  await expect(sw).toHaveAttribute('aria-checked', 'false');
  expect(await page.evaluate(() => JSON.parse(localStorage.getItem('nearpilot-dashboard')!).follow.followRequester)).toBe(false);
  await page.getByRole('tab', { name: /^점검/ }).click();
  await expect(page.locator('[data-component="ReadyBanner"]')).toContainText('확인할 것 3건');
  await expect(page.locator('.np-crow[data-state="bad"]')).toContainText('사물함 2');
  // 설정 화면에는 사이드 메뉴가 없고, 설정 닫기로 모니터에 돌아간다
  await expect(page.locator('[data-component="SideMenu"]')).toHaveCount(0);
  await page.getByRole('button', { name: '설정 닫기' }).click();
  await expect(page.locator('[data-component="MonitorLayout"]')).toHaveCount(1);
  // 설정을 다시 열면 마지막에 본 탭으로 간다
  await page.getByRole('button', { name: '설정' }).click();
  await expect(page.getByRole('tab', { name: /^점검/ })).toHaveAttribute('aria-selected', 'true');
  // Esc로도 닫는다
  await page.keyboard.press('Escape');
  await expect(page.locator('[data-component="MonitorLayout"]')).toHaveCount(1);
});

test('FR12B 무대를 휠로 확대하고 끌어 옮기고, 맞춤으로 되돌린다. 다시 열어도 유지한다', async ({ page }) => {
  await ready(page, 'scene=flow');
  const svg = page.locator('.np-monitor .np-stage__svg');
  const vb = async () => (await svg.getAttribute('viewBox'))!.split(' ').map(Number);
  expect(await vb()).toEqual([0, 0, 1400, 800]);
  await page.mouse.move(1000, 400);
  await page.mouse.wheel(0, -400);
  const [, , w1] = await vb();
  expect(w1).toBeLessThan(1400);
  const [x1] = await vb();
  await page.mouse.move(900, 500);
  await page.mouse.down();
  await page.mouse.move(700, 500, { steps: 4 });
  await page.mouse.up();
  const [x2] = await vb();
  expect(x2).toBeGreaterThan(x1);
  // 끌어도 글자가 선택되지 않는다
  expect(await page.evaluate(() => String(getSelection()))).toBe('');
  await page.reload();
  await page.waitForSelector('[data-component="TopBar"]');
  expect((await vb())[2]).toBeCloseTo(w1);
  await page.getByRole('button', { name: '무대 전체 보기' }).click();
  expect(await vb()).toEqual([0, 0, 1400, 800]);
  await page.getByRole('button', { name: '확대' }).click();
  expect((await vb())[2]).toBeCloseTo(1400 / 1.25);
});

test('FR12A 폰 화면 폭, 패널 폭, 흐름 레일 높이를 끌어 바꾸고, 다시 열어도 유지한다', async ({ page }) => {
  await ready(page, 'scene=later&panel=uses');
  const width = (sel: string) => page.locator(sel).evaluate((e) => Math.round(e.getBoundingClientRect().width));
  const height = (sel: string) => page.locator(sel).evaluate((e) => Math.round(e.getBoundingClientRect().height));
  const drag = async (sel: string, dx: number, dy: number) => {
    const b = (await page.locator(sel).boundingBox())!;
    await page.mouse.move(b.x + b.width / 2, b.y + b.height / 2);
    await page.mouse.down();
    await page.mouse.move(b.x + b.width / 2 + dx, b.y + b.height / 2 + dy, { steps: 4 });
    await page.mouse.up();
  };
  const phone = await width('.np-monitor > .np-phone'), panel = await width('.np-panel'), rail = await height('.np-monitor > .np-rail');
  await drag('.np-split[data-edge="phone"]', 80, 0);
  await drag('.np-split[data-edge="panel"]', -100, 0);
  await drag('.np-split[data-edge="rail"]', 0, -40);
  expect(await width('.np-monitor > .np-phone')).toBeCloseTo(phone + 80, -1);
  expect(await width('.np-panel')).toBeCloseTo(panel + 100, -1);
  expect(await height('.np-monitor > .np-rail')).toBeCloseTo(rail + 40, -1);
  // 키보드로도 바꾸고, 두 번 누르면 기본 크기로 돌아간다
  await page.locator('.np-split[data-edge="phone"]').focus();
  await page.keyboard.press('ArrowRight');
  expect(await width('.np-monitor > .np-phone')).toBeCloseTo(phone + 96, -1);
  await page.reload();
  await page.waitForSelector('[data-component="TopBar"]');
  expect(await width('.np-monitor > .np-phone')).toBeCloseTo(phone + 96, -1);
  expect(await height('.np-monitor > .np-rail')).toBeCloseTo(rail + 40, -1);
  await page.locator('.np-split[data-edge="phone"]').dblclick();
  expect(await width('.np-monitor > .np-phone')).toBeCloseTo(phone, -1);
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
  await expect(page.locator('.np-rail__title')).toHaveText('요청 대기');
  await page.clock.runFor(6100);
  await expect(page.locator('.np-rail__title')).toHaveText('방문자 A · 사물함 열기');
  await expect(page.locator('.np-panel__title')).toContainText('ASK');
  // 다음 요청으로 바뀌면 이전 요청의 패널은 닫힌다
  await page.clock.runFor(6100);
  await expect(page.locator('.np-rail__title')).toHaveText('방문자 A · 사물함 열기');
  await expect(page.locator('.np-rail__meta')).toContainText('14:28:10');
  await expect(page.locator('[data-component="Panel"]')).toHaveCount(0);
});

// D4: 무대 배치는 설정에서 끌어다 놓아 정하고, 이 PC에만 저장한다
const device = (page: Page, name: string) => page.locator(`[data-component="StageDevice"][aria-label^="${name} 옮기기"]`);
const spotOf = async (page: Page, name: string) => device(page, name).getAttribute('data-spot');
/** 평면 배치 좌표의 바닥 자리를 화면 좌표로 (무대는 아이소메트릭으로 투영해 그린다) */
const toScreen = (page: Page, x: number, y: number) => page.locator('.np-stage__svg').evaluate((svg: SVGSVGElement, p) => {
  const m = svg.getScreenCTM()!;
  const q = new DOMPoint(p.x, p.y).matrixTransform(m);
  return { x: q.x, y: q.y };
}, iso({ x, y }));

test('FR12A D4 배치 화면에서 기기를 끌어 옮기면 이 PC에 저장되고 모니터 무대도 따라간다', async ({ page }) => {
  await ready(page, 'scene=later&view=layout');
  expect(await spotOf(page, '사물함 1')).toBe('456 120');
  const from = await toScreen(page, 456, 120);
  const to = await toScreen(page, 1032, 120);
  await page.mouse.move(from.x, from.y);
  await page.mouse.down();
  await page.mouse.move(to.x, to.y, { steps: 8 });
  await page.mouse.up();
  expect(await spotOf(page, '사물함 1')).toBe('1032 120');

  // 겹치는 자리에는 놓지 않는다
  const busy = await toScreen(page, 792, 120);
  const back = await toScreen(page, 1032, 120);
  await page.mouse.move(back.x, back.y);
  await page.mouse.down();
  await page.mouse.move(busy.x, busy.y, { steps: 8 });
  await page.mouse.up();
  expect(await spotOf(page, '사물함 1')).toBe('1032 120');

  // 화살표 키로 한 칸씩
  await device(page, '사물함 2').focus();
  await page.keyboard.press('ArrowDown');
  expect(await spotOf(page, '사물함 2')).toBe('624 144');
  // 이웃 기기에 막히면 그 너머의 첫 빈자리로 건너뛴다 (사물함 3, 사물함 1 너머)
  await page.keyboard.press('ArrowRight');
  expect(await spotOf(page, '사물함 2')).toBe('1200 144');

  await ready(page, 'scene=later');
  await expect(page.locator('[data-component="StageDevice"]').filter({ hasText: '사물함 1' })).toHaveAttribute('data-spot', '1032 120');

  await ready(page, 'scene=later&view=layout');
  await page.getByRole('button', { name: '기본 배치로' }).click();
  expect(await spotOf(page, '사물함 1')).toBe('456 120');
  expect(writes).toEqual([]);
});

test('FR12A D4 배치를 파일로 내보내고 다시 불러온다', async ({ page }) => {
  await ready(page, 'scene=later&view=layout');
  const [download] = await Promise.all([page.waitForEvent('download'), page.getByRole('button', { name: '파일로 내보내기' }).click()]);
  const file = JSON.parse(await (await download.createReadStream()).toArray().then((c) => Buffer.concat(c).toString('utf8')));
  expect(file).toMatchObject({ kind: 'nearpilot-arrangement', version: 1, place: 'lounge', placeName: '공용 라운지' });
  file.devices['locker-1'] = { x: 1176, y: 120 };
  await page.locator('input[type="file"]').setInputFiles({ name: 'arr.json', mimeType: 'application/json', buffer: Buffer.from(JSON.stringify(file)) });
  await expect(page.getByRole('status').filter({ hasText: '자리를 불러왔어요' })).toBeVisible();
  expect(await spotOf(page, '사물함 1')).toBe('1176 120');
  await page.locator('input[type="file"]').setInputFiles({ name: 'bad.json', mimeType: 'application/json', buffer: Buffer.from('{') });
  await expect(page.getByRole('status').filter({ hasText: 'JSON 파일이 아니에요' })).toBeVisible();
  // 한 대라도 놓을 수 없는 자리면 아무것도 바꾸지 않는다
  file.devices['locker-1'] = { x: 456, y: 120 };
  file.devices['locker-2'] = { x: 456, y: 120 };
  await page.locator('input[type="file"]').setInputFiles({ name: 'clash.json', mimeType: 'application/json', buffer: Buffer.from(JSON.stringify(file)) });
  await expect(page.getByRole('status').filter({ hasText: '배치는 그대로예요' })).toBeVisible();
  expect(await spotOf(page, '사물함 1')).toBe('1176 120');
});

test('FR12A D4 배치 화면의 장소 탭: 옆방 탭에서 무드등을 옮기면 옆방 배치로만 저장되고 라운지 배치는 그대로다', async ({ page }) => {
  await ready(page, 'scene=later&view=layout');
  const tabs = page.getByRole('tablist', { name: '장소' });
  await expect(tabs.getByRole('tab', { name: '공용 라운지' })).toHaveAttribute('aria-selected', 'true');
  await tabs.getByRole('tab', { name: '옆방' }).click();
  await expect(tabs.getByRole('tab', { name: '옆방' })).toHaveAttribute('aria-selected', 'true');
  // 옆방의 배치: 기기는 무드등 하나
  await expect(page.locator('[data-component="StageDevice"]')).toHaveCount(1);
  const before = await spotOf(page, '무드등');
  await device(page, '무드등').focus();
  await page.keyboard.press('ArrowLeft');
  const after = await spotOf(page, '무드등');
  expect(after).not.toBe(before);
  const [x, y] = after!.split(' ').map(Number);
  const saved = await page.evaluate(() => [localStorage.getItem('nearpilot-arrangement:side-room'), localStorage.getItem('nearpilot-arrangement:lounge')]);
  expect(JSON.parse(saved[0]!)).toMatchObject({ kind: 'nearpilot-arrangement', place: 'side-room', devices: { 'mood-1': { x, y } } });
  expect(saved[1], '라운지 배치는 저장하지 않았다').toBeNull();
  // 내보내기는 고른 장소의 배치다
  const [download] = await Promise.all([page.waitForEvent('download'), page.getByRole('button', { name: '파일로 내보내기' }).click()]);
  const file = JSON.parse(await (await download.createReadStream()).toArray().then((c) => Buffer.concat(c).toString('utf8')));
  expect(file).toMatchObject({ place: 'side-room', placeName: '옆방', devices: { 'mood-1': { x, y } } });
  // 라운지 탭의 배치는 그대로다. 마지막에 본 탭은 설정을 닫았다 다시 열어도 남는다.
  await tabs.getByRole('tab', { name: '공용 라운지' }).click();
  expect(await spotOf(page, '사물함 1')).toBe('456 120');
  await tabs.getByRole('tab', { name: '옆방' }).click();
  await page.getByRole('button', { name: '설정 닫기' }).click();
  await page.locator('[data-component="SideMenu"]').getByRole('button', { name: '설정' }).click();
  await expect(page.getByRole('tablist', { name: '장소' }).getByRole('tab', { name: '옆방' })).toHaveAttribute('aria-selected', 'true');
  // 모니터로 돌아가 라운지를 고르면 라운지 무대는 기본 배치다
  await page.getByRole('button', { name: '설정 닫기' }).click();
  await page.locator('[data-component="TopBar"]').getByRole('button', { name: '공용 라운지' }).click();
  await expect(page.locator('[data-component="StageDevice"]').filter({ hasText: '사물함 1' })).toHaveAttribute('data-spot', '456 120');
  expect(writes).toEqual([]);
});

test('FR12A D4 옆방 탭의 파일 불러오기와 기본 배치로는 옆방에만 적용되고 라운지 배치는 그대로다', async ({ page }) => {
  await ready(page, 'scene=later&view=layout');
  const tabs = page.getByRole('tablist', { name: '장소' });
  const input = page.locator('input[type="file"]');
  const file = (place: string, devices: object) => ({ name: `${place}.json`, mimeType: 'application/json', buffer: Buffer.from(JSON.stringify({ kind: 'nearpilot-arrangement', version: 1, place, devices })) });
  const saved = () => page.evaluate(() => [localStorage.getItem('nearpilot-arrangement:side-room'), localStorage.getItem('nearpilot-arrangement:lounge')]);
  // 라운지 탭에서 사물함 1을 옮겨 둔다
  await input.setInputFiles(file('lounge', { 'locker-1': { x: 1032, y: 120 } }));
  await expect(page.getByRole('status').filter({ hasText: '기기 1대의 자리를 불러왔어요' })).toBeVisible();
  expect(await spotOf(page, '사물함 1')).toBe('1032 120');

  await tabs.getByRole('tab', { name: '옆방' }).click();
  expect(await spotOf(page, '무드등')).toBe('456 720');
  await input.setInputFiles(file('side-room', { 'mood-1': { x: 600, y: 240 } }));
  await expect(page.getByRole('status').filter({ hasText: '기기 1대의 자리를 불러왔어요' })).toBeVisible();
  expect(await spotOf(page, '무드등')).toBe('600 240');
  expect(JSON.parse((await saved())[0]!)).toMatchObject({ place: 'side-room', devices: { 'mood-1': { x: 600, y: 240 } } });
  // 라운지 파일의 기기는 옆방에 없어 아무것도 바꾸지 않는다
  await input.setInputFiles(file('lounge', { 'locker-1': { x: 456, y: 120 } }));
  await expect(page.getByRole('status').filter({ hasText: '지금 장소의 기기가 없어요' })).toBeVisible();
  expect(await spotOf(page, '무드등')).toBe('600 240');

  // 기본 배치로: 옆방만 되돌린다
  await page.getByRole('button', { name: '기본 배치로' }).click();
  expect(await spotOf(page, '무드등')).toBe('456 720');
  const [side, lounge] = await saved();
  expect(side, '옆방의 저장한 배치는 지웠다').toBeNull();
  expect(JSON.parse(lounge!)).toMatchObject({ place: 'lounge', devices: { 'locker-1': { x: 1032, y: 120 } } });
  await tabs.getByRole('tab', { name: '공용 라운지' }).click();
  expect(await spotOf(page, '사물함 1')).toBe('1032 120');
  expect(writes).toEqual([]);
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
  await expect(page.locator('.np-rail__title')).toHaveText('요청 대기');
  await expect(page.locator('.np-topbar__mock')).toHaveText('목 데이터');
  // 무대 장면과 레일 3단계 표시를 0.2초마다 적는다
  const seen: string[] = [];
  const stage = page.locator('[data-component="Stage"]');
  const labels = () => page.locator('.np-stage__signal-label').allTextContents();
  // ASK 결과가 보이는 동안 신호 세기가 계속 들어와 바뀐다
  let askLabels: string[] | undefined;
  let rssiChanged = false;
  for (let i = 0; i < 400 && !seen.includes('flow'); i++) {
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
  await untilTitle(page, '방문자 B · 조명 켜기');
  await expect(page.locator('.np-topbar__recv')).toContainText('수신 14:');
  expect(writes).toEqual([]);
});

/** 흐름 모드에서 레일 제목이 나올 때까지 시계를 1초씩 돌린다 */
async function untilTitle(page: Page, title: string) {
  const rail = page.locator('.np-rail__title');
  for (let i = 0; i < 200 && (await rail.textContent()) !== title; i++) await page.clock.runFor(1000);
  await expect(rail).toHaveText(title);
}

test('FR12B 판정 중 요청 상세는 판정 중이고 단계는 기다림이다', async ({ page }) => {
  await ready(page, 'scene=judging');
  await page.getByRole('button', { name: '요청 상세' }).click();
  await expect(page.locator('[data-component="Panel"] [data-component="VerdictTag"]')).toContainText('판정 중');
  await expect(page.locator('.np-steps__item[data-result="pending"]')).toHaveCount(7);
  await expect(page.locator('.np-stage__gauge-fill')).toHaveCount(1);
});

test('FR12B 흐름 중에 사이드 메뉴로 연 목록 패널은 자동 패널로 바뀌지 않는다', async ({ page }) => {
  await page.clock.install();
  await ready(page, 'scene=live&panel=audit');
  await expect(page.locator('.np-panel__title')).toHaveText('감사 로그');
  await untilTitle(page, '방문자 B · 조명 켜기');
  await expect(page.locator('.np-panel__title')).toHaveText('감사 로그');
});

const MAC = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36';
const WIN = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36';
// 공유 창 대신 정한 결과를 차례로 돌려주고 호출 횟수를 센다. 결과가 바닥나면 캔버스 영상을 준다.
const fakeShare = (page: Page, results: [string, string][]) => page.addInitScript((queue) => {
  const w = window as unknown as { __shares: number };
  w.__shares = 0;
  navigator.mediaDevices.getDisplayMedia = async () => {
    w.__shares++;
    const next = queue.shift();
    if (next) throw new DOMException(next[1], next[0]);
    return document.createElement('canvas').captureStream();
  };
  // 운영체제 설정 주소는 테스트 브라우저에서 열지 않는다
  document.addEventListener('click', (e) => { if ((e.target as Element).closest?.('a[href^="x-apple"], a[href^="ms-settings"]')) e.preventDefault(); }, true);
}, results);
const shares = (page: Page) => page.evaluate(() => (window as unknown as { __shares: number }).__shares);

test.describe('미러링 권한 안내 macOS', () => {
  test.use({ userAgent: MAC });
  test('FR12A D7 처음에는 권한 안내를 먼저 띄우고, 설정 열기 뒤에는 창 고르기를 앞세우며, 브라우저를 다시 열어도 이어진다', async ({ page }) => {
    await fakeShare(page, []);
    await ready(page, 'view=phones');
    const guide = page.locator('[data-component="MirrorGuide"]');
    await page.getByRole('button', { name: '미러링 창 고르기' }).first().click();
    await expect(guide).toContainText('화면 공유 권한이 필요해요');
    expect(await shares(page)).toBe(0);
    const open = guide.getByRole('link', { name: '설정 열기' });
    await expect(open).toHaveAttribute('href', /Privacy_ScreenCapture/);
    await expect(open).toBeFocused();
    await open.click();
    await expect(guide).toContainText('권한을 켰으면 창을 골라요');
    await expect(guide.getByRole('button', { name: '창 고르기' })).toBeFocused();

    // 권한을 켜고 브라우저를 다시 열면 모니터로 열어도 폰 연결 탭에서 안내가 이어진다
    await ready(page, 'scene=idle');
    await expect(guide).toContainText('권한을 켰으면 창을 골라요');
    await guide.getByRole('button', { name: '창 고르기' }).click();
    await expect(guide).toHaveCount(0);
    await expect(page.locator('[data-component="PhoneCard"]').first()).toContainText('미러링 연결됨');
    expect(await shares(page)).toBe(1);

    // 한 번 성공했으면 다음부터는 바로 공유 창을 연다
    await ready(page, 'view=phones');
    await page.getByRole('button', { name: '미러링 창 고르기' }).first().click();
    await expect(page.locator('[data-component="PhoneCard"]').first()).toContainText('미러링 연결됨');
    await expect(guide).toHaveCount(0);
  });
});

test.describe('미러링 권한 안내 Windows', () => {
  test.use({ userAgent: WIN });
  test('FR12A D7 바로 공유 창을 열고, 운영체제가 막으면 권한 안내를, 화면을 못 잡으면 카드 안내를 띄우며, 취소는 안내하지 않는다', async ({ page }) => {
    await fakeShare(page, [['NotAllowedError', 'Permission denied by system'], ['NotReadableError', 'Could not start video source'], ['NotAllowedError', 'Permission denied']]);
    await ready(page, 'view=phones');
    const guide = page.locator('[data-component="MirrorGuide"]');
    const cards = page.locator('[data-component="PhoneCard"]');
    await cards.nth(1).getByRole('button', { name: '미러링 창 고르기' }).click();
    await expect(guide).toContainText('아직 화면 공유 권한이 꺼져 있어요');
    await expect(guide).toContainText('Windows 설정');
    await expect(guide.getByRole('link', { name: '설정 열기' })).toHaveAttribute('href', 'ms-settings:privacy');
    expect(await shares(page)).toBe(1);
    // Esc는 안내만 닫고 설정 화면은 그대로다
    await page.keyboard.press('Escape');
    await expect(guide).toHaveCount(0);
    await expect(page.locator('.np-stabs')).toBeVisible();

    await cards.nth(1).getByRole('button', { name: '미러링 창 고르기' }).click();
    await expect(cards.nth(1).locator('[data-component="MirrorNotice"]')).toContainText('고른 화면을 잡지 못했어요');
    await cards.nth(1).getByRole('button', { name: '미러링 창 고르기' }).click();
    await expect(page.locator('[data-component="MirrorNotice"]')).toHaveCount(0);
    await expect(guide).toHaveCount(0);
    expect(await shares(page)).toBe(3);
  });
});

test.describe('미러링 신호', () => {
  test.use({ userAgent: WIN });
  test('FR12A 폰을 뽑아 미러링 창이 검은 화면만 보내면 신호 없음을 띄우고, 화면이 다시 들어오면 영상으로 돌아온다', async ({ page }) => {
    // 폰 화면처럼 그리다가 window.__unplug가 켜지면 검은 화면만 보내는 캔버스 영상
    await page.addInitScript(() => {
      const w = window as unknown as { __unplug: boolean };
      w.__unplug = false;
      navigator.mediaDevices.getDisplayMedia = async () => {
        const c = document.createElement('canvas'); c.width = 90; c.height = 195;
        const x = c.getContext('2d')!;
        setInterval(() => {
          x.fillStyle = '#000'; x.fillRect(0, 0, c.width, c.height);
          if (!w.__unplug) { x.fillStyle = '#fff'; x.fillRect(10, 10, 40, 8); }
        }, 100);
        return c.captureStream(10);
      };
    });
    await ready(page, 'view=phones');
    await page.locator('[data-component="PhoneCard"]').first().getByRole('button', { name: '미러링 창 고르기' }).click();
    await page.getByRole('button', { name: '설정 닫기' }).click();
    const phone = page.locator('[data-component="PhoneMirror"]');
    await expect(phone.locator('.np-phone__live')).toBeVisible();
    await page.evaluate(() => { (window as unknown as { __unplug: boolean }).__unplug = true; });
    await expect(phone.getByRole('status')).toContainText('폰 화면 신호 없음', { timeout: 6000 });
    await expect(phone).toContainText('폰 화면 · 신호 없음');
    await expect(phone.locator('.np-phone__live')).toHaveCount(0);
    await page.evaluate(() => { (window as unknown as { __unplug: boolean }).__unplug = false; });
    await expect(phone.locator('.np-phone__live')).toBeVisible({ timeout: 4000 });
    await expect(phone.getByRole('status')).toHaveCount(0);
  });
});

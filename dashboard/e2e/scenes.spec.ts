// FR-12B · D9: screens.ts의 화면이 네 해상도와 두 테마에서 넘침·잘림·겹침 없이 그려진다
import { expect, test, type Page } from '@playwright/test';
import { SCREENS, SIZES } from './screens';
import { inspect, ready } from './inspect';

for (const [title, query] of SCREENS) {
  for (const [w, h] of SIZES) {
    test(`${title} · 다크 ${w}×${h}`, async ({ page }) => {
      await page.setViewportSize({ width: w, height: h });
      await ready(page, `${query}&theme=dark`);
      const r = await inspect(page);
      await page.screenshot({ path: `test-results/shots/${query.replace(/[^a-z0-9]+/gi, '_')}-dark-${w}.png` });
      expect(r).toEqual({ ...r, pageScroll: [], innerScroll: [], textOverflow: [], ellipsized: [], clipped: [], smallTargets: [], inlineStyle: 0, smallFont: [], overlaps: [], lowContrast: [], missingComponent: 0 });
    });
  }
  for (const [w, h] of SIZES) {
    test(`${title} · 라이트 ${w}×${h}`, async ({ page }) => {
      await page.setViewportSize({ width: w, height: h });
      await ready(page, `${query}&theme=light`);
      const r = await inspect(page);
      await page.screenshot({ path: `test-results/shots/${query.replace(/[^a-z0-9]+/gi, '_')}-light-${w}.png` });
      expect(r).toEqual({ ...r, pageScroll: [], innerScroll: [], textOverflow: [], ellipsized: [], clipped: [], smallTargets: [], inlineStyle: 0, smallFont: [], overlaps: [], lowContrast: [], missingComponent: 0 });
    });
  }
}

// FR-12B NFR-10: 두 장소. 상단 바 장소 고르기와 작은 방으로 주 장소를 고정한다
const CLEAN = { pageScroll: [], innerScroll: [], textOverflow: [], ellipsized: [], clipped: [], smallTargets: [], inlineStyle: 0, smallFont: [], overlaps: [], lowContrast: [], missingComponent: 0 };
const bar = (page: Page) => page.locator('[data-component="TopBar"]');
const stage = (page: Page) => page.locator('[data-component="Stage"]');
const inset = (page: Page) => page.locator('[data-component="StageInset"]:not([data-leaving="true"])');
const placeText = (page: Page) => page.locator('.np-stage__code');
/** 겹쳐 바꾸는 0.4초와 도착 강조가 끝날 때까지 기다린다 (끝없이 도는 선과 고리 움직임은 기다리지 않는다) */
const settled = async (page: Page) => {
  await expect(page.locator('[data-leaving="true"]')).toHaveCount(0);
  await page.evaluate(() => Promise.all(document.getAnimations().filter((a) => a.effect?.getTiming().iterations !== Infinity).map((a) => a.finished.catch(() => undefined))));
};

test('FR12B MON-11 상단 바에서 장소를 고르면 무대의 장소 이름이 바뀌고 자동을 누르면 돌아오며, 고르기 버튼의 폭은 변하지 않는다', async ({ page }) => {
  await ready(page, 'scene=side-room&theme=dark');
  const chooser = bar(page).locator('[data-component="PlaceChooser"]');
  // 고정해도 자동·장소 버튼의 자리와 폭이 변하지 않는다
  const boxes = async () => (await Promise.all(['자동', '공용 라운지', '옆방'].map((n) => bar(page).getByRole('button', { name: n }).boundingBox()))).flatMap((b) => [b!.x, b!.width]);
  const steady = async () => { const now = await boxes(); return now.every((v, i) => Math.abs(v - before[i]) < 0.5); };
  const before = await boxes();
  await expect(placeText(page)).toContainText('옆방');
  await expect(stage(page)).toHaveAttribute('data-locked', 'false');

  await bar(page).getByRole('button', { name: '공용 라운지' }).click();
  await expect(placeText(page)).toContainText('공용 라운지');
  await expect(stage(page)).toHaveAttribute('data-locked', 'true');
  await expect(chooser).toHaveAttribute('data-locked', 'true');
  await expect(bar(page).getByRole('button', { name: '공용 라운지' })).toHaveAttribute('aria-pressed', 'true');
  expect(await steady()).toBe(true);
  // 고정하면 옆방의 요청이 와도 주 장소는 라운지이고, 옆방은 작은 방이다
  await expect(inset(page)).toHaveAttribute('data-place', 'side-room');
  await settled(page);
  const r = await inspect(page);
  expect(r).toEqual({ ...r, ...CLEAN });

  await bar(page).getByRole('button', { name: '자동' }).click();
  await expect(placeText(page)).toContainText('옆방');
  await expect(stage(page)).toHaveAttribute('data-locked', 'false');
  await expect(bar(page).getByRole('button', { name: '자동' })).toHaveAttribute('aria-pressed', 'true');
  expect(await steady()).toBe(true);
});

test('FR12B MON-11 작은 방은 버튼이다: 누르거나 Enter, Space를 누르면 그 장소로 고정하고, 무대를 확대하면 함께 커진다', async ({ page }) => {
  await ready(page, 'scene=side-room&theme=dark');
  await expect(inset(page)).toHaveAttribute('role', 'button');
  await expect(inset(page)).toHaveAccessibleName('공용 라운지에 방문자 B, 방문자 C. 누르면 이 장소로 고정해요');
  await inset(page).click();
  await expect(placeText(page)).toContainText('공용 라운지');
  await expect(stage(page)).toHaveAttribute('data-locked', 'true');
  await expect(inset(page)).toHaveAttribute('data-place', 'side-room');

  // 키보드: 포커스를 두고 Enter, 이어서 Space
  await bar(page).getByRole('button', { name: '자동' }).click();
  await inset(page).focus();
  await page.keyboard.press('Enter');
  await expect(placeText(page)).toContainText('공용 라운지');
  // 장소가 바뀌어도 작은 방 버튼에 포커스가 남아 한 번 더 누르면 되돌아간다
  await expect(inset(page)).toBeFocused();
  await bar(page).getByRole('button', { name: '자동' }).click();
  await inset(page).focus();
  await page.keyboard.press('Space');
  await expect(placeText(page)).toContainText('공용 라운지');

  // 무대를 확대하면 작은 방은 무대의 일부로 함께 커진다
  await bar(page).getByRole('button', { name: '자동' }).click();
  await settled(page);
  const size = async () => (await inset(page).locator('.np-stage__inset-box').boundingBox())!.width;
  const small = await size();
  await page.getByRole('button', { name: '확대', exact: true }).click();
  expect(await size()).toBeGreaterThan(small * 1.1);
});

test('FR12B MON-11 패널이 열리면 작은 방을 숨긴다', async ({ page }) => {
  await ready(page, 'scene=side-room&panel=uses');
  await expect(page.locator('[data-component="Panel"]')).toBeVisible();
  await expect(page.locator('[data-component="StageInset"]')).toHaveCount(0);
  await page.getByRole('button', { name: '패널 닫기' }).click();
  await expect(inset(page)).toHaveAttribute('data-place', 'lounge');
});

/**
 * 시계를 직접 다룬다: 페이지를 열기 전에 가짜 시계를 깔고, 도착 강조를 켜는 0.8초 타이머(App)가 지나도록 시간을 넘긴 뒤 멈춘다.
 * 멈춘 시계에서는 떠나는 그림을 지우는 400ms 타이머(Stage의 PLACE_FADE_MS)가 실제 시간에 밀리지 않는다.
 */
async function frozenAt(page: Page, query: string) {
  await page.clock.install();
  await ready(page, query);
  await page.clock.pauseAt(Date.now() + 2000);
  await expect(page.locator('.np-app')).toHaveAttribute('data-motion', 'on');
}

test('FR12B MON-11 주 장소가 바뀌면 방과 작은 방을 0.4초 동안 겹쳐 바꾼다', async ({ page }) => {
  await frozenAt(page, 'scene=side-room&theme=dark');
  await bar(page).getByRole('button', { name: '공용 라운지' }).click();
  // 눌린 그 그림에서 떠나는 방(바닥 층, 기기 층)과 작은 방이 남아 있고 새 방과 작은 방이 나타난다. 둘 다 0.4초다.
  const seen = await page.evaluate(() => {
    const leaving = [...document.querySelectorAll('[data-leaving="true"]')];
    const coming = [...document.querySelectorAll('[data-fade="in"]')];
    // 새 장소의 기기는 도착 강조 없이 겹쳐 나타난다
    const arriving = document.getAnimations().filter((a) => (a as CSSAnimation).animationName.startsWith('np-arrive') && (a.effect as KeyframeEffect).target?.closest('[data-component="StageDevice"]')).length;
    const style = (e: Element) => getComputedStyle(e);
    return {
      leaving: leaving.map((e) => [style(e).animationName, style(e).animationDuration]),
      coming: coming.map((e) => [style(e).animationName, style(e).animationDuration]),
      arriving
    };
  });
  expect(seen).toEqual({
    leaving: [['np-place-out', '0.4s'], ['np-place-out', '0.4s'], ['np-place-out', '0.4s']],
    coming: [['np-place-in', '0.4s'], ['np-place-in', '0.4s'], ['np-place-in', '0.4s']],
    arriving: 0
  });
  // 0.4초가 되기 전에는 떠나는 그림이 남아 있고, 지나면 없어진다
  await page.clock.runFor(399);
  await expect(page.locator('[data-leaving="true"]')).toHaveCount(3);
  await page.clock.runFor(2);
  await expect(page.locator('[data-leaving="true"]')).toHaveCount(0);
});

test('FR12B MON-11 움직임 줄이기 설정이면 주 장소를 바로 바꾼다', async ({ page }) => {
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await frozenAt(page, 'scene=side-room&theme=dark');
  await bar(page).getByRole('button', { name: '공용 라운지' }).click();
  // 시계가 멈춰 있으니 기다린 시간 때문이 아니라 처음부터 떠나는 그림을 남기지 않은 것이다
  await expect(placeText(page)).toContainText('공용 라운지');
  await expect(page.locator('[data-leaving="true"]')).toHaveCount(0);
});

for (const theme of ['dark', 'light']) {
  for (const [w, h] of SIZES) {
    test(`FR12B MON-11 라운지 고정 · ${theme === 'dark' ? '다크' : '라이트'} ${w}×${h}`, async ({ page }) => {
      await page.setViewportSize({ width: w, height: h });
      await ready(page, `scene=side-room&theme=${theme}`);
      await bar(page).getByRole('button', { name: '공용 라운지' }).click();
      await expect(inset(page)).toHaveAttribute('data-place', 'side-room');
      await settled(page);
      const r = await inspect(page);
      expect(r).toEqual({ ...r, ...CLEAN });
    });
  }
}

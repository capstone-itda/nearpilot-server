// NFR-12: 외부 네트워크 없이 글꼴과 화면이 그려진다
import { expect, test } from '@playwright/test';

test('NFR12 외부 요청을 막아도 번들 글꼴로 그린다', async ({ page }) => {
  const external: string[] = [];
  await page.route('**/*', (route) => {
    const u = new URL(route.request().url());
    if (u.hostname !== 'localhost') { external.push(u.href); return route.abort(); }
    return route.continue();
  });
  await page.goto('/?scene=flow');
  await page.waitForSelector('[data-component="FlowRail"]');
  await page.evaluate(() => document.fonts.ready);
  const fonts = await page.evaluate(() => [...document.fonts].filter((f) => f.status === 'loaded').map((f) => f.family.replace(/"/g, '')));
  expect(new Set(fonts)).toEqual(new Set(['IBM Plex Sans KR', 'IBM Plex Mono']));
  expect(external).toEqual([]);
});

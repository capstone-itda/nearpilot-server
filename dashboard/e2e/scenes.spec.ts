// FR-12B · D9: 15화면이 네 해상도와 두 테마에서 넘침·잘림·겹침 없이 그려진다
import { expect, test } from '@playwright/test';
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

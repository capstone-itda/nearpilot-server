// D4: 배치 화면에서 허용한 배치라면 모니터 무대의 글자·콜아웃이 겹치거나 넘치지 않는다
import { expect, test } from '@playwright/test';
import { inspect, ready } from './inspect';

const ARRANGEMENTS: Record<string, Record<string, { x: number; y: number }>> = {
  mid2: { 'locker-1': { x: 456, y: 432 }, 'locker-2': { x: 624, y: 432 }, 'locker-3': { x: 792, y: 432 } },
  stack: { 'locker-1': { x: 600, y: 120 }, 'locker-2': { x: 600, y: 336 } },
  mid: { 'locker-1': { x: 456, y: 336 }, 'locker-2': { x: 624, y: 336 }, 'locker-3': { x: 792, y: 336 },
    'speaker-1': { x: 456, y: 120 }, 'light-1': { x: 624, y: 120 }, 'light-2': { x: 792, y: 120 }, 'light-3': { x: 960, y: 120 } },
  right: { 'speaker-1': { x: 744, y: 720 }, 'light-1': { x: 912, y: 720 }, 'light-2': { x: 1080, y: 720 }, 'light-3': { x: 1248, y: 720 } },
  rightcluster: { 'locker-1': { x: 1248, y: 120 }, 'locker-2': { x: 1248, y: 336 }, 'locker-3': { x: 1080, y: 120 }, 'light-3': { x: 1080, y: 336 },
    'speaker-1': { x: 744, y: 720 }, 'light-1': { x: 912, y: 720 }, 'light-2': { x: 1080, y: 720 } },
  stackleft: { 'locker-1': { x: 408, y: 120 }, 'locker-2': { x: 408, y: 336 } },
  bottomall: { 'locker-1': { x: 408, y: 720 }, 'locker-2': { x: 576, y: 720 }, 'locker-3': { x: 744, y: 720 },
    'speaker-1': { x: 912, y: 720 }, 'light-1': { x: 1080, y: 720 }, 'light-2': { x: 1248, y: 504 }, 'light-3': { x: 1080, y: 504 } }
};
const SCENES = ['idle', 'ask', 'flow', 'done', 'fault', 'approval', 'offline', 'ask&panel=trust', 'later&view=layout'];
const SIZES: [number, number][] = [[1366, 768], [1920, 1080]];

for (const [name, devices] of Object.entries(ARRANGEMENTS)) {
  for (const scene of SCENES) {
    for (const [w, h] of SIZES) {
      test(`FR12B D4 ${name} 배치 · ${scene} · ${w}×${h}`, async ({ page }) => {
        await page.addInitScript((file) => localStorage.setItem('nearpilot-arrangement:lounge', file),
          JSON.stringify({ kind: 'nearpilot-arrangement', version: 1, place: 'lounge', devices }));
        await page.setViewportSize({ width: w, height: h });
        await ready(page, `scene=${scene}`);
        const r = await inspect(page);
        await page.screenshot({ path: `test-results/shots/arr-${name}-${scene.replace(/[^a-z]+/g, '_')}-${w}.png` });
        expect(r).toEqual({ ...r, pageScroll: [], innerScroll: [], textOverflow: [], ellipsized: [], clipped: [], smallTargets: [], inlineStyle: 0, smallFont: [], overlaps: [], lowContrast: [], missingComponent: 0 });
      });
    }
  }
}

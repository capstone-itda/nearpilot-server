// 브라우저 안에서 화면을 검사한다. 결과는 문제 목록이다 (빈 배열이면 통과).
import type { Page } from '@playwright/test';

export async function ready(page: Page, query: string) {
  await page.goto(`/?${query}`);
  await page.waitForSelector('[data-component="TopBar"]');
  await page.evaluate(() => document.fonts.ready);
  await page.waitForTimeout(150);
}

export interface Report {
  pageScroll: string[];
  innerScroll: string[];
  textOverflow: string[];
  ellipsized: string[];
  clipped: string[];
  smallTargets: string[];
  inlineStyle: number;
  smallFont: string[];
  overlaps: string[];
  lowContrast: string[];
  missingComponent: number;
}

export function inspect(page: Page): Promise<Report> {
  return page.evaluate(() => {
    const de = document.documentElement;
    const vw = de.clientWidth, vh = de.clientHeight;
    const name = (el: Element) => `${el.getAttribute('class') || el.tagName} "${(el.textContent ?? '').trim().slice(0, 24)}"`;
    const visible = (el: Element) => {
      if (el.closest('[data-on="false"]')) return false;
      const cs = getComputedStyle(el);
      if (cs.display === 'none' || cs.visibility === 'hidden') return false;
      const b = el.getBoundingClientRect();
      return b.width > 0 && b.height > 0;
    };
    const ownText = (el: Element) => [...el.childNodes].some((n) => n.nodeType === 3 && n.textContent!.trim());
    const r = {
      pageScroll: [] as string[], innerScroll: [] as string[], textOverflow: [] as string[], ellipsized: [] as string[], clipped: [] as string[],
      smallTargets: [] as string[], inlineStyle: document.querySelectorAll('.np-app [style]').length, smallFont: [] as string[], overlaps: [] as string[],
      lowContrast: [] as string[], missingComponent: 0
    };
    if (de.scrollWidth > vw) r.pageScroll.push(`가로 ${de.scrollWidth}/${vw}`);
    if (de.scrollHeight > vh) r.pageScroll.push(`세로 ${de.scrollHeight}/${vh}`);

    const all = [...document.querySelectorAll('.np-app *')];
    for (const el of all) {
      if (!visible(el)) continue;
      const cs = getComputedStyle(el);
      if (!(el instanceof SVGElement)) {
        if ((cs.overflowY === 'auto' || cs.overflowY === 'scroll') && el.scrollHeight > el.clientHeight + 2) r.innerScroll.push(`세로 ${name(el)} ${el.scrollHeight}/${el.clientHeight}`);
        if ((cs.overflowX === 'auto' || cs.overflowX === 'scroll') && el.scrollWidth > el.clientWidth + 1) r.innerScroll.push(`가로 ${name(el)} ${el.scrollWidth}/${el.clientWidth}`);
        if (cs.textOverflow === 'ellipsis' && el.scrollWidth > el.clientWidth + 1) r.ellipsized.push(name(el));
        else if (cs.overflow === 'visible' && el.children.length === 0 && el.clientWidth > 0 && el.scrollWidth > el.clientWidth + 2) r.textOverflow.push(name(el));
      }
      const b = el.getBoundingClientRect();
      if (b.right > vw + 1 || b.left < -1 || b.bottom > vh + 1) {
        if (!el.closest('svg')) r.clipped.push(name(el));
      }
      if (ownText(el)) {
        let fs = parseFloat(cs.fontSize);
        if (el instanceof SVGGraphicsElement) { const m = el.getScreenCTM()!; fs *= Math.hypot(m.a, m.b); }
        if (fs < 10.95) r.smallFont.push(`${name(el)} ${fs.toFixed(1)}px`);
      }
    }
    for (const el of document.querySelectorAll('.np-app button, .np-app input, .np-app select, .np-app [role=switch], .np-app [onclick], .np-app tr[tabindex]')) {
      if (!visible(el)) continue;
      const b = el.getBoundingClientRect();
      if (b.width < 43.5 || b.height < 43.5) r.smallTargets.push(`${name(el)} ${Math.round(b.width)}x${Math.round(b.height)}`);
    }

    // 무대: 보이는 글자끼리, 글자와 콜아웃 상자가 겹치는지
    for (const svg of document.querySelectorAll('.np-stage__svg')) {
      const callout = svg.querySelector('.np-stage__callout[data-on="true"] .np-stage__callout-box');
      const cb = callout?.getBoundingClientRect();
      const panel = document.querySelector('.np-panel')?.getBoundingClientRect();
      const texts = [...svg.querySelectorAll('text')].filter((t) => visible(t) && t.textContent!.trim() && !t.closest('.np-stage__callout'))
        .map((t) => {
          // 글자 상자는 줄 높이를 포함하므로 위아래 20%를 빼고 글자 몸통만 본다
          const o = t.getBoundingClientRect();
          return { t, b: new DOMRect(o.x, o.y + o.height * 0.2, o.width, o.height * 0.6) };
        })
        .filter(({ b }) => !panel || b.right <= panel.left);
      const hit = (a: DOMRect, c: DOMRect, pad = 1) => a.left < c.right - pad && c.left < a.right - pad && a.top < c.bottom - pad && c.top < a.bottom - pad;
      if (cb && panel && cb.right > panel.left) r.overlaps.push('콜아웃이 패널 아래에 가려짐');
      for (let i = 0; i < texts.length; i++) {
        if (cb && hit(texts[i].b, cb)) r.overlaps.push(`콜아웃 ↔ ${name(texts[i].t)}`);
        for (let j = i + 1; j < texts.length; j++) if (hit(texts[i].b, texts[j].b, 2)) r.overlaps.push(`${name(texts[i].t)} ↔ ${name(texts[j].t)}`);
      }
    }

    // 글자 대비 (흐리게 한 요소와 미러링 영상 자리 제외)
    const parse = (c: string) => { const m = c.match(/[\d.]+/g)!.map(Number); return { r: m[0], g: m[1], b: m[2], a: m[3] ?? 1 }; };
    const lum = (c: { r: number; g: number; b: number }) => {
      const f = (v: number) => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; };
      return 0.2126 * f(c.r) + 0.7152 * f(c.g) + 0.0722 * f(c.b);
    };
    const blend = (fg: { r: number; g: number; b: number; a: number }, bg: { r: number; g: number; b: number }) =>
      ({ r: fg.r * fg.a + bg.r * (1 - fg.a), g: fg.g * fg.a + bg.g * (1 - fg.a), b: fg.b * fg.a + bg.b * (1 - fg.a) });
    const bgOf = (el: Element): { r: number; g: number; b: number } => {
      const layers: { r: number; g: number; b: number; a: number }[] = [];
      for (let e: Element | null = el; e; e = e.parentElement) {
        if (e instanceof SVGElement && !(e instanceof SVGSVGElement)) continue;
        const c = parse(getComputedStyle(e).backgroundColor);
        if (c.a > 0) { layers.push(c); if (c.a >= 1) break; }
      }
      let base = { r: 255, g: 255, b: 255 };
      for (const l of layers.reverse()) base = blend(l, base);
      return base;
    };
    const app = document.querySelector('.np-app')!;
    const stageBg = parse(getComputedStyle(app).getPropertyValue('--bg').trim().startsWith('#') ? (() => { const h = getComputedStyle(app).getPropertyValue('--bg').trim(); return `rgb(${parseInt(h.slice(1, 3), 16)}, ${parseInt(h.slice(3, 5), 16)}, ${parseInt(h.slice(5, 7), 16)})`; })() : 'rgb(0,0,0)');
    for (const el of all) {
      if (!ownText(el) || !visible(el) || el.closest('[data-dim="true"]') || el.closest('[data-mock]') || el.closest('[data-live="false"] .np-stage, [data-live="false"] .np-rail, [data-live="false"] .np-phone')) continue;
      const cs = getComputedStyle(el);
      let fg = parse(el instanceof SVGElement ? cs.fill : cs.color);
      if (el instanceof SVGElement && !cs.fill.startsWith('rgb')) continue;
      let op = 1;
      for (let e: Element | null = el; e && e !== app; e = e.parentElement) op *= parseFloat(getComputedStyle(e).opacity);
      fg = { ...fg, a: fg.a * op };
      let bg = el instanceof SVGElement ? stageBg : bgOf(el);
      if (el instanceof SVGElement) {
        // 무대 글자 뒤의 도형(콜아웃 상자, 노드 상자, 방문자 점): 같은 묶음에서 칠한 도형 중 마지막
        // 글자 가운데가 도형 안에 있을 때만 그 도형을 바탕으로 본다
        const tb = el.getBoundingClientRect();
        const cx = tb.x + tb.width / 2, cy = tb.y + tb.height / 2;
        for (const shape of el.parentElement?.querySelectorAll(':scope > rect, :scope > circle') ?? []) {
          const f = getComputedStyle(shape).fill;
          const sb = shape.getBoundingClientRect();
          if (f.startsWith('rgb') && cx > sb.left && cx < sb.right && cy > sb.top && cy < sb.bottom) bg = blend(parse(f), bg);
        }
      }
      const c = blend(fg, bg);
      const L1 = lum(c), L2 = lum(bg);
      const ratio = (Math.max(L1, L2) + 0.05) / (Math.min(L1, L2) + 0.05);
      if (ratio < 4.5) r.lowContrast.push(`${name(el)} ${ratio.toFixed(2)}`);
    }

    r.missingComponent = [...document.querySelectorAll('.np-app > *')].filter((e) => !(e as HTMLElement).dataset.component).length
      + [...document.querySelectorAll('.np-monitor > *, .np-settings > *')].filter((e) => !(e as HTMLElement).dataset.component).length;
    for (const k of Object.keys(r) as (keyof typeof r)[]) if (Array.isArray(r[k])) (r[k] as string[]).splice(0, (r[k] as string[]).length, ...new Set(r[k] as string[]));
    return r;
  });
}

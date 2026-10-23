import { describe, expect, it } from 'vitest';
import { FIT, ZOOM_MAX, clampView, isFit, panBy, panelEdgeOf, viewBoxOf, zoomAt } from './zoom';

describe('무대 확대·이동', () => {
  it('FR12B 맞춤 상태는 무대 전체를 보인다', () => {
    expect(viewBoxOf(FIT)).toEqual([0, 0, 1400, 800]);
    expect(isFit(FIT)).toBe(true);
  });
  it('FR12B 휠 확대는 커서 아래 자리를 그대로 둔다', () => {
    const p = { x: 1000, y: 300 };
    const v = zoomAt(FIT, p, 2);
    const [x, y, w, h] = viewBoxOf(v);
    // p의 viewBox 안 비율이 확대 전과 같다
    expect((p.x - x) / w).toBeCloseTo(1000 / 1400);
    expect((p.y - y) / h).toBeCloseTo(300 / 800);
  });
  it('FR12B 배율은 1~4배로 묶고, 1배로 돌아오면 맞춤 상태다', () => {
    expect(zoomAt(FIT, { x: 700, y: 400 }, 100).k).toBe(ZOOM_MAX);
    const v = zoomAt(zoomAt(FIT, { x: 1200, y: 100 }, 2), { x: 300, y: 600 }, 0.1);
    expect(v).toEqual(FIT);
  });
  it('FR12B 끌어도 보이는 영역이 무대 밖으로 나가지 않는다', () => {
    const v = panBy(zoomAt(FIT, { x: 700, y: 400 }, 2), 5000, -5000);
    const [x, y, w, h] = viewBoxOf(v);
    expect(x).toBeCloseTo(0);
    expect(y + h).toBeCloseTo(800);
    expect(w).toBeCloseTo(700);
    expect(clampView({ k: 2, cx: -1, cy: 9999 })).toEqual(v);
  });
  it('FR12B 패널 경계를 무대 좌표로 바꾼다', () => {
    // 1400×800 무대 그대로 그리면 패널 폭 570px은 x 830에서 시작한다
    expect(panelEdgeOf(FIT, { w: 1400, h: 800 }, 570)).toBeCloseTo(830);
    // 가로가 남으면 가운데 맞춤 여백을 빼고 잰다
    expect(panelEdgeOf(FIT, { w: 1600, h: 800 }, 670)).toBeCloseTo(830);
    // 2배로 보면 같은 패널이 무대 좌표로 더 좁다
    expect(panelEdgeOf(zoomAt(FIT, { x: 700, y: 400 }, 2), { w: 1400, h: 800 }, 570)).toBeCloseTo(350 + 415);
  });
});

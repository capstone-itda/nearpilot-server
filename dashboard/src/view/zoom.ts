// 모니터 무대의 확대·이동 (2026-10-23 사용자 결정). 무대 좌표(viewBox 0 0 1400 800)에서 다룬다.
import type { Spot } from '../model/types';

export const STAGE_W = 1400;
export const STAGE_H = 800;
/** 무대 전체가 보이는 배율 1부터 4배까지 */
export const ZOOM_MIN = 1;
export const ZOOM_MAX = 4;
/** 확대·축소 버튼 한 번의 배율 */
export const ZOOM_STEP = 1.25;

/** k배로 보면서 무대 좌표 (cx, cy)를 가운데에 둔다 */
export interface ZoomView { k: number; cx: number; cy: number }
export const FIT: ZoomView = { k: 1, cx: STAGE_W / 2, cy: STAGE_H / 2 };

export const isFit = (v: ZoomView) => v.k === FIT.k && v.cx === FIT.cx && v.cy === FIT.cy;

/** 보이는 영역이 무대 밖으로 나가지 않게 가운데를 묶는다 */
export function clampView(v: ZoomView): ZoomView {
  const k = Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, v.k));
  const hw = STAGE_W / 2 / k, hh = STAGE_H / 2 / k;
  const cx = Math.min(STAGE_W - hw, Math.max(hw, v.cx)), cy = Math.min(STAGE_H - hh, Math.max(hh, v.cy));
  const r = { k, cx, cy };
  // 배율 1로 돌아오면 정확히 맞춤 상태로 둔다 (방 밀기가 다시 켜진다)
  return k === ZOOM_MIN ? FIT : r;
}

export function viewBoxOf(v: ZoomView): [number, number, number, number] {
  const w = STAGE_W / v.k, h = STAGE_H / v.k;
  return [v.cx - w / 2, v.cy - h / 2, w, h];
}

/** 무대 좌표 p가 화면에서 그 자리에 머물도록 factor배 확대한다 (휠·핀치) */
export function zoomAt(v: ZoomView, p: Spot, factor: number): ZoomView {
  const k = Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, v.k * factor));
  const r = v.k / k;
  return clampView({ k, cx: p.x + (v.cx - p.x) * r, cy: p.y + (v.cy - p.y) * r });
}

/** 무대 좌표로 (dx, dy)만큼 끌었다. 보이는 영역은 반대로 움직인다. */
export const panBy = (v: ZoomView, dx: number, dy: number): ZoomView => clampView({ ...v, cx: v.cx - dx, cy: v.cy - dy });

/**
 * 패널 왼쪽 끝의 무대 x 좌표. 무대는 viewBox를 가운데 맞춤(meet)으로 그린다.
 * stage: 무대 화면 크기(px), panelPx: 무대 오른쪽 끝부터 덮는 패널 폭(px)
 */
export function panelEdgeOf(v: ZoomView, stage: { w: number; h: number }, panelPx: number): number {
  const [x, , w, h] = viewBoxOf(v);
  const scale = Math.min(stage.w / w, stage.h / h);
  const off = (stage.w - w * scale) / 2;
  return x + (stage.w - panelPx - off) / scale;
}

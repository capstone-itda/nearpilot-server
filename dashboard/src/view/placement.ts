// 무대 위에서 사람, 이름표, 콜아웃이 기기 글자와 겹치지 않는 자리를 고른다.
// 배치는 이 PC에서 자유롭게 바뀌므로 고정 좌표 대신 장애물 상자를 피해서 찾는다.
import type { Spot } from '../model/types';
import { GRID, ROOM } from './arrange';

export interface Rect { x1: number; y1: number; x2: number; y2: number }
export const hits = (a: Rect, b: Rect) => a.x1 < b.x2 && b.x1 < a.x2 && a.y1 < b.y2 && b.y1 < a.y2;
const hitsAny = (a: Rect, list: Rect[]) => list.some((b) => hits(a, b));
const inside = (a: Rect, b: Rect) => a.x1 >= b.x1 && a.x2 <= b.x2 && a.y1 >= b.y1 && a.y2 <= b.y2;
const ROOM_RECT: Rect = { x1: ROOM.x, y1: ROOM.y, x2: ROOM.x + ROOM.w, y2: ROOM.y + ROOM.h };

/** 기기 본체와 이름·상태·코드 세 줄 */
export interface DeviceGeom { x: number; y: number; fx: number; fy: number; wall: 'top' | 'bottom' }
export function deviceTextBox(d: DeviceGeom): Rect {
  return d.wall === 'top' ? { x1: d.x - 84, x2: d.x + 84, y1: d.y - 22, y2: d.y + 100 } : { x1: d.x - 84, x2: d.x + 84, y1: d.y - 100, y2: d.y + 22 };
}
/** 신호 세기 글자 (Stage.tsx의 np-stage__signal-label 자리) */
export function rssiBox(d: DeviceGeom): Rect {
  return d.wall === 'top' ? { x1: d.x - 132, x2: d.x - 38, y1: d.y - 14, y2: d.y + 12 } : { x1: d.x - 102, x2: d.x - 8, y1: d.y + 26, y2: d.y + 52 };
}

/** 사람 점과 안쪽 고리 */
export const personBox = (p: Spot): Rect => ({ x1: p.x - 26, x2: p.x + 26, y1: p.y - 26, y2: p.y + 26 });
/** 사람 고리 전체 (콜아웃이 덮지 않게) */
export const ringBox = (p: Spot): Rect => ({ x1: p.x - 48, x2: p.x + 48, y1: p.y - 48, y2: p.y + 48 });
/** 기기 앞 점선 원 */
export const frontBox = (d: DeviceGeom): Rect => ({ x1: d.fx - 44, x2: d.fx + 44, y1: d.fy - 44, y2: d.fy + 44 });

export type LabelSide = 'right' | 'left' | 'above';
const LABEL_W = 100;
/** 사람 이름표 (Stage.tsx의 Visitor 글자 자리) */
export function labelBox(p: Spot, side: LabelSide): Rect {
  if (side === 'right') return { x1: p.x + 52, x2: p.x + 52 + LABEL_W, y1: p.y - 14, y2: p.y + 14 };
  if (side === 'left') return { x1: p.x - 52 - LABEL_W, x2: p.x - 52, y1: p.y - 14, y2: p.y + 14 };
  return { x1: p.x - LABEL_W / 2, x2: p.x + LABEL_W / 2, y1: p.y - 74, y2: p.y - 46 };
}

/**
 * 기준 자리에서 dir 방향(+1 아래, −1 위)으로 격자 한 칸씩 옮겨 가며 비어 있는 자리를 찾는다.
 * 방 안이고 기기 글자와 다른 사람에 겹치지 않는 첫 자리. 없으면 기준 자리.
 */
export function freePersonSpot(base: Spot, dir: 1 | -1, obstacles: Rect[]): Spot {
  for (const k of [0, 1, 2, 3, 4, 5, 6, 7, 8, -1, -2, -3, -4]) {
    const s = { x: base.x, y: base.y + dir * k * GRID };
    const b = personBox(s);
    if (inside(b, ROOM_RECT) && !hitsAny(b, obstacles)) return s;
  }
  return base;
}

/** 이름표는 오른쪽, 왼쪽, 위 순서로 방 안이고 겹치지 않는 쪽에 둔다 */
export function chooseLabel(p: Spot, obstacles: Rect[], prefer: LabelSide[] = ['right', 'left', 'above']): LabelSide {
  return prefer.find((side) => { const b = labelBox(p, side); return inside(b, ROOM_RECT) && !hitsAny(b, obstacles); }) ?? prefer[0];
}

export const CALLOUT_W = 390;
export const CALLOUT_H = 76;

/**
 * 콜아웃 상자 자리. 방 안에서 장애물을 피하고 가리키는 점에 가장 가까운 곳.
 * 패널이 열려 있으면 패널에 가리지 않는 왼쪽(x ≤ maxX)만 쓴다. 비어 있는 곳이 없으면 가장 덜 겹치는 곳.
 */
export function calloutSpot(ax: number, ay: number, obstacles: Rect[], maxX: number): Spot {
  const minX = ROOM.x + 12;
  const lastX = Math.min(maxX, ROOM.x + ROOM.w - CALLOUT_W - 12);
  let best: { s: Spot; overlap: number; dist: number } | undefined;
  for (let y = ROOM.y + 12; y + CALLOUT_H <= ROOM.y + ROOM.h - 12; y += 12) {
    for (let x = minX; x <= lastX; x += 12) {
      const box = { x1: x, y1: y, x2: x + CALLOUT_W, y2: y + CALLOUT_H };
      const overlap = obstacles.reduce((n, o) => n + (hits(box, o) ? (Math.min(box.x2, o.x2) - Math.max(box.x1, o.x1)) * (Math.min(box.y2, o.y2) - Math.max(box.y1, o.y1)) : 0), 0);
      const dx = ax < x ? x - ax : ax > x + CALLOUT_W ? ax - x - CALLOUT_W : 0;
      const dy = ay < y ? y - ay : ay > y + CALLOUT_H ? ay - y - CALLOUT_H : 0;
      const dist = Math.hypot(dx, dy) + (dx === 0 && dy === 0 ? 1000 : 0);
      if (!best || overlap < best.overlap || (overlap === best.overlap && dist < best.dist)) best = { s: { x, y }, overlap, dist };
    }
  }
  return best!.s;
}

/** 콜아웃 상자에서 가리키는 점에 가장 가까운 테두리 점 (지시선 끝) */
export function leaderEnd(c: Spot, ax: number, ay: number): Spot {
  const x = Math.min(Math.max(ax, c.x), c.x + CALLOUT_W);
  const y = Math.min(Math.max(ay, c.y), c.y + CALLOUT_H);
  if (x > c.x && x < c.x + CALLOUT_W && y > c.y && y < c.y + CALLOUT_H) return { x: c.x, y: c.y + CALLOUT_H / 2 };
  return { x, y };
}

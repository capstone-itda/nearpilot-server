// 무대 위에서 기기 글자, 사람, 이름표, 콜아웃이 서로 겹치지 않는 자리를 고른다.
// 기기와 사람은 평면 배치를 아이소메트릭으로 투영해 그리고(iso.ts), 글자는 투영한 무대 좌표에 똑바로 쓴다.
// 그래서 겹침은 무대 좌표의 상자로 잰다. 배치는 이 PC에서 자유롭게 바뀌므로 고정 좌표 대신 장애물을 피해서 찾는다.
import type { Capability, Spot } from '../model/types';
import { GRID, ROOM } from './arrange';
import { ROOM_ISO, iso } from './iso';

export interface Rect { x1: number; y1: number; x2: number; y2: number }
export const hits = (a: Rect, b: Rect) => a.x1 < b.x2 && b.x1 < a.x2 && a.y1 < b.y2 && b.y1 < a.y2;
const hitsAny = (a: Rect, list: Rect[]) => list.some((b) => hits(a, b));
const inside = (a: Rect, b: Rect) => a.x1 >= b.x1 && a.x2 <= b.x2 && a.y1 >= b.y1 && a.y2 <= b.y2;
const area = (a: Rect, b: Rect) => (hits(a, b) ? (Math.min(a.x2, b.x2) - Math.max(a.x1, b.x1)) * (Math.min(a.y2, b.y2) - Math.max(a.y1, b.y1)) : 0);

/** 글자를 둘 수 있는 무대 영역. 왼쪽의 호스트·서버·소유자 폰 칸은 뺀다. */
export const STAGE_BOUNDS: Rect = { x1: 320, y1: 60, x2: 1392, y2: 792 };
/** 패널이 열렸을 때 패널에 가리지 않는 무대의 오른쪽 끝 (무대 단위) */
export const PANEL_EDGE = 830;
/** 방을 왼쪽으로 dx만큼 밀어 그릴 때, 방 좌표에서 본 글자 영역 */
export const shiftBounds = (dx: number): Rect => ({ ...STAGE_BOUNDS, x1: STAGE_BOUNDS.x1 + dx, x2: STAGE_BOUNDS.x2 + dx });

/** 글자 폭 어림값: 한글은 1em, 영문·숫자·기호는 0.6em */
export function textWidth(s: string, size: number): number {
  let w = 0;
  for (const ch of s) w += /[\u0000-ɏ -⃿]/.test(ch) ? 0.6 : 1;
  return Math.ceil(w * size);
}

// ── 기기 ────────────────────────────────────────────────────────────────

/** 기기 본체의 평면 크기와 높이 (평면 단위) */
export const DEVICE_W = 60;
export const DEVICE_D = 36;
export const DEVICE_H: Record<Capability, number> = { locker: 110, speaker: 56, light: 84 };

/** 기기 본체가 무대에서 차지하는 상자 */
export function deviceBody(x: number, y: number, cap: Capability): Rect {
  const h = DEVICE_H[cap];
  const pts = [[x - DEVICE_W / 2, y - DEVICE_D / 2], [x + DEVICE_W / 2, y - DEVICE_D / 2], [x - DEVICE_W / 2, y + DEVICE_D / 2], [x + DEVICE_W / 2, y + DEVICE_D / 2]]
    .flatMap(([a, b]) => [ROOM_ISO.P(a, b, 0), ROOM_ISO.P(a, b, h)]);
  return { x1: Math.min(...pts.map((p) => p.x)), x2: Math.max(...pts.map((p) => p.x)), y1: Math.min(...pts.map((p) => p.y)), y2: Math.max(...pts.map((p) => p.y)) };
}

/** 기기 이름표: 이름(20px)과 상태(17px) 두 줄. x는 가운데, y는 윗변 */
export const DEVICE_LABEL_H = 48;
export interface LabelSpot { x: number; y: number; w: number }
export const deviceLabelBox = (l: LabelSpot): Rect => ({ x1: l.x - l.w / 2, x2: l.x + l.w / 2, y1: l.y, y2: l.y + DEVICE_LABEL_H });
/** 신호 세기 한 줄(17px). x는 가운데, y는 윗변 */
export const RSSI_H = 22;
export const RSSI_W = 84;
export const rssiBox = (l: Spot): Rect => ({ x1: l.x - RSSI_W / 2, x2: l.x + RSSI_W / 2, y1: l.y, y2: l.y + RSSI_H });

export interface DeviceGeom { body: Rect; labelW: number; label?: LabelSpot; rssi?: Spot }

/** 이름표 자리 후보: 아래, 오른쪽, 왼쪽, 위 */
function labelCandidates(b: Rect, w: number): LabelSpot[] {
  const cx = (b.x1 + b.x2) / 2, cy = (b.y1 + b.y2) / 2;
  return [
    { x: cx, y: b.y2 + 6, w },
    { x: b.x2 + 10 + w / 2, y: cy - DEVICE_LABEL_H / 2, w },
    { x: b.x1 - 10 - w / 2, y: cy - DEVICE_LABEL_H / 2, w },
    { x: cx, y: b.y1 - 6 - DEVICE_LABEL_H, w }
  ];
}

/**
 * 첫 후보부터 무대 안이고 장애물과 겹치지 않는 자리. 없으면 무대 안에서 가장 덜 겹치는 자리.
 * soft는 덮어도 덜 나쁜 것(기기 본체)이다. 글자를 덮는 것보다 낫게 친다.
 */
function pick<T>(cands: T[], box: (c: T) => Rect, obstacles: Rect[], soft: Rect[] = [], bounds = STAGE_BOUNDS): T {
  const ok = cands.filter((c) => inside(box(c), bounds));
  const pool = ok.length ? ok : cands;
  // 글자끼리는 2px 띄우고, 글자를 덮는 것은 본체를 덮는 것보다 100배 나쁘게 친다
  const pad = (r: Rect): Rect => ({ x1: r.x1 - 2, y1: r.y1 - 2, x2: r.x2 + 2, y2: r.y2 + 2 });
  const cost = (c: T) => obstacles.reduce((n, o) => n + 100 * area(pad(box(c)), o), 0) + soft.reduce((n, o) => n + area(box(c), o), 0);
  return pool.find((c) => cost(c) === 0) ?? [...pool].sort((a, b) => cost(a) - cost(b))[0];
}

/** 기기마다 이름표 자리를 정한다. 화면 위쪽 기기부터 정해 다른 기기의 본체와 앞서 놓은 이름표를 피한다. */
export function placeDeviceLabels(devices: DeviceGeom[], bounds = STAGE_BOUNDS): void {
  const placed: Rect[] = [];
  for (const d of [...devices].sort((a, b) => a.body.y2 - b.body.y2)) {
    const others = devices.filter((o) => o !== d).map((o) => o.body);
    d.label = pick(labelCandidates(d.body, d.labelW), deviceLabelBox, [...others, ...placed], [], bounds);
    placed.push(deviceLabelBox(d.label));
  }
}

/** 신호 세기 글자: 본체 위, 위쪽 옆, 옆, 아래 순서로 이름표와 다른 본체를 피한다 */
export function placeRssi(devices: DeviceGeom[], bounds = STAGE_BOUNDS): void {
  const placed: Rect[] = [];
  for (const d of [...devices].sort((a, b) => a.body.y1 - b.body.y1)) {
    const labels = devices.flatMap((o) => (o.label ? [deviceLabelBox(o.label)] : []));
    const bodies = devices.filter((o) => o !== d).map((o) => o.body);
    const b = d.body, cx = (b.x1 + b.x2) / 2, cy = (b.y1 + b.y2) / 2;
    const right = b.x2 + 8 + RSSI_W / 2, left = b.x1 - 8 - RSSI_W / 2;
    const near: Spot[] = [
      { x: cx, y: b.y1 - 4 - RSSI_H }, { x: right, y: b.y1 }, { x: left, y: b.y1 },
      { x: right, y: cy - RSSI_H / 2 }, { x: left, y: cy - RSSI_H / 2 }, { x: cx, y: b.y2 + 4 },
      { x: right, y: b.y2 - RSSI_H }, { x: left, y: b.y2 - RSSI_H }
    ];
    // 가까운 자리가 모두 막히면 한 줄씩 더 위로 올린 자리
    const cands = [...near, ...[1, 2].flatMap((k) => near.slice(0, 3).map((c) => ({ x: c.x, y: c.y - k * (RSSI_H + 4) })))];
    d.rssi = pick(cands, rssiBox, [...labels, ...placed], bodies, bounds);
    placed.push(rssiBox(d.rssi));
  }
}

// ── 사람 ────────────────────────────────────────────────────────────────

/** 사람 머리의 높이 (평면 단위) */
export const PERSON_Z = 70;
export const HEAD_R = 15;
/** 사람 점과 기둥이 무대에서 차지하는 상자 (평면 좌표 p에 선 사람) */
export function personBox(p: Spot): Rect {
  const head = iso(p, PERSON_Z), foot = iso(p, 0);
  return { x1: head.x - HEAD_R - 2, x2: head.x + HEAD_R + 2, y1: head.y - HEAD_R - 2, y2: foot.y + 6 };
}
/** 사람 고리 전체 (콜아웃이 덮지 않게) */
export function ringBox(p: Spot): Rect {
  const b = personBox(p), foot = iso(p, 0);
  return { x1: Math.min(b.x1, foot.x - 34), x2: Math.max(b.x2, foot.x + 34), y1: b.y1, y2: foot.y + 20 };
}
/** 기기 앞 바닥 원 */
export function frontBox(f: Spot): Rect {
  const c = iso(f, 0);
  return { x1: c.x - 34, x2: c.x + 34, y1: c.y - 20, y2: c.y + 20 };
}

/** 평면에서 사람 몸이 방 안인지 */
const inRoom = (p: Spot) => p.x - 24 >= ROOM.x && p.x + 24 <= ROOM.x + ROOM.w && p.y - 24 >= ROOM.y && p.y + 24 <= ROOM.y + ROOM.h;

/**
 * 기준 자리에서 dir 방향(+1 평면 아래, −1 위)으로 격자 한 칸씩 옮겨 가며 비어 있는 자리를 찾는다.
 * 방 안이고 무대에서 장애물(기기 본체, 글자, 다른 사람)에 겹치지 않고, ok(이름표 둘 곳이 있는지)를 만족하는 첫 자리.
 * ok를 만족하는 자리가 없으면 겹치지만 않는 첫 자리, 그것도 없으면 기준 자리.
 */
export function freePersonSpot(base: Spot, dir: 1 | -1, obstacles: Rect[], ok: (s: Spot) => boolean = () => true): Spot {
  let loose: Spot | undefined;
  // 기기 앞 방향으로 먼저 찾고, 없으면 옆으로 한 칸씩 비켜서 다시 찾는다
  for (const j of [0, 1, -1, 2, -2, 3, -3, 4, -4, 5, -5, 6, -6]) {
    for (const k of [0, 1, 2, 3, 4, 5, 6, 7, 8, -1, -2, -3, -4]) {
      const s = { x: base.x + j * GRID, y: base.y + dir * k * GRID };
      if (!inRoom(s) || hitsAny(personBox(s), obstacles)) continue;
      if (ok(s)) return s;
      loose ??= s;
    }
  }
  return loose ?? base;
}

/** 이름표를 둘 수 있는 쪽이 하나라도 있는지: 무대 안이고 장애물(글자와 사람)에 겹치지 않는다. 기기 본체는 덮어도 된다. */
export function hasLabelRoom(p: Spot, obstacles: Rect[], w: number, bounds = STAGE_BOUNDS): boolean {
  return (['right', 'left', 'above', 'below'] as LabelSide[]).some((side) => {
    const b = labelBox(p, side, w);
    return inside(b, bounds) && !hitsAny(b, obstacles);
  });
}

export type LabelSide = 'right' | 'left' | 'above' | 'below';
const LABEL_H = 28;
/** 사람 이름표 (Stage.tsx의 Visitor 글자 자리). w는 글자 폭 */
export function labelBox(p: Spot, side: LabelSide, w = 96): Rect {
  const h = iso(p, PERSON_Z);
  if (side === 'right') return { x1: h.x + 22, x2: h.x + 22 + w, y1: h.y - LABEL_H / 2, y2: h.y + LABEL_H / 2 };
  if (side === 'left') return { x1: h.x - 22 - w, x2: h.x - 22, y1: h.y - LABEL_H / 2, y2: h.y + LABEL_H / 2 };
  if (side === 'above') return { x1: h.x - w / 2, x2: h.x + w / 2, y1: h.y - HEAD_R - 6 - LABEL_H, y2: h.y - HEAD_R - 6 };
  const foot = iso(p, 0);
  return { x1: foot.x - w / 2, x2: foot.x + w / 2, y1: foot.y + 8, y2: foot.y + 8 + LABEL_H };
}

/**
 * 이름표는 오른쪽, 왼쪽, 위, 발 아래 순서로 무대 안이고 겹치지 않는 쪽에 둔다. 모두 겹치면 덜 겹치는 쪽.
 * soft(기기 본체)는 글자 뒤 바탕 테가 있어 덮어도 읽히므로, 글자를 덮는 것보다 낫게 친다.
 */
export function chooseLabel(p: Spot, obstacles: Rect[], w = 96, soft: Rect[] = [], bounds = STAGE_BOUNDS): LabelSide {
  return pick(['right', 'left', 'above', 'below'] as LabelSide[], (side) => labelBox(p, side, w), obstacles, soft, bounds);
}

// ── 콜아웃 ──────────────────────────────────────────────────────────────

/** 선분이 상자를 지나는지 (Liang–Barsky) */
export function crosses(a: Spot, b: Spot, r: Rect): boolean {
  let t0 = 0, t1 = 1;
  const dx = b.x - a.x, dy = b.y - a.y;
  const edges: [number, number][] = [[-dx, a.x - r.x1], [dx, r.x2 - a.x], [-dy, a.y - r.y1], [dy, r.y2 - a.y]];
  for (const [p, q] of edges) {
    if (p === 0) { if (q < 0) return false; continue; }
    const t = q / p;
    if (p < 0) { if (t > t1) return false; if (t > t0) t0 = t; } else { if (t < t0) return false; if (t < t1) t1 = t; }
  }
  return t0 < t1;
}

export const CALLOUT_W = 390;
export const CALLOUT_H = 76;

/**
 * 콜아웃 상자 자리. 무대 안에서 장애물을 피하고 가리키는 점에 가장 가까운 곳.
 * 패널이 열려 있으면 패널에 가리지 않는 왼쪽(x ≤ maxX)만 쓴다. 비어 있는 곳이 없으면 가장 덜 겹치는 곳이고 clean은 false.
 */
export function calloutSpot(ax: number, ay: number, obstacles: Rect[], maxX: number, lineObstacles: Rect[] = [], B = STAGE_BOUNDS): Spot & { clean: boolean } {
  const minX = B.x1 + 12;
  const lastX = Math.min(maxX, B.x2 - CALLOUT_W - 12);
  let best: { s: Spot; overlap: number; cross: number; dist: number } | undefined;
  const a = { x: ax, y: ay };
  for (let y = B.y1 + 12; y + CALLOUT_H <= B.y2 - 12; y += 12) {
    for (let x = minX; x <= lastX; x += 12) {
      const box = { x1: x, y1: y, x2: x + CALLOUT_W, y2: y + CALLOUT_H };
      const overlap = obstacles.reduce((n, o) => n + area(box, o), 0);
      const dx = ax < x ? x - ax : ax > x + CALLOUT_W ? ax - x - CALLOUT_W : 0;
      const dy = ay < y ? y - ay : ay > y + CALLOUT_H ? ay - y - CALLOUT_H : 0;
      const dist = Math.hypot(dx, dy) + (dx === 0 && dy === 0 ? 1000 : 0);
      // 지시선이 기기 글자를 가로지르면 덜 좋은 자리로 친다
      const end = leaderEnd({ x, y }, ax, ay);
      const cross = lineObstacles.filter((o) => crosses(a, end, o)).length;
      const better = !best || overlap < best.overlap || (overlap === best.overlap && (cross < best.cross || (cross === best.cross && dist < best.dist)));
      if (better) best = { s: { x, y }, overlap, cross, dist };
    }
  }
  return { ...best!.s, clean: best!.overlap === 0 && best!.cross === 0 };
}

/** 콜아웃 상자에서 가리키는 점에 가장 가까운 테두리 점 (지시선 끝) */
export function leaderEnd(c: Spot, ax: number, ay: number): Spot {
  const x = Math.min(Math.max(ax, c.x), c.x + CALLOUT_W);
  const y = Math.min(Math.max(ay, c.y), c.y + CALLOUT_H);
  if (x > c.x && x < c.x + CALLOUT_W && y > c.y && y < c.y + CALLOUT_H) return { x: c.x, y: c.y + CALLOUT_H / 2 };
  return { x, y };
}

/** 테스트용: 저장한 자리가 모두 그대로 쓰였는지 */
export const canPlaceAll = (arr: Record<string, Spot>, saved: Record<string, Spot>) => Object.entries(saved).every(([id, s]) => arr[id]?.x === s.x && arr[id]?.y === s.y);

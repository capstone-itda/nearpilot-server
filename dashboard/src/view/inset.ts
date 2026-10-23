// 작은 방: 주 장소가 아닌 장소에 사람이 있으면, 그 장소의 기기 상태와 사람을 무대 오른쪽 아래 빈자리에 작은 카드로 줄여 보인다.
// 큰 무대와 달리 투영하지 않고 평면 배치(FloorLayout.devices[].spot)를 그대로 줄인다.
// 글자는 장소 이름과 사람 글자뿐이다 (기기 이름과 상태 글자는 쓰지 않는다. 크게 볼 때는 그 장소로 고정해서 본다).
import type { FloorLayout, MonitorState, Spot } from '../model/types';
import { ROOM, frontOf } from './arrange';
import { ROOM_ISO } from './iso';
import { SLAB_H, STAGE_BOUNDS, WALL_H, deviceLabelBox, hits, labelBox, personBox, ringBox, rssiBox, type Rect } from './placement';
import { baseDevice, type StageOptions, type StageView, type Tone } from './stage';

export interface InsetView {
  placeId: string; placeName: string;
  /** 무대 좌표의 작은 방 상자 */
  box: { x1: number; y1: number; x2: number; y2: number };
  devices: { nodeId: string; x: number; y: number; tone: Tone }[];
  people: { personId: string; letter: string; x: number; y: number }[];
  aria: string;
}

/** 작은 방 상자의 크기와 안쪽 여백, 이름이 들어가는 윗줄의 높이 (무대 단위) */
const W = 252;
const H = 192;
const PAD = 12;
const TITLE_H = 34;
/** 기기 표시(작은 상자)의 크기와 사람 점의 반지름. Stage.tsx가 같은 크기로 그린다. */
export const MARK_W = 16;
export const MARK_H = 12;
export const PERSON_R = 12;
/** 상자가 방과 글자에서 띄우는 거리 */
const GAP = 8;
/** 상자를 둘 수 있는 구역: 무대 오른쪽 아래. 상자 왼쪽 위가 이 안에 든다. */
const AREA = { x1: 700, y1: 420 };
/** 상자 오른쪽 아래가 먼저 놓이는 자리와 자리를 옮기는 간격 */
const CORNER = { x: STAGE_BOUNDS.x2 - 4, y: STAGE_BOUNDS.y2 - 4 };
const STEP = 12;

/** 투영한 방의 바깥 모양(볼록 다각형): 뒤쪽 벽 윗변의 양 끝, 오른쪽·앞쪽·왼쪽 구석의 바닥판 아랫변 */
const ROOM_HULL: Spot[] = (() => {
  const P = ROOM_ISO.P, { x, y, w, h } = ROOM;
  return [P(x, y, WALL_H), P(x + w, y, WALL_H), P(x + w, y, -SLAB_H), P(x + w, y + h, -SLAB_H), P(x, y + h, -SLAB_H), P(x, y + h, WALL_H)];
})();

/** 상자와 볼록 다각형이 겹치는지 (분리축 검사). 닿기만 해도 겹친 것으로 본다. */
function hitsHull(r: Rect, hull: Spot[]): boolean {
  const corners: Spot[] = [{ x: r.x1, y: r.y1 }, { x: r.x2, y: r.y1 }, { x: r.x2, y: r.y2 }, { x: r.x1, y: r.y2 }];
  const axes: Spot[] = [{ x: 1, y: 0 }, { x: 0, y: 1 }];
  hull.forEach((a, i) => { const b = hull[(i + 1) % hull.length]; axes.push({ x: a.y - b.y, y: b.x - a.x }); });
  return axes.every((ax) => {
    const span = (pts: Spot[]) => { const v = pts.map((p) => p.x * ax.x + p.y * ax.y); return [Math.min(...v), Math.max(...v)]; };
    const [a0, a1] = span(corners), [b0, b1] = span(hull);
    return a0 <= b1 && b0 <= a1;
  });
}

/** 큰 무대의 방·기기 글자·사람과 겹치지 않는, 오른쪽 아래 구석에 가장 가까운 자리. 없으면 undefined */
function freeBox(stage: StageView): Rect | undefined {
  const blocks: Rect[] = [
    ...stage.devices.filter((d) => !d.hidden).flatMap((d) => [d.body, deviceLabelBox(d.label), rssiBox(d.rssiAt)]),
    ...stage.visitors.filter((v) => v.on).flatMap((v) => [personBox(v), ringBox(v), labelBox(v, v.label, v.labelW)])
  ];
  let best: { box: Rect; cost: number } | undefined;
  for (let y2 = CORNER.y; y2 - H >= AREA.y1; y2 -= STEP) {
    for (let x2 = CORNER.x; x2 - W >= AREA.x1; x2 -= STEP) {
      const cost = CORNER.x - x2 + (CORNER.y - y2);
      if (best && cost >= best.cost) continue;
      const box = { x1: x2 - W, y1: y2 - H, x2, y2 };
      const wide = { x1: box.x1 - GAP, y1: box.y1 - GAP, x2: box.x2 + GAP, y2: box.y2 + GAP };
      if (blocks.some((b) => hits(wide, b)) || hitsHull(wide, ROOM_HULL)) continue;
      best = { box, cost };
    }
  }
  return best?.box;
}

const tenth = (v: number) => Math.round(v * 10) / 10;

/** 방의 평면을 줄여 상자 안(이름 줄 아래)에 가운데로 놓는 자리: 평면 (ROOM.x, ROOM.y)가 놓이는 곳과 줄인 배율 */
export function planIn(box: Rect): { x: number; y: number; k: number } {
  const inner = { w: W - 2 * PAD, h: H - TITLE_H - PAD };
  const k = Math.min(inner.w / ROOM.w, inner.h / ROOM.h);
  return { x: box.x1 + PAD + (inner.w - ROOM.w * k) / 2, y: box.y1 + TITLE_H + (inner.h - ROOM.h * k) / 2, k };
}

/**
 * 주 장소가 아닌 장소(layout)의 작은 방. 이 장소의 기기 앞에 있는 사람이 없거나, 패널이 열려 있거나, 둘 자리가 없으면 undefined.
 * stage는 큰 무대의 계산 결과다. 작은 방은 그 방(투영한 바닥)과 기기 글자, 사람 이름표, 신호 세기 글자를 피해 놓는다.
 */
export function insetView(s: MonitorState, layout: FloorLayout, stage: StageView, opt: StageOptions): InsetView | undefined {
  if (opt.panelOpen || opt.layoutCheck || layout.placeId === stage.placeId) return undefined;
  const here = new Map(layout.devices.map((d) => [d.nodeId, d]));
  const people = s.people.filter((p) => p.role === 'visitor' && p.letter && p.beaconSeen && p.near !== undefined && here.has(p.near));
  if (!people.length) return undefined;
  const box = freeBox(stage);
  if (!box) return undefined;

  // 방의 평면 배치를 줄여 상자 안에 놓는다
  const { x: ox, y: oy, k } = planIn(box);
  const at = (p: Spot): Spot => ({ x: tenth(ox + (p.x - ROOM.x) * k), y: tenth(oy + (p.y - ROOM.y) * k) });

  const status = new Map(s.devices.map((d) => [d.nodeId, d]));
  const devices = layout.devices.map((d) => ({ nodeId: d.nodeId, ...at(d.spot), tone: baseDevice(status.get(d.nodeId)).tone }));

  // 사람은 쓰는 기기 앞(방 안쪽)에 선다. 기기 표시나 다른 사람과 겹치면 비켜 서고, 방 밖으로 나가지 않는다.
  const lo = { x: ox + PERSON_R, y: oy + PERSON_R }, hi = { x: ox + ROOM.w * k - PERSON_R, y: oy + ROOM.h * k - PERSON_R };
  const placed: Spot[] = [];
  const standing = people.map((p) => {
    const dev = here.get(p.near!)!;
    const front = frontOf(dev.spot);
    const base = at(front);
    const dir = front.y > dev.spot.y ? 1 : -1;
    const ok = (c: Spot) => c.x >= lo.x && c.x <= hi.x && c.y >= lo.y && c.y <= hi.y
      && !devices.some((d) => Math.abs(c.x - d.x) < MARK_W / 2 + PERSON_R && Math.abs(c.y - d.y) < MARK_H / 2 + PERSON_R)
      && placed.every((q) => Math.hypot(q.x - c.x, q.y - c.y) >= 2 * PERSON_R + 2);
    let spot: Spot | undefined;
    search: for (const j of [0, 1, -1, 2, -2, 3, -3]) {
      for (const i of [0, 1, 2, 3, 4, -1, -2]) {
        const c = { x: tenth(base.x + j * (2 * PERSON_R + 4)), y: tenth(base.y + dir * i * 8) };
        if (ok(c)) { spot = c; break search; }
      }
    }
    const stand = spot ?? { x: Math.min(hi.x, Math.max(lo.x, base.x)), y: Math.min(hi.y, Math.max(lo.y, base.y)) };
    placed.push(stand);
    return { personId: p.id, letter: p.letter!, ...stand };
  });

  return {
    placeId: layout.placeId, placeName: layout.placeName, box, devices, people: standing,
    aria: `${layout.placeName}에 ${people.map((p) => p.pseudonym).join(', ')}. 누르면 이 장소로 고정해요`
  };
}

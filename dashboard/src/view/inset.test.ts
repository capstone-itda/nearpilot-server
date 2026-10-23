// FR-12B NFR-10: 주 장소가 아닌 장소에 사람이 있으면 무대 오른쪽 아래에 작은 방으로 줄여 보인다
import { describe, expect, it } from 'vitest';
import { LOUNGE, PLACES, SIDE_ROOM, WEAK_RSSI_DBM } from '../data/mock/fixtures';
import { SCENES } from '../data/mock/story';
import { initialState } from '../data/reduce';
import type { MonitorState, Person, Spot } from '../model/types';
import { ROOM, resolveArrangement, withArrangement } from './arrange';
import { insetView } from './inset';
import { ROOM_ISO, iso } from './iso';
import { CALLOUT_H, CALLOUT_W, SLAB_H, STAGE_BOUNDS, WALL_H, deviceLabelBox, labelBox, personBox, rssiBox, type Rect } from './placement';
import { stageView, type StageOptions } from './stage';

const SIDE = withArrangement(SIDE_ROOM, resolveArrangement(SIDE_ROOM.devices), WEAK_RSSI_DBM);
const LOUNGE_LAYOUT = withArrangement(LOUNGE, resolveArrangement(LOUNGE.devices), WEAK_RSSI_DBM);
const OPT: StageOptions = { panelOpen: false, places: PLACES };

/** 옆방이 주 장소인 장면에서 라운지 작은 방을 계산한다 (App과 같은 순서: 큰 무대 → 작은 방 → 작은 방을 피한 큰 무대) */
function world(s: MonitorState = SCENES['side-room'](), primary = SIDE, other = LOUNGE_LAYOUT, opt = OPT) {
  const base = stageView(s, primary, opt);
  const inset = insetView(s, other, base, opt);
  const stage = inset ? stageView(s, primary, { ...opt, inset: inset.box }) : base;
  return { base, inset, stage };
}

const inBox = (p: Spot, b: Rect) => p.x > b.x1 && p.x < b.x2 && p.y > b.y1 && p.y < b.y2;
/** 투영한 방(바닥판, 낮은 벽의 윗변)을 점으로 훑는다 */
function roomPoints(): Spot[] {
  const out: Spot[] = [];
  for (let x = ROOM.x; x <= ROOM.x + ROOM.w; x += 10) {
    for (let y = ROOM.y; y <= ROOM.y + ROOM.h; y += 10) out.push(iso({ x, y }), iso({ x, y }, -SLAB_H));
  }
  for (let t = 0; t <= 1; t += 0.02) out.push(ROOM_ISO.P(ROOM.x + t * ROOM.w, ROOM.y, WALL_H), ROOM_ISO.P(ROOM.x, ROOM.y + t * ROOM.h, WALL_H));
  return out;
}
const person = (id: string, near?: string): Person => ({
  id, pseudonym: `방문자 ${id.toUpperCase()}`, letter: id.toUpperCase(), role: 'visitor', beaconSeen: near !== undefined, rssi: near ? { [near]: -50 } : {}, near
});

describe('insetView', () => {
  it('FR12B 옆방 장면: 작은 방은 라운지이고 라운지에 있는 사람 글자가 있다', () => {
    const { inset } = world();
    expect(inset).toMatchObject({ placeId: 'lounge', placeName: '공용 라운지' });
    expect(inset!.people.map((p) => p.letter)).toEqual(['B', 'C']);
    expect(inset!.aria).toContain('공용 라운지');
    expect(inset!.aria).toContain('방문자 C');
    // 옆방에 있는 A는 큰 무대에 있으므로 작은 방에 없다
    expect(inset!.people.some((p) => p.personId === 'p-a')).toBe(false);
  });

  it('FR12B 작은 방 상자는 무대 안이고, 기기와 사람은 상자 안에 있다', () => {
    const { inset } = world();
    const b = inset!.box;
    expect(b.x1).toBeGreaterThanOrEqual(STAGE_BOUNDS.x1);
    expect(b.y1).toBeGreaterThanOrEqual(STAGE_BOUNDS.y1);
    expect(b.x2).toBeLessThanOrEqual(STAGE_BOUNDS.x2);
    expect(b.y2).toBeLessThanOrEqual(STAGE_BOUNDS.y2);
    expect(inset!.devices.map((d) => d.nodeId)).toEqual(LOUNGE_LAYOUT.devices.map((d) => d.nodeId));
    for (const p of [...inset!.devices, ...inset!.people]) expect(inBox(p, b), JSON.stringify(p)).toBe(true);
  });

  it('FR12B 작은 방은 큰 무대의 방(투영한 바닥)·기기 글자·사람 이름표·콜아웃과 겹치지 않는다', () => {
    const { inset, stage } = world();
    const b = inset!.box;
    expect(roomPoints().filter((p) => inBox(p, b))).toEqual([]);
    for (const d of stage.devices) for (const r of [d.body, deviceLabelBox(d.label), rssiBox(d.rssiAt)]) expect(hit(b, r), d.name).toBe(false);
    for (const v of stage.visitors.filter((x) => x.on)) for (const r of [personBox(v), labelBox(v, v.label, v.labelW)]) expect(hit(b, r), v.name).toBe(false);
    expect(stage.callout.on).toBe(true);
    expect(hit(b, { x1: stage.callout.x, y1: stage.callout.y, x2: stage.callout.x + CALLOUT_W, y2: stage.callout.y + CALLOUT_H })).toBe(false);
  });

  it('FR12B 작은 방의 기기 색은 큰 무대의 상태 → 색 규칙을 따른다', () => {
    const tone = Object.fromEntries(world().inset!.devices.map((d) => [d.nodeId, d.tone]));
    // 사물함 3은 대여 중(사용 중), 사물함 2는 결과 불명으로 격리, 나머지는 비어 있다
    expect(tone).toMatchObject({ 'locker-3': 'active', 'locker-2': 'deny', 'speaker-1': 'none', 'light-1': 'none' });
  });

  it('FR12B 사람은 쓰는 기기 앞(방 안쪽)에 선다', () => {
    const { inset } = world();
    const dev = (id: string) => inset!.devices.find((d) => d.nodeId === id)!;
    const at = (id: string) => inset!.people.find((p) => p.personId === id)!;
    // C는 위쪽 줄의 사물함 3 앞(아래), B는 아래쪽 줄의 조명 1 앞(위)
    expect(at('p-c').x).toBeCloseTo(dev('locker-3').x, 0);
    expect(at('p-c').y).toBeGreaterThan(dev('locker-3').y);
    expect(at('p-b').x).toBeCloseTo(dev('light-1').x, 0);
    expect(at('p-b').y).toBeLessThan(dev('light-1').y);
  });

  it('FR12B 같은 기기 앞의 두 사람은 겹치지 않는다', () => {
    const s = { ...initialState(PLACES), people: [person('a', 'mood-1'), person('b', 'locker-1'), person('c', 'locker-1')] };
    const { inset } = world(s);
    const [b, c] = inset!.people;
    expect(Math.hypot(b.x - c.x, b.y - c.y)).toBeGreaterThanOrEqual(24);
  });

  it('FR12B 패널이 열려 있으면 작은 방을 숨긴다', () => {
    const base = stageView(SCENES['side-room'](), SIDE, OPT);
    expect(insetView(SCENES['side-room'](), LOUNGE_LAYOUT, base, { ...OPT, panelOpen: true })).toBeUndefined();
    expect(insetView(SCENES['side-room'](), LOUNGE_LAYOUT, base, { ...OPT, layoutCheck: true })).toBeUndefined();
  });

  it('FR12B 사람이 없는 장소는 작은 방이 아니다', () => {
    // 라운지 사람이 모두 비콘을 보내지 않거나 옆방에 있다
    const s = { ...initialState(PLACES), people: [person('a', 'mood-1'), person('b'), person('c', 'mood-1')] };
    expect(world(s).inset).toBeUndefined();
    // 장소의 기기 앞에 선 사람이 없으면 비콘을 받아도 없다
    expect(world({ ...initialState(PLACES), people: [person('a', 'mood-1')] }).inset).toBeUndefined();
  });

  it('FR12B 주 장소 자신은 작은 방이 아니다', () => {
    const s = SCENES['side-room']();
    const base = stageView(s, SIDE, OPT);
    expect(insetView(s, SIDE, base, OPT)).toBeUndefined();
  });

  it('FR12B 큰 무대 쪽에 사람이 있어도 라운지 사람은 작은 방에만 있다 (두 곳에 같은 사람을 그리지 않는다)', () => {
    const { stage, inset } = world();
    const drawn = new Set(stage.visitors.filter((v) => v.on).map((v) => v.personId));
    expect([...drawn]).toEqual(['p-a']);
    expect(inset!.people.some((p) => drawn.has(p.personId))).toBe(false);
  });
});

// 라운지가 주 장소일 때 여러 배치에서도 작은 방(옆방)은 큰 무대의 방·글자·사람과 겹치지 않는다
describe('insetView 배치를 바꾼 라운지', () => {
  const ARRANGEMENTS: Record<string, Record<string, Spot>> = {
    default: {},
    right: { 'speaker-1': { x: 744, y: 720 }, 'light-1': { x: 912, y: 720 }, 'light-2': { x: 1080, y: 720 }, 'light-3': { x: 1248, y: 720 } },
    rightcluster: { 'locker-1': { x: 1248, y: 120 }, 'locker-2': { x: 1248, y: 336 }, 'locker-3': { x: 1080, y: 120 }, 'light-3': { x: 1080, y: 336 },
      'speaker-1': { x: 744, y: 720 }, 'light-1': { x: 912, y: 720 }, 'light-2': { x: 1080, y: 720 } },
    bottomall: { 'locker-1': { x: 408, y: 720 }, 'locker-2': { x: 576, y: 720 }, 'locker-3': { x: 744, y: 720 },
      'speaker-1': { x: 912, y: 720 }, 'light-1': { x: 1080, y: 720 }, 'light-2': { x: 1248, y: 504 }, 'light-3': { x: 1080, y: 504 } }
  };
  // 라운지는 주 장소, 옆방에 A가 있는 장면 (잠금 중 상황)
  const scene = () => SCENES['side-room']();
  for (const [name, saved] of Object.entries(ARRANGEMENTS)) {
    it(`FR12B ${name} 배치: 작은 방이 있으면 큰 무대와 겹치지 않는다`, () => {
      const primary = withArrangement(LOUNGE, resolveArrangement(LOUNGE.devices, saved), WEAK_RSSI_DBM);
      const { inset, stage } = world(scene(), primary, SIDE);
      if (name === 'default') expect(inset, '기본 배치에는 오른쪽 아래 빈자리가 있다').toBeDefined();
      if (!inset) return;
      const b = inset.box;
      expect(b.x2).toBeLessThanOrEqual(STAGE_BOUNDS.x2);
      expect(b.y2).toBeLessThanOrEqual(STAGE_BOUNDS.y2);
      expect(roomPoints().filter((p) => inBox(p, b))).toEqual([]);
      for (const d of stage.devices) for (const r of [d.body, deviceLabelBox(d.label), rssiBox(d.rssiAt)]) expect(hit(b, r), d.name).toBe(false);
      for (const v of stage.visitors.filter((x) => x.on)) for (const r of [personBox(v), labelBox(v, v.label, v.labelW)]) expect(hit(b, r), v.name).toBe(false);
      if (stage.callout.on) expect(hit(b, { x1: stage.callout.x, y1: stage.callout.y, x2: stage.callout.x + CALLOUT_W, y2: stage.callout.y + CALLOUT_H })).toBe(false);
    });
  }
});

describe('insetView 둘 자리가 없을 때', () => {
  it('FR12B 오른쪽 아래 구석이 글자로 막혀 방 밖에 둘 자리가 없으면 방 위에 겹쳐 그리지 않고 작은 방을 그리지 않는다', () => {
    // 조명 3을 방 오른쪽 가운데에 두면 그 글자가 구석을 막고, 구석 밖의 빈자리는 큰 방의 바닥 위다
    const primary = withArrangement(LOUNGE, resolveArrangement(LOUNGE.devices, { 'light-3': { x: 1200, y: 408 } }), WEAK_RSSI_DBM);
    expect(world(SCENES['side-room'](), primary, SIDE).inset).toBeUndefined();
  });
});

function hit(a: Rect, b: Rect) {
  return a.x1 < b.x2 && b.x1 < a.x2 && a.y1 < b.y2 && b.y1 < a.y2;
}

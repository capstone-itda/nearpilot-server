// 무대의 표시용 배치. 실제 위치·거리가 아니다. 서버는 기기 목록만 주고, 자리는 이 PC에서 정한다.
// 좌표는 무대 SVG 단위이고 격자(GRID)에 맞춘다.
import type { DeviceInfo, FloorLayout, PlaceInfo, Spot } from '../model/types';

/** 평면도 SVG 좌표계: 방 벽 사각형 */
export const ROOM = { x: 330, y: 90, w: 1010, h: 650 };
/** 격자 한 칸. 무대 배경의 잔 격자와 같다. */
export const GRID = 24;
/** 기기 본체에서 기기 앞(방문자가 서는 자리)까지 */
const FRONT_GAP = 144;
/** 기기 하나가 차지하는 상자: 본체, 이름표, 신호 세기 글자, 기기 앞 원 */
const HALF_W = 84;
const BODY_H = 21;
const REACH = 192;
/** 본체가 벽선에 걸쳐도 되는 여유 */
const SLACK = 12;
/** 기본 배치: 대여형은 위쪽 벽, 공용형은 아래쪽 벽 */
const ROW_TOP = 120;
const ROW_BOTTOM = 720;
const COL_START = 456;
const COL_STEP = 168;

export type Arrangement = Record<string, Spot>;
export type Facing = 'top' | 'bottom';

export const snap = (v: number) => Math.round(v / GRID) * GRID;

/** 방 위쪽 절반의 기기는 위쪽 벽에 붙은 것으로 보고 이름표와 기기 앞을 아래에 둔다 */
export const facing = (s: Spot): Facing => (s.y < ROOM.y + ROOM.h / 2 ? 'top' : 'bottom');
export const frontOf = (s: Spot): Spot => ({ x: s.x, y: facing(s) === 'top' ? s.y + FRONT_GAP : s.y - FRONT_GAP });

interface Box { x1: number; x2: number; y1: number; y2: number }
function box(s: Spot): Box {
  const top = facing(s) === 'top';
  return { x1: s.x - HALF_W, x2: s.x + HALF_W, y1: top ? s.y - BODY_H : s.y - REACH, y2: top ? s.y + REACH : s.y + BODY_H };
}
const overlap = (a: Box, b: Box) => a.x1 < b.x2 && b.x1 < a.x2 && a.y1 < b.y2 && b.y1 < a.y2;

function insideRoom(s: Spot): boolean {
  const b = box(s);
  return b.x1 >= ROOM.x - SLACK && b.x2 <= ROOM.x + ROOM.w + SLACK && b.y1 >= ROOM.y - SLACK && b.y2 <= ROOM.y + ROOM.h + SLACK;
}

/** 방 안이고 다른 기기와 겹치지 않으면 놓을 수 있다 */
export function canPlace(arr: Arrangement, nodeId: string, s: Spot): boolean {
  if (s.x % GRID !== 0 || s.y % GRID !== 0 || !insideRoom(s)) return false;
  const b = box(s);
  return Object.entries(arr).every(([id, o]) => id === nodeId || !overlap(b, box(o)));
}

/** 선호하는 줄부터 왼쪽에서 오른쪽으로 빈자리를 찾는다 */
function firstFree(arr: Arrangement, nodeId: string, preferTop: boolean): Spot | undefined {
  const rows: number[] = [];
  for (let y = ROOM.y; y <= ROOM.y + ROOM.h; y += GRID) rows.push(y);
  rows.sort((a, b) => Math.abs(a - (preferTop ? ROW_TOP : ROW_BOTTOM)) - Math.abs(b - (preferTop ? ROW_TOP : ROW_BOTTOM)));
  for (const y of rows) {
    for (let x = COL_START; x <= ROOM.x + ROOM.w; x += GRID) {
      if (canPlace(arr, nodeId, { x, y })) return { x, y };
    }
  }
  return undefined;
}

/** 저장한 배치를 지금 기기 목록에 맞춘다. 없는 기기는 빼고, 새 기기와 놓을 수 없는 자리는 빈자리에 넣는다. */
export function resolveArrangement(devices: DeviceInfo[], saved: Arrangement = {}): Arrangement {
  const out: Arrangement = {};
  for (const d of devices) {
    const s = saved[d.nodeId];
    if (s && canPlace(out, d.nodeId, s)) out[d.nodeId] = { x: s.x, y: s.y };
  }
  // 기본 배치는 줄마다 COL_STEP 간격으로 놓고, 자리가 없으면 빈자리를 찾는다
  const next = { top: COL_START, bottom: COL_START };
  for (const d of devices) {
    if (out[d.nodeId]) continue;
    const top = d.occupancy === 'rental';
    const row = top ? 'top' : 'bottom';
    let s: Spot | undefined = { x: next[row], y: top ? ROW_TOP : ROW_BOTTOM };
    while (!canPlace(out, d.nodeId, s) && s.x + HALF_W <= ROOM.x + ROOM.w) s = { ...s, x: s.x + COL_STEP };
    if (!canPlace(out, d.nodeId, s)) s = firstFree(out, d.nodeId, top);
    if (!s) continue;
    out[d.nodeId] = s;
    next[row] = s.x + COL_STEP;
  }
  return out;
}

/** 서버의 장소 정보에 이 PC의 배치를 입힌다. 자리가 없는 기기는 무대에 그리지 않는다. */
export function withArrangement(place: PlaceInfo, arr: Arrangement): FloorLayout {
  return { ...place, devices: place.devices.filter((d) => arr[d.nodeId]).map((d) => ({ ...d, spot: arr[d.nodeId] })) };
}

// ── 파일 내보내기·불러오기 ──────────────────────────────────────────────

export interface ArrangementFile { kind: 'nearpilot-arrangement'; version: 1; place: string; devices: Arrangement }

export const toFile = (place: string, arr: Arrangement): ArrangementFile => ({ kind: 'nearpilot-arrangement', version: 1, place, devices: arr });

/** 불러온 파일을 읽는다. 형식이 틀리면 이유를 돌려준다. */
export function parseFile(text: string): { ok: true; devices: Arrangement; place: string } | { ok: false; error: string } {
  let v: unknown;
  try { v = JSON.parse(text); } catch { return { ok: false, error: 'JSON 파일이 아니에요' }; }
  const f = v as Partial<ArrangementFile>;
  if (!f || f.kind !== 'nearpilot-arrangement' || f.version !== 1 || typeof f.devices !== 'object' || f.devices === null) {
    return { ok: false, error: 'NearPilot 배치 파일이 아니에요' };
  }
  const devices: Arrangement = {};
  for (const [id, s] of Object.entries(f.devices)) {
    if (s && typeof s.x === 'number' && typeof s.y === 'number' && Number.isFinite(s.x) && Number.isFinite(s.y)) devices[id] = { x: snap(s.x), y: snap(s.y) };
  }
  return { ok: true, devices, place: typeof f.place === 'string' ? f.place : '' };
}

/**
 * 불러온 자리를 지금 배치에 한꺼번에 입힌다. 지금 기기 목록에 있는 기기만 옮긴다.
 * 한 대라도 방 밖이거나 겹치면 아무것도 바꾸지 않고 그 기기 이름을 돌려준다.
 */
export function applyFile(arr: Arrangement, devices: DeviceInfo[], spots: Arrangement):
  { ok: true; next: Arrangement; moved: number; unknown: number } | { ok: false; bad: string[] } {
  const known = devices.filter((d) => spots[d.nodeId]);
  const next: Arrangement = { ...arr };
  for (const d of known) next[d.nodeId] = spots[d.nodeId];
  const bad = known.filter((d) => !canPlace(next, d.nodeId, next[d.nodeId])).map((d) => d.name);
  if (bad.length) return { ok: false, bad };
  const ids = new Set(devices.map((d) => d.nodeId));
  return { ok: true, next, moved: known.length, unknown: Object.keys(spots).filter((id) => !ids.has(id)).length };
}

// 무대의 아이소메트릭 투영 (iso-glow, MIT, github.com/MrBongoC/ai-iso-skill의 iso-figure 커널).
// 배치는 평면 좌표(arrange.ts의 방 좌표)로 정하고, 무대에는 이 투영으로 그린다.
// +x는 오른쪽 아래, +y는 왼쪽 아래, +z는 위. 평면 좌표는 SVG 단위 그대로 쓰고 ISO_K배로 줄인다.
import type { Spot } from '../model/types';
import { ROOM } from './arrange';

const C = Math.cos(Math.PI / 6);
const S = 0.5;
/** 평면 단위 → 무대 단위 */
export const ISO_K = 0.66;
/** 방의 뒤쪽 구석(평면 ROOM.x, ROOM.y)이 무대에 놓이는 자리 */
export const ISO_ORIGIN: Spot = { x: 745, y: 140 };

export type P3 = [number, number, number];

export interface Kernel {
  P: (x: number, y: number, z?: number) => Spot;
  /** 면 위에 평면 도형을 그리는 transform. 면의 왼쪽 위 모서리에서 u는 오른쪽, v는 아래로 자란다. */
  TOP: (x: number, y: number, z: number) => string;
  FRONT: (x: number, y: number, z: number) => string;
  SIDE: (x: number, y: number, z: number) => string;
  poly: (pts: P3[]) => string;
  line: (pts: P3[]) => string;
  /** 보이는 세 면(옆, 앞, 위)의 path */
  box: (x: number, y: number, z: number, w: number, d: number, h: number) => { side: string; front: string; top: string };
  /** 바닥 원(타원) */
  ring: (cx: number, cy: number, r: number, z?: number) => string;
}

const f = (n: number) => Math.round(n * 100) / 100;

/** (0,0,0)이 화면 (ox, oy)에 오는 투영 */
export function kernel(ox: number, oy: number, k = ISO_K): Kernel {
  const P = (x: number, y: number, z = 0): Spot => ({ x: ox + (x - y) * C * k, y: oy + (x + y) * S * k - z * k });
  const D = (x: number, y: number, z: number): [number, number] => [(x - y) * C * k, (x + y) * S * k - z * k];
  const plane = (o: P3, u: P3, v: P3) => {
    const p = P(...o), a = D(...u), b = D(...v);
    return `matrix(${f(a[0])} ${f(a[1])} ${f(b[0])} ${f(b[1])} ${f(p.x)} ${f(p.y)})`;
  };
  const pts = (list: P3[]) => list.map((q) => { const p = P(...q); return `${f(p.x)} ${f(p.y)}`; }).join('L');
  const poly = (list: P3[]) => `M${pts(list)}Z`;
  return {
    P,
    TOP: (x, y, z) => plane([x, y, z], [1, 0, 0], [0, 1, 0]),
    FRONT: (x, y, z) => plane([x, y, z], [1, 0, 0], [0, 0, -1]),
    SIDE: (x, y, z) => plane([x, y, z], [0, -1, 0], [0, 0, -1]),
    poly,
    line: (list) => `M${pts(list)}`,
    box: (x, y, z, w, d, h) => ({
      side: poly([[x + w, y, z], [x + w, y + d, z], [x + w, y + d, z + h], [x + w, y, z + h]]),
      front: poly([[x, y + d, z], [x + w, y + d, z], [x + w, y + d, z + h], [x, y + d, z + h]]),
      top: poly([[x, y, z + h], [x + w, y, z + h], [x + w, y + d, z + h], [x, y + d, z + h]])
    }),
    ring: (cx, cy, r, z = 0) => {
      const n = 32;
      const list: P3[] = [];
      for (let i = 0; i < n; i++) list.push([cx + r * Math.cos((2 * Math.PI * i) / n), cy + r * Math.sin((2 * Math.PI * i) / n), z]);
      return poly(list);
    }
  };
}

/** 방 투영: 평면 좌표를 그대로 넣는다 */
export const ROOM_ISO = kernel(ISO_ORIGIN.x - (ROOM.x - ROOM.y) * C * ISO_K, ISO_ORIGIN.y - (ROOM.x + ROOM.y) * S * ISO_K);

/** 평면 좌표 → 무대 좌표 (바닥 높이 z) */
export const iso = (p: Spot, z = 0): Spot => ROOM_ISO.P(p.x, p.y, z);

/** 무대 좌표 → 바닥(z = 0)의 평면 좌표. 배치 화면에서 끄는 자리를 읽는다. */
export function unIso(s: Spot): Spot {
  const a = (s.x - ISO_ORIGIN.x) / (C * ISO_K);
  const b = (s.y - ISO_ORIGIN.y) / (S * ISO_K);
  return { x: ROOM.x + (a + b) / 2, y: ROOM.y + (b - a) / 2 };
}

// 무대: 표시용 배치 위의 기기, 사람, 무선 흐름(D5). 배치는 이 PC에서 정한다(D4). 실제 위치·거리가 아니다.
// 그림은 iso-glow 헤어라인 아이소메트릭이다: 어두운 판에 가는 선, 살아 있는 것만 판정 색으로 빛난다.
// 평면 배치를 투영해 그리고(view/iso.ts), 글자는 투영한 자리에 똑바로 쓴다.
import { useEffect, useRef, useState, type KeyboardEvent, type PointerEvent, type ReactNode } from 'react';
import type { Spot } from '../model/types';
import { GRID, ROOM, facing, frontOf, snap } from '../view/arrange';
import { ISO_K, ROOM_ISO, iso, kernel, unIso, type Kernel } from '../view/iso';
import { DEVICE_D, DEVICE_H, DEVICE_LABEL_H, DEVICE_W, HEAD_R, PERSON_Z, deviceBody, labelBox } from '../view/placement';
import type { StageDeviceView, StageView, StageVisitorView } from '../view/stage';
import { FIT, ZOOM_MAX, ZOOM_STEP, isFit, panBy, viewBoxOf, zoomAt, type ZoomView } from '../view/zoom';
import { Icon } from './common';

/** SET-01 배치 편집. 모니터 무대에는 넘기지 않는다. */
export interface StageEdit {
  canPlace: (nodeId: string, s: Spot) => boolean;
  onMove: (nodeId: string, s: Spot) => void;
}

const b = (v: unknown) => (v ? 'true' : 'false');
const pt = (p: Spot) => `${Math.round(p.x * 10) / 10} ${Math.round(p.y * 10) / 10}`;
const pathOf = (list: Spot[]) => (list.length ? `M${list.map(pt).join('L')}` : '');

/** 화살촉: 마지막 두 점의 방향으로 */
function arrowOf(list: Spot[]): string {
  if (list.length < 2) return '';
  const a = list[list.length - 2], e = list[list.length - 1];
  const len = Math.hypot(e.x - a.x, e.y - a.y) || 1;
  const ux = (e.x - a.x) / len, uy = (e.y - a.y) / len;
  const bx = e.x - ux * 13, by = e.y - uy * 13;
  return `${pt({ x: bx - uy * 7, y: by + ux * 7 })} ${pt(e)} ${pt({ x: bx + uy * 7, y: by - ux * 7 })}`;
}

interface DeviceEditProps {
  invalid: boolean;
  onPointerDown: (e: PointerEvent<SVGGElement>) => void;
  onKeyDown: (e: KeyboardEvent<SVGGElement>) => void;
}

/** 메시지 하나: 흐름 선이 켜질 때 점이 출발지에서 도착지까지 한 번 이동한다 (ARRIVE_SEC). 도착지의 바뀐 표시는 그 뒤에 나타난다 (np-flash). */
export const ARRIVE_SEC = 0.6;
function Packet({ d, tone }: { d: string; tone: string }) {
  const move = useRef<SVGAnimationElement>(null);
  const fade = useRef<SVGAnimationElement>(null);
  useEffect(() => { move.current?.beginElement?.(); fade.current?.beginElement?.(); }, []);
  return (
    <circle className="np-stage__packet" data-tone={tone} r="7" opacity="0">
      <animateMotion ref={move} dur={`${ARRIVE_SEC}s`} begin="indefinite" fill="freeze" path={d} />
      <animate ref={fade} attributeName="opacity" values="1;1;0" keyTimes="0;0.8;1" dur={`${ARRIVE_SEC + 0.15}s`} begin="indefinite" fill="freeze" />
    </circle>
  );
}

/** 신호가 지나는 선: 켜지면 판정 색으로 빛나고 점이 한 번 지나간다 */
function Flow({ on, tone, points }: { on: boolean; tone: string; points: Spot[] }) {
  const d = pathOf(points);
  return (
    <g data-on={b(on)} data-tone={tone}>
      <path className="np-stage__flow" d={d} />
      <polygon className="np-stage__arrow" points={arrowOf(points)} />
      {on && d ? <Packet d={d} tone={tone} /> : null}
    </g>
  );
}

// ── 기기 ────────────────────────────────────────────────────────────────

/** 기기 본체: 상자 세 면과 앞면의 부품. 켜진 기기는 유리(문, 스피커 망, 등)가 빛난다. */
function DeviceFigure({ d }: { d: StageDeviceView }) {
  const K = ROOM_ISO;
  const h = DEVICE_H[d.capability];
  const x0 = d.x - DEVICE_W / 2, y0 = d.y - DEVICE_D / 2;
  const f = K.box(x0, y0, 0, DEVICE_W, DEVICE_D, h);
  const front = K.FRONT(x0, y0 + DEVICE_D, h);
  const side = K.SIDE(x0 + DEVICE_W, y0 + DEVICE_D, h);
  return (
    <g className="np-stage__device-body">
      <path className="np-iso__face" d={f.side} />
      <path className="np-iso__face" d={f.front} />
      <path className="np-iso__face np-iso__top" d={f.top} />
      {d.capability === 'locker' ? (
        <>
          <g transform={front}><rect className="np-iso__glass" x="7" y="8" width="46" height={h - 16} rx="2" /><rect className="np-iso__detail-fill" x="44" y={h / 2 - 8} width="3" height="16" /></g>
          <g transform={side}>{[0, 1, 2, 3].map((i) => <path key={i} className="np-iso__detail" d={`M8 ${14 + i * 6}H28`} />)}</g>
        </>
      ) : d.capability === 'speaker' ? (
        <>
          <g transform={front}><circle className="np-iso__glass" cx="30" cy={h / 2} r="17" /><circle className="np-iso__detail" cx="30" cy={h / 2} r="7" /></g>
          <g transform={side}>{[0, 1, 2].map((i) => <path key={i} className="np-iso__detail" d={`M8 ${14 + i * 8}H28`} />)}</g>
        </>
      ) : null}
    </g>
  );
}

/** 조명: 가는 받침 위의 등머리. 켜지면 등머리 앞면과 윗면이 빛난다 */
function LightFigure({ d }: { d: StageDeviceView }) {
  const K = ROOM_ISO;
  const h = DEVICE_H.light, head = 26, pw = 20;
  const x0 = d.x - DEVICE_W / 2, y0 = d.y - DEVICE_D / 2;
  const post = K.box(d.x - pw / 2, d.y - pw / 2, 0, pw, pw, h - head);
  const top = K.box(x0, y0, h - head, DEVICE_W, DEVICE_D, head);
  const foot = K.box(d.x - 18, d.y - 14, 0, 36, 28, 5);
  return (
    <g className="np-stage__device-body">
      {[foot, post, top].map((f, i) => <g key={i}><path className="np-iso__face" d={f.side} /><path className="np-iso__face" d={f.front} /><path className="np-iso__face np-iso__top" d={f.top} /></g>)}
      <g transform={K.FRONT(x0, y0 + DEVICE_D, h)}><rect className="np-iso__glass" x="6" y="6" width={DEVICE_W - 12} height={head - 12} rx="2" /></g>
      <g transform={K.TOP(x0, y0, h)}><rect className="np-iso__glass" x="6" y="5" width={DEVICE_W - 12} height={DEVICE_D - 10} rx="3" /></g>
    </g>
  );
}

function Device({ d, edit }: { d: StageDeviceView; edit?: DeviceEditProps }) {
  const editAttrs = edit ? {
    tabIndex: 0, role: 'button', 'aria-label': `${d.name} 옮기기. 끌거나 화살표 키로 한 칸씩 옮겨요`,
    'data-edit': 'true', 'data-invalid': b(edit.invalid), onPointerDown: edit.onPointerDown, onKeyDown: edit.onKeyDown
  } : {};
  const l = d.label;
  return (
    <g className="np-stage__device np-flash" data-component="StageDevice" data-spot={`${d.x} ${d.y}`} data-tone={d.tone} data-dim={b(d.dim)} data-on={b(!d.hidden)} {...editAttrs}>
      {edit ? <rect className="np-stage__hit" x={d.body.x1 - 12} y={d.body.y1 - 12} width={d.body.x2 - d.body.x1 + 24} height={d.body.y2 - d.body.y1 + 24} /> : null}
      {d.capability === 'light' ? <LightFigure d={d} /> : <DeviceFigure d={d} />}
      <text className="np-stage__label" x={l.x} y={l.y + 19} data-anchor="middle">{d.name}</text>
      <text className="np-stage__device-status" x={l.x} y={l.y + DEVICE_LABEL_H - 6} data-anchor="middle">{d.status}</text>
    </g>
  );
}

// ── 사람 ────────────────────────────────────────────────────────────────

/** 사람 점이 다른 기기 앞으로 옮길 때 미끄러지는 시간 (초). 표시용 값이다. */
const SLIDE_SEC = 0.6;
const ORIGIN = kernel(0, 0);
const RINGS = [36, 54, 72].map((r) => ORIGIN.ring(0, 0, r));
const HEAD_Y = -PERSON_Z * ISO_K;

function Visitor({ v }: { v: StageVisitorView }) {
  // 자리가 바뀌면 지난 자리에서 새 자리로 미끄러진다 (순간 이동하면 눈이 따라가지 못한다)
  const foot = iso(v);
  const at = pt(foot);
  const shown = useRef(at);
  const slide = useRef<{ from: string; to: string } | null>(null);
  if (shown.current !== at) { slide.current = { from: shown.current, to: at }; shown.current = at; }
  const anim = useRef<SVGAnimateTransformElement>(null);
  useEffect(() => {
    if (window.matchMedia?.('(prefers-reduced-motion: reduce)').matches) return;
    anim.current?.beginElement?.();
  }, [at]);
  // 이름표 자리는 무대 좌표로 고르고, 사람 묶음 안에서는 발 기준으로 옮겨 쓴다
  const lb = labelBox(v, v.label, v.labelW);
  const lx = v.label === 'left' ? lb.x2 - foot.x : v.label === 'right' ? lb.x1 - foot.x : (lb.x1 + lb.x2) / 2 - foot.x;
  const ly = (lb.y1 + lb.y2) / 2 + 7 - foot.y;
  const anchor = v.label === 'left' ? 'end' : v.label === 'right' ? 'start' : 'middle';
  return (
    <g className="np-stage__visitor" data-component="StageVisitor" data-active={b(v.active)} data-dim={b(v.dim)} data-on={b(v.on)} transform={`translate(${at})`}>
      {slide.current ? <animateTransform key={slide.current.to} ref={anim} attributeName="transform" type="translate" from={slide.current.from} to={slide.current.to} dur={`${SLIDE_SEC}s`} begin="indefinite" fill="freeze" calcMode="spline" keyTimes="0;1" keySplines="0.4 0 0.2 1" /> : null}
      {RINGS.map((d, i) => <path key={i} className="np-stage__ring" d={d} />)}
      <path className="np-stage__visitor-stem" d={`M0 0V${HEAD_Y + HEAD_R}`} />
      <circle className="np-stage__visitor-dot" cy={HEAD_Y} r={HEAD_R} />
      <text className="np-stage__visitor-letter" y={HEAD_Y + 6} data-anchor="middle">{v.letter}</text>
      <text className="np-stage__label" x={lx} y={ly} data-anchor={anchor}>{v.name}</text>
    </g>
  );
}

// ── 왼쪽 칸: AI 호스트, 서버, 소유자 폰 ─────────────────────────────────

const HOST_K = kernel(80, 226);
const SERVER_K = kernel(85, 462);
const OWNER_K = kernel(95, 650);
const RACK = { w: 64, d: 44, h: 150 };

function Faces({ K, x, y, z, w, d, h }: { K: Kernel; x: number; y: number; z: number; w: number; d: number; h: number }) {
  const f = K.box(x, y, z, w, d, h);
  return <><path className="np-iso__face" d={f.side} /><path className="np-iso__face" d={f.front} /><path className="np-iso__face np-iso__top" d={f.top} /></>;
}

/** AI 호스트: 받침 위의 화면. 요청을 보내는 동안 화면이 빛난다. */
function HostNode({ active }: { active: boolean }) {
  return (
    <g className="np-stage__node" data-active={b(active)} data-dim="false">
      <Faces K={HOST_K} x={0} y={0} z={0} w={80} d={56} h={14} />
      <Faces K={HOST_K} x={8} y={6} z={14} w={64} d={10} h={56} />
      <g transform={HOST_K.FRONT(8, 16, 70)}><rect className="np-iso__glass" x="6" y="6" width="52" height="40" rx="2" /></g>
      <text className="np-stage__label" x={160} y={204}>AI 호스트</text>
    </g>
  );
}

const CHECK_MARK = { pass: '✓', fail: '✕', wait: '…', unsure: '?', none: '·', skip: '–' };

/** 서버: 확인 네 칸마다 불이 하나씩 있는 랙. 판정 전에는 앞면 위쪽의 판정 게이지가 찬다. 판정이 나면 다시 그려져 도착 강조가 한 번 나온다. */
function ServerNode({ on, j }: { on: boolean; j: StageView['judge'] }) {
  return (
    <g className="np-stage__node np-stage__server np-flash" data-component="StageServer" data-active={b(on)} data-dim="false" data-tone={j.tone}>
      <Faces K={SERVER_K} x={0} y={0} z={0} w={RACK.w} d={RACK.d} h={RACK.h} />
      <g transform={SERVER_K.FRONT(0, RACK.d, RACK.h)}>
        <rect className="np-stage__gauge" x="8" y="12" width="48" height="6" />
        {j.gauge ? <rect className="np-stage__gauge-fill" x="8" y="12" width="48" height="6" /> : null}
        {j.checks.map(([res], k) => (
          <g key={k}>
            <rect className="np-stage__led" data-result={res} x="8" y={38 + k * 26} width="12" height="8" rx="1" />
            <path className="np-iso__detail" d={`M26 ${42 + k * 26}H56`} />
          </g>
        ))}
      </g>
      <g transform={SERVER_K.SIDE(RACK.w, RACK.d, RACK.h)}>{[0, 1, 2, 3, 4, 5].map((i) => <path key={i} className="np-iso__detail" d={`M8 ${20 + i * 18}H36`} />)}</g>
    </g>
  );
}

/** 서버 글자: 서버에서 방으로 가는 선 위에 그려, 글자 테가 선을 가린다 */
function ServerText({ j }: { j: StageView['judge'] }) {
  return (
    <g className="np-stage__node np-flash" data-dim="false" data-tone={j.tone}>
      <text className="np-stage__label" x={160} y={384}>서버</text>
      <text className="np-stage__judge-mark" x={300} y={384} data-anchor="end">{j.mark}</text>
      {j.checks.map(([res, text], k) => (
        <text key={k} className="np-stage__judge-row" data-result={res} x={160} y={414 + k * 24}><tspan className="np-stage__judge-icon">{CHECK_MARK[res]}</tspan> {text}</text>
      ))}
    </g>
  );
}

/** 소유자 폰: 바닥에 놓인 폰. 승인 알림이 오면 화면이 승인 색으로 빛난다. */
function OwnerNode({ active }: { active: boolean }) {
  return (
    <g className="np-stage__node np-flash" data-active={b(active)} data-dim="false" data-tone="appr">
      <Faces K={OWNER_K} x={0} y={0} z={0} w={40} d={72} h={6} />
      <g transform={OWNER_K.TOP(0, 0, 6)}><rect className="np-iso__glass" x="4" y="6" width="32" height="58" rx="3" /></g>
      <text className="np-stage__label" x={160} y={660}>소유자 폰</text>
      <text className="np-stage__sub" x={160} y={686}>승인 알림</text>
    </g>
  );
}

// ── 배선 ────────────────────────────────────────────────────────────────

const HOST_TO_SERVER: Spot[] = [{ x: 90, y: 270 }, { x: 90, y: 382 }];
const SERVER_TO_OWNER: Spot[] = [{ x: 82, y: 492 }, { x: 82, y: 648 }];
/** 서버 옆면에서 나와 확인 글자 아래로 지나는 선 */
const TRUNK: Spot[] = [{ x: 112, y: 446 }, { x: 146, y: 446 }, { x: 146, y: 506 }, { x: 300, y: 506 }];
/** 방이 왼쪽 칸 뒤로 잘리는 선 */
const CLIP_X = 322;
/** 잘린 가장자리를 흐리게 지우는 폭 */
const FADE_W = 240;
const BACK = ROOM.y + 8, FRONT_EDGE = ROOM.y + ROOM.h - 8, LEFT = ROOM.x + 8;

/** 방 안의 배선: 왼쪽 벽을 따라 뒤쪽 벽 또는 앞쪽 가장자리로 간 뒤 기기 쪽으로 꺾는다 */
function floorPlan(d: { x: number; y: number }): Spot[] {
  return facing(d) === 'top'
    ? [{ x: LEFT, y: FRONT_EDGE }, { x: LEFT, y: BACK }, { x: d.x, y: BACK }, { x: d.x, y: d.y - DEVICE_D / 2 }]
    : [{ x: LEFT, y: FRONT_EDGE }, { x: d.x, y: FRONT_EDGE }, { x: d.x, y: d.y + DEVICE_D / 2 }];
}
const floorRoute = (d: { x: number; y: number }): Spot[] => floorPlan(d).map((p) => iso(p));
const moveX = (list: Spot[], dx: number) => list.map((p) => ({ x: p.x - dx, y: p.y }));
/** 서버에서 방 안 배선(방의 왼쪽 구석에서 시작하는 평면 경로)까지. 방을 밀면 잘림 선을 넘는 자리에서 잇는다. */
function fromServer(plan: Spot[], shift: number): Spot[] {
  const pts = moveX(plan.map((p) => iso(p)), shift);
  const i = pts.findIndex((p) => p.x >= CLIP_X);
  if (i <= 0) return [...TRUNK, ...pts.slice(Math.max(i, 0))];
  const a = pts[i - 1], b = pts[i], t = (CLIP_X - a.x) / (b.x - a.x);
  return [...TRUNK, { x: CLIP_X, y: a.y + (b.y - a.y) * t }, ...pts.slice(i)];
}
/** 서버에서 기기까지 (무대 좌표) */
const routeTo = (d: StageDeviceView | undefined, shift: number): Spot[] => (d ? fromServer(floorPlan(d), shift) : []);

const ARROWS: Record<string, [number, number]> = { ArrowLeft: [-GRID, 0], ArrowRight: [GRID, 0], ArrowUp: [0, -GRID], ArrowDown: [0, GRID] };

/** 바닥 격자: 평면 120 단위마다 */
const FLOOR_GRID = (() => {
  const out: string[] = [];
  for (let x = ROOM.x + 120; x < ROOM.x + ROOM.w; x += 120) out.push(ROOM_ISO.line([[x, ROOM.y, 0], [x, ROOM.y + ROOM.h, 0]]));
  for (let y = ROOM.y + 120; y < ROOM.y + ROOM.h; y += 120) out.push(ROOM_ISO.line([[ROOM.x, y, 0], [ROOM.x + ROOM.w, y, 0]]));
  return out.join('');
})();
const SLAB = ROOM_ISO.box(ROOM.x, ROOM.y, -12, ROOM.w, ROOM.h, 12);
const WALL_H = 34;
const WALL_LEFT = ROOM_ISO.poly([[ROOM.x, ROOM.y, 0], [ROOM.x, ROOM.y + ROOM.h, 0], [ROOM.x, ROOM.y + ROOM.h, WALL_H], [ROOM.x, ROOM.y, WALL_H]]);
const WALL_BACK = ROOM_ISO.poly([[ROOM.x, ROOM.y, 0], [ROOM.x + ROOM.w, ROOM.y, 0], [ROOM.x + ROOM.w, ROOM.y, WALL_H], [ROOM.x, ROOM.y, WALL_H]]);

/** 모니터 무대의 확대·이동 (설정의 배치 화면에서는 쓰지 않는다) */
export interface StageZoom {
  view: ZoomView;
  onChange: (v: ZoomView) => void;
  /** 그림 영역의 화면 크기와, 무대 오른쪽 끝에서 그림 영역까지의 여백(px). 패널 경계를 무대 좌표로 바꿀 때 쓴다. */
  onSize: (s: { w: number; h: number; right: number }) => void;
}

export function Stage({ v, edit, zoom }: { v: StageView; edit?: StageEdit; zoom?: StageZoom }) {
  const svg = useRef<SVGSVGElement>(null);
  const section = useRef<HTMLElement>(null);
  const pan = useRef<{ x: number; y: number; view: ZoomView; scale: number } | null>(null);
  const zoomRef = useRef(zoom);
  zoomRef.current = zoom;
  /** 화면 좌표 → 무대 좌표 */
  const toStage = (x: number, y: number): Spot => {
    const m = svg.current?.getScreenCTM();
    if (!m) return { x: 0, y: 0 };
    const p = new DOMPoint(x, y).matrixTransform(m.inverse());
    return { x: p.x, y: p.y };
  };
  // 그림 영역 크기를 알린다
  const sized = !!zoom;
  useEffect(() => {
    const el = svg.current, sec = section.current;
    if (!sized || !el || !sec) return;
    const report = () => {
      const a = el.getBoundingClientRect(), s = sec.getBoundingClientRect();
      zoomRef.current?.onSize({ w: a.width, h: a.height, right: s.right - a.right });
    };
    const ro = new ResizeObserver(report);
    ro.observe(sec);
    report();
    return () => ro.disconnect();
  }, [sized]);
  // 휠(트랙패드 핀치 포함)로 커서 자리를 중심으로 확대·축소한다. 페이지가 스크롤되지 않게 passive를 끈다.
  useEffect(() => {
    const el = svg.current;
    if (!sized || !el) return;
    const onWheel = (e: WheelEvent) => {
      const z = zoomRef.current;
      if (!z) return;
      e.preventDefault();
      const unit = e.deltaMode === 1 ? 16 : 1;
      z.onChange(zoomAt(z.view, toStage(e.clientX, e.clientY), Math.exp(-e.deltaY * unit * 0.0015)));
    };
    el.addEventListener('wheel', onWheel, { passive: false });
    return () => el.removeEventListener('wheel', onWheel);
  }, [sized]);
  const [drag, setDrag] = useState<{ id: string; dx: number; dy: number; s: Spot; ok: boolean } | null>(null);
  /** 누른 자리의 바닥 평면 좌표 */
  const toPlan = (e: PointerEvent): Spot => {
    const m = svg.current?.getScreenCTM();
    if (!m) return { x: 0, y: 0 };
    const p = new DOMPoint(e.clientX, e.clientY).matrixTransform(m.inverse());
    return unIso({ x: p.x, y: p.y });
  };
  const moved = (d: StageDeviceView, s: Spot): StageDeviceView => {
    const f = frontOf(s);
    const body = deviceBody(s.x, s.y, d.capability);
    const dx = (body.x1 + body.x2) / 2 - (d.body.x1 + d.body.x2) / 2, dy = body.y2 - d.body.y2;
    return { ...d, x: s.x, y: s.y, fx: f.x, fy: f.y, wall: facing(s), body, label: { ...d.label, x: d.label.x + dx, y: d.label.y + dy } };
  };
  const devices = v.devices.map((d) => (drag && drag.id === d.nodeId ? moved(d, drag.s) : d));
  const editFor = (d: StageDeviceView): DeviceEditProps | undefined => edit && {
    invalid: !!drag && drag.id === d.nodeId && !drag.ok,
    onPointerDown: (e) => {
      e.currentTarget.setPointerCapture(e.pointerId);
      const p = toPlan(e);
      setDrag({ id: d.nodeId, dx: p.x - d.x, dy: p.y - d.y, s: { x: d.x, y: d.y }, ok: true });
    },
    onKeyDown: (e) => {
      const step = ARROWS[e.key];
      if (!step) return;
      e.preventDefault();
      // 이웃 기기에 막히면 그 너머의 첫 빈자리까지 건너뛴다
      for (let k = 1; k <= 60; k++) {
        const s = { x: d.x + step[0] * k, y: d.y + step[1] * k };
        if (edit.canPlace(d.nodeId, s)) { edit.onMove(d.nodeId, s); break; }
      }
    }
  };
  // 확대한 무대는 끌어서 옮긴다
  const onPanStart = (e: PointerEvent<SVGSVGElement>) => {
    if (!zoom || edit || e.button !== 0) return;
    const m = svg.current?.getScreenCTM();
    if (!m) return;
    e.currentTarget.setPointerCapture(e.pointerId);
    pan.current = { x: e.clientX, y: e.clientY, view: zoom.view, scale: Math.hypot(m.a, m.b) };
  };
  const onPointerMove = (e: PointerEvent<SVGSVGElement>) => {
    const p0 = pan.current;
    if (p0 && zoom) { zoom.onChange(panBy(p0.view, (e.clientX - p0.x) / p0.scale, (e.clientY - p0.y) / p0.scale)); return; }
    if (!drag || !edit) return;
    const p = toPlan(e);
    const s = { x: snap(p.x - drag.dx), y: snap(p.y - drag.dy) };
    if (s.x !== drag.s.x || s.y !== drag.s.y) setDrag({ ...drag, s, ok: edit.canPlace(drag.id, s) });
  };
  const onPointerUp = () => {
    pan.current = null;
    if (drag && edit && drag.ok) edit.onMove(drag.id, drag.s);
    setDrag(null);
  };

  const shift = v.shift;
  const bus = devices.map((d) => pathOf(floorRoute(d))).join('');
  const cmd = routeTo(devices.find((d) => d.nodeId === v.cmd), shift);
  const fault = routeTo(devices.find((d) => d.nodeId === v.fault), shift);
  // 비콘 신호는 방 안쪽 끝의 기기에서 뒤쪽 벽과 왼쪽 벽을 따라 서버로 올라간다
  const farX = Math.max(...devices.map((d) => d.x), ROOM.x + ROOM.w / 2);
  const uplink = fromServer([{ x: LEFT, y: FRONT_EDGE }, { x: LEFT, y: BACK }, { x: farX, y: BACK }], shift).reverse();
  // 패널이 열려 방을 밀었으면 왼쪽 칸 뒤로 들어간 부분을 잘라 낸다. 바닥은 잘린 쪽을 흐리게 지워 잘렸음을 보이고,
  // 글자가 있는 층은 흐리지 않게 경계에서 자르기만 한다.
  // 구조는 늘 같게 두어(컴포넌트가 아닌 함수) 밀 때 기기와 사람이 다시 그려지지 않는다
  const room = (children: ReactNode, fade = false) => (
    <g mask={shift && fade ? 'url(#np-room-cut)' : undefined} clipPath={shift && !fade ? 'url(#np-room-clip)' : undefined}><g transform={shift ? `translate(${-shift} 0)` : undefined}>{children}</g></g>
  );
  const c = v.callout;
  // 앞에 있는 것(평면 x + y가 큰 것)을 나중에 그린다
  const depth = [
    ...devices.map((d) => ({ k: d.x + d.y, el: <Device key={`${d.nodeId}|${d.status}|${d.tone}`} d={d} edit={editFor(d)} /> })),
    ...v.visitors.map((x) => ({ k: x.x + x.y + 1, el: <Visitor key={x.personId} v={x} /> }))
  ].sort((a, z) => a.k - z.k);

  return (
    <section ref={section} className="np-stage" data-component="Stage" data-scene={v.scene} data-zoomed={b(!!zoom && !isFit(zoom.view))} aria-label={`${v.placeName} 배치도`}>
      <svg ref={svg} className="np-stage__svg" data-editing={b(!!drag)} onPointerDown={onPanStart} onPointerMove={onPointerMove} onPointerUp={onPointerUp} onPointerCancel={() => { pan.current = null; setDrag(null); }}
        viewBox={v.scene === 'layout' ? '300 0 1100 800' : (zoom ? viewBoxOf(zoom.view) : viewBoxOf(FIT)).join(' ')} preserveAspectRatio="xMidYMid meet" role="img" aria-label={v.aria}>
        <defs>
          <filter id="np-glow" filterUnits="userSpaceOnUse" x="-2000" y="-2000" width="6000" height="6000"><feGaussianBlur stdDeviation="4" result="b" /><feMerge><feMergeNode in="b" /><feMergeNode in="b" /><feMergeNode in="SourceGraphic" /></feMerge></filter>
          {/* 방을 밀면 왼쪽 칸 쪽 가장자리를 흐리게 지워, 방이 그 뒤로 이어진다는 것을 보인다 */}
          <linearGradient id="np-room-fade" gradientUnits="userSpaceOnUse" x1={CLIP_X} y1="0" x2={CLIP_X + FADE_W} y2="0">
            <stop offset="0" stopColor="#fff" stopOpacity="0" /><stop offset="1" stopColor="#fff" stopOpacity="1" />
          </linearGradient>
          <clipPath id="np-room-clip"><rect x={CLIP_X} y="0" width="1100" height="800" /></clipPath>
          <mask id="np-room-cut" maskUnits="userSpaceOnUse" x="0" y="0" width="1400" height="800">
            <rect x={CLIP_X} y="0" width="1100" height="800" fill="url(#np-room-fade)" />
          </mask>
          <filter id="np-soft" filterUnits="userSpaceOnUse" x="-2000" y="-2000" width="6000" height="6000"><feGaussianBlur stdDeviation="2.2" result="b" /><feMerge><feMergeNode in="b" /><feMergeNode in="SourceGraphic" /></feMerge></filter>
        </defs>

        <text className="np-stage__note" x={330} y={40}>표시용 배치 · 실제 거리와 다름</text>

        {/* 방: 바닥판, 격자, 뒤쪽 낮은 벽, 바닥 배선, 기기 앞 원, 비콘 신호 */}
        {room(<>
          <path className="np-iso__face" d={SLAB.side} /><path className="np-iso__face" d={SLAB.front} />
          <path className="np-iso__face np-iso__floor" d={SLAB.top} />
          <path className="np-iso__detail" d={FLOOR_GRID} />
          <path className="np-iso__face np-iso__wall" d={WALL_LEFT} /><path className="np-iso__face np-iso__wall" d={WALL_BACK} />
          <path className="np-stage__bus" d={bus} data-on={b(v.scene !== 'layout')} />
          {devices.map((d) => <path key={d.nodeId} className="np-stage__front" d={ROOM_ISO.ring(d.fx, d.fy, 44)} />)}
          {v.signals.map((s) => {
            const p = iso(s);
            return (
              <g key={s.personId} className="np-stage__signals" data-on={b(s.on)}>
                {devices.map((d) => <path key={d.nodeId} className="np-stage__signal" d={`M${pt(p)}L${pt(iso({ x: d.fx, y: d.fy }))}`} />)}
              </g>
            );
          })}
        </>, true)}

        <g className="np-stage__nodes" data-on={b(v.scene !== 'layout')}>
          <path className="np-stage__wire" d={pathOf(HOST_TO_SERVER)} />
          <path className="np-stage__wire" data-dashed="true" d={pathOf(SERVER_TO_OWNER)} />
          <HostNode key={`host|${v.host}`} active={v.host} />
          <ServerNode key={`server|${v.server}|${JSON.stringify(v.judge)}`} on={v.server} j={v.judge} />
          <OwnerNode key={`owner|${v.owner}`} active={v.owner} />
        </g>
        <path className="np-stage__bus" d={pathOf(fromServer([{ x: LEFT, y: FRONT_EDGE }, { x: LEFT, y: BACK }], shift))} data-on={b(v.scene !== 'layout')} />

        <Flow on={v.mcp} tone="accent" points={HOST_TO_SERVER} />
        <Flow on={v.reply} tone="ok" points={[...HOST_TO_SERVER].reverse()} />
        <Flow on={v.uplink} tone="accent" points={uplink} />
        <Flow key={`cmd-${v.cmd}`} on={!!v.cmd} tone="ok" points={cmd} />
        <Flow on={v.push} tone="appr" points={SERVER_TO_OWNER} />
        <Flow key={`fault-${v.fault}`} on={!!v.fault} tone="deny" points={fault} />
        <g data-on={b(v.scene !== 'layout')} className="np-stage__nodes"><ServerText key={`server|${v.server}|${JSON.stringify(v.judge)}`} j={v.judge} /></g>

        {room(<>
        {/* 상태가 바뀐 기기는 다시 그려져 도착 강조가 한 번 나온다 */}
        {depth.map((x) => x.el)}

        <g className="np-stage__rssi" data-on={b(v.rssiOn)}>
          {devices.filter((d) => !d.hidden).map((d) => <text key={d.nodeId} className="np-stage__signal-label" x={d.rssiAt.x} y={d.rssiAt.y + 17} data-anchor="middle">{v.rssi[d.nodeId] ?? ''}</text>)}
        </g>

        <g key={`${c.title}|${c.body}`} className="np-stage__callout np-flash" data-component="StageCallout" data-on={b(c.on)} data-tone={c.tone}>
          <path className="np-stage__callout-leader" d={`M${c.ax} ${c.ay}L${c.lx} ${c.ly}`} />
          <circle className="np-stage__callout-dot" cx={c.ax} cy={c.ay} r="4" />
          <g transform={`translate(${c.x} ${c.y})`}>
            <rect className="np-stage__callout-box" width="390" height={c.body ? 76 : 50} rx="8" />
            <text className="np-stage__callout-title" x="18" y="32">{c.title}</text>
            <text className="np-stage__callout-body" x="18" y="58">{c.body}</text>
          </g>
        </g>
        </>)}

        <text className="np-stage__code" x={1380} y={40} data-anchor="end">{v.placeName} · 근접 모델 {v.modelVersion}</text>
      </svg>
      {zoom ? (
        <div className="np-stage__zoom" data-component="StageZoom" role="group" aria-label="무대 확대">
          <button className="np-btn" type="button" aria-label="확대" title="확대" disabled={zoom.view.k >= ZOOM_MAX} onClick={() => zoom.onChange(zoomAt(zoom.view, { x: zoom.view.cx, y: zoom.view.cy }, ZOOM_STEP))}><Icon name="plus" /></button>
          <button className="np-btn" type="button" aria-label="축소" title="축소" disabled={isFit(zoom.view)} onClick={() => zoom.onChange(zoomAt(zoom.view, { x: zoom.view.cx, y: zoom.view.cy }, 1 / ZOOM_STEP))}><Icon name="minus" /></button>
          <button className="np-btn" type="button" aria-label="무대 전체 보기" title="무대 전체 보기" disabled={isFit(zoom.view)} onClick={() => zoom.onChange(FIT)}><Icon name="fit" /></button>
        </div>
      ) : null}
    </section>
  );
}

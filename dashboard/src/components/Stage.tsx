// 무대: 표시용 배치 위의 기기, 사람, 무선 흐름(D5). 배치는 이 PC에서 정한다(D4). 실제 위치·거리가 아니다.
import { useEffect, useRef, useState, type KeyboardEvent, type PointerEvent } from 'react';
import type { HostKind, Spot } from '../model/types';
import { GRID, facing, frontOf, snap } from '../view/arrange';
import type { StageDeviceView, StageView, StageVisitorView } from '../view/stage';
import { ROOM } from '../view/stage';

/** SET-01 배치 편집. 모니터 무대에는 넘기지 않는다. */
export interface StageEdit {
  canPlace: (nodeId: string, s: Spot) => boolean;
  onMove: (nodeId: string, s: Spot) => void;
}

const b = (v: unknown) => (v ? 'true' : 'false');

const GLYPH = {
  locker: <path className="np-stage__device-glyph" d="M-12 -12h24v24h-24zM6 -2v4" />,
  speaker: <path className="np-stage__device-glyph" d="M-12 -5h6l8 -7v24l-8 -7h-6zM6 -5a7 7 0 0 1 0 10" />,
  light: <path className="np-stage__device-glyph" d="M0 -13a8 8 0 0 1 5 14v4h-10v-4a8 8 0 0 1 5 -14zM-4 9h8" />
};

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
    <circle className="np-stage__packet" data-tone={tone} r="9" opacity="0">
      <animateMotion ref={move} dur={`${ARRIVE_SEC}s`} begin="indefinite" fill="freeze" path={d} />
      <animate ref={fade} attributeName="opacity" values="1;1;0" keyTimes="0;0.8;1" dur={`${ARRIVE_SEC + 0.15}s`} begin="indefinite" fill="freeze" />
    </circle>
  );
}

/** 선과 화살표. 켜지는 순간 점이 이동한다 */
function Flow({ on, tone, d, arrow }: { on: boolean; tone: string; d: string; arrow: string }) {
  return (
    <g data-on={b(on)}>
      <path className="np-stage__flow" data-tone={tone} d={d} />
      <polygon className="np-stage__arrow" data-tone={tone} points={arrow} />
      {on && d ? <Packet d={d} tone={tone} /> : null}
    </g>
  );
}

function Device({ d, edit }: { d: StageDeviceView; edit?: DeviceEditProps }) {
  const ly = d.wall === 'top' ? [50, 72, 92] : [-78, -56, -36];
  const editAttrs = edit ? {
    tabIndex: 0, role: 'button', 'aria-label': `${d.name} 옮기기. 끌거나 화살표 키로 한 칸씩 옮겨요`,
    'data-edit': 'true', 'data-invalid': b(edit.invalid), onPointerDown: edit.onPointerDown, onKeyDown: edit.onKeyDown
  } : {};
  return (
    <g className="np-stage__device np-flash" data-component="StageDevice" data-tone={d.tone} data-dim={b(d.dim)} transform={`translate(${d.x} ${d.y})`} {...editAttrs}>
      {edit ? <rect className="np-stage__hit" x="-66" y="-48" width="132" height="96" /> : null}
      <rect className="np-stage__device-body" x="-36" y="-21" width="72" height="42" />
      {GLYPH[d.capability]}
      <text className="np-stage__label" x="0" y={ly[0]} data-anchor="middle">{d.name}</text>
      <text className="np-stage__device-status" x="0" y={ly[1]} data-anchor="middle">{d.status}</text>
      <text className="np-stage__code" x="0" y={ly[2]} data-anchor="middle">{d.nodeId}</text>
    </g>
  );
}

/** 사람 점이 다른 기기 앞으로 옮길 때 미끄러지는 시간 (초). 표시용 값이다. */
const SLIDE_SEC = 0.6;

function Visitor({ v }: { v: StageVisitorView }) {
  // 자리가 바뀌면 지난 자리에서 새 자리로 미끄러진다 (순간 이동하면 눈이 따라가지 못한다)
  const at = `${v.x} ${v.y}`;
  const shown = useRef(at);
  const slide = useRef<{ from: string; to: string } | null>(null);
  if (shown.current !== at) { slide.current = { from: shown.current, to: at }; shown.current = at; }
  const anim = useRef<SVGAnimateTransformElement>(null);
  useEffect(() => {
    if (window.matchMedia?.('(prefers-reduced-motion: reduce)').matches) return;
    anim.current?.beginElement?.();
  }, [at]);
  const lx = v.label === 'left' ? -56 : v.label === 'above' ? 0 : 56;
  const ly = v.label === 'above' ? -54 : 7;
  const anchor = v.label === 'left' ? 'end' : v.label === 'above' ? 'middle' : 'start';
  return (
    <g className="np-stage__visitor" data-component="StageVisitor" data-active={b(v.active)} data-dim={b(v.dim)} data-on={b(v.on)} transform={`translate(${v.x} ${v.y})`}>
      {slide.current ? <animateTransform key={slide.current.to} ref={anim} attributeName="transform" type="translate" from={slide.current.from} to={slide.current.to} dur={`${SLIDE_SEC}s`} begin="indefinite" fill="freeze" calcMode="spline" keyTimes="0;1" keySplines="0.4 0 0.2 1" /> : null}
      <circle className="np-stage__ring" r="24" /><circle className="np-stage__ring" r="35" /><circle className="np-stage__ring" r="46" />
      <circle className="np-stage__visitor-dot" r="14" />
      <text className="np-stage__visitor-letter" y="6" data-anchor="middle">{v.letter}</text>
      <text className="np-stage__label" x={lx} y={ly} data-anchor={anchor}>{v.name}</text>
    </g>
  );
}

/** flash: 메시지를 받는 쪽(서버, 소유자 폰)이라 켜질 때 도착 강조를 한다. 보내는 쪽(AI 호스트)은 바로 켜진다. */
function Node({ x, y, w, h, label, sub, active, dim, flash }: { x: number; y: number; w: number; h: number; label: string; sub?: string; active: boolean; dim: boolean; flash?: boolean }) {
  return (
    <g className={flash ? 'np-stage__node np-flash' : 'np-stage__node'} data-active={b(active)} data-dim={b(dim)}>
      <rect className="np-stage__node-box" x={x} y={y} width={w} height={h} />
      <text className="np-stage__label" x={x + 18} y={y + 30}>{label}</text>
      {sub ? <text className="np-stage__sub" x={x + 18} y={y + 52}>{sub}</text> : null}
    </g>
  );
}

const CHECK_MARK = { pass: '✓', fail: '✕', wait: '…', unsure: '?', none: '·', skip: '–' };

/** 서버 상자: 판정 표시와 확인 네 칸. 판정 전에는 윗변의 판정 게이지가 찬다. 판정이 나면 다시 그려져 도착 강조가 한 번 나온다. */
function ServerNode({ on, j }: { on: boolean; j: StageView['judge'] }) {
  return (
    <g className="np-stage__node np-stage__server np-flash" data-component="StageServer" data-active={b(on)} data-dim="false" data-tone={j.tone}>
      <rect className="np-stage__node-box" x={30} y={370} width={220} height={140} />
      {j.gauge ? <><rect className="np-stage__gauge" x={31} y={371} width={218} height={6} /><rect className="np-stage__gauge-fill" x={31} y={371} width={218} height={6} /></> : null}
      <text className="np-stage__label" x={48} y={402}>서버</text>
      <text className="np-stage__judge-mark" x={232} y={402} data-anchor="end">{j.mark}</text>
      {j.checks.map(([res, text], k) => (
        <text key={k} className="np-stage__judge-row" data-result={res} x={48} y={430 + k * 22}><tspan className="np-stage__judge-icon">{CHECK_MARK[res]}</tspan> {text}</text>
      ))}
    </g>
  );
}

/** 서버에서 기기까지 배선을 따라가는 흐름 선과 화살표 */
function routeTo(d: StageDeviceView | undefined): { path: string; arrow: string } {
  if (!d) return { path: '', arrow: '' };
  if (d.wall === 'top') {
    const top = d.y - 21;
    return { path: `M250 470H284V60H${d.x}V${top - 13}`, arrow: `${d.x - 8},${top - 15} ${d.x},${top - 1} ${d.x + 8},${top - 15}` };
  }
  const bottom = d.y + 21;
  return { path: `M250 470H284V775H${d.x}V${bottom + 12}`, arrow: `${d.x - 8},${bottom + 14} ${d.x},${bottom} ${d.x + 8},${bottom + 14}` };
}

const HOSTS: [HostKind, string, number][] = [['claude', 'Claude', 165], ['chatgpt', 'ChatGPT', 223], ['gemini', 'Gemini', 281]];

const ARROWS: Record<string, [number, number]> = { ArrowLeft: [-GRID, 0], ArrowRight: [GRID, 0], ArrowUp: [0, -GRID], ArrowDown: [0, GRID] };

export function Stage({ v, edit }: { v: StageView; edit?: StageEdit }) {
  const svg = useRef<SVGSVGElement>(null);
  const [drag, setDrag] = useState<{ id: string; dx: number; dy: number; s: Spot; ok: boolean } | null>(null);
  const toSvgPoint = (e: PointerEvent): Spot => {
    const m = svg.current?.getScreenCTM();
    if (!m) return { x: 0, y: 0 };
    const p = new DOMPoint(e.clientX, e.clientY).matrixTransform(m.inverse());
    return { x: p.x, y: p.y };
  };
  const moved = (d: StageDeviceView, s: Spot): StageDeviceView => {
    const f = frontOf(s);
    return { ...d, x: s.x, y: s.y, fx: f.x, fy: f.y, wall: facing(s) };
  };
  const devices = v.devices.map((d) => (drag && drag.id === d.nodeId ? moved(d, drag.s) : d));
  const editFor = (d: StageDeviceView): DeviceEditProps | undefined => edit && {
    invalid: !!drag && drag.id === d.nodeId && !drag.ok,
    onPointerDown: (e) => {
      e.currentTarget.setPointerCapture(e.pointerId);
      const p = toSvgPoint(e);
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
  const onPointerMove = (e: PointerEvent<SVGSVGElement>) => {
    if (!drag || !edit) return;
    const p = toSvgPoint(e);
    const s = { x: snap(p.x - drag.dx), y: snap(p.y - drag.dy) };
    if (s.x !== drag.s.x || s.y !== drag.s.y) setDrag({ ...drag, s, ok: edit.canPlace(drag.id, s) });
  };
  const onPointerUp = () => {
    if (drag && edit && drag.ok) edit.onMove(drag.id, drag.s);
    setDrag(null);
  };

  const tops = devices.filter((d) => d.wall === 'top');
  const bottoms = devices.filter((d) => d.wall === 'bottom');
  const topEnd = Math.max(...tops.map((d) => d.x), 284);
  const botEnd = Math.max(...bottoms.map((d) => d.x), 284);
  const bus = [
    `M250 470H284V60H${topEnd}`, ...tops.map((d) => `M${d.x} 60V${d.y - 21}`),
    `M284 470V775H${botEnd}`, ...bottoms.map((d) => `M${d.x} 775V${d.y + 21}`)
  ].join('');
  const cmd = routeTo(devices.find((d) => d.nodeId === v.cmd));
  const fault = routeTo(devices.find((d) => d.nodeId === v.fault));
  const c = v.callout;

  return (
    <section className="np-stage" data-component="Stage" data-scene={v.scene} aria-label={`${v.placeName} 평면도`}>
      <svg ref={svg} className="np-stage__svg" data-editing={b(!!drag)} onPointerMove={onPointerMove} onPointerUp={onPointerUp} onPointerCancel={() => setDrag(null)} viewBox={v.scene === 'layout' ? '276 0 1100 800' : '0 0 1400 800'} preserveAspectRatio="xMidYMid meet" role="img" aria-label={v.aria}>
        <defs>
          <pattern id="np-grid-minor" width="24" height="24" patternUnits="userSpaceOnUse"><path className="np-stage__grid-minor" d="M24 0H0V24" /></pattern>
          <pattern id="np-grid-major" width="120" height="120" patternUnits="userSpaceOnUse"><rect className="np-stage__grid-minor-fill" width="120" height="120" /><path className="np-stage__grid-major" d="M120 0H0V120" /></pattern>
        </defs>
        <rect className="np-stage__grid-fill" width="1400" height="800" />

        <text className="np-stage__note" x={ROOM.x} y="40">표시용 배치 · 실제 거리와 다름</text>

        <g className="np-stage__nodes" data-on={b(v.scene !== 'layout')}>
          <text className="np-stage__cap" x="30" y="150">AI 호스트</text>
          {HOSTS.map(([k, name, y]) => <Node key={`${k}|${v.host === k}`} x={30} y={y} w={220} h={46} label={name} active={v.host === k} dim={!!v.host && v.host !== k} />)}
          <path className="np-stage__link" d="M250 188H272M250 246H272M250 304H272M272 188V420H250" />
          <ServerNode key={`server|${v.server}|${JSON.stringify(v.judge)}`} on={v.server} j={v.judge} />
          <path className="np-stage__node-corner" d="M30 386V370H46M234 370H250V386M250 494V510H234M46 510H30V494" />
          <path className="np-stage__link" data-dashed="true" d="M140 510V600" />
          <Node key={`owner|${v.owner}`} x={30} y={600} w={220} h={64} flash label="소유자 폰" sub="승인 알림" active={v.owner} dim={false} />
          <path className="np-stage__bus" d={bus} />
        </g>

        <rect className="np-stage__wall" x={ROOM.x} y={ROOM.y} width={ROOM.w} height={ROOM.h} />

        {devices.map((d) => <circle key={d.nodeId} className="np-stage__front" cx={d.fx} cy={d.fy} r="44" />)}

        {v.signals.map((s) => (
          <g key={s.personId} className="np-stage__signals" data-on={b(s.on)}>
            {devices.map((d) => <path key={d.nodeId} className="np-stage__signal" d={`M${s.x} ${s.y}L${d.fx} ${d.fy}`} />)}
          </g>
        ))}

        {/* 상태가 바뀐 기기는 다시 그려져 도착 강조가 한 번 나온다 */}
        {devices.map((d) => <Device key={`${d.nodeId}|${d.status}|${d.tone}`} d={d} edit={editFor(d)} />)}

        <g className="np-stage__rssi" data-on={b(v.rssiOn)}>
          {devices.map((d) => (
            <text key={d.nodeId} className="np-stage__signal-label" x={d.rssiRight ? d.x + 42 : d.wall === 'top' ? d.x - 42 : d.x - 10} y={d.wall === 'top' ? d.y + 6 : d.y + 45} data-anchor={d.rssiRight ? 'start' : 'end'}>{v.rssi[d.nodeId] ?? ''}</text>
          ))}
        </g>

        {HOSTS.map(([k, , y]) => <Flow key={k} on={v.mcp === k} tone="accent" d={`M250 ${y + 23}H272V420H262`} arrow="264,412 250,420 264,428" />)}
        {HOSTS.map(([k, , y]) => <Flow key={`reply-${k}`} on={v.reply === k} tone="ok" d={`M250 420H272V${y + 23}H262`} arrow={`264,${y + 15} 250,${y + 23} 264,${y + 31}`} />)}
        <Flow on={v.uplink} tone="accent" d={`M${topEnd} 60H284V470H262`} arrow="264,462 250,470 264,478" />
        <Flow key={`cmd-${v.cmd}`} on={!!v.cmd} tone="ok" d={cmd.path} arrow={cmd.arrow} />
        <Flow on={v.push} tone="appr" d="M140 510V586" arrow="132,584 140,598 148,584" />
        <Flow key={`fault-${v.fault}`} on={!!v.fault} tone="deny" d={fault.path} arrow={fault.arrow} />

        {v.visitors.map((x) => <Visitor key={x.personId} v={x} />)}

        <g key={`${c.title}|${c.body}`} className="np-stage__callout np-flash" data-component="StageCallout" data-on={b(c.on)} data-tone={c.tone}>
          <path className="np-stage__callout-leader" d={`M${c.ax} ${c.ay}L${c.lx} ${c.ly}`} />
          <g transform={`translate(${c.x} ${c.y})`}>
            <rect className="np-stage__callout-box" width="390" height={c.body ? 76 : 50} />
            <text className="np-stage__callout-title" x="18" y="32">{c.title}</text>
            <text className="np-stage__callout-body" x="18" y="58">{c.body}</text>
          </g>
        </g>

        <rect className="np-stage__titleblock" x="1060" y="12" width="280" height="40" />
        <text className="np-stage__code" x="1074" y="37">{v.placeName} · 근접 모델 {v.modelVersion}</text>
      </svg>
    </section>
  );
}

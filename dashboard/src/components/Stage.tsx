// 무대: 평면도 위의 기기, 사람, 무선 흐름(D5). 좌표는 근접 모델 파일의 배치에서 온다(D4).
import type { HostKind } from '../model/types';
import type { StageDeviceView, StageView, StageVisitorView } from '../view/stage';
import { ROOM } from '../view/stage';

const b = (v: unknown) => (v ? 'true' : 'false');

const GLYPH = {
  locker: <path className="np-stage__device-glyph" d="M-12 -12h24v24h-24zM6 -2v4" />,
  speaker: <path className="np-stage__device-glyph" d="M-12 -5h6l8 -7v24l-8 -7h-6zM6 -5a7 7 0 0 1 0 10" />,
  light: <path className="np-stage__device-glyph" d="M0 -13a8 8 0 0 1 5 14v4h-10v-4a8 8 0 0 1 5 -14zM-4 9h8" />
};

function Device({ d }: { d: StageDeviceView }) {
  const ly = d.wall === 'top' ? [50, 72, 92] : [-78, -56, -36];
  return (
    <g className="np-stage__device" data-component="StageDevice" data-tone={d.tone} data-dim={b(d.dim)} transform={`translate(${d.x} ${d.y})`}>
      <rect className="np-stage__device-body" x="-36" y="-21" width="72" height="42" />
      {GLYPH[d.capability]}
      <text className="np-stage__label" x="0" y={ly[0]} data-anchor="middle">{d.name}</text>
      <text className="np-stage__device-status" x="0" y={ly[1]} data-anchor="middle">{d.status}</text>
      <text className="np-stage__code" x="0" y={ly[2]} data-anchor="middle">{d.nodeId}</text>
    </g>
  );
}

function Visitor({ v }: { v: StageVisitorView }) {
  const lx = v.label === 'left' ? -52 : v.label === 'above' ? 0 : 40;
  const ly = v.label === 'above' ? -54 : v.label === 'left' ? 7 : -30;
  const anchor = v.label === 'left' ? 'end' : v.label === 'above' ? 'middle' : 'start';
  return (
    <g className="np-stage__visitor" data-component="StageVisitor" data-active={b(v.active)} data-dim={b(v.dim)} data-on={b(v.on)} transform={`translate(${v.x} ${v.y})`}>
      <circle className="np-stage__ring" r="24" /><circle className="np-stage__ring" r="35" /><circle className="np-stage__ring" r="46" />
      <circle className="np-stage__visitor-dot" r="14" />
      <text className="np-stage__visitor-letter" y="6" data-anchor="middle">{v.letter}</text>
      <text className="np-stage__label" x={lx} y={ly} data-anchor={anchor}>방문자 {v.letter}</text>
    </g>
  );
}

function Node({ x, y, w, h, label, sub, active, dim }: { x: number; y: number; w: number; h: number; label: string; sub?: string; active: boolean; dim: boolean }) {
  return (
    <g className="np-stage__node" data-active={b(active)} data-dim={b(dim)}>
      <rect className="np-stage__node-box" x={x} y={y} width={w} height={h} />
      <text className="np-stage__label" x={x + 18} y={y + 30}>{label}</text>
      {sub ? <text className="np-stage__sub" x={x + 18} y={y + 52}>{sub}</text> : null}
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

export function Stage({ v }: { v: StageView }) {
  const tops = v.devices.filter((d) => d.wall === 'top');
  const bottoms = v.devices.filter((d) => d.wall === 'bottom');
  const topEnd = Math.max(...tops.map((d) => d.x), 284);
  const botEnd = Math.max(...bottoms.map((d) => d.x), 284);
  const bus = [
    `M250 470H284V60H${topEnd}`, ...tops.map((d) => `M${d.x} 60V${d.y - 21}`),
    `M284 470V775H${botEnd}`, ...bottoms.map((d) => `M${d.x} 775V${d.y + 21}`)
  ].join('');
  const cmd = routeTo(v.devices.find((d) => d.nodeId === v.cmd));
  const fault = routeTo(v.devices.find((d) => d.nodeId === v.fault));
  const c = v.callout;
  const fmt = (n: number) => String(n).replace(/\.0+$/, '');

  return (
    <section className="np-stage" data-component="Stage" data-scene={v.scene} aria-label={`${v.placeName} 평면도`}>
      <svg className="np-stage__svg" viewBox={v.scene === 'layout' ? '276 0 1100 800' : '0 0 1400 800'} preserveAspectRatio="xMidYMid meet" role="img" aria-label={v.aria}>
        <defs>
          <pattern id="np-grid-minor" width="24" height="24" patternUnits="userSpaceOnUse"><path className="np-stage__grid-minor" d="M24 0H0V24" /></pattern>
          <pattern id="np-grid-major" width="120" height="120" patternUnits="userSpaceOnUse"><rect className="np-stage__grid-minor-fill" width="120" height="120" /><path className="np-stage__grid-major" d="M120 0H0V120" /></pattern>
        </defs>
        <rect className="np-stage__grid-fill" width="1400" height="800" />

        <path className="np-stage__dim" d={`M${ROOM.x} 14V30M${ROOM.x + ROOM.w} 14V30M${ROOM.x} 22H${ROOM.x + ROOM.w}`} />
        <text className="np-stage__cap" x={ROOM.x + 14} y="16" data-anchor="start">{fmt(v.widthM)} M</text>
        <path className="np-stage__dim" d={`M304 ${ROOM.y}H320M304 ${ROOM.y + ROOM.h}H320M312 ${ROOM.y}V${ROOM.y + ROOM.h}`} />
        <text className="np-stage__cap" transform="translate(304 415) rotate(-90)" data-anchor="middle">{fmt(v.depthM)} M</text>

        <g className="np-stage__nodes" data-on={b(v.scene !== 'layout')}>
          <text className="np-stage__cap" x="30" y="150">AI 호스트</text>
          {HOSTS.map(([k, name, y]) => <Node key={k} x={30} y={y} w={220} h={46} label={name} active={v.host === k} dim={!!v.host && v.host !== k} />)}
          <path className="np-stage__link" d="M250 188H272M250 246H272M250 304H272M272 188V420H250" />
          <Node x={30} y={370} w={220} h={140} label="NearPilot 서버" sub="누구·어디·정책·빈자리" active={v.server} dim={false} />
          <path className="np-stage__node-corner" d="M30 386V370H46M234 370H250V386M250 494V510H234M46 510H30V494" />
          <text className="np-stage__code" x="48" y="482">MCP · MQTT</text>
          <path className="np-stage__link" data-dashed="true" d="M140 510V600" />
          <Node x={30} y={600} w={220} h={64} label="소유자 폰" sub="승인 알림을 받아요" active={v.owner} dim={false} />
          <path className="np-stage__bus" d={bus} />
        </g>

        <rect className="np-stage__wall" x={ROOM.x} y={ROOM.y} width={ROOM.w} height={ROOM.h} />
        <line className="np-stage__door" x1="1340" y1="400" x2="1340" y2="480" />
        <path className="np-stage__door-arc" d="M1340 400A80 80 0 0 0 1260 480" />
        <text className="np-stage__cap" x="1326" y="510" data-anchor="end">입구</text>

        {v.devices.map((d) => <circle key={d.nodeId} className="np-stage__front" cx={d.fx} cy={d.fy} r="44" />)}

        {v.signals.map((s) => (
          <g key={s.personId} className="np-stage__signals" data-on={b(s.on)}>
            {v.devices.map((d) => <path key={d.nodeId} className="np-stage__signal" d={`M${s.x} ${s.y}L${d.fx} ${d.fy}`} />)}
          </g>
        ))}

        {v.devices.map((d) => <Device key={d.nodeId} d={d} />)}

        <g className="np-stage__rssi" data-on={b(v.rssiOn)}>
          {v.devices.map((d) => (
            <text key={d.nodeId} className="np-stage__signal-label" x={d.wall === 'top' ? d.x - 42 : d.x - 10} y={d.wall === 'top' ? d.y + 6 : d.y + 45} data-anchor="end">{v.rssi[d.nodeId] ?? ''}</text>
          ))}
        </g>
        <g className="np-stage__coords" data-on={b(v.coords)}>
          {v.devices.map((d) => {
            const y0 = d.wall === 'top' ? d.fy + 66 : d.fy - 72;
            return [
              <text key={`${d.nodeId}-x`} className="np-stage__code" x={d.fx} y={y0} data-anchor="middle">x {((d.fx - ROOM.x) / (ROOM.w / v.widthM)).toFixed(2)}</text>,
              <text key={`${d.nodeId}-y`} className="np-stage__code" x={d.fx} y={y0 + 20} data-anchor="middle">y {((d.fy - ROOM.y) / (ROOM.h / v.depthM)).toFixed(2)}</text>
            ];
          })}
        </g>

        {HOSTS.map(([k, , y]) => (
          <g key={k} data-on={b(v.mcp === k)}>
            <path className="np-stage__flow" data-tone="accent" d={`M250 ${y + 23}H272V420H262`} />
            <polygon className="np-stage__arrow" data-tone="accent" points="264,412 250,420 264,428" />
          </g>
        ))}
        <g data-on={b(v.uplink)}><path className="np-stage__flow" data-tone="accent" d={`M${topEnd} 60H284V470H262`} /><polygon className="np-stage__arrow" data-tone="accent" points="264,462 250,470 264,478" /></g>
        <g data-on={b(!!v.cmd)}><path className="np-stage__flow" data-tone="ok" d={cmd.path} /><polygon className="np-stage__arrow" data-tone="ok" points={cmd.arrow} /></g>
        <g data-on={b(v.push)}><path className="np-stage__flow" data-tone="appr" d="M140 510V586" /><polygon className="np-stage__arrow" data-tone="appr" points="132,584 140,598 148,584" /></g>
        <g data-on={b(!!v.fault)}><path className="np-stage__flow" data-tone="deny" d={fault.path} /><polygon className="np-stage__arrow" data-tone="deny" points={fault.arrow} /></g>

        {v.visitors.map((x) => <Visitor key={x.personId} v={x} />)}

        <g className="np-stage__callout" data-component="StageCallout" data-on={b(c.on)} data-tone={c.tone}>
          <path className="np-stage__callout-leader" d={`M${c.ax} ${c.ay}L${c.x} ${c.y + 38}`} />
          <g transform={`translate(${c.x} ${c.y})`}>
            <rect className="np-stage__callout-box" width="390" height="76" />
            <text className="np-stage__callout-title" x="18" y="32">{c.title}</text>
            <text className="np-stage__callout-body" x="18" y="58">{c.body}</text>
          </g>
        </g>

        <rect className="np-stage__titleblock" x="1060" y="752" width="280" height="40" />
        <text className="np-stage__code" x="1074" y="777">{v.placeName} · 근접 모델 {v.modelVersion}</text>
        <path className="np-stage__dim" d="M900 772H1020M900 766V778M1020 766V778" />
        <text className="np-stage__code" x="960" y="792" data-anchor="middle">1 M</text>
      </svg>
    </section>
  );
}

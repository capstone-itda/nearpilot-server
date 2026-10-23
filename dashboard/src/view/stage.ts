// 모니터 상태 → 무대 표시값 (평면 배치를 아이소메트릭으로 투영해 그린다). 장면별 고정 값 없이 규칙으로 계산한다.
import type { DeviceInfo, DeviceStatus, FloorLayout, LayoutDevice, Person, PlaceInfo, RequestProgress, MonitorState, Spot } from '../model/types';
import { ROOM, facing, frontOf, type Facing } from './arrange';
import {
  CALLOUT_W, HEAD_R, PERSON_Z, calloutSpot, chooseLabel, deviceBody, deviceLabelBox, freePersonSpot, frontBox, hasLabelRoom, labelBox, leaderEnd, personBox,
  PANEL_EDGE, STAGE_BOUNDS, placeDeviceLabels, placeRssi, ringBox, rssiBox, shiftBounds, textWidth, type LabelSide, type LabelSpot, type Rect
} from './placement';
import { iso } from './iso';
import { placeOfPerson, requestPlace } from './places';
import {
  ACTION_DID, ACTION_DOING, ACTION_DONE, ACTION_NAME, CAPABILITY_NAME,
  DEVICE_STATE_NAME, approvalText, josa, reasonText
} from './text';
import { isDecided } from '../model/types';
import { CHECK_NAMES, serverChecks, type CheckResult } from './rail';
import { VERDICT_LABEL } from './text';

/** 응답 선을 보이는 시간 (초). 표시용 값이라 설정이 아니다. */
const REPLY_SHOW_SEC = 3;
const secOf = (t: string) => { const [h, m, x = '0'] = t.split(':'); return Number(h) * 3600 + Number(m) * 60 + Number(x); };

export { ROOM };

export type Tone = 'none' | 'active' | 'ok' | 'ask' | 'appr' | 'busy' | 'deny' | 'accent';
export type StageScene = 'idle' | 'judging' | 'flow' | 'fault' | 'ask' | 'approval' | 'busy' | 'deny' | 'layout';

export interface StageDeviceView {
  nodeId: string; name: string; capability: LayoutDevice['capability'];
  /** 평면 배치 자리와 기기 앞 */
  x: number; y: number; fx: number; fy: number; wall: Facing;
  /** 무대에서 본체가 차지하는 상자, 이름표 자리, 신호 세기 글자 자리 */
  body: Rect; label: LabelSpot; rssiAt: Spot;
  /** 방을 밀어 그려서 왼쪽 칸 뒤로 들어가 보이지 않는다 */
  hidden: boolean;
  status: string; code: string; tone: Tone; dim: boolean;
}
export interface StageVisitorView {
  /** x, y는 평면 자리. 무대에는 투영해서 그린다. */
  personId: string; letter: string; name: string; x: number; y: number;
  active: boolean; dim: boolean; on: boolean; label: LabelSide; labelW: number;
}
/** 콜아웃은 무대 좌표: 상자 왼쪽 위(x, y), 가리키는 점(ax, ay), 지시선 끝(lx, ly) */
export interface CalloutView { on: boolean; tone: Tone; x: number; y: number; ax: number; ay: number; lx: number; ly: number; title: string; body: string }

/** 왼쪽 칸의 호스트·서버·소유자 폰이 무대에 놓이는 자리 (상자 오른쪽 가운데, 콜아웃이 가리키는 점) */
export const NODE_ANCHOR = { host: { x: 300, y: 196 }, server: { x: 300, y: 430 }, owner: { x: 300, y: 664 } };

export interface StageView {
  scene: StageScene;
  /** 크게 그리는 장소. 주 장소가 바뀌었는지 가린다. */
  placeId: string;
  /** 방을 왼쪽으로 미는 거리 (무대 단위). 패널이 대상 기기를 가리지 않게 한다. 방 안의 좌표는 밀기 전 값이다. */
  shift: number;
  aria: string;
  placeName: string;
  modelVersion: string;
  /** 요청을 보낸 AI 호스트를 밝힌다. 호스트 종류는 구분하지 않는다 (2026-10-21 사용자 결정) */
  host: boolean;
  server: boolean;
  /** 서버 랙: 판정 표시, 판정 게이지, 확인 네 칸 */
  judge: { mark: string; tone: Tone; gauge: boolean; checks: [CheckResult, string][] };
  owner: boolean;
  devices: StageDeviceView[];
  visitors: StageVisitorView[];
  /** 사람별 비콘 신호 선 */
  signals: { personId: string; x: number; y: number; on: boolean }[];
  rssiOn: boolean;
  rssi: Record<string, string>;
  /** AI 호스트 → 서버: 도구 호출이 도착했고 아직 응답하지 않음 */
  mcp: boolean;
  /** 서버 → AI 호스트: 방금 도구 호출의 결과를 돌려줌 */
  reply: boolean;
  uplink: boolean;
  /** 실행 명령 선의 대상 기기 */
  cmd: string | null;
  push: boolean;
  /** 격리 선의 대상 기기 */
  fault: string | null;
  callout: CalloutView;
}

const VERDICT_TONE = { OK: 'ok', ASK: 'ask', NEED_APPROVAL: 'appr', BUSY: 'busy', DENY: 'deny' } as const;

const dbm = (v: number) => `${String(v).replace('-', '−')} dBm`;

/** 기기 상태 → 표시 글자와 색. 작은 방(inset.ts)도 같은 규칙으로 칠한다. */
export function baseDevice(st: DeviceStatus | undefined): { status: string; code: string; tone: Tone } {
  if (!st) return { status: '알 수 없음', code: '—', tone: 'none' };
  if (st.state === 'FAULT') return { status: DEVICE_STATE_NAME.FAULT, code: 'FAULT', tone: 'deny' };
  if (st.state === 'AVAILABLE') return { status: DEVICE_STATE_NAME.AVAILABLE, code: 'AVAILABLE', tone: 'none' };
  if (st.state === 'RESERVED') return { status: DEVICE_STATE_NAME.RESERVED, code: 'RESERVED', tone: 'active' };
  const name = st.state === 'CLOSING' ? DEVICE_STATE_NAME.CLOSING : DEVICE_STATE_NAME.ACTIVE;
  return { status: st.overdue ? `${name} · 대여 초과` : name, code: st.state, tone: 'active' };
}

/** 요청과 관계있는 기기: 고른 기기, 없으면 요청한 기능의 기기 전부 */
export function focusDevices(r: RequestProgress, layout: FloorLayout): string[] {
  if (r.nodeId) return [r.nodeId];
  if (r.target.nodeId) return [r.target.nodeId];
  return layout.devices.filter((d) => d.capability === r.target.capability).map((d) => d.nodeId);
}

/**
 * 무대에서 사람을 세울 자리. 서버는 사람 좌표를 주지 않으므로 표시용으로만 고른다 (판정과 무관).
 * 요청한 사람: 고른 기기 앞, ASK면 확률이 높은 두 기기 사이, 아니면 확률이 가장 높은 기기 앞.
 * 그 밖의 사람: 신호가 가장 센 기기 앞. 신호가 없으면 그리지 않는다.
 * 무대에서 기기 본체·글자나 다른 사람과 겹치면 기기 앞 방향(방 안쪽)으로 비켜 선다.
 */
export function personAnchors(s: MonitorState, devices: StageDeviceView[], visitors: Person[], bounds = STAGE_BOUNDS): Map<string, Spot> {
  const byId = new Map(devices.map((d) => [d.nodeId, d]));
  const dirOf = (d: StageDeviceView): 1 | -1 => (d.wall === 'top' ? 1 : -1);
  const obstacles = devices.flatMap((d) => [d.body, deviceLabelBox(d.label)]);
  // 이름표가 피할 것: 기기 글자, 신호 세기 글자, 사람, 다른 이름표. 기기 본체는 덮어도 된다.
  const texts = devices.flatMap((d) => [deviceLabelBox(d.label), rssiBox(d.rssiAt)]);
  const bodies = devices.map((d) => d.body);
  const people: Rect[] = [];
  const labels: Rect[] = [];
  const r = s.request;
  const out = new Map<string, Spot>();
  // 요청한 사람을 먼저 세워서 그 자리를 비켜 주지 않게 한다
  const order = [...visitors].sort((a, b) => Number(b.id === r?.personId) - Number(a.id === r?.personId));
  for (const p of order) {
    // 다른 장소의 기기 앞에 있는 사람은 이 장소에 세우지 않는다 (작은 방에 그린다)
    if (!p.beaconSeen || (p.near && !byId.has(p.near))) continue;
    let base: Spot | undefined;
    let dir: 1 | -1 = 1;
    if (r && r.personId === p.id) {
      const ranked = Object.entries(r.posteriors ?? {}).filter(([id]) => byId.has(id)).sort((a, b) => b[1] - a[1]).map(([id]) => byId.get(id)!);
      const pick = byId.get(r.nodeId ?? r.target.nodeId ?? '');
      if (pick) { base = { x: pick.fx, y: pick.fy }; dir = dirOf(pick); }
      else if (r.verdict === 'ASK' && ranked.length >= 2) {
        const [a, b] = ranked;
        base = { x: Math.round((a.fx + b.fx) / 2), y: Math.round((a.fy + b.fy) / 2) };
        dir = dirOf(a);
      } else if (ranked[0]) { base = { x: ranked[0].fx, y: ranked[0].fy }; dir = dirOf(ranked[0]); }
    }
    if (!base) {
      // 신호가 흔들려도 바로 옮기지 않는 기기(near). 배치에 없으면 신호가 가장 센 기기.
      const d = byId.get(p.near ?? '') ?? byId.get(Object.entries(p.rssi).filter(([id]) => byId.has(id)).sort((a, b) => b[1] - a[1])[0]?.[0] ?? '');
      if (d) { base = { x: d.fx, y: d.fy }; dir = dirOf(d); }
    }
    if (!base) continue;
    // 이름표 둘 곳이 있는 자리를 고른다
    const w = textWidth(p.pseudonym, 20) + 8;
    const spot = freePersonSpot(base, dir, obstacles, (x) => hasLabelRoom(x, [...texts, ...people, ...labels], w, bounds));
    out.set(p.id, spot);
    obstacles.push(personBox(spot));
    people.push(personBox(spot));
    labels.push(labelBox(spot, chooseLabel(spot, [...texts, ...people, ...labels], w, bodies, bounds), w));
  }
  return out;
}

export interface StageOptions {
  /** 무대 위에 패널이 열려 있는가 (콜아웃을 패널 밖으로 둔다) */
  panelOpen: boolean;
  /** 넓은 패널(감사, 이력, 신뢰도)이 열려 있다. 콜아웃 둘 자리가 없어 숨긴다. */
  panelWide?: boolean;
  /** SET-01 배치 */
  layoutCheck?: boolean;
  /** 패널 왼쪽 끝의 무대 x 좌표. 패널과 폰 화면 폭을 사람이 바꾸므로 화면에서 잰다. 없으면 기본 크기의 값. */
  panelEdge?: number;
  /** 사람이 무대를 확대하거나 옮겼다. 방을 밀지 않는다. */
  viewFixed?: boolean;
  /** 모든 장소. 주 장소에 없는 기기(다른 장소의 기기)의 이름과 기능, 공용 판정의 같은 기능 기기를 여기서 찾는다. 없으면 주 장소만 안다. */
  places?: PlaceInfo[];
  /** 작은 방 상자 (무대 좌표, inset.ts). 콜아웃이 피하고, 주 장소에 없는 기기를 가리킬 때 이 상자를 가리킨다. */
  inset?: Rect;
}

export function stageView(s: MonitorState, layout: FloorLayout, opt: StageOptions): StageView {
  const r = s.request;
  const byId = new Map(s.devices.map((d) => [d.nodeId, d]));
  // 이름과 기능은 모든 장소에서 찾는다 (잠금 중에는 요청한 기기가 주 장소에 없을 수 있다)
  const places: PlaceInfo[] = opt.places ?? [layout];
  const known = (id?: string): DeviceInfo | undefined => places.flatMap((p) => p.devices).find((d) => d.nodeId === id);
  const devName = (id?: string) => known(id)?.name ?? id ?? '';
  const visitors = s.people.filter((p) => p.role === 'visitor' && p.letter);
  const requester = r ? s.people.find((p) => p.id === r.personId) : undefined;
  // 요청 장소가 이 장소가 아니면 이 장소의 기기는 요청과 관계가 없다. 같은 기능의 기기를 강조하지 않고, 다른 기기를 흐리지도 않는다.
  // 요청 장소를 정할 수 없으면(원격 요청) 이 장소의 요청으로 본다.
  const reqPlace = r ? requestPlace(s, places) : undefined;
  const elsewhere = reqPlace !== undefined && reqPlace !== layout.placeId;
  const keep = r && !elsewhere ? focusDevices(r, layout) : [];
  // 판정 전 비콘→서버 선은 늘 켠다. 요청한 사람이 다른 장소에 있으면 이 장소의 선이 아니라 끈다. 장소를 알 수 없는 요청자(원격)는 켠다.
  const requesterAt = requester && placeOfPerson(places, requester);
  const uplinkHere = requesterAt === undefined || requesterAt === layout.placeId;

  // 장면 이름
  let scene: StageScene = 'idle';
  if (opt.layoutCheck) scene = 'layout';
  else if (r) {
    if (!isDecided(r)) scene = 'judging';
    else if (r.verdict === 'OK') scene = r.execution === 'unknown' || r.execution === 'failed' ? 'fault' : 'flow';
    else scene = ({ ASK: 'ask', NEED_APPROVAL: 'approval', BUSY: 'busy', DENY: 'deny' } as const)[r.verdict];
  }

  // 기기
  const devices: StageDeviceView[] = layout.devices.map((d) => {
    const b = d.spot;
    const f = frontOf(d.spot);
    const v = { ...baseDevice(byId.get(d.nodeId)) };
    if (r && !opt.layoutCheck && keep.length === 1 && keep[0] === d.nodeId) {
      if (r.verdict === 'OK' && r.execution === 'sent') { v.status = ACTION_DOING[r.action]; v.tone = 'ok'; }
      else if (r.verdict === 'NEED_APPROVAL') v.tone = 'appr';
      else if (r.verdict === 'BUSY') v.tone = 'busy';
    }
    return {
      nodeId: d.nodeId, name: d.name, capability: d.capability, x: b.x, y: b.y, fx: f.x, fy: f.y, wall: facing(d.spot),
      body: deviceBody(b.x, b.y, d.capability), label: { x: 0, y: 0, w: 0 }, rssiAt: { x: 0, y: 0 }, hidden: false,
      ...v, dim: !!r && !opt.layoutCheck && !elsewhere && !keep.includes(d.nodeId)
    };
  });
  const geoms = devices.map((d) => ({ d, body: d.body, labelW: Math.max(textWidth(d.name, 20), textWidth(d.status, 17)) + 8 } as { d: StageDeviceView; body: Rect; labelW: number; label?: LabelSpot; rssi?: Spot }));
  // 패널이 열려 있으면 요청과 관계있는 기기가 패널 왼쪽에 보이도록 방을 왼쪽으로 민다 (기기 글자 폭만큼 여유)
  const focus = r && opt.panelOpen && !opt.panelWide && !opt.layoutCheck ? devices.filter((d) => keep.includes(d.nodeId)) : [];
  // 관계있는 기기가 왼쪽 칸 뒤로 들어가지 않을 만큼만 민다
  const edge = opt.panelEdge ?? PANEL_EDGE;
  const want = focus.length && !opt.viewFixed ? Math.ceil(Math.max(...focus.map((d) => d.body.x2)) + 110 - edge) : 0;
  const room = focus.length ? Math.floor(Math.min(...focus.map((d) => d.body.x1)) - STAGE_BOUNDS.x1 - 10) : 0;
  const shift = Math.max(0, Math.min(want, room));
  const bounds = shiftBounds(shift);
  // 왼쪽 칸 경계에 걸친 기기는 그리지 않는다 (글자 둘 곳이 없고, 관계없는 기기다)
  for (const d of devices) d.hidden = d.body.x1 - shift < STAGE_BOUNDS.x1;
  const shown = geoms.filter((g) => !g.d.hidden);
  placeDeviceLabels(shown, bounds);
  placeRssi(shown, bounds);
  for (const g of shown) { g.d.label = g.label!; g.d.rssiAt = g.rssi!; }

  // 사람
  const anchors = personAnchors(s, devices, visitors, bounds);
  // 이름표는 기기 본체와 글자, 사람 점, 이미 놓은 이름표를 피한다.
  // 신호 세기 글자는 요청 중에만 보이지만 이름표가 그때마다 옮겨 다니지 않게 늘 피한다. 기기 본체는 덮어도 된다.
  const labelObstacles: Rect[] = [...devices.flatMap((d) => [deviceLabelBox(d.label), rssiBox(d.rssiAt)]), ...[...anchors.values()].map(personBox)];
  const bodies = devices.map((d) => d.body);
  const vis: StageVisitorView[] = visitors.map((p) => {
    const at = anchors.get(p.id);
    const pos = at ?? { x: ROOM.x + ROOM.w - 200, y: ROOM.y + ROOM.h / 2 };
    const isReq = !!r && p.id === r.personId;
    const labelW = textWidth(p.pseudonym, 20) + 8;
    const label = chooseLabel(pos, labelObstacles, labelW, bodies, bounds);
    if (at) labelObstacles.push(labelBox(pos, label, labelW));
    return {
      personId: p.id, letter: p.letter!, name: p.pseudonym, x: pos.x, y: pos.y, labelW,
      active: s.connected && (r ? isReq : p.beaconSeen),
      dim: !!r && !opt.layoutCheck && !isReq,
      on: !!at && p.beaconSeen && personBox(pos).x1 - shift >= STAGE_BOUNDS.x1,
      label
    };
  });
  // 사람을 세운 뒤 신호 세기 글자를 다시 놓아 사람 점과 이름표를 피한다
  placeRssi(shown, bounds, vis.filter((p) => p.on).flatMap((p) => [personBox(p), labelBox(p, p.label, p.labelW)]));
  for (const g of shown) g.d.rssiAt = g.rssi!;

  // 신호와 흐름 선
  const idleShow = !r && s.connected && !opt.panelOpen && !opt.layoutCheck;
  const idlePerson = idleShow ? visitors.find((p) => p.beaconSeen && anchors.has(p.id)) : undefined;
  // 실행이 끝난 요청은 신호와 호출 선을 끈다
  const inFlight = !!r && (scene === 'judging' || (scene === 'flow' && r.execution !== 'succeeded') || scene === 'ask');
  const signalPerson: Person | undefined = opt.layoutCheck ? undefined : inFlight ? requester : idlePerson;
  // 이 장소에 세우지 않은 사람(다른 장소에 있는 사람)의 신호는 이 장소의 선으로 그리지 않는다
  const signalOn = !!signalPerson && anchors.has(signalPerson.id);
  const signals = vis.map((v) => ({ personId: v.personId, x: v.x, y: v.y, on: signalOn && signalPerson?.id === v.personId }));
  // 요청한 사람이 다른 장소에 있으면 이 장소 기기 옆에 그 사람의 신호 세기를 쓰지 않는다 (그 사람은 작은 방에 있다)
  const rssiPerson = !opt.layoutCheck && inFlight && uplinkHere ? requester : undefined;
  const rssi: Record<string, string> = {};
  if (rssiPerson) for (const [id, v] of Object.entries(rssiPerson.rssi)) rssi[id] = dbm(v);

  const target = r?.nodeId ?? null;
  // 잠금 중에 다른 장소의 기기에 요청이 오면 그 기기는 주 장소에 없다. 명령 선과 격리 선은 그리지 않는다.
  const targetHere = target !== null && devices.some((d) => d.nodeId === target);
  const view: StageView = {
    scene,
    placeId: layout.placeId,
    shift,
    aria: '',
    placeName: layout.placeName,
    modelVersion: layout.modelVersion,
    host: !opt.layoutCheck && !!r,
    server: !opt.layoutCheck && !!r,
    judge: !r || opt.layoutCheck
      ? { mark: '', tone: 'none', gauge: false, checks: CHECK_NAMES.map((n) => ['none', n] as [CheckResult, string]) }
      : isDecided(r)
        ? { mark: VERDICT_LABEL[r.verdict], tone: VERDICT_TONE[r.verdict], gauge: false, checks: serverChecks(r, places, reqPlace) }
        : { mark: '판정 중', tone: 'accent', gauge: true, checks: serverChecks(r, places, reqPlace) },
    owner: !opt.layoutCheck && !!r && r.verdict === 'NEED_APPROVAL' && s.ownerNotified,
    devices,
    visitors: vis,
    signals,
    rssiOn: !!rssiPerson,
    rssi,
    mcp: !!r && !opt.layoutCheck && !r.respondedAt,
    reply: !!r && !opt.layoutCheck && !!r.respondedAt && secOf(s.lastReceivedAt) - secOf(r.respondedAt) <= REPLY_SHOW_SEC,
    uplink: scene === 'judging' || scene === 'ask' ? uplinkHere : !!idlePerson,
    cmd: scene === 'flow' && r!.execution === 'sent' && targetHere ? target : null,
    push: scene === 'approval' && s.ownerNotified,
    fault: scene === 'fault' && targetHere ? target : null,
    callout: { on: false, tone: 'accent', x: 0, y: 0, ax: 0, ay: 0, lx: 0, ly: 0, title: '', body: '' }
  };

  // 콜아웃: 한 번에 하나. 기기 글자, 사람, 이름표, 신호 세기 글자를 피해서 가리키는 점 가까이에 둔다.
  const deviceTexts = devices.map((d) => deviceLabelBox(d.label));
  const calloutObstacles: Rect[] = [
    ...devices.flatMap((d) => [d.body, deviceLabelBox(d.label), frontBox({ x: d.fx, y: d.fy })]),
    ...vis.filter((v) => v.on).flatMap((v) => [ringBox(v), labelBox(v, v.label, v.labelW)]),
    ...(rssiPerson ? devices.map((d) => rssiBox(d.rssiAt)) : []),
    ...(opt.inset ? [opt.inset] : [])
  ];
  type Pos = Pick<CalloutView, 'x' | 'y' | 'ax' | 'ay' | 'lx' | 'ly'> & { clean: boolean };
  const place = (ax: number, ay: number): Pos => {
    const c = calloutSpot(ax, ay, calloutObstacles, opt.panelOpen ? edge - CALLOUT_W + shift : Infinity, deviceTexts, bounds);
    const l = leaderEnd(c, ax, ay);
    return { x: c.x, y: c.y, ax, ay, lx: l.x, ly: l.y, clean: c.clean };
  };
  const atServer = () => place(NODE_ANCHOR.server.x + shift, NODE_ANCHOR.server.y);
  // 이 장소에 없는 기기나 사람(다른 장소)을 가리킬 때: 작은 방이 있으면 작은 방 윗변 가운데, 없으면 서버 랙
  const atElsewhere = () => (opt.inset ? place((opt.inset.x1 + opt.inset.x2) / 2, opt.inset.y1) : atServer());
  const atDevice = (id: string | null) => {
    const d = devices.find((x) => x.nodeId === id);
    if (!d) return atElsewhere();
    const f = iso({ x: d.fx, y: d.fy });
    return place(f.x, f.y);
  };
  const atVisitor = (personId: string) => {
    const v = vis.find((x) => x.personId === personId && x.on);
    if (!v) {
      const p = s.people.find((x) => x.id === personId);
      return p?.beaconSeen && p.near && !devices.some((d) => d.nodeId === p.near) ? atElsewhere() : atServer();
    }
    const h = iso(v, PERSON_Z);
    return place(h.x, h.y - HEAD_R);
  };
  // 패널이 열려 있으면 패널이 같은 내용을 보이므로, 글자를 가리지 않고 둘 자리가 없을 때 콜아웃을 숨긴다
  const set = (tone: Tone, { clean, ...pos }: Pos, title: string, body: string) => {
    view.callout = { on: !opt.panelWide && (clean || !opt.panelOpen), tone, ...pos, title, body };
  };

  const who = requester?.pseudonym ?? '방문자';
  if (opt.layoutCheck) {
    view.aria = `${layout.placeName}의 표시용 기기 배치`;
  } else if (!r) {
    view.aria = s.connected ? `대기 중인 ${layout.placeName}` : `연결이 끊긴 ${layout.placeName}의 마지막 화면`;
    // 대기 안내는 글자를 가리지 않을 자리가 있을 때만 보인다
    const spot = idlePerson && atVisitor(idlePerson.id);
    if (spot?.clean) set('accent', spot, '비콘 신호를 받는 중', '');
  } else {
    const name = devName(target ?? undefined);
    const cap = CAPABILITY_NAME[r.target.capability ?? known(target ?? undefined)?.capability ?? 'locker'];
    const act = ACTION_NAME[r.action];
    switch (scene) {
      case 'judging':
        // 서버 랙이 판정 게이지를 보인다. 콜아웃은 두지 않는다.
        view.aria = `${who}의 ${cap} ${act} 요청을 서버가 판정하는 중`;
        break;
      case 'flow':
        if (r.execution === 'succeeded') {
          view.aria = `${who}의 요청으로 ${josa(name, '이', '가')} ${ACTION_DONE[r.action]}`;
          set('ok', atDevice(target), `${name} ${act} 완료`, '');
        } else if (!r.execution) {
          view.aria = `${who}의 ${cap} ${act} 요청을 허가함`;
          set('ok', atDevice(target), `${name} ${act} 허가`, '');
        } else {
          view.aria = `${who}의 ${cap} ${act} 요청이 실행되는 중`;
          set('ok', atDevice(target), `${name}에 ${act} 명령`, '');
        }
        break;
      case 'fault':
        if (r.execution === 'failed') {
          view.aria = `${josa(name, '이', '가')} 실행 실패를 알려 격리됨`;
          set('deny', atDevice(target), `${name} 격리`, '기기가 실패를 알려 사용을 막음');
        } else {
          view.aria = `${josa(name, '이', '가')} 결과 불명으로 격리됨`;
          set('deny', atDevice(target), `${name} 격리`, `${ACTION_DID[r.action]} 몰라 사용을 막음`);
        }
        break;
      case 'ask': {
        const top = Object.entries(r.posteriors ?? {}).sort((a, b) => b[1] - a[1]).slice(0, 2).map(([id]) => devName(id));
        if (r.reason === 'MOVE_CLOSER') view.aria = `${josa(who, '이', '가')} ${devName(r.target.nodeId)} 앞인지 확실하지 않아 서버가 다시 묻는 중`;
        else view.aria = top.length === 2 ? `${josa(who, '이', '가')} ${josa(top[0], '과', '와')} ${top[1]} 사이에 있어 서버가 다시 묻는 중` : `${who}에게 서버가 다시 묻는 중`;
        if (r.reason === 'MOVE_CLOSER') set('ask', atVisitor(r.personId), `${devName(r.target.nodeId)} 앞인지 애매해요`, '더 가까이 와 달라고 함');
        else set('ask', atVisitor(r.personId), `어느 ${cap} 앞인지 애매해요`, top.length === 2 ? `${top[0]}·${top[1]} 사이` : '');
        break;
      }
      case 'approval':
        view.aria = s.ownerNotified ? `${who}의 ${cap} ${act} 요청이 소유자 승인을 기다리는 중`
          : `${who}의 ${cap} ${act} 요청을 실행하지 않음: ${approvalText(r.reason!).title}`;
        if (r.accessExpiredAt) set('appr', place(NODE_ANCHOR.owner.x + shift, NODE_ANCHOR.owner.y), '승인 신청 만료', '소유자 응답 없음');
        else if (s.ownerNotified) set('appr', place(NODE_ANCHOR.owner.x + shift, NODE_ANCHOR.owner.y), '소유자에게 승인 요청', '');
        else set('appr', atDevice(target), approvalText(r.reason!).title, '');
        break;
      case 'busy':
        view.aria = `${who}의 ${cap} ${act} 요청이 바빠서 실행되지 않음`;
        if (target) set('busy', atDevice(target), r.reason === 'USER_BUSY' ? '이미 다른 기기를 쓰는 중' : r.reason === 'COMMAND_IN_PROGRESS' ? `${name} 명령 실행 중` : `${name} 사용 중`,
          '');
        else set('busy', atVisitor(r.personId), reasonText(r.reason!), '');
        break;
      case 'deny': {
        view.aria = `${who}의 ${cap} ${act} 요청을 거부함`;
        const t = ({
          BEACON_NOT_SEEN: ['폰 신호를 받지 못했어요', '비콘이 꺼졌거나 장소 밖'],
          NO_CANDIDATE: [`쓸 수 있는 ${josa(cap, '이', '가')} 없어요`, ''],
          TARGET_UNAVAILABLE: ['지금 쓸 수 없는 기기예요', '고장, 연결 끊김, 미신뢰 중 하나'],
          POLICY_DENIED: ['정책이 허락하지 않아요', ''],
          RECHECK_FAILED: ['실행 직전 재검사 실패', ''],
          INTERNAL_ERROR: ['서버 오류로 거부했어요', ''],
          CAPACITY_EXCEEDED: ['요청이 너무 많아요', ''],
          INVALID_INPUT: ['요청 형식이 맞지 않아요', ''],
          INVALID_TOKEN: ['인증하지 못했어요', ''],
          REQUEST_ID_CONFLICT: ['같은 요청 번호가 이미 있어요', '']
        } as Record<string, [string, string]>)[r.reason!] ?? ['요청을 거부했어요', ''];
        const pos = target ? atDevice(target) : atVisitor(r.personId);
        set('deny', pos, t[0], t[1]);
        break;
      }
    }
  }
  return view;
}

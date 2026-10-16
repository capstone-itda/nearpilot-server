// 스냅숏 → 무대(평면도) 표시값. 장면별 고정 값 없이 규칙으로 계산한다.
import type { DeviceStatus, FloorLayout, HostKind, LayoutDevice, Person, Point, RequestProgress, Snapshot } from '../model/types';
import {
  ACTION_DID, ACTION_DOING, ACTION_CAN, ACTION_DONE, ACTION_NAME, ACTION_OBJ, CAPABILITY_NAME,
  DEVICE_STATE_NAME, josa
} from './text';

/** 평면도 SVG 좌표계: 방 벽 사각형 */
export const ROOM = { x: 330, y: 90, w: 1010, h: 650 };
const CALLOUT_H = 76;

export type Tone = 'none' | 'active' | 'ok' | 'ask' | 'appr' | 'busy' | 'deny' | 'accent';
export type StageScene = 'idle' | 'flow' | 'fault' | 'ask' | 'approval' | 'busy' | 'deny' | 'layout';

export interface StageDeviceView {
  nodeId: string; name: string; capability: LayoutDevice['capability'];
  x: number; y: number; fx: number; fy: number; wall: LayoutDevice['wall'];
  status: string; code: string; tone: Tone; dim: boolean;
}
export interface StageVisitorView {
  personId: string; letter: string; x: number; y: number;
  active: boolean; dim: boolean; on: boolean; label: 'right' | 'left' | 'above';
}
export interface CalloutView { on: boolean; tone: Tone; x: number; y: number; ax: number; ay: number; title: string; body: string }

export interface StageView {
  scene: StageScene;
  aria: string;
  placeName: string;
  widthM: number;
  depthM: number;
  /** 축척 막대 1 m의 SVG 길이 */
  meterPx: number;
  modelVersion: string;
  host: HostKind | null;
  server: boolean;
  owner: boolean;
  devices: StageDeviceView[];
  visitors: StageVisitorView[];
  /** 사람별 비콘 신호 선 */
  signals: { personId: string; x: number; y: number; on: boolean }[];
  rssiOn: boolean;
  rssi: Record<string, string>;
  coords: boolean;
  mcp: HostKind | null;
  uplink: boolean;
  /** 실행 명령 선의 대상 기기 */
  cmd: string | null;
  push: boolean;
  /** 격리 선의 대상 기기 */
  fault: string | null;
  callout: CalloutView;
}

export const toSvg = (p: Point, layout: Pick<FloorLayout, 'widthM' | 'depthM'>) => ({
  x: Math.round((ROOM.x + (p.x * ROOM.w) / layout.widthM) * 10) / 10,
  y: Math.round((ROOM.y + (p.y * ROOM.h) / layout.depthM) * 10) / 10
});

const dbm = (v: number) => `${String(v).replace('-', '−')} dBm`;

function baseDevice(st: DeviceStatus | undefined): { status: string; code: string; tone: Tone } {
  if (!st) return { status: '알 수 없음', code: '—', tone: 'none' };
  if (st.state === 'FAULT') return { status: DEVICE_STATE_NAME.FAULT, code: 'FAULT', tone: 'deny' };
  if (st.state === 'AVAILABLE') return { status: DEVICE_STATE_NAME.AVAILABLE, code: 'AVAILABLE', tone: 'none' };
  const name = st.state === 'CLOSING' ? DEVICE_STATE_NAME.CLOSING : DEVICE_STATE_NAME.ACTIVE;
  return { status: st.overdue ? `${name} · 대여 초과` : name, code: st.state, tone: 'active' };
}

/** 요청과 관계있는 기기: 고른 기기, 없으면 요청한 기능의 기기 전부 */
export function focusDevices(r: RequestProgress, layout: FloorLayout): string[] {
  if (r.nodeId) return [r.nodeId];
  if (r.target.nodeId) return [r.target.nodeId];
  return layout.devices.filter((d) => d.capability === r.target.capability).map((d) => d.nodeId);
}

export interface StageOptions {
  /** 무대 위에 패널이 열려 있는가 (콜아웃을 패널 밖으로 둔다) */
  panelOpen: boolean;
  /** SET-01 배치 점검 */
  layoutCheck?: boolean;
}

export function stageView(s: Snapshot, layout: FloorLayout, opt: StageOptions): StageView {
  const r = s.request;
  const byId = new Map(s.devices.map((d) => [d.nodeId, d]));
  const devName = (id?: string) => layout.devices.find((d) => d.nodeId === id)?.name ?? id ?? '';
  const visitors = s.people.filter((p) => p.role === 'visitor' && p.letter);
  const requester = r ? s.people.find((p) => p.id === r.personId) : undefined;
  const keep = r ? focusDevices(r, layout) : [];

  // 장면 이름
  let scene: StageScene = 'idle';
  if (opt.layoutCheck) scene = 'layout';
  else if (r) {
    if (r.verdict === 'OK') scene = r.execution === 'unknown' || r.execution === 'failed' ? 'fault' : 'flow';
    else scene = ({ ASK: 'ask', NEED_APPROVAL: 'approval', BUSY: 'busy', DENY: 'deny' } as const)[r.verdict];
  }

  // 기기
  const devices: StageDeviceView[] = layout.devices.map((d) => {
    const b = toSvg(d.body, layout);
    const f = toSvg(d.front, layout);
    const v = { ...baseDevice(byId.get(d.nodeId)) };
    if (r && !opt.layoutCheck && keep.length === 1 && keep[0] === d.nodeId) {
      if (r.verdict === 'OK' && r.execution === 'sent') { v.status = ACTION_DOING[r.action]; v.tone = 'ok'; }
      else if (r.verdict === 'NEED_APPROVAL') v.tone = 'appr';
      else if (r.verdict === 'BUSY') v.tone = 'busy';
    }
    return {
      nodeId: d.nodeId, name: d.name, capability: d.capability, x: b.x, y: b.y, fx: f.x, fy: f.y, wall: d.wall,
      ...v, dim: !!r && !opt.layoutCheck && !keep.includes(d.nodeId)
    };
  });

  // 사람
  const calloutOnVisitor = !opt.layoutCheck && !!r && (r.verdict === 'ASK' || (r.verdict === 'DENY' && !r.nodeId));
  const vis: StageVisitorView[] = visitors.map((p) => {
    const pos = p.position ? toSvg(p.position, layout) : { x: ROOM.x + ROOM.w - 200, y: ROOM.y + ROOM.h / 2 };
    const isReq = !!r && p.id === r.personId;
    const label: StageVisitorView['label'] = isReq && calloutOnVisitor ? 'left' : pos.y > ROOM.y + ROOM.h * 0.6 ? 'above' : 'right';
    return {
      personId: p.id, letter: p.letter!, x: pos.x, y: pos.y,
      active: s.connected && (r ? isReq : p.beaconSeen),
      dim: !!r && !opt.layoutCheck && !isReq,
      on: !!p.position && p.beaconSeen,
      label
    };
  });

  // 신호와 흐름 선
  const idleShow = !r && s.connected && !opt.panelOpen && !opt.layoutCheck;
  const idlePerson = idleShow ? visitors.find((p) => p.beaconSeen && p.position) : undefined;
  const signalPerson: Person | undefined = opt.layoutCheck
    ? undefined
    : r && (scene === 'flow' || scene === 'ask') ? requester : idlePerson;
  const signals = vis.map((v) => ({ personId: v.personId, x: v.x, y: v.y, on: !!signalPerson && signalPerson.id === v.personId }));
  const rssiPerson = r && (scene === 'flow' || scene === 'ask') ? requester : undefined;
  const rssi: Record<string, string> = {};
  if (rssiPerson) for (const [id, v] of Object.entries(rssiPerson.rssi)) rssi[id] = dbm(v);

  const target = r?.nodeId ?? null;
  const view: StageView = {
    scene,
    aria: '',
    placeName: layout.placeName,
    widthM: layout.widthM,
    depthM: layout.depthM,
    meterPx: Math.round((ROOM.w / layout.widthM) * 10) / 10,
    modelVersion: layout.modelVersion,
    host: opt.layoutCheck ? null : r?.host ?? null,
    server: !opt.layoutCheck && !!r,
    owner: !opt.layoutCheck && !!r && r.verdict === 'NEED_APPROVAL' && s.ownerNotified,
    devices,
    visitors: vis,
    signals,
    rssiOn: !!rssiPerson,
    rssi,
    coords: !!opt.layoutCheck,
    mcp: scene === 'flow' ? r!.host : null,
    uplink: scene === 'ask' || !!idlePerson,
    cmd: scene === 'flow' && r!.execution === 'sent' ? target : null,
    push: scene === 'approval' && s.ownerNotified,
    fault: scene === 'fault' ? target : null,
    callout: { on: false, tone: 'accent', x: 0, y: 0, ax: 0, ay: 0, title: '', body: '' }
  };

  // 콜아웃: 한 번에 하나
  const anchorDevice = (id: string | null) => devices.find((d) => d.nodeId === id);
  const place = (kind: 'device-top' | 'device-bottom' | 'visitor' | 'owner' | 'server', ax: number, ay: number): Pick<CalloutView, 'x' | 'y' | 'ax' | 'ay'> => {
    const maxX = opt.panelOpen ? 440 : 860;
    const x = Math.min(Math.max(ax - 90, 345), maxX);
    let y: number;
    if (kind === 'owner' || kind === 'server') y = 400;
    else if (kind === 'device-bottom') y = ay - 34 - CALLOUT_H;
    else y = ay + 34 + CALLOUT_H > ROOM.y + ROOM.h ? ay - 92 - 34 - CALLOUT_H : ay + 34;
    return { x, y, ax, ay };
  };
  const atDevice = (id: string | null) => {
    const d = anchorDevice(id)!;
    return d.wall === 'top' ? place('device-top', d.fx, d.fy + 46) : place('device-bottom', d.fx, d.fy - 46);
  };
  const atVisitor = (personId: string) => {
    const v = vis.find((x) => x.personId === personId && x.on);
    return v ? place('visitor', v.x, v.y + 46) : place('server', 250, 440);
  };
  const set = (tone: Tone, pos: Pick<CalloutView, 'x' | 'y' | 'ax' | 'ay'>, title: string, body: string) => {
    view.callout = { on: true, tone, ...pos, title, body };
  };

  const who = requester?.pseudonym ?? '방문자';
  if (opt.layoutCheck) {
    view.aria = `${layout.placeName} 근접 모델 파일의 기기 배치`;
  } else if (!r) {
    view.aria = s.connected ? `대기 중인 ${layout.placeName}` : `연결이 끊긴 ${layout.placeName}의 마지막 화면`;
    if (idlePerson) set('accent', atVisitor(idlePerson.id), '폰이 비콘 신호를 보내는 중', '기기가 신호 세기를 재서 서버로 보내요');
  } else {
    const name = devName(target ?? undefined);
    const cap = CAPABILITY_NAME[r.target.capability ?? layout.devices.find((d) => d.nodeId === target)?.capability ?? 'locker'];
    const act = ACTION_NAME[r.action];
    switch (scene) {
      case 'flow':
        if (r.execution === 'succeeded') {
          view.aria = `${who}의 요청으로 ${josa(name, '이', '가')} ${ACTION_DONE[r.action]}`;
          set('ok', atDevice(target), `${name} ${act} 완료`, `${josa(name, '이', '가')} ${ACTION_DONE[r.action]}`);
        } else {
          view.aria = `${who}의 ${cap} ${act} 요청이 실행되는 중`;
          set('ok', atDevice(target), `${name} ${act} 명령`, `${layout.devices.find((d) => d.nodeId === target)?.occupancy === 'shared' ? '세 가지' : '네 가지'} 확인을 모두 통과해서 ${ACTION_OBJ[r.action]} 보냈어요`);
        }
        break;
      case 'fault':
        if (r.execution === 'failed') {
          view.aria = `${josa(name, '이', '가')} 실행 실패를 알림`;
          set('deny', atDevice(target), `${name} 실행 실패`, '기기가 실패를 알려서 실행하지 않았어요');
        } else {
          view.aria = `${josa(name, '이', '가')} 결과 불명으로 격리됨`;
          set('deny', atDevice(target), `${name} 격리`, `${ACTION_DID[r.action]} 알 수 없어서 다시 쓰지 않게 막았어요`);
        }
        break;
      case 'ask': {
        const top = Object.entries(r.posteriors ?? {}).sort((a, b) => b[1] - a[1]).slice(0, 2).map(([id]) => devName(id));
        view.aria = top.length === 2 ? `${josa(who, '이', '가')} ${josa(top[0], '과', '와')} ${top[1]} 사이에 있어 서버가 다시 묻는 중` : `${who}에게 서버가 다시 묻는 중`;
        if (r.reason === 'MOVE_CLOSER') set('ask', atVisitor(r.personId), `${devName(r.target.nodeId)} 앞인지 확실하지 않아요`, '조금 더 가까이 가 달라고 해요');
        else set('ask', atVisitor(r.personId), `어느 ${cap} 앞인지 애매해요`, top.length === 2 ? `${josa(top[0], '과', '와')} ${top[1]} 사이라서 다시 물어봐요` : '다시 물어봐요');
        break;
      }
      case 'approval':
        view.aria = `${who}의 ${cap} ${act} 요청이 소유자 승인을 기다리는 중`;
        if (s.ownerNotified) set('appr', place('owner', 250, 632), '소유자에게 승인 요청', `${josa(name || cap, '은', '는')} 소유자가 승인해야 ${ACTION_CAN[r.action]} 수 있어요`);
        else set('appr', atDevice(target), '소유자 승인이 필요해요', 'AI가 승인을 신청하면 소유자 폰에 알림이 가요');
        break;
      case 'busy':
        view.aria = `${who}의 ${cap} ${act} 요청이 바빠서 실행되지 않음`;
        if (target) set('busy', atDevice(target), r.reason === 'USER_BUSY' ? '이미 다른 기기를 쓰는 중' : `${name} 사용 중`,
          r.reason === 'USER_BUSY' ? '쓰던 기기를 먼저 반납해야 해요' : r.reason === 'COMMAND_IN_PROGRESS' ? '다른 명령을 실행하는 중이라 기다려야 해요' : '다른 사람이 쓰고 있어서 실행하지 않았어요');
        break;
      case 'deny': {
        view.aria = `${who}의 ${cap} ${act} 요청을 거부함`;
        const t = ({
          BEACON_NOT_SEEN: ['폰 신호를 받지 못했어요', '비콘이 꺼져 있거나 장소 밖이에요'],
          NO_CANDIDATE: ['쓸 수 있는 기기가 없어요', `이 장소에 쓸 수 있는 ${josa(cap, '이', '가')} 없어요`],
          TARGET_UNAVAILABLE: ['지금 쓸 수 없는 기기예요', '고장, 연결 끊김, 미신뢰 중 하나예요'],
          POLICY_DENIED: ['정책이 허락하지 않아요', '이 기기는 이 사람에게 열려 있지 않아요']
        } as Record<string, [string, string]>)[r.reason] ?? ['요청을 거부했어요', r.reason];
        const pos = target ? atDevice(target) : atVisitor(r.personId);
        set('deny', pos, t[0], t[1]);
        break;
      }
    }
  }
  return view;
}

// FR-12B: 모니터 상태에서 무대 표시를 규칙으로 계산한다
import { describe, expect, it } from 'vitest';
import { LAYOUT } from '../data/mock/fixtures';
import { SCENES, sec, stateAt } from '../data/mock/story';
import { NODE_ANCHOR, stageView } from './stage';
import { LOUNGE, PLACES, SIDE_ROOM, WEAK_RSSI_DBM } from '../data/mock/fixtures';
import type { MonitorState } from '../model/types';
import { resolveArrangement, withArrangement } from './arrange';
import { insetView } from './inset';
import { CALLOUT_H, CALLOUT_W, PANEL_EDGE, STAGE_BOUNDS, canPlaceAll, crosses, deviceLabelBox, hits, labelBox, personBox, rssiBox, type Rect } from './placement';
import { nextFocus } from './places';

const dev = (v: ReturnType<typeof stageView>, id: string) => v.devices.find((d) => d.nodeId === id)!;

describe('stageView', () => {
  it('FR12B 대기: 비콘을 보내는 첫 방문자의 신호와 상향 흐름, 안내 콜아웃', () => {
    const v = stageView(SCENES.idle(), LAYOUT, { panelOpen: false });
    expect(v.scene).toBe('idle');
    expect(v.signals.find((s) => s.personId === 'p-a')!.on).toBe(true);
    expect(v.signals.find((s) => s.personId === 'p-b')!.on).toBe(false);
    expect(v.uplink).toBe(true);
    expect(v.callout).toMatchObject({ on: true, tone: 'accent', title: '비콘 신호를 받는 중' });
    expect(v.devices.every((d) => !d.dim)).toBe(true);
    expect(dev(v, 'locker-3')).toMatchObject({ status: '사용 중', code: 'ACTIVE', tone: 'active' });
  });

  it('FR12B 대기: 패널이 열려 있으면 콜아웃과 신호를 숨긴다', () => {
    const v = stageView(SCENES.later(), LAYOUT, { panelOpen: true });
    expect(v.callout.on).toBe(false);
    expect(v.signals.some((s) => s.on)).toBe(false);
  });

  it('FR12B 요청 진행(OK, 명령 보냄): 대상만 강조, MCP와 명령 선, RSSI', () => {
    const v = stageView(SCENES.flow(), LAYOUT, { panelOpen: false });
    expect(v.scene).toBe('flow');
    expect(dev(v, 'locker-1')).toMatchObject({ status: '결과 대기', code: 'RESERVED', tone: 'ok', dim: false });
    expect(v.devices.filter((d) => d.nodeId !== 'locker-1').every((d) => d.dim)).toBe(true);
    expect(dev(v, 'locker-3').status).toBe('사용 중 · 대여 초과');
    expect(v).toMatchObject({ host: true, server: true, mcp: true, cmd: 'locker-1', fault: null, push: false, rssiOn: true });
    expect(v.rssi['locker-1']).toBe('−52 dBm');
    expect(v.callout).toMatchObject({ on: true, tone: 'ok', title: '사물함 1에 열기 명령', body: '' });
    expect(v.judge).toEqual({ mark: '허가', tone: 'ok', gauge: false, checks: [['pass', '누구인지'], ['pass', '사물함 1 앞'], ['pass', '정책 통과'], ['pass', '비어 있음']] });
    expect(v.visitors.find((x) => x.personId === 'p-b')!.on).toBe(false);
  });

  it('FR12B 요청 중 신호 세기 아래에 후보 확률 막대와 기준선', () => {
    const ok = stageView(SCENES.flow(), LAYOUT, { panelOpen: false });
    expect(ok.threshold).toBe(0.95);
    expect(ok.prob).toEqual({ 'locker-1': { v: 0.97, tone: 'ok' }, 'locker-2': { v: 0.02, tone: 'muted' }, 'locker-3': { v: 0.01, tone: 'muted' } });
    const ask = stageView(SCENES.ask(), LAYOUT, { panelOpen: false });
    expect(ask.prob['locker-1']).toEqual({ v: 0.58, tone: 'ask' });
    expect(ask.prob['locker-2'].tone).toBe('muted');
  });

  it('FR12B 확률이 없거나 신호 세기를 끄면 막대도 없다', () => {
    const s = SCENES.flow();
    s.request = { ...s.request!, posteriors: undefined, threshold: undefined };
    expect(stageView(s, LAYOUT, { panelOpen: false })).toMatchObject({ rssiOn: true, prob: {} });
    const done = SCENES.flow();
    done.request = { ...done.request!, execution: 'succeeded' };
    expect(stageView(done, LAYOUT, { panelOpen: false })).toMatchObject({ rssiOn: false, prob: {}, threshold: undefined });
  });

  it('FR12B 공용형 OK: 서버 랙의 빈자리 칸은 공용형 생략', () => {
    const s = SCENES.approval();
    s.request = { ...s.request!, verdict: 'OK', reason: 'OK', execution: 'sent', steps: s.request!.steps.map((x, i) => (i < 5 ? { result: 'pass' as const } : x)) };
    expect(stageView(s, LAYOUT, { panelOpen: false }).judge.checks[3]).toEqual(['none', '공용형 생략']);
  });

  it('FR12B 패널이 열려 있으면 방을 왼쪽으로 밀어 대상 기기를 패널 왼쪽에 보인다', () => {
    for (const make of [SCENES.fault, SCENES.ask, SCENES.approval]) {
      const v = stageView(make(), LAYOUT, { panelOpen: true });
      const focus = v.devices.filter((d) => !d.dim);
      expect(focus.length).toBeGreaterThan(0);
      for (const d of focus) {
        expect(d.body.x2 - v.shift, d.name).toBeLessThanOrEqual(PANEL_EDGE);
        expect(d.hidden, d.name).toBe(false);
      }
      expect(stageView(make(), LAYOUT, { panelOpen: false }).shift).toBe(0);
      expect(stageView(make(), LAYOUT, { panelOpen: true, panelWide: true }).shift, '넓은 패널이면 밀지 않음').toBe(0);
    }
  });

  it('FR12B 패널을 넓혀 콜아웃 둘 자리가 없으면 숨기고, 무대를 확대하면 방을 밀지 않는다', () => {
    const v = stageView(SCENES.fault(), LAYOUT, { panelOpen: true, panelEdge: 500 });
    expect(v.callout.on).toBe(false);
    expect(stageView(SCENES.fault(), LAYOUT, { panelOpen: true, viewFixed: true }).shift).toBe(0);
  });

  it('FR12B 고장 격리: 격리 선과 빨간 콜아웃, 패널이 열려 있으면 콜아웃을 왼쪽에', () => {
    const v = stageView(SCENES.fault(), LAYOUT, { panelOpen: true });
    expect(v.scene).toBe('fault');
    expect(dev(v, 'locker-2')).toMatchObject({ status: '고장 격리', tone: 'deny', dim: false });
    expect(v).toMatchObject({ fault: 'locker-2', cmd: null, mcp: false });
    expect(v.callout).toMatchObject({ tone: 'deny', title: '사물함 2 격리', body: '제어 신호 출력 결과 불명' });
    expect(v.callout.x - v.shift).toBeLessThanOrEqual(440);
  });

  it('FR12B ASK: 요청한 기능의 기기만 남기고 방문자에게 콜아웃', () => {
    const v = stageView(SCENES.ask(), LAYOUT, { panelOpen: true });
    expect(v.devices.filter((d) => !d.dim).map((d) => d.nodeId)).toEqual(['locker-1', 'locker-2', 'locker-3']);
    expect(v.uplink).toBe(true);
    expect(v.callout).toMatchObject({ tone: 'ask', title: '어느 사물함 앞인지 애매해요', body: '사물함 1·사물함 2 사이' });
    expect(v.judge).toMatchObject({ mark: '다시 질문', tone: 'ask' });
    expect(v.judge.checks[1]).toEqual(['unsure', '어디 앞인지 애매']);
  });

  it('FR12B 승인 대기: 소유자 폰 알림 선과 보라 콜아웃, 대상 조명은 승인 색', () => {
    const v = stageView(SCENES.approval(), LAYOUT, { panelOpen: true });
    expect(v).toMatchObject({ scene: 'approval', owner: true, push: true, host: true });
    expect(dev(v, 'light-1')).toMatchObject({ tone: 'appr', status: '사용 가능', dim: false });
    expect(v.callout).toMatchObject({ tone: 'appr', title: '소유자에게 승인 요청', body: '' });
    expect(v.callout.x - v.shift).toBeLessThanOrEqual(440);
    const a = v.visitors.find((x) => x.personId === 'p-a')!;
    expect(a).toMatchObject({ active: false, dim: true });
  });

  it('FR12B BUSY와 DENY도 콜아웃 색을 판정 색으로 쓴다', () => {
    const busy = { ...SCENES.later(), request: { ...SCENES.flow().request!, verdict: 'BUSY' as const, reason: 'DEVICE_BUSY' as const, nodeId: 'locker-3', execution: undefined } };
    expect(stageView(busy, LAYOUT, { panelOpen: false }).callout).toMatchObject({ tone: 'busy', title: '사물함 3 사용 중' });
    const deny = { ...SCENES.later(), request: { ...SCENES.flow().request!, verdict: 'DENY' as const, reason: 'BEACON_NOT_SEEN' as const, nodeId: undefined, execution: undefined } };
    expect(stageView(deny, LAYOUT, { panelOpen: false }).callout).toMatchObject({ tone: 'deny', title: '폰 신호를 받지 못했어요' });
  });

  it('FR12B 연결 끊김: 콜아웃과 움직임을 멈춘다', () => {
    const v = stageView(SCENES.offline(), LAYOUT, { panelOpen: false });
    expect(v.callout.on).toBe(false);
    expect(v.visitors.every((x) => !x.active)).toBe(true);
  });

  it('FR12B D4 배치 화면: 흐름과 콜아웃을 숨긴다', () => {
    const v = stageView(SCENES.later(), LAYOUT, { panelOpen: false, layoutCheck: true });
    expect(v).toMatchObject({ scene: 'layout', host: false, server: false, uplink: false, rssiOn: false });
    expect(v.callout.on).toBe(false);
  });

  it('FR12B D4 기기는 표시 배치 자리에 그리고, 기기 앞은 방 안쪽에 둔다', () => {
    const v = stageView(SCENES.idle(), LAYOUT, { panelOpen: false });
    expect(dev(v, 'locker-1')).toMatchObject({ x: 456, y: 120, fx: 456, fy: 264, wall: 'top' });
    expect(dev(v, 'light-3')).toMatchObject({ x: 960, y: 720, fx: 960, fy: 576, wall: 'bottom' });
  });
});

describe('stageView 사유별 콜아웃', () => {
  const failed = () => {
    const s = SCENES.fault();
    s.request = { ...s.request!, execution: 'failed' };
    return s;
  };
  it('FR12B 실행 실패도 격리로 보이고 실행하지 않았다고 단정하지 않는다', () => {
    const c = stageView(failed(), LAYOUT, { panelOpen: false }).callout;
    expect(c).toMatchObject({ tone: 'deny', title: '사물함 2 격리', body: '기기가 실패를 알려 사용을 막음' });
  });
  it('FR12B 다른 명령이 진행 중인 BUSY는 점유와 구분한다', () => {
    const s = SCENES.flow();
    s.request = { ...s.request!, verdict: 'BUSY', reason: 'COMMAND_IN_PROGRESS', execution: undefined };
    expect(stageView(s, LAYOUT, { panelOpen: false }).callout.title).toBe('사물함 1 명령 실행 중');
  });
  it('FR12B 거부 사유를 코드 그대로 보이지 않는다', () => {
    const s = SCENES.flow();
    for (const reason of ['RECHECK_FAILED', 'INTERNAL_ERROR', 'CAPACITY_EXCEEDED'] as const) {
      s.request = { ...SCENES.flow().request!, verdict: 'DENY', reason, execution: undefined };
      expect(stageView(s, LAYOUT, { panelOpen: false }).callout.body).not.toContain(reason);
    }
  });
});

describe('무대의 사람 자리 (표시용)', () => {
  const at = (v: ReturnType<typeof stageView>, id: string) => v.visitors.find((x) => x.personId === id)!;
  it('FR12B 요청한 사람은 판정이 고른 기기 앞에 선다', () => {
    const v = stageView(SCENES.flow(), LAYOUT, { panelOpen: false });
    expect(at(v, 'p-a')).toMatchObject({ x: dev(v, 'locker-1').fx, y: dev(v, 'locker-1').fy, on: true });
  });
  it('FR12B ASK면 확률이 높은 두 기기 사이에 선다', () => {
    const v = stageView(SCENES.ask(), LAYOUT, { panelOpen: false });
    expect(at(v, 'p-a').x).toBe((dev(v, 'locker-1').fx + dev(v, 'locker-2').fx) / 2);
  });
  it('FR12B 요청하지 않은 사람은 신호가 가장 센 기기 앞, 신호가 없으면 그리지 않는다', () => {
    const v = stageView(SCENES.idle(), LAYOUT, { panelOpen: false });
    expect(at(v, 'p-b')).toMatchObject({ x: dev(v, 'locker-3').fx, on: true });
    expect(at(v, 'p-c').on).toBe(false);
  });
  it('FR12B 같은 기기 앞의 두 사람은 겹치지 않게 띄운다', () => {
    const s = SCENES.idle();
    s.people = s.people.map((p) => (p.id === 'p-b' ? { ...p, rssi: { 'locker-1': -50 } } : p.id === 'p-a' ? { ...p, rssi: { 'locker-1': -50 } } : p));
    const v = stageView(s, LAYOUT, { panelOpen: false });
    expect(at(v, 'p-a').y).not.toBe(at(v, 'p-b').y);
  });
  it('FR12B 실행이 끝난 요청은 호출·신호 선을 끈다', () => {
    const v = stageView(SCENES.done(), LAYOUT, { panelOpen: false });
    expect(v).toMatchObject({ mcp: false, rssiOn: false, cmd: null });
    expect(v.callout).toMatchObject({ title: '사물함 1 열기', body: '제어 신호 출력 완료' });
  });
  it('FR12B 예약된 기기는 사용 중이 아니라 예약으로 보인다', () => {
    const v = stageView(SCENES.flow(), LAYOUT, { panelOpen: false, layoutCheck: true });
    expect(dev(v, 'locker-1')).toMatchObject({ status: '예약', code: 'RESERVED' });
  });
  it('FR12B 승인 필요 사유를 구분해서 보인다', () => {
    const s = SCENES.approval();
    s.ownerNotified = false;
    s.request = { ...s.request!, reason: 'APPROVAL_PENDING', accessRequestedAt: undefined };
    expect(stageView(s, LAYOUT, { panelOpen: false }).callout.title).toBe('소유자 응답을 기다려요');
  });
  it('FR12B 승인 대기가 아닌 승인 사유는 읽기용 설명에도 사유를 쓰고, 알림 수단은 단정하지 않는다', () => {
    const s = SCENES.approval();
    s.ownerNotified = false;
    s.request = { ...s.request!, reason: 'ACTION_NOT_ALLOWED', accessRequestedAt: undefined };
    expect(stageView(s, LAYOUT, { panelOpen: false }).aria).toContain('승인받은 동작과 달라요');
    s.request = { ...s.request!, reason: 'APPROVAL_REQUIRED' };
    expect(stageView(s, LAYOUT, { panelOpen: false }).callout.body).not.toContain('알림');
  });
  it('FR12B 기기 없이 멈춘 BUSY도 요청한 사람에게 콜아웃을 단다', () => {
    const s = SCENES.flow();
    s.request = { ...s.request!, verdict: 'BUSY', reason: 'NOT_ASSIGNED', nodeId: undefined, target: { kind: 'nearest', capability: 'locker' }, execution: undefined };
    const v = stageView(s, LAYOUT, { panelOpen: false });
    expect(v.callout).toMatchObject({ on: true, title: '배정받지 못함' });
  });
});

describe('바꾼 배치에서도 무대 글자가 겹치지 않는다', () => {
  const ARRANGEMENTS: Record<string, Record<string, { x: number; y: number }>> = {
    // 사물함을 방 가운데 줄에, 조명·스피커는 아래 벽에
    mid2: { 'locker-1': { x: 456, y: 432 }, 'locker-2': { x: 624, y: 432 }, 'locker-3': { x: 792, y: 432 } },
    // 사물함 두 대를 위아래로 쌓기
    stack: { 'locker-1': { x: 600, y: 120 }, 'locker-2': { x: 600, y: 336 } },
    // 사물함은 가운데 위쪽 줄, 조명·스피커는 위 벽에
    mid: { 'locker-1': { x: 456, y: 336 }, 'locker-2': { x: 624, y: 336 }, 'locker-3': { x: 792, y: 336 },
      'speaker-1': { x: 456, y: 120 }, 'light-1': { x: 624, y: 120 }, 'light-2': { x: 792, y: 120 }, 'light-3': { x: 960, y: 120 } },
    // 아래 기기를 오른쪽 끝으로
    right: { 'speaker-1': { x: 744, y: 720 }, 'light-1': { x: 912, y: 720 }, 'light-2': { x: 1080, y: 720 }, 'light-3': { x: 1248, y: 720 } },
    // 오른쪽 위 구석에 사물함과 조명을 모으기
    rightcluster: { 'locker-1': { x: 1248, y: 120 }, 'locker-2': { x: 1248, y: 336 }, 'locker-3': { x: 1080, y: 120 }, 'light-3': { x: 1080, y: 336 },
      'speaker-1': { x: 744, y: 720 }, 'light-1': { x: 912, y: 720 }, 'light-2': { x: 1080, y: 720 } },
    // 왼쪽 벽에 사물함 두 대를 위아래로
    stackleft: { 'locker-1': { x: 408, y: 120 }, 'locker-2': { x: 408, y: 336 } },
    // 모든 기기를 아래 벽에
    bottomall: { 'locker-1': { x: 408, y: 720 }, 'locker-2': { x: 576, y: 720 }, 'locker-3': { x: 744, y: 720 },
      'speaker-1': { x: 912, y: 720 }, 'light-1': { x: 1080, y: 720 }, 'light-2': { x: 1248, y: 504 }, 'light-3': { x: 1080, y: 504 } }
  };
  // 무대 좌표(투영 뒤)에서 잰다. 글자는 무대 영역 안에 있고 서로 겹치지 않는다.
  const within = (a: Rect) => a.x1 >= STAGE_BOUNDS.x1 && a.x2 <= STAGE_BOUNDS.x2 && a.y1 >= STAGE_BOUNDS.y1 && a.y2 <= STAGE_BOUNDS.y2;
  const pairs = (list: Rect[]) => list.some((a, i) => list.some((b, j) => j > i && hits(a, b)));
  /** 그린 무대의 기기 글자, 사람, 콜아웃, 신호 글자가 무대 안에 있고 서로 겹치지 않는다 */
  const expectClean = (v: ReturnType<typeof stageView>) => {
    // 방을 밀면 글자 영역도 같이 밀린다. 왼쪽 칸 뒤로 들어간 기기는 그리지 않는다.
    const within = (a: Rect) => a.x1 >= STAGE_BOUNDS.x1 + v.shift && a.x2 <= STAGE_BOUNDS.x2 + v.shift && a.y1 >= STAGE_BOUNDS.y1 && a.y2 <= STAGE_BOUNDS.y2;
    const texts = v.devices.filter((d) => !d.hidden).map((d) => deviceLabelBox(d.label));
    expect(pairs(texts), '기기 이름표끼리').toBe(false);
    expect(texts.every(within), '기기 이름표가 무대 안').toBe(true);
    const people = v.visitors.filter((x) => x.on);
    for (const p of people) {
      expect(texts.some((t) => hits(personBox(p), t)), `${p.name} 점`).toBe(false);
      expect(texts.some((t) => hits(labelBox(p, p.label, p.labelW), t)), `${p.name} 이름표`).toBe(false);
      expect(within(labelBox(p, p.label, p.labelW)), `${p.name} 이름표가 무대 안`).toBe(true);
    }
    expect(pairs(people.map((p) => labelBox(p, p.label, p.labelW))), '사람 이름표끼리').toBe(false);
    if (v.callout.on) {
      const c = { x1: v.callout.x, y1: v.callout.y, x2: v.callout.x + CALLOUT_W, y2: v.callout.y + CALLOUT_H };
      expect(texts.some((t) => hits(c, t)), '콜아웃과 기기 글자').toBe(false);
      expect(within(c), '콜아웃이 무대 안').toBe(true);
      const a = { x: v.callout.ax, y: v.callout.ay };
      const e = { x: v.callout.lx, y: v.callout.ly };
      expect(texts.some((t) => crosses(a, e, t)), '지시선과 기기 글자').toBe(false);
    }
    if (v.rssiOn) {
      const rs = v.devices.filter((d) => !d.hidden).map((d) => rssiBox(d.rssiAt));
      expect(pairs(rs), '신호 글자끼리').toBe(false);
      expect(rs.some((r) => texts.some((t) => hits(r, t))), '신호 글자와 기기 이름표').toBe(false);
      expect(rs.every(within), '신호 글자가 무대 안').toBe(true);
      expect(rs.some((r) => people.some((p) => hits(r, labelBox(p, p.label, p.labelW)))), '신호 글자와 사람 이름표').toBe(false);
      expect(rs.some((r) => people.some((p) => hits(r, personBox(p)))), '신호 글자와 사람 점').toBe(false);
    }
  };

  for (const [arrName, saved] of Object.entries(ARRANGEMENTS)) {
    const arr = resolveArrangement(LOUNGE.devices, saved);
    it(`FR12A D4 ${arrName} 배치는 저장한 자리 그대로 허용된다`, () => expect(canPlaceAll(arr, saved)).toBe(true));
    const layout = withArrangement(LOUNGE, arr, WEAK_RSSI_DBM);
    for (const [scene, make] of Object.entries(SCENES)) {
      for (const panelOpen of [false, true]) {
        it(`FR12B D4 ${arrName} 배치 · ${scene} 장면 · 패널 ${panelOpen ? '열림' : '닫힘'}`, () => {
          expectClean(stageView(make(), layout, { panelOpen }));
          expect(stageView(make(), layout, { panelOpen: true, panelWide: true }).callout.on, '넓은 패널이면 콜아웃 숨김').toBe(false);
        });
      }
    }
    // 옆방 장면은 자동이면 주 장소가 옆방이라 라운지 배치가 쓰이지 않는다. 라운지에 고정하면 라운지를 크게 그리고 옆방을 작은 방으로 그린다.
    for (const panelOpen of [false, true]) {
      it(`FR12B D4 ${arrName} 배치 · side-room 장면 · 라운지 고정, 작은 방 포함 · 패널 ${panelOpen ? '열림' : '닫힘'}`, () => {
        const s = SCENES['side-room']();
        const opt = { panelOpen, places: PLACES };
        const base = stageView(s, layout, opt);
        const side = withArrangement(SIDE_ROOM, resolveArrangement(SIDE_ROOM.devices), WEAK_RSSI_DBM);
        const inset = insetView(s, side, base, opt);
        // 패널이 열리면 작은 방을 그리지 않는다. 닫혀 있으면 어느 배치에서도 그릴 자리가 있다.
        expect(inset !== undefined, '작은 방을 그림').toBe(!panelOpen);
        const v = inset ? stageView(s, layout, { ...opt, inset: inset.box }) : base;
        expectClean(v);
        if (inset && v.callout.on) {
          const c = { x1: v.callout.x, y1: v.callout.y, x2: v.callout.x + CALLOUT_W, y2: v.callout.y + CALLOUT_H };
          expect(hits(c, inset.box), '콜아웃과 작은 방').toBe(false);
        }
      });
    }
  }
});

describe('stageView 이벤트 흐름', () => {
  it('FR12B 판정 전: 호스트→서버 선과 요청자 신호, 서버 랙은 판정 게이지, 콜아웃 없음', () => {
    const v = stageView(stateAt(sec('14:28:10.05')), LAYOUT, { panelOpen: false });
    expect(v).toMatchObject({ scene: 'judging', mcp: true, reply: false, server: true, uplink: true, rssiOn: true, cmd: null });
    expect(v.judge).toMatchObject({ mark: '판정 중', tone: 'accent', gauge: true });
    expect(v.callout.on).toBe(false);
  });
  it('FR12B 명령을 보내면 명령 선, 결과가 오고 응답하면 호스트로 돌아가는 선', () => {
    expect(stageView(SCENES.flow(), LAYOUT, { panelOpen: false })).toMatchObject({ cmd: 'locker-1', mcp: true, reply: false });
    expect(stageView(SCENES.done(), LAYOUT, { panelOpen: false })).toMatchObject({ cmd: null, mcp: false, reply: true });
  });
  it('FR12B 끝난 요청이 내려가면 대기 화면으로 돌아간다', () => {
    const v = stageView(stateAt(sec('14:28:40')), LAYOUT, { panelOpen: false });
    expect(v).toMatchObject({ scene: 'idle', mcp: false, reply: false });
  });
  it('FR12B 승인 신청 만료: 소유자 알림 선을 끄고 만료를 말한다', () => {
    const v = stageView(stateAt(sec('14:41:01')), LAYOUT, { panelOpen: false });
    expect(v).toMatchObject({ scene: 'approval', push: false, owner: false });
    expect(v.callout.title).toBe('승인 신청 만료');
  });
});

describe('stageView 승인 신청의 끝과 실행 거절', () => {
  /** 승인 대기 장면에서 신청이 끝난 상태 */
  const ended = (accessEnd?: 'approved' | 'denied' | 'expired') => {
    const s = SCENES.approval();
    s.request = { ...s.request!, accessExpiredAt: '14:31:30', ...(accessEnd ? { accessEnd } : {}) };
    s.ownerNotified = false;
    return s;
  };
  const rejected = () => {
    const s = SCENES.flow();
    s.request = { ...s.request!, execution: 'rejected' };
    return s;
  };

  it('FR12B 승인 신청이 끝난 까닭마다 콜아웃이 다르다: 승인은 다시 요청하면 실행, 거절은 실행 안 함, 만료는 소유자 응답 없음', () => {
    for (const [accessEnd, title, body] of [
      ['approved', '소유자가 승인', '다시 요청하면 실행'],
      ['denied', '소유자가 거절', '실행 안 함'],
      ['expired', '승인 신청 만료', '소유자 응답 없음'],
      // 끝난 까닭을 모르는 요청(조회 REST)은 만료로 그린다
      [undefined, '승인 신청 만료', '소유자 응답 없음']
    ] as const) {
      const v = stageView(ended(accessEnd), LAYOUT, { panelOpen: false });
      expect(v, String(accessEnd)).toMatchObject({ scene: 'approval', push: false, owner: false });
      expect(v.callout, String(accessEnd)).toMatchObject({ on: true, tone: 'appr', title, body });
    }
  });
  it('FR12B 승인·거절로 끝난 장면의 화면 읽기 문구는 소유자 승인이 필요하다고 말하지 않는다', () => {
    const aria = (accessEnd: 'approved' | 'denied') => stageView(ended(accessEnd), LAYOUT, { panelOpen: false }).aria;
    expect(aria('approved')).toBe('방문자 B의 조명 켜기 요청: 소유자가 승인, 다시 요청하면 실행');
    expect(aria('denied')).toBe('방문자 B의 조명 켜기 요청: 소유자가 거절, 실행 안 함');
  });

  it('FR12B 기기가 실행 전에 거절하면 고장 격리가 아니다: 격리 선과 명령 선이 없고 기기 위에 거절 콜아웃(바쁨 색)을 둔다', () => {
    const v = stageView(rejected(), LAYOUT, { panelOpen: false });
    expect(v).toMatchObject({ scene: 'flow', fault: null, cmd: null });
    expect(v.callout).toMatchObject({ on: true, tone: 'busy', title: '사물함 1 거절', body: '실행 안 함 · 격리 안 함' });
    // 명령을 보내던 때와 같은 기기 앞을 가리킨다
    const sending = stageView(SCENES.flow(), LAYOUT, { panelOpen: false }).callout;
    expect([v.callout.ax, v.callout.ay]).toEqual([sending.ax, sending.ay]);
    // 서버의 판정(허가)은 그대로다
    expect(v.judge).toMatchObject({ mark: '허가', tone: 'ok' });
    expect(v.aria).toBe('사물함 1이 실행 전에 거절해 격리하지 않음');
    expect(JSON.stringify(v)).not.toContain('고장 격리');
  });
  it('FR12B 실행이 끝난 요청이라 거절도 완료처럼 요청자 신호와 신호 세기를 끈다', () => {
    expect(stageView(SCENES.flow(), LAYOUT, { panelOpen: false })).toMatchObject({ rssiOn: true });
    for (const execution of ['succeeded', 'rejected'] as const) {
      const s = SCENES.flow();
      s.request = { ...s.request!, execution };
      const v = stageView(s, LAYOUT, { panelOpen: false });
      expect(v.rssiOn, execution).toBe(false);
      expect(v.signals.some((x) => x.on), execution).toBe(false);
    }
  });
  it('FR12B 실행 결과마다 장면이 갈린다: 실패와 결과 불명은 고장 격리, 완료와 거절은 격리가 아니다', () => {
    const scene = (execution: 'succeeded' | 'failed' | 'unknown' | 'rejected') => {
      const s = SCENES.flow();
      s.request = { ...s.request!, execution };
      const v = stageView(s, LAYOUT, { panelOpen: false });
      return [v.scene, v.fault];
    };
    expect(scene('failed')).toEqual(['fault', 'locker-1']);
    expect(scene('unknown')).toEqual(['fault', 'locker-1']);
    expect(scene('succeeded')).toEqual(['flow', null]);
    expect(scene('rejected')).toEqual(['flow', null]);
  });
});

// 주 장소가 아닌 장소의 사람과 요청: 작은 방에 그리고, 콜아웃은 작은 방을 가리킨다
describe('stageView 두 장소', () => {
  const layoutOf = (id: string) => {
    const place = PLACES.find((p) => p.placeId === id)!;
    return withArrangement(place, resolveArrangement(place.devices), WEAK_RSSI_DBM);
  };
  /** App과 같은 순서로 그린다: 큰 무대 → 작은 방 → 작은 방을 피한 큰 무대 */
  function draw(s: MonitorState, lock?: string, panelOpen = false) {
    const focus = nextFocus(undefined, s, PLACES, lock);
    const opt = { panelOpen, places: PLACES };
    const primary = layoutOf(focus.primary!);
    const base = stageView(s, primary, opt);
    const inset = focus.inset ? insetView(s, layoutOf(focus.inset), base, opt) : undefined;
    return { focus, inset, v: inset ? stageView(s, primary, { ...opt, inset: inset.box }) : base };
  }
  const calloutBox = (v: ReturnType<typeof stageView>): Rect => ({ x1: v.callout.x, y1: v.callout.y, x2: v.callout.x + CALLOUT_W, y2: v.callout.y + CALLOUT_H });

  for (const [scene, make] of Object.entries(SCENES)) {
    for (const lock of [undefined, 'lounge', 'side-room']) {
      it(`FR12B ${scene} 장면 · ${lock ?? '자동'}: 콜아웃이 작은 방과 겹치지 않고 어떤 장면도 예외 없이 그려진다`, () => {
        const { inset, v } = draw(make(), lock);
        if (inset && v.callout.on) expect(hits(calloutBox(v), inset.box)).toBe(false);
        expect(v.placeId).toBe(lock ?? v.placeId);
      });
    }
  }

  it('FR12B 라운지 고정 중 옆방 요청: 호스트·서버 선과 서버 랙 판정은 그리고, 명령 선은 그리지 않는다', () => {
    const { focus, inset, v } = draw(SCENES['side-room'](), 'lounge');
    expect(focus).toMatchObject({ primary: 'lounge', inset: 'side-room' });
    expect(inset!.placeId).toBe('side-room');
    expect(inset!.people.map((p) => p.letter)).toEqual(['A']);
    expect(v).toMatchObject({ placeId: 'lounge', scene: 'flow', mcp: true, server: true, cmd: null, fault: null });
    expect(v.judge).toMatchObject({ mark: '허가', tone: 'ok' });
    // 무드등은 라운지에 없지만 이름과 공용형 판정은 모든 장소에서 찾는다
    expect(v.judge.checks).toEqual([['pass', '누구인지'], ['pass', '무드등 앞'], ['pass', '정책 통과'], ['none', '공용형 생략']]);
    // 옆방에 있는 요청자는 라운지에 세우지 않고, 신호 선도 그리지 않는다
    expect(v.visitors.find((x) => x.personId === 'p-a')!.on).toBe(false);
    expect(v.signals.some((x) => x.on)).toBe(false);
    expect(v.uplink).toBe(false);
  });

  it('FR12B 라운지 고정 중 옆방 요청: 콜아웃은 작은 방 윗변 가운데를 가리킨다', () => {
    const { inset, v } = draw(SCENES['side-room'](), 'lounge');
    expect(v.callout).toMatchObject({ on: true, tone: 'ok', title: '무드등에 켜기 명령' });
    expect(v.callout.ax).toBeCloseTo((inset!.box.x1 + inset!.box.x2) / 2, 5);
    expect(v.callout.ay).toBe(inset!.box.y1);
    expect(hits(calloutBox(v), inset!.box)).toBe(false);
  });

  it('FR12B 작은 방이 없으면 콜아웃은 서버 랙을 가리킨다', () => {
    const s = SCENES['side-room']();
    const v = stageView(s, layoutOf('lounge'), { panelOpen: false, places: PLACES });
    expect(v.cmd).toBeNull();
    expect(v.callout).toMatchObject({ on: true, title: '무드등에 켜기 명령', ax: NODE_ANCHOR.server.x, ay: NODE_ANCHOR.server.y });
  });

  it('FR12B 다른 장소의 기기에 온 요청은 어떤 판정이어도 예외 없이 콜아웃을 단다', () => {
    const base = SCENES['side-room']();
    const r = base.request!;
    const variants: [string, NonNullable<MonitorState['request']>][] = [
      ['허가', { ...r, execution: undefined }],
      ['완료', { ...r, execution: 'succeeded' }],
      ['실패 격리', { ...r, execution: 'failed' }],
      ['불명 격리', { ...r, execution: 'unknown' }],
      ['승인 필요', { ...r, verdict: 'NEED_APPROVAL', reason: 'APPROVAL_REQUIRED', execution: undefined }],
      ['바쁨', { ...r, verdict: 'BUSY', reason: 'DEVICE_BUSY', execution: undefined }],
      ['거부', { ...r, verdict: 'DENY', reason: 'POLICY_DENIED', execution: undefined }],
      ['되물음', { ...r, verdict: 'ASK', reason: 'LOW_CONFIDENCE', nodeId: undefined, execution: undefined, posteriors: { 'mood-1': 0.6 } }]
    ];
    for (const [name, request] of variants) {
      const { inset, v } = draw({ ...base, request }, 'lounge');
      expect(v.callout.on, name).toBe(true);
      expect(v.cmd, name).toBeNull();
      expect(v.fault, name).toBeNull();
      expect(v.callout.ay, name).toBe(inset!.box.y1);
    }
  });

  it('FR12B 판정 중인 옆방 요청도 라운지에서는 호스트→서버 선과 판정 게이지만 그린다', () => {
    const { v } = draw(stateAt(sec('14:43:48.05')), 'lounge');
    expect(v).toMatchObject({ scene: 'judging', mcp: true, server: true, cmd: null, uplink: false });
    expect(v.judge).toMatchObject({ mark: '판정 중', gauge: true });
    expect(v.signals.some((x) => x.on)).toBe(false);
  });

  const lit = (v: ReturnType<typeof stageView>) => v.devices.filter((d) => !d.dim).map((d) => d.nodeId);
  /** 요청자의 비콘을 끈다: 요청 장소를 정할 수 없는 원격 요청이 된다 */
  const withoutBeacon = (s: MonitorState, personId = 'p-a'): MonitorState =>
    ({ ...s, people: s.people.map((p) => (p.id === personId ? { ...p, beaconSeen: false, rssi: {}, near: undefined } : p)) });

  it('FR12B 라운지 고정 중 옆방 요청이 기기를 고르기 전(판정 중)이면 라운지 기기를 흐리지 않고, 패널이 열려도 방을 밀지 않는다', () => {
    for (const panelOpen of [false, true]) {
      const { v } = draw(stateAt(sec('14:43:48.05')), 'lounge', panelOpen);
      expect(v.scene, `패널 ${panelOpen}`).toBe('judging');
      expect(v.devices.filter((d) => d.dim).map((d) => d.nodeId), `패널 ${panelOpen} 흐린 기기`).toEqual([]);
      expect(v.shift, `패널 ${panelOpen} 방 밀기`).toBe(0);
    }
  });

  it('FR12B 라운지 고정 중 옆방 요청이 ASK여도 라운지 기기를 흐리지 않고, 패널이 열려도 방을 밀지 않는다', () => {
    const base = SCENES['side-room']();
    const request: NonNullable<MonitorState['request']> = { ...base.request!, verdict: 'ASK', reason: 'LOW_CONFIDENCE', nodeId: undefined, execution: undefined, posteriors: { 'mood-1': 0.6 } };
    for (const panelOpen of [false, true]) {
      const { v } = draw({ ...base, request }, 'lounge', panelOpen);
      expect(v.scene, `패널 ${panelOpen}`).toBe('ask');
      expect(v.devices.filter((d) => d.dim).map((d) => d.nodeId), `패널 ${panelOpen} 흐린 기기`).toEqual([]);
      expect(v.shift, `패널 ${panelOpen} 방 밀기`).toBe(0);
    }
  });

  it('FR12B 요청 장소가 이 장소이거나 정할 수 없으면(원격) 요청한 기능의 기기만 남기고 흐리고, 패널이 열리면 방을 민다', () => {
    const judging = stateAt(sec('14:28:10.05'));
    // 요청자가 라운지에 있다
    expect(lit(draw(judging, 'lounge').v)).toEqual(['locker-1', 'locker-2', 'locker-3']);
    // 요청자의 비콘이 없다
    const remote = withoutBeacon(judging);
    expect(lit(draw(remote, 'lounge').v)).toEqual(['locker-1', 'locker-2', 'locker-3']);
    expect(lit(stageView(remote, layoutOf('lounge'), { panelOpen: false })), '장소 목록 없이').toEqual(['locker-1', 'locker-2', 'locker-3']);
    // 같은 요청에서 패널이 열리면 요청한 기능의 기기가 패널 왼쪽에 보이도록 방을 민다
    expect(draw(remote, 'lounge', true).v.shift).toBeGreaterThan(0);
  });

  it('FR12B 판정 전 비콘→서버 선은 늘 켜고, 요청한 사람이 다른 장소에 있을 때만 끈다', () => {
    const judging = stateAt(sec('14:43:48.05'));
    expect(draw(judging, 'side-room').v.uplink, '요청자가 있는 장소').toBe(true);
    expect(draw(judging, 'lounge').v.uplink, '요청자가 다른 장소').toBe(false);
    // 요청자의 비콘이 없으면(원격) 어느 장소에서도 켠다
    const remote = withoutBeacon(judging);
    expect(draw(remote, 'lounge').v.uplink, '원격 요청자 · 라운지').toBe(true);
    expect(draw(remote, 'side-room').v.uplink, '원격 요청자 · 옆방').toBe(true);
    // 장소가 하나인 화면(장소 목록 없음)에서도 켠다
    expect(stageView(withoutBeacon(stateAt(sec('14:28:10.05'))), LAYOUT, { panelOpen: false }).uplink, '장소가 하나').toBe(true);
  });

  it('FR12B 되묻는 중(ASK)에도 비콘→서버 선은 요청한 사람이 다른 장소에 있을 때만 끈다', () => {
    const base = SCENES['side-room']();
    const request: NonNullable<MonitorState['request']> = { ...base.request!, verdict: 'ASK', reason: 'LOW_CONFIDENCE', nodeId: undefined, execution: undefined, posteriors: { 'mood-1': 0.6 } };
    const ask = { ...base, request };
    expect(draw(ask, 'side-room').v).toMatchObject({ scene: 'ask', uplink: true });
    expect(draw(ask, 'lounge').v).toMatchObject({ scene: 'ask', uplink: false });
    expect(draw(withoutBeacon(ask), 'lounge').v).toMatchObject({ scene: 'ask', uplink: true });
  });

  it('FR12B 요청한 사람이 다른 장소에 있으면 이 장소 기기 옆에 그 사람의 신호 세기를 쓰지 않는다', () => {
    const base = stateAt(sec('14:43:48.05'));
    const people = base.people.map((p) => (p.id === base.request!.personId ? { ...p, rssi: { 'mood-1': -49, 'light-3': -86, 'speaker-1': -88 } } : p));
    const s = { ...base, people };
    expect(draw(s, 'lounge').v).toMatchObject({ rssiOn: false, uplink: false });
    expect(draw(s, 'side-room').v).toMatchObject({ rssiOn: true, uplink: true });
  });

  it('FR12B 다른 장소의 기기 앞에 있는 사람은 이 장소에 세우지 않는다 (신호가 이 장소의 기기에 잡혀도)', () => {
    const s = SCENES.idle();
    const people = s.people.map((p) => (p.id === 'p-a' ? { ...p, near: 'mood-1', rssi: { 'locker-1': -60, 'mood-1': -49 } } : p));
    const v = stageView({ ...s, people }, layoutOf('lounge'), { panelOpen: false, places: PLACES });
    expect(v.visitors.find((x) => x.personId === 'p-a')!.on).toBe(false);
  });

  it('FR12B 라운지에서 옆방으로 옮긴 사람은 라운지 무대에서 사라지고 옆방 무대에 나타난다', () => {
    const s = SCENES['side-room']();
    expect(draw(s, 'lounge').v.visitors.filter((x) => x.on).map((x) => x.letter)).toEqual(['B', 'C']);
    expect(draw(s, 'side-room').v.visitors.filter((x) => x.on).map((x) => x.letter)).toEqual(['A']);
  });
});

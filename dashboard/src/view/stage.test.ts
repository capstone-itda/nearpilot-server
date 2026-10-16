// FR-12B: 모니터 상태에서 무대 표시를 규칙으로 계산한다
import { describe, expect, it } from 'vitest';
import { LAYOUT } from '../data/mock/fixtures';
import { SCENES, sec, stateAt } from '../data/mock/story';
import { stageView } from './stage';
import { PLACE } from '../data/mock/fixtures';
import { resolveArrangement, withArrangement } from './arrange';
import { CALLOUT_H, CALLOUT_W, canPlaceAll, crosses, deviceTextBox, hits, labelBox, personBox, rssiBox } from './placement';

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
    expect(dev(v, 'locker-1')).toMatchObject({ status: '여는 중', code: 'RESERVED', tone: 'ok', dim: false });
    expect(v.devices.filter((d) => d.nodeId !== 'locker-1').every((d) => d.dim)).toBe(true);
    expect(dev(v, 'locker-3').status).toBe('사용 중 · 대여 초과');
    expect(v).toMatchObject({ host: 'claude', server: true, mcp: 'claude', cmd: 'locker-1', fault: null, push: false, rssiOn: true });
    expect(v.rssi['locker-1']).toBe('−52 dBm');
    expect(v.callout).toMatchObject({ on: true, tone: 'ok', title: '사물함 1에 열기 명령', body: '' });
    expect(v.judge).toEqual({ mark: '허가', tone: 'ok', gauge: false, checks: [['pass', '누구인지'], ['pass', '사물함 1 앞'], ['pass', '정책 통과'], ['pass', '비어 있음']] });
    expect(v.visitors.find((x) => x.personId === 'p-b')!.on).toBe(false);
  });

  it('FR12B 공용형 OK: 서버 상자의 빈자리 칸은 공용형 생략', () => {
    const s = SCENES.approval();
    s.request = { ...s.request!, verdict: 'OK', reason: 'OK', execution: 'sent', steps: s.request!.steps.map((x, i) => (i < 5 ? { result: 'pass' as const } : x)) };
    expect(stageView(s, LAYOUT, { panelOpen: false }).judge.checks[3]).toEqual(['none', '공용형 생략']);
  });

  it('FR12B 고장 격리: 격리 선과 빨간 콜아웃, 패널이 열려 있으면 콜아웃을 왼쪽에', () => {
    const v = stageView(SCENES.fault(), LAYOUT, { panelOpen: true });
    expect(v.scene).toBe('fault');
    expect(dev(v, 'locker-2')).toMatchObject({ status: '고장 격리', tone: 'deny', dim: false });
    expect(v).toMatchObject({ fault: 'locker-2', cmd: null, mcp: null });
    expect(v.callout).toMatchObject({ tone: 'deny', title: '사물함 2 격리', body: '열었는지 몰라 사용을 막음' });
    expect(v.callout.x).toBeLessThanOrEqual(440);
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
    expect(v).toMatchObject({ scene: 'approval', owner: true, push: true, host: 'chatgpt' });
    expect(dev(v, 'light-1')).toMatchObject({ tone: 'appr', status: '사용 가능', dim: false });
    expect(v.callout).toMatchObject({ tone: 'appr', title: '소유자에게 승인 요청', body: '' });
    expect(v.callout.x).toBeLessThanOrEqual(440);
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
    expect(v).toMatchObject({ scene: 'layout', host: null, server: false, uplink: false, rssiOn: false });
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
    expect(v).toMatchObject({ mcp: null, rssiOn: false, cmd: null });
    expect(v.callout.title).toBe('사물함 1 열기 완료');
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
  const ROOM_BOX = { x1: 330, y1: 90, x2: 1340, y2: 740 };
  const within = (a: { x1: number; y1: number; x2: number; y2: number }) => a.x1 >= ROOM_BOX.x1 && a.x2 <= ROOM_BOX.x2 && a.y1 >= ROOM_BOX.y1 && a.y2 <= ROOM_BOX.y2;

  for (const [arrName, saved] of Object.entries(ARRANGEMENTS)) {
    const arr = resolveArrangement(PLACE.devices, saved);
    it(`FR12A D4 ${arrName} 배치는 저장한 자리 그대로 허용된다`, () => expect(canPlaceAll(arr, saved)).toBe(true));
    const layout = withArrangement(PLACE, arr);
    for (const [scene, make] of Object.entries(SCENES)) {
      for (const panelOpen of [false, true]) {
        it(`FR12B D4 ${arrName} 배치 · ${scene} 장면 · 패널 ${panelOpen ? '열림' : '닫힘'}`, () => {
          const v = stageView(make(), layout, { panelOpen });
          const texts = v.devices.map(deviceTextBox);
          for (const p of v.visitors.filter((x) => x.on)) {
            expect(texts.some((t) => hits(personBox(p), t)), `${p.name} 점`).toBe(false);
            expect(texts.some((t) => hits(labelBox(p, p.label), t)), `${p.name} 이름표`).toBe(false);
            expect(within(labelBox(p, p.label)), `${p.name} 이름표가 방 안`).toBe(true);
          }
          if (v.callout.on) {
            const c = { x1: v.callout.x, y1: v.callout.y, x2: v.callout.x + CALLOUT_W, y2: v.callout.y + CALLOUT_H };
            expect(texts.some((t) => hits(c, t)), '콜아웃과 기기 글자').toBe(false);
            expect(within(c), '콜아웃이 방 안').toBe(true);
            const a = { x: v.callout.ax, y: v.callout.ay };
            const e = { x: v.callout.lx, y: v.callout.ly };
            if (v.callout.ax > ROOM_BOX.x1) expect(texts.some((t) => crosses(a, e, t)), '지시선과 기기 글자').toBe(false);
          }
          if (v.rssiOn) for (const d of v.devices) {
            expect(rssiBox(d).x1 >= ROOM_BOX.x1 - 12 || d.wall === 'bottom', `${d.name} 신호 글자가 왼쪽 벽 안`).toBe(true);
            expect(v.devices.some((o) => o !== d && hits(rssiBox(d), rssiBox(o))), `${d.name} 신호 글자끼리`).toBe(false);
          }
          expect(stageView(make(), layout, { panelOpen: true, panelWide: true }).callout.on, '넓은 패널이면 콜아웃 숨김').toBe(false);
        });
      }
    }
  }
});

describe('stageView 이벤트 흐름', () => {
  it('FR12B 판정 전: 호스트→서버 선과 요청자 신호, 서버 상자는 판정 게이지, 콜아웃 없음', () => {
    const v = stageView(stateAt(sec('14:28:10.05')), LAYOUT, { panelOpen: false });
    expect(v).toMatchObject({ scene: 'judging', mcp: 'claude', reply: null, server: true, uplink: true, rssiOn: true, cmd: null });
    expect(v.judge).toMatchObject({ mark: '판정 중', tone: 'accent', gauge: true });
    expect(v.callout.on).toBe(false);
  });
  it('FR12B 명령을 보내면 명령 선, 결과가 오고 응답하면 호스트로 돌아가는 선', () => {
    expect(stageView(SCENES.flow(), LAYOUT, { panelOpen: false })).toMatchObject({ cmd: 'locker-1', mcp: 'claude', reply: null });
    expect(stageView(SCENES.done(), LAYOUT, { panelOpen: false })).toMatchObject({ cmd: null, mcp: null, reply: 'claude' });
  });
  it('FR12B 끝난 요청이 내려가면 대기 화면으로 돌아간다', () => {
    const v = stageView(stateAt(sec('14:28:40')), LAYOUT, { panelOpen: false });
    expect(v).toMatchObject({ scene: 'idle', mcp: null, reply: null });
  });
  it('FR12B 승인 신청 만료: 소유자 알림 선을 끄고 만료를 말한다', () => {
    const v = stageView(stateAt(sec('14:41:01')), LAYOUT, { panelOpen: false });
    expect(v).toMatchObject({ scene: 'approval', push: false, owner: false });
    expect(v.callout.title).toBe('승인 신청 만료');
  });
});

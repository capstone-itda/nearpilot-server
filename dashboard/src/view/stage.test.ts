// FR-12B: 스냅숏에서 무대 표시를 규칙으로 계산한다
import { describe, expect, it } from 'vitest';
import { LAYOUT, SCENES } from '../data/mock/fixtures';
import { stageView } from './stage';

const dev = (v: ReturnType<typeof stageView>, id: string) => v.devices.find((d) => d.nodeId === id)!;

describe('stageView', () => {
  it('FR12B 대기: 비콘을 보내는 첫 방문자의 신호와 상향 흐름, 안내 콜아웃', () => {
    const v = stageView(SCENES.idle(), LAYOUT, { panelOpen: false });
    expect(v.scene).toBe('idle');
    expect(v.signals.find((s) => s.personId === 'p-a')!.on).toBe(true);
    expect(v.signals.find((s) => s.personId === 'p-b')!.on).toBe(false);
    expect(v.uplink).toBe(true);
    expect(v.callout).toMatchObject({ on: true, tone: 'accent', title: '폰이 비콘 신호를 보내는 중' });
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
    expect(v.callout).toMatchObject({ on: true, tone: 'ok', title: '사물함 1 열기 명령', body: '네 가지 확인을 모두 통과해서 open을 보냈어요' });
    expect(v.visitors.find((x) => x.personId === 'p-b')!.on).toBe(false);
  });

  it('FR12B 공용형 OK: 빈자리 확인이 없어 세 가지 확인이라고 말한다', () => {
    const s = SCENES.approval();
    s.request = { ...s.request!, verdict: 'OK', reason: 'OK', execution: 'sent', steps: s.request!.steps.map((x, i) => (i < 5 ? { result: 'pass' as const } : x)) };
    expect(stageView(s, LAYOUT, { panelOpen: false }).callout.body).toBe('세 가지 확인을 모두 통과해서 on을 보냈어요');
  });

  it('FR12B 고장 격리: 격리 선과 빨간 콜아웃, 패널이 열려 있으면 콜아웃을 왼쪽에', () => {
    const v = stageView(SCENES.fault(), LAYOUT, { panelOpen: true });
    expect(v.scene).toBe('fault');
    expect(dev(v, 'locker-2')).toMatchObject({ status: '고장 격리', tone: 'deny', dim: false });
    expect(v).toMatchObject({ fault: 'locker-2', cmd: null, mcp: null });
    expect(v.callout).toMatchObject({ tone: 'deny', title: '사물함 2 격리', body: '열었는지 알 수 없어서 다시 쓰지 않게 막았어요', x: 440 });
  });

  it('FR12B ASK: 요청한 기능의 기기만 남기고 방문자에게 콜아웃', () => {
    const v = stageView(SCENES.ask(), LAYOUT, { panelOpen: true });
    expect(v.devices.filter((d) => !d.dim).map((d) => d.nodeId)).toEqual(['locker-1', 'locker-2', 'locker-3']);
    expect(v.uplink).toBe(true);
    expect(v.callout).toMatchObject({ tone: 'ask', title: '어느 사물함 앞인지 애매해요', body: '사물함 1과 사물함 2 사이라서 다시 물어봐요' });
    expect(v.visitors.find((x) => x.personId === 'p-a')!.label).toBe('left');
  });

  it('FR12B 승인 대기: 소유자 폰 알림 선과 보라 콜아웃, 대상 조명은 승인 색', () => {
    const v = stageView(SCENES.approval(), LAYOUT, { panelOpen: true });
    expect(v).toMatchObject({ scene: 'approval', owner: true, push: true, host: 'chatgpt' });
    expect(dev(v, 'light-1')).toMatchObject({ tone: 'appr', status: '사용 가능', dim: false });
    expect(v.callout).toMatchObject({ tone: 'appr', title: '소유자에게 승인 요청', body: '조명 1은 소유자가 승인해야 켤 수 있어요', x: 345, y: 400 });
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

  it('D4 배치 점검: 좌표를 보이고 흐름은 숨긴다', () => {
    const v = stageView(SCENES.later(), LAYOUT, { panelOpen: false, layoutCheck: true });
    expect(v).toMatchObject({ scene: 'layout', coords: true, host: null, server: false, uplink: false });
    expect(v.callout.on).toBe(false);
  });

  it('D4 배치 좌표는 모델 파일의 m 값을 평면도 좌표로 바꾼다', () => {
    const v = stageView(SCENES.idle(), LAYOUT, { panelOpen: false });
    expect(dev(v, 'locker-1')).toMatchObject({ x: 450, y: 112, fx: 450, fy: 260 });
    expect(dev(v, 'light-3')).toMatchObject({ x: 805, y: 719, fx: 805, fy: 560 });
  });
});

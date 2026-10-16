// 목 어댑터: 기간 거르기와 한 번 재생
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { StreamEvent } from '../../model/events';
import { initialState, reduceAll } from '../reduce';
import { PLACE } from './fixtures';
import { PLAY_ORDER, SCENES, sec } from './story';
import { LIVE_START, createMockSource, liveClock } from './mockSource';

afterEach(() => { vi.useRealTimers(); });

describe('createMockSource', () => {
  it('FR12A 이력 기간에 시각 범위를 쓰면 그 안의 기록만 준다', async () => {
    const s = createMockSource({ scene: 'later' });
    expect((await s.history({ period: '오늘' })).length).toBe(6);
    expect((await s.history({ period: '오늘 14:26~14:29' })).map((h) => h.at)).toEqual(['14:29', '14:28', '14:26']);
  });
  it('FR12B 재생은 마지막 장면에서 멈춘다', () => {
    vi.useFakeTimers();
    const batches: StreamEvent[][] = [];
    const s = createMockSource({ scene: 'play', intervalMs: 10 });
    s.subscribe((x) => batches.push(x));
    vi.advanceTimersByTime(10 * PLAY_ORDER.length * 3);
    // 연결 직후 한 묶음 + 장면 사이마다 한 묶음
    expect(batches.length).toBe(PLAY_ORDER.length);
    const flat = batches.flat();
    expect(flat.some((e) => e.type === 'exec_result' && e.result === 'succeeded')).toBe(true);
    expect(reduceAll(initialState(PLACE), flat)).toEqual(SCENES[PLAY_ORDER.at(-1)!]());
  });
});

describe('목 조회는 마지막 이벤트 시각까지만 보인다', () => {
  it('FR12A 대기 장면(14:05)에는 그 뒤의 점유·감사·이력이 없다', async () => {
    const s = createMockSource({ scene: 'idle' });
    expect((await s.uses()).map((u) => u.nodeId)).toEqual(['locker-3']);
    expect((await s.uses())[0].overdueMinutes).toBeUndefined();
    expect(await s.audit()).toEqual([]);
    expect((await s.rental('locker-3')).timeline.at(-1)).toMatchObject({ text: '열림 · ACTIVE', now: true });
  });
  it('FR12A 연결 끊김 장면은 마지막 수신 뒤의 기록을 보이지 않는다', async () => {
    const s = createMockSource({ scene: 'offline' });
    expect((await s.audit()).every((a) => a.at <= '14:25:40')).toBe(true);
  });
  it('FR12A 진행 중인 예약은 아직 열리지 않았다', async () => {
    const s = createMockSource({ scene: 'flow' });
    expect((await s.rental('locker-1')).timeline.at(-1)).toMatchObject({ text: '예약 · RESERVED', now: true });
    expect((await s.uses()).find((u) => u.nodeId === 'locker-1')?.session).toBe('RESERVED');
  });
  it('FR12A 신뢰도 실시간 집계는 지금까지 보인 판정만 센다', async () => {
    const idle = (await createMockSource({ scene: 'idle' }).trust()).live;
    const later = (await createMockSource({ scene: 'later' }).trust()).live;
    expect(idle).toMatchObject({ decisions: 0, latencyMedianSec: null });
    expect(later.decisions).toBe((await createMockSource({ scene: 'later' }).history({ period: '오늘' })).length);
    expect(later.counts).toEqual({ OK: 2, ASK: 1, NEED_APPROVAL: 1, BUSY: 1, DENY: 1 });
  });
  it('FR12A 요청 상세는 진행 중이면 모니터 상태 그대로, 아직 생기지 않은 요청은 거절한다', async () => {
    const flow = createMockSource({ scene: 'flow' });
    expect(await flow.request('r-1428')).toEqual(SCENES.flow().request);
    await expect(flow.request('r-1430')).rejects.toThrow();
    await expect(createMockSource({ scene: 'idle' }).request('r-1426')).rejects.toThrow();
    expect((await createMockSource({ scene: 'later' }).request('r-1426')).id).toBe('r-1426');
  });
});

describe('live', () => {
  it('FR12B 요청 하나의 이벤트가 도착 순서대로, 한 묶음에 하나씩 느리게 온다', () => {
    vi.useFakeTimers();
    const batches: { t: number; types: string[] }[] = [];
    const s = createMockSource({ scene: 'live' });
    let t = 0;
    s.subscribe((x) => batches.push({ t, types: x.filter((e) => e.type !== 'rssi' && e.type !== 'mock_phone' && 'requestId' in e && e.requestId === 'r-1428').map((e) => e.type) }));
    for (; t < 120_000; t += 100) vi.advanceTimersByTime(100);
    const flow = batches.filter((b) => b.types.length);
    expect(flow.flatMap((b) => b.types)).toEqual(['mcp_call', 'step', 'step', 'step', 'posterior', 'step', 'step', 'step', 'step', 'verdict', 'command', 'exec_result', 'mcp_result']);
    // 0.2초 안의 단계들이 묶음 하나에 몰리지 않는다
    expect(flow.every((b) => b.types.length === 1)).toBe(true);
    const gaps = flow.slice(1).map((b, i) => b.t - flow[i].t);
    expect(Math.min(...gaps.slice(0, 9))).toBeGreaterThanOrEqual(700);
  });
  it('FR12B liveClock은 빠른 이벤트를 늘리고 긴 조용한 구간을 줄인다', () => {
    const clock = liveClock();
    expect(clock(0)).toBe(LIVE_START);
    // 14:26:12의 ASK 요청은 25초 안쪽에 시작한다 (실제로는 22초 뒤)
    let e = 0;
    while (clock(e) < sec('14:26:12')) e += 0.1;
    expect(e).toBeLessThan(25);
  });
  it('FR12B 연결 끊김에서 다시 연결하면 끊긴 동안의 이벤트와 연결 이벤트가 온다', () => {
    const s = createMockSource({ scene: 'offline' });
    const batches: StreamEvent[][] = [];
    s.subscribe((x) => batches.push(x));
    expect(batches[0].at(-1)).toMatchObject({ type: 'link', connected: false });
    s.reconnect();
    expect(batches[1].some((e) => e.type === 'mcp_call')).toBe(true);
    expect(batches[1].at(-1)).toMatchObject({ type: 'link', connected: true });
  });
});

describe('조회는 판정을 앞서가지 않는다', () => {
  it('FR12A 판정 중인 요청은 이력, 감사, 신뢰도 집계에 아직 없다', async () => {
    const judging = createMockSource({ scene: 'judging' });
    expect((await judging.history({ period: '오늘' })).map((h) => h.requestId)).not.toContain('r-1428');
    const ask = createMockSource({ scene: 'ask' });
    expect((await ask.history({ period: '오늘' })).map((h) => h.requestId)).toContain('r-1426');
    expect((await ask.audit()).map((a) => a.id)).toContain('a4');
  });
});

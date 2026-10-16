// 목 어댑터: 기간 거르기와 한 번 재생
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { Snapshot } from '../../model/types';
import { PLAY_ORDER, SCENES } from './fixtures';
import { createMockSource } from './mockSource';

afterEach(() => { vi.useRealTimers(); });

describe('createMockSource', () => {
  it('FR12A 이력 기간에 시각 범위를 쓰면 그 안의 기록만 준다', async () => {
    const s = createMockSource({ scene: 'later' });
    expect((await s.history({ period: '오늘' })).length).toBe(6);
    expect((await s.history({ period: '오늘 14:26~14:29' })).map((h) => h.at)).toEqual(['14:29', '14:28', '14:26']);
  });
  it('FR12B 재생은 마지막 장면에서 멈춘다', () => {
    vi.useFakeTimers();
    const seen: Snapshot[] = [];
    const s = createMockSource({ scene: 'play', intervalMs: 10 });
    s.subscribe((x) => seen.push(x));
    vi.advanceTimersByTime(10 * PLAY_ORDER.length * 3);
    expect(seen.length).toBe(PLAY_ORDER.length);
    expect(seen.some((x) => x.request?.execution === 'succeeded')).toBe(true);
    expect(seen.at(-1)).toEqual(SCENES[PLAY_ORDER.at(-1)!]());
  });
});

describe('목 조회는 지금 스냅숏 시각까지만 보인다', () => {
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
});

// 목 어댑터: 기간 거르기와 한 번 재생
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { Snapshot } from '../../model/types';
import { PLAY_ORDER, SCENES } from './fixtures';
import { createMockSource } from './mockSource';

afterEach(() => { vi.useRealTimers(); });

describe('createMockSource', () => {
  it('FR12A 이력 기간에 시각 범위를 쓰면 그 안의 기록만 준다', async () => {
    const s = createMockSource({ scene: 'idle' });
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

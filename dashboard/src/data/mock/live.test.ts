// 목 실시간 흐름: 신호는 계속 바뀌고, 판정과 실행 결과는 그 시각에 도착한다
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { Snapshot } from '../../model/types';
import { LIVE_LENGTH_SEC, liveSnapshot, liveTime, sec } from './live';
import { createMockSource } from './mockSource';

afterEach(() => { vi.useRealTimers(); });

const at = (elapsed: number) => liveSnapshot(liveTime(elapsed));

describe('목 실시간 흐름', () => {
  it('FR12B 장소 시각은 앞으로만 가고, 이야기가 끝나면 실제 속도로 간다', () => {
    let prev = -1;
    for (let e = 0; e <= LIVE_LENGTH_SEC + 10; e += 0.5) {
      const t = liveTime(e);
      expect(t).toBeGreaterThan(prev);
      prev = t;
    }
    expect(liveTime(LIVE_LENGTH_SEC + 10) - liveTime(LIVE_LENGTH_SEC)).toBeCloseTo(10);
  });
  it('FR12B 판정과 실행 결과가 시각 순서대로 도착한다', () => {
    const seen: string[] = [];
    for (let e = 0; e <= LIVE_LENGTH_SEC; e += 0.5) {
      const r = at(e).request;
      const key = r ? `${r.id}:${r.verdict}:${r.execution ?? '-'}` : '없음';
      if (seen.at(-1) !== key) seen.push(key);
    }
    expect(seen).toEqual([
      '없음', 'r-1426:ASK:-', 'r-1428:OK:sent', 'r-1428:OK:succeeded', 'r-1429:OK:sent', 'r-1429:OK:unknown', 'r-1430:NEED_APPROVAL:-'
    ]);
    expect(at(LIVE_LENGTH_SEC).ownerNotified).toBe(true);
  });
  it('FR12B 비콘 신호 세기는 판정이 없어도 계속 바뀐다', () => {
    const rssi = (e: number) => JSON.stringify(at(e).people.find((p) => p.id === 'p-a')!.rssi);
    const values = new Set([0, 0.5, 1, 1.5, 2, 2.5].map(rssi));
    expect(values.size).toBeGreaterThan(3);
    expect(rssi(1)).toBe(rssi(1));
  });
  it('FR12B 사람은 걸어온 뒤에 비콘이 잡히고, 요청 시각에는 판정이 고른 기기의 신호가 가장 세다', () => {
    expect(at(0).people.find((p) => p.id === 'p-b')!.beaconSeen).toBe(false);
    const strongest = (s: Snapshot, id: string) => Object.entries(s.people.find((p) => p.id === id)!.rssi).sort((a, b) => b[1] - a[1])[0][0];
    const when = (clock: string) => { let e = 0; while (liveTime(e) < sec(clock)) e += 0.1; return liveSnapshot(liveTime(e)); };
    expect(strongest(when('14:28:10'), 'p-a')).toBe('locker-1');
    expect(strongest(when('14:29:37'), 'p-b')).toBe('locker-2');
    expect(strongest(when('14:30:58'), 'p-b')).toBe('light-1');
  });
  it('FR12A 조회 결과도 흐르는 시각을 따른다', async () => {
    vi.useFakeTimers();
    const s = createMockSource({ scene: 'live', intervalMs: 500 });
    const got: Snapshot[] = [];
    const off = s.subscribe((x) => got.push(x));
    expect((await s.history({ period: '오늘' })).map((h) => h.requestId)).toEqual(['r-1422', 'r-1420']);
    vi.advanceTimersByTime(LIVE_LENGTH_SEC * 1000);
    expect((await s.history({ period: '오늘' })).length).toBe(6);
    expect(got.length).toBeGreaterThan(LIVE_LENGTH_SEC);
    off();
  });
  it('FR12B 이력에 결과가 오른 요청은 무대에서도 결과를 기다리지 않는다', async () => {
    vi.useFakeTimers();
    const s = createMockSource({ scene: 'live', intervalMs: 500 });
    let cur: Snapshot | undefined;
    const off = s.subscribe((x) => { cur = x; });
    for (let e = 0; e <= LIVE_LENGTH_SEC; e += 0.5) {
      const rows = await s.history({ period: '오늘' });
      const r = cur!.request;
      if (r && rows.some((h) => h.requestId === r.id)) expect(r.execution, `${cur!.lastReceivedAt} ${r.id}`).not.toBe('sent');
      vi.advanceTimersByTime(500);
    }
    off();
  });
  it('FR12B 서 있는 사람의 가장 센 기기는 흔들림으로 바뀌지 않는다', () => {
    const strongest = (e: number, id: string) => Object.entries(at(e).people.find((p) => p.id === id)!.rssi).sort((a, b) => b[1] - a[1])[0]?.[0];
    const tail = new Set<string | undefined>();
    for (let e = LIVE_LENGTH_SEC; e < LIVE_LENGTH_SEC + 120; e += 0.5) { tail.add(strongest(e, 'p-a')); tail.add(`B:${strongest(e, 'p-b')}`); }
    expect([...tail].sort()).toEqual(['B:light-1', 'locker-1']);
    const entry = new Set<string | undefined>();
    for (let e = 0; e < 2; e += 0.5) entry.add(strongest(e, 'p-a'));
    expect([...entry]).toEqual(['speaker-1']);
  });
  it('FR12A 기다리는 승인의 남은 시간은 줄고, 다 되면 대기 만료로 끝난다', async () => {
    vi.useFakeTimers();
    const s = createMockSource({ scene: 'live', intervalMs: 500 });
    const off = s.subscribe(() => {});
    vi.advanceTimersByTime(LIVE_LENGTH_SEC * 1000);
    expect((await s.approvals()).pending[0].remainingMinutes).toBe(9);
    vi.advanceTimersByTime(5 * 60 * 1000);
    expect((await s.approvals()).pending[0].remainingMinutes).toBe(4);
    vi.advanceTimersByTime(6 * 60 * 1000);
    const a = await s.approvals();
    expect(a.pending).toEqual([]);
    expect(a.ended[0]).toMatchObject({ nodeId: 'light-1', personId: 'p-b', endReason: 'APPROVAL_REQUEST_EXPIRED' });
    off();
  });
});

// 패널 조회는 판정 흐름이 바뀌면 다시 부르고, 다시 부르는 동안 지금 결과를 둔다
import { act, cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import { DataCtx, useQuery } from './context';
import { LAYOUT, SCENES } from './mock/fixtures';
import { createMockSource } from './mock/mockSource';
import type { Snapshot } from '../model/types';

afterEach(cleanup);

describe('useQuery', () => {
  it('FR12A 스냅숏의 판정 흐름이 바뀌면 다시 조회하고, 신호 세기만 바뀌면 다시 부르지 않는다', async () => {
    let calls = 0;
    let release: (v: number) => void = () => {};
    const fetch = () => new Promise<number>((res) => { calls += 1; release = res; });
    function Probe() { const q = useQuery(fetch, []); return <p>{q.data === undefined ? '없음' : `값 ${q.data}`}</p>; }
    const ui = (snapshot: Snapshot) => (
      <DataCtx.Provider value={{ source: createMockSource({ scene: 'idle' }), layout: LAYOUT, snapshot, personName: (id) => id, deviceName: (id) => id }}>
        <Probe />
      </DataCtx.Provider>
    );
    const idle = SCENES.idle();
    const r = render(ui(idle));
    await act(async () => release(1));
    expect(screen.getByText('값 1')).toBeTruthy();
    r.rerender(ui({ ...idle, people: idle.people.map((p) => ({ ...p, rssi: {} })) }));
    expect(calls).toBe(1);
    r.rerender(ui(SCENES.ask()));
    expect(calls).toBe(2);
    expect(screen.getByText('값 1')).toBeTruthy();
    await act(async () => release(2));
    expect(screen.getByText('값 2')).toBeTruthy();
  });
});

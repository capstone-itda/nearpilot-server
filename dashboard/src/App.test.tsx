// 장소 목록이 비어도 화면이 불러오는 중에 머물지 않고, 상단 바에서 고른 장소가 무대의 주 장소가 된다
import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, beforeAll, describe, expect, it } from 'vitest';
import { App } from './App';
import { createMockSource } from './data/mock/mockSource';
import type { DataSource } from './data/source';

afterEach(cleanup);
// jsdom에는 없는 크기 관찰자: 무대가 그림 영역 크기를 알릴 때 쓴다
beforeAll(() => { globalThis.ResizeObserver ??= class { observe() {} unobserve() {} disconnect() {} }; });

/** 장소가 없는 서버 */
const emptySource = (): DataSource => ({
  ...createMockSource({ scene: 'idle' }),
  places: () => Promise.resolve({ places: [], weakRssiDbm: -80 }),
  subscribe: (l) => { l([]); return () => undefined; }
});

const stage = (c: HTMLElement) => c.querySelector<HTMLElement>('[data-component="Stage"]')!;

describe('App', () => {
  it('FR12B 장소 목록이 비면 무대에 "장소 없음"을 보이고 상단 바, 레일, 폰 화면은 그대로 둔다', async () => {
    const { container } = render(<App source={emptySource()} />);
    expect(await screen.findByText('장소 없음')).toBeTruthy();
    expect(screen.queryByText('불러오는 중…')).toBeNull();
    for (const c of ['TopBar', 'FlowRail', 'PhoneMirror']) expect(container.querySelector(`[data-component="${c}"]`), c).not.toBeNull();
    // 고를 장소가 없으니 자동만 남고 누를 수 없다
    expect(within(container.querySelector<HTMLElement>('[data-component="TopBar"]')!).getByRole('button', { name: '자동' }).hasAttribute('disabled')).toBe(true);
  });

  it('FR12B 장소 목록이 비면 배치 화면도 불러오는 중에 머물지 않고 장소가 없다고 보인다', async () => {
    render(<App source={emptySource()} initialView="layout" />);
    expect(await screen.findByText('장소 없음')).toBeTruthy();
    expect(screen.queryByRole('tablist', { name: '장소' })).toBeNull();
  });

  it('FR12B 상단 바에서 장소를 고르면 그 장소로 고정하고, 작은 방을 누르면 그 장소로 고정하고, 자동을 누르면 푼다', async () => {
    const { container } = render(<App source={createMockSource({ scene: 'side-room' })} />);
    const bar = within(await screen.findByRole('banner'));
    // 옆방 장면: 요청이 옆방이라 주 장소는 옆방이고 라운지는 작은 방이다
    expect(stage(container)).toMatchObject({ dataset: { place: 'side-room', locked: 'false' } });
    expect(container.querySelector('[data-component="StageInset"]')!.getAttribute('data-place')).toBe('lounge');
    expect(bar.getByRole('button', { name: '자동' }).getAttribute('aria-pressed')).toBe('true');
    expect(bar.getByRole('button', { name: '옆방' }).getAttribute('aria-current')).toBe('true');

    fireEvent.click(bar.getByRole('button', { name: '공용 라운지' }));
    expect(stage(container)).toMatchObject({ dataset: { place: 'lounge', locked: 'true' } });
    expect(bar.getByRole('button', { name: '공용 라운지' }).getAttribute('aria-pressed')).toBe('true');
    expect(bar.getByRole('button', { name: '자동' }).getAttribute('aria-pressed')).toBe('false');
    // 라운지에 사람이 있어도 옆방에 있는 요청자가 작은 방으로 보인다
    expect(container.querySelector('[data-component="StageInset"]:not([data-leaving="true"])')!.getAttribute('data-place')).toBe('side-room');

    fireEvent.click(container.querySelector('[data-component="StageInset"]:not([data-leaving="true"])')!);
    expect(stage(container)).toMatchObject({ dataset: { place: 'side-room', locked: 'true' } });
    expect(bar.getByRole('button', { name: '옆방' }).getAttribute('aria-pressed')).toBe('true');

    // 자동으로 풀면 사람 이동과 요청이 다시 주 장소를 고른다 (옆방 요청이 진행 중이라 옆방 그대로)
    fireEvent.click(bar.getByRole('button', { name: '자동' }));
    expect(stage(container)).toMatchObject({ dataset: { place: 'side-room', locked: 'false' } });
    await act(async () => undefined);
  });
});

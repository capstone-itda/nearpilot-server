// 상단 바 장소 고르기: 자동과 장소 이름 버튼, 고정 상태는 aria-pressed와 data-locked로 보인다
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { TopBar, type TopBarProps } from './TopBar';

afterEach(cleanup);

const PLACES = [{ placeId: 'lounge', placeName: '공용 라운지' }, { placeId: 'side-room', placeName: '옆방' }];
const bar = (p: Partial<TopBarProps> = {}) => render(
  <TopBar theme="dark" live receivedAt="14:00:00" places={PLACES} current="lounge" onLock={() => undefined} onToggleTheme={() => undefined} {...p} />
);
const chooser = (c: HTMLElement) => c.querySelector<HTMLElement>('[data-component="PlaceChooser"]')!;

describe('TopBar 장소 고르기', () => {
  it('FR12B 자동과 장소 이름이 버튼이고, 자동이면 자동이 눌려 있고 지금 장소를 표시한다', () => {
    const { container } = bar();
    expect(screen.getAllByRole('button').filter((b) => chooser(container).contains(b)).map((b) => b.textContent)).toEqual(['자동', '공용 라운지', '옆방']);
    expect(chooser(container).dataset.locked).toBe('false');
    expect(screen.getByRole('button', { name: '자동' }).getAttribute('aria-pressed')).toBe('true');
    expect(screen.getByRole('button', { name: '공용 라운지' }).getAttribute('aria-pressed')).toBe('false');
    expect(screen.getByRole('button', { name: '공용 라운지' }).getAttribute('aria-current')).toBe('true');
    expect(screen.getByRole('button', { name: '옆방' }).hasAttribute('aria-current')).toBe(false);
  });

  it('FR12B 고정하면 그 장소가 눌려 있고 data-locked가 켜진다', () => {
    const { container } = bar({ locked: 'side-room', current: 'side-room' });
    expect(chooser(container).dataset.locked).toBe('true');
    expect(screen.getByRole('button', { name: '옆방' }).getAttribute('aria-pressed')).toBe('true');
    expect(screen.getByRole('button', { name: '자동' }).getAttribute('aria-pressed')).toBe('false');
  });

  it('FR12B 장소 버튼은 그 장소로 고정하고 자동은 푼다', () => {
    const onLock = vi.fn();
    bar({ onLock });
    fireEvent.click(screen.getByRole('button', { name: '옆방' }));
    fireEvent.click(screen.getByRole('button', { name: '자동' }));
    expect(onLock.mock.calls).toEqual([['side-room'], [undefined]]);
  });

  it('FR12B 장소가 없으면 자동만 남고 누를 수 없다', () => {
    bar({ places: [], current: undefined });
    expect(screen.getByRole('button', { name: '자동' }).hasAttribute('disabled')).toBe(true);
    expect(screen.queryByRole('button', { name: '옆방' })).toBeNull();
  });
});

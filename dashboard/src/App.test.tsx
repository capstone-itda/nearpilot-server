// 장소 목록이 비어도 화면이 불러오는 중에 머물지 않고, 상단 바에서 고른 장소가 무대의 주 장소가 된다
import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, beforeAll, describe, expect, it } from 'vitest';
import { App } from './App';
import { createMockSource } from './data/mock/mockSource';
import { LOUNGE, WEAK_RSSI_DBM } from './data/mock/fixtures';
import { rssiEvents, sec, storyBetween, storyUntil } from './data/mock/story';
import type { DataSource } from './data/source';
import type { StreamEvent } from './model/events';
import { resolveArrangement, withArrangement } from './view/arrange';

// 배치는 localStorage에 저장하므로 시험마다 비운다
afterEach(() => { cleanup(); localStorage.clear(); });
// jsdom에는 없는 크기 관찰자: 무대가 그림 영역 크기를 알릴 때 쓴다
beforeAll(() => { globalThis.ResizeObserver ??= class { observe() {} unobserve() {} disconnect() {} }; });

/** 장소가 없는 서버 */
const emptySource = (): DataSource => ({
  ...createMockSource({ scene: 'idle' }),
  places: () => Promise.resolve({ places: [], weakRssiDbm: -80 }),
  subscribe: (l) => { l([]); return () => undefined; }
});

/**
 * 이야기를 원하는 시각까지 직접 흘려 보내는 서버. 시각 `from`의 지금 상태로 시작하고, advance로 이야기 시각을 나아가며
 * 그 사이 사건과 그 시각의 신호 세기를 한 묶음으로 보낸다 (실제 시간을 기다리지 않는다).
 */
function storySource(from: string) {
  let listener: ((events: StreamEvent[]) => void) | undefined;
  let now = sec(from);
  const source: DataSource = {
    ...createMockSource({ scene: 'idle' }),
    subscribe: (l) => { listener = l; l(storyUntil(now)); return () => { listener = undefined; }; }
  };
  const advance = async (to: string) => {
    const t = sec(to);
    const events = [...storyBetween(now, t), ...rssiEvents(t)];
    now = t;
    await act(async () => listener!(events));
  };
  return { source, advance };
}

const stage = (c: HTMLElement) => c.querySelector<HTMLElement>('[data-component="Stage"]')!;
/** 무대에 그린 기기의 평면 자리 ("x y") */
const spotOf = (c: HTMLElement, name: string) =>
  [...c.querySelectorAll<SVGGElement>('[data-component="StageDevice"]')].find((g) => g.querySelector('.np-stage__label')?.textContent === name)?.getAttribute('data-spot');

/** 이전 버전이 장소 이름을 열쇠로 저장한 배치 파일 */
const LEGACY_KEY = 'nearpilot-arrangement:공용 라운지';
const MID2 = { 'locker-1': { x: 456, y: 432 }, 'locker-2': { x: 624, y: 432 }, 'locker-3': { x: 792, y: 432 } };
const arrangementFile = (place: string) => JSON.stringify({ kind: 'nearpilot-arrangement', version: 1, place, devices: MID2 });

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

  // 이전 주 장소를 App이 넘겨야 '옮긴 사람'과 '그대로'를 가린다. 스냅숏이 여러 번 바뀌는 흐름으로 본다.
  it('FR12B 사람이 옆방으로 옮기면 요청이 오기 전에도 주 장소가 옆방이 되고, 요청이 내려간 뒤에도 옆방에 머문다', async () => {
    const { source, advance } = storySource('14:43:30');
    const { container } = render(<App source={source} />);
    await screen.findByRole('banner');
    expect(stage(container)).toMatchObject({ dataset: { place: 'lounge', scene: 'idle' } });
    // A가 라운지를 떠나 비콘이 끊긴다 (14:43:40): 어디로 갔는지 아직 모른다
    await advance('14:43:42');
    expect(stage(container).dataset.place).toBe('lounge');
    // A가 옆방 무드등 앞에서 다시 잡힌다 (14:43:44): 요청(14:43:48)이 오기 전이다
    await advance('14:43:45');
    expect(stage(container)).toMatchObject({ dataset: { place: 'side-room', scene: 'idle' } });
    await advance('14:43:47');
    expect(stage(container)).toMatchObject({ dataset: { place: 'side-room', scene: 'idle' } });
    // 옆방 요청이 오고, 끝나서 화면에서 내려간 뒤에도 옮긴 사람이 없으니 그대로다
    await advance('14:43:49');
    expect(stage(container)).toMatchObject({ dataset: { place: 'side-room', scene: 'flow' } });
    await advance('14:44:10');
    expect(stage(container)).toMatchObject({ dataset: { place: 'side-room', scene: 'idle' } });
  });

  it('FR12B 라운지에 고정한 채 사람이 옆방으로 옮겨도 고정은 그대로이고, 자동으로 풀면 옮긴 사람의 장소로 간다', async () => {
    const { source, advance } = storySource('14:43:30');
    const { container } = render(<App source={source} />);
    const bar = within(await screen.findByRole('banner'));
    fireEvent.click(bar.getByRole('button', { name: '공용 라운지' }));
    await advance('14:43:42');
    await advance('14:43:47');
    // A가 옆방으로 옮겼지만 고정한 라운지를 크게 보이고, 옆방은 작은 방이다
    expect(stage(container)).toMatchObject({ dataset: { place: 'lounge', locked: 'true' } });
    expect(container.querySelector('[data-component="StageInset"]:not([data-leaving="true"])')!.getAttribute('data-place')).toBe('side-room');
    // 요청이 없는 채로 풀면 옮긴 사람의 장소다
    fireEvent.click(bar.getByRole('button', { name: '자동' }));
    expect(stage(container)).toMatchObject({ dataset: { place: 'side-room', locked: 'false' } });
  });

  it('FR12B 라운지가 주 장소여도 패널은 옆방 기기를 이름(무드등)으로 보인다', async () => {
    const { container } = render(<App source={createMockSource({ scene: 'side-room' })} />);
    const bar = within(await screen.findByRole('banner'));
    fireEvent.click(bar.getByRole('button', { name: '공용 라운지' }));
    expect(stage(container).dataset.place).toBe('lounge');
    // 옆방 요청의 상세: 명령 줄에 무드등이 나온다
    fireEvent.click(screen.getByRole('button', { name: '요청 상세' }));
    const panel = container.querySelector<HTMLElement>('[data-component="Panel"]')!;
    expect(panel.textContent).toContain('무드등 켜기');
    expect(panel.textContent).not.toContain('mood-1 켜기');
  });

  it('FR12A D4 placeId 배치가 없으면 이전 버전이 장소 이름 열쇠로 저장한 배치로 무대를 그린다', async () => {
    const def = withArrangement(LOUNGE, resolveArrangement(LOUNGE.devices), WEAK_RSSI_DBM).devices.find((d) => d.nodeId === 'locker-1')!.spot;
    expect(`${def.x} ${def.y}`).not.toBe('456 432');
    localStorage.setItem(LEGACY_KEY, arrangementFile('공용 라운지'));
    const { container } = render(<App source={createMockSource({ scene: 'idle' })} />);
    await screen.findByRole('banner');
    expect(spotOf(container, '사물함 1')).toBe('456 432');
    // 읽기만 한다: 새 열쇠는 사람이 배치를 바꿔 저장할 때 생긴다
    expect(localStorage.getItem('nearpilot-arrangement:lounge')).toBeNull();
  });

  it('FR12A D4 배치 화면의 장소 탭은 연 뒤에 무대의 장소가 바뀌어도 그대로다', async () => {
    const { source, advance } = storySource('14:43:00');
    const { container } = render(<App source={source} initialView="layout" />);
    const tabs = await screen.findByRole('tablist', { name: '장소' });
    const selected = () => tabs.querySelector('[aria-selected="true"]')!.textContent;
    expect(selected()).toBe('공용 라운지');
    // A가 옆방으로 옮겨 무대의 주 장소는 옆방이 되지만, 보던 탭은 그대로다
    await advance('14:43:46');
    expect(selected()).toBe('공용 라운지');
    expect(container.querySelectorAll('[data-component="StageDevice"]').length).toBe(7);
  });

  it('FR12A D4 기본 배치로 되돌리면 이전 열쇠의 배치도 지워 다시 열어도 되살아나지 않는다', async () => {
    localStorage.setItem(LEGACY_KEY, arrangementFile('공용 라운지'));
    const first = render(<App source={createMockSource({ scene: 'idle' })} initialView="layout" />);
    await screen.findByRole('tablist', { name: '장소' });
    expect(spotOf(first.container, '사물함 1')).toBe('456 432');
    fireEvent.click(screen.getByRole('button', { name: '기본 배치로' }));
    expect(spotOf(first.container, '사물함 1')).not.toBe('456 432');
    expect(localStorage.getItem(LEGACY_KEY)).toBeNull();
    // 다시 연다
    first.unmount();
    const again = render(<App source={createMockSource({ scene: 'idle' })} initialView="layout" />);
    await screen.findByRole('tablist', { name: '장소' });
    expect(spotOf(again.container, '사물함 1')).not.toBe('456 432');
  });

  describe('배치 파일 불러오기', () => {
    /** 배치 화면에서 파일을 불러오고 결과 안내를 돌려준다 */
    async function importFile(place: string) {
      const { container } = render(<App source={createMockSource({ scene: 'idle' })} initialView="layout" />);
      await screen.findByRole('tablist', { name: '장소' });
      const file = new File([arrangementFile(place)], 'arrangement.json', { type: 'application/json' });
      // jsdom의 File에 text()가 없으면 채운다
      if (!file.text) Object.defineProperty(file, 'text', { value: () => Promise.resolve(arrangementFile(place)) });
      fireEvent.change(container.querySelector<HTMLInputElement>('input[type="file"]')!, { target: { files: [file] } });
      return (await screen.findByText(/자리를 불러왔어요/)).textContent;
    }

    it('FR12A D4 파일의 place가 placeId이거나 이전 버전의 장소 이름이면 같은 장소의 파일로 보고 안내하지 않는다', async () => {
      expect(await importFile('lounge')).toBe('기기 3대의 자리를 불러왔어요.');
      cleanup();
      expect(await importFile('공용 라운지')).toBe('기기 3대의 자리를 불러왔어요.');
    });

    it('FR12A D4 다른 장소의 파일이면 다른 장소라고 안내한다', async () => {
      expect(await importFile('옆방')).toContain('다른 장소(옆방)의 파일이라 이름이 같은 기기만 옮겼어요.');
    });
  });
});

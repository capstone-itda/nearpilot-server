import { afterEach, describe, expect, it, vi } from 'vitest';
import { DEFAULT_PREFS, clearArrangement, loadArrangement, loadMirrorPending, loadPrefs, saveArrangement, saveMirrorPending, savePrefs } from './prefs';

afterEach(() => { vi.restoreAllMocks(); localStorage.clear(); });

describe('prefs', () => {
  it('FR12A D7 저장한 테마와 자동 전환을 다시 읽는다', () => {
    savePrefs({ ...DEFAULT_PREFS, theme: 'light', follow: { followRequester: false, followApprover: true } });
    expect(loadPrefs()).toEqual({ ...DEFAULT_PREFS, theme: 'light', follow: { followRequester: false, followApprover: true } });
  });
  it('FR12B 무대 확대·이동과 영역 크기를 다시 읽는다. 잘못된 값은 버린다', () => {
    savePrefs({ ...DEFAULT_PREFS, stage: { k: 2, cx: 900, cy: 300 }, sizes: { phone: 320, rail: 200 } });
    expect(loadPrefs().stage).toEqual({ k: 2, cx: 900, cy: 300 });
    expect(loadPrefs().sizes).toEqual({ phone: 320, panel: undefined, wide: undefined, rail: 200 });
    localStorage.setItem('nearpilot-dashboard', JSON.stringify({ stage: { k: 'x' }, sizes: { phone: -5, panel: 'wide' } }));
    expect(loadPrefs().stage).toEqual(DEFAULT_PREFS.stage);
    expect(loadPrefs().sizes).toEqual({ phone: undefined, panel: undefined, wide: undefined, rail: undefined });
  });
  it('FR12A D7 미러링에 한 번 성공했는지와 권한 설정을 열고 기다리는 사람을 다시 읽는다', () => {
    savePrefs({ ...DEFAULT_PREFS, mirrorReady: true });
    expect(loadPrefs().mirrorReady).toBe(true);
    localStorage.setItem('nearpilot-dashboard', JSON.stringify({ mirrorReady: 'yes' }));
    expect(loadPrefs().mirrorReady).toBe(false);
    saveMirrorPending('visitor-a');
    expect(loadMirrorPending()).toBe('visitor-a');
    saveMirrorPending(null);
    expect(loadMirrorPending()).toBeNull();
  });
  it('FR12A D7 저장소를 쓸 수 없으면 기본값으로 동작한다', () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => { throw new Error('blocked'); });
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('blocked'); });
    expect(loadPrefs()).toEqual(DEFAULT_PREFS);
    expect(() => savePrefs(DEFAULT_PREFS)).not.toThrow();
  });
  it('FR12A D4 장소별 배치를 placeId 열쇠로 저장하고 다시 읽고 지운다. 저장소를 쓸 수 없으면 빈 배치다', () => {
    saveArrangement('lounge', { 'locker-1': { x: 456, y: 120 } });
    expect(loadArrangement('lounge')).toEqual({ 'locker-1': { x: 456, y: 120 } });
    expect(loadArrangement('side-room')).toEqual({});
    // 저장한 파일의 place는 placeId다
    expect(JSON.parse(localStorage.getItem('nearpilot-arrangement:lounge')!)).toMatchObject({ kind: 'nearpilot-arrangement', place: 'lounge' });
    clearArrangement('lounge');
    expect(loadArrangement('lounge')).toEqual({});
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => { throw new Error('blocked'); });
    expect(loadArrangement('lounge')).toEqual({});
  });
  it('FR12A D4 장소마다 배치를 따로 저장하고 한 장소를 지워도 다른 장소는 남는다', () => {
    saveArrangement('lounge', { 'locker-1': { x: 456, y: 120 } });
    saveArrangement('side-room', { 'mood-1': { x: 600, y: 720 } });
    expect(loadArrangement('lounge')).toEqual({ 'locker-1': { x: 456, y: 120 } });
    expect(loadArrangement('side-room')).toEqual({ 'mood-1': { x: 600, y: 720 } });
    clearArrangement('lounge');
    expect(loadArrangement('side-room')).toEqual({ 'mood-1': { x: 600, y: 720 } });
  });

  // 이전 버전은 장소 이름을 열쇠로 저장했다 (nearpilot-arrangement:공용 라운지)
  const LEGACY_KEY = 'nearpilot-arrangement:공용 라운지';
  const legacyFile = (x: number) => JSON.stringify({ kind: 'nearpilot-arrangement', version: 1, place: '공용 라운지', devices: { 'locker-1': { x, y: 120 } } });

  it('FR12A D4 placeId 배치가 없으면 장소 이름 열쇠로 저장한 이전 배치를 읽는다. 읽기만 하고 새 열쇠에는 쓰지 않는다', () => {
    localStorage.setItem(LEGACY_KEY, legacyFile(456));
    expect(loadArrangement('lounge', '공용 라운지')).toEqual({ 'locker-1': { x: 456, y: 120 } });
    // 장소 이름을 주지 않거나 다른 장소의 이름이면 이전 열쇠를 읽지 않는다
    expect(loadArrangement('lounge')).toEqual({});
    expect(loadArrangement('side-room', '옆방')).toEqual({});
    // 새 열쇠는 저장할 때 생긴다
    expect(localStorage.getItem('nearpilot-arrangement:lounge')).toBeNull();
    expect(localStorage.getItem(LEGACY_KEY)).toBe(legacyFile(456));
  });

  it('FR12A D4 placeId 배치가 있으면 이전 배치보다 우선한다', () => {
    localStorage.setItem(LEGACY_KEY, legacyFile(456));
    saveArrangement('lounge', { 'locker-1': { x: 600, y: 336 } });
    expect(loadArrangement('lounge', '공용 라운지')).toEqual({ 'locker-1': { x: 600, y: 336 } });
  });

  it('FR12A D4 배치를 지우면 이전 열쇠도 지워 다음에 열 때 지운 배치가 되살아나지 않는다', () => {
    localStorage.setItem(LEGACY_KEY, legacyFile(456));
    clearArrangement('lounge', '공용 라운지');
    expect(localStorage.getItem(LEGACY_KEY)).toBeNull();
    expect(loadArrangement('lounge', '공용 라운지')).toEqual({});
  });
});

import { afterEach, describe, expect, it, vi } from 'vitest';
import { DEFAULT_PREFS, clearArrangement, loadArrangement, loadPrefs, saveArrangement, savePrefs } from './prefs';

afterEach(() => { vi.restoreAllMocks(); localStorage.clear(); });

describe('prefs', () => {
  it('FR12A D7 저장한 테마와 자동 전환을 다시 읽는다', () => {
    savePrefs({ theme: 'light', follow: { followRequester: false, followApprover: true } });
    expect(loadPrefs()).toEqual({ theme: 'light', follow: { followRequester: false, followApprover: true } });
  });
  it('FR12A D7 저장소를 쓸 수 없으면 기본값으로 동작한다', () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => { throw new Error('blocked'); });
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('blocked'); });
    expect(loadPrefs()).toEqual(DEFAULT_PREFS);
    expect(() => savePrefs(DEFAULT_PREFS)).not.toThrow();
  });
  it('FR12A D4 장소별 배치를 저장하고 다시 읽고 지운다. 저장소를 쓸 수 없으면 빈 배치다', () => {
    saveArrangement('공용 라운지', { 'locker-1': { x: 456, y: 120 } });
    expect(loadArrangement('공용 라운지')).toEqual({ 'locker-1': { x: 456, y: 120 } });
    expect(loadArrangement('다른 장소')).toEqual({});
    clearArrangement('공용 라운지');
    expect(loadArrangement('공용 라운지')).toEqual({});
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => { throw new Error('blocked'); });
    expect(loadArrangement('공용 라운지')).toEqual({});
  });
});

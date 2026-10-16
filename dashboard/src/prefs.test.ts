import { afterEach, describe, expect, it, vi } from 'vitest';
import { DEFAULT_PREFS, loadPrefs, savePrefs } from './prefs';

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
});

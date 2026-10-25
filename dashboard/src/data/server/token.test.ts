import { afterEach, describe, expect, it, vi } from 'vitest';
import { clearToken, loadToken, saveToken } from './token';

afterEach(() => { vi.restoreAllMocks(); clearToken(); sessionStorage.clear(); localStorage.clear(); });

describe('서버 어댑터 토큰', () => {
  it('FR12A 토큰이 없으면 null이다', () => {
    expect(loadToken()).toBeNull();
  });
  it('FR12A 저장한 토큰을 이 탭의 세션 저장소에서 다시 읽는다. 장기 저장소에는 두지 않는다', () => {
    saveToken('np_owner_secret');
    expect(loadToken()).toBe('np_owner_secret');
    expect(sessionStorage.length).toBe(1);
    expect(sessionStorage.getItem(sessionStorage.key(0)!)).toBe('np_owner_secret');
    expect(localStorage.length).toBe(0);
  });
  it('FR12A 새로 저장하면 앞의 토큰을 바꾼다', () => {
    saveToken('first');
    saveToken('second');
    expect(loadToken()).toBe('second');
    expect(sessionStorage.length).toBe(1);
  });
  it('FR12A 지우면 다시 읽을 수 없다', () => {
    saveToken('np_owner_secret');
    clearToken();
    expect(loadToken()).toBeNull();
    expect(sessionStorage.length).toBe(0);
  });
  it('FR12A 세션 저장소를 쓸 수 없으면 메모리에만 두고, 지우면 메모리도 비운다', () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => { throw new Error('blocked'); });
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('blocked'); });
    vi.spyOn(Storage.prototype, 'removeItem').mockImplementation(() => { throw new Error('blocked'); });
    expect(loadToken()).toBeNull();
    expect(() => saveToken('in-memory')).not.toThrow();
    expect(loadToken()).toBe('in-memory');
    expect(() => clearToken()).not.toThrow();
    expect(loadToken()).toBeNull();
  });
  it('FR12A 저장소가 막혀 메모리에 둔 뒤 저장소가 열려도 새로 저장한 토큰이 이긴다', () => {
    const set = vi.spyOn(Storage.prototype, 'setItem').mockImplementationOnce(() => { throw new Error('blocked'); });
    saveToken('stale');
    expect(loadToken()).toBe('stale');
    set.mockRestore();
    saveToken('fresh');
    expect(loadToken()).toBe('fresh');
  });
});

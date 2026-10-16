// 이 PC의 표시 설정. 브라우저 저장소를 쓸 수 없으면 기본값으로 동작한다.
import type { FollowSettings } from './view/phone';

export interface Prefs { theme: 'dark' | 'light'; follow: FollowSettings }
const KEY = 'nearpilot-dashboard';
export const DEFAULT_PREFS: Prefs = { theme: 'dark', follow: { followRequester: true, followApprover: true } };

export function loadPrefs(): Prefs {
  try {
    const raw = localStorage.getItem(KEY);
    if (!raw) return DEFAULT_PREFS;
    const p = JSON.parse(raw) as Partial<Prefs>;
    return {
      theme: p.theme === 'light' ? 'light' : 'dark',
      follow: { ...DEFAULT_PREFS.follow, ...(p.follow ?? {}) }
    };
  } catch {
    return DEFAULT_PREFS;
  }
}

export function savePrefs(p: Prefs): void {
  try {
    localStorage.setItem(KEY, JSON.stringify(p));
  } catch {
    // 저장하지 못해도 화면은 그대로 동작한다
  }
}

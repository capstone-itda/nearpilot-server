// 이 PC의 표시 설정. 브라우저 저장소를 쓸 수 없으면 기본값으로 동작한다.
import { parseFile, toFile, type Arrangement } from './view/arrange';
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

/** 장소별 표시 배치. 파일 내보내기와 같은 형식으로 둔다. */
const arrangementKey = (place: string) => `nearpilot-arrangement:${place}`;

export function loadArrangement(place: string): Arrangement {
  try {
    const raw = localStorage.getItem(arrangementKey(place));
    if (!raw) return {};
    const f = parseFile(raw);
    return f.ok ? f.devices : {};
  } catch {
    return {};
  }
}

export function saveArrangement(place: string, arr: Arrangement): void {
  try {
    localStorage.setItem(arrangementKey(place), JSON.stringify(toFile(place, arr)));
  } catch {
    // 저장하지 못해도 이번 화면에서는 바꾼 배치를 쓴다
  }
}

export function clearArrangement(place: string): void {
  try {
    localStorage.removeItem(arrangementKey(place));
  } catch {
    // 지우지 못해도 이번 화면은 기본 배치로 바뀐다
  }
}

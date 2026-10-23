// 이 PC의 표시 설정. 브라우저 저장소를 쓸 수 없으면 기본값으로 동작한다.
import { parseFile, toFile, type Arrangement } from './view/arrange';
import type { FollowSettings } from './view/phone';
import { FIT, clampView, type ZoomView } from './view/zoom';

/** 사람이 끌어 바꾼 영역 크기(px). 없으면 기본 크기. */
export interface Sizes { phone?: number; panel?: number; wide?: number; rail?: number }
/** mirrorReady: 이 PC에서 미러링 창 공유에 한 번 성공했다. 그 전에는 macOS에서 권한 안내를 먼저 띄운다. */
export interface Prefs { theme: 'dark' | 'light'; follow: FollowSettings; stage: ZoomView; sizes: Sizes; mirrorReady: boolean }
const KEY = 'nearpilot-dashboard';
export const DEFAULT_PREFS: Prefs = { theme: 'dark', follow: { followRequester: true, followApprover: true }, stage: FIT, sizes: {}, mirrorReady: false };

const num = (v: unknown) => (typeof v === 'number' && Number.isFinite(v) && v > 0 ? v : undefined);

export function loadPrefs(): Prefs {
  try {
    const raw = localStorage.getItem(KEY);
    if (!raw) return DEFAULT_PREFS;
    const p = JSON.parse(raw) as Partial<Prefs>;
    return {
      theme: p.theme === 'light' ? 'light' : 'dark',
      follow: { ...DEFAULT_PREFS.follow, ...(p.follow ?? {}) },
      stage: p.stage && num(p.stage.k) && Number.isFinite(p.stage.cx) && Number.isFinite(p.stage.cy) ? clampView(p.stage) : FIT,
      sizes: { phone: num(p.sizes?.phone), panel: num(p.sizes?.panel), wide: num(p.sizes?.wide), rail: num(p.sizes?.rail) },
      mirrorReady: p.mirrorReady === true
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

/** 권한 설정을 열고 아직 창을 고르지 않은 사람. 권한을 켠 뒤 브라우저를 다시 열어도 안내를 이어서 띄운다. */
const PENDING = 'nearpilot-mirror-pending';

export function loadMirrorPending(): string | null {
  try {
    return localStorage.getItem(PENDING);
  } catch {
    return null;
  }
}

export function saveMirrorPending(personId: string | null): void {
  try {
    if (personId) localStorage.setItem(PENDING, personId);
    else localStorage.removeItem(PENDING);
  } catch {
    // 저장하지 못하면 브라우저를 다시 열었을 때 안내가 이어지지 않을 뿐이다
  }
}

/** 장소별 표시 배치. 파일 내보내기와 같은 형식으로 두고, 열쇠는 placeId다. */
const arrangementKey = (placeId: string) => `nearpilot-arrangement:${placeId}`;

export function loadArrangement(placeId: string): Arrangement {
  try {
    const raw = localStorage.getItem(arrangementKey(placeId));
    if (!raw) return {};
    const f = parseFile(raw);
    return f.ok ? f.devices : {};
  } catch {
    return {};
  }
}

export function saveArrangement(placeId: string, arr: Arrangement): void {
  try {
    localStorage.setItem(arrangementKey(placeId), JSON.stringify(toFile(placeId, arr)));
  } catch {
    // 저장하지 못해도 이번 화면에서는 바꾼 배치를 쓴다
  }
}

export function clearArrangement(placeId: string): void {
  try {
    localStorage.removeItem(arrangementKey(placeId));
  } catch {
    // 지우지 못해도 이번 화면은 기본 배치로 바뀐다
  }
}

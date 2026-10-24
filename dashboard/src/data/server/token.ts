// 소유자 토큰은 이 탭의 세션 저장소에만 둔다. 주소에 넣지 않는다 (방문 기록과 화면 공유에 남는다).
// 저장소를 쓸 수 없으면 메모리에만 두고, 탭을 닫거나 새로 고치면 다시 입력한다.
const KEY = 'nearpilot-owner-token';

/** 저장소에 쓰지 못했을 때만 쓴다 */
let memory: string | null = null;

export function loadToken(): string | null {
  if (memory !== null) return memory;
  try {
    return sessionStorage.getItem(KEY);
  } catch {
    return null;
  }
}

export function saveToken(token: string): void {
  memory = null;
  try {
    sessionStorage.setItem(KEY, token);
  } catch {
    memory = token;
  }
}

export function clearToken(): void {
  memory = null;
  try {
    sessionStorage.removeItem(KEY);
  } catch {
    // 저장소를 쓸 수 없으면 지울 값도 없다
  }
}

// 미러링 창 공유(getDisplayMedia)의 권한 안내와 실패 이유.
// 공유 창과 운영체제 권한 창은 브라우저와 운영체제가 띄운다. 페이지는 운영체제 권한을 미리 알 수 없고,
// 공유 창은 사람이 누른 순간에만 열 수 있다. 그래서 권한 안내는 공유 창을 열기 전에 페이지가 따로 띄운다.

/** cancel: 사람이 공유 창에서 취소함 · system: 운영체제가 막음 · capture: 고른 화면을 잡지 못함 · unsupported: 이 브라우저나 주소에서 쓸 수 없음 */
export type MirrorFailure = 'cancel' | 'system' | 'capture' | 'unsupported';
export type Platform = 'mac' | 'windows' | 'other';

export function mirrorFailure(err: unknown): MirrorFailure {
  const name = (err as { name?: string } | null)?.name ?? '';
  const message = (err as { message?: string } | null)?.message ?? '';
  // Chrome·Edge는 운영체제가 막으면 "Permission denied by system", 사람이 취소하면 "Permission denied"를 준다
  if (name === 'NotAllowedError') return /system/i.test(message) ? 'system' : 'cancel';
  if (name === 'NotFoundError' || name === 'NotReadableError' || name === 'AbortError') return 'capture';
  return 'unsupported';
}

export function platformOf(userAgent: string): Platform {
  if (/Mac OS X|Macintosh/.test(userAgent)) return 'mac';
  if (/Windows/.test(userAgent)) return 'windows';
  return 'other';
}

/** 공유 창을 열기 전에 권한 안내를 먼저 띄울까. macOS는 처음 공유할 때 화면 녹화 권한이 필요하고, 이 PC에서 한 번 성공했으면 권한이 있다고 본다. */
export const guideFirst = (p: Platform, mirrorReady: boolean) => p === 'mac' && !mirrorReady;

/** 운영체제의 화면 공유 권한 설정을 바로 여는 주소 */
export function settingsUrl(p: Platform): string | null {
  if (p === 'mac') return 'x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture';
  if (p === 'windows') return 'ms-settings:privacy';
  return null;
}

/** 권한 안내 창의 설명. retry면 공유했는데 운영체제가 막은 뒤다. */
export function guideText(p: Platform, retry: boolean): { title: string; detail: string } {
  const title = retry ? '아직 화면 공유 권한이 꺼져 있어요' : '화면 공유 권한이 필요해요';
  if (p === 'mac') return { title, detail: '폰 미러링 창을 보여 주려면 macOS에서 이 브라우저의 화면 녹화를 허락해야 해요. 설정 열기를 누르고 목록에서 이 브라우저를 켜 주세요. 브라우저를 다시 열라고 하면 다시 열어도 돼요. 이 화면이 이어서 열려요.' };
  if (p === 'windows') return { title, detail: 'Windows 설정 → 개인 정보 및 보안에서 이 브라우저의 화면 캡처가 막혀 있지 않은지 확인해 주세요. 보안 프로그램이나 회사 정책이 막고 있을 수도 있어요.' };
  return { title, detail: '운영체제 설정에서 이 브라우저의 화면 녹화·캡처 권한을 켜고 다시 시도해 주세요.' };
}

/** 폰 카드에 보일 실패 안내. 취소는 안내하지 않고, 운영체제가 막은 경우는 권한 안내 창이 맡는다. */
export function mirrorNotice(f: MirrorFailure): { title: string; detail: string } | null {
  if (f === 'capture') return { title: '고른 화면을 잡지 못했어요', detail: '미러링 창이 닫혔거나 최소화됐는지 확인하고 다시 골라 주세요.' };
  if (f === 'unsupported') return { title: '이 브라우저에서는 화면 공유를 쓸 수 없어요', detail: '최신 Chrome이나 Edge에서 localhost 또는 https 주소로 열어 주세요.' };
  return null;
}

/** 신호 확인: 영상을 이 크기로 줄여 BLANK_CHECK_MS마다 보고, 가장 밝은 점이 BLANK_LUMA보다 어두운 확인이 BLANK_CHECKS번 이어지면 신호 없음으로 본다 */
export const BLANK_SAMPLE = 96;
export const BLANK_LUMA = 16;
export const BLANK_CHECK_MS = 1000;
export const BLANK_CHECKS = 2;

/** RGBA 픽셀이 모두 검은가. 폰을 뽑아도 QuickTime 녹화 창은 열린 채 검은 화면만 보낸다. */
export function isBlank(rgba: ArrayLike<number>, luma = BLANK_LUMA): boolean {
  for (let i = 0; i < rgba.length; i += 4) {
    if (0.299 * rgba[i] + 0.587 * rgba[i + 1] + 0.114 * rgba[i + 2] >= luma) return false;
  }
  return true;
}

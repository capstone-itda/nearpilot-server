import { describe, expect, it } from 'vitest';
import { guideFirst, guideText, mirrorFailure, mirrorNotice, platformOf, settingsUrl, isBlank } from './mirror';

const err = (name: string, message = '') => Object.assign(new Error(message), { name });

describe('mirror', () => {
  it('FR12A 공유 실패 이유를 나눈다: 운영체제가 막음, 사람이 취소함, 화면을 못 잡음, 쓸 수 없음', () => {
    expect(mirrorFailure(err('NotAllowedError', 'Permission denied by system'))).toBe('system');
    expect(mirrorFailure(err('NotAllowedError', 'Permission denied'))).toBe('cancel');
    expect(mirrorFailure(err('NotReadableError', 'Could not start video source'))).toBe('capture');
    expect(mirrorFailure(err('NotFoundError'))).toBe('capture');
    expect(mirrorFailure(err('AbortError'))).toBe('capture');
    expect(mirrorFailure(err('TypeError', 'getDisplayMedia is not a function'))).toBe('unsupported');
    expect(mirrorFailure(undefined)).toBe('unsupported');
  });
  it('FR12A macOS에서 이 PC가 공유에 성공한 적이 없을 때만 권한 안내를 먼저 띄운다', () => {
    expect(platformOf('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)')).toBe('mac');
    expect(platformOf('Mozilla/5.0 (Windows NT 10.0; Win64; x64)')).toBe('windows');
    expect(platformOf('Mozilla/5.0 (X11; Linux x86_64)')).toBe('other');
    expect(guideFirst('mac', false)).toBe(true);
    expect(guideFirst('mac', true)).toBe(false);
    expect(guideFirst('windows', false)).toBe(false);
    expect(guideFirst('other', false)).toBe(false);
  });
  it('FR12A 운영체제마다 권한 설정을 여는 주소와 안내를 고르고, 카드 안내는 캡처 실패와 쓸 수 없음만 보인다', () => {
    expect(settingsUrl('mac')).toContain('Privacy_ScreenCapture');
    expect(settingsUrl('windows')).toBe('ms-settings:privacy');
    expect(settingsUrl('other')).toBeNull();
    expect(guideText('mac', false).title).toBe('화면 공유 권한이 필요해요');
    expect(guideText('windows', true).title).toBe('아직 화면 공유 권한이 꺼져 있어요');
    expect(guideText('windows', true).detail).toContain('Windows 설정');
    expect(mirrorNotice('cancel')).toBeNull();
    expect(mirrorNotice('system')).toBeNull();
    expect(mirrorNotice('capture')?.title).toBe('고른 화면을 잡지 못했어요');
    expect(mirrorNotice('unsupported')?.title).toBe('이 브라우저에서는 화면 공유를 쓸 수 없어요');
  });
  it('FR12A 미러링 영상이 모두 검으면 신호 없음으로 보고, 밝은 점이 하나라도 있으면 신호로 본다', () => {
    const black = new Uint8ClampedArray(4 * 64).fill(0);
    expect(isBlank(black)).toBe(true);
    const dim = black.slice(); dim.fill(10);
    expect(isBlank(dim)).toBe(true);
    const text = black.slice(); text.set([255, 255, 255, 255], 4 * 40);
    expect(isBlank(text)).toBe(false);
  });
});

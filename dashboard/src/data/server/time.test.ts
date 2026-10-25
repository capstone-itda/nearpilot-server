import { afterAll, beforeAll, describe, expect, it, vi } from 'vitest';
import { formatLocal, localTime, parseServerTime } from './time';

// 서버 시각은 시간대를 담고 화면은 이 PC의 지역 시각을 쓴다. 시험은 시간대를 서울(UTC+9, 서머타임 없음)로 고정한다.
beforeAll(() => { vi.stubEnv('TZ', 'Asia/Seoul'); });
afterAll(() => { vi.unstubAllEnvs(); });

describe('서버 어댑터 시각', () => {
  it('FR12B 이벤트 시각은 HH:MM:SS.mmm 지역 시각이다', () => {
    expect(localTime('2026-10-24T05:28:10.123+00:00', 'HH:MM:SS.mmm')).toBe('14:28:10.123');
  });
  it('FR12A 요청·감사·타임라인 시각은 HH:MM:SS, 점유·승인·이력 시각은 HH:MM이다', () => {
    expect(localTime('2026-10-24T05:28:10.123+00:00', 'HH:MM:SS')).toBe('14:28:10');
    expect(localTime('2026-10-24T05:28:10.123+00:00', 'HH:MM')).toBe('14:28');
  });
  it('FR12A 다른 시간대의 오프셋과 Z를 같은 순간의 지역 시각으로 바꾼다', () => {
    // 같은 순간을 UTC, 한국, 미국 동부 표기로 써도 같은 지역 시각이다
    expect(localTime('2026-10-24T05:28:10.000Z', 'HH:MM:SS')).toBe('14:28:10');
    expect(localTime('2026-10-24T14:28:10.000+09:00', 'HH:MM:SS')).toBe('14:28:10');
    expect(localTime('2026-10-24T01:28:10.000-04:00', 'HH:MM:SS')).toBe('14:28:10');
  });
  it('FR12A 날짜가 바뀌는 시각도 지역 시각의 시·분으로 쓴다', () => {
    // UTC 23:59는 서울의 다음 날 08:59이고, UTC 15:30은 서울의 다음 날 00:30이다
    expect(localTime('2026-10-24T23:59:59.999+00:00', 'HH:MM:SS.mmm')).toBe('08:59:59.999');
    expect(localTime('2026-10-24T15:30:00.000+00:00', 'HH:MM')).toBe('00:30');
  });
  it('FR12B 밀리초가 없거나 자리 수가 달라도 받는다', () => {
    expect(localTime('2026-10-24T05:28:10+00:00', 'HH:MM:SS.mmm')).toBe('14:28:10.000');
    expect(localTime('2026-10-24T05:28:10.5+00:00', 'HH:MM:SS.mmm')).toBe('14:28:10.500');
    expect(localTime('2026-10-24T05:28:10.123456+00:00', 'HH:MM:SS.mmm')).toBe('14:28:10.123');
  });
  it('FR12A 시간대가 없거나 ISO 8601이 아닌 값, 문자열이 아닌 값은 받지 않는다', () => {
    for (const bad of ['2026-10-24T05:28:10', '2026-10-24', '14:28:10', 'yesterday', '', '2026-13-45T25:61:61+00:00', 1761283690000, null, undefined, {}]) {
      expect(localTime(bad, 'HH:MM:SS'), String(bad)).toBeUndefined();
      expect(parseServerTime(bad), String(bad)).toBeUndefined();
    }
  });
  it('FR12B 한 자리 시·분·초·밀리초는 0을 채워 쓴다', () => {
    expect(formatLocal(new Date(2026, 9, 24, 3, 4, 5, 6), 'HH:MM:SS.mmm')).toBe('03:04:05.006');
    expect(formatLocal(new Date(2026, 9, 24, 3, 4, 5, 6), 'HH:MM')).toBe('03:04');
  });
});

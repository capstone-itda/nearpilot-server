// 서버 시각(ISO 8601, 밀리초, 시간대 포함. 예: 2026-10-24T14:28:10.123+00:00)을 이 PC의 지역 시각 문자열로 바꾼다.
// 화면은 목 데이터로 검증한 문자열을 그대로 쓰므로 필드마다 목 데이터가 쓰는 형식을 따른다. 날짜는 버린다 (시연은 하루 안이다).
//
// | 서버 필드 | 형식 | 목 데이터에서 찾은 같은 필드 |
// |---|---|---|
// | 이벤트 `at` | HH:MM:SS.mmm | story.ts의 stamp(), STORY 이벤트의 at |
// | 요청 `at`·`accessRequestedAt`·`accessExpiredAt`·`respondedAt` | HH:MM:SS | reduce.ts의 clockOf(e.at), RequestProgress.at |
// | 감사 행 `at` (조회와 스트림 audit의 row) | HH:MM:SS | fixtures.ts의 AUDIT |
// | 대여 타임라인 `at` | HH:MM:SS | fixtures.ts의 RENTALS timeline |
// | 점유 `startedAt` | HH:MM | fixtures.ts의 USES |
// | 승인 `requestedAt`·`validUntil` | HH:MM | fixtures.ts의 APPROVALS |
// | 이력 `at` | HH:MM | fixtures.ts의 HISTORY |
// | 신뢰도 `live.since` | HH:MM | fixtures.ts의 TRUST_BASE.since |
// | 대여 `startedAt` | HH:MM | fixtures.ts의 RENTALS |
// | 평가 `date` | 날짜 문자열 그대로 (바꾸지 않는다) | fixtures.ts의 TRUST.evaluation.date |

export type TimeFormat = 'HH:MM:SS.mmm' | 'HH:MM:SS' | 'HH:MM';

/** 밀리초 아래 자리 수는 서버마다 다를 수 있어 받아 준다. 시간대가 없는 시각은 받지 않는다 */
const ISO = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/;

/** 서버 시각을 읽는다. 형식이 틀리면 undefined */
export function parseServerTime(iso: unknown): Date | undefined {
  if (typeof iso !== 'string' || !ISO.test(iso)) return undefined;
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? undefined : d;
}

const two = (n: number) => String(n).padStart(2, '0');

/** 이 PC의 지역 시각으로 쓴다 */
export function formatLocal(d: Date, format: TimeFormat): string {
  const hm = `${two(d.getHours())}:${two(d.getMinutes())}`;
  if (format === 'HH:MM') return hm;
  const hms = `${hm}:${two(d.getSeconds())}`;
  return format === 'HH:MM:SS' ? hms : `${hms}.${String(d.getMilliseconds()).padStart(3, '0')}`;
}

/** 서버 시각 → 지역 시각 문자열. 형식이 틀리면 undefined */
export function localTime(iso: unknown, format: TimeFormat): string | undefined {
  const d = parseServerTime(iso);
  return d && formatLocal(d, format);
}

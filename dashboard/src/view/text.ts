// 화면에 쓰는 쉬운 말. 코드 값은 enums.py 그대로 두고 옆에 보조 글자로 붙인다.
import type { Action, Capability, ReasonCode, RequestProgress, Verdict } from '../model/types';

export const VERDICT_LABEL: Record<string, string> = {
  OK: '허가', ASK: '다시 질문', NEED_APPROVAL: '승인 필요', BUSY: '바쁨', DENY: '거부',
  FAULT: '고장 격리', QUERY: '조회', WAIT: '대기', INFO: '정보'
};

export const CAPABILITY_NAME: Record<Capability, string> = { locker: '사물함', light: '조명', speaker: '스피커' };
export const ACTION_NAME: Record<Action, string> = { open: '열기', on: '켜기', off: '끄기' };
export const OUTPUT_COMPLETE = '제어 신호 출력 완료';
export const ACTION_VERB: Record<Action, string> = { open: '열어요', on: '켜요', off: '꺼요' };

export const DEVICE_STATE_NAME: Record<string, string> = {
  AVAILABLE: '사용 가능', RESERVED: '예약', ACTIVE: '사용 중', CLOSING: '점유 해제 중', FAULT: '고장 격리'
};

/** 판정 단계 ⓪~⑥ */
export const STEP_NAMES = ['중복 확인', '누구인지 (비콘)', '후보 기기', '어디 앞인지 (근접)', '정책에 맞는지 (정책)', '기기가 비었는지', '사람이 다른 기기를 쓰는지'];
export const STEP_SHORT = ['중복', '비콘', '후보', '근접', '정책', '기기 점유', '사용자 점유'];
export const CIRCLED = ['⓪', '①', '②', '③', '④', '⑤', '⑥'];

/** 사유 코드의 쉬운 말. 감사 로그와 이력의 기본 문장이다. */
export const REASON_TEXT: Partial<Record<ReasonCode, string>> = {
  OK: '통과',
  INVALID_TOKEN: '토큰 확인 실패',
  INVALID_INPUT: '요청 형식이 맞지 않음',
  REQUEST_ID_CONFLICT: '같은 요청 번호에 다른 내용',
  CAPACITY_EXCEEDED: '서버가 바쁨',
  BEACON_NOT_SEEN: '비콘 수신 없음',
  NO_CANDIDATE: '쓸 수 있는 기기 없음',
  TARGET_UNAVAILABLE: '지금 쓸 수 없는 기기',
  LOW_CONFIDENCE: '어느 기기 앞인지 확신 부족',
  MOVE_CLOSER: '기기에 더 가까이 가야 함',
  APPROVAL_REQUIRED: '승인 필요',
  POLICY_DENIED: '정책이 허락하지 않음',
  APPROVAL_PENDING: '소유자 응답 기다림',
  APPROVAL_REJECTED: '소유자 거부',
  APPROVAL_EXPIRED: '승인 유효시간 끝남',
  APPROVAL_REQUEST_EXPIRED: '대기 만료',
  APPROVAL_EXHAUSTED: '사용 횟수를 다 씀',
  ACTION_NOT_ALLOWED: '승인과 다른 동작',
  DEVICE_BUSY: '이미 점유된 기기',
  USER_BUSY: '이미 다른 기기를 쓰는 중',
  NOT_ASSIGNED: '배정받지 못함',
  RECHECK_FAILED: '실행 직전 재검사 실패',
  INTERNAL_ERROR: '서버 오류',
  COMMAND_IN_PROGRESS: '다른 명령을 실행하는 중',
  EXEC_FAILED: '기기가 실패를 알림',
  EXEC_UNKNOWN: '결과 불명으로 격리',
  RESERVATION_EXPIRED: '예약 대기 시간 끝남',
  NODE_NOT_CONNECTED: '기기 연결 없음',
  COMMAND_NOT_SENT: '명령을 보내지 못함'
};
export const reasonText = (code: ReasonCode) => REASON_TEXT[code] ?? code;

/** NEED_APPROVAL 사유별 표시: 레일 표시, 무대 콜아웃 제목, 설명 (architecture.md 승인 사유 순서) */
export const APPROVAL_TEXT: Record<string, { mark: string; title: string; detail: string }> = {
  APPROVAL_REQUIRED: { mark: '승인 필요', title: '소유자 승인이 필요해요', detail: 'AI가 승인을 신청해야 해요' },
  ACTION_NOT_ALLOWED: { mark: '다른 동작', title: '승인받은 동작과 달라요', detail: '승인받은 것과 다른 동작이라 새 승인이 필요해요' },
  APPROVAL_PENDING: { mark: '승인 기다림', title: '소유자 응답을 기다려요', detail: '이미 보낸 승인 신청에 소유자가 아직 답하지 않았어요' },
  APPROVAL_REQUEST_EXPIRED: { mark: '신청 만료', title: '승인 신청이 만료됐어요', detail: '소유자가 답하지 않아 지난 신청이 만료됐어요' },
  APPROVAL_REJECTED: { mark: '승인 거부', title: '소유자가 거부했어요', detail: '소유자가 지난 승인 신청을 거부했어요' },
  APPROVAL_EXPIRED: { mark: '승인 만료', title: '승인 시간이 끝났어요', detail: '승인받은 유효시간이 지났어요' },
  APPROVAL_EXHAUSTED: { mark: '횟수 소진', title: '승인 횟수를 다 썼어요', detail: '승인받은 사용 횟수를 다 썼어요' }
};
export const approvalText = (code: ReasonCode) => APPROVAL_TEXT[code] ?? APPROVAL_TEXT.APPROVAL_REQUIRED;

/** 마지막 글자에 받침이 있는가. 숫자는 한국어 읽기로 본다. */
export function hasBatchim(word: string): boolean {
  const ch = word.trim().slice(-1);
  if (!ch) return false;
  const digit = '0136781'.includes(ch) ? true : '2459'.includes(ch) ? false : null;
  if (digit !== null) return digit;
  const code = ch.charCodeAt(0);
  if (code >= 0xac00 && code <= 0xd7a3) return (code - 0xac00) % 28 !== 0;
  return /[lmnr]$/i.test(ch);
}
export const josa = (word: string, withB: string, without: string) => word + (hasBatchim(word) ? withB : without);

/** request_use 의 대상 표기: nearest:locker, locker-2, any:locker */
export function targetCode(t: RequestProgress['target']): string {
  if (t.kind === 'explicit') return t.nodeId ?? '';
  return `${t.kind}:${t.capability}`;
}
export const toolCall = (r: Pick<RequestProgress, 'tool' | 'target' | 'action'>) => `${r.tool}(${targetCode(r.target)}, ${r.action})`;

/** 요청 제목: "사물함 열기", 명시 지정이면 "사물함 2 열기" */
export function requestTitle(r: Pick<RequestProgress, 'target' | 'action'>, deviceName: (id: string) => string): string {
  const what = r.target.kind === 'explicit' && r.target.nodeId ? deviceName(r.target.nodeId) : CAPABILITY_NAME[r.target.capability ?? 'locker'];
  return `${what} ${ACTION_NAME[r.action]}`;
}

export const verdictLabel = (v: Verdict | 'FAULT') => VERDICT_LABEL[v];

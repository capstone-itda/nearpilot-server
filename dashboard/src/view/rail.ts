// 모니터 상태 → 흐름 레일 다섯 단계. 쉬운 말이 크게, 코드는 보조 글자로.
// 서버의 확인 네 칸은 무대의 서버 상자가 보이고(serverChecks), 레일은 한 줄로 줄인다.
import { isDecided, type FloorLayout, type RequestProgress, type MonitorState } from '../model/types';
import { ACTION_DONE, ACTION_NAME, CIRCLED, HOST_NAME, STEP_SHORT, approvalText, josa, reasonText, requestTitle, toolCall } from './text';

export type CheckResult = 'pass' | 'fail' | 'wait' | 'unsure' | 'none' | 'skip';
export type StepTone = 'accent' | 'ok' | 'ask' | 'appr' | 'busy' | 'deny';
export interface RailStep {
  status: 'guide' | 'done' | 'current' | 'todo';
  tone?: StepTone;
  mark?: string;
  detail?: string;
  code?: string;
}
export interface RailView { mode: 'guide' | 'live'; title: string; meta: string; code: string; steps: RailStep[] }

export const GUIDE_STEPS: RailStep[] = [
  { status: 'guide', detail: '폰의 AI에게 말해요', code: '“내 앞 사물함 열어 줘”' },
  { status: 'guide', detail: 'AI가 서버에 요청해요', code: 'MCP · request_use' },
  { status: 'guide', detail: '누구·어디·정책·빈자리', code: '' },
  { status: 'guide', detail: '허가되면 기기가 움직여요', code: 'MQTT 명령' },
  { status: 'guide', detail: 'AI가 폰에 답해요', code: '' }
];

/** 확인 네 칸의 기본 이름 */
export const CHECK_NAMES = ['누구인지', '어디 앞인지', '정책', '비었는지'];

function shared(r: RequestProgress, layout: FloorLayout): boolean {
  const dev = layout.devices.find((d) => d.nodeId === r.nodeId);
  return (dev?.occupancy ?? layout.devices.find((d) => d.capability === r.target.capability)?.occupancy) === 'shared';
}

/** 서버의 확인 네 칸: ①비콘 ③근접 ④정책 ⑤⑥점유. 판정 전에는 비어 있다. 단계 밖에서 멈춘 요청(인증, 접수 제한, 재검사)은 통과한 칸만 ✓ */
export function serverChecks(r: RequestProgress, layout: FloorLayout): [CheckResult, string][] {
  const out: [CheckResult, string][] = CHECK_NAMES.map((n) => ['none', n]);
  if (!isDecided(r)) return out;
  const name = layout.devices.find((d) => d.nodeId === r.nodeId)?.name ?? '';
  const st = r.steps;
  const stopAt = st.findIndex((x) => x.result === 'stop');
  const passed = (i: number) => st[i]?.result === 'pass';
  // 서버 오류로 멈추면 그 단계의 거절 이유가 아니므로 사유를 그대로 쓴다
  const fail = (text: string): [CheckResult, string] => ['fail', r.reason === 'INTERNAL_ERROR' ? reasonText(r.reason) : text];

  // ① 누구인지
  if (stopAt === 1) out[0] = fail('폰 신호 없음');
  else if (passed(1)) out[0] = ['pass', '누구인지'];
  // ③ 어디 앞인지 (②에서 멈추면 후보 없음)
  if (stopAt === 2) out[1] = fail('쓸 기기 없음');
  else if (stopAt === 3) out[1] = r.verdict !== 'ASK' ? fail('기기 앞이 아님') : r.reason === 'MOVE_CLOSER' ? ['unsure', '지정 기기 앞인지 애매'] : ['unsure', '어디 앞인지 애매'];
  else if (passed(3)) out[1] = ['pass', name ? `${name} 앞` : '기기 앞'];
  // ④ 정책
  if (stopAt === 4) out[2] = r.verdict === 'NEED_APPROVAL' ? ['wait', '승인 필요'] : fail('정책이 막음');
  else if (passed(4)) out[2] = ['pass', '정책 통과'];
  // ⑤⑥ 비어 있는지
  if (shared(r, layout)) out[3] = ['none', '공용형 생략'];
  else if (stopAt === 5) out[3] = fail(r.reason === 'COMMAND_IN_PROGRESS' ? '다른 명령 실행 중' : '이미 점유됨');
  else if (stopAt === 6) out[3] = fail('다른 기기 사용 중');
  else if (passed(5) && passed(6)) out[3] = ['pass', '비어 있음'];
  return out;
}

/** 판정 결과 한 줄: 멈춘 단계, 모두 통과, 또는 단계 밖 사유 */
export function judgeSummary(r: RequestProgress): string {
  if (!isDecided(r)) return '';
  const stopAt = r.steps.findIndex((x) => x.result === 'stop');
  if (stopAt !== -1) return `${CIRCLED[stopAt]} ${STEP_SHORT[stopAt]}에서 멈춤`;
  return r.verdict === 'OK' ? '모두 통과' : reasonText(r.reason);
}

export function railView(s: MonitorState, layout: FloorLayout, mirroredPersonId: string | undefined): RailView {
  const r = s.request;
  if (!r) {
    return { mode: 'guide', title: s.connected ? '요청 대기' : '마지막으로 받은 화면', meta: '', code: '', steps: GUIDE_STEPS };
  }
  const name = layout.devices.find((d) => d.nodeId === r.nodeId)?.name ?? '';
  const who = s.people.find((p) => p.id === r.personId)?.pseudonym ?? '방문자';
  const host = HOST_NAME[r.host];
  const act = ACTION_NAME[r.action];
  const nodeAct = `${r.nodeId} · ${r.action}`;
  const head = { mode: 'live' as const, title: `${who} · ${requestTitle(r, (id) => layout.devices.find((d) => d.nodeId === id)?.name ?? id)}`, meta: `${host} · ${r.at}`, code: toolCall(r) };

  const s1: RailStep = { status: 'done', detail: `${josa(who, '이', '가')} AI에게 말함`, code: mirroredPersonId === r.personId ? '' : `대화는 ${who}의 폰에` };
  const s2: RailStep = { status: 'done', detail: `${josa(host, '이', '가')} 요청함`, code: r.tool };

  if (!isDecided(r)) {
    // 판정 전: 판정 게이지는 무대의 서버 상자에만 둔다
    return { ...head, steps: [s1, s2, { status: 'current', tone: 'accent', mark: '판정 중' }, { status: 'todo' }, { status: 'todo' }] };
  }
  const s3: RailStep = { status: 'done', detail: judgeSummary(r) };
  let s4: RailStep = { status: 'todo', detail: '실행하지 않음' };
  let s5: RailStep;

  switch (r.verdict) {
    case 'OK':
      if (!r.execution) {
        // 판정은 났고 명령은 아직 보내지 않았다
        s4 = { status: 'current', tone: 'ok', mark: '허가', detail: `${name}에 ${act} 명령 보낼 차례`, code: nodeAct };
        s5 = { status: 'todo' };
      } else if (r.execution === 'sent') {
        s4 = { status: 'current', tone: 'ok', mark: '실행 중', detail: `${name}에 ${act} 명령`, code: `${nodeAct} · MQTT` };
        s5 = { status: 'todo' };
      } else {
        s4 = { status: 'done', detail: `${act} 명령 보냄`, code: nodeAct };
        s5 = r.execution === 'succeeded'
          ? { status: 'current', tone: 'ok', mark: '완료', detail: `${josa(name, '이', '가')} ${ACTION_DONE[r.action]}`, code: 'OK' }
          : r.execution === 'failed'
            ? { status: 'current', tone: 'deny', mark: '고장 격리', detail: '기기가 실패를 알림', code: 'EXEC_FAILED → FAULT' }
            : { status: 'current', tone: 'deny', mark: '고장 격리', detail: '응답 없음 · 결과 불명', code: 'EXEC_UNKNOWN → FAULT' };
      }
      break;
    case 'ASK':
      s5 = { status: 'current', tone: 'ask', mark: '다시 질문', detail: r.reason === 'MOVE_CLOSER' ? '더 가까이 와 달라고 함' : '어느 기기인지 되물음', code: `ASK · ${r.reason}` };
      break;
    case 'NEED_APPROVAL':
      s5 = r.accessExpiredAt
        ? { status: 'current', tone: 'appr', mark: '신청 만료', detail: '소유자 응답 없음', code: 'APPROVAL_REQUEST_EXPIRED' }
        : r.accessRequestedAt
        ? { status: 'current', tone: 'appr', mark: '승인 기다림', detail: '소유자에게 알림 보냄', code: 'NEED_APPROVAL → request_access' }
        : { status: 'current', tone: 'appr', mark: approvalText(r.reason).mark, detail: approvalText(r.reason).title, code: `NEED_APPROVAL · ${r.reason}` };
      break;
    case 'BUSY':
      s5 = { status: 'current', tone: 'busy', mark: '바쁨', detail: r.reason === 'USER_BUSY' ? '먼저 반납해 달라고 함' : r.reason === 'COMMAND_IN_PROGRESS' ? '명령이 끝나면 다시 해 달라고 함' : '지금은 쓸 수 없다고 함', code: `BUSY · ${r.reason}` };
      break;
    default:
      s5 = { status: 'current', tone: 'deny', mark: '거부', detail: '쓸 수 없다고 답함', code: `DENY · ${r.reason}` };
  }

  return { ...head, steps: [s1, s2, s3, s4, s5] };
}

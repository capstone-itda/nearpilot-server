// 스냅숏 → 흐름 레일 다섯 단계. 쉬운 말이 크게, 코드는 보조 글자로.
import type { FloorLayout, RequestProgress, Snapshot } from '../model/types';
import { ACTION_DONE, ACTION_NAME, HOST_NAME, josa, requestTitle, toolCall } from './text';

export type CheckResult = 'pass' | 'fail' | 'wait' | 'unsure' | 'none' | 'skip';
export type StepTone = 'accent' | 'ok' | 'ask' | 'appr' | 'busy' | 'deny';
export interface RailStep {
  status: 'guide' | 'done' | 'current' | 'todo';
  tone?: StepTone;
  mark?: string;
  detail?: string;
  code?: string;
  /** 3단계의 네 가지 확인: [결과, 문구]. 문구가 없으면 기본 이름 */
  checks?: [CheckResult, string?][];
}
export interface RailView { mode: 'guide' | 'live'; title: string; meta: string; code: string; steps: RailStep[] }

export const GUIDE_STEPS: RailStep[] = [
  { status: 'guide', detail: '방문자가 폰의 AI에게 말해요', code: '예: “내 앞 사물함 열어 줘”' },
  { status: 'guide', detail: 'AI가 서버에 기기 사용을 요청해요', code: 'MCP · request_use' },
  { status: 'guide', checks: [['none'], ['none'], ['none'], ['none']] },
  { status: 'guide', detail: '서버가 허가하면 기기가 움직여요', code: 'MQTT 명령' },
  { status: 'guide', detail: '결과가 AI를 거쳐 폰에 돌아가요', code: '판정 결과' }
];

/** 3단계 확인 네 칸: ①비콘 ③근접 ④정책 ⑤⑥점유 */
function checks(r: RequestProgress, name: string, shared: boolean): [CheckResult, string?][] {
  const st = r.steps;
  const stopAt = st.findIndex((x) => x.result === 'stop');
  const reached = (i: number) => stopAt === -1 || i <= stopAt;
  const out: [CheckResult, string?][] = [['none'], ['none'], ['none'], ['none']];

  // ① 누구인지 (⓪은 통과로 본다)
  if (stopAt === 1) out[0] = ['fail', '폰 신호 없음'];
  else if (reached(1) && stopAt !== 0) out[0] = ['pass', '누구인지'];
  // ③ 어디 앞인지 (②에서 멈추면 후보 없음)
  if (stopAt === 2) out[1] = ['fail', '쓸 수 있는 기기 없음'];
  else if (stopAt === 3) out[1] = r.verdict === 'ASK' ? ['unsure', '어디 앞인지 애매함'] : ['fail', '기기 앞이 아님'];
  else if (reached(3) && stopAt > 3 || stopAt === -1) out[1] = ['pass', `${name} 앞`];
  // ④ 정책
  if (stopAt === 4) out[2] = r.verdict === 'NEED_APPROVAL' ? ['wait', '소유자 승인 필요'] : ['fail', '정책이 막음'];
  else if (stopAt > 4 || stopAt === -1) out[2] = ['pass', '정책 통과'];
  // ⑤⑥ 비어 있는지
  if (shared) out[3] = ['none', '공용형 생략'];
  else if (stopAt === 5) out[3] = ['fail', '다른 사람이 사용 중'];
  else if (stopAt === 6) out[3] = ['fail', '이미 다른 기기 사용 중'];
  else if (stopAt === -1) out[3] = ['pass', '비어 있음'];
  return out;
}

export function railView(s: Snapshot, layout: FloorLayout, mirroredPersonId: string | undefined): RailView {
  const r = s.request;
  if (!r) {
    return s.connected
      ? { mode: 'guide', title: '요청을 기다리고 있어요', meta: '방문자가 AI에게 말하면 아래 다섯 단계가 차례로 밝아져요', code: '', steps: GUIDE_STEPS }
      : { mode: 'guide', title: '마지막으로 받은 화면이에요', meta: '다시 연결되면 지금 상태로 바뀌어요', code: '', steps: GUIDE_STEPS };
  }
  const dev = layout.devices.find((d) => d.nodeId === r.nodeId);
  const name = dev?.name ?? '';
  const who = s.people.find((p) => p.id === r.personId)?.pseudonym ?? '방문자';
  const host = HOST_NAME[r.host];
  const shared = (dev?.occupancy ?? layout.devices.find((d) => d.capability === r.target.capability)?.occupancy) === 'shared';
  const act = ACTION_NAME[r.action];
  const nodeAct = `${r.nodeId} · ${r.action}`;

  const s1: RailStep = { status: 'done', detail: `${josa(who, '이', '가')} AI에게 말함`, code: mirroredPersonId === r.personId ? '대화는 왼쪽 폰 화면에' : `대화는 ${who}의 폰에` };
  const s2: RailStep = { status: 'done', detail: `${josa(host, '이', '가')} 요청함`, code: r.tool };
  const s3: RailStep = { status: 'done', checks: checks(r, name, shared) };
  let s4: RailStep;
  let s5: RailStep;

  switch (r.verdict) {
    case 'OK':
      if (r.execution === 'sent' || !r.execution) {
        s4 = { status: 'current', tone: 'ok', mark: '실행 중', detail: `${name}에 ${act} 명령`, code: `${nodeAct} · MQTT` };
        s5 = { status: 'todo', detail: '결과를 기다려요', code: '' };
      } else {
        s4 = { status: 'done', detail: `${act} 명령 보냄`, code: nodeAct };
        s5 = r.execution === 'succeeded'
          ? { status: 'current', tone: 'ok', mark: '완료', detail: `${josa(name, '이', '가')} ${ACTION_DONE[r.action]}`, code: 'OK' }
          : r.execution === 'failed'
            ? { status: 'current', tone: 'deny', mark: '고장 격리', detail: '기기가 실패를 알렸어요', code: 'EXEC_FAILED → FAULT' }
            : { status: 'current', tone: 'deny', mark: '고장 격리', detail: '응답이 없어 결과를 몰라요', code: 'EXEC_UNKNOWN → FAULT' };
      }
      break;
    case 'ASK':
      s4 = { status: 'todo', detail: '실행하지 않음', code: '' };
      s5 = { status: 'current', tone: 'ask', mark: '다시 질문', detail: r.reason === 'MOVE_CLOSER' ? '가까이 가서 다시 말해 달라고 안내' : '어느 기기인지 다시 물어봐요', code: `ASK · ${r.reason}` };
      break;
    case 'NEED_APPROVAL':
      s4 = { status: 'todo', detail: `아직 ${r.action === 'open' ? '열지' : r.action === 'on' ? '켜지' : '끄지'} 않았어요`, code: '승인 뒤 request_use 다시 호출' };
      s5 = r.accessRequestedAt
        ? { status: 'current', tone: 'appr', mark: '승인 기다림', detail: 'AI가 승인을 신청했어요', code: 'NEED_APPROVAL → request_access' }
        : { status: 'current', tone: 'appr', mark: '승인 필요', detail: 'AI가 승인을 신청해야 해요', code: `NEED_APPROVAL · ${r.reason}` };
      break;
    case 'BUSY':
      s4 = { status: 'todo', detail: '실행하지 않음', code: '' };
      s5 = { status: 'current', tone: 'busy', mark: '바쁨', detail: r.reason === 'USER_BUSY' ? '쓰던 기기를 먼저 반납해 달라고 안내' : r.reason === 'COMMAND_IN_PROGRESS' ? '다른 명령이 끝나면 다시 해 달라고 안내' : '지금은 쓸 수 없다고 안내', code: `BUSY · ${r.reason}` };
      break;
    default:
      s4 = { status: 'todo', detail: '실행하지 않음', code: '' };
      s5 = { status: 'current', tone: 'deny', mark: '거부', detail: '쓸 수 없다고 안내', code: `DENY · ${r.reason}` };
  }

  return {
    mode: 'live',
    title: `${who} · ${requestTitle(r, (id) => layout.devices.find((d) => d.nodeId === id)?.name ?? id)}`,
    meta: `${host} · ${r.at}`,
    code: toolCall(r),
    steps: [s1, s2, s3, s4, s5]
  };
}

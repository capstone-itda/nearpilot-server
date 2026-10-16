// FR-12B: 흐름 레일 다섯 단계를 쉬운 말로 계산한다
import { describe, expect, it } from 'vitest';
import { LAYOUT } from '../data/mock/fixtures';
import { SCENES, sec, stateAt } from '../data/mock/story';
import { railView } from './rail';

describe('railView', () => {
  it('FR12B 대기: 안내 모드와 다섯 단계 설명', () => {
    const v = railView(SCENES.idle(), LAYOUT, 'p-a');
    expect(v.mode).toBe('guide');
    expect(v.title).toBe('요청을 기다리고 있어요');
    expect(v.steps.every((s) => s.status === 'guide')).toBe(true);
  });

  it('FR12B 연결 끊김: 마지막 화면 안내', () => {
    expect(railView(SCENES.offline(), LAYOUT, 'p-a').title).toBe('마지막으로 받은 화면이에요');
  });

  it('FR12B 요청 진행: 4단계가 지금 단계, 네 가지 확인 통과', () => {
    const v = railView(SCENES.flow(), LAYOUT, 'p-a');
    expect(v).toMatchObject({ mode: 'live', title: '방문자 A · 사물함 열기', meta: 'Claude · 14:28:10', code: 'request_use(nearest:locker, open)' });
    expect(v.steps.map((s) => s.status)).toEqual(['done', 'done', 'done', 'current', 'todo']);
    expect(v.steps[0]).toMatchObject({ detail: '방문자 A가 AI에게 말함', code: '대화는 왼쪽 폰 화면에' });
    expect(v.steps[2].checks).toEqual([['pass', '누구인지'], ['pass', '사물함 1 앞'], ['pass', '정책 통과'], ['pass', '비어 있음']]);
    expect(v.steps[3]).toMatchObject({ tone: 'ok', mark: '실행 중', detail: '사물함 1에 열기 명령', code: 'locker-1 · open · MQTT' });
  });

  it('FR12B 실행 완료: 5단계가 완료', () => {
    const s = SCENES.flow();
    s.request!.execution = 'succeeded';
    const v = railView(s, LAYOUT, 'p-a');
    expect(v.steps[3].status).toBe('done');
    expect(v.steps[4]).toMatchObject({ status: 'current', tone: 'ok', mark: '완료', detail: '사물함 1이 열렸어요' });
  });

  it('FR12B 고장 격리: 5단계가 빨강, 결과 불명', () => {
    const v = railView(SCENES.fault(), LAYOUT, 'p-b');
    expect(v.steps[4]).toMatchObject({ status: 'current', tone: 'deny', mark: '고장 격리', code: 'EXEC_UNKNOWN → FAULT' });
  });

  it('FR12B ASK: 실행하지 않고 근접 확인이 애매함, 뒤 확인은 하지 않음', () => {
    const v = railView(SCENES.ask(), LAYOUT, 'p-a');
    expect(v.steps[2].checks).toEqual([['pass', '누구인지'], ['unsure', '어디 앞인지 애매함'], ['none'], ['none']]);
    expect(v.steps[3]).toMatchObject({ status: 'todo', detail: '실행하지 않음' });
    expect(v.steps[4]).toMatchObject({ tone: 'ask', code: 'ASK · LOW_CONFIDENCE' });
  });

  it('FR12B 승인 대기: 폰은 소유자를 보여서 대화는 요청자 폰에 있다고 안내', () => {
    const v = railView(SCENES.approval(), LAYOUT, 'p-owner');
    expect(v.steps[0].code).toBe('대화는 방문자 B의 폰에');
    expect(v.steps[2].checks).toEqual([['pass', '누구인지'], ['pass', '조명 1 앞'], ['wait', '소유자 승인 필요'], ['none', '공용형 생략']]);
    expect(v.steps[3]).toMatchObject({ status: 'todo', detail: '아직 켜지 않았어요' });
    expect(v.steps[4]).toMatchObject({ tone: 'appr', mark: '승인 기다림', code: 'NEED_APPROVAL → request_access' });
  });

  it('FR12B BUSY와 DENY는 멈춘 확인 칸을 실패로 보인다', () => {
    const busy = SCENES.flow();
    busy.request = { ...busy.request!, verdict: 'BUSY', reason: 'DEVICE_BUSY', execution: undefined,
      steps: [0, 1, 2, 3, 4].map(() => ({ result: 'pass' as const })).concat([{ result: 'stop' }, { result: 'skip' }]) };
    const b = railView(busy, LAYOUT, 'p-a');
    expect(b.steps[2].checks![3]).toEqual(['fail', '이미 점유됨']);
    expect(b.steps[4]).toMatchObject({ tone: 'busy', mark: '바쁨' });
    const deny = SCENES.flow();
    deny.request = { ...deny.request!, verdict: 'DENY', reason: 'BEACON_NOT_SEEN', nodeId: undefined, execution: undefined,
      steps: [{ result: 'pass' }, { result: 'stop' }, ...[0, 1, 2, 3, 4].map(() => ({ result: 'skip' as const }))] };
    const d = railView(deny, LAYOUT, 'p-a');
    expect(d.steps[2].checks![0]).toEqual(['fail', '폰 신호 없음']);
    expect(d.steps[4]).toMatchObject({ tone: 'deny', code: 'DENY · BEACON_NOT_SEEN' });
  });
});

describe('railView 사유별 문구', () => {
  it('FR12B 실행 실패는 고장 격리로 보인다', () => {
    const s = SCENES.fault();
    s.request = { ...s.request!, execution: 'failed' };
    expect(railView(s, LAYOUT, 'p-b').steps[4]).toMatchObject({ mark: '고장 격리', code: 'EXEC_FAILED → FAULT' });
  });
  it('FR12B 확신 부족 ASK는 다시 묻고, 이동 안내는 MOVE_CLOSER에만 쓴다', () => {
    const s = SCENES.ask();
    expect(railView(s, LAYOUT, 'p-a').steps[4].detail).toBe('어느 기기인지 다시 물어봐요');
    s.request = { ...s.request!, reason: 'MOVE_CLOSER' };
    expect(railView(s, LAYOUT, 'p-a').steps[4].detail).toBe('가까이 가서 다시 말해 달라고 안내');
  });
});

describe('railView 단계 밖 판정', () => {
  it('FR12B 단계 밖에서 멈춘 거부는 확인 칸을 통과로 보이지 않는다', () => {
    const s = SCENES.flow();
    s.request = { ...s.request!, verdict: 'DENY', reason: 'CAPACITY_EXCEEDED', nodeId: undefined, execution: undefined, steps: Array(7).fill({ result: 'skip' }) };
    expect(railView(s, LAYOUT, 'p-a').steps[2].checks!.every(([c]) => c === 'none')).toBe(true);
  });
  it('FR12B 다른 명령이 진행 중인 BUSY는 점유와 다르게 쓴다', () => {
    const s = SCENES.flow();
    const steps = s.request!.steps.map((x, i) => (i === 5 ? { result: 'stop' as const } : i === 6 ? { result: 'skip' as const } : x));
    s.request = { ...s.request!, verdict: 'BUSY', reason: 'COMMAND_IN_PROGRESS', execution: undefined, steps };
    expect(railView(s, LAYOUT, 'p-a').steps[2].checks![3]).toEqual(['fail', '다른 명령 실행 중']);
  });
  it('FR12B 승인 필요 사유를 구분해서 보인다', () => {
    const s = SCENES.approval();
    s.request = { ...s.request!, reason: 'APPROVAL_PENDING', accessRequestedAt: undefined };
    expect(railView(s, LAYOUT, 'p-b').steps[4]).toMatchObject({ mark: '승인 기다림', code: 'NEED_APPROVAL · APPROVAL_PENDING' });
  });
  it('FR12B 서버 오류로 멈춘 칸은 단계 이유 대신 서버 오류라고 쓴다', () => {
    const s = SCENES.flow();
    const steps = s.request!.steps.map((x, i) => (i === 4 ? { result: 'stop' as const } : i > 4 ? { result: 'skip' as const } : x));
    s.request = { ...s.request!, verdict: 'DENY', reason: 'INTERNAL_ERROR', execution: undefined, steps };
    expect(railView(s, LAYOUT, 'p-a').steps[2].checks![2]).toEqual(['fail', '서버 오류']);
  });
});

describe('railView 판정 전', () => {
  it('FR12B 단계 결과가 도착하는 동안 3단계가 지금 단계이고 통과한 칸만 채운다', () => {
    const early = railView(stateAt(sec('14:28:10.05')), LAYOUT, 'p-a');
    expect(early.steps.map((s) => s.status)).toEqual(['done', 'done', 'current', 'todo', 'todo']);
    expect(early.steps[2]).toMatchObject({ mark: '확인 중', checks: [['pass', '누구인지'], ['none'], ['none'], ['none']] });
    // ③ 근접까지 통과
    const mid = railView(stateAt(sec('14:28:10.09')), LAYOUT, 'p-a');
    expect(mid.steps[2].checks![1][0]).toBe('pass');
  });
  it('FR12B 멈춘 단계가 왔지만 판정 전이면 그 칸을 판정 중으로 보인다', () => {
    const v = railView(stateAt(sec('14:26:12.09')), LAYOUT, 'p-a');
    expect(v.steps[2].checks![1]).toEqual(['unsure', '여기서 멈춤 · 판정 중']);
  });
  it('FR12B 승인 신청이 만료되면 5단계가 만료를 말한다', () => {
    const s = stateAt(sec('14:41:01'));
    expect(railView(s, LAYOUT, 'p-b').steps[4]).toMatchObject({ mark: '신청 만료', code: 'APPROVAL_REQUEST_EXPIRED' });
  });
});

describe('railView 명령 전', () => {
  it('FR12B 판정은 OK지만 명령 이벤트가 오기 전에는 허가로 보이고, 명령이 오면 실행 중', () => {
    expect(railView(stateAt(sec('14:28:10.205')), LAYOUT, 'p-a').steps[3]).toMatchObject({ status: 'current', mark: '허가' });
    expect(railView(stateAt(sec('14:28:10.215')), LAYOUT, 'p-a').steps[3]).toMatchObject({ status: 'current', mark: '실행 중' });
  });
});

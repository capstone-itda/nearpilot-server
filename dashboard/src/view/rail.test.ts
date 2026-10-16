// FR-12B: 흐름 레일 다섯 단계를 쉬운 말로 계산한다
import { describe, expect, it } from 'vitest';
import { LAYOUT, SCENES } from '../data/mock/fixtures';
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
    expect(v.steps[0]).toMatchObject({ detail: '방문자 A가 AI에게 말함', code: '말은 왼쪽 폰 화면' });
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

  it('FR12B 승인 대기: 폰은 소유자를 보여서 말은 요청자 폰에 있다고 안내', () => {
    const v = railView(SCENES.approval(), LAYOUT, 'p-owner');
    expect(v.steps[0].code).toBe('말은 방문자 B의 폰에');
    expect(v.steps[2].checks).toEqual([['pass', '누구인지'], ['pass', '조명 1 앞'], ['wait', '소유자 승인 필요'], ['none', '공용형 생략']]);
    expect(v.steps[3]).toMatchObject({ status: 'todo', detail: '아직 켜지 않았어요' });
    expect(v.steps[4]).toMatchObject({ tone: 'appr', mark: '승인 기다림', code: 'NEED_APPROVAL → request_access' });
  });

  it('FR12B BUSY와 DENY는 멈춘 확인 칸을 실패로 보인다', () => {
    const busy = SCENES.flow();
    busy.request = { ...busy.request!, verdict: 'BUSY', reason: 'DEVICE_BUSY', execution: undefined,
      steps: [0, 1, 2, 3, 4].map(() => ({ result: 'pass' as const })).concat([{ result: 'stop' }, { result: 'skip' }]) };
    const b = railView(busy, LAYOUT, 'p-a');
    expect(b.steps[2].checks![3]).toEqual(['fail', '다른 사람이 사용 중']);
    expect(b.steps[4]).toMatchObject({ tone: 'busy', mark: '바쁨' });
    const deny = SCENES.flow();
    deny.request = { ...deny.request!, verdict: 'DENY', reason: 'BEACON_NOT_SEEN', nodeId: undefined, execution: undefined,
      steps: [{ result: 'pass' }, { result: 'stop' }, ...[0, 1, 2, 3, 4].map(() => ({ result: 'skip' as const }))] };
    const d = railView(deny, LAYOUT, 'p-a');
    expect(d.steps[2].checks![0]).toEqual(['fail', '폰 신호 없음']);
    expect(d.steps[4]).toMatchObject({ tone: 'deny', code: 'DENY · BEACON_NOT_SEEN' });
  });
});

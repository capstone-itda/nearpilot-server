// FR-12B: 흐름 레일 다섯 단계를 쉬운 말로 계산하고, 서버 랙의 확인 네 칸을 계산한다
import { describe, expect, it } from 'vitest';
import { PLACES } from '../data/mock/fixtures';
import { SCENES, sec, stateAt } from '../data/mock/story';
import type { MonitorState, PlaceInfo } from '../model/types';
import { requestPlace } from './places';
import { railView, serverChecks } from './rail';

const checksOf = (s: MonitorState) => serverChecks(s.request!, PLACES, requestPlace(s, PLACES));

describe('railView', () => {
  it('FR12B 대기: 안내 모드와 다섯 단계 설명', () => {
    const v = railView(SCENES.idle(), PLACES, 'p-a');
    expect(v).toMatchObject({ mode: 'guide', title: '요청 대기', meta: '' });
    expect(v.steps.every((s) => s.status === 'guide')).toBe(true);
  });

  it('FR12B 연결 끊김: 마지막 화면 안내', () => {
    expect(railView(SCENES.offline(), PLACES, 'p-a').title).toBe('마지막으로 받은 화면');
  });

  it('FR12B 요청 진행: 4단계가 지금 단계, 3단계는 모두 통과', () => {
    const s = SCENES.flow();
    const v = railView(s, PLACES, 'p-a');
    expect(v).toMatchObject({ mode: 'live', title: '방문자 A · 사물함 열기', meta: '14:28:10', code: 'request_use(nearest:locker, open)' });
    expect(v.steps.map((x) => x.status)).toEqual(['done', 'done', 'done', 'current', 'todo']);
    // 대화가 왼쪽 폰에 보이면 따로 말하지 않는다
    expect(v.steps[0]).toMatchObject({ detail: '방문자 A가 AI에게 말함', code: '' });
    expect(v.steps[2].detail).toBe('모두 통과');
    expect(checksOf(s)).toEqual([['pass', '누구인지'], ['pass', '사물함 1 앞'], ['pass', '정책 통과'], ['pass', '비어 있음']]);
    expect(v.steps[3]).toMatchObject({ tone: 'ok', mark: '실행 중', detail: '사물함 1에 열기 명령', code: 'locker-1 · open · MQTT' });
  });

  it('FR12B 실행 완료: 5단계가 완료', () => {
    const s = SCENES.flow();
    s.request!.execution = 'succeeded';
    const v = railView(s, PLACES, 'p-a');
    expect(v.steps[3].status).toBe('done');
    expect(v.steps[4]).toMatchObject({ status: 'current', tone: 'ok', mark: '완료', detail: '사물함 1이 열렸어요' });
  });

  it('FR12B 고장 격리: 5단계가 빨강, 결과 불명', () => {
    const v = railView(SCENES.fault(), PLACES, 'p-b');
    expect(v.steps[4]).toMatchObject({ status: 'current', tone: 'deny', mark: '고장 격리', code: 'EXEC_UNKNOWN → FAULT' });
  });

  it('FR12B ASK: 실행하지 않고, 3단계는 멈춘 단계, 근접 칸이 애매함', () => {
    const s = SCENES.ask();
    const v = railView(s, PLACES, 'p-a');
    expect(v.steps[2].detail).toBe('③ 근접에서 멈춤');
    expect(checksOf(s)).toEqual([['pass', '누구인지'], ['unsure', '어디 앞인지 애매'], ['none', '정책'], ['none', '비었는지']]);
    expect(v.steps[3]).toMatchObject({ status: 'todo', detail: '실행하지 않음' });
    expect(v.steps[4]).toMatchObject({ tone: 'ask', code: 'ASK · LOW_CONFIDENCE' });
  });

  it('FR12B 승인 대기: 폰은 소유자를 보여서 대화는 요청자 폰에 있다고 안내', () => {
    const s = SCENES.approval();
    const v = railView(s, PLACES, 'p-owner');
    expect(v.steps[0].code).toBe('대화는 방문자 B의 폰에');
    expect(checksOf(s)).toEqual([['pass', '누구인지'], ['pass', '조명 1 앞'], ['wait', '승인 필요'], ['none', '공용형 생략']]);
    expect(v.steps[3]).toMatchObject({ status: 'todo', detail: '실행하지 않음' });
    expect(v.steps[4]).toMatchObject({ tone: 'appr', mark: '승인 기다림', code: 'NEED_APPROVAL → request_access' });
  });

  it('FR12B BUSY와 DENY는 멈춘 확인 칸을 실패로 보인다', () => {
    const busy = SCENES.flow();
    busy.request = { ...busy.request!, verdict: 'BUSY', reason: 'DEVICE_BUSY', execution: undefined,
      steps: [0, 1, 2, 3, 4].map(() => ({ result: 'pass' as const })).concat([{ result: 'stop' }, { result: 'skip' }]) };
    expect(checksOf(busy)[3]).toEqual(['fail', '이미 점유됨']);
    expect(railView(busy, PLACES, 'p-a').steps[4]).toMatchObject({ tone: 'busy', mark: '바쁨' });
    const deny = SCENES.flow();
    deny.request = { ...deny.request!, verdict: 'DENY', reason: 'BEACON_NOT_SEEN', nodeId: undefined, execution: undefined,
      steps: [{ result: 'pass' }, { result: 'stop' }, ...[0, 1, 2, 3, 4].map(() => ({ result: 'skip' as const }))] };
    expect(checksOf(deny)[0]).toEqual(['fail', '폰 신호 없음']);
    const d = railView(deny, PLACES, 'p-a');
    expect(d.steps[2].detail).toBe('① 비콘에서 멈춤');
    expect(d.steps[4]).toMatchObject({ tone: 'deny', code: 'DENY · BEACON_NOT_SEEN' });
  });
});

describe('railView 사유별 문구', () => {
  it('FR12B 실행 실패는 고장 격리로 보인다', () => {
    const s = SCENES.fault();
    s.request = { ...s.request!, execution: 'failed' };
    expect(railView(s, PLACES, 'p-b').steps[4]).toMatchObject({ mark: '고장 격리', code: 'EXEC_FAILED → FAULT' });
  });
  it('FR12B 확신 부족 ASK는 되묻고, 이동 안내는 MOVE_CLOSER에만 쓴다', () => {
    const s = SCENES.ask();
    expect(railView(s, PLACES, 'p-a').steps[4].detail).toBe('어느 기기인지 되물음');
    s.request = { ...s.request!, reason: 'MOVE_CLOSER' };
    expect(railView(s, PLACES, 'p-a').steps[4].detail).toBe('더 가까이 와 달라고 함');
  });
});

describe('railView 단계 밖 판정', () => {
  it('FR12B 단계 밖에서 멈춘 거부는 확인 칸을 통과로 보이지 않고 사유를 적는다', () => {
    const s = SCENES.flow();
    s.request = { ...s.request!, verdict: 'DENY', reason: 'CAPACITY_EXCEEDED', nodeId: undefined, execution: undefined, steps: Array(7).fill({ result: 'skip' }) };
    expect(checksOf(s).every(([c]) => c === 'none')).toBe(true);
    expect(railView(s, PLACES, 'p-a').steps[2].detail).not.toBe('모두 통과');
  });
  it('FR12B 다른 명령이 진행 중인 BUSY는 점유와 다르게 쓴다', () => {
    const s = SCENES.flow();
    const steps = s.request!.steps.map((x, i) => (i === 5 ? { result: 'stop' as const } : i === 6 ? { result: 'skip' as const } : x));
    s.request = { ...s.request!, verdict: 'BUSY', reason: 'COMMAND_IN_PROGRESS', execution: undefined, steps };
    expect(checksOf(s)[3]).toEqual(['fail', '다른 명령 실행 중']);
  });
  it('FR12B 승인 필요 사유를 구분해서 보인다', () => {
    const s = SCENES.approval();
    s.request = { ...s.request!, reason: 'APPROVAL_PENDING', accessRequestedAt: undefined };
    expect(railView(s, PLACES, 'p-b').steps[4]).toMatchObject({ mark: '승인 기다림', code: 'NEED_APPROVAL · APPROVAL_PENDING' });
  });
  it('FR12B 서버 오류로 멈춘 칸은 단계 이유 대신 서버 오류라고 쓴다', () => {
    const s = SCENES.flow();
    const steps = s.request!.steps.map((x, i) => (i === 4 ? { result: 'stop' as const } : i > 4 ? { result: 'skip' as const } : x));
    s.request = { ...s.request!, verdict: 'DENY', reason: 'INTERNAL_ERROR', execution: undefined, steps };
    expect(checksOf(s)[2]).toEqual(['fail', '서버 오류']);
  });
});

describe('railView 판정 전', () => {
  it('FR12B 판정 전에는 3단계가 판정 중이고, 확인 칸은 비어 있다', () => {
    for (const t of ['14:28:10.01', '14:28:10.09', '14:26:12.09']) {
      const s = stateAt(sec(t));
      const v = railView(s, PLACES, 'p-a');
      expect(v.steps.map((x) => x.status)).toEqual(['done', 'done', 'current', 'todo', 'todo']);
      expect(v.steps[2]).toEqual({ status: 'current', tone: 'accent', mark: '판정 중' });
      expect(checksOf(s).every(([c]) => c === 'none')).toBe(true);
    }
  });
  it('FR12B 승인 신청이 만료되면 5단계가 만료를 말한다', () => {
    const s = stateAt(sec('14:41:01'));
    expect(railView(s, PLACES, 'p-b').steps[4]).toMatchObject({ mark: '신청 만료', code: 'APPROVAL_REQUEST_EXPIRED' });
  });
});

describe('railView 명령 전', () => {
  it('FR12B 판정은 OK지만 명령 이벤트가 오기 전에는 허가로 보이고, 명령이 오면 실행 중', () => {
    expect(railView(stateAt(sec('14:28:10.205')), PLACES, 'p-a').steps[3]).toMatchObject({ status: 'current', mark: '허가' });
    expect(railView(stateAt(sec('14:28:10.215')), PLACES, 'p-a').steps[3]).toMatchObject({ status: 'current', mark: '실행 중' });
  });
});

describe('railView 승인 신청의 끝', () => {
  /** 승인 대기 장면에서 소유자가 답했거나 기한이 지나 신청이 끝난 상태 */
  const ended = (accessEnd?: 'approved' | 'denied' | 'expired') => {
    const s = SCENES.approval();
    s.request = { ...s.request!, accessExpiredAt: '14:31:30', ...(accessEnd ? { accessEnd } : {}) };
    s.ownerNotified = false;
    return s;
  };
  const fifth = (s: MonitorState) => railView(s, PLACES, 'p-b').steps[4];

  it('FR12B 소유자가 승인하면 승인됨 · 다시 요청하면 실행이라고 쓴다 (승인은 실행 허가가 아니다)', () => {
    expect(fifth(ended('approved'))).toEqual({ status: 'current', tone: 'appr', mark: '승인됨', detail: '다시 요청하면 실행', code: 'approved' });
  });
  it('FR12B 소유자가 거절하면 거절됨 · 소유자가 거절이라고 쓴다', () => {
    expect(fifth(ended('denied'))).toEqual({ status: 'current', tone: 'appr', mark: '거절됨', detail: '소유자가 거절', code: 'APPROVAL_REJECTED' });
  });
  it('FR12B 소유자 응답 없이 만료되면 신청 만료 · 소유자 응답 없음이라고 쓰고, 끝난 까닭을 모르는 요청(조회 REST)도 만료로 읽는다', () => {
    const expired = { status: 'current', tone: 'appr', mark: '신청 만료', detail: '소유자 응답 없음', code: 'APPROVAL_REQUEST_EXPIRED' };
    expect(fifth(ended('expired'))).toEqual(expired);
    expect(fifth(ended())).toEqual(expired);
  });
  it('FR12B 승인을 기다리는 동안에는 끝난 까닭을 쓰지 않는다', () => {
    expect(fifth(SCENES.approval())).toMatchObject({ mark: '승인 기다림', code: 'NEED_APPROVAL → request_access' });
  });
});

describe('railView 실행 거절', () => {
  const rejected = () => {
    const s = SCENES.flow();
    s.request = { ...s.request!, execution: 'rejected' };
    return s;
  };

  it('FR12B 기기가 실행 전에 거절하면 거절됨이다. 고장 격리가 아니다', () => {
    const v = railView(rejected(), PLACES, 'p-a');
    expect(v.steps[3]).toMatchObject({ status: 'done', detail: '열기 명령 보냄' });
    expect(v.steps[4]).toEqual({ status: 'current', tone: 'busy', mark: '거절됨', detail: '기기가 실행 전에 거절', code: 'rejected' });
    expect(JSON.stringify(v.steps)).not.toMatch(/고장 격리|FAULT|EXEC_/);
  });
  it('FR12B 실행 결과마다 5단계가 갈린다: 완료, 실패와 결과 불명은 고장 격리, 거절은 거절됨', () => {
    const mark = (execution: 'succeeded' | 'failed' | 'unknown' | 'rejected') => {
      const s = SCENES.flow();
      s.request = { ...s.request!, execution };
      const step = railView(s, PLACES, 'p-a').steps[4];
      return [step.mark, step.code];
    };
    expect(mark('succeeded')).toEqual(['완료', 'OK']);
    expect(mark('failed')).toEqual(['고장 격리', 'EXEC_FAILED → FAULT']);
    expect(mark('unknown')).toEqual(['고장 격리', 'EXEC_UNKNOWN → FAULT']);
    expect(mark('rejected')).toEqual(['거절됨', 'rejected']);
  });
});

describe('railView 여러 장소', () => {
  it('FR12B 라운지가 주 장소여도 옆방 기기의 이름으로 레일과 서버 랙을 채운다', () => {
    const s = SCENES['side-room']();
    expect(railView(s, PLACES, 'p-a')).toMatchObject({ mode: 'live', title: '방문자 A · 조명 켜기' });
    expect(railView(s, PLACES, 'p-a').steps[3]).toMatchObject({ tone: 'ok', mark: '실행 중', detail: '무드등에 켜기 명령' });
    expect(checksOf(s)[1]).toEqual(['pass', '무드등 앞']);
    // 공용형인 무드등은 점유 칸을 생략한다
    expect(checksOf(s)[3]).toEqual(['none', '공용형 생략']);
  });

  it('FR12B 고른 기기가 없으면 공용 판정은 요청 장소의 같은 기능 기기로 정한다', () => {
    const place = (id: string, occupancy: 'rental' | 'shared'): PlaceInfo =>
      ({ placeId: id, placeName: id, modelVersion: 'v', calibrationVersion: 'c', devices: [{ nodeId: `${id}-1`, name: `${id} 등`, capability: 'light', occupancy }] });
    const places = [place('a', 'rental'), place('b', 'shared')];
    const s = SCENES.flow();
    s.request = { ...s.request!, verdict: 'DENY', reason: 'POLICY_DENIED', nodeId: undefined, execution: undefined, target: { kind: 'nearest', capability: 'light' }, steps: Array(7).fill({ result: 'skip' }) };
    expect(serverChecks(s.request, places, 'b')[3]).toEqual(['none', '공용형 생략']);
    expect(serverChecks(s.request, places, 'a')[3]).toEqual(['none', '비었는지']);
    // 요청 장소를 모르면 모든 장소의 첫 같은 기능 기기를 쓴다
    expect(serverChecks(s.request, places)[3]).toEqual(['none', '비었는지']);
  });
});

// D3: 폰 미러링 자동 따라가기
import { describe, expect, it } from 'vitest';
import { SCENES } from '../data/mock/fixtures';
import { choosePhone } from './phone';

const ON = { followRequester: true, followApprover: true };

describe('choosePhone', () => {
  it('FR12B D3 요청이 있으면 요청한 사람을 따라간다', () => {
    expect(choosePhone(SCENES.fault(), ON, 'p-a')).toMatchObject({ personId: 'p-b', who: '방문자 B', follow: '요청한 사람을 따라가는 중' });
  });
  it('FR12B D3 승인 알림이 가면 소유자로 바꾼다', () => {
    expect(choosePhone(SCENES.approval(), ON, 'p-b')).toMatchObject({ personId: 'p-owner', follow: '승인할 사람으로 자동 전환됨' });
  });
  it('FR12B D3 승인자 따라가기를 끄면 요청자를 그대로 보인다', () => {
    expect(choosePhone(SCENES.approval(), { ...ON, followApprover: false }, 'p-b').personId).toBe('p-b');
  });
  it('FR12B D3 자동 전환을 모두 끄면 보던 사람을 유지한다', () => {
    expect(choosePhone(SCENES.flow(), { followRequester: false, followApprover: false }, 'p-b')).toMatchObject({ personId: 'p-b', follow: '자동 전환 꺼짐' });
  });
  it('FR12B D3 대기 중에는 보던 사람, 처음에는 첫 방문자', () => {
    expect(choosePhone(SCENES.idle(), ON, undefined).personId).toBe('p-a');
    expect(choosePhone(SCENES.idle(), ON, 'p-b').personId).toBe('p-b');
  });
  it('FR12B D3 승인 알림 뒤 요청이 끝나면 소유자에 머물지 않는다', () => {
    expect(choosePhone(SCENES.later(), ON, 'p-owner').personId).toBe('p-a');
  });
  it('FR12B D3 요청자 따라가기만 끄면 문구가 승인 알림만 따른다고 말한다', () => {
    expect(choosePhone(SCENES.flow(), { followRequester: false, followApprover: true }, 'p-b')).toMatchObject({ personId: 'p-b', follow: '승인 알림이 오면 소유자로 바꿔요' });
  });
});

describe('목 폰 화면', () => {
  it('FR12B D3 어느 장면에서도 보이는 사람마다 목 화면이 있다', () => {
    for (const make of Object.values(SCENES)) {
      const s = make();
      for (const p of s.people) expect(s.mockPhones?.some((m) => m.personId === p.id)).toBe(true);
    }
  });
});

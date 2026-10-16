// D3: 폰 미러링 자동 따라가기
import { describe, expect, it } from 'vitest';
import { SCENES } from '../data/mock/fixtures';
import { choosePhone } from './phone';

const ON = { followRequester: true, followApprover: true };

describe('choosePhone', () => {
  it('D3 요청이 있으면 요청한 사람을 따라간다', () => {
    expect(choosePhone(SCENES.fault(), ON, 'p-a')).toMatchObject({ personId: 'p-b', who: '방문자 B', follow: '요청한 사람을 따라가는 중' });
  });
  it('D3 승인 알림이 가면 소유자로 바꾼다', () => {
    expect(choosePhone(SCENES.approval(), ON, 'p-b')).toMatchObject({ personId: 'p-owner', follow: '승인할 사람으로 자동 전환됨' });
  });
  it('D3 승인자 따라가기를 끄면 요청자를 그대로 보인다', () => {
    expect(choosePhone(SCENES.approval(), { ...ON, followApprover: false }, 'p-b').personId).toBe('p-b');
  });
  it('D3 자동 전환을 모두 끄면 보던 사람을 유지한다', () => {
    expect(choosePhone(SCENES.flow(), { followRequester: false, followApprover: false }, 'p-b')).toMatchObject({ personId: 'p-b', follow: '자동 전환 꺼짐' });
  });
  it('D3 대기 중에는 보던 사람, 처음에는 첫 방문자', () => {
    expect(choosePhone(SCENES.idle(), ON, undefined).personId).toBe('p-a');
    expect(choosePhone(SCENES.idle(), ON, 'p-b').personId).toBe('p-b');
  });
});

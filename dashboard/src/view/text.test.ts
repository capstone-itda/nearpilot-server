import { describe, expect, it } from 'vitest';
import { actionName, actionVerb, hasBatchim, josa, kindName, reasonText, requestTitle, toolCall } from './text';

describe('text', () => {
  it('FR12B 받침: 한글과 숫자 읽기', () => {
    expect(josa('사물함 1', '이', '가')).toBe('사물함 1이');
    expect(josa('사물함 2', '이', '가')).toBe('사물함 2가');
    expect(josa('조명', '은', '는')).toBe('조명은');
    expect(josa('스피커', '은', '는')).toBe('스피커는');
    expect(hasBatchim('방문자 A')).toBe(false);
  });
  it('FR12B 도구 호출 표기', () => {
    expect(toolCall({ tool: 'request_use', target: { kind: 'nearest', deviceKind: 'light' }, action: 'on' })).toBe('request_use(nearest:light, on)');
    expect(toolCall({ tool: 'request_use', target: { kind: 'explicit', nodeId: 'locker-2' }, action: 'open' })).toBe('request_use(locker-2, open)');
  });
  it('FR12A 아는 종류·동작은 쉬운 말, 기기가 선언한 다른 이름은 그대로 보인다', () => {
    expect([kindName('locker'), kindName('light'), kindName('speaker'), kindName('fan')]).toEqual(['사물함', '조명', '스피커', 'fan']);
    expect([actionName('open'), actionName('close'), actionName('on'), actionName('off'), actionName('spin')]).toEqual(['열기', '닫기', '켜기', '끄기', 'spin']);
    expect([actionVerb('close'), actionVerb('spin')]).toEqual(['닫아요', 'spin 실행해요']);
    // Object의 속성 이름은 아는 이름이 아니다
    expect([kindName('constructor'), actionName('toString')]).toEqual(['constructor', 'toString']);
    expect(requestTitle({ target: { kind: 'any', deviceKind: 'fan' }, action: 'spin' }, (id) => id)).toBe('fan spin');
    expect(toolCall({ tool: 'request_use', target: { kind: 'nearest', deviceKind: 'fan' }, action: 'spin' })).toBe('request_use(nearest:fan, spin)');
  });
  it('FR12A 선언 기반 판정의 거부 사유를 서버 문구와 같게 보인다', () => {
    expect(reasonText('UNSUPPORTED_ACTION')).toBe('기기가 선언하지 않은 동작');
    expect(reasonText('PARAMETER_OUT_OF_RANGE')).toBe('인자가 기기의 상한을 넘음');
    expect(reasonText('UNSUPPORTED_PARAMETER')).toBe('기기가 받은 인자를 지원하지 않음');
  });
});

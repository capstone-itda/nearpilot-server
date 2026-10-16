import { describe, expect, it } from 'vitest';
import { hasBatchim, josa, toolCall } from './text';

describe('text', () => {
  it('받침: 한글과 숫자 읽기', () => {
    expect(josa('사물함 1', '이', '가')).toBe('사물함 1이');
    expect(josa('사물함 2', '이', '가')).toBe('사물함 2가');
    expect(josa('조명', '은', '는')).toBe('조명은');
    expect(josa('스피커', '은', '는')).toBe('스피커는');
    expect(hasBatchim('방문자 A')).toBe(false);
  });
  it('도구 호출 표기', () => {
    expect(toolCall({ tool: 'request_use', target: { kind: 'nearest', capability: 'light' }, action: 'on' })).toBe('request_use(nearest:light, on)');
    expect(toolCall({ tool: 'request_use', target: { kind: 'explicit', nodeId: 'locker-2' }, action: 'open' })).toBe('request_use(locker-2, open)');
  });
});

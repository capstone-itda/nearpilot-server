// FR-12A: 대시보드는 서버에 쓰지 않는다
import { readdirSync, readFileSync, statSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';
import { createMockSource } from './mock/mockSource';

const files = (dir: string): string[] => readdirSync(dir).flatMap((f) => {
  const p = join(dir, f);
  return statSync(p).isDirectory() ? files(p) : /\.(ts|tsx)$/.test(f) && !f.endsWith('.test.ts') ? [p] : [];
});

describe('쓰기 없음', () => {
  it('FR12A 데이터 소스에는 구독, 다시 연결, 조회 함수만 있다', () => {
    const keys = Object.keys(createMockSource({ scene: 'idle' })).sort();
    expect(keys).toEqual(['approvals', 'audit', 'health', 'history', 'place', 'reconnect', 'rental', 'request', 'subscribe', 'trust', 'uses'].sort());
  });
  it('FR12A 앱 코드에 쓰기 요청이 없다', () => {
    const src = join(__dirname, '..');
    for (const f of files(src)) {
      const text = readFileSync(f, 'utf8');
      expect(text, f).not.toMatch(/method\s*:\s*['"](POST|PUT|PATCH|DELETE)['"]/i);
      expect(text, f).not.toMatch(/\bnew\s+WebSocket\b[^;]*\.send\(|XMLHttpRequest/);
    }
  });
});

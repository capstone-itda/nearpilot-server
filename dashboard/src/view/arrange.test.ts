// D4: 무대의 표시용 배치는 이 PC에서 정하고, 격자에 맞추며, 겹치거나 방을 벗어나지 않는다
import { describe, expect, it } from 'vitest';
import { PLACE } from '../data/mock/fixtures';
import { canPlace, parseFile, resolveArrangement, toFile } from './arrange';

describe('표시용 배치', () => {
  const base = resolveArrangement(PLACE.devices);

  it('FR12A D4 기본 배치: 대여형은 위쪽, 공용형은 아래쪽 줄에 모두 놓인다', () => {
    expect(Object.keys(base).sort()).toEqual(PLACE.devices.map((d) => d.nodeId).sort());
    expect(base['locker-1'].y).toBeLessThan(base['light-1'].y);
    for (const [id, s] of Object.entries(base)) expect(canPlace(base, id, s)).toBe(true);
  });
  it('FR12A D4 격자에 맞지 않거나 방 밖이거나 다른 기기와 겹치면 놓을 수 없다', () => {
    const s = base['locker-2'];
    expect(canPlace(base, 'locker-1', { x: s.x + 1, y: s.y })).toBe(false);
    expect(canPlace(base, 'locker-1', s)).toBe(false);
    expect(canPlace(base, 'locker-1', { x: 0, y: 0 })).toBe(false);
    expect(canPlace(base, 'locker-1', { x: 1104, y: s.y })).toBe(true);
  });
  it('FR12A D4 저장한 배치를 지금 기기 목록에 맞춘다: 없는 기기는 빼고, 겹치는 자리는 다시 고른다', () => {
    const saved = { ...base, gone: { x: 600, y: 360 }, 'locker-1': { ...base['locker-2'] } };
    const r = resolveArrangement(PLACE.devices, saved);
    expect(r.gone).toBeUndefined();
    expect(r['locker-1']).not.toEqual(r['locker-2']);
    for (const [id, s] of Object.entries(r)) expect(canPlace(r, id, s)).toBe(true);
  });
  it('FR12A D4 파일로 내보낸 배치를 다시 읽고, 다른 형식은 이유와 함께 거절한다', () => {
    const back = parseFile(JSON.stringify(toFile('공용 라운지', base)));
    expect(back).toEqual({ ok: true, devices: base, place: '공용 라운지' });
    expect(parseFile('{')).toEqual({ ok: false, error: 'JSON 파일이 아니에요' });
    expect(parseFile('{"kind":"x"}')).toEqual({ ok: false, error: 'NearPilot 배치 파일이 아니에요' });
  });
});

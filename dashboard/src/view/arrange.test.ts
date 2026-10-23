// D4: 무대의 표시용 배치는 이 PC에서 정하고, 격자에 맞추며, 겹치거나 방을 벗어나지 않는다
import { describe, expect, it } from 'vitest';
import { LOUNGE, SIDE_ROOM, WEAK_RSSI_DBM } from '../data/mock/fixtures';
import { applyFile, canPlace, parseFile, resolveArrangement, toFile, withArrangement } from './arrange';

describe('표시용 배치', () => {
  const base = resolveArrangement(LOUNGE.devices);

  it('FR12A D4 기본 배치: 대여형은 위쪽, 공용형은 아래쪽 줄에 모두 놓인다', () => {
    expect(Object.keys(base).sort()).toEqual(LOUNGE.devices.map((d) => d.nodeId).sort());
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
    const r = resolveArrangement(LOUNGE.devices, saved);
    expect(r.gone).toBeUndefined();
    expect(r['locker-1']).not.toEqual(r['locker-2']);
    for (const [id, s] of Object.entries(r)) expect(canPlace(r, id, s)).toBe(true);
  });
  it('FR12A D4 파일로 내보낸 배치를 다시 읽고, 다른 형식은 이유와 함께 거절한다', () => {
    const back = parseFile(JSON.stringify(toFile('lounge', base)));
    expect(back).toEqual({ ok: true, devices: base, place: 'lounge' });
    expect(parseFile('{')).toEqual({ ok: false, error: 'JSON 파일이 아니에요' });
    expect(parseFile('{"kind":"x"}')).toEqual({ ok: false, error: 'NearPilot 배치 파일이 아니에요' });
  });
  it('FR12A D4 배치 파일의 place는 placeId이고 장소 이름은 placeName으로 함께 적는다. 이름이 없는 파일도 읽는다', () => {
    const file = toFile('lounge', base, '공용 라운지');
    expect(file).toMatchObject({ kind: 'nearpilot-arrangement', version: 1, place: 'lounge', placeName: '공용 라운지' });
    expect(parseFile(JSON.stringify(file))).toEqual({ ok: true, devices: base, place: 'lounge', placeName: '공용 라운지' });
    expect(toFile('lounge', base)).not.toHaveProperty('placeName');
  });
  it('FR12A D4 장소 정보에 배치와 약한 신호 기준을 입힌다. 자리가 없는 기기는 그리지 않는다', () => {
    const layout = withArrangement(LOUNGE, base, WEAK_RSSI_DBM);
    expect(layout).toMatchObject({ placeId: 'lounge', placeName: '공용 라운지', modelVersion: 'v3', calibrationVersion: 'cal-2', weakRssiDbm: WEAK_RSSI_DBM });
    expect(layout.devices.map((d) => [d.nodeId, d.spot])).toEqual(LOUNGE.devices.map((d) => [d.nodeId, base[d.nodeId]]));
    expect(withArrangement(LOUNGE, { 'locker-1': base['locker-1'] }, WEAK_RSSI_DBM).devices.map((d) => d.nodeId)).toEqual(['locker-1']);
  });
  it('FR12A D4 다른 장소의 파일은 이름이 같은 기기만 옮긴다', () => {
    const side = resolveArrangement(SIDE_ROOM.devices);
    const lounge = parseFile(JSON.stringify(toFile('lounge', base, '공용 라운지')));
    if (!lounge.ok) throw new Error(lounge.error);
    // 옆방 기기 목록에 라운지 파일의 기기가 없다
    expect(applyFile(side, SIDE_ROOM.devices, lounge.devices)).toEqual({ ok: true, next: side, moved: 0, unknown: LOUNGE.devices.length });
    // 같은 이름(nodeId)의 기기가 있으면 옮긴다
    const moved = applyFile(side, SIDE_ROOM.devices, { ...lounge.devices, 'mood-1': { x: 600, y: 720 } });
    expect(moved).toEqual({ ok: true, next: { 'mood-1': { x: 600, y: 720 } }, moved: 1, unknown: LOUNGE.devices.length });
  });
  it('FR12A D4 불러온 자리가 한 대라도 놓을 수 없으면 배치를 바꾸지 않고, 놓을 수 있으면 옮긴 수와 모르는 기기 수를 센다', () => {
    const name = (id: string) => LOUNGE.devices.find((d) => d.nodeId === id)!.name;
    expect(applyFile(base, LOUNGE.devices, { 'locker-1': { x: 0, y: 0 } })).toEqual({ ok: false, outside: [name('locker-1')], overlap: [] });
    expect(applyFile(base, LOUNGE.devices, { 'locker-1': { ...base['locker-2'] } })).toMatchObject({ ok: false, outside: [], overlap: [name('locker-1')] });
    expect(parseFile(JSON.stringify({ kind: 'nearpilot-arrangement', version: 1, place: '', devices: { 'locker-1': { x: '1', y: null } } }))).toEqual({ ok: false, error: '좌표가 숫자가 아닌 기기가 있어요 (locker-1)' });
    expect(applyFile(base, LOUNGE.devices, { 'locker-1': { ...base['locker-2'] } }).ok).toBe(false);
    const swap = applyFile(base, LOUNGE.devices, { 'locker-1': base['locker-2'], 'locker-2': base['locker-1'], gone: { x: 600, y: 360 } });
    expect(swap).toEqual({ ok: true, next: { ...base, 'locker-1': base['locker-2'], 'locker-2': base['locker-1'] }, moved: 2, unknown: 1 });
  });
});

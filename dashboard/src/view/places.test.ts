// FR-12B: 사람 이동과 요청으로 무대의 주 장소와 작은 방을 고른다
import { describe, expect, it } from 'vitest';
import { LOUNGE, PLACES, SIDE_ROOM, WEAK_RSSI_DBM } from '../data/mock/fixtures';
import { SCENES } from '../data/mock/story';
import { initialState } from '../data/reduce';
import type { MonitorState, Person, PlaceInfo, RequestProgress } from '../model/types';
import { resolveArrangement, withArrangement } from './arrange';
import { nextFocus, placeOfNode, requestPlace } from './places';
import { stageView } from './stage';

/** near가 없으면 비콘을 받지 않는 사람 */
const person = (id: string, near?: string): Person => ({
  id, pseudonym: `방문자 ${id.toUpperCase()}`, letter: id.toUpperCase(), role: 'visitor',
  beaconSeen: near !== undefined, rssi: near ? { [near]: -50 } : {}, near
});
const request = (r: Partial<RequestProgress> = {}): RequestProgress => ({
  id: 'r-1', personId: 'a', tool: 'request_use', target: { kind: 'nearest', capability: 'light' }, action: 'on', at: '14:00:00', steps: [], ...r
});
const state = (people: Person[], r?: RequestProgress): MonitorState => ({ ...initialState(PLACES), people, request: r });

describe('placeOfNode', () => {
  it('FR12B 기기가 속한 장소를 찾고, 모르는 기기는 장소가 없다', () => {
    expect(placeOfNode(PLACES, 'locker-2')).toBe('lounge');
    expect(placeOfNode(PLACES, 'mood-1')).toBe('side-room');
    expect(placeOfNode(PLACES, 'nobody')).toBeUndefined();
    expect(placeOfNode(PLACES, undefined)).toBeUndefined();
  });
});

describe('requestPlace', () => {
  it('FR12B 대상 기기가 있으면 요청한 사람이 어디 있든 그 기기의 장소다', () => {
    expect(requestPlace(state([person('a', 'locker-1')], request({ target: { kind: 'explicit', nodeId: 'mood-1' } })), PLACES)).toBe('side-room');
    expect(requestPlace(state([person('a', 'mood-1')], request({ target: { kind: 'explicit', nodeId: 'light-1' } })), PLACES)).toBe('lounge');
  });

  it('FR12B 대상 기기가 없으면 요청한 사람의 장소다', () => {
    expect(requestPlace(state([person('a', 'mood-1')], request()), PLACES)).toBe('side-room');
    expect(requestPlace(state([person('a', 'locker-1')], request()), PLACES)).toBe('lounge');
  });

  it('FR12B 요청한 사람의 장소를 모르면 판정 기기, 그다음 후보 확률이 가장 큰 기기의 장소다', () => {
    const away = person('a');
    expect(requestPlace(state([away], request({ nodeId: 'mood-1', posteriors: { 'light-1': 0.9 } })), PLACES)).toBe('side-room');
    expect(requestPlace(state([away], request({ posteriors: { 'light-1': 0.2, 'mood-1': 0.7 } })), PLACES)).toBe('side-room');
    expect(requestPlace(state([away], request({ posteriors: { 'light-1': 0.7, 'mood-1': 0.2 } })), PLACES)).toBe('lounge');
  });

  it('FR12B 비콘을 받지 않는 사람의 near는 장소가 아니다', () => {
    const stale: Person = { ...person('a'), near: 'mood-1' };
    expect(requestPlace(state([stale], request()), PLACES)).toBeUndefined();
  });

  it('FR12B 정할 수 없으면(원격 요청) 장소가 없고, 요청이 없어도 없다', () => {
    expect(requestPlace(state([person('a')], request()), PLACES)).toBeUndefined();
    expect(requestPlace(state([person('a', 'mood-1')]), PLACES)).toBeUndefined();
  });
});

describe('nextFocus', () => {
  it('FR12B 요청 없이 사람이 옆방으로 옮기면 주 장소가 옆방이고, 라운지에 남은 사람이 있으면 작은 방이 라운지다', () => {
    const before = nextFocus(undefined, state([person('a', 'locker-1'), person('c', 'locker-3')]), PLACES);
    expect(before.primary).toBe('lounge');
    expect(before.inset).toBeUndefined();
    const moved = nextFocus(before, state([person('a', 'mood-1'), person('c', 'locker-3')]), PLACES);
    expect(moved).toMatchObject({ primary: 'side-room', inset: 'lounge', personAt: { a: 'side-room', c: 'lounge' } });
  });

  it('FR12B 라운지에 남은 사람이 없으면 작은 방이 없다', () => {
    const before = nextFocus(undefined, state([person('a', 'locker-1')]), PLACES);
    const moved = nextFocus(before, state([person('a', 'mood-1')]), PLACES);
    expect(moved.primary).toBe('side-room');
    expect(moved.inset).toBeUndefined();
    // 남은 사람이 비콘을 보내지 않게 되어도 주 장소는 그대로다
    const gone = nextFocus(moved, state([person('a', 'mood-1'), person('c')]), PLACES);
    expect(gone.primary).toBe('side-room');
    expect(gone.inset).toBeUndefined();
  });

  it('FR12B 아무도 옮기지 않으면 주 장소는 그대로다. 다른 장소에 사람이 있어도 바뀌지 않는다', () => {
    const before = nextFocus(undefined, state([person('b', 'locker-1')]), PLACES);
    const moved = nextFocus(before, state([person('b', 'locker-1'), person('a', 'mood-1')]), PLACES);
    expect(moved.primary).toBe('side-room');
    const again = nextFocus(moved, state([person('b', 'locker-1'), person('a', 'mood-1')]), PLACES);
    expect(again).toMatchObject({ primary: 'side-room', inset: 'lounge' });
  });

  it('FR12B 앞 계산이 있으면 처음 나타난 사람의 장소도 주 장소가 된다', () => {
    const before = nextFocus(undefined, state([person('a', 'locker-1')]), PLACES);
    const after = nextFocus(before, state([person('a', 'locker-1'), person('c', 'mood-1')]), PLACES);
    expect(after).toMatchObject({ primary: 'side-room', inset: 'lounge' });
  });

  it('FR12B 앞 계산이 없으면(화면을 새로 엶) 옮긴 사람이 없어 사람이 있는 첫 장소가 주 장소다. 아무도 없으면 첫 장소다', () => {
    // 사람 순서가 아니라 장소 목록 순서의 첫 장소다 (옛 계산은 목록 마지막 사람의 장소 side-room)
    expect(nextFocus(undefined, state([person('b', 'locker-1'), person('a', 'mood-1')]), PLACES).primary).toBe('lounge');
    expect(nextFocus(undefined, state([person('a', 'mood-1'), person('b', 'locker-1')]), PLACES).primary).toBe('lounge');
    expect(nextFocus(undefined, state([person('a', 'mood-1')]), PLACES).primary).toBe('side-room');
    expect(nextFocus(undefined, state([person('a'), person('b')]), PLACES).primary).toBe('lounge');
    expect(nextFocus(undefined, state([]), PLACES).primary).toBe('lounge');
  });

  it('FR12B 비콘이 끊겼다가 같은 장소로 돌아온 사람은 옮긴 사람이 아니라 주 장소가 그대로다', () => {
    const lounge = nextFocus(undefined, state([person('b', 'locker-1')]), PLACES);
    const side = nextFocus(lounge, state([person('b', 'locker-1'), person('a', 'mood-1')]), PLACES);
    expect(side).toMatchObject({ primary: 'side-room', inset: 'lounge' });
    // b의 비콘이 끊긴다: 장소는 앞 값을 이어받고 작은 방은 비콘을 받는 사람만 센다
    const dropped = nextFocus(side, state([person('b'), person('a', 'mood-1')]), PLACES);
    expect(dropped).toMatchObject({ primary: 'side-room', personAt: { a: 'side-room', b: 'lounge' } });
    expect(dropped.inset).toBeUndefined();
    // b가 같은 locker-1로 돌아온다
    const back = nextFocus(dropped, state([person('b', 'locker-1'), person('a', 'mood-1')]), PLACES);
    expect(back).toMatchObject({ primary: 'side-room', inset: 'lounge' });
  });

  it('FR12B 비콘이 끊겼다가 다른 장소로 돌아온 사람은 옮긴 사람이다', () => {
    const lounge = nextFocus(undefined, state([person('b', 'locker-1')]), PLACES);
    const side = nextFocus(lounge, state([person('b', 'locker-1'), person('a', 'mood-1')]), PLACES);
    const dropped = nextFocus(side, state([person('b', 'locker-1'), person('a')]), PLACES);
    expect(dropped.primary).toBe('side-room');
    // a가 옆방에서 끊겼다가 라운지의 locker-1 앞으로 돌아온다
    expect(nextFocus(dropped, state([person('b', 'locker-1'), person('a', 'locker-1')]), PLACES)).toMatchObject({ primary: 'lounge', personAt: { a: 'lounge' } });
  });

  it('FR12B 비콘이 끊긴 사람의 앞 장소는 목록에 있을 때만 이어받는다', () => {
    const prev = { primary: 'lounge', anchor: 'lounge', personAt: { a: 'attic', b: 'side-room' } };
    const f = nextFocus(prev, state([person('a'), person('b')]), PLACES);
    expect(f.personAt).toEqual({ b: 'side-room' });
    // 이어받은 장소는 기억일 뿐이라 작은 방을 만들지 않는다
    expect(f.inset).toBeUndefined();
    expect(f.primary).toBe('lounge');
  });

  it('FR12B 옮긴 사람이 여럿이면 목록 순서의 마지막 사람의 장소다', () => {
    const before = nextFocus(undefined, state([person('a', 'locker-1'), person('b', 'mood-1')]), PLACES);
    // a는 옆방으로, b는 라운지로 옮겼다
    const a = person('a', 'mood-1');
    const b = person('b', 'locker-1');
    expect(nextFocus(before, state([a, b]), PLACES).primary).toBe('lounge');
    expect(nextFocus(before, state([b, a]), PLACES).primary).toBe('side-room');
  });

  it('FR12B 진행 중인 요청이 있으면 다른 사람이 옮겨도 주 장소는 요청 장소다', () => {
    const asking = request({ personId: 'b' });
    const before = nextFocus(undefined, state([person('a', 'locker-1'), person('b', 'light-1')], asking), PLACES);
    expect(before.primary).toBe('lounge');
    const after = nextFocus(before, state([person('a', 'mood-1'), person('b', 'light-1')], asking), PLACES);
    expect(after).toMatchObject({ primary: 'lounge', inset: 'side-room' });
  });

  it('FR12B 다른 장소의 기기에 요청이 오면 주 장소를 바로 요청 장소로 바꾼다', () => {
    const before = nextFocus(undefined, state([person('a', 'locker-1'), person('b', 'light-1')]), PLACES);
    const after = nextFocus(before, state([person('a', 'locker-1'), person('b', 'light-1')], request({ personId: 'a', target: { kind: 'explicit', nodeId: 'mood-1' } })), PLACES);
    expect(after).toMatchObject({ primary: 'side-room', inset: 'lounge' });
  });

  it('FR12B 원격 요청(장소를 정할 수 없음)이면 주 장소가 그대로다', () => {
    const moved = nextFocus(nextFocus(undefined, state([person('a', 'locker-1')]), PLACES), state([person('a', 'mood-1')]), PLACES);
    expect(moved.primary).toBe('side-room');
    const remote = nextFocus(moved, state([person('a', 'mood-1'), person('b')], request({ personId: 'b' })), PLACES);
    expect(remote.primary).toBe('side-room');
  });

  it('FR12B 요청이 끝나 내려가도 주 장소는 그대로다', () => {
    const asked = nextFocus(undefined, state([person('a', 'mood-1')], request()), PLACES);
    expect(asked.primary).toBe('side-room');
    expect(nextFocus(asked, state([person('a', 'mood-1')]), PLACES).primary).toBe('side-room');
  });

  it('FR12B 잠금이면 요청과 이동에도 주 장소가 그대로이고, 다른 장소에 사람이 있으면 작은 방이 그 장소다', () => {
    const before = nextFocus(undefined, state([person('a', 'locker-1')]), PLACES, 'lounge');
    const moved = state([person('a', 'mood-1')], request({ personId: 'a' }));
    expect(nextFocus(before, moved, PLACES, 'lounge')).toMatchObject({ primary: 'lounge', inset: 'side-room' });
    // 다른 장소에 사람이 없으면 작은 방도 없다
    const none = nextFocus(before, state([person('a', 'locker-1')], request({ target: { kind: 'explicit', nodeId: 'mood-1' } })), PLACES, 'lounge');
    expect(none.primary).toBe('lounge');
    expect(none.inset).toBeUndefined();
    // 옆방으로 잠그면 라운지에 있는 사람이 작은 방이다
    expect(nextFocus(before, state([person('a', 'locker-1')]), PLACES, 'side-room')).toMatchObject({ primary: 'side-room', inset: 'lounge' });
  });

  it('FR12B 장소 목록에 없는 잠금은 쓰지 않는다', () => {
    expect(nextFocus(undefined, state([person('a', 'mood-1')]), PLACES, 'attic').primary).toBe('side-room');
  });

  it('FR12B 고정 중에도 주 장소를 이어 계산해 두고, 고정을 풀면 그 장소가 주 장소다', () => {
    const start = nextFocus(undefined, state([person('a', 'locker-1')]), PLACES, 'lounge');
    expect(start).toMatchObject({ primary: 'lounge', anchor: 'lounge' });
    // 라운지에 고정한 채 사람이 옆방으로 옮긴다: 보이는 주 장소는 그대로, 계산해 둔 주 장소는 옆방
    const moved = nextFocus(start, state([person('a', 'mood-1')]), PLACES, 'lounge');
    expect(moved).toMatchObject({ primary: 'lounge', anchor: 'side-room', inset: 'side-room' });
    // 고정을 풀면 옮긴 사람의 장소다. 한 번 더 계산해도 그대로다
    const freed = nextFocus(moved, state([person('a', 'mood-1')]), PLACES);
    expect(freed).toMatchObject({ primary: 'side-room', anchor: 'side-room' });
    expect(nextFocus(freed, state([person('a', 'mood-1')]), PLACES).primary).toBe('side-room');
  });

  it('FR12B 고정 중에 온 요청의 장소도 이어 계산해 두어, 요청이 내려간 뒤 고정을 풀면 그 장소다', () => {
    const start = nextFocus(undefined, state([person('a', 'locker-1')]), PLACES, 'lounge');
    const asking = nextFocus(start, state([person('a', 'locker-1')], request({ target: { kind: 'explicit', nodeId: 'mood-1' } })), PLACES, 'lounge');
    expect(asking).toMatchObject({ primary: 'lounge', anchor: 'side-room' });
    // 요청이 내려간 뒤에도 계산해 둔 주 장소는 그대로이고, 고정을 풀면 그 장소다
    const done = nextFocus(asking, state([person('a', 'locker-1')]), PLACES, 'lounge');
    expect(done).toMatchObject({ primary: 'lounge', anchor: 'side-room' });
    expect(nextFocus(done, state([person('a', 'locker-1')]), PLACES).primary).toBe('side-room');
  });

  it('FR12B 고정한 채 시작해 아무 일도 없었다면 고정을 풀 때 사람이 있는 첫 장소다', () => {
    const start = nextFocus(undefined, state([person('a', 'locker-1')]), PLACES, 'side-room');
    expect(start).toMatchObject({ primary: 'side-room', anchor: 'lounge' });
    expect(nextFocus(start, state([person('a', 'locker-1')]), PLACES).primary).toBe('lounge');
  });

  it('FR12B 비콘을 받지 않는 사람은 장소가 없다', () => {
    const stale: Person = { ...person('a'), near: 'mood-1' };
    const f = nextFocus(undefined, state([stale, person('b', 'locker-1')]), PLACES);
    expect(f.personAt).toEqual({ b: 'lounge' });
    expect(f.inset).toBeUndefined();
  });

  it('FR12B 장소 목록이 비면 주 장소와 작은 방이 없다', () => {
    expect(nextFocus(undefined, state([person('a', 'mood-1')]), [])).toEqual({ personAt: {} });
  });

  it('FR12B 앞에 계산해 둔 주 장소가 목록에 없으면 사람이 있는 첫 장소, 아무도 없으면 첫 장소다', () => {
    const prev = { primary: 'attic', anchor: 'attic', personAt: { a: 'side-room' } };
    expect(nextFocus({ ...prev, personAt: {} }, state([]), PLACES).primary).toBe('lounge');
    // a는 앞 계산에서도 옆방이라 옮긴 사람이 아니다
    expect(nextFocus(prev, state([person('a', 'mood-1')]), PLACES)).toMatchObject({ primary: 'side-room', anchor: 'side-room' });
    expect(nextFocus(undefined, state([]), PLACES).primary).toBe('lounge');
  });

  it('FR12B 장소가 셋 이상이면 작은 방은 주 장소가 아닌 장소 중 사람이 있는 첫 장소다', () => {
    const attic: PlaceInfo = { placeId: 'attic', placeName: '다락', modelVersion: 'v1', calibrationVersion: 'cal-0', devices: [{ nodeId: 'x-1', name: '등', capability: 'light', occupancy: 'shared' }] };
    const f = nextFocus(undefined, state([person('a', 'x-1'), person('b', 'mood-1'), person('c', 'locker-1')]), [LOUNGE, SIDE_ROOM, attic]);
    expect(f).toMatchObject({ primary: 'lounge', inset: 'side-room' });
  });
});

describe('주 장소의 배치로 무대를 그린다', () => {
  it('FR12B 옆방 장면은 주 장소가 옆방이라 무드등 앞에 A만 서고 라운지의 사람은 그리지 않는다', () => {
    const s = SCENES['side-room']();
    const focus = nextFocus(undefined, s, PLACES);
    expect(focus).toMatchObject({ primary: 'side-room', inset: 'lounge' });
    const layout = withArrangement(SIDE_ROOM, resolveArrangement(SIDE_ROOM.devices), WEAK_RSSI_DBM);
    const v = stageView(s, layout, { panelOpen: false });
    expect(v).toMatchObject({ scene: 'flow', placeName: '옆방', modelVersion: 'v2', cmd: 'mood-1' });
    expect(v.devices.map((d) => d.nodeId)).toEqual(['mood-1']);
    expect(v.visitors.filter((x) => x.on).map((x) => x.personId)).toEqual(['p-a']);
    expect(v.callout).toMatchObject({ on: true, tone: 'ok', title: '무드등에 켜기 명령' });
  });
});

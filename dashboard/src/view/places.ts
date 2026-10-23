// 무대에 크게 그릴 장소(주 장소)와 작은 방으로 그릴 장소를 모니터 상태에서 규칙으로 고른다.
// 사람의 장소는 무대에 세우는 기기(near)의 장소다. near는 이미 4dB·2초 규칙으로 흔들림을 걸러서 문턱에서 장소가 오가지 않는다.
import type { MonitorState, Person, PlaceInfo } from '../model/types';

/** 기기가 속한 장소. 기기를 모르면 undefined */
export function placeOfNode(places: PlaceInfo[], nodeId: string | undefined): string | undefined {
  return nodeId ? places.find((p) => p.devices.some((d) => d.nodeId === nodeId))?.placeId : undefined;
}

/** 사람의 장소: 비콘을 받는 사람의 near가 속한 장소 */
export const placeOfPerson = (places: PlaceInfo[], p: Person) => (p.beaconSeen ? placeOfNode(places, p.near) : undefined);

/** 요청의 장소: 대상 기기(target.nodeId) → 요청한 사람의 장소(near) → 판정 기기(nodeId) → 후보 확률이 가장 큰 기기. 정할 수 없으면 undefined (원격 요청) */
export function requestPlace(s: MonitorState, places: PlaceInfo[]): string | undefined {
  const r = s.request;
  if (!r) return undefined;
  const requester = s.people.find((p) => p.id === r.personId);
  const likeliest = Object.entries(r.posteriors ?? {}).sort((a, b) => b[1] - a[1])[0]?.[0];
  return placeOfNode(places, r.target.nodeId)
    ?? (requester && placeOfPerson(places, requester))
    ?? placeOfNode(places, r.nodeId)
    ?? placeOfNode(places, likeliest);
}

export interface StageFocus {
  /** 크게 그리는 장소. 장소 목록이 비면 undefined */
  primary?: string;
  /** 작은 방으로 그리는 장소: 주 장소가 아니고 비콘을 받는 사람이 있는 장소. 없으면 undefined */
  inset?: string;
  /** 사람별 마지막 장소. 비콘이 끊겨도(장소를 정할 수 없어도) 앞 값을 이어받는다 */
  personAt: Record<string, string>;
  /** 고정과 상관없이 계산한 주 장소: 요청 장소 → 이번에 옮긴 사람의 장소 → prev.anchor(목록에 있으면) → 사람이 있는 첫 장소 → 첫 장소 */
  anchor?: string;
}

/**
 * 주 장소를 고른다. lock이 목록에 있으면 그 장소가 주 장소이고, 없으면 anchor다.
 * anchor는 고정 중에도 이어 계산해, 고정을 풀면 그 장소로 돌아간다. 옮긴 사람은 prev가 있고, 이번 장소를 정할 수 있으며,
 * 앞 장소와 다른 사람이다(처음 나타난 사람 포함). 여럿이면 목록(s.people) 순서의 마지막 사람을 쓴다.
 * prev가 없으면(화면을 새로 엶) 옮긴 사람이 없다. 비콘이 끊겨 장소를 정할 수 없는 사람은 앞 장소를 이어받아, 같은 장소로 돌아와도 옮긴 사람이 아니다.
 * 작은 방은 주 장소가 아닌 장소 중 비콘을 받는 사람이 있는 첫 장소 하나다.
 */
export function nextFocus(prev: StageFocus | undefined, s: MonitorState, places: PlaceInfo[], lock?: string): StageFocus {
  const listed = (id: string | undefined) => (places.some((pl) => pl.placeId === id) ? id : undefined);
  /** 비콘을 받는 사람의 장소 */
  const seenAt: Record<string, string> = {};
  const personAt: Record<string, string> = {};
  for (const p of s.people) {
    const at = placeOfPerson(places, p);
    if (at) seenAt[p.id] = at;
    const known = at ?? listed(prev?.personAt[p.id]);
    if (known) personAt[p.id] = known;
  }
  if (!places.length) return { personAt };
  const mover = prev && s.people.filter((p) => personAt[p.id] && personAt[p.id] !== prev.personAt[p.id]).at(-1);
  const occupied = places.find((pl) => Object.values(personAt).includes(pl.placeId))?.placeId;
  const anchor = requestPlace(s, places) ?? (mover && personAt[mover.id]) ?? listed(prev?.anchor) ?? occupied ?? places[0].placeId;
  const primary = listed(lock) ?? anchor;
  const inset = places.find((pl) => pl.placeId !== primary && Object.values(seenAt).includes(pl.placeId))?.placeId;
  return { primary, inset, personAt, anchor };
}

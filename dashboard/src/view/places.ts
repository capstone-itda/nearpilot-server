// 무대에 크게 그릴 장소(주 장소)와 작은 방으로 그릴 장소를 모니터 상태에서 규칙으로 고른다.
// 사람의 장소는 무대에 세우는 기기(near)의 장소다. near는 이미 4dB·2초 규칙으로 흔들림을 걸러서 문턱에서 장소가 오가지 않는다.
import type { MonitorState, Person, PlaceInfo } from '../model/types';

/** 기기가 속한 장소. 기기를 모르면 undefined */
export function placeOfNode(places: PlaceInfo[], nodeId: string | undefined): string | undefined {
  return nodeId ? places.find((p) => p.devices.some((d) => d.nodeId === nodeId))?.placeId : undefined;
}

/** 사람의 장소: 비콘을 받는 사람의 near가 속한 장소 */
const placeOfPerson = (places: PlaceInfo[], p: Person) => (p.beaconSeen ? placeOfNode(places, p.near) : undefined);

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
  /** 사람별 장소 (near의 장소). 다음 계산에서 장소를 옮긴 사람을 찾는다 */
  personAt: Record<string, string>;
}

/**
 * 주 장소를 고른다. lock이 있으면 그 장소가 주 장소다. 없으면 진행 중인 요청의 장소 → 이번에 장소를 옮긴(또는 처음 나타난) 사람의 장소
 * → prev.primary(목록에 있으면) → 첫 장소. 옮긴 사람이 여럿이면 목록(s.people) 순서의 마지막 사람을 쓴다.
 * 작은 방은 주 장소가 아닌 장소 중 사람이 있는 첫 장소 하나다.
 */
export function nextFocus(prev: StageFocus | undefined, s: MonitorState, places: PlaceInfo[], lock?: string): StageFocus {
  const personAt: Record<string, string> = {};
  for (const p of s.people) {
    const at = placeOfPerson(places, p);
    if (at) personAt[p.id] = at;
  }
  if (!places.length) return { personAt };
  const listed = (id: string | undefined) => (places.some((pl) => pl.placeId === id) ? id : undefined);
  const mover = s.people.filter((p) => personAt[p.id] && personAt[p.id] !== prev?.personAt[p.id]).at(-1);
  const primary = listed(lock) ?? requestPlace(s, places) ?? (mover && personAt[mover.id]) ?? listed(prev?.primary) ?? places[0].placeId;
  const inset = places.find((pl) => pl.placeId !== primary && Object.values(personAt).includes(pl.placeId))?.placeId;
  return { primary, inset, personAt };
}

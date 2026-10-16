// 왼쪽 폰 미러링에 누구의 폰을 보일지 고른다 (D3 자동 따라가기).
import type { Snapshot } from '../model/types';

export interface FollowSettings {
  /** 요청한 사람의 폰을 자동으로 보인다 */
  followRequester: boolean;
  /** 승인 알림이 오면 소유자 폰으로 바꾼다 */
  followApprover: boolean;
}

export interface PhoneChoice { personId: string | undefined; who: string; follow: string }

/** previous: 직전에 보이던 사람. 자동 전환이 꺼져 있으면 그대로 둔다. */
export function choosePhone(s: Snapshot, f: FollowSettings, previous: string | undefined): PhoneChoice {
  const name = (id?: string) => s.people.find((p) => p.id === id)?.pseudonym ?? '—';
  const owner = s.people.find((p) => p.role === 'owner');
  const r = s.request;

  if (r && r.verdict === 'NEED_APPROVAL' && s.ownerNotified && f.followApprover && owner) {
    return { personId: owner.id, who: name(owner.id), follow: '승인할 사람으로 자동 전환됨' };
  }
  if (r && f.followRequester) {
    return { personId: r.personId, who: name(r.personId), follow: '요청한 사람을 따라가는 중' };
  }
  if (!f.followRequester && !f.followApprover) {
    const id = previous ?? s.people.find((p) => p.role === 'visitor')?.id;
    return { personId: id, who: name(id), follow: '자동 전환 꺼짐' };
  }
  const id = previous ?? s.people.find((p) => p.role === 'visitor')?.id;
  return { personId: id, who: name(id), follow: '요청하는 사람을 자동으로 따라가요' };
}

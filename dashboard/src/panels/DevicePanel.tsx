// MON-05 기기 상세: 상태, 고장 격리의 복구 준비, 대여 초과
import { Checklist, KeyValue, NoteBox, Section, StateStrip, Timeline, VerdictTag } from '../components/common';
import { Loading, Panel } from '../components/Panel';
import { useData, useQuery } from '../data/context';
import { ACTION_NAME, DEVICE_STATE_NAME } from '../view/text';

const BLOCKER: Record<string, [string, string]> = {
  SAFETY_UNCONFIRMED: ['안전 확인 필요', '안전 확인됨'],
  EXECUTION_UNCONFIRMED: ['명령 결과 확인 필요', '명령 결과 확인됨'],
  PHYSICAL_STATE_UNSYNCED: ['실제 상태 맞추기 필요', '실제 상태 맞춤']
};

function FaultBody({ nodeId }: { nodeId: string }) {
  const { source } = useData();
  const q = useQuery(() => source.recovery(nodeId), [nodeId]);
  if (!q.data) return <Loading error={q.error} />;
  const r = q.data;
  const act = r.action ? `${r.action} 명령의` : '명령의';
  return (
    <>
      <Section title="상태" hint={`${act} 결과를 몰라서(${r.cause}) 후보에서 빼고 격리했어요`}>
        <StateStrip current="FAULT" tone="deny" />
      </Section>
      <Section title="복구 준비" hint="모두 충족한 뒤 소유자가 승인해야 다시 쓸 수 있어요">
        <Checklist items={[
          [r.connectedAt ? 'pass' : 'fail', r.connectedAt ? '기기 연결됨' : '기기 연결 필요', r.connectedAt ? `OFFLINE 아님 · ${r.connectedAt} 확인` : 'OFFLINE'],
          [r.trust === 'trusted' ? 'pass' : 'fail', r.trust === 'trusted' ? '신뢰 상태 유지' : '신뢰 필요', r.trust],
          ...(Object.keys(BLOCKER) as (keyof typeof BLOCKER)[]).map((k): ['pass' | 'fail', string, string] => {
            const blocked = r.blockers.includes(k as never);
            return [blocked ? 'fail' : 'pass', BLOCKER[k][blocked ? 0 : 1], blocked ? k : ''];
          })
        ]} />
      </Section>
      <NoteBox text="이 화면은 보기만 해요. 복구 승인은 소유자가 스크립트로 따로 보내요." code="POST /admin/nodes/…/recovery/…/approve" />
    </>
  );
}

function RentalBody({ nodeId, state }: { nodeId: string; state: string }) {
  const { source, personName } = useData();
  const q = useQuery(() => source.rental(nodeId), [nodeId]);
  if (!q.data) return <Loading error={q.error} />;
  const r = q.data;
  return (
    <>
      <Section title="상태"><StateStrip current={state} tone="accent" /></Section>
      <Section title="대여">
        <KeyValue rows={[['사용자', personName(r.personId)], ['방식', '대여형 · rental'], ['시작', r.startedAt], ['대여 시간', '설정값 rental_duration_sec'], ['초과', r.overdueMinutes > 0 ? `${r.overdueMinutes}분 초과` : '—']]} />
      </Section>
      {r.overdueMinutes > 0 ? (
        <Section title="알아 둘 점"><NoteBox text="대여 시간을 넘겨도 자동으로 반납하지 않아요. 초과 시간만 보여 줘요." code="FR-20" /></Section>
      ) : null}
      <Section title="최근 기록">
        <Timeline items={r.timeline.map((t) => [t.now ? 'now' : 'done', t.at, t.text])} />
      </Section>
    </>
  );
}

export function DevicePanel({ nodeId, onClose }: { nodeId: string; onClose: () => void }) {
  const { snapshot, layout, deviceName } = useData();
  const st = snapshot.devices.find((d) => d.nodeId === nodeId);
  const dev = layout.devices.find((d) => d.nodeId === nodeId);
  const state = st?.state ?? 'AVAILABLE';
  const tag = state === 'FAULT'
    ? <VerdictTag verdict="FAULT" />
    : <VerdictTag verdict="INFO" label={DEVICE_STATE_NAME[state]} code={state} />;
  let body;
  if (state === 'FAULT') body = <FaultBody nodeId={nodeId} />;
  else if (dev?.occupancy === 'rental' && state !== 'AVAILABLE') body = <RentalBody nodeId={nodeId} state={state} />;
  else {
    body = (
      <>
        <Section title="상태"><StateStrip current={state} tone="accent" /></Section>
        <Section title="기기">
          <KeyValue rows={[['코드', nodeId, true], ['방식', dev?.occupancy === 'shared' ? '공용형 · shared' : '대여형 · rental'], ['연결', st?.online ? '연결됨' : '연결 끊김'], ['신뢰', st?.trust ?? '—', true],
            ['동작', dev ? (dev.capability === 'locker' ? ACTION_NAME.open : `${ACTION_NAME.on} · ${ACTION_NAME.off}`) : '—']]} />
        </Section>
      </>
    );
  }
  return <Panel kind="device" cap="기기 상세 · MON-05" title={deviceName(nodeId)} tag={tag} onClose={onClose}>{body}</Panel>;
}

// MON-05 기기 상세: 상태, 고장 격리 원인, 대여 초과
import { KeyValue, NoteBox, Section, StateStrip, Timeline, VerdictTag } from '../components/common';
import { Loading, Panel } from '../components/Panel';
import { useData, useQuery } from '../data/context';
import { ACTION_NAME, DEVICE_STATE_NAME } from '../view/text';

function FaultBody({ nodeId }: { nodeId: string }) {
  const { snapshot } = useData();
  const st = snapshot.devices.find((d) => d.nodeId === nodeId);
  const f = st?.fault;
  const cmd = f?.action ? `${ACTION_NAME[f.action]} 명령` : '명령';
  const cause = !f ? '' : f.cause === 'EXEC_UNKNOWN' ? `${cmd}의 결과를 몰라서` : f.cause === 'EXEC_FAILED' ? `기기가 ${cmd} 실패를 알려서` : `기기 이상(${f.cause})으로`;
  const why = f ? `${cause} 후보에서 빼고 격리했어요` : '후보에서 빼고 격리했어요';
  return (
    <>
      <Section title="상태" hint={why}>
        <StateStrip current="FAULT" tone="deny" />
      </Section>
      <Section title="기기">
        <KeyValue rows={[['코드', nodeId, true], ['연결', st?.online ? '연결됨' : '연결 끊김'], ['신뢰', st?.trust ?? '—', true]]} />
      </Section>
      {f ? <Section title="최근 기록"><Timeline items={[['now', f.at, `격리 · ${f.cause}`, 'deny']]} /></Section> : null}
      <NoteBox text="다시 쓰려면 소유자가 스크립트로 복구 준비를 확인하고 승인해요. 이 화면은 상태만 보여요." code="FR-12A · NFR-04" />
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

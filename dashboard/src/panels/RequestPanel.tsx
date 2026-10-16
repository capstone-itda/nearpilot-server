// MON-04 요청 상세: 판정 단계 ⓪~⑥, 근접 확률, 승인 진행
import { isDecided, type DecidedRequest, type RequestProgress, type Verdict } from '../model/types';
import { KeyValue, ProbBars, Section, StepList, Timeline, VerdictTag, type StepRowResult } from '../components/common';
import { Panel } from '../components/Panel';
import { useData } from '../data/context';
import { ACTION_DONE, ACTION_NAME, ACTION_VERB, CAPABILITY_NAME, CIRCLED, HOST_NAME, STEP_SHORT, VERDICT_LABEL, approvalText, josa, reasonText, requestTitle, toolCall } from '../view/text';

const STOP_RESULT: Record<Verdict, StepRowResult> = { OK: 'pass', ASK: 'ask', NEED_APPROVAL: 'appr', BUSY: 'fail', DENY: 'fail' };

function stopHint(r: RequestProgress, cap: string): string {
  if (!isDecided(r)) return '판정 중';
  const i = r.steps.findIndex((s) => s.result === 'stop');
  if (i === -1) return r.verdict === 'OK' ? '모든 단계를 통과했어요' : `단계 확인 밖에서 멈췄어요 · ${reasonText(r.reason)}`;
  const why: Record<string, string> = {
    APPROVAL_REQUIRED: '소유자 승인이 필요해',
    LOW_CONFIDENCE: `어느 ${cap} 앞인지 확신이 부족해`,
    MOVE_CLOSER: '기기 앞이라는 확신이 부족해',
    DEVICE_BUSY: '이미 점유된 기기라',
    USER_BUSY: '이미 다른 기기를 쓰고 있어',
    COMMAND_IN_PROGRESS: '다른 명령을 실행하는 중이라',
    BEACON_NOT_SEEN: '폰 신호가 없어',
    NO_CANDIDATE: '쓸 수 있는 기기가 없어',
    TARGET_UNAVAILABLE: '지금 쓸 수 없는 기기라',
    POLICY_DENIED: '정책이 허락하지 않아'
  };
  return why[r.reason] ? `${CIRCLED[i]} ${STEP_SHORT[i]}에서 ${why[r.reason]} 멈췄어요` : `${CIRCLED[i]} ${STEP_SHORT[i]}에서 멈췄어요 · ${reasonText(r.reason)}`;
}

function execText(r: RequestProgress, subject: string): string {
  switch (r.execution) {
    case 'succeeded': return `${subject} ${ACTION_DONE[r.action]}`;
    case 'failed': return '기기가 실패를 알려 고장 격리했어요 (EXEC_FAILED)';
    case 'unknown': return '응답이 없어 결과를 몰라 고장 격리했어요 (EXEC_UNKNOWN)';
    case 'sent': return '명령 보냄 · 결과 기다림';
    default: return '허가 · 명령 보낼 차례';
  }
}

export function RequestPanel({ r, onClose }: { r: RequestProgress; onClose: () => void }) {
  const { personName, deviceName, layout, snapshot } = useData();
  const cap = CAPABILITY_NAME[r.target.capability ?? layout.devices.find((d) => d.nodeId === r.nodeId)?.capability ?? 'locker'];
  const d: DecidedRequest | undefined = isDecided(r) ? r : undefined;
  const rows: [StepRowResult, string, string][] = r.steps.map((s) =>
    s.result === 'pass' ? ['pass', '통과', s.detail ?? '']
      : s.result === 'stop' ? (d ? [STOP_RESULT[d.verdict], VERDICT_LABEL[d.verdict], s.detail ?? d.reason] : ['fail', '멈춤', s.detail ?? ''])
        : s.result === 'pending' ? ['pending', '기다림', '']
          : ['skip', '확인 안 함', s.detail ?? '']);
  const post = Object.entries(r.posteriors ?? {}).sort((a, b) => b[1] - a[1]);
  const showBars = r.verdict === 'ASK' && post.length > 0 && r.threshold !== undefined;

  return (
    <Panel kind="request" cap="요청 상세 · MON-04" title={`${requestTitle(r, deviceName)} 요청`} tag={d ? <VerdictTag verdict={d.verdict} /> : <VerdictTag verdict="WAIT" code="…" label="판정 중" />} onClose={onClose}>
      <Section title="요청">
        <KeyValue rows={[['누가', `${personName(r.personId)} · ${HOST_NAME[r.host]}`], ['도구', toolCall(r), true]]} />
      </Section>
      <Section title="서버가 확인한 순서" hint={stopHint(r, cap)}>
        <StepList items={rows} />
      </Section>
      {r.verdict === 'OK' && r.nodeId ? (
        <Section title="실행 결과">
          <KeyValue rows={[['명령', `${deviceName(r.nodeId)} ${ACTION_NAME[r.action]}`], ['결과', execText(r, josa(deviceName(r.nodeId), '이', '가'))]]} />
        </Section>
      ) : null}
      {showBars ? (
        <Section title={`어느 ${cap} 앞일까`} hint={`가장 높은 확률이 기준선을 넘어야 ${ACTION_VERB[r.action]}`}>
          <ProbBars rows={layout.devices.filter((d) => r.posteriors![d.nodeId] !== undefined).map((d) => [d.name, r.posteriors![d.nodeId], d.nodeId === post[0][0] ? 'ask' : 'muted'])} threshold={r.threshold!} />
        </Section>
      ) : null}
      {d?.verdict === 'NEED_APPROVAL' ? (
        <Section title="승인 진행">
          <Timeline items={d.accessRequestedAt
            ? [['done', d.accessRequestedAt, 'request_access로 승인 신청 → 소유자 폰에 알림 (알림 수단 미결)'],
              ...(d.accessExpiredAt
                ? [['now', d.accessExpiredAt, '소유자 응답 없이 신청 만료 · 실행 안 함', 'appr'] as ['now', string, string, string]]
                : snapshot.request?.id === d.id ? [['now', snapshot.lastReceivedAt, '소유자 응답 기다림', 'appr'] as ['now', string, string, string]] : [])]
            : [['now', d.at, d.reason === 'APPROVAL_REQUIRED'
              ? 'AI의 승인 신청(request_access)을 기다림'
              : approvalText(d.reason).detail, 'appr']]} />
        </Section>
      ) : null}
    </Panel>
  );
}

// MON-06~10 메뉴로 여는 패널. 모두 조회만 한다 (FR-12A).
import { useState } from 'react';
import type { AuditStep, HistoryQuery, HostKind, Verdict } from '../model/types';
import { DataTable, FilterBar, NoteBox, Section, Td, TdLink, TdMono, VerdictTag, Zone } from '../components/common';
import { Loading, Panel } from '../components/Panel';
import { useData, useQuery } from '../data/context';
import { ACTION_NAME, CAPABILITY_NAME, HOST_NAME, REASON_TEXT, VERDICT_LABEL, targetCode } from '../view/text';

type Close = { onClose: () => void };

export function UsesPanel({ onClose, onOpenDevice }: Close & { onOpenDevice: (id: string) => void }) {
  const { source, deviceName, personName } = useData();
  const q = useQuery(() => source.uses(), []);
  return (
    <Panel kind="uses" cap="패널 · MON-06" title="점유·대여 현황" onClose={onClose}>
      <Section title="지금 쓰는 기기" hint="대여형 기기의 점유 세션만 보여요. 공용형은 세션이 없어 이 표에 없어요">
        {q.data ? (
          <DataTable label="점유·대여 목록" head={['기기', '사용자', '세션', '시작', '초과']} rows={q.data.map((u) => ({
            key: u.nodeId,
            cells: [
              <TdLink key="d" main={deviceName(u.nodeId)} sub={u.nodeId} onClick={() => onOpenDevice(u.nodeId)} />,
              <Td key="p" main={personName(u.personId)} sub={u.occupancy} />,
              <Td key="s" main={u.session} sub={u.sessionNote} />,
              <TdMono key="t" v={u.startedAt} />,
              <Td key="o" main={u.overdueMinutes ? `${u.overdueMinutes}분 초과` : '—'} />
            ]
          }))} />
        ) : <Loading error={q.error} />}
      </Section>
    </Panel>
  );
}

export function ApprovalsPanel({ onClose }: Close) {
  const { source, deviceName, personName } = useData();
  const q = useQuery(() => source.approvals(), []);
  const a = q.data;
  return (
    <Panel kind="approvals" cap="패널 · MON-07" title="승인 현황" onClose={onClose}>
      {a ? (
        <>
          <Section title="기다리는 승인">
            <DataTable label="대기 승인" head={['기기', '요청자', '신청', '남은 시간']} rows={a.pending.map((r, i) => ({
              key: `p${i}`, cells: [<Td key="d" main={deviceName(r.nodeId)} sub={r.nodeId} />, <Td key="p" main={personName(r.personId)} />, <TdMono key="t" v={r.requestedAt ?? '—'} />, <Td key="r" main={r.remainingMinutes !== undefined ? `${r.remainingMinutes}분` : '—'} />]
            }))} />
          </Section>
          <Section title="승인됨">
            <DataTable label="유효 승인" head={['기기', '요청자', '유효', '사용']} rows={a.active.map((r, i) => ({
              key: `a${i}`, cells: [<Td key="d" main={deviceName(r.nodeId)} sub={r.nodeId} />, <Td key="p" main={personName(r.personId)} sub={r.mode} />, <Td key="v" main={r.validUntil ? `${r.validUntil}까지` : '—'} />, <Td key="u" main={`${r.uses ?? 0}회 사용`} />]
            }))} />
          </Section>
          <Section title="끝난 승인">
            <DataTable label="종료 승인" head={['기기', '요청자', '끝난 이유']} rows={a.ended.map((r, i) => ({
              key: `e${i}`, cells: [<Td key="d" main={deviceName(r.nodeId)} sub={r.nodeId} />, <Td key="p" main={personName(r.personId)} sub={r.mode} />, <Td key="r" main={r.endReason ? REASON_TEXT[r.endReason] ?? r.endReason : '—'} sub={r.endReason} />]
            }))} />
          </Section>
          <NoteBox text="승인과 거부는 소유자 폰에서 해요. 이 패널은 보기만 해요." code="D2 보기 전용" />
        </>
      ) : <Loading error={q.error} />}
    </Panel>
  );
}

const STEPS: AuditStep[] = ['auth', '0', '1', '2', '3', '4', '5', '6', 'exec', 'close', 'fault', 'announce', 'admin'];

export function AuditPanel({ onClose }: Close) {
  const { source, deviceName } = useData();
  const q = useQuery(() => source.audit(), []);
  const [filter, setFilter] = useState<AuditStep | null>(null);
  const rows = (q.data ?? []).filter((r) => !filter || r.step === filter);
  const range = q.data && q.data.length ? `${q.data[q.data.length - 1].at.slice(0, 5)}~${q.data[0].at.slice(0, 5)}` : '';
  return (
    <Panel kind="audit" size="wide" cap="패널 · MON-08" title="감사 로그" onClose={onClose}>
      <Section title="단계로 거르기">
        <FilterBar items={[['전체', filter === null], ...STEPS.map((s): [string, boolean] => [s, filter === s])]}
          onToggle={(l) => setFilter(l === '전체' ? null : (l as AuditStep))} />
      </Section>
      <Section title="기록" hint={q.data ? `${range} · 새 기록이 위${filter ? ` · ${rows.length}건` : ''}` : undefined}>
        {q.data ? (
          <DataTable label="감사 로그" head={['시각', '단계', '기기', '내용']} rows={rows.map((r) => ({
            key: r.id,
            cells: [<TdMono key="t" v={r.at} />, <TdMono key="s" v={r.step} />, <Td key="d" main={r.nodeId ? deviceName(r.nodeId) : '—'} sub={r.nodeId} />, <Td key="x" main={r.text} sub={r.code} />]
          }))} />
        ) : <Loading error={q.error} />}
      </Section>
    </Panel>
  );
}

const VERDICTS: Verdict[] = ['OK', 'ASK', 'NEED_APPROVAL', 'BUSY', 'DENY'];
const HOSTS: HostKind[] = ['claude', 'chatgpt', 'gemini'];

export function HistoryPanel({ onClose, onOpenRequest }: Close & { onOpenRequest: (requestId: string) => void }) {
  const { source, layout, deviceName, personName } = useData();
  const [form, setForm] = useState<HistoryQuery>({ period: '오늘' });
  const [query, setQuery] = useState<HistoryQuery>(form);
  const q = useQuery(() => source.history(query), [query]);
  return (
    <Panel kind="history" size="wide" cap="패널 · MON-09" title="요청·판정 이력" onClose={onClose}>
      <Section title="찾기">
        <form className="np-form" data-component="SearchForm" onSubmit={(e) => { e.preventDefault(); setQuery(form); }}>
          <label className="np-field"><span className="np-field__label">기간</span>
            <input className="np-field__input" value={form.period} onChange={(e) => setForm({ ...form, period: e.target.value })} /></label>
          <label className="np-field"><span className="np-field__label">판정</span>
            <select className="np-field__input" value={form.verdict ?? ''} onChange={(e) => setForm({ ...form, verdict: (e.target.value || undefined) as Verdict | undefined })}>
              <option value="">전체</option>{VERDICTS.map((v) => <option key={v} value={v}>{VERDICT_LABEL[v]}</option>)}
            </select></label>
          <label className="np-field"><span className="np-field__label">기기</span>
            <select className="np-field__input" value={form.nodeId ?? ''} onChange={(e) => setForm({ ...form, nodeId: e.target.value || undefined })}>
              <option value="">전체</option>{layout.devices.map((d) => <option key={d.nodeId} value={d.nodeId}>{d.name}</option>)}
            </select></label>
          <label className="np-field"><span className="np-field__label">AI</span>
            <select className="np-field__input" value={form.host ?? ''} onChange={(e) => setForm({ ...form, host: (e.target.value || undefined) as HostKind | undefined })}>
              <option value="">전체</option>{HOSTS.map((h) => <option key={h} value={h}>{HOST_NAME[h]}</option>)}
            </select></label>
          <button className="np-btn" type="submit">찾기</button>
        </form>
      </Section>
      <Section title={q.data ? `결과 ${q.data.length}건` : '결과'}>
        {q.data ? (
          <DataTable label="요청·판정 이력" head={['시각', '누가', '요청', '판정', '기기', '결과']} rows={q.data.map((h) => ({
            key: h.requestId,
            cells: [
              <TdMono key="t" v={h.at} />,
              <TdLink key="p" main={personName(h.personId)} sub={HOST_NAME[h.host]} onClick={() => onOpenRequest(h.requestId)} />,
              <Td key="r" main={`${h.target.kind === 'explicit' && h.target.nodeId ? deviceName(h.target.nodeId) : CAPABILITY_NAME[h.target.capability ?? 'locker']} ${ACTION_NAME[h.action]}`} sub={`${targetCode(h.target)} · ${h.action}`} />,
              <td key="v"><VerdictTag verdict={h.verdict} /></td>,
              <Td key="d" main={h.nodeId ? deviceName(h.nodeId) : '—'} sub={h.nodeId ?? '대상 없음'} />,
              <Td key="o" main={h.outcome} sub={h.code} />
            ]
          }))} />
        ) : <Loading error={q.error} />}
      </Section>
    </Panel>
  );
}

export function TrustPanel({ onClose }: Close) {
  const { source } = useData();
  const q = useQuery(() => source.trust(), []);
  const t = q.data;
  if (!t) return <Panel kind="trust" size="wide" cap="패널 · MON-10" title="신뢰도 지표" onClose={onClose}><Loading error={q.error} /></Panel>;
  const total = VERDICTS.reduce((n, v) => n + t.live.counts[v], 0) || 1;
  let x = 0;
  const segs = VERDICTS.map((v) => {
    const w = (t.live.counts[v] / total) * 100;
    const seg = { v, x, w: Math.max(w - 0.5, 0) };
    x += w;
    return seg;
  });
  const { labels, values } = t.evaluation.confusion;
  return (
    <Panel kind="trust" size="wide" cap="패널 · MON-10" title="신뢰도 지표" onClose={onClose}>
      <Zone kind="live" title="이번 시연" cap="실시간">
        <div className="np-tiles">
          <div className="np-tile"><span className="np-tile__label">판정</span><span className="np-tile__value">{t.live.decisions}</span><span className="np-tile__note">{t.live.since}부터</span></div>
          <div className="np-tile"><span className="np-tile__label">이중 점유</span><span className="np-tile__value" data-tone={t.live.doubleOccupancy === 0 ? 'ok' : 'deny'}>{t.live.doubleOccupancy}</span><span className="np-tile__note">지표 3</span></div>
          <div className="np-tile"><span className="np-tile__label">서버 판정 지연 중앙값</span><span className="np-tile__value">{t.live.latencyMedianSec.toFixed(2)}초</span><span className="np-tile__note">지표 7 · 기준 1초</span></div>
          <div className="np-tile"><span className="np-tile__label">서버 판정 지연 p95</span><span className="np-tile__value">{t.live.latencyP95Sec.toFixed(2)}초</span><span className="np-tile__note">지표 7 · 기준 2초</span></div>
        </div>
        <div className="np-stack" role="img" aria-label="판정 비율">
          <svg className="np-bars__svg" viewBox="0 0 100 10" preserveAspectRatio="none">
            {segs.map((s) => <rect key={s.v} className="np-stack__seg" data-verdict={s.v} x={s.x} width={s.w} height="10" />)}
          </svg>
        </div>
        <div className="np-legend">
          {VERDICTS.map((v) => <span key={v} className="np-legend__item"><span className="np-legend__swatch" data-verdict={v} />{VERDICT_LABEL[v]} {t.live.counts[v]}</span>)}
        </div>
      </Zone>
      <Zone kind="eval" title="사전 평가 · 시연 숫자와 섞지 않음" cap={`평가일 ${t.evaluation.date}`}>
        <div className="np-zone__cols">
          <DataTable label="인수 기준 지표" head={['지표', '결과', '표본']} rows={t.evaluation.metrics.map((m) => ({
            key: String(m.id),
            cells: [
              <Td key="n" main={`${m.id} ${m.name}`} />,
              <td key="r"><span className="np-pass" data-pass={m.pass === null ? 'none' : String(m.pass)}>{m.label}</span></td>,
              m.sampleNote ? <Td key="s" main={m.sample} sub={m.sampleNote} /> : <TdMono key="s" v={m.sample} />
            ]
          }))} />
          <div className="np-sec">
            <div className="np-sec__head"><h3 className="np-sec__title">근접 모델 혼동 행렬 · 지표 8</h3><span className="np-cap">시험셋</span></div>
            <table className="np-matrix" data-component="ConfusionMatrix" aria-label="근접 모델 혼동 행렬">
              <thead><tr><th scope="col">실제＼고름</th>{labels.map((h) => <th key={h} scope="col">{h}</th>)}</tr></thead>
              <tbody>
                {values.map((r, i) => (
                  <tr key={labels[i]}><th scope="row">{labels[i]}</th>{r.map((v, j) => <td key={j} data-level={i === j ? 'hi' : v ? 'lo' : 'zero'}>{v}</td>)}</tr>
                ))}
              </tbody>
            </table>
            <p className="np-sec__hint">값은 %. 1~3 사물함 · 스 스피커 · 조1~3 조명 · 없음 none</p>
          </div>
        </div>
      </Zone>
    </Panel>
  );
}

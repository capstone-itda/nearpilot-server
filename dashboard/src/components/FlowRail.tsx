import type { RailStep, RailView } from '../view/rail';

const STEP_TITLES = ['말하기', 'AI가 요청', '서버가 확인', '기기 실행', '결과'];
const CHECK_NAMES = ['누구인지', '어디 앞인지', '정책에 맞는지', '비어 있는지'];
const CHECK_MARK = { pass: '✓', fail: '✕', wait: '…', unsure: '?', none: '·', skip: '–' };

export function FlowStep({ s, i }: { s: RailStep; i: number }) {
  const checks = s.checks ?? [];
  return (
    <li className={s.status === 'current' ? 'np-step np-flash' : 'np-step'} data-component="FlowStep" data-status={s.status} data-tone={s.tone ?? 'accent'}>
      <div className="np-step__head"><span className="np-step__num">{i + 1}</span><span className="np-step__title">{STEP_TITLES[i]}</span><span className="np-step__mark">{s.mark ?? ''}</span></div>
      <p className="np-step__detail">{s.detail ?? ''}</p>
      <p className="np-step__code">{s.code ?? ''}</p>
      {s.gauge ? <span className="np-step__gauge" aria-hidden="true"><span className="np-step__gauge-fill" /></span> : null}
      <ul className="np-step__checks">
        {/* 판정이 나면 멈춘 칸이 한 번 빛난다 */}
        {checks.map(([res, text], k) => (
          <li key={k} className={res === 'fail' || res === 'unsure' || res === 'wait' ? 'np-check np-flash' : 'np-check'} data-result={res}><span className="np-check__mark" aria-hidden="true">{CHECK_MARK[res]}</span>{text ?? CHECK_NAMES[k]}</li>
        ))}
      </ul>
    </li>
  );
}

/** onOpen: 진행 중인 요청의 상세를 연다 */
export function FlowRail({ v, onOpen }: { v: RailView; onOpen?: () => void }) {
  return (
    <section className="np-rail" data-component="FlowRail" data-mode={v.mode} aria-label="요청 흐름">
      <div className="np-rail__head"><h2 className="np-rail__title">{v.title}</h2><span className="np-rail__meta">{v.meta}</span><span className="np-rail__code">{v.code}</span>
        {v.mode === 'live' && onOpen ? <button className="np-btn np-rail__open" data-variant="quiet" type="button" onClick={onOpen}>요청 상세</button> : null}</div>
      <ol className="np-rail__steps">
        {/* 내용이 바뀐 단계는 다시 그려져 도착 강조가 한 번 나온다 */}
        {v.steps.map((s, i) => <FlowStep key={`${i}|${s.status}|${s.mark}|${s.detail}|${s.code}|${JSON.stringify(s.checks)}`} s={s} i={i} />)}
      </ol>
    </section>
  );
}

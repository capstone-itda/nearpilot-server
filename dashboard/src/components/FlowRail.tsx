import { useLayoutEffect, useRef } from 'react';
import type { RailStep, RailView } from '../view/rail';
import { Icon, type IconName } from './common';

const STEP_TITLES = ['말하기', 'AI가 요청', '서버가 확인', '기기 실행', '결과'];
/** 레일이 좁아 글자가 가려질 때 제목 대신 보이는 단계 아이콘 */
const STEP_ICONS: IconName[] = ['talk', 'ai', 'check', 'run', 'result'];
/** 좁을 때 숨기는 글자. 이 중 하나라도 잘리면 아이콘으로 바꾼다. */
const SQUEEZED = '.np-step__title, .np-step__detail, .np-step__code, .np-step__mark, .np-rail__code';

export function FlowStep({ s, i }: { s: RailStep; i: number }) {
  return (
    <li className={s.status === 'current' ? 'np-step np-flash' : 'np-step'} data-component="FlowStep" data-status={s.status} data-tone={s.tone ?? 'accent'}
      title={[STEP_TITLES[i], s.mark, s.detail, s.code].filter(Boolean).join(' · ')}>
      <div className="np-step__head"><span className="np-step__num">{i + 1}</span><span className="np-step__icon"><Icon name={STEP_ICONS[i]} /></span><span className="np-step__title">{STEP_TITLES[i]}</span><span className="np-step__mark">{s.mark ?? ''}</span></div>
      <p className="np-step__detail">{s.detail ?? ''}</p>
      <p className="np-step__code">{s.code ?? ''}</p>
    </li>
  );
}

/** onOpen: 진행 중인 요청의 상세를 연다 */
export function FlowRail({ v, onOpen }: { v: RailView; onOpen?: () => void }) {
  const ref = useRef<HTMLElement>(null);
  // 글자가 잘리면 data-compact로 단계를 아이콘으로 바꾼다. 재기 전에 풀어서 넓을 때의 모양으로 잰다.
  // 아이콘으로 바꿔도 단계가 레일 높이를 넘으면 상태 표시와 번호까지 숨기고 아이콘만 둔다(icons).
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const check = () => {
      el.removeAttribute('data-compact');
      const cut = [...el.querySelectorAll<HTMLElement>(SQUEEZED)].some((t) => t.scrollWidth > t.clientWidth + 1);
      if (!cut) return;
      el.setAttribute('data-compact', 'true');
      const steps = el.querySelector<HTMLElement>('.np-rail__steps');
      if (steps && steps.scrollHeight > steps.clientHeight + 1) el.setAttribute('data-compact', 'icons');
    };
    check();
    const ro = new ResizeObserver(check);
    ro.observe(el);
    return () => ro.disconnect();
  }, [v]);
  return (
    <section ref={ref} className="np-rail" data-component="FlowRail" data-mode={v.mode} aria-label="요청 흐름">
      <div className="np-rail__head"><h2 className="np-rail__title">{v.title}</h2><span className="np-rail__meta">{v.meta}</span><span className="np-rail__code">{v.code}</span>
        {v.mode === 'live' && onOpen ? <button className="np-btn np-rail__open" data-variant="quiet" type="button" onClick={onOpen}>요청 상세</button> : null}</div>
      <ol className="np-rail__steps">
        {/* 내용이 바뀐 단계는 다시 그려져 도착 강조가 한 번 나온다 */}
        {v.steps.map((s, i) => <FlowStep key={`${i}|${s.status}|${s.mark}|${s.detail}|${s.code}`} s={s} i={i} />)}
      </ol>
    </section>
  );
}

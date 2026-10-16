// 작은 공용 컴포넌트. 마크업은 화면 설계 목업과 같다.
import type { ReactNode } from 'react';
import { CIRCLED, STEP_NAMES, VERDICT_LABEL } from '../view/text';

const b = (v: unknown) => (v ? 'true' : 'false');

export function Icon({ name }: { name: 'menu' | 'sun' | 'moon' | 'close' | 'back' }) {
  switch (name) {
    case 'menu': return <svg className="np-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M4 7h16M4 12h16M4 17h16" /></svg>;
    case 'sun': return <svg className="np-icon" data-icon="sun" viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="4" /><path d="M12 2v2M12 20v2M2 12h2M20 12h2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4" /></svg>;
    case 'moon': return <svg className="np-icon" data-icon="moon" viewBox="0 0 24 24" aria-hidden="true"><path d="M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5z" /></svg>;
    case 'close': return <svg className="np-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18" /></svg>;
    case 'back': return <svg className="np-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M15 6l-6 6 6 6" /></svg>;
  }
}

export type TagVerdict = 'OK' | 'ASK' | 'NEED_APPROVAL' | 'BUSY' | 'DENY' | 'FAULT' | 'QUERY' | 'WAIT' | 'INFO';
export function VerdictTag({ verdict, code, label }: { verdict: TagVerdict; code?: string; label?: string }) {
  return (
    <span className="np-verdict" data-component="VerdictTag" data-verdict={verdict}>
      {label ?? VERDICT_LABEL[verdict]}<span className="np-verdict__code">{code ?? verdict}</span>
    </span>
  );
}

export function Section({ title, hint, right, children }: { title: string; hint?: string; right?: ReactNode; children: ReactNode }) {
  return (
    <section className="np-sec" data-component="Section">
      <div className="np-sec__head"><h3 className="np-sec__title">{title}</h3>{right}</div>
      {hint ? <p className="np-sec__hint">{hint}</p> : null}
      {children}
    </section>
  );
}

export function KeyValue({ rows }: { rows: [string, ReactNode, boolean?][] }) {
  return (
    <dl className="np-kv" data-component="KeyValue">
      {rows.map(([k, v, mono]) => [<dt key={`${k}-t`}>{k}</dt>, <dd key={`${k}-d`} data-mono={b(mono)}>{v}</dd>])}
    </dl>
  );
}

export type StepRowResult = 'pass' | 'ask' | 'appr' | 'fail' | 'skip';
export function StepList({ items }: { items: [StepRowResult, string, string][] }) {
  return (
    <ol className="np-steps" data-component="StepList">
      {items.map(([res, result, detail], i) => (
        <li key={i} className="np-steps__item" data-result={res}>
          <span className="np-steps__n">{i}</span><span className="np-steps__name">{STEP_NAMES[i]}</span>
          <span className="np-steps__detail">{detail}</span><span className="np-steps__result">{result}</span>
        </li>
      ))}
    </ol>
  );
}
export const circled = (i: number) => CIRCLED[i];

export function ProbBars({ rows, threshold }: { rows: [string, number, 'ok' | 'ask' | 'muted'][]; threshold: number }) {
  const t = Math.round(threshold * 100);
  return (
    <div className="np-bars" data-component="ProbBars">
      {rows.map(([name, v, tone]) => (
        <div key={name} className="np-bars__row" data-tone={tone}>
          <span>{name}</span>
          <svg className="np-bars__svg" viewBox="0 0 100 10" preserveAspectRatio="none" aria-hidden="true">
            <rect className="np-bars__track" width="100" height="10" />
            <rect className="np-bars__fill" width={Math.round(v * 100)} height="10" />
            <line className="np-bars__threshold" x1={t} y1="-3" x2={t} y2="13" />
          </svg>
          <span className="np-bars__value">{v.toFixed(2)}</span>
        </div>
      ))}
      <p className="np-bars__legend"><span className="np-bars__legend-mark" aria-hidden="true" />기준선 {threshold.toFixed(2)} · 설정값</p>
    </div>
  );
}

export function Timeline({ items }: { items: ['done' | 'now' | 'next', string, string, string?][] }) {
  return (
    <ol className="np-tl" data-component="Timeline">
      {items.map(([state, time, text, tone], i) => (
        <li key={i} className="np-tl__item" data-state={state} data-tone={tone ?? 'accent'}>
          <span className="np-tl__time">{time}</span><span className="np-tl__text">{text}</span>
        </li>
      ))}
    </ol>
  );
}

export function Checklist({ items }: { items: ['pass' | 'fail', string, string][] }) {
  return (
    <ul className="np-checklist" data-component="Checklist">
      {items.map(([res, name, detail]) => (
        <li key={name} className="np-checklist__item" data-result={res}>
          <span className="np-checklist__mark" aria-hidden="true">{res === 'pass' ? '✓' : '✕'}</span>
          <span className="np-checklist__name">{name}</span><span className="np-checklist__detail">{detail}</span>
        </li>
      ))}
    </ul>
  );
}

const STATES: [string, string][] = [['AVAILABLE', '사용 가능'], ['RESERVED', '예약'], ['ACTIVE', '사용 중'], ['CLOSING', '반납 확인'], ['FAULT', '고장 격리']];
export function StateStrip({ current, tone }: { current: string; tone: string }) {
  return (
    <div className="np-states" data-component="StateStrip">
      {STATES.map(([code, name]) => (
        <span key={code} className="np-states__item" data-current={b(code === current)} data-tone={code === current ? tone : 'none'}>
          <span className="np-states__name">{name}</span><span className="np-states__code">{code}</span>
        </span>
      ))}
    </div>
  );
}

export interface TableRow { key: string; tone?: string; selected?: boolean; cells: ReactNode[] }
export function DataTable({ head, rows, label }: { head: string[]; rows: TableRow[]; label: string }) {
  return (
    <div className="np-table-wrap" data-component="DataTable">
      <table className="np-table" aria-label={label}>
        <thead><tr>{head.map((h) => <th key={h} scope="col">{h}</th>)}</tr></thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.key} data-tone={r.tone ?? 'none'} data-selected={b(r.selected)}>{r.cells}</tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
export const Td = ({ main, sub }: { main: ReactNode; sub?: string }) => (
  <td>{main}{sub ? <span className="np-table__sub">{sub}</span> : null}</td>
);
export const TdLink = ({ main, sub, onClick }: { main: string; sub?: string; onClick: () => void }) => (
  <td><button className="np-rowlink" type="button" onClick={onClick}>{main}{sub ? <span className="np-table__sub">{sub}</span> : null}</button></td>
);
export const TdMono = ({ v }: { v: string }) => <td className="np-table__mono">{v}</td>;

export function FilterBar({ items, onToggle }: { items: [string, boolean][]; onToggle: (label: string) => void }) {
  return (
    <div className="np-filters" data-component="FilterBar" role="group" aria-label="거르기">
      {items.map(([label, on]) => (
        <button key={label} className="np-filter" type="button" aria-pressed={b(on)} onClick={() => onToggle(label)}>{label}</button>
      ))}
    </div>
  );
}

export function Zone({ kind, title, cap, children }: { kind: 'live' | 'eval'; title: string; cap: string; children: ReactNode }) {
  return (
    <section className="np-zone" data-component="Zone" data-kind={kind}>
      <div className="np-zone__head"><h3 className="np-zone__title">{title}</h3><span className="np-cap">{cap}</span></div>
      <div className="np-zone__body">{children}</div>
    </section>
  );
}

export function NoteBox({ text, code }: { text: string; code?: string }) {
  return <div className="np-note" data-component="NoteBox"><span>{text}</span>{code ? <span className="np-note__code">{code}</span> : null}</div>;
}

export function OfflineBanner({ title, detail, onReconnect }: { title: string; detail: string; onReconnect: () => void }) {
  return (
    <div className="np-offline" data-component="OfflineBanner" role="alert">
      <span className="np-offline__title">{title}</span><span className="np-offline__detail">{detail}</span>
      <button className="np-btn" type="button" onClick={onReconnect}>다시 연결</button>
    </div>
  );
}

export function Switch({ label, on, onChange }: { label: string; on: boolean; onChange: (on: boolean) => void }) {
  return (
    <button className="np-switch" data-component="Switch" type="button" role="switch" aria-checked={b(on)} onClick={() => onChange(!on)}>
      <span className="np-switch__track" aria-hidden="true"><span className="np-switch__thumb" /></span><span>{label}</span>
    </button>
  );
}

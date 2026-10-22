import type { KeyboardEvent, PointerEvent } from 'react';

export interface SplitterProps {
  /** 무엇의 크기인가 (CSS 자리와 화면 낭독기 이름) */
  edge: 'phone' | 'panel' | 'rail';
  label: string;
  /** 지금 크기(px) */
  value: number;
  min: number;
  max: number;
  /** 누른 자리(clientX/clientY)에서 새 크기(px)를 구한다 */
  toValue: (e: PointerEvent<HTMLDivElement>) => number;
  onChange: (px: number) => void;
  /** 두 번 누르거나 Home 키: 기본 크기로 */
  onReset: () => void;
}

const STEP = 16;

/** 영역 경계를 끌어 크기를 바꾸는 손잡이 (2026-10-23 사용자 결정). 화살표 키로도 바꾼다. */
export function Splitter({ edge, label, value, min, max, toValue, onChange, onReset }: SplitterProps) {
  const clamp = (v: number) => Math.round(Math.min(max, Math.max(min, v)));
  const vertical = edge !== 'rail';
  // 패널과 레일은 손잡이 반대쪽으로 끌수록 커진다
  const grow = edge === 'phone' ? 1 : -1;
  const onKey = (e: KeyboardEvent<HTMLDivElement>) => {
    const keys: Record<string, number> = vertical ? { ArrowLeft: -STEP * grow, ArrowRight: STEP * grow } : { ArrowUp: STEP, ArrowDown: -STEP };
    if (e.key === 'Home') { e.preventDefault(); onReset(); return; }
    if (keys[e.key] === undefined) return;
    e.preventDefault();
    onChange(clamp(value + keys[e.key]));
  };
  return (
    <div className="np-split" data-edge={edge} data-component="Splitter" role="separator" tabIndex={0} aria-label={label}
      aria-orientation={vertical ? 'vertical' : 'horizontal'} aria-valuenow={Math.round(value)} aria-valuemin={min} aria-valuemax={max}
      onPointerDown={(e) => { if (e.button === 0) e.currentTarget.setPointerCapture(e.pointerId); }}
      onPointerMove={(e) => { if (e.currentTarget.hasPointerCapture(e.pointerId)) onChange(clamp(toValue(e))); }}
      onDoubleClick={onReset} onKeyDown={onKey} />
  );
}

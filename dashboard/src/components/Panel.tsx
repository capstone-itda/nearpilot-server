import type { ReactNode } from 'react';
import { Icon } from './common';

export interface PanelProps {
  kind: 'request' | 'device' | 'uses' | 'approvals' | 'audit' | 'history' | 'trust';
  size?: 'normal' | 'wide';
  cap: string;
  title: string;
  tag?: ReactNode;
  onClose: () => void;
  children: ReactNode;
}

/** 무대 위에 겹쳐 여는 패널 (D1). 무대 크기와 위치는 바뀌지 않는다. */
export function Panel({ kind, size = 'normal', cap, title, tag, onClose, children }: PanelProps) {
  return (
    <aside className="np-panel" data-component="Panel" data-kind={kind} data-size={size} aria-label={`${title} 패널`}>
      <div className="np-panel__head">
        <div><span className="np-cap">{cap}</span><h2 className="np-panel__title">{title}{tag}</h2></div>
        <button className="np-btn np-panel__close" type="button" aria-label="패널 닫기" onClick={onClose}><Icon name="close" /></button>
      </div>
      <div className="np-panel__body">{children}</div>
    </aside>
  );
}

/** 조회 중이거나 실패했을 때 패널 본문 */
export function Loading({ error }: { error?: string }) {
  return <p className="np-sec__hint" role="status">{error ? `불러오지 못했어요 · ${error}` : '불러오는 중…'}</p>;
}

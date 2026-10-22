import { Icon, type IconName } from './common';
import type { PanelKey } from './TopBar';

/** 사이드 메뉴 항목: 목록 패널 다섯과 설정 */
export type SideKey = PanelKey | 'settings';

const ITEMS: [SideKey, string, string][] = [
  ['uses', '점유', '점유·대여 현황'],
  ['approvals', '승인', '승인 현황'],
  ['audit', '감사', '감사 로그'],
  ['history', '이력', '요청·판정 이력'],
  ['trust', '신뢰도', '신뢰도 지표']
];

/** 화면 오른쪽 끝의 세로 메뉴. 같은 항목을 다시 누르면 목록 패널은 닫히고, 설정은 모니터로 돌아간다. */
export function SideMenu({ current, onPick }: { current: SideKey | null; onPick: (k: SideKey) => void }) {
  const item = ([k, short, full]: [SideKey, string, string]) => (
    <button key={k} className="np-side__item" type="button" aria-label={full} title={full} aria-current={current === k ? 'page' : 'false'} onClick={() => onPick(k)}>
      <Icon name={k as IconName} /><span className="np-side__label" aria-hidden="true">{short}</span>
    </button>
  );
  return (
    <nav className="np-side" data-component="SideMenu" aria-label="패널과 설정">
      {ITEMS.map(item)}
      <span className="np-side__spacer" />
      {item(['settings', '설정', '설정'])}
    </nav>
  );
}

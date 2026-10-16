import { Icon } from './common';

export type PanelKey = 'uses' | 'approvals' | 'audit' | 'history' | 'trust';
export type SettingsKey = 'layout' | 'phones' | 'check';
export type MenuKey = PanelKey | SettingsKey;

export const MENU: [PanelKey, string][] = [
  ['uses', '점유·대여 현황'], ['approvals', '승인 현황'], ['audit', '감사 로그'], ['history', '요청·판정 이력'], ['trust', '신뢰도 지표']
];
export const SETTINGS: [SettingsKey, string][] = [['layout', '배치'], ['phones', '폰 연결'], ['check', '점검']];

export function PanelMenu({ open, current, onPick }: { open: boolean; current: MenuKey | null; onPick: (k: MenuKey) => void }) {
  const item = ([k, label]: [MenuKey, string]) => (
    <button key={k} className="np-menu__item" type="button" role="menuitem" aria-current={current === k ? 'true' : 'false'} onClick={() => onPick(k)}>{label}</button>
  );
  return (
    <div className="np-menu" data-component="PanelMenu" data-open={open ? 'true' : 'false'} role="menu" aria-label="패널과 설정">
      <span className="np-menu__group np-cap">패널</span>
      {MENU.map(item)}
      <span className="np-menu__sep" aria-hidden="true" />
      <span className="np-menu__group np-cap">설정</span>
      {SETTINGS.map(item)}
    </div>
  );
}

export interface TopBarProps {
  theme: 'dark' | 'light';
  live: boolean;
  /** 마지막 이벤트 수신 시각. 스트림이 들어올 때마다 바뀐다. */
  receivedAt: string;
  /** 목 어댑터의 데이터다 */
  mock?: boolean;
  placeName: string;
  menuOpen: boolean;
  current: MenuKey | null;
  onToggleTheme: () => void;
  onToggleMenu: () => void;
  onPick: (k: MenuKey) => void;
}

export function TopBar({ theme, live, receivedAt, mock, placeName, menuOpen, current, onToggleTheme, onToggleMenu, onPick }: TopBarProps) {
  const toLight = theme === 'dark';
  return (
    <header className="np-topbar" data-component="TopBar">
      <div className="np-topbar__brand">
        <svg className="np-topbar__logo" viewBox="0 0 28 28" aria-hidden="true">
          <path className="np-topbar__logo-frame" d="M2 9V2h7M19 2h7v7M26 19v7h-7M9 26H2v-7" />
          <circle className="np-topbar__logo-ring" cx="14" cy="14" r="7" />
          <circle className="np-topbar__logo-dot" cx="14" cy="14" r="3" />
        </svg>
        <div className="np-topbar__name"><span className="np-topbar__wordmark">NEARPILOT</span><span className="np-cap">운영 모니터</span></div>
      </div>
      <span className="np-topbar__divider" aria-hidden="true" />
      <div className="np-topbar__place"><span className="np-cap">장소</span><span className="np-topbar__place-name">{placeName}</span></div>
      <span className="np-topbar__spacer" />
      <div className="np-topbar__live" data-live={live ? 'true' : 'false'} role="status">
        <span className="np-topbar__live-dot" aria-hidden="true" /><span>{live ? '실시간 연결됨' : '연결 끊김'}</span>
      </div>
      {mock ? <span className="np-topbar__mock np-cap">목 데이터</span> : null}
      <span className="np-topbar__recv" aria-hidden="true">수신 {receivedAt}</span>
      <div className="np-topbar__actions">
        <button className="np-btn" data-variant="quiet" type="button" aria-label={toLight ? '라이트 모드로 바꾸기' : '다크 모드로 바꾸기'} onClick={onToggleTheme}>
          <Icon name="sun" /><Icon name="moon" /><span>{toLight ? '라이트' : '다크'}</span>
        </button>
        <button className="np-btn" type="button" aria-haspopup="menu" aria-expanded={menuOpen ? 'true' : 'false'} onClick={onToggleMenu}>
          <Icon name="menu" /><span>메뉴</span>
        </button>
        <PanelMenu open={menuOpen} current={current} onPick={onPick} />
      </div>
    </header>
  );
}

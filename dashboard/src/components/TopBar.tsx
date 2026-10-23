import { Icon } from './common';

export type PanelKey = 'uses' | 'approvals' | 'audit' | 'history' | 'trust';
export type SettingsKey = 'layout' | 'phones' | 'check';

export interface TopBarProps {
  theme: 'dark' | 'light';
  live: boolean;
  /** 마지막 이벤트 수신 시각. 스트림이 들어올 때마다 바뀐다. */
  receivedAt: string;
  /** 목 어댑터의 데이터다 */
  mock?: boolean;
  placeName: string;
  onToggleTheme: () => void;
}

export function TopBar({ theme, live, receivedAt, mock, placeName, onToggleTheme }: TopBarProps) {
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
        {/* 글자 없이 아이콘만 둔다. 지금 테마가 아니라 누르면 바뀔 테마를 그린다(다크에서는 해, 라이트에서는 달). */}
        <button className="np-btn np-topbar__theme" data-variant="quiet" type="button" aria-label={toLight ? '라이트 모드로 바꾸기' : '다크 모드로 바꾸기'} title={toLight ? '라이트 모드로 바꾸기' : '다크 모드로 바꾸기'} onClick={onToggleTheme}>
          <Icon name="sun" /><Icon name="moon" />
        </button>
      </div>
    </header>
  );
}

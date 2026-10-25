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
  /** 고를 수 있는 장소. 비어 있으면 자동만 남고 누를 수 없다. */
  places: { placeId: string; placeName: string }[];
  /** 지금 무대에 크게 그리는 장소 */
  current?: string;
  /** 사람이 고정한 장소. 자동이면 undefined */
  locked?: string;
  /** 장소를 고정한다. undefined면 자동으로 푼다. */
  onLock: (placeId: string | undefined) => void;
  onToggleTheme: () => void;
}

export function TopBar({ theme, live, receivedAt, mock, places, current, locked, onLock, onToggleTheme }: TopBarProps) {
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
      <div className="np-topbar__place" data-component="PlaceChooser" data-locked={locked ? 'true' : 'false'}>
        <span className="np-cap" id="np-place-cap">장소</span>
        {/* 고르면 그 장소로 고정하고, 자동을 누르면 푼다. 폭이 변하지 않게 지금 보이는 장소의 점 자리를 늘 둔다. */}
        <div className="np-topbar__chooser" role="group" aria-labelledby="np-place-cap">
          <button className="np-btn np-topbar__pick" type="button" aria-pressed={!locked} disabled={!places.length} onClick={() => onLock(undefined)}>자동</button>
          {places.map((p) => (
            <button key={p.placeId} className="np-btn np-topbar__pick" type="button" data-place={p.placeId} aria-pressed={locked === p.placeId} aria-current={current === p.placeId ? 'true' : undefined} onClick={() => onLock(p.placeId)}>
              <span className="np-topbar__pick-dot" aria-hidden="true" />{p.placeName}
            </button>
          ))}
        </div>
      </div>
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

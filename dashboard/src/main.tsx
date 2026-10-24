import { StrictMode, useMemo, useState } from 'react';
import { createRoot } from 'react-dom/client';
import '@fontsource/ibm-plex-sans-kr/400.css';
import '@fontsource/ibm-plex-sans-kr/500.css';
import '@fontsource/ibm-plex-sans-kr/600.css';
import '@fontsource/ibm-plex-mono/400.css';
import '@fontsource/ibm-plex-mono/500.css';
import '@fontsource/ibm-plex-mono/600.css';
import './styles/tokens.css';
import './styles/base.css';
import './styles/TopBar.css';
import './styles/PhoneMirror.css';
import './styles/Stage.css';
import './styles/FlowRail.css';
import './styles/Panel.css';
import './styles/Settings.css';
import { App, type AppProps } from './App';
import { TokenGate } from './components/TokenGate';
import { createMockSource, isSceneName } from './data/mock/mockSource';
import { createServerSource } from './data/server/serverSource';
import { clearToken, loadToken, saveToken } from './data/server/token';
import { loadPrefs } from './prefs';

// 주소 값: ?scene=idle|ask|judging|flow|done|fault|approval|later|side-room|offline|play|live (목 데이터 장면, live는 이벤트가 시각 순서대로 흐름)
//          &view=layout|phones|check  &panel=uses|approvals|audit|history|trust|device:<id>|request:<id>  &theme=dark|light
//          &source=server (목 데이터 대신 NearPilot 서버에 붙는다. 소유자 토큰은 입력 화면에서 받고 주소에는 쓰지 않는다)
const q = new URLSearchParams(location.search);
const scene = q.get('scene');

const view = q.get('view');
const panel = q.get('panel');
const theme = q.get('theme');
const props: Omit<AppProps, 'source'> = {
  initialView: view === 'layout' || view === 'phones' || view === 'check' ? view : 'monitor',
  initialTheme: theme === 'light' || theme === 'dark' ? theme : undefined
};
if (panel) {
  const [kind, id] = panel.split(':');
  if (kind === 'device' && id) props.initialPanel = { kind: 'device', nodeId: id };
  else if (kind === 'request' && id) props.initialPanel = { kind: 'request', requestId: id };
  else if (kind === 'uses' || kind === 'approvals' || kind === 'audit' || kind === 'history' || kind === 'trust') props.initialPanel = { kind };
}

/** 서버 어댑터: 토큰이 없으면 입력 화면을 보인다. 서버가 토큰을 거부하면 토큰을 지우고 이유와 함께 입력 화면으로 돌아간다. */
function ServerApp() {
  const [token, setToken] = useState(loadToken);
  const [reason, setReason] = useState<'invalid' | 'not_owner'>();
  const source = useMemo(() => token ? createServerSource({
    token,
    // 앞서 거부된 토큰의 늦은 응답이 새로 넣은 토큰을 지우지 않게, 지금 토큰의 거부만 받는다
    onRejected: (why) => { if (loadToken() !== token) return; clearToken(); setToken(null); setReason(why); }
  }) : undefined, [token]);
  if (!token || !source) {
    return <TokenGate theme={props.initialTheme ?? loadPrefs().theme} reason={reason} onSubmit={(t) => { saveToken(t); setReason(undefined); setToken(t); }} />;
  }
  return <App source={source} {...props} />;
}

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    {q.get('source') === 'server'
      ? <ServerApp />
      : <App source={createMockSource({ scene: scene === 'play' || scene === 'live' ? scene : isSceneName(scene) ? scene : 'idle' })} {...props} />}
  </StrictMode>
);

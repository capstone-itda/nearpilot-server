import { StrictMode } from 'react';
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
import { createMockSource, isSceneName } from './data/mock/mockSource';

// 주소 값: ?scene=idle|ask|flow|fault|approval|later|offline|play (목 데이터 장면)
//          &view=layout|phones|check  &panel=uses|approvals|audit|history|trust|device:<id>|request:<id>  &theme=dark|light
const q = new URLSearchParams(location.search);
const scene = q.get('scene');
const source = createMockSource({ scene: scene === 'play' ? 'play' : isSceneName(scene) ? scene : 'idle' });

const view = q.get('view');
const panel = q.get('panel');
const theme = q.get('theme');
const props: AppProps = {
  source,
  initialView: view === 'layout' || view === 'phones' || view === 'check' ? view : 'monitor',
  initialTheme: theme === 'light' || theme === 'dark' ? theme : undefined
};
if (panel) {
  const [kind, id] = panel.split(':');
  if (kind === 'device' && id) props.initialPanel = { kind: 'device', nodeId: id };
  else if (kind === 'request' && id) props.initialPanel = { kind: 'request', requestId: id };
  else if (kind === 'uses' || kind === 'approvals' || kind === 'audit' || kind === 'history' || kind === 'trust') props.initialPanel = { kind };
}

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App {...props} />
  </StrictMode>
);

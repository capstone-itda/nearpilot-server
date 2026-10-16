import { useEffect, useMemo, useRef, useState } from 'react';
import type { DataSource } from './data/source';
import { DataCtx, type Ctx } from './data/context';
import type { FloorLayout, RequestProgress, Snapshot } from './model/types';
import { OfflineBanner } from './components/common';
import { FlowRail } from './components/FlowRail';
import { PhoneMirror } from './components/PhoneMirror';
import { Stage } from './components/Stage';
import { TopBar, type MenuKey, type PanelKey, type SettingsKey } from './components/TopBar';
import { DevicePanel } from './panels/DevicePanel';
import { ApprovalsPanel, AuditPanel, HistoryPanel, TrustPanel, UsesPanel } from './panels/ListPanels';
import { RequestPanel } from './panels/RequestPanel';
import { Check, LayoutCheck, Phones, SettingsNav } from './settings/Settings';
import { choosePhone, type FollowSettings } from './view/phone';
import { railView } from './view/rail';
import { stageView } from './view/stage';
import { loadPrefs, savePrefs } from './prefs';

export type PanelState =
  | { kind: 'request'; request: RequestProgress; live: boolean }
  | { kind: 'device'; nodeId: string }
  | { kind: PanelKey };
export type ViewState = 'monitor' | SettingsKey;

export interface AppProps {
  source: DataSource;
  /** 처음 화면 (주소의 view, panel 값) */
  initialView?: ViewState;
  initialPanel?: { kind: PanelKey } | { kind: 'device'; nodeId: string } | { kind: 'request'; requestId: string };
  initialTheme?: 'dark' | 'light';
}

const SETTINGS_KEYS: SettingsKey[] = ['layout', 'phones', 'check'];

export function App({ source, initialView = 'monitor', initialPanel, initialTheme }: AppProps) {
  const prefs = useMemo(loadPrefs, []);
  const [theme, setTheme] = useState<'dark' | 'light'>(initialTheme ?? prefs.theme);
  const [follow, setFollow] = useState<FollowSettings>(prefs.follow);
  const [layout, setLayout] = useState<FloorLayout>();
  const [snapshot, setSnapshot] = useState<Snapshot>();
  const [view, setView] = useState<ViewState>(initialView);
  const [panel, setPanel] = useState<PanelState | null>(null);
  const [menuOpen, setMenuOpen] = useState(false);
  const [streams, setStreams] = useState<Record<string, MediaStream>>({});
  const shownPerson = useRef<string>();
  const seen = useRef<{ init?: boolean; request?: string; faults: Set<string> }>({ faults: new Set() });

  useEffect(() => { savePrefs({ theme, follow }); }, [theme, follow]);
  useEffect(() => { source.layout().then(setLayout, () => undefined); }, [source]);
  useEffect(() => source.subscribe(setSnapshot), [source]);

  // 처음 화면의 패널
  useEffect(() => {
    if (!initialPanel) return;
    if (initialPanel.kind === 'request') source.request(initialPanel.requestId).then((request) => setPanel({ kind: 'request', request, live: false }), () => undefined);
    else setPanel(initialPanel);
  }, [source, initialPanel]);

  // 새 ASK·NEED_APPROVAL 판정과 새 고장 격리는 상세 패널을 자동으로 연다.
  // 첫 스냅숏에서는 주소로 연 패널이 없을 때, 진행 중인 요청에 딸린 것만 연다.
  useEffect(() => {
    if (!snapshot) return;
    const s = seen.current;
    const first = !s.init;
    s.init = true;
    const r = snapshot.request;
    const key = r ? `${r.id}:${r.verdict}` : undefined;
    const faults = snapshot.devices.filter((d) => d.state === 'FAULT').map((d) => d.nodeId);
    const newFaults = faults.filter((id) => !s.faults.has(id) && (!first || id === r?.nodeId));
    const auto = !(first && initialPanel);
    if (auto && r && key !== s.request && (r.verdict === 'ASK' || r.verdict === 'NEED_APPROVAL')) setPanel({ kind: 'request', request: r, live: true });
    else if (auto && newFaults.length) setPanel({ kind: 'device', nodeId: newFaults[0] });
    else if (r) setPanel((p) => (p && p.kind === 'request' && p.live && p.request.id === r.id ? { ...p, request: r } : p));
    s.request = key;
    s.faults = new Set(faults);
  }, [snapshot, initialPanel]);

  // 메뉴는 Esc로 닫는다
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') setMenuOpen(false); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);

  if (!layout || !snapshot) return <div className="np-app" data-theme={theme}><p className="np-loading" role="status">불러오는 중…</p></div>;

  const people = new Map(snapshot.people.map((p) => [p.id, p.pseudonym]));
  const ctx: Ctx = {
    source, layout, snapshot,
    personName: (id) => people.get(id) ?? '알 수 없음',
    deviceName: (id) => layout.devices.find((d) => d.nodeId === id)?.name ?? id
  };

  const pick = (k: MenuKey) => {
    setMenuOpen(false);
    if ((SETTINGS_KEYS as string[]).includes(k)) { setView(k as SettingsKey); return; }
    setView('monitor');
    setPanel({ kind: k as PanelKey });
  };
  const closePanel = () => setPanel(null);
  const openRequest = (id: string) => source.request(id).then((request) => setPanel({ kind: 'request', request, live: snapshot.request?.id === id }), () => undefined);
  const pickWindow = (personId: string) => {
    navigator.mediaDevices?.getDisplayMedia({ video: true, audio: false }).then((stream) => {
      stream.getVideoTracks()[0]?.addEventListener('ended', () => setStreams((s) => { const n = { ...s }; delete n[personId]; return n; }));
      setStreams((s) => ({ ...s, [personId]: stream }));
    }, () => undefined);
  };

  const current: MenuKey | null = view !== 'monitor' ? view : panel && panel.kind !== 'request' && panel.kind !== 'device' ? panel.kind : null;
  const topBar = (
    <TopBar theme={theme} live={snapshot.connected} placeName={layout.placeName} menuOpen={menuOpen} current={current}
      onToggleTheme={() => setTheme((t) => (t === 'dark' ? 'light' : 'dark'))} onToggleMenu={() => setMenuOpen((o) => !o)} onPick={pick} />
  );

  let screen;
  if (view === 'monitor') {
    const phone = choosePhone(snapshot, follow, shownPerson.current);
    shownPerson.current = phone.personId;
    const mock = snapshot.mockPhone && snapshot.mockPhone.personId === phone.personId ? snapshot.mockPhone : undefined;
    screen = (
      <div className="np-monitor" data-component="MonitorLayout" data-live={snapshot.connected ? 'true' : 'false'}>
        {topBar}
        <PhoneMirror who={phone.who} follow={phone.follow} stream={phone.personId ? streams[phone.personId] : undefined} mock={mock} />
        <Stage v={stageView(snapshot, layout, { panelOpen: !!panel })} />
        <FlowRail v={railView(snapshot, layout, phone.personId)} />
        {!snapshot.connected ? (
          <OfflineBanner title="서버와 연결이 끊겼어요" detail={`마지막 수신 ${snapshot.lastReceivedAt} · 다시 연결 시도 ${snapshot.reconnectAttempts}회째`} onReconnect={() => source.reconnect()} />
        ) : null}
        {panel ? <PanelView panel={panel} onClose={closePanel} onOpenDevice={(nodeId) => setPanel({ kind: 'device', nodeId })} onOpenRequest={openRequest} /> : null}
      </div>
    );
  } else {
    screen = (
      <div className="np-settings" data-component="SettingsLayout">
        {topBar}
        <SettingsNav current={view} onPick={setView} onBack={() => setView('monitor')} />
        {view === 'layout' ? <LayoutCheck /> : view === 'phones' ? <Phones streams={streams} onPick={pickWindow} follow={follow} onFollow={setFollow} /> : <Check />}
      </div>
    );
  }

  return (
    <DataCtx.Provider value={ctx}>
      <div className="np-app" data-theme={theme}>{screen}</div>
    </DataCtx.Provider>
  );
}

function PanelView({ panel, onClose, onOpenDevice, onOpenRequest }: {
  panel: PanelState; onClose: () => void; onOpenDevice: (nodeId: string) => void; onOpenRequest: (id: string) => void;
}) {
  switch (panel.kind) {
    case 'request': return <RequestPanel r={panel.request} onClose={onClose} />;
    case 'device': return <DevicePanel nodeId={panel.nodeId} onClose={onClose} />;
    case 'uses': return <UsesPanel onClose={onClose} onOpenDevice={onOpenDevice} />;
    case 'approvals': return <ApprovalsPanel onClose={onClose} />;
    case 'audit': return <AuditPanel onClose={onClose} />;
    case 'history': return <HistoryPanel onClose={onClose} onOpenRequest={onOpenRequest} />;
    case 'trust': return <TrustPanel onClose={onClose} />;
  }
}

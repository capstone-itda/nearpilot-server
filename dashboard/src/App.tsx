import { useEffect, useMemo, useRef, useState } from 'react';
import type { DataSource } from './data/source';
import { DataCtx, type Ctx } from './data/context';
import { initialState, reduceAll } from './data/reduce';
import type { PlaceInfo, RequestProgress, MonitorState } from './model/types';
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
import { canPlace, resolveArrangement, withArrangement, type Arrangement } from './view/arrange';
import { clearArrangement, loadArrangement, loadPrefs, saveArrangement, savePrefs } from './prefs';

/** 무대 위를 넓게 덮는 패널 (ListPanels의 size="wide") */
const WIDE_PANELS: string[] = ['audit', 'history', 'trust'];

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
  const [place, setPlace] = useState<PlaceInfo>();
  const [arrangement, setArrangement] = useState<Arrangement>({});
  const [snapshot, setSnapshot] = useState<MonitorState>();
  const [view, setView] = useState<ViewState>(initialView);
  const [panel, setPanel] = useState<PanelState | null>(null);
  const [menuOpen, setMenuOpen] = useState(false);
  const [streams, setStreams] = useState<Record<string, MediaStream>>({});
  const shownPerson = useRef<string>();
  const seen = useRef<{ init?: boolean; request?: string; faults: Set<string> }>({ faults: new Set() });

  useEffect(() => { savePrefs({ theme, follow }); }, [theme, follow]);
  useEffect(() => {
    source.place().then((p) => { setPlace(p); setArrangement(resolveArrangement(p.devices, loadArrangement(p.placeName))); }, () => undefined);
  }, [source]);
  const layout = useMemo(() => (place ? withArrangement(place, arrangement) : undefined), [place, arrangement]);
  // 받은 이벤트 묶음을 차례로 쌓는다. 기기 목록이 있어야 처음 상태를 만들 수 있어서 장소를 받은 뒤 구독한다.
  useEffect(() => {
    if (!place) return;
    return source.subscribe((events) => setSnapshot((s) => reduceAll(s ?? initialState(place), events)));
  }, [source, place]);

  // 처음 화면의 패널
  useEffect(() => {
    if (!initialPanel) return;
    if (initialPanel.kind === 'request') source.request(initialPanel.requestId).then((request) => setPanel({ kind: 'request', request, live: false }), () => undefined);
    else setPanel(initialPanel);
  }, [source, initialPanel]);

  // 새 ASK·NEED_APPROVAL 판정과 새 고장 격리는 상세 패널을 자동으로 연다.
  // 첫 상태에서는 주소로 연 패널이 없을 때, 진행 중인 요청에 딸린 것만 연다.
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
    // 사람이 메뉴로 연 목록 패널(점유, 승인, 감사, 이력, 신뢰도)은 자동 패널로 바꾸지 않는다
    const keepList = (next: PanelState) => (p: PanelState | null) => (p && p.kind !== 'request' && p.kind !== 'device' ? p : next);
    if (auto && r && key !== s.request && (r.verdict === 'ASK' || r.verdict === 'NEED_APPROVAL')) setPanel(keepList({ kind: 'request', request: r, live: true }));
    else if (auto && newFaults.length) setPanel(keepList({ kind: 'device', nodeId: newFaults[0] }));
    else setPanel((p) => {
      // live: 흐름을 따라가는 상세. 다음 요청이 오면 닫고, 요청이 화면에서 내려가기만 하면 마지막 내용으로 남긴다.
      if (!p || p.kind !== 'request' || !p.live || !r) return p;
      return p.request.id === r.id ? { ...p, request: r } : null;
    });
    s.request = key;
    s.faults = new Set(faults);
  }, [snapshot, initialPanel]);

  // Esc는 메뉴를 먼저 닫고, 메뉴가 닫혀 있으면 패널을 닫는다
  const menuRef = useRef(menuOpen);
  menuRef.current = menuOpen;
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape') return;
      if (menuRef.current) setMenuOpen(false);
      else setPanel(null);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);

  if (!place || !layout || !snapshot) return <div className="np-app" data-theme={theme}><p className="np-loading" role="status">불러오는 중…</p></div>;

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

  const arrange = {
    arrangement,
    canPlace: (nodeId: string, s: { x: number; y: number }) => canPlace(arrangement, nodeId, s),
    onChange: (next: Arrangement) => { const r = resolveArrangement(place.devices, next); setArrangement(r); saveArrangement(place.placeName, r); },
    onReset: () => { clearArrangement(place.placeName); setArrangement(resolveArrangement(place.devices)); }
  };

  const current: MenuKey | null = view !== 'monitor' ? view : panel && panel.kind !== 'request' && panel.kind !== 'device' ? panel.kind : null;
  const topBar = (
    <TopBar theme={theme} live={snapshot.connected} receivedAt={snapshot.lastReceivedAt} mock={source.mock} placeName={layout.placeName} menuOpen={menuOpen} current={current}
      onToggleTheme={() => setTheme((t) => (t === 'dark' ? 'light' : 'dark'))} onToggleMenu={() => setMenuOpen((o) => !o)} onPick={pick} />
  );

  let screen;
  if (view === 'monitor') {
    const phone = choosePhone(snapshot, follow, shownPerson.current);
    shownPerson.current = phone.personId;
    const mock = snapshot.mockPhones?.find((m) => m.personId === phone.personId);
    screen = (
      <div className="np-monitor" data-component="MonitorLayout" data-live={snapshot.connected ? 'true' : 'false'}>
        {topBar}
        <PhoneMirror who={phone.who} follow={phone.follow} stream={phone.personId ? streams[phone.personId] : undefined} mock={mock} />
        <Stage v={stageView(snapshot, layout, { panelOpen: !!panel, panelWide: !!panel && WIDE_PANELS.includes(panel.kind) })} />
        <FlowRail v={railView(snapshot, layout, phone.personId)} onOpen={snapshot.request ? () => setPanel({ kind: 'request', request: snapshot.request!, live: true }) : undefined} />
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
        {view === 'layout' ? <LayoutCheck {...arrange} /> : view === 'phones' ? <Phones streams={streams} onPick={pickWindow} follow={follow} onFollow={setFollow} /> : <Check />}
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

import { useEffect, useMemo, useRef, useState } from 'react';
import { flushSync } from 'react-dom';
import type { DataSource } from './data/source';
import { DataCtx, type Ctx } from './data/context';
import { initialState, reduceAll } from './data/reduce';
import { paceJudging } from './data/pace';
import type { PlaceInfo, RequestProgress, MonitorState } from './model/types';
import { OfflineBanner } from './components/common';
import { FlowRail } from './components/FlowRail';
import { PhoneMirror } from './components/PhoneMirror';
import { Stage } from './components/Stage';
import { SideMenu, type SideKey } from './components/SideMenu';
import { Splitter } from './components/Splitter';
import { TopBar, type PanelKey, type SettingsKey } from './components/TopBar';
import { DevicePanel } from './panels/DevicePanel';
import { ApprovalsPanel, AuditPanel, HistoryPanel, TrustPanel, UsesPanel } from './panels/ListPanels';
import { RequestPanel } from './panels/RequestPanel';
import { Check, LayoutCheck, Phones, SettingsTabs } from './settings/Settings';
import { choosePhone, type FollowSettings } from './view/phone';
import { railView } from './view/rail';
import { stageView } from './view/stage';
import { canPlace, resolveArrangement, withArrangement, type Arrangement } from './view/arrange';
import { clearArrangement, loadArrangement, loadPrefs, saveArrangement, savePrefs, type Sizes } from './prefs';
import { isFit, panelEdgeOf, type ZoomView } from './view/zoom';

/** 무대 위를 넓게 덮는 패널 (ListPanels의 size="wide") */
const WIDE_PANELS: string[] = ['audit', 'history', 'trust'];

/** 영역의 기본 크기와 끌어서 바꿀 수 있는 범위(px). 화면 크기에 따라 잰다. */
function sizeLimits() {
  const vw = window.innerWidth, vh = window.innerHeight;
  const rem = parseFloat(getComputedStyle(document.documentElement).fontSize) || 16;
  return {
    phone: { base: vw * 0.24, min: 240, max: vw * 0.4 },
    panel: { base: vw * 0.3, min: 360, max: vw * 0.6 },
    wide: { base: vw * 0.45, min: 480, max: vw * 0.7 },
    rail: { base: rem * 11, min: 140, max: vh * 0.4 }
  };
}

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

export function App({ source, initialView = 'monitor', initialPanel, initialTheme }: AppProps) {
  const prefs = useMemo(loadPrefs, []);
  const [theme, setTheme] = useState<'dark' | 'light'>(initialTheme ?? prefs.theme);
  const [follow, setFollow] = useState<FollowSettings>(prefs.follow);
  const [zoomView, setZoomView] = useState<ZoomView>(prefs.stage);
  const [sizes, setSizes] = useState<Sizes>(prefs.sizes);
  const [stageSize, setStageSize] = useState<{ w: number; h: number; right: number }>();
  const [place, setPlace] = useState<PlaceInfo>();
  const [arrangement, setArrangement] = useState<Arrangement>({});
  const [snapshot, setSnapshot] = useState<MonitorState>();
  const [view, setView] = useState<ViewState>(initialView);
  const [panel, setPanel] = useState<PanelState | null>(null);
  // 설정을 다시 열면 마지막에 본 탭으로 간다
  const [lastSettings, setLastSettings] = useState<SettingsKey>(initialView === 'monitor' ? 'layout' : initialView);
  // 처음 그린 화면은 강조하지 않는다. 그 뒤로 바뀌는 곳만 도착 강조를 보인다.
  const [motion, setMotion] = useState(false);
  const [streams, setStreams] = useState<Record<string, MediaStream>>({});
  const shownPerson = useRef<string>();
  const seen = useRef<{ init?: boolean; request?: string; faults: Set<string> }>({ faults: new Set() });

  useEffect(() => { savePrefs({ theme, follow, stage: zoomView, sizes }); }, [theme, follow, zoomView, sizes]);
  const loaded = !!snapshot;
  useEffect(() => {
    if (!loaded) return;
    const t = setTimeout(() => setMotion(true), 800);
    return () => clearTimeout(t);
  }, [loaded]);
  useEffect(() => {
    source.place().then((p) => { setPlace(p); setArrangement(resolveArrangement(p.devices, loadArrangement(p.placeName))); }, () => undefined);
  }, [source]);
  const layout = useMemo(() => (place ? withArrangement(place, arrangement) : undefined), [place, arrangement]);
  // 받은 이벤트 묶음을 차례로 쌓는다. 기기 목록이 있어야 처음 상태를 만들 수 있어서 장소를 받은 뒤 구독한다.
  useEffect(() => {
    if (!place) return;
    // 판정 결과는 판정 게이지가 찬 뒤에 넘긴다
    const pacer = paceJudging((events) => setSnapshot((s) => reduceAll(s ?? initialState(place), events)));
    const off = source.subscribe(pacer.push);
    return () => { off(); pacer.stop(); };
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
    // 사람이 사이드 메뉴로 연 목록 패널(점유, 승인, 감사, 이력, 신뢰도)은 자동 패널로 바꾸지 않는다
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

  // Esc는 설정 화면이면 설정을 닫고, 모니터면 패널을 닫는다
  const viewRef = useRef(view);
  viewRef.current = view;
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape') return;
      if (viewRef.current !== 'monitor') setView('monitor');
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

  const isList = (p: PanelState | null): p is { kind: PanelKey } => !!p && p.kind !== 'request' && p.kind !== 'device';
  const current: SideKey | null = isList(panel) ? panel.kind : null;
  // 사이드 메뉴(모니터에만 있다): 같은 항목을 다시 누르면 목록 패널이 닫힌다. 설정은 마지막에 본 탭으로 연다.
  const pick = (k: SideKey) => {
    if (k === 'settings') { setView(lastSettings); return; }
    setPanel(current === k ? null : { kind: k });
  };
  const pickSettings = (k: SettingsKey) => { setLastSettings(k); setView(k); };
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

  // 테마는 화면 전체를 천천히 겹쳐 바꾼다(styles/base.css의 view-transition). 움직임 줄이기 설정이면 바로 바꾼다.
  const toggleTheme = () => {
    const next = theme === 'dark' ? 'light' : 'dark';
    const doc = document as Document & { startViewTransition?: (cb: () => void) => unknown };
    if (doc.startViewTransition && !matchMedia('(prefers-reduced-motion: reduce)').matches) doc.startViewTransition(() => flushSync(() => setTheme(next)));
    else setTheme(next);
  };
  const topBar = (
    <TopBar theme={theme} live={snapshot.connected} receivedAt={snapshot.lastReceivedAt} mock={source.mock} placeName={layout.placeName}
      onToggleTheme={toggleTheme} />
  );
  const side = <SideMenu current={current} onPick={pick} />;

  let screen;
  if (view === 'monitor') {
    const phone = choosePhone(snapshot, follow, shownPerson.current);
    shownPerson.current = phone.personId;
    const mock = snapshot.mockPhones?.find((m) => m.personId === phone.personId);
    const wide = !!panel && WIDE_PANELS.includes(panel.kind);
    const lim = sizeLimits();
    const px = (k: keyof Sizes) => sizes[k] ?? lim[k].base;
    const setSize = (k: keyof Sizes) => (v: number) => setSizes((s) => ({ ...s, [k]: v }));
    const resetSize = (k: keyof Sizes) => () => setSizes((s) => ({ ...s, [k]: undefined }));
    // 패널 왼쪽 끝을 무대 좌표로 바꿔, 방 밀기와 콜아웃이 실제 패널 크기를 따른다
    const panelEdge = panel && stageSize ? panelEdgeOf(zoomView, stageSize, px(wide ? 'wide' : 'panel') - stageSize.right) : undefined;
    // 끌어 바꾼 크기는 CSS 변수로 넘긴다 (요소에 style 속성을 쓰지 않는다)
    const vars = (['phone', 'panel', 'wide', 'rail'] as const).filter((k) => sizes[k] !== undefined)
      .map((k) => `--${{ phone: 'phone-w', panel: 'panel-w', wide: 'panel-wide-w', rail: 'rail-h' }[k]}: ${Math.round(sizes[k]!)}px;`).join(' ');
    screen = (
      <div className="np-monitor" data-component="MonitorLayout" data-live={snapshot.connected ? 'true' : 'false'} data-panel={panel ? (wide ? 'wide' : 'normal') : 'none'}>
        {vars ? <style>{`.np-app .np-monitor { ${vars} }`}</style> : null}
        {topBar}
        <PhoneMirror who={phone.who} follow={phone.follow} stream={phone.personId ? streams[phone.personId] : undefined} mock={mock} />
        <Stage v={stageView(snapshot, layout, { panelOpen: !!panel, panelWide: wide, panelEdge, viewFixed: !isFit(zoomView) })}
          zoom={{ view: zoomView, onChange: setZoomView, onSize: setStageSize }} />
        <Splitter edge="phone" label="폰 화면 폭" value={px('phone')} min={lim.phone.min} max={lim.phone.max} toValue={(e) => e.clientX} onChange={setSize('phone')} onReset={resetSize('phone')} />
        <Splitter edge="rail" label="흐름 레일 높이" value={px('rail')} min={lim.rail.min} max={lim.rail.max} toValue={(e) => window.innerHeight - e.clientY} onChange={setSize('rail')} onReset={resetSize('rail')} />
        <FlowRail v={railView(snapshot, layout, phone.personId)} onOpen={snapshot.request ? () => setPanel({ kind: 'request', request: snapshot.request!, live: true }) : undefined} />
        {!snapshot.connected ? (
          <OfflineBanner title="서버와 연결이 끊겼어요" detail={`마지막 수신 ${snapshot.lastReceivedAt} · 다시 연결 시도 ${snapshot.reconnectAttempts}회째`} onReconnect={() => source.reconnect()} />
        ) : null}
        {side}
        {panel ? (
          <Splitter edge="panel" label="패널 폭" value={px(wide ? 'wide' : 'panel')} min={lim[wide ? 'wide' : 'panel'].min} max={lim[wide ? 'wide' : 'panel'].max}
            toValue={(e) => (document.querySelector('.np-side')?.getBoundingClientRect().left ?? window.innerWidth) - e.clientX}
            onChange={setSize(wide ? 'wide' : 'panel')} onReset={resetSize(wide ? 'wide' : 'panel')} />
        ) : null}
        {panel ? <PanelView panel={panel} onClose={closePanel} onOpenDevice={(nodeId) => setPanel({ kind: 'device', nodeId })} onOpenRequest={openRequest} /> : null}
      </div>
    );
  } else {
    screen = (
      <div className="np-settings" data-component="SettingsLayout">
        {topBar}
        <SettingsTabs current={view} onPick={pickSettings} onClose={() => setView('monitor')} />
        {view === 'layout' ? <LayoutCheck {...arrange} /> : view === 'phones' ? <Phones streams={streams} onPick={pickWindow} follow={follow} onFollow={setFollow} /> : <Check />}
      </div>
    );
  }

  return (
    <DataCtx.Provider value={ctx}>
      <div className="np-app" data-theme={theme} data-motion={motion ? 'on' : 'off'}>{screen}</div>
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

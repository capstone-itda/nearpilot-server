import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { flushSync } from 'react-dom';
import type { DataSource } from './data/source';
import { DataCtx, type Ctx } from './data/context';
import { initialState, reduceAll } from './data/reduce';
import { paceJudging } from './data/pace';
import type { FloorLayout, PlacesInfo, RequestProgress, MonitorState } from './model/types';
import { OfflineBanner } from './components/common';
import { FlowRail } from './components/FlowRail';
import { PhoneMirror } from './components/PhoneMirror';
import { Stage, StageEmpty } from './components/Stage';
import { SideMenu, type SideKey } from './components/SideMenu';
import { Splitter } from './components/Splitter';
import { TopBar, type PanelKey, type SettingsKey } from './components/TopBar';
import { DevicePanel } from './panels/DevicePanel';
import { ApprovalsPanel, AuditPanel, HistoryPanel, TrustPanel, UsesPanel } from './panels/ListPanels';
import { RequestPanel } from './panels/RequestPanel';
import { Check, LayoutCheck, Phones, SettingsTabs, type MirrorGuideState } from './settings/Settings';
import { insetView } from './view/inset';
import { choosePhone, type FollowSettings } from './view/phone';
import { nextFocus, type StageFocus } from './view/places';
import { railView } from './view/rail';
import { stageView } from './view/stage';
import { canPlace, resolveArrangement, withArrangement, type Arrangement } from './view/arrange';
import { clearArrangement, loadArrangement, loadMirrorPending, loadPrefs, saveArrangement, saveMirrorPending, savePrefs, type Sizes } from './prefs';
import { guideFirst, mirrorFailure, platformOf, type MirrorFailure } from './view/mirror';
import { isFit, panelEdgeOf, type ZoomView } from './view/zoom';

/** 무대 위를 넓게 덮는 패널 (ListPanels의 size="wide") */
const WIDE_PANELS: string[] = ['audit', 'history', 'trust'];

/** 영역의 기본 크기와 끌어서 바꿀 수 있는 범위(px). 화면 크기에 따라 잰다. */
function sizeLimits(phoneRatio: number) {
  const vw = window.innerWidth, vh = window.innerHeight;
  const rem = parseFloat(getComputedStyle(document.documentElement).fontSize) || 16;
  // 폰 칸은 폰 화면이 칸 높이를 다 쓸 만큼보다 좁아지지 않는다. 좁아지면 폰 비율이 무너진다.
  const stage = document.querySelector('.np-phone__stage'), card = stage?.closest('.np-phone');
  // 최대 폭은 넘지 않는다. 폰을 뽑아 영상이 가로로 바뀌어도 무대를 밀어내지 않는다.
  const phoneMin = stage && card ? Math.min(vw * 0.4, Math.ceil(stage.clientHeight * phoneRatio + card.getBoundingClientRect().width - stage.clientWidth + 4)) : 240;
  return {
    phone: { base: vw * 0.24, min: phoneMin, max: vw * 0.4 },
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
  const [info, setInfo] = useState<PlacesInfo>();
  /** 처음 장소 조회가 실패한 이유 (어댑터가 정한 문구). 다시 시도하면 지운다 */
  const [placesError, setPlacesError] = useState<string>();
  /** 장소별 표시 배치 (placeId → 배치) */
  const [arrangements, setArrangements] = useState<Record<string, Arrangement>>({});
  /** 사람이 고정한 장소. 이 화면을 연 동안만 두고 저장하지 않는다. 자동이면 undefined */
  const [lock, setLock] = useState<string>();
  /** 배치 화면에서 마지막에 본 장소 탭. 메모리에만 둔다. */
  const [layoutPlace, setLayoutPlace] = useState<string>();
  const [snapshot, setSnapshot] = useState<MonitorState>();
  // 권한 설정을 열고 돌아오기 전에 브라우저를 다시 열었으면 폰 연결 탭에서 안내를 이어서 띄운다
  const pending = useMemo(loadMirrorPending, []);
  const [view, setView] = useState<ViewState>(pending ? 'phones' : initialView);
  const [panel, setPanel] = useState<PanelState | null>(null);
  // 설정을 다시 열면 마지막에 본 탭으로 간다
  const [lastSettings, setLastSettings] = useState<SettingsKey>(initialView === 'monitor' ? 'layout' : initialView);
  // 처음 그린 화면은 강조하지 않는다. 그 뒤로 바뀌는 곳만 도착 강조를 보인다.
  const [motion, setMotion] = useState(false);
  const [streams, setStreams] = useState<Record<string, MediaStream>>({});
  /** 마지막 미러링 창 공유가 실패한 사람과 이유. 다시 고르거나 성공하면 지운다. */
  const [mirrorFail, setMirrorFail] = useState<{ personId: string; reason: MirrorFailure } | null>(null);
  const [mirrorReady, setMirrorReady] = useState(prefs.mirrorReady);
  /** 화면 공유 권한 안내. ask: 설정을 열기 전 · back: 설정을 연 뒤라 창 고르기를 앞세운다. retry: 공유했는데 운영체제가 막았다. */
  const [guide, setGuide] = useState<MirrorGuideState | null>(pending ? { personId: pending, stage: 'back', retry: false } : null);
  const shownPerson = useRef<string>();
  const seen = useRef<{ init?: boolean; request?: string; faults: Set<string> }>({ faults: new Set() });

  useEffect(() => { savePrefs({ theme, follow, stage: zoomView, sizes, mirrorReady }); }, [theme, follow, zoomView, sizes, mirrorReady]);
  const loaded = !!snapshot;
  useEffect(() => {
    if (!loaded) return;
    const t = setTimeout(() => setMotion(true), 800);
    return () => clearTimeout(t);
  }, [loaded]);
  // 토큰이 거부되면(401·403) 어댑터가 입력 화면으로 돌려보내므로 이 실패 화면이 끼어들지 않는다
  const loadPlaces = useCallback(() => {
    setPlacesError(undefined);
    source.places().then((p) => {
      setInfo(p);
      setArrangements(Object.fromEntries(p.places.map((pl) => [pl.placeId, resolveArrangement(pl.devices, loadArrangement(pl.placeId, pl.placeName))])));
    }, (e: unknown) => setPlacesError(e instanceof Error && e.message ? e.message : '원인을 알 수 없어요'));
  }, [source]);
  useEffect(loadPlaces, [loadPlaces]);
  // 무대의 주 장소: 스냅숏이 바뀔 때마다 진행 중인 요청과 사람의 이동으로 고른다. 고정한 장소가 있으면 그 장소다.
  const focusRef = useRef<StageFocus>();
  const focus = useMemo(() => (info && snapshot ? nextFocus(focusRef.current, snapshot, info.places, lock) : undefined), [info, snapshot, lock]);
  focusRef.current = focus;
  // 배치 화면을 처음 열면 그때의 무대 장소로 탭을 정한다. 그 뒤로는 무대의 장소가 바뀌어도 탭은 그대로다.
  // 효과가 아니라 그리는 중에 정한다. 효과는 그린 뒤에 돌아서, 그 사이에 다음 묶음이 반영되면 탭이 바뀐 장소로 정해질 수 있다. 첫 화면부터 정한 장소를 쓴다.
  if (view === 'layout' && layoutPlace === undefined && focus?.primary) setLayoutPlace(focus.primary);
  /** 장소마다 표시 배치를 입힌 것 (placeId → 배치된 장소) */
  const layouts = useMemo(() => Object.fromEntries((info?.places ?? []).map((p) => [p.placeId, withArrangement(p, arrangements[p.placeId] ?? {}, info!.weakRssiDbm)])) as Record<string, FloorLayout>, [info, arrangements]);
  // 받은 이벤트 묶음을 차례로 쌓는다. 기기 목록이 있어야 처음 상태를 만들 수 있어서 장소를 받은 뒤 구독한다.
  useEffect(() => {
    if (!info) return;
    // 판정 결과는 판정 게이지가 찬 뒤에 넘긴다
    const pacer = paceJudging((events) => setSnapshot((s) => reduceAll(s ?? initialState(info.places), events)));
    const off = source.subscribe(pacer.push);
    return () => { off(); pacer.stop(); };
  }, [source, info]);

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
      // 열린 대화상자(미러링 권한 안내)가 있으면 Esc는 그 대화상자만 닫는다
      if (e.key !== 'Escape' || document.querySelector('dialog[open]')) return;
      if (viewRef.current !== 'monitor') setView('monitor');
      else setPanel(null);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);

  if (!info || !snapshot) {
    return (
      <div className="np-app" data-theme={theme}>
        {placesError && !info ? <PlacesError message={placesError} onRetry={loadPlaces} /> : <p className="np-loading" role="status">불러오는 중…</p>}
      </div>
    );
  }

  const people = new Map(snapshot.people.map((p) => [p.id, p.pseudonym]));
  // 이력과 감사 로그는 주 장소가 아닌 장소의 기기도 이름으로 보인다
  const deviceNames = new Map(info.places.flatMap((p) => p.devices).map((d) => [d.nodeId, d.name]));
  const ctx: Ctx = {
    source, places: info.places, snapshot,
    personName: (id) => people.get(id) ?? '알 수 없음',
    deviceName: (id) => deviceNames.get(id) ?? id
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
  const platform = platformOf(navigator.userAgent);
  const shareWindow = (personId: string) => {
    setMirrorFail(null);
    // 화면 공유를 쓸 수 없는 브라우저·주소면 getDisplayMedia가 없다. 그때도 같은 실패 안내로 보낸다.
    Promise.resolve().then(() => navigator.mediaDevices.getDisplayMedia({ video: true, audio: false })).then((stream) => {
      stream.getVideoTracks()[0]?.addEventListener('ended', () => setStreams((s) => { const n = { ...s }; delete n[personId]; return n; }));
      setStreams((s) => ({ ...s, [personId]: stream }));
      setMirrorReady(true);
      saveMirrorPending(null);
    }, (err: unknown) => {
      const reason = mirrorFailure(err);
      saveMirrorPending(null);
      if (reason === 'system') setGuide({ personId, stage: 'ask', retry: true });
      else if (reason !== 'cancel') setMirrorFail({ personId, reason });
    });
  };
  // 권한이 없을 수 있으면 공유 창보다 권한 안내를 먼저 띄운다. 공유 창이 열린 채로 운영체제 권한 창이 뜨면 공유 창을 닫을 때까지 권한 창이 화면을 덮는다.
  const pickWindow = (personId: string) => {
    setMirrorFail(null);
    if (guideFirst(platform, mirrorReady)) setGuide({ personId, stage: 'ask', retry: false });
    else shareWindow(personId);
  };
  const mirrorGuide = guide ? {
    ...guide, platform,
    onSettings: () => { saveMirrorPending(guide.personId); setGuide({ ...guide, stage: 'back' }); },
    onPick: () => { setGuide(null); shareWindow(guide.personId); },
    onClose: () => { setGuide(null); saveMirrorPending(null); }
  } : null;

  // 고정한 장소가 목록에 없으면 자동으로 본다
  const locked = lock && layouts[lock] ? lock : undefined;
  const layout = focus?.primary ? layouts[focus.primary] : undefined;
  // 배치 화면은 장소 탭으로 고른 장소의 배치를 편집한다. 배치 화면을 처음 연 순간에는 지금 무대의 장소다.
  const tab = info.places.find((p) => p.placeId === layoutPlace) ?? info.places.find((p) => p.placeId === focus?.primary) ?? info.places[0];
  const arrangement = (tab && arrangements[tab.placeId]) || {};
  const arrange = {
    arrangement,
    canPlace: (nodeId: string, s: { x: number; y: number }) => canPlace(arrangement, nodeId, s),
    onChange: (next: Arrangement) => { const r = resolveArrangement(tab.devices, next); setArrangements((a) => ({ ...a, [tab.placeId]: r })); saveArrangement(tab.placeId, r); },
    onReset: () => { clearArrangement(tab.placeId, tab.placeName); setArrangements((a) => ({ ...a, [tab.placeId]: resolveArrangement(tab.devices) })); }
  };

  // 테마는 화면 전체를 천천히 겹쳐 바꾼다(styles/base.css의 view-transition). 움직임 줄이기 설정이면 바로 바꾼다.
  const toggleTheme = () => {
    const next = theme === 'dark' ? 'light' : 'dark';
    const doc = document as Document & { startViewTransition?: (cb: () => void) => unknown };
    if (doc.startViewTransition && !matchMedia('(prefers-reduced-motion: reduce)').matches) doc.startViewTransition(() => flushSync(() => setTheme(next)));
    else setTheme(next);
  };
  const topBar = (
    <TopBar theme={theme} live={snapshot.connected} receivedAt={snapshot.lastReceivedAt} mock={source.mock}
      places={info.places} current={focus?.primary} locked={locked} onLock={setLock} onToggleTheme={toggleTheme} />
  );
  const side = <SideMenu current={current} onPick={pick} />;

  let screen;
  if (view === 'monitor') {
    const phone = choosePhone(snapshot, follow, shownPerson.current);
    shownPerson.current = phone.personId;
    const mock = snapshot.mockPhones?.find((m) => m.personId === phone.personId);
    const wide = !!panel && WIDE_PANELS.includes(panel.kind);
    const stream = phone.personId ? streams[phone.personId] : undefined;
    const video = stream?.getVideoTracks()[0]?.getSettings();
    // 폰 화면의 가로/세로 비율. 세로 영상은 영상 크기, 그 밖에는 폰 틀(9:19.5)
    const lim = sizeLimits(video?.width && video.height && video.width < video.height ? video.width / video.height : 9 / 19.5);
    // 카드 사이 간격(px). 손잡이는 간격 가운데를 잡는다.
    const gutter = Math.max(4, (parseFloat(getComputedStyle(document.documentElement).fontSize) || 16) * 0.25);
    const px = (k: keyof Sizes) => Math.max(sizes[k] ?? lim[k].base, lim[k].min);
    const setSize = (k: keyof Sizes) => (v: number) => setSizes((s) => ({ ...s, [k]: v }));
    const resetSize = (k: keyof Sizes) => () => setSizes((s) => ({ ...s, [k]: undefined }));
    // 패널 왼쪽 끝을 무대 좌표로 바꿔, 방 밀기와 콜아웃이 실제 패널 크기를 따른다
    const panelEdge = panel && stageSize ? panelEdgeOf(zoomView, stageSize, px(wide ? 'wide' : 'panel') - gutter - stageSize.right) : undefined;
    // 무대: 큰 무대를 먼저 계산하고, 작은 방을 그 글자와 방을 피해 놓은 뒤, 콜아웃이 작은 방을 피하게 큰 무대를 다시 계산한다
    const stageOpt = { panelOpen: !!panel, panelWide: wide, panelEdge, viewFixed: !isFit(zoomView), places: info.places };
    const firstPass = layout && stageView(snapshot, layout, stageOpt);
    const insetLayout = focus?.inset ? layouts[focus.inset] : undefined;
    const inset = firstPass && insetLayout ? insetView(snapshot, insetLayout, firstPass, stageOpt) : undefined;
    const stage = layout && inset ? stageView(snapshot, layout, { ...stageOpt, inset: inset.box }) : firstPass;
    // 끌어 바꾼 크기는 CSS 변수로 넘긴다 (요소에 style 속성을 쓰지 않는다)
    const vars = (['phone', 'panel', 'wide', 'rail'] as const).filter((k) => sizes[k] !== undefined || (k === 'phone' && px(k) > lim[k].base))
      .map((k) => `--${{ phone: 'phone-w', panel: 'panel-w', wide: 'panel-wide-w', rail: 'rail-h' }[k]}: ${Math.round(px(k))}px;`).join(' ');
    screen = (
      <div className="np-monitor" data-component="MonitorLayout" data-live={snapshot.connected ? 'true' : 'false'} data-panel={panel ? (wide ? 'wide' : 'normal') : 'none'}>
        {vars ? <style data-component="SizeVars">{`.np-app .np-monitor { ${vars} }`}</style> : null}
        {topBar}
        <PhoneMirror who={phone.who} follow={phone.follow} stream={stream} mock={mock} />
        {stage ? <Stage v={stage} inset={inset} locked={!!locked} onInset={setLock} zoom={{ view: zoomView, onChange: setZoomView, onSize: setStageSize }} /> : <StageEmpty />}
        <Splitter edge="phone" label="폰 화면 폭" value={px('phone')} min={lim.phone.min} max={lim.phone.max} toValue={(e) => e.clientX - gutter * 1.5} onChange={setSize('phone')} onReset={resetSize('phone')} />
        <Splitter edge="rail" label="흐름 레일 높이" value={px('rail')} min={lim.rail.min} max={lim.rail.max} toValue={(e) => window.innerHeight - gutter * 1.5 - e.clientY} onChange={setSize('rail')} onReset={resetSize('rail')} />
        <FlowRail v={railView(snapshot, info.places, phone.personId)} onOpen={snapshot.request ? () => setPanel({ kind: 'request', request: snapshot.request!, live: true }) : undefined} />
        {!snapshot.connected ? (
          <OfflineBanner title="서버와 연결이 끊겼어요" detail={`마지막 수신 ${snapshot.lastReceivedAt} · 다시 연결 시도 ${snapshot.reconnectAttempts}회째`} onReconnect={() => source.reconnect()} />
        ) : null}
        {side}
        {/* 패널에 가려진 무대 카드의 보이는 오른쪽 끝을 둥글게 마감한다 */}
        {panel ? <div className="np-stage-cap" data-component="StageCap" aria-hidden="true" /> : null}
        {panel ? (
          <Splitter edge="panel" label="패널 폭" value={px(wide ? 'wide' : 'panel')} min={lim[wide ? 'wide' : 'panel'].min} max={lim[wide ? 'wide' : 'panel'].max}
            toValue={(e) => (document.querySelector('.np-side')?.getBoundingClientRect().left ?? window.innerWidth) - gutter / 2 - e.clientX}
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
        {view === 'layout' ? <LayoutCheck {...arrange} places={info.places} layout={tab && layouts[tab.placeId]} onPlace={setLayoutPlace} /> : view === 'phones' ? <Phones streams={streams} onPick={pickWindow} follow={follow} onFollow={setFollow} fail={mirrorFail} guide={mirrorGuide} /> : <Check />}
      </div>
    );
  }

  return (
    <DataCtx.Provider value={ctx}>
      <div className="np-app" data-theme={theme} data-motion={motion ? 'on' : 'off'}>{screen}</div>
    </DataCtx.Provider>
  );
}

/** 처음 장소 조회가 실패했을 때. 이유는 어댑터가 정한 문구라 토큰이나 브라우저 오류 원문이 없다. */
function PlacesError({ message, onRetry }: { message: string; onRetry: () => void }) {
  return (
    <main className="np-gate" data-component="PlacesError">
      <div className="np-gate__card">
        <span className="np-cap">NEARPILOT · 운영 모니터</span>
        <h1 className="np-gate__title">장소를 불러오지 못했어요</h1>
        <p className="np-gate__error" role="alert">{message}</p>
        <button className="np-btn" data-variant="primary" type="button" onClick={onRetry}>다시 시도</button>
      </div>
    </main>
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

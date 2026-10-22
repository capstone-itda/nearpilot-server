// SET-01~03 설정. 이 PC의 표시만 바꾸고 서버 데이터는 바꾸지 않는다 (D7).
import { useRef, useState, type ReactNode } from 'react';
import { DataTable, KeyValue, Switch, Td } from '../components/common';
import { Stage } from '../components/Stage';
import { Loading } from '../components/Panel';
import { MirrorVideo } from '../components/PhoneMirror';
import type { SettingsKey } from '../components/TopBar';
import type { Spot } from '../model/types';
import { applyFile, parseFile, toFile, type Arrangement } from '../view/arrange';
import { useData, useQuery } from '../data/context';
import type { FollowSettings } from '../view/phone';
import { stageView } from '../view/stage';

const NAV: [SettingsKey, string, string][] = [
  ['layout', '배치', '무대 배치와 실시간 신호'],
  ['phones', '폰 연결', '미러링 창과 비콘'],
  ['check', '점검', '시연 직전 한 번에 확인']
];

/** 설정 화면 위쪽 탭 (2026-10-23 사용자 결정: 사이드 메뉴의 설정을 누르면 탭으로 고른다) */
export function SettingsTabs({ current, onPick }: { current: SettingsKey; onPick: (k: SettingsKey) => void }) {
  return (
    <nav className="np-stabs" data-component="SettingsTabs" aria-label="설정 항목">
      <div className="np-stabs__list" role="tablist">
        {NAV.map(([k, n, d]) => (
          <button key={k} className="np-stabs__item" type="button" role="tab" aria-selected={k === current ? 'true' : 'false'} onClick={() => onPick(k)}>
            <span className="np-stabs__name">{n}</span><span className="np-stabs__desc">{d}</span>
          </button>
        ))}
      </div>
      <p className="np-stabs__note">이 PC의 표시만 바꿔요. 서버에는 쓰지 않아요.</p>
      <p className="np-stabs__credit"><a href="https://iso-glow.vercel.app" target="_blank" rel="noreferrer">Made with ISO-GLOW ↗</a></p>
    </nav>
  );
}

export function SettingsContent({ title, sub, children }: { title: string; sub: string; children: ReactNode }) {
  return (
    <main className="np-scontent" data-component="SettingsContent">
      <div className="np-scontent__head"><h1 className="np-scontent__title">{title}</h1><p className="np-scontent__sub">{sub}</p></div>
      {children}
    </main>
  );
}

const fmtDbm = (v: number) => String(v).replace('-', '−');

export interface LayoutCheckProps {
  arrangement: Arrangement;
  canPlace: (nodeId: string, s: Spot) => boolean;
  onChange: (next: Arrangement) => void;
  onReset: () => void;
}

export function LayoutCheck({ arrangement, canPlace, onChange, onReset }: LayoutCheckProps) {
  const { snapshot, layout } = useData();
  const file = useRef<HTMLInputElement>(null);
  const [status, setStatus] = useState('');
  const weakDbm = layout.weakRssiDbm;
  const visitors = snapshot.people.filter((p) => p.role === 'visitor' && p.beaconSeen);
  // 사람별로 묶어서 한 문장으로 쓴다
  let weakCount = 0;
  const weak: string[] = [];
  for (const p of visitors) {
    const names = layout.devices.filter((d) => p.rssi[d.nodeId] !== undefined && p.rssi[d.nodeId] < weakDbm).map((d) => d.name);
    weakCount += names.length;
    if (names.length) weak.push(`${p.pseudonym}의 신호가 ${names.join(', ')}에서 약해요.`);
  }
  const exportFile = () => {
    const blob = new Blob([JSON.stringify(toFile(layout.placeName, arrangement), null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `nearpilot-배치-${layout.placeName}.json`;
    a.click();
    URL.revokeObjectURL(url);
    setStatus('배치를 파일로 내보냈어요.');
  };
  const importFile = (f: File | undefined) => {
    if (!f) return;
    f.text().then((text) => {
      const r = parseFile(text);
      if (!r.ok) { setStatus(`불러오지 못했어요. ${r.error}.`); return; }
      const a = applyFile(arrangement, layout.devices, r.devices);
      if (!a.ok) {
        const why = [a.outside.length ? `${a.outside.join(', ')}의 자리가 방 밖이에요.` : '', a.overlap.length ? `${a.overlap.join(', ')}의 자리가 다른 기기와 겹쳐요.` : ''].filter(Boolean).join(' ');
        setStatus(`불러오지 못했어요. ${why} 배치는 그대로예요.`);
        return;
      }
      if (a.moved === 0) { setStatus('불러오지 못했어요. 파일에 지금 장소의 기기가 없어요. 배치는 그대로예요.'); return; }
      onChange(a.next);
      const other = r.place && r.place !== layout.placeName ? ` 다른 장소(${r.place})의 파일이라 이름이 같은 기기만 옮겼어요.` : '';
      const skipped = a.unknown ? ` 지금 장소에 없는 기기 ${a.unknown}대는 건너뛰었어요.` : '';
      setStatus(`기기 ${a.moved}대의 자리를 불러왔어요.${other}${skipped}`);
    }, () => setStatus('파일을 읽지 못했어요.'));
  };
  return (
    <SettingsContent title="배치" sub="기기를 끌어다 놓아 무대의 배치를 정해요. 이 PC에만 저장하고 서버는 바꾸지 않아요.">
      <div className="np-layoutcheck">
        <div className="np-layoutcheck__plan">
          <Stage v={stageView(snapshot, layout, { panelOpen: false, layoutCheck: true })} edit={{ canPlace, onMove: (id, s) => onChange({ ...arrangement, [id]: s }) }} />
        </div>
        <div className="np-layoutcheck__side">
          <section className="np-card" data-component="Card">
            <div className="np-card__head"><h2 className="np-card__title">표시용 배치</h2></div>
            <p className="np-sec__hint">끌거나 화살표 키로 옮겨요. 실제 거리와는 상관없어요.</p>
            <div className="np-actions">
              <button className="np-btn" type="button" onClick={exportFile}>파일로 내보내기</button>
              <button className="np-btn" type="button" onClick={() => file.current?.click()}>파일 불러오기</button>
              <button className="np-btn" data-variant="quiet" type="button" onClick={() => { onReset(); setStatus('기본 배치로 되돌렸어요.'); }}>기본 배치로</button>
            </div>
            <input ref={file} hidden type="file" accept="application/json,.json" aria-label="배치 파일" tabIndex={-1}
              onChange={(e) => { importFile(e.target.files?.[0]); e.target.value = ''; }} />
            {status ? <p className="np-sec__hint" role="status">{status}</p> : null}
          </section>
          <section className="np-card" data-component="Card">
            <div className="np-card__head"><h2 className="np-card__title">실시간 신호</h2><span className="np-cap" data-case="keep">dBm</span></div>
            <DataTable label="기기별 신호 세기" head={['기기', ...visitors.map((p) => p.pseudonym)]} rows={layout.devices.map((d) => ({
              key: d.nodeId,
              cells: [<Td key="n" main={d.name} />, ...visitors.map((p) => {
                const v = p.rssi[d.nodeId];
                return <td key={p.id} className="np-signal__value" data-tone={v !== undefined && v < weakDbm ? 'weak' : 'normal'}>{v === undefined ? '—' : fmtDbm(v)}</td>;
              })]
            }))} />
            <p className="np-sec__hint">점선 밑줄: {fmtDbm(weakDbm)} dBm보다 약함 (설정값 · 판정에는 쓰지 않음)</p>
          </section>
          {weak.length ? (
            <div className="np-warn" data-component="Warning" role="note">
              <span className="np-warn__title">확인할 것 {weakCount}건</span>
              <span>{weak.join(' ')} 기기 위치나 폰 방향을 확인해 주세요.</span>
            </div>
          ) : null}
        </div>
      </div>
    </SettingsContent>
  );
}

export interface PhonesProps {
  streams: Record<string, MediaStream>;
  onPick: (personId: string) => void;
  follow: FollowSettings;
  onFollow: (f: FollowSettings) => void;
}

const ago = (sec: number | null) => (sec === null ? '—' : sec < 60 ? `${sec}초 전` : `${Math.round(sec / 60)}분 전`);

export function Phones({ streams, onPick, follow, onFollow }: PhonesProps) {
  const { source, snapshot } = useData();
  const q = useQuery(() => source.health(), []);
  const people = snapshot.people.filter((p) => p.role === 'owner' || p.beaconSeen || streams[p.id]);
  return (
    <SettingsContent title="폰 연결" sub="사람마다 폰 미러링 창을 골라요. 브라우저 화면 공유를 쓰고 서버는 거치지 않아요.">
      <div className="np-phones">
        {people.map((p) => {
          const linked = !!streams[p.id];
          const mock = !linked && snapshot.mockPhones?.some((m) => m.personId === p.id);
          const beacon = q.data?.beacons.find((x) => x.personId === p.id);
          return (
            <section key={p.id} className="np-card" data-component="PhoneCard">
              <div className="np-card__head"><h2 className="np-card__title">{p.pseudonym}</h2><span className="np-pcard__status" data-linked={linked ? 'true' : 'false'}>{linked ? '미러링 연결됨' : mock ? '목 화면' : '미러링 창 없음'}</span></div>
              <div className="np-pcard__preview" data-linked={linked ? 'true' : 'false'}>
                <div className="np-pcard__preview-screen">{linked ? <MirrorVideo stream={streams[p.id]} /> : mock ? '목 화면 사용 중' : '창을 고르면 여기에 보여요'}</div>
              </div>
              <KeyValue rows={[['비콘', beacon?.label ?? '—', true], ['수신', beacon ? (beacon.lastSeenSec === null ? beacon.note ?? '—' : ago(beacon.lastSeenSec)) : '—']]} />
              <button className="np-btn" type="button" onClick={() => onPick(p.id)}>{linked ? '창 다시 고르기' : '미러링 창 고르기'}</button>
            </section>
          );
        })}
      </div>
      <section className="np-card" data-component="Card">
        <div className="np-card__head"><h2 className="np-card__title">자동 전환</h2></div>
        <div className="np-switches">
          <Switch label="요청한 사람의 폰을 자동으로 보여 줘요" on={follow.followRequester} onChange={(v) => onFollow({ ...follow, followRequester: v })} />
          <Switch label="승인 알림이 오면 소유자 폰으로 바꿔요" on={follow.followApprover} onChange={(v) => onFollow({ ...follow, followApprover: v })} />
        </div>
      </section>
    </SettingsContent>
  );
}

function Crow({ state, name, right }: { state: 'ok' | 'warn' | 'bad'; name: string; right: string }) {
  return <li className="np-crow" data-state={state}><span className="np-crow__dot" aria-hidden="true" /><span>{name}</span><span className="np-crow__right">{right}</span></li>;
}
function CheckCard({ title, children }: { title: string; children: ReactNode }) {
  return <section className="np-card" data-component="CheckCard"><div className="np-card__head"><h2 className="np-card__title">{title}</h2></div><ul>{children}</ul></section>;
}

export function Check() {
  const { source, snapshot, deviceName, personName } = useData();
  const q = useQuery(() => source.health(), []);
  const h = q.data;
  const faulted = new Set(snapshot.devices.filter((d) => d.state === 'FAULT').map((d) => d.nodeId));
  if (!h) return <SettingsContent title="점검" sub="시연 직전에 한 번에 확인해요."><Loading error={q.error} /></SettingsContent>;
  const devices = h.devices.map((d) => ({ ...d, fault: faulted.has(d.nodeId), state: (faulted.has(d.nodeId) ? 'bad' : !d.connected || d.trust !== 'trusted' ? 'warn' : 'ok') as 'ok' | 'warn' | 'bad' }));
  // 방문자 비콘만 본다. 소유자는 비콘 없이 승인만 한다.
  const visitorIds = new Set(snapshot.people.filter((p) => p.role === 'visitor').map((p) => p.id));
  const beacons = h.beacons.filter((b) => visitorIds.has(b.personId)).map((b) => ({ ...b, state: (b.note || b.lastSeenSec === null ? 'warn' : 'ok') as 'ok' | 'warn' }));
  const server: [string, boolean, string][] = [['데이터베이스', h.server.database, h.server.database ? '정상' : '응답 없음'], ['MQTT 브로커', h.server.mqtt, h.server.mqtt ? '연결됨' : '연결 끊김'], ['근접 모델 파일', true, h.server.model]];
  const issues = [...devices, ...beacons].filter((x) => x.state !== 'ok').length + server.filter(([, ok]) => !ok).length;
  return (
    <SettingsContent title="점검" sub="시연 직전에 한 번에 확인해요.">
      {snapshot.connected ? (
        <div className="np-ready" data-component="ReadyBanner" data-state={issues ? 'warn' : 'ok'} role="status">
          <span className="np-ready__title">{issues ? `확인할 것 ${issues}건` : '모두 정상이에요'}</span>
          <span className="np-ready__body">{issues ? '나머지는 모두 정상이에요.' : '시연을 시작해도 돼요.'}</span>
        </div>
      ) : (
        <div className="np-ready" data-component="ReadyBanner" data-state="warn" role="status">
          <span className="np-ready__title">서버와 연결이 끊겼어요</span>
          <span className="np-ready__body">아래는 {snapshot.lastReceivedAt}에 마지막으로 받은 결과예요. 지금 상태와 다를 수 있어요.</span>
        </div>
      )}
      <div className="np-checks">
        <CheckCard title="기기">{devices.map((d) => <Crow key={d.nodeId} state={d.state} name={deviceName(d.nodeId)} right={`${d.fault ? '고장 격리 · ' : ''}${d.connected ? '연결됨' : '연결 끊김'} · ${d.trust}`} />)}</CheckCard>
        <CheckCard title="폰 비콘">{beacons.map((b) => <Crow key={b.personId} state={b.state} name={personName(b.personId)} right={b.lastSeenSec === null ? b.note ?? '신호 없음' : `${ago(b.lastSeenSec)}${b.note ? ` · ${b.note}` : ''}`} />)}</CheckCard>
        <CheckCard title="서버">{server.map(([n, ok, r]) => <Crow key={n} state={ok ? 'ok' : 'warn'} name={n} right={r} />)}</CheckCard>
      </div>
    </SettingsContent>
  );
}

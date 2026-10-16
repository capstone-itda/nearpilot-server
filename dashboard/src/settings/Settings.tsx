// SET-01~03 설정. 이 PC의 표시만 바꾸고 서버 데이터는 바꾸지 않는다 (D7).
import type { ReactNode } from 'react';
import { DataTable, Icon, KeyValue, Switch, Td } from '../components/common';
import { Stage } from '../components/Stage';
import { Loading } from '../components/Panel';
import { MirrorVideo } from '../components/PhoneMirror';
import type { SettingsKey } from '../components/TopBar';
import { useData, useQuery } from '../data/context';
import type { FollowSettings } from '../view/phone';
import { stageView } from '../view/stage';
import { HOST_NAME } from '../view/text';

const NAV: [SettingsKey, string, string][] = [
  ['layout', '배치·신호 점검', '모델 배치와 실시간 신호'],
  ['phones', '폰 연결', '미러링 창과 비콘'],
  ['check', '점검', '시연 직전 한 번에 확인']
];

export function SettingsNav({ current, onPick, onBack }: { current: SettingsKey; onPick: (k: SettingsKey) => void; onBack: () => void }) {
  return (
    <nav className="np-snav" data-component="SettingsNav" aria-label="설정 항목">
      <button className="np-btn np-snav__back" data-variant="quiet" type="button" onClick={onBack}><Icon name="back" /><span>모니터로 돌아가기</span></button>
      {NAV.map(([k, n, d]) => (
        <button key={k} className="np-snav__item" type="button" aria-current={k === current ? 'page' : 'false'} onClick={() => onPick(k)}>
          <span className="np-snav__name">{n}</span><span className="np-snav__desc">{d}</span>
        </button>
      ))}
      <p className="np-snav__note">설정 화면은 이 PC의 표시만 바꿔요. 서버 데이터는 바꾸지 않아요.</p>
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

export function LayoutCheck() {
  const { snapshot, layout } = useData();
  const weakDbm = layout.weakRssiDbm;
  const visitors = snapshot.people.filter((p) => p.role === 'visitor' && p.beaconSeen);
  const weak: string[] = [];
  for (const d of layout.devices) for (const p of visitors) {
    const v = p.rssi[d.nodeId];
    if (v !== undefined && v < weakDbm) weak.push(`${d.name}에서 ${p.pseudonym}의 신호가 약해요.`);
  }
  return (
    <SettingsContent title="배치·신호 점검" sub="근접 모델 파일의 배치를 그대로 그려요. 여기서는 고치지 않아요.">
      <div className="np-layoutcheck">
        <div className="np-layoutcheck__plan"><Stage v={stageView(snapshot, layout, { panelOpen: false, layoutCheck: true })} /></div>
        <div className="np-layoutcheck__side">
          <section className="np-card" data-component="Card">
            <div className="np-card__head"><h2 className="np-card__title">모델 파일</h2></div>
            <KeyValue rows={[['모델 버전', layout.modelVersion, true], ['보정 버전', layout.calibrationVersion, true], ['기기', `이 장소 ${layout.devices.length}대 (NFR-10)`], ['점선 원 옆 숫자', '기기 앞 위치 x, y (m)']]} />
          </section>
          <section className="np-card" data-component="Card">
            <div className="np-card__head"><h2 className="np-card__title">실시간 신호</h2><span className="np-cap">dBm</span></div>
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
              <span className="np-warn__title">확인할 것 {weak.length}건</span>
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
                <div className="np-pcard__preview-screen">{linked ? <MirrorVideo stream={streams[p.id]} /> : mock ? '모니터에 목 화면을 보여 줘요. 창을 고르면 바뀌어요' : '창을 고르면 여기에 보여요'}</div>
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
  const devices = h.devices.map((d) => ({ ...d, fault: faulted.has(d.nodeId), state: (!d.connected || faulted.has(d.nodeId) ? 'bad' : d.trust !== 'trusted' ? 'warn' : 'ok') as 'ok' | 'warn' | 'bad' }));
  const beacons = h.beacons.filter((b) => b.lastSeenSec !== null).map((b) => ({ ...b, state: (b.note ? 'warn' : 'ok') as 'ok' | 'warn' }));
  const hosts = h.hosts.map((x) => ({ ...x, state: (x.lastToolsList ? 'ok' : 'warn') as 'ok' | 'warn' }));
  const server: [string, boolean, string][] = [['데이터베이스', h.server.database, h.server.database ? '정상' : '응답 없음'], ['MQTT 브로커', h.server.mqtt, h.server.mqtt ? '연결됨' : '연결 끊김'], ['근접 모델 파일', true, h.server.model]];
  const issues = [...devices, ...beacons, ...hosts].filter((x) => x.state !== 'ok').length + server.filter(([, ok]) => !ok).length;
  return (
    <SettingsContent title="점검" sub="시연 직전에 한 번에 확인해요.">
      <div className="np-ready" data-component="ReadyBanner" data-state={issues ? 'warn' : 'ok'} role="status">
        <span className="np-ready__title">{issues ? `확인할 것 ${issues}건` : '모두 정상이에요'}</span>
        <span className="np-ready__body">{issues ? '나머지는 모두 정상이에요.' : '시연을 시작해도 돼요.'}</span>
      </div>
      <div className="np-checks">
        <CheckCard title="기기">{devices.map((d) => <Crow key={d.nodeId} state={d.state} name={deviceName(d.nodeId)} right={`${d.fault ? '고장 격리 · ' : ''}${d.connected ? '연결됨' : '연결 끊김'} · ${d.trust}`} />)}</CheckCard>
        <CheckCard title="폰 비콘">{beacons.map((b) => <Crow key={b.personId} state={b.state} name={personName(b.personId)} right={`${ago(b.lastSeenSec)}${b.note ? ` · ${b.note}` : ''}`} />)}</CheckCard>
        <CheckCard title="AI 호스트">{hosts.map((x) => <Crow key={x.host} state={x.state} name={HOST_NAME[x.host]} right={x.lastToolsList ? `tools/list ${x.lastToolsList}` : '연결 기록 없음'} />)}</CheckCard>
        <CheckCard title="서버">{server.map(([n, ok, r]) => <Crow key={n} state={ok ? 'ok' : 'bad'} name={n} right={r} />)}</CheckCard>
      </div>
    </SettingsContent>
  );
}

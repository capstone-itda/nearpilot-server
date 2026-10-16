import { createContext, useContext, useEffect, useState } from 'react';
import type { DataSource } from './source';
import type { FloorLayout, MonitorState } from '../model/types';

export interface Ctx {
  source: DataSource;
  layout: FloorLayout;
  snapshot: MonitorState;
  personName: (id: string) => string;
  deviceName: (id: string) => string;
}
export const DataCtx = createContext<Ctx | null>(null);
export function useData(): Ctx {
  const c = useContext(DataCtx);
  if (!c) throw new Error('DataCtx 없음');
  return c;
}

/**
 * 판정 흐름이 바뀌었는지 가리는 열쇠. 요청, 판정, 실행 결과, 승인 알림, 기기 상태, 비콘 수신, 연결, 분 단위 수신 시각이 바뀌면 달라진다.
 * 신호 세기처럼 자주 바뀌는 값은 넣지 않는다.
 */
export function snapshotKey(s: MonitorState): string {
  const r = s.request;
  return [s.connected, s.lastReceivedAt.slice(0, 5), r?.id, r?.verdict, r?.execution, r?.respondedAt, r?.accessExpiredAt, s.ownerNotified, ...s.devices.map((d) => `${d.nodeId}:${d.state}`), ...s.people.map((p) => `${p.id}:${p.beaconSeen}`)].join('|');
}

/**
 * 조회 REST 한 번. deps가 바뀌면 비우고 다시 부른다.
 * 모니터 상태의 열쇠가 바뀌면 지금 결과를 둔 채로 다시 불러 바꾼다.
 */
export function useQuery<T>(fn: () => Promise<T>, deps: unknown[]): { data?: T; error?: string } {
  const { snapshot } = useData();
  const key = snapshotKey(snapshot);
  const [state, setState] = useState<{ data?: T; error?: string; deps?: unknown[] }>({});
  const same = !!state.deps && state.deps.length === deps.length && state.deps.every((d, i) => Object.is(d, deps[i]));
  useEffect(() => {
    let alive = true;
    fn().then((data) => alive && setState({ data, deps }), (e: unknown) => alive && setState({ error: e instanceof Error ? e.message : String(e), deps }));
    return () => { alive = false; };
  }, [...deps, key]);
  return same ? state : {};
}

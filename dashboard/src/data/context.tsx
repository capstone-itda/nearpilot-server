import { createContext, useContext, useEffect, useState } from 'react';
import type { DataSource } from './source';
import type { FloorLayout, Snapshot } from '../model/types';

export interface Ctx {
  source: DataSource;
  layout: FloorLayout;
  snapshot: Snapshot;
  personName: (id: string) => string;
  deviceName: (id: string) => string;
}
export const DataCtx = createContext<Ctx | null>(null);
export function useData(): Ctx {
  const c = useContext(DataCtx);
  if (!c) throw new Error('DataCtx 없음');
  return c;
}

/** 조회 REST 한 번. deps가 바뀌면 다시 부른다. */
export function useQuery<T>(fn: () => Promise<T>, deps: unknown[]): { data?: T; error?: string } {
  const [state, setState] = useState<{ data?: T; error?: string }>({});
  useEffect(() => {
    let alive = true;
    setState({});
    fn().then((data) => alive && setState({ data }), (e: unknown) => alive && setState({ error: e instanceof Error ? e.message : String(e) }));
    return () => { alive = false; };
  }, deps);
  return state;
}

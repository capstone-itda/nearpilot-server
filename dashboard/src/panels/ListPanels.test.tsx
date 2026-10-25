// MON-06: 점유 패널은 서버 어댑터가 준 대여형 점유 세션과 공용 조명 점유자를 함께 그린다
// MON-09: 이력의 기기 거르기는 모든 장소의 기기를 고를 수 있다
// MON-10: 신뢰도 패널은 평가 리포트나 혼동 행렬이 없어도 그린다
import { cleanup, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { DataCtx } from '../data/context';
import { PLACES, TRUST, TRUST_BASE } from '../data/mock/fixtures';
import { createMockSource } from '../data/mock/mockSource';
import { createServerSource } from '../data/server/serverSource';
import { SCENES } from '../data/mock/story';
import type { MonitorState, TrustReport } from '../model/types';
import { HistoryPanel, TrustPanel, UsesPanel } from './ListPanels';

afterEach(cleanup);

describe('UsesPanel 공용 조명', () => {
  const NOW = '2026-10-24T05:28:10.123+00:00';
  const NAMES = new Map(PLACES.flatMap((p) => p.devices).map((d) => [d.nodeId, d.name]));
  const WHO: Record<string, string> = { 'p-a': '방문자 A', 'p-b': '방문자 B' };
  /** 서버 uses 조회: 대여형 점유 세션 한 줄과 공용 조명을 지금 쓰는 사람 한 줄 (core/dashboard/queries.py의 uses) */
  const USES = [
    { nodeId: 'locker-1', personId: 'p-a', occupancy: 'rental', session: 'ACTIVE', startedAt: NOW, overdueMinutes: 3.5 },
    { nodeId: 'light-1', personId: 'p-b', occupancy: 'shared', session: 'ACTIVE', startedAt: NOW }
  ];
  const renderUses = () => {
    const source = createServerSource({
      origin: 'http://dash.test', token: 'np_owner_tok', onRejected: () => undefined,
      fetch: (async () => new Response(JSON.stringify(USES), { status: 200, headers: { 'content-type': 'application/json' } })) as unknown as typeof fetch
    });
    render(
      <DataCtx.Provider value={{ source, places: PLACES, snapshot: SCENES.later(), personName: (id) => WHO[id] ?? id, deviceName: (id) => NAMES.get(id) ?? id }}>
        <UsesPanel onClose={() => {}} onOpenDevice={() => {}} />
      </DataCtx.Provider>
    );
  };

  it('FR12A 안내는 대여형 점유 세션과 공용 조명을 지금 쓰는 사람을 보여준다고 말한다', () => {
    renderUses();
    expect(screen.getByText('대여형 기기의 점유 세션과 공용 조명을 지금 쓰는 사람을 보여요')).toBeTruthy();
    expect(screen.queryByText(/공용형은 세션이 없어/)).toBeNull();
  });

  it('FR12A 서버 어댑터로 받은 공용 행(occupancy: shared)도 표에 그린다', async () => {
    renderUses();
    const table = await screen.findByRole('table', { name: '점유·대여 목록' });
    const rows = within(table).getAllByRole('row').slice(1);
    expect(rows).toHaveLength(2);
    const shared = rows.find((r) => r.textContent?.includes('조명 1'))!;
    expect(shared.textContent).toContain('방문자 B');
    expect(shared.textContent).toContain('shared');
    expect(shared.textContent).toContain('ACTIVE');
    // 공용 조명은 대여 초과가 없다
    expect(shared.textContent).not.toContain('초과');
    const rental = rows.find((r) => r.textContent?.includes('사물함 1'))!;
    expect(rental.textContent).toContain('방문자 A');
    expect(rental.textContent).toContain('rental');
    expect(rental.textContent).toContain('3분 초과');
  });
});

describe('UsesPanel 한 기기에 여러 행', () => {
  const NOW = '2026-10-24T05:28:10.123+00:00';
  const NAMES = new Map(PLACES.flatMap((p) => p.devices).map((d) => [d.nodeId, d.name]));
  const WHO: Record<string, string> = { 'p-a': '방문자 A', 'p-b': '방문자 B', 'p-c': '방문자 C' };
  const row = (nodeId: string, personId: string, occupancy: 'rental' | 'shared') => ({ nodeId, personId, occupancy, session: 'ACTIVE', startedAt: NOW });
  /** 사물함 하나를 C가 빌렸고, 공용 조명 하나를 A와 B가 함께 쓴다. 서버는 공용 조명의 유지 대상자마다 한 행을 준다 */
  const FIRST = [row('locker-1', 'p-c', 'rental'), row('light-1', 'p-a', 'shared'), row('light-1', 'p-b', 'shared')];

  /** 서버가 주는 목록은 시험이 바꾼다. 모니터 상태가 바뀌면 패널이 다시 조회한다 */
  function setup(first: unknown[]) {
    let body = first;
    const source = createServerSource({
      origin: 'http://dash.test', token: 'np_owner_tok', onRejected: () => undefined,
      fetch: (async () => new Response(JSON.stringify(body), { status: 200, headers: { 'content-type': 'application/json' } })) as unknown as typeof fetch
    });
    const ui = (snapshot: MonitorState) => (
      <DataCtx.Provider value={{ source, places: PLACES, snapshot, personName: (id) => WHO[id] ?? id, deviceName: (id) => NAMES.get(id) ?? id }}>
        <UsesPanel onClose={() => {}} onOpenDevice={() => {}} />
      </DataCtx.Provider>
    );
    const view = render(ui(SCENES.later()));
    /** 표의 행마다 [기기 이름, 사람 이름] */
    const rows = () => within(screen.getByRole('table', { name: '점유·대여 목록' })).getAllByRole('row').slice(1)
      .map((r) => [/사물함 1|조명 1/.exec(r.textContent ?? '')?.[0], /방문자 [ABC]/.exec(r.textContent ?? '')?.[0]]);
    return {
      rows,
      /** 첫 조회가 끝나 표가 나올 때까지 기다린다 */
      loaded: async () => { await screen.findByRole('table', { name: '점유·대여 목록' }); return rows(); },
      /** 서버의 목록을 바꾸고 모니터 상태를 바꿔 패널이 다시 조회하게 한다 */
      change: (next: unknown[]) => { body = next; view.rerender(ui(SCENES.idle())); }
    };
  }

  it('FR12A 같은 공용 조명을 둘이 쓰면 사람마다 한 행씩 그리고 React 열쇠 경고가 없다', async () => {
    const error = vi.spyOn(console, 'error').mockImplementation(() => undefined);
    try {
      const panel = setup(FIRST);
      expect(await panel.loaded()).toEqual([['사물함 1', '방문자 C'], ['조명 1', '방문자 A'], ['조명 1', '방문자 B']]);
      expect(error.mock.calls.flat().join(' ')).not.toMatch(/same key/);
    } finally {
      error.mockRestore();
    }
  });

  it('FR12A 다시 조회해 목록이 줄면 표도 준다 (지난 행이 남지 않는다)', async () => {
    const panel = setup(FIRST);
    expect(await panel.loaded()).toHaveLength(3);
    panel.change([row('light-1', 'p-a', 'shared'), row('light-1', 'p-b', 'shared')]);
    await waitFor(() => expect(panel.rows()).toEqual([['조명 1', '방문자 A'], ['조명 1', '방문자 B']]));
  });

  it('FR12A 같은 조명을 쓰는 사람이 바뀌면 행도 그 사람으로 바뀐다', async () => {
    const panel = setup(FIRST);
    await panel.loaded();
    panel.change([row('light-1', 'p-b', 'shared'), row('light-1', 'p-c', 'shared')]);
    await waitFor(() => expect(panel.rows()).toEqual([['조명 1', '방문자 B'], ['조명 1', '방문자 C']]));
  });
});

describe('HistoryPanel 여러 장소', () => {
  it('FR12A 기기 선택지에 주 장소가 아닌 장소의 기기도 있다', () => {
    render(
      <DataCtx.Provider value={{ source: createMockSource({ scene: 'later' }), places: PLACES, snapshot: SCENES.later(), personName: (id) => id, deviceName: (id) => id }}>
        <HistoryPanel onClose={() => {}} onOpenRequest={() => {}} />
      </DataCtx.Provider>
    );
    const options = [...(screen.getByRole('combobox', { name: '기기' }) as HTMLSelectElement).options].map((o) => o.textContent);
    expect(options).toEqual(['전체', '사물함 1', '사물함 2', '사물함 3', '스피커', '조명 1', '조명 2', '조명 3', '무드등']);
  });
});

describe('TrustPanel 평가 리포트', () => {
  const renderTrust = (report: TrustReport) => {
    const source = { ...createMockSource({ scene: 'later' }), trust: () => Promise.resolve(report) };
    render(
      <DataCtx.Provider value={{ source, places: PLACES, snapshot: SCENES.later(), personName: (id) => id, deviceName: (id) => id }}>
        <TrustPanel onClose={() => {}} />
      </DataCtx.Provider>
    );
  };

  it('FR12A 평가 리포트가 없으면 사전 평가 칸에 "평가 리포트 없음"을 보이고, 이번 시연 지표는 그대로 그린다', async () => {
    renderTrust({ live: TRUST_BASE });
    expect(await screen.findByText('평가 리포트 없음')).toBeTruthy();
    expect(screen.getByText('이번 시연')).toBeTruthy();
    expect(screen.queryByRole('table', { name: '인수 기준 지표' })).toBeNull();
    expect(screen.queryByRole('table', { name: '근접 모델 혼동 행렬' })).toBeNull();
  });
  it('FR12A 평가 리포트에 혼동 행렬이 없으면 지표 표만 그리고 혼동 행렬 칸은 그리지 않는다', async () => {
    renderTrust({ live: TRUST_BASE, evaluation: { date: TRUST.evaluation!.date, metrics: TRUST.evaluation!.metrics } });
    expect(await screen.findByRole('table', { name: '인수 기준 지표' })).toBeTruthy();
    expect(screen.getByText(`평가일 ${TRUST.evaluation!.date}`)).toBeTruthy();
    expect(screen.queryByText('평가 리포트 없음')).toBeNull();
    expect(screen.queryByRole('table', { name: '근접 모델 혼동 행렬' })).toBeNull();
  });
  it('FR12A 평가 리포트에 혼동 행렬이 있으면 지표 표와 혼동 행렬을 함께 그린다', async () => {
    renderTrust(TRUST);
    expect(await screen.findByRole('table', { name: '인수 기준 지표' })).toBeTruthy();
    expect(screen.getByRole('table', { name: '근접 모델 혼동 행렬' })).toBeTruthy();
    expect(screen.queryByText('평가 리포트 없음')).toBeNull();
  });
});

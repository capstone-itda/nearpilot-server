// MON-09: 이력의 기기 거르기는 모든 장소의 기기를 고를 수 있다
// MON-10: 신뢰도 패널은 평가 리포트나 혼동 행렬이 없어도 그린다
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import { DataCtx } from '../data/context';
import { PLACES, TRUST, TRUST_BASE } from '../data/mock/fixtures';
import { createMockSource } from '../data/mock/mockSource';
import { SCENES } from '../data/mock/story';
import type { TrustReport } from '../model/types';
import { HistoryPanel, TrustPanel } from './ListPanels';

afterEach(cleanup);

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

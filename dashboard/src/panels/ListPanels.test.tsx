// MON-09: 이력의 기기 거르기는 모든 장소의 기기를 고를 수 있다
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import { DataCtx } from '../data/context';
import { PLACES } from '../data/mock/fixtures';
import { createMockSource } from '../data/mock/mockSource';
import { SCENES } from '../data/mock/story';
import { HistoryPanel } from './ListPanels';

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

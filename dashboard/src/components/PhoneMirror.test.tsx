// D3: 미러링 창이 없을 때의 폰 자리
import { render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import { cleanup } from '@testing-library/react';
import { SCENES } from '../data/mock/story';

afterEach(cleanup);
import { PhoneMirror } from './PhoneMirror';

describe('PhoneMirror', () => {
  it('D3 미러링 창도 목 화면도 없으면 "미러링 창 없음"을 보인다', () => {
    render(<PhoneMirror who="소유자" follow="요청하는 사람을 자동으로 따라가요" />);
    expect(screen.getByText(/미러링 창 없음/)).toBeTruthy();
  });
  it('D3 목 화면을 보일 때는 실제 폰이 아니라고 밝힌다', () => {
    render(<PhoneMirror who="방문자 A" follow="" mock={SCENES.flow().mockPhones![0]} />);
    expect(screen.getByText('목 화면 · 실제 폰 아님')).toBeTruthy();
    expect(screen.queryByText('실제 폰')).toBeNull();
  });
});

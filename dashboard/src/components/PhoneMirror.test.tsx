// D3: 미러링 창이 없을 때의 폰 자리
import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { PhoneMirror } from './PhoneMirror';

describe('PhoneMirror', () => {
  it('D3 미러링 창도 목 화면도 없으면 "미러링 창 없음"을 보인다', () => {
    render(<PhoneMirror who="소유자" follow="요청하는 사람을 자동으로 따라가요" />);
    expect(screen.getByText(/미러링 창 없음/)).toBeTruthy();
  });
});

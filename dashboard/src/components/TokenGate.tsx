// 소유자 토큰 입력 화면 (?source=server). 토큰은 이 탭의 세션 저장소에만 둔다 (data/server/token.ts).
import { useState } from 'react';

export interface TokenGateProps {
  theme: 'dark' | 'light';
  /** 앞서 넣은 토큰이 거부된 이유. 처음 열었으면 없다 */
  reason?: 'invalid' | 'not_owner';
  onSubmit: (token: string) => void;
}

const REASON_TEXT = {
  invalid: '토큰이 올바르지 않아요. 다시 넣어 주세요.',
  not_owner: '소유자 토큰이 아니에요. 소유자 토큰을 넣어 주세요.'
};

export function TokenGate({ theme, reason, onSubmit }: TokenGateProps) {
  const [value, setValue] = useState('');
  const token = value.trim();
  return (
    <div className="np-app" data-theme={theme}>
      <main className="np-gate" data-component="TokenGate">
        <form className="np-gate__card" onSubmit={(e) => { e.preventDefault(); if (token) onSubmit(token); }}>
          <span className="np-cap">NEARPILOT · 운영 모니터</span>
          <h1 className="np-gate__title">소유자 토큰</h1>
          <p className="np-gate__hint">서버에 연결하려면 소유자 토큰이 필요해요. 토큰은 이 탭에만 두고 주소에는 쓰지 않아요.</p>
          <label className="np-field">
            <span className="np-field__label">토큰</span>
            <input className="np-field__input" type="password" autoComplete="off" spellCheck={false} autoFocus required value={value} onChange={(e) => setValue(e.target.value)} />
          </label>
          {reason ? <p className="np-gate__error" role="alert">{REASON_TEXT[reason]}</p> : null}
          <button className="np-btn" data-variant="primary" type="submit">연결</button>
        </form>
      </main>
    </div>
  );
}

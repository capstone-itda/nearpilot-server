import { useEffect, useRef, useState } from 'react';
import type { MockPhoneScreen } from '../model/types';
import { BLANK_CHECKS, BLANK_CHECK_MS, BLANK_SAMPLE, isBlank } from '../view/mirror';

export interface PhoneMirrorProps {
  who: string;
  follow: string;
  /** 이 사람에게 연결한 미러링 영상 (SET-02) */
  stream?: MediaStream;
  /** 영상이 없을 때 쓰는 목 화면 */
  mock?: MockPhoneScreen;
}

export function MirrorVideo({ stream, live, blank, onBlank }: { stream: MediaStream; live?: boolean; blank?: boolean; onBlank?: (blank: boolean) => void }) {
  const ref = useRef<HTMLVideoElement>(null);
  useEffect(() => {
    if (ref.current) ref.current.srcObject = stream;
  }, [stream]);
  // 영상을 작게 줄여 주기적으로 보고, 검은 화면만 이어지면 신호 없음을 알린다
  useEffect(() => {
    if (!onBlank) return;
    const canvas = document.createElement('canvas');
    canvas.width = canvas.height = BLANK_SAMPLE;
    const ctx = canvas.getContext('2d', { willReadFrequently: true });
    let dark = 0;
    const id = window.setInterval(() => {
      const v = ref.current;
      if (!ctx || !v || v.readyState < 2) return;
      ctx.drawImage(v, 0, 0, BLANK_SAMPLE, BLANK_SAMPLE);
      dark = isBlank(ctx.getImageData(0, 0, BLANK_SAMPLE, BLANK_SAMPLE).data) ? dark + 1 : 0;
      onBlank(dark >= BLANK_CHECKS);
    }, BLANK_CHECK_MS);
    return () => window.clearInterval(id);
  }, [stream, onBlank]);
  // 신호가 없을 때도 영상은 계속 받아 확인해야 하므로 지우지 않고 보이지 않게만 둔다
  return <video ref={ref} className={live ? (blank ? 'np-phone__probe' : 'np-phone__live np-flash') : 'np-phone__video'} autoPlay muted playsInline aria-hidden={blank || undefined} />;
}

function MockScreen({ mock }: { mock: MockPhoneScreen }) {
  if (mock.scene === 'idle') return <p className="np-phone__idle">무엇을 도와드릴까요?</p>;
  if (mock.scene === 'notice' && mock.notice) {
    return (
      <>
        <p className="np-phone__clock">{mock.notice.clock}</p>
        <div className="np-phone__notice">
          <span className="np-cap">NearPilot · 지금</span>
          <span className="np-phone__notice-title">{mock.notice.title}</span>
          <span className="np-phone__notice-body">{mock.notice.body}</span>
          <span className="np-phone__notice-hint">눌러서 승인 화면 열기</span>
        </div>
        <span className="np-phone__spacer" />
      </>
    );
  }
  return (
    <>
      <p className="np-phone__msg-me">{mock.said}</p>
      <span className="np-phone__tool">NearPilot · request_use</span>
      <p className="np-phone__msg-ai">{mock.reply}</p>
      <span className="np-phone__spacer" />
      <span className="np-phone__composer">메시지 입력</span>
    </>
  );
}

export function PhoneMirror({ who, follow, stream, mock }: PhoneMirrorProps) {
  const [blank, setBlank] = useState(false);
  useEffect(() => setBlank(false), [stream]);
  const scene = stream ? 'video' : mock ? mock.scene : 'idle';
  return (
    <aside className="np-phone" data-component="PhoneMirror" aria-label="폰 미러링">
      {/* 머리글은 한 줄로 두어 폰 화면이 높이를 최대한 쓴다 */}
      <div className="np-phone__head">
        <span className="np-phone__who">{who}</span>
        <span className="np-phone__follow"><span className="np-phone__follow-dot" aria-hidden="true" />{follow}</span>
      </div>
      <div className="np-phone__stage">
        {/* 실제 영상은 가짜 폰 틀 없이 영상 비율 그대로 칸 높이를 채운다 */}
        {stream ? <MirrorVideo stream={stream} live blank={blank} onBlank={setBlank} /> : null}
        {stream && !blank ? null : <div className="np-phone__device">
          <span className="np-phone__camera" aria-hidden="true" />
          <div key={stream ? 'blank' : JSON.stringify(mock ?? null)} className="np-phone__screen np-flash" data-scene={stream ? 'blank' : scene} data-mock={stream ? undefined : 'mirror'}>
            {stream ? (
              <div className="np-phone__blank" role="status">
                <span className="np-phone__blank-title">폰 화면 신호 없음</span>
                <span className="np-phone__blank-text">미러링 창이 검은 화면만 보내요. 폰 USB 연결과 폰 화면이 켜져 있는지 확인해요.</span>
              </div>
            ) : mock ? <MockScreen mock={mock} /> : <p className="np-phone__idle">미러링 창 없음 · 설정의 폰 연결에서 골라요</p>}
          </div>
        </div>}
      </div>
      <div className="np-phone__foot"><span className="np-cap">{stream ? (blank ? '폰 화면 · 신호 없음' : '폰 화면 · 미러링 영상') : mock ? '목 화면 · 실제 폰 아님' : '미러링 영상 없음'}</span>{stream ? <span className="np-cap">실제 폰</span> : null}</div>
    </aside>
  );
}

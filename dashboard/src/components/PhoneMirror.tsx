import { useEffect, useRef } from 'react';
import type { MockPhoneScreen } from '../model/types';

export interface PhoneMirrorProps {
  who: string;
  follow: string;
  /** 이 사람에게 연결한 미러링 영상 (SET-02) */
  stream?: MediaStream;
  /** 영상이 없을 때 쓰는 목 화면 */
  mock?: MockPhoneScreen;
}

export function MirrorVideo({ stream }: { stream: MediaStream }) {
  const ref = useRef<HTMLVideoElement>(null);
  useEffect(() => {
    if (ref.current) ref.current.srcObject = stream;
  }, [stream]);
  return <video ref={ref} className="np-phone__video" autoPlay muted playsInline />;
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
  const scene = stream ? 'video' : mock ? mock.scene : 'idle';
  return (
    <aside className="np-phone" data-component="PhoneMirror" aria-label="폰 미러링">
      <div className="np-phone__head">
        <span className="np-cap">폰 화면</span>
        <span className="np-phone__who">{who}</span>
        <span className="np-phone__follow"><span className="np-phone__follow-dot" aria-hidden="true" />{follow}</span>
      </div>
      <div className="np-phone__stage">
        <div className="np-phone__device">
          <span className="np-phone__camera" aria-hidden="true" />
          <div className="np-phone__screen" data-scene={scene} data-mock={stream ? undefined : 'mirror'}>
            {stream ? <MirrorVideo stream={stream} /> : mock ? <MockScreen mock={mock} /> : <p className="np-phone__idle">미러링 창 없음 · 설정의 폰 연결에서 골라요</p>}
          </div>
        </div>
      </div>
      <div className="np-phone__foot"><span className="np-cap">미러링 영상</span><span className="np-cap">실제 폰</span></div>
    </aside>
  );
}

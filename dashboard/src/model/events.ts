// FR-12B 모니터링 스트림의 이벤트. 메시지 형식은 미결이라 이 형태는 가정이다 (spec 7절).
// 서버 어댑터는 WebSocket 메시지를 이 형태로 바꿔 넘긴다. 시각은 서버 시각 (HH:MM:SS.mmm).
import type {
  Action, AuditRow, DeviceState, HostKind, MockPhoneScreen, Person, ReasonCode, RequestProgress, Verdict
} from './types';

interface At { at: string }

export type StreamEvent = At & (
  /** 비콘 수신 상태. 가명과 역할을 함께 준다 (사용자 ID는 없다, FR-11) */
  | { type: 'beacon'; personId: string; pseudonym: string; letter?: string; role: Person['role']; seen: boolean }
  /** 기기별 비콘 RSSI (dBm) */
  | { type: 'rssi'; personId: string; rssi: Record<string, number> }
  /** AI 호스트의 MCP 도구 호출이 서버에 도착함 */
  | { type: 'mcp_call'; requestId: string; personId: string; host: HostKind; tool: RequestProgress['tool']; target: RequestProgress['target']; action: Action }
  /** 판정 단계 ⓪~⑥ 하나의 결과 */
  | { type: 'step'; requestId: string; step: number; result: 'pass' | 'stop'; detail?: string }
  /** ③ 근접의 후보 확률과 이 요청에 적용한 임계값 */
  | { type: 'posterior'; requestId: string; posteriors: Record<string, number>; threshold: number }
  /** 판정 */
  | { type: 'verdict'; requestId: string; verdict: Verdict; reason: ReasonCode; nodeId?: string }
  /** 서버가 MQTT로 기기에 명령을 보냄 */
  | { type: 'command'; requestId: string; nodeId: string; action: Action }
  /** 기기 실행 결과. unknown은 기한 안에 결과를 받지 못함 */
  | { type: 'exec_result'; requestId: string; nodeId: string; result: 'succeeded' | 'failed' | 'unknown' }
  /** 서버가 AI 호스트에 도구 호출의 결과를 돌려줌 */
  | { type: 'mcp_result'; requestId: string }
  /** 기기 상태 전이 */
  | { type: 'transition'; nodeId: string; to: DeviceState; overdue?: boolean; fault?: { cause: ReasonCode; action?: Action } }
  /** 승인 신청(request_access)과 그 끝 */
  | { type: 'approval'; requestId: string; nodeId: string; personId: string; kind: 'requested' | 'expired' }
  /** 감사 로그 한 줄 */
  | { type: 'audit'; row: AuditRow }
  /** 어댑터가 만드는 연결 상태. 서버 이벤트가 아니다 */
  | { type: 'link'; connected: boolean; reconnectAttempts: number }
  /** 목 어댑터 전용: 사람의 가짜 폰 화면. 서버 어댑터는 보내지 않는다 */
  | { type: 'mock_phone'; screen: MockPhoneScreen }
);

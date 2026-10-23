// 화면이 의존하는 데이터 창구. 읽기 함수만 둔다 (FR-12A: 대시보드는 서버에 쓰지 않는다).
import type {
  Approvals, AuditRow, Health, PlacesInfo, HistoryQuery, HistoryRow,
  RentalInfo, RequestProgress, TrustReport, UseRow
} from '../model/types';
import type { StreamEvent } from '../model/events';

export interface DataSource {
  /** 모니터링 스트림 (FR-12B). 받은 이벤트를 받은 순서대로 묶어서 준다. 연결 직후 첫 묶음은 지금 상태를 다시 보낸 이벤트다. 해제 함수를 돌려준다. */
  subscribe(listener: (events: StreamEvent[]) => void): () => void;
  /** 서버가 아닌 목 데이터인가 (상단 바에 적는다) */
  readonly mock?: boolean;
  /** 끊긴 스트림을 다시 연결한다. 서버 상태는 바꾸지 않는다. */
  reconnect(): void;

  // 대시보드 조회 REST (FR-12A). 경로와 형식은 미결이다.
  /** 장소 목록(장소마다 이름, 모델·보정 버전, 기기 목록)과 약한 신호 기준. 무대 배치는 이 PC에서 정한다. */
  places(): Promise<PlacesInfo>;
  uses(): Promise<UseRow[]>;
  approvals(): Promise<Approvals>;
  audit(): Promise<AuditRow[]>;
  history(query: HistoryQuery): Promise<HistoryRow[]>;
  /** 요청 한 건의 판정 단계별 결과 */
  request(requestId: string): Promise<RequestProgress>;
  trust(): Promise<TrustReport>;
  rental(nodeId: string): Promise<RentalInfo>;
  health(): Promise<Health>;
}

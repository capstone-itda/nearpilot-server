// 화면이 의존하는 데이터 창구. 읽기 함수만 둔다 (FR-12A: 대시보드는 서버에 쓰지 않는다).
import type {
  Approvals, AuditRow, Health, PlaceInfo, HistoryQuery, HistoryRow,
  RentalInfo, RequestProgress, Snapshot, TrustReport, UseRow
} from '../model/types';

export interface DataSource {
  /** 모니터링 스트림 (FR-12B). 바뀔 때마다 새 스냅숏을 준다. 해제 함수를 돌려준다. */
  subscribe(listener: (snapshot: Snapshot) => void): () => void;
  /** 끊긴 스트림을 다시 연결한다. 서버 상태는 바꾸지 않는다. */
  reconnect(): void;

  // 대시보드 조회 REST (FR-12A). 경로와 형식은 미결이다.
  /** 장소 이름, 모델 버전, 기기 목록. 무대 배치는 이 PC에서 정한다. */
  place(): Promise<PlaceInfo>;
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

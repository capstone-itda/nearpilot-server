# FR-23 any:locker와 실제 DB의 통합 시나리오

- 담당: eunji719 (판정), DB와 공동
- 요구사항: FR-10, FR-11, FR-19, FR-23, NFR-03
- 따르는 결정: #27
- 선행: `core-integration-real-db`, `core-allocation-window`

## 1. 왜

- 대기 구간과 `any:locker` 연결(`core-allocation-window`)은 fake 저장소로만 검증했다. 방문자 둘이 같은 사물함을 두고 겹칠 때 실제 DB에서 중복 예약이 없는지 확인하지 못했다.
- 대기 구간은 여러 스레드가 같은 구간을 쓴다. 스레드마다 DB 연결이 따로여서, 실제 DB의 `BEGIN IMMEDIATE` 점유와 함께 동작하는지 봐야 한다.

## 2. 무엇

- 입력: 없다. 테스트만 추가한다.
- 출력: `server/tests/integration/`의 시나리오 테스트
- 바뀌는 상태: 없다. 임시 파일 SQLite를 테스트마다 새로 만든다.
- 약속: 실제 DB(`SqliteRepository`), 정책 검사, 실제 대기 구간(`AllocationWindow`)을 연결해서 아래 시나리오가 성공한다. 실패하는 시나리오가 있으면 코드를 고치지 않고 원인을 기록해서 담당자에게 알린다.
  - 대신하는 것: 근접 추정, 실행, 감사, 기기 복원 여부

## 3. 제약

- 코드는 `server/tests/integration/`에만 둔다. `src/`를 고치지 않는다.
- 스레드마다 DB 연결을 따로 만든다. SQLite 연결을 스레드 사이에서 공유하지 않는다.
- 대기 구간은 실제 시간(`allocation_window_ms`)을 쓰되 테스트에서는 짧게 설정한다.
- 테스트 데이터는 SQL로 직접 넣는다. 난수와 실제 시계를 쓰지 않는다 (NFR-09).

## 4. 경계

### 고치는 곳
- `server/tests/integration/`
- `specs/core-integration-any-locker/`

### 고치지 않는 곳
- 항상: `docs/`, `AGENTS.md` (Gotchas 절은 제외), `server/src/nearpilot/shared/`, `server/pyproject.toml`, `.github/`
- 이 작업: `server/src/` 전체

### 다른 영역과 닿는 곳
| 계약 | 사용 또는 제공 | 상대 영역 |
|---|---|---|
| `SqliteRepository`, `RepositoryPolicyChecker` | 사용 | DB |
| `DecisionPipeline`, `AllocationWindow` | 사용 | 판정 |

### 만들지 않는 것
- 실제 실행과 MQTT를 붙인 시나리오 (서버 조립 #88 뒤)
- 부하·동시성 시험 (지표 3)

## 5. 예외 상황

| 시나리오 | 기대 | 테스트 |
|---|---|---|
| 방문자 둘, 허용 사물함 둘 | 서로 다른 사물함을 받는다. 점유 세션이 둘이다 | `test_FR23_허용_사물함이_둘이면_서로_다른_기기를_받는다` |
| 방문자 둘, 허용 사물함 하나 | 한쪽은 `OK`, 한쪽은 `BUSY`. 점유 세션이 하나다 | `test_FR23_허용_사물함이_하나면_한쪽만_받는다` |
| 승인이 필요한 사물함이 더 가깝다 | 승인이 필요 없는 사물함을 받는다. 승인 필요 기기는 점유하지 않는다 | `test_FR23_승인이_필요한_사물함은_배정하지_않는다` |
| 방문자 하나 | 구간이 닫힌 뒤 `OK` | `test_FR23_방문자_하나도_배정받는다` |
| 같은 `request_id`로 다시 보낸다 | 기존 판정을 반환한다. 세션이 늘지 않는다 | `test_FR10_any_재전송은_기존_판정을_반환한다` |
| 호스트 응답 | 사용자 ID, 다른 방문자 정보가 없다 | `test_FR11_any_호스트_응답에_내부_값이_없다` |

## 6. 결정

- 세계(계정, 기기, 정책)는 `_world.py`를 확장해서 쓴다. 담당자가 정했다 — 사물함 하나(`locker-4`, 공개·개방)를 더 넣고, 임시 파일 DB와 연결 복제(`fork`)를 추가한다.
- 시나리오 파일은 `test_any_locker_real_db.py` 하나다. 스레드마다 `fork`한 연결과 파이프라인을 쓰고 대기 구간은 하나를 공유한다.

## 7. 가정

- 대기 구간은 60ms다. 첫 요청이 구간에 들어온 것을 `pending()`으로 확인한 뒤 두 번째 요청을 보내서 같은 구간에 들어가게 한다.

## 8. 테스트

| 요구사항 | 테스트 | 확인하는 것 |
|---|---|---|
| FR-23·FR-19 | `test_FR23_*` | 배정, 중복 예약 없음, 승인 필요 기기 제외 |
| FR-10 | `test_FR10_any_재전송은_기존_판정을_반환한다` | 재전송 |
| FR-11 | `test_FR11_any_호스트_응답에_내부_값이_없다` | 호스트 응답 |

- 구현은 `server/tests/integration/test_any_locker_real_db.py`다.

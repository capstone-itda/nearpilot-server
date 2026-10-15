# 근접 추정기 (`ProximityEstimator`)

- 상태: 구현 중
- 담당: dnnals (근접 추정)
- 요구사항: FR-16, FR-22, NFR-02
- 따르는 결정: #26, #31, #38, #39, #40, #42

## 1. 왜

- `shared/interfaces.py`에는 판정이 ③에서 부르는 `ProximityEstimator`가 있다. `core/proximity/`에는 구현이 없다.
- 구현이 없으면 판정은 ③을 fake로만 실행한다. 방문자가 어느 기기 앞에 있는지 RSSI로 계산하지 못한다.
- 판정 spec은 "후보가 있는데 관측이 없을 때의 `posteriors`"를 근접 추정 담당에게 묻는다 (`specs/core-decision-pipeline/spec.md` 7절).

## 2. 무엇

- 입력:
  - `observe(frame, *, node)`: `RssiFrame`과 호출자가 조회한 관측 기기의 `NodeInfo`
  - `posterior(beacons, candidates, at, *, place_nodes)`: 활성 비콘 하나, ②의 후보, 판정 시각, 장소의 전체 기기
  - 장소별 모델 파일
  - `Settings.rssi_frame_sec`
- 출력: `ProximityResult` (`posteriors`, `window_start`, `window_end`, `model_version`, `calib_version`)
- 바뀌는 상태: 메모리의 최근 RSSI 프레임 버퍼. DB를 바꾸지 않는다.
- 약속:
  - 장소의 전체 상태로 사후확률을 계산한다. 전체 상태는 장소의 `trusted` 기기 앞 상태들과 "어느 기기 앞도 아님"이다.
  - 후보의 확률을 다시 정규화하지 않고 반환한다. 합은 1 이하다.
  - `trusted`가 아닌 기기의 RSSI를 쓰지 않는다.
  - 후보가 없으면 빈 `posteriors`를 반환한다.
  - 후보는 있는데 쓸 관측이 없으면 후보마다 처음 분포의 값을 반환한다.
  - 같은 프레임, 모델 파일, 설정, `at`에는 같은 결과를 반환한다.
  - 결과에 모델 파일의 모델 버전과 보정 버전을 붙인다.

## 3. 제약

- 앵커별 RSSI에 관측 모델과 HMM을 적용한다 (FR-16).
- 판정이 넘긴 활성 비콘 하나만 쓴다. 여러 비콘의 확률을 합치지 않는다 (FR-16, #42).
- `FAULT`, 오프라인, 요청 종류가 다른 기기도 `trusted`이면 전체 상태에 넣는다 (FR-16, #39).
- 후보 밖 상태의 확률을 어느 후보에도 더하지 않는다 (FR-16, #39).
- 임계값을 적용하지 않는다. 명시 지정 여부와 지정 대상을 받지 않는다 (#39).
- 파라미터와 기기 배치는 모델 파일에서 읽는다. DB의 `node`에서 읽지 않는다 (FR-16, #40).
- 시간 값과 파라미터를 코드 상수로 쓰지 않는다. 모델 파일과 `Settings`에서 받는다 (AGENTS.md 코딩 규칙).
- 같은 모델, 보정, 설정 값으로 결과를 재현한다 (FR-22).
- `shared`의 모델과 Protocol에만 의존한다 (AGENTS.md 의존 규칙 4).
- 관측 시각 `frame_ts`를 수신 시각으로 바꾸지 않는다 (#38).

## 4. 경계

### 고치는 곳
- `server/src/nearpilot/core/proximity/`
- `server/tests/core/`

### 고치지 않는 곳
- 항상: `docs/`, `AGENTS.md` (Gotchas 절은 제외), `server/src/nearpilot/shared/`, `server/pyproject.toml`, `.github/`
- 이 작업: `core/decision/`, `core/lifecycle/`, `core/policy/`, `core/allocation/`, `api/`, `db/`, `iot/`, `tools/`

### 다른 영역과 닿는 곳
| 계약 | 사용 또는 제공 | 상대 영역 |
|---|---|---|
| `ProximityEstimator.posterior` | 제공 | 판정 (③) |
| `ProximityEstimator.observe` | 제공 | 상태 관리 (`NodeEventSink.on_rssi`, `specs/state-management/core-node-events.md`) |
| `ProximityResult` | 제공 | 판정, DB (`DecisionRecord`로 옮김) |
| `Settings.rssi_frame_sec` | 사용 | 공용 설정 |

### 만들지 않는 것
| 후속 작업 | 내용 | 근거 |
|---|---|---|
| 상태 관리 (`core-node-events`, 작성 중) | `NodeEventSink.on_rssi`에서 `observe`를 부르는 배선. 이 작업의 테스트는 `observe`를 직접 부른다 | `core-node-events` 7절 가정. `NodeEventSink`의 구현 담당은 미결이다 |
| (#88 결정 뒤) | 서버 시작 때 `Repository.rssi_since`로 최근 프레임을 읽어 복원 접점에 넘기는 호출 | #31, #88 |
| (#88 결정 뒤) | 서버 시작 때 장소별로 어느 모델 버전을 읽을지 고르는 조립 코드 | #88 |
| `feat/core-proximity-data-collection` | 수집 회차와 라벨 기록, "앞"의 거리 기준 | FR-16 |
| `feat/core-proximity-calibration` | 실데이터 적합, 보정, 명시 지정 임계값 실험, 운영 모델 파일 | FR-16, FR-17, 지표 8 |

- 임계값 비교와 대상 선택 — 판정의 몫이다 (#39).
- 운영용 모델 파일 — 실데이터가 없다. 이 작업은 테스트용 합성 모델 파일만 만든다.

## 5. 예외 상황

| 상황 | 기대 동작 | 테스트 |
|---|---|---|
| 후보가 없다 | 빈 `posteriors`를 반환한다. 버전과 관측 구간은 채운다 | `test_FR16_empty_candidates` |
| 후보는 있는데 관측 구간에 쓸 프레임이 없다 | 후보마다 처음 분포의 값을 반환한다 | `test_FR16_no_observation_returns_initial_distribution` |
| `place_nodes`에 모델 파일에 없는 `trusted` 기기가 있다 | 예외를 던진다. 판정은 `DENY`/`INTERNAL_ERROR`로 거부한다 | `test_FR16_trusted_node_missing_from_model_raises` |
| 모델 파일에는 있지만 `trusted`가 아닌 기기가 있다 | 그 기기를 전체 상태와 앵커에서 뺀다 | `test_FR16_untrusted_model_node_is_excluded` |
| 장소에 모델 파일이 없다 | 예외를 던진다 | `test_FR16_missing_place_model_raises` |
| `place_nodes`가 비었거나 장소가 둘 이상이다 | 예외를 던진다 | `test_FR16_place_relation_mismatch_raises` |
| 후보가 `place_nodes`에 없거나 `trusted`가 아니다 | 예외를 던진다 | `test_FR16_place_relation_mismatch_raises` |
| `beacons`가 하나가 아니다 | 예외를 던진다 | `test_FR16_beacons_must_be_single` |
| `observe`의 `node.node_id`가 프레임의 `node_id`와 다르다 | 프레임을 쓰지 않는다 | `test_FR16_mismatched_node_frame_is_ignored` |
| `observe`의 `node`가 `trusted`가 아니다 | 프레임을 쓰지 않는다 | `test_FR16_untrusted_frame_does_not_change_result` |
| 관측 기기가 나중에 `trusted`에서 빠졌다 | `posterior` 때의 `place_nodes` 기준으로 그 기기의 프레임을 쓰지 않는다 | `test_FR16_untrusted_frame_does_not_change_result` |
| 같은 `(node_id, session_id, seq, beacon)` 프레임이 다시 온다 | 한 번만 쓴다 | `test_FR16_duplicate_frame_is_used_once` |
| `received_at`이 `at`보다 뒤인 프레임이 있다 | 그 프레임을 쓰지 않는다 | `test_FR22_late_frame_is_excluded` |
| RSSI가 NaN이나 무한대다 | 결측과 같이 그 프레임을 쓰지 않는다 | `test_FR16_non_finite_rssi_is_treated_as_missing` |
| 프레임 도착 순서가 다르다 | 같은 결과를 반환한다 | `test_FR22_same_input_same_output` |
| `observe`와 `posterior`가 다른 스레드에서 동시에 불린다 | 버퍼가 깨지지 않는다 | `test_FR16_concurrent_observe_and_posterior` |
| 모델 파일의 장소 ID, 버전이 경로와 다르거나 필드가 빠졌다 | 읽을 때 예외를 던진다 | `test_FR16_invalid_model_file_raises` |

- 권한 없는 호출, 시간 초과: 해당 없음. 근접 추정은 인증하지 않고 외부를 기다리지 않는다.

## 6. 결정

- 관측이 없는 후보의 값은 처음 분포의 값이다 — 판정 spec 7절의 질문에 대한 답이다. 판정은 임계값과 비교해서 `ASK`를 낸다. 빈 `posteriors`는 명시 지정 요청에서 `posteriors[지정 대상]`이 없고, 0은 감사 기록에서 "그 기기 앞이 아님"으로 읽힌다. 명시 지정 임계값은 처음 분포의 값보다 높아야 한다 (`feat/core-proximity-calibration`에서 확인).
- 모델 파일에 없는 `trusted` 기기가 있으면 예외를 던진다 — 그 기기를 빼고 계산하면 그 기기 앞의 확률이 옆 후보로 옮겨 가서 오선택이 생길 수 있다. 예외는 판정이 거부로 처리하고 감사 기록에 남긴다.
- 예외는 세 가지다 — 입력 관계가 맞지 않으면 `ProximityInputError`(`ValueError`), 장소의 모델이 없거나 `trusted` 기기가 모델에 없으면 `ModelMismatchError`(`LookupError`), 모델 파일이 잘못됐으면 `ModelFileError`(`ValueError`). 판정은 어느 것이든 예상하지 못한 예외로 거부한다.
- 모델 파일의 경로는 `{모델 루트}/{place_id}/{model_version}.json`이고 형식은 JSON이다 — 표준 라이브러리로 읽고 diff로 검토할 수 있다. 모델 루트는 생성자 인자로 받는다. 같은 버전의 파일을 덮어쓰지 않는다 (#40).
- 모델 파일은 아래 필드를 담는다.
  - `place_id`, `model_version`, `calib_version`
  - `window_sec`: 관측 구간 길이
  - `transition.stay`: 한 시간 칸에서 같은 상태에 머무를 확률. 나머지는 다른 상태에 똑같이 나눈다.
  - `nodes`: `node_id`마다 앵커 위치 `x`, `y`, 기기 앞 위치 `front_x`, `front_y`, 경로손실 값 `A`, `n`, `sigma`, "어느 기기 앞도 아님" 상태의 RSSI 평균 `none_rssi`와 표준편차 `none_sigma`
  - `min_distance_m`: 거리 계산의 최솟값
  - `calibration.method`: 보정 방법
  - `validation`: 검증에 쓴 임계값과 결과. 없어도 된다. 근접 추정은 이 값을 계산에 쓰지 않는다 (#40).
- 관측 구간을 매번 다시 계산한다 — `posterior(at)`은 `(at − window_sec, at]`의 프레임으로 처음 분포부터 필터를 다시 돌린다. 판정, 재현, 평가가 같은 함수를 쓴다. 메모리 필터는 서버를 켠 뒤의 모든 프레임에 기대서 재현하기 어렵다 (FR-22).
- 관측 구간 길이 `window_sec`는 모델 파일에 둔다 — HMM 전이와 함께 데이터로 고르는 모델 값이고, 모델 버전과 함께 바뀌어야 재현된다. `Settings`에 새 값을 더하면 공용 설정 변경이다.
- 시간 칸의 길이는 `Settings.rssi_frame_sec`이다 — 기기의 보고 주기와 같다.
- 시간 칸은 `frame_ts`로 나누고, `received_at`이 `at` 이하인 프레임만 쓴다 — 판정 때 아직 도착하지 않은 프레임이 재현에 끼어들지 않는다.
- 칸 번호는 정수 마이크로초로 센다 — `rssi_frame_sec`가 0.3처럼 이진 부동소수로 정확하지 않아도 칸 경계의 프레임이 옆 칸으로 가지 않는다.
- 칸은 `(시작, 끝]`이고 칸 수는 `window_sec / rssi_frame_sec`를 반올림한 값(최소 1)이다. 칸 범위 밖의 프레임은 가까운 양 끝 칸에 넣는다 — `window_sec`가 `rssi_frame_sec`의 정수배가 아니어도 관측 구간의 프레임을 버리지 않는다.
- 계산 순서를 고정한다 — 프레임은 `(frame_ts, node_id, session_id, seq, received_at, rssi)` 순으로 정렬한다. 같은 `(node_id, session_id, seq, beacon)`은 이 순서로 처음 하나만 쓴다. 상태는 `node_id` 순과 마지막의 "어느 기기 앞도 아님" 순이다. 무작위를 쓰지 않는다.
- 버퍼는 같은 `(node_id, session_id, seq, beacon)`의 프레임 중 먼저 도착한 하나만 둔다 — DB도 먼저 온 프레임만 저장한다 (FR-05). 그래서 `posterior`와 DB 프레임으로 부른 재현 함수의 결과가 같다. 같은 키에 값이 다른 프레임이 오면 `posterior`의 결과는 도착 순서를 따른다. 재현 함수의 정렬 규칙은 두 프레임을 모두 받은 경우에만 적용된다.
- 계산은 로그 공간에서 한다 — 작은 확률의 곱에서 언더플로가 나지 않는다.
- 버퍼는 잠금 하나로 보호한다. 버퍼에서 가장 늦은 `received_at`보다 `가장 긴 window_sec + rssi_frame_sec` 넘게 이른 프레임을 버린다 — 관측 구간은 `frame_ts`로 자르고 버퍼는 `received_at`으로 자르므로 칸 하나를 더 둔다. 그래서 `at`이 가장 늦은 `received_at − rssi_frame_sec`보다 이르면 프레임이 빠진 결과가 나온다. 과거 구간의 재현은 DB 프레임으로 재현 함수를 직접 부른다.
- 유한하지 않은 RSSI는 결측과 같이 버린다 — "결측은 증거로 쓰지 않는다"는 가정과 맞고, 판정과 재현에 같은 규칙이 적용된다.
- 재시작 복원은 DB에서 읽은 프레임을 `observe`와 같은 규칙으로 버퍼에 넣는 접점 하나로 한다 — `core-restore`가 HMM 복원을 근접 추정에 맡겼다. 관측 구간을 다시 계산하므로 최근 프레임만 다시 넣으면 필터가 복원된다. 이 접점은 `shared` Protocol 밖의 구현 메서드다.
- 재현용 순수 함수를 따로 둔다 — `posterior`는 버퍼의 프레임으로 이 함수를 부른다. 재현과 평가는 DB의 프레임으로 같은 함수를 부른다.

## 7. 가정

FR-16의 미결(관측 특징, 결측값 처리, HMM 상태와 전이, 보정 방법)은 이슈가 없다. 결정이 어떻게 나도 `core/proximity/`와 모델 파일만 바뀌므로 가정을 세우고 진행한다.

- 관측 특징은 앵커별 RSSI 값 하나다. 기기 앞 상태의 RSSI는 `A − 10·n·log10(거리)`를 평균으로, `sigma`를 표준편차로 하는 정규분포를 따른다. 거리는 앵커와 기기 앞 위치 사이의 거리다 — 미결 (FR-16)
- "어느 기기 앞도 아님" 상태의 RSSI는 앵커마다 `none_rssi`, `none_sigma`의 정규분포를 따른다 — 미결 (FR-16)
- 한 시간 칸에 프레임이 없는 앵커는 증거로 쓰지 않는다. 그 앵커의 우도는 모든 상태에서 같다 — 미결 (FR-16 결측값 처리)
- HMM 상태는 전체 상태와 같다. 처음 분포는 균등이다. 전이는 머무를 확률 하나로 정한다 — 미결 (FR-16 HMM 상태와 전이)
- 보정은 하지 않는다. `calibration.method`는 `none`이다 — 미결 (FR-16 보정 방법)
- 한 시간 칸에 같은 앵커의 프레임이 여럿이면 RSSI를 평균한다 — 미결 (프레임 안 집계 방법, MQTT 계약 v1에 없음)

## 8. 테스트

| 요구사항 | 테스트 | 확인하는 것 |
|---|---|---|
| FR-16 | `test_FR16_front_position_selects_device` | 합성 배치에서 기기 앞에 선 방문자의 사후확률 최대 기기가 그 기기다 |
| FR-16 | `test_FR16_candidate_sum_at_most_one` | 후보 확률을 다시 정규화하지 않는다. 합은 1 이하다 |
| FR-16 | `test_FR16_outside_state_does_not_raise_candidates` | 후보 밖 기기 앞에 서면 후보 확률이 모두 낮다 |
| FR-16 | 5절 표의 `test_FR16_*` | 예외 상황 |
| FR-22 | `test_FR22_same_input_same_output`, `test_FR22_late_frame_is_excluded`, `test_FR22_replay_matches_posterior` | 같은 입력에 같은 결과. 재현 함수와 `posterior`의 결과가 같다 |
| FR-16 | `test_FR16_restore_matches_observe` | 복원 접점으로 넣은 프레임과 `observe`로 넣은 프레임의 결과가 같다 |
| NFR-02 | `test_NFR02_switch_within_two_seconds`, `test_NFR02_flicker_reduced` | 합성 이동 경로에서 대상 전환이 2초 이내다. 정지 때 깜빡임이 시간 칸마다 고른 최대 우도 기기보다 80% 이상 적다 |

- 합성 데이터는 시드를 고정한 생성기로 만든다.
- NFR-02의 인수는 지표 8의 실데이터로 한다. 이 작업의 NFR-02 테스트는 합성 데이터의 회귀 테스트다.
- 합성 조건: 잡음은 모델의 `sigma`와 같은 4 dB, 프레임 탈락 30%, 시드 20개. 측정 순서는 실제 서버처럼 초마다 `observe`한 뒤 `posterior`를 부른다.
- 깜빡임은 기준선 757회에서 HMM 47회로 94% 줄었다 (통과).
- 전환은 20개 시드 중 15개가 2초 이내다 (시드별 지연 2, 3, 1, 28, 2, 1, 1, 1, 1, 2, 22, 1, 2, 1, 1, 2, 1, 1, 1, 3초). `test_NFR02_switch_within_two_seconds`는 `xfail(strict=True)`로 둔다. 머무를 확률과 관측 구간은 `feat/core-proximity-calibration`에서 검증셋으로 고른다. 그 작업에서 통과하면 strict 때문에 실패로 드러나므로 표시를 뺀다.
- 잡음이 모델 `sigma`의 두 배(8 dB)이면 깜빡임은 41%만 줄고 전환은 0/20이다. 실데이터로 `sigma`를 적합하는 일이 반응 품질에 크다.

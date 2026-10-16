# 근접 모델 적합과 보정 (`core/proximity` 적합·보정)

- 담당: dnnals (근접 추정)
- 요구사항: FR-16, FR-22
- 따르는 결정: #39, #40, #41

## 1. 왜

- 근접 추정기는 모델 파일의 값으로만 계산한다. 그런데 모델 파일을 만드는 코드가 없다. 지금 있는 모델 파일은 손으로 쓴 테스트용 합성 파일뿐이다.
- 수집 회차가 붙은 관측이 생겨도 경로손실 값, 머무를 확률, 관측 구간을 고르는 방법이 정해져 있지 않다. 그러면 지표 8을 평가할 모델을 만들 수 없다.
- 모델 파일은 `calibration.method`로 `none`만 받는다. 공개 데이터 점검에서 HMM은 같은 위치 오차를 칸마다 새 증거처럼 쌓아 과신했다. 보정 방법을 넣을 자리가 없으면 지표 8의 ECE 기준(≤ 0.05)을 맞출 방법이 없다.
- requirements는 관측 처리, 보정, HMM을 교체할 수 있는 인터페이스로 분리하라고 요구한다 (requirements.md §1.2).

## 2. 무엇

- 입력:
  - 학습 회차와 검증 회차: 회차마다 회차 ID, 라벨(`node_id` 또는 `none`), 비콘, 구간, 그 회차에 연결된 `RssiFrame` 목록
  - 장소 배치: 기기마다 앵커 위치와 기기 앞 위치, `none_points`
  - 적합 설정: 모델 버전, 보정 버전, `min_distance_m`, 후보 τ·`stay`·`window_sec` 목록
  - `Settings.rssi_frame_sec`
- 출력:
  - 적합한 `ProximityModel`과 후보 조합마다의 검증 손실
  - 모델 파일 `{모델 루트}/{place_id}/{model_version}.json`
- 바뀌는 상태: 새 모델 파일 하나. DB를 읽거나 쓰지 않는다.
- 약속:
  - 모델 파일의 `calibration.method`로 `none`과 `temperature`를 받는다. `temperature`는 시간 칸마다 증거의 로그우도를 τ로 나눈다.
  - 학습 회차로 앵커별 `A`, `n`, `sigma`, `none_rssi`, `none_sigma`를 적합한다.
  - 검증 회차로 τ, `stay`, `window_sec`의 조합을 고른다. 기준은 정답 상태의 로그 손실이고, 근접 추정기와 같은 계산 함수로 잰다.
  - 같은 입력에는 같은 모델 파일을 만든다.
  - 쓴 모델 파일은 `load_model`이 그대로 읽는다. 같은 버전의 파일을 덮어쓰지 않는다.

## 3. 제약

- 학습·검증 분할을 하지 않는다. 호출자가 회차 단위로 나눠서 넘긴다. 분할과 평가는 `tools/evaluation`이 맡는다 (`architecture.md` §4 RSSI 수집 회차, #41).
- 같은 회차를 학습과 검증에 함께 쓰지 않는다 (지표 8).
- DB를 읽지 않는다. 수집 회차의 조회 접점이 미결이다 (`architecture.md` §4).
- 사후확률은 근접 추정기의 `compute_posteriors`로 계산한다. 적합용으로 다른 계산을 두지 않는다 (FR-22).
- 모델 파일은 `{모델 루트}/{place_id}/{model_version}.json`이다. 같은 버전의 파일을 덮어쓰지 않는다 (#40).
- 후보 값과 버전을 코드 상수로 쓰지 않는다. 적합 설정으로 받는다 (AGENTS.md 코딩 규칙).
- 명시 지정 임계값을 고르지 않는다. 근접 추정은 임계값을 적용하지 않는다 (#39).
- `shared`의 모델에만 의존한다 (AGENTS.md 의존 규칙 4).

## 4. 경계

### 고치는 곳
- `server/src/nearpilot/core/proximity/`
- `server/tests/core/proximity/`
- `specs/core-proximity-estimator/spec.md` (모델 파일 필드, 보정 가정, 후속 작업 표를 이 작업과 맞춘다)

### 고치지 않는 곳
- 항상: `docs/`, `AGENTS.md` (Gotchas 절은 제외), `server/src/nearpilot/shared/`, `server/pyproject.toml`, `.github/`
- 이 작업: `core/decision/`, `core/lifecycle/`, `core/policy/`, `core/allocation/`, `api/`, `db/`, `iot/`, `tools/`

### 다른 영역과 닿는 곳
| 계약 | 사용 또는 제공 | 상대 영역 |
|---|---|---|
| `RssiFrame` | 사용 | 공용 모델 |
| `NodeInfo` | 사용 (평가용 장소 기기를 만든다) | 공용 모델 |
| `Settings.rssi_frame_sec` | 사용 | 공용 설정 |
| 모델 파일 (`load_model`) | 제공 | 서버 조립 (`specs/core-server-assembly/spec.md`) |

### 만들지 않는 것
| 후속 작업 | 내용 | 근거 |
|---|---|---|
| DB의 `tools/evaluation` | 수집 회차와 관측을 DB에서 읽어 회차 단위로 나누고 이 적합 함수를 부르는 호출, 시험셋 평가와 지표 8 | `architecture.md` §4, 지표 8 |
| `feat/core-proximity-calibration` | 실데이터 적합, 운영 모델 파일, 운영 `none_points`, 명시 지정 임계값 실험, NFR-02 `xfail` 해제 | FR-16, FR-17, 지표 8 |

- 적합 CLI와 입력 파일 형식 — 입력을 만드는 쪽(회차 조회 접점)이 미결이다.
- 기기 배치의 측정 — 실제 장소의 배치는 사람이 재서 넘긴다.

## 5. 예외 상황

| 상황 | 기대 동작 | 테스트 |
|---|---|---|
| `calibration.method`가 `temperature`인데 `tau`가 없거나, 0 이하이거나, 모르는 키가 있다 | `ModelFileError` | `test_FR16_invalid_model_file_raises` |
| `calibration.method`가 `none`인데 `tau`가 있다 | `ModelFileError` | `test_FR16_invalid_model_file_raises` |
| `calibration.method`가 모르는 값이다 | `ModelFileError` | `test_FR16_invalid_model_file_raises` |
| 같은 회차 ID가 학습과 검증에 함께 있거나, 한쪽에 두 번 있다 | `FitError`. 아무것도 쓰지 않는다 | `test_FR16_fit_rejects_shared_run` |
| 라벨이 배치에 없는 기기다 (`none`이 아니다) | `FitError` | `test_FR16_fit_rejects_unknown_label` |
| 학습 회차에 기기 앞 회차나 `none` 회차가 없다 | `FitError` | `test_FR16_fit_rejects_missing_label_kind` |
| 앵커 하나가 기기 앞 회차에서 프레임을 세 개 이상 받지 못했거나, 서로 다른 거리 두 개 이상을 받지 못했다. 거리는 log10 뒤의 값으로 센다 | `FitError`. 앵커 ID를 쓴다 | `test_FR16_fit_rejects_underdetermined_anchor`, `test_FR16_fit_counts_distances_after_log` |
| 앵커 하나가 `none` 회차에서 프레임을 두 개 이상 받지 못했다 | `FitError`. 앵커 ID를 쓴다 | `test_FR16_fit_rejects_underdetermined_anchor` |
| 적합한 `n`이 0 이하이거나, `sigma` 또는 `none_sigma`가 0이다 | `FitError`. 앵커 ID와 값을 쓴다. 모델 파일이 받지 않는 값이다 | `test_FR16_fit_rejects_nonpositive_n` |
| 후보 목록이 비었거나 범위 밖이다 (τ ≤ 0, `stay`가 (0, 1) 밖, `window_sec` ≤ 0, 유한하지 않은 값). 버전이 빈 문자열이거나 `min_distance_m` ≤ 0이다 | `FitError` | `test_FR16_fit_rejects_invalid_grid` |
| `model_version`이나 배치의 `place_id`가 경로 조각 하나가 아니다 (빈 값, `.`, `..`, `/`나 `\`가 있다) | `FitError`. 모델 루트 밖에 쓰거나 `load_model`이 읽지 못하는 파일을 만들지 않는다 | `test_FR16_fit_rejects_non_component_names` |
| 배치의 좌표나 `none_points`가 유한하지 않다 | `FitError` | `test_FR16_fit_rejects_non_finite_layout` |
| `fit_observation_params`를 직접 부를 때 `min_distance_m`이 0 이하이거나 유한하지 않다 | `FitError` | `test_FR16_fit_observation_params_checks_min_distance` |
| `bin_sec`가 1 마이크로초 단위로 0이 된다 (0.5 µs 미만) | `FitError` | `test_FR16_fit_rejects_bin_sec_below_timedelta_resolution` |
| 검증 회차가 가장 긴 후보 `window_sec`보다 짧아 평가 시각이 없다 | `FitError`. 회차 ID를 쓴다 | `test_FR16_fit_rejects_short_validation_run` |
| 검증 회차가 없다 | `FitError` | `test_FR16_fit_rejects_short_validation_run` |
| 프레임의 비콘이 회차의 비콘과 다르거나, 앵커가 배치에 없거나, `frame_ts`가 구간 밖이다 | 그 프레임을 쓰지 않는다 | `test_FR16_fit_ignores_foreign_frames` |
| 정답 상태의 확률이 0으로 내려간다 | 그 조합의 손실은 무한대다. 다른 조합을 고른다. 모든 조합이 무한대이면 `FitError` | `test_FR16_zero_probability_loses` |
| 쓸 모델 파일이 이미 있다 | `FileExistsError`. 있는 파일을 바꾸지 않는다 | `test_FR16_write_refuses_overwrite` |
| `ProximityModel`을 만들 때 `calibration_method`가 `none`, `temperature`가 아니거나, `none`인데 τ ≠ 1.0이거나, `temperature`인데 τ가 0 이하·유한하지 않다 | `ValueError`. 계산(τ)과 파일(방법)이 어긋난 모델을 만들지 않는다 | `test_FR16_model_rejects_inconsistent_calibration` |
| 쓸 모델에 무한대·NaN 값이 남아 있다 | `ValueError`. 디렉터리도 파일도 만들지 않는다 | `test_FR16_write_rejects_non_finite_values` |
| `write_model_file`에 넘긴 모델의 `place_id`나 `model_version`이 경로 조각 하나가 아니다 | `ValueError`. 루트 안에도 밖에도 아무것도 만들지 않는다 | `test_FR16_write_rejects_non_component_names` |
| `validation_log_loss`를 직접 부를 때 회차가 없거나, `bin_sec`가 0 이하·유한하지 않거나, 라벨이 배치에 없다 | `FitError` (`fit_model`과 같은 조건이다) | `test_FR16_validation_log_loss_rejects_invalid_input` |

## 6. 결정

### 보정

- 보정 방법 `temperature`는 우도 온도다. 시간 칸마다 기기 앞 상태들과 "어느 기기 앞도 아님" 상태의 증거 로그우도를 τ로 나눈 뒤 HMM에 넣는다 — 담당자가 골랐다. 공개 데이터 점검에서 HMM이 같은 위치 오차를 칸마다 새 증거로 쌓아 과신했고, 우도 온도는 그 과신을 직접 줄였다(ECE 0.30 → 0.10). 사후확률 온도는 HMM의 동작을 바꾸지 못한다.
- 모델 파일의 `calibration`은 `{"method": "none"}` 또는 `{"method": "temperature", "tau": τ}`다. τ > 0이다. 방법마다 키를 고정하고 모르는 키를 거부한다 — 지금 읽는 규칙과 같다.
- τ = 1인 `temperature`는 `none`과 같은 결과를 낸다.
- `ProximityModel`에 `calibration_tau`를 더한다. `none`일 때는 1.0이다 — 계산 식 하나로 두 방법을 다룬다.
- `ProximityModel`은 만들 때 방법과 τ가 맞는지 검사한다 — 계산은 τ만 읽고 모델 파일은 방법을 쓴다. `replace`로 방법만 바꾼 모델(예: 단계별 제거 실험)이 파일과 다른 계산을 하지 않는다.

### 적합

- 적합 코드는 `core/proximity/`의 순수 함수다. 입력은 메모리의 회차 목록이고 DB를 읽지 않는다 — 담당자가 골랐다. 회차 조회 접점이 미결이라, 정해지면 `tools/evaluation`이 이 함수를 부른다.
- 앵커별로 `A`와 `n`을 최소제곱으로 적합한다. 표본은 학습의 기기 앞 회차에서 그 앵커가 받은 프레임이고, 거리는 앵커와 라벨 기기의 앞 위치 사이다. `min_distance_m`로 아래를 자른다 — 근접 추정기의 관측 모델 식과 같다. 공개 데이터에서 앵커 높이에 따라 `n`이 0.8~2.4로 달랐다.
- `sigma`는 그 적합의 잔차 표준편차다. 자유도는 표본 수 − 2다.
- `none_rssi`와 `none_sigma`는 학습의 `none` 회차에서 그 앵커가 받은 RSSI의 평균과 표준편차다. 자유도는 표본 수 − 1이다.
- 회차의 프레임은 근접 추정기와 같은 규칙으로 고른다. 비콘이 같고, 앵커가 배치에 있고, RSSI가 유한하고, `started_at <= frame_ts < ended_at`이다. 같은 `(node_id, session_id, seq, beacon)`은 정렬 순서로 처음 하나만 쓴다.
- 기기 배치(앵커 위치, 앞 위치, `none_points`)는 입력으로 받는다. 적합하지 않는다.
- 최소제곱과 표준편차는 `math.fsum`의 닫힌 식으로 계산한다 — 같은 입력에서 같은 바이트의 모델 파일이 나온다.
- 적합 코드에서 회차의 프레임을 `received_at`으로 거르지 않는다. 평가 때는 `compute_posteriors`가 판정과 같은 규칙으로 늦은 프레임을 뺀다.
- 적합한 모델은 항상 `calibration.method`가 `temperature`다. τ = 1을 골라도 그렇다 — 고른 값이 파일에 그대로 남는다.

### 선택

- 검증 회차로 τ, `stay`, `window_sec`의 모든 조합을 잰다. 기준은 정답 상태의 평균 로그 손실 −log P(정답)이다 — 담당자가 골랐다. 적절한 점수 규칙이라 정확도와 보정을 함께 본다.
- 평가 시각은 회차마다 `started_at + window_sec`부터 `rssi_frame_sec` 간격으로 `ended_at` 이하까지다 — 관측 구간이 회차 안에 들어간다.
- 평가 시각마다 `compute_posteriors`에 배치의 모든 기기를 `trusted` 후보로 넘긴다. 이 `NodeInfo`의 기능, 점유 방식, 기기 상태는 자리 채움 값이다 — 근접 추정은 `node_id`, `place_id`, `trust_state`만 읽는다. "어느 기기 앞도 아님"의 확률은 1 − (후보 확률의 합)이다. 확률이 0 이하이면 그 시각의 손실은 무한대다.
- 손실은 회차마다 평균한 뒤 회차끼리 평균한다 — 회차가 독립 단위다.
- 손실이 같으면 적합 설정의 후보 목록 순서(τ, `stay`, `window_sec` 순)로 앞선 조합을 고른다.
- 모델 파일의 `validation`에 기준 이름, 고른 조합의 손실, 조합마다의 손실, 학습·검증 회차 ID를 쓴다 — 근접 추정은 이 값을 읽지 않는다 (#40). 무한대 손실은 `null`로 쓴다 — 표준 JSON에는 무한대가 없다.
- 모든 조합의 손실이 무한대이면 `FitError`다 — 고를 근거가 없다.

### 파일 쓰기

- 공개 함수는 `run_frames`(회차 프레임 선택), `fit_observation_params`(관측 값 적합), `validation_log_loss`(검증 손실), `fit_model`(적합과 선택), `model_to_dict`, `write_model_file`이다 — 회차 조회가 정해지면 `tools/evaluation`이 손실 함수를 시험셋 평가에도 쓸 수 있다.
- 공개 자료형은 입력 `LabeledRun`(회차), `PlaceLayout`·`AnchorLayout`(배치), `FitConfig`(적합 설정), 출력 `ObservationParams`(앵커별 적합 값), `GridLoss`(조합별 손실), `FitResult`, 상수 `NONE_LABEL`(`"none"`), 예외 `FitError`(`ValueError`)다.
- 검사 순서는 설정 → 회차(ID 중복, 검증 라벨, 빈 검증, 검증 길이) → 적합이다. 앵커 하나 안에서는 앞 표본 → `n`·`sigma` → none 표본 → `none_sigma`다 — 잘못된 입력에서 계산을 시작하지 않고, 오류 메시지가 입력마다 하나로 정해진다.
- 모델 파일 JSON의 키 순서는 `place_id`, `model_version`, `calib_version`, `window_sec`, `transition`, `min_distance_m`, `calibration`, `nodes`(node_id 순), `none_points`, `validation`이다. 들여쓰기 2칸이다.
- 모델 파일은 새로 만들기 모드로 연다. 디렉터리는 없으면 만든다 — 같은 버전을 덮어쓰지 않는다 (#40).
- JSON은 키 순서를 고정하고 UTF-8로 쓴다 — 같은 입력에서 같은 바이트가 나오고 diff로 검토할 수 있다.

## 7. 가정

- 보정 방법은 우도 온도 τ 하나다 — 미결 (FR-16 보정 방법). 다른 방법이 정해져도 `core/proximity/`와 모델 파일만 바뀐다.
- 관측 모델(경로손실 정규분포), 결측 처리, HMM 전이는 근접 추정기 spec 7절의 가정을 그대로 쓴다 — 미결 (FR-16).
- 회차 입력은 회차 ID, 라벨, 비콘, `started_at`, `ended_at`, 프레임 목록이다 — 미결 (수집 회차의 조회 접점, `architecture.md` §4). 조회 접점이 다른 모양으로 정해지면 부르는 쪽이 이 입력으로 바꾼다.

## 8. 테스트

| 요구사항 | 테스트 | 확인하는 것 |
|---|---|---|
| FR-16 | `test_FR16_temperature_model_file_is_read` | `temperature`와 `tau`를 읽는다. `none`의 τ는 1.0이다 |
| FR-16 | `test_FR16_temperature_one_equals_none` | τ = 1의 결과가 `none`과 같다 |
| FR-16 | `test_FR16_temperature_divides_evidence_log_likelihood` | 사후확률이 τ로 나눈 증거로 직접 계산한 forward와 같다 |
| FR-16 | `test_FR16_temperature_tempers_evidence` | τ가 크면 같은 관측의 사후확률이 처음 분포에 가깝다. 최대 기기는 바뀌지 않는다 |
| FR-16 | `test_FR16_fit_recovers_path_loss` | 알려진 값으로 만든 합성 회차에서 `A`, `n`, `sigma`를 허용 오차 안으로 되찾는다 |
| FR-16 | `test_FR16_fit_none_params_from_none_runs` | `none_rssi`, `none_sigma`가 `none` 회차의 평균과 표준편차다 |
| FR-16 | `test_FR16_select_minimizes_validation_log_loss` | 고른 조합의 손실이 모든 조합 중 가장 작다. 회차별 위치 오차가 있는 합성 데이터에서 τ > 1을 고른다 |
| FR-16 | `test_FR16_tie_breaks_by_grid_order` | 손실이 같으면 목록에서 앞선 조합을 고른다 |
| FR-16 | 5절 표의 `test_FR16_*` | 예외 상황 |
| FR-22 | `test_FR22_fit_is_deterministic` | 같은 입력에서 같은 모델과 같은 파일 바이트가 나온다 |
| FR-22 | `test_FR22_written_model_file_reloads_same` | 쓴 파일을 `load_model`로 읽은 모델이 적합 결과와 같다 |

- 합성 회차는 시드를 고정한 생성기로 만든다. 값은 테스트용이고 운영 값이 아니다.
- 공개 데이터의 숫자를 테스트의 기대값이나 모델 파일 값으로 쓰지 않는다.

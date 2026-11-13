# 거리 순서 평가 — 좌표표, 지표 10, 경계 확률 설정 추정과 기준 확률 고르기

- 상태: 완료
- 담당: dnnals (DB, 근접 추정의 적합 설정 읽기)
- 요구사항: FR-09, FR-16, NFR-13 (지표 10)
- 따르는 결정: #279 (decision PR #281), #278 (decision PR #280)

## 1. 왜

- 지표 10(거리 순서)은 좌표표로 정답 순서를 정한다. 그런데 좌표표를 읽어 지표를 내는 도구가 없다 (requirements.md §4, #279).
- 경계 확률 설정 `order_frame_sigma_db`, `order_static_sigma_db`, `order_boundary_min_prob`는 실측으로 정한다. 지금은 가정 값이고, 실측 회차에서 이 값을 구하는 도구가 없다 (requirements.md §1.2).
- 수집하기 전에 좌표표 형식을 정해야 현장에서 무엇을 적을지 알 수 있다.

## 2. 무엇

- 입력:
  - 분할 결과 파일 (`nearpilot split`의 결과)
  - 적합 설정 TOML (`config/fit/`): `[acceptance] max_boundary_error` (목표 오류 비율)
  - 좌표표 TOML (`server/config/layouts/<이름>.toml`): 장소, 기기 좌표, 서는 위치 좌표
  - 수집 회차 기록 파일 (`var/records/*.jsonl`, `collect run`이 쓴다): 회차 ID와 `spot`
  - 서버 설정의 DB (읽기 전용): 수집 회차와 RSSI 관측
- 출력: `var/reports/`의 보고서 CSV·Markdown(기존 `write_report` 형식), 원자료 JSON, 그래프 PNG
- 바뀌는 상태: 없음. DB는 읽기 전용으로만 연다.
- 약속:
  - `nearpilot evaluate-order --split FILE --layout NAME`이 학습 회차로 σf와 σs를 추정하고, 검증 회차로 기준 확률을 고르고, 시험 회차로 지표 10을 잰다.
  - 지표 10은 서버와 같은 방법(최근 구간의 RSSI 평균)으로 세운 거리 순서를 좌표표의 정답 순서와 비교한다.
  - 기준 확률은 "통과한 경계 중 순서가 틀린 비율이 `max_boundary_error` 이하인 후보 중 가장 낮은 값"으로 고른다. 만족하는 후보가 없으면 "고르지 못함"이다.
  - 고른 기준 확률로 시험 회차의 통과 비율과 통과 중 오류 비율을 보고한다. 시험 회차의 오류 비율을 `max_boundary_error`와 비교해 통과 칸에 쓴다.
  - 보고서와 함께 후보별 통과 비율·오류 비율 곡선 그래프를 그린다.

## 3. 제약

- 좌표표는 평가 도구만 읽는다. 서버, 공통 모델 파일, 수집 도구는 좌표를 받지 않는다 (requirements.md §4, #252, #279).
- 좌표표의 키는 수집 계획의 `spot`이다 (requirements.md §4).
- 같은 수집 회차의 프레임은 서로 다른 분할에 섞지 않는다. 회차 단위 분할은 분할 결과 파일을 그대로 쓴다 (requirements.md §4).
- 설정 값은 시험 회차를 보기 전에 정한다. σ는 학습 회차, 기준 확률은 검증 회차로 정하고, 시험 회차는 지표에만 쓴다 (requirements.md §4 "임계값은 검증셋에서만 고른다").
- 거리 순서는 서버의 `distance_order`와 같은 규칙이다: 최근 구간(`anchor_recent_sec`) 안에 서버가 받은 그 비콘의 유한한 RSSI 평균, 큰 값부터, 같으면 기기 ID 순서, 비콘을 보지 못한 기기는 끝 (FR-09, FR-16).
- 경계 확률은 판정의 `boundary_probability`를 그대로 쓴다 (FR-09).
- 지표 10은 거리 차이가 1 m 이상인 기기 쌍만 센다 (requirements.md §4).
- 평가 기준(목표 오류 비율)은 적합 설정 파일에서 받는다. 코드 상수로 쓰지 않는다 (AGENTS.md). 수치는 미결이다 (#279).
- 같은 입력에는 같은 결과가 나온다. 현재 시각과 무작위를 쓰지 않는다 (보고서 이름의 날짜와 보고서 머리의 `generated_at`은 제외).

## 4. 경계

### 고치는 곳

- `server/tools/evaluation/` — 좌표표·기록 파일 읽기, 거리 순서 평가, σ 추정, 보고서 행
- `server/src/nearpilot/cli/` — `evaluate-order` 명령 등록 (`evaluate.py`), 기본 폴더에 `layouts` 추가 (`common.py`)
- `server/src/nearpilot/core/proximity/files.py` — 적합 설정 `[acceptance]`의 `max_boundary_error` 읽기
- `server/config/fit/example.toml` — `max_boundary_error` 예시 값
- `server/config/layouts/` — 좌표표 예시 (새 폴더)
- `server/config/README.md` — 좌표표와 `evaluate-order` 사용법
- `server/tests/tools/`, `server/tests/cli/`
- `server/tests/core/proximity/test_files.py` — `max_boundary_error` 읽기 시험
- 적합 설정 TOML이나 `AcceptanceSettings(...)`를 직접 만드는 시험 (`server/tests/core/proximity/`, `server/tests/integration/`) — 새 필수 키만 더한다
- `specs/core-ops-cli/spec.md` — `[acceptance]`의 새 키를 적는 한 문장 (담당자가 2026-11-13에 이 PR에서 고치기로 정했다)
- `docs/architecture.md` — `evaluate-order`, `config/layouts/`, 지표 10 (담당자가 2026-11-13에 이 PR에서 고치기로 정했다)

### 고치지 않는 곳

- 항상: `docs/` (위의 `docs/architecture.md`는 제외), `AGENTS.md` (Gotchas 절은 제외), `server/src/nearpilot/shared/`, `server/pyproject.toml`, `.github/`
- 이 작업: `server/src/nearpilot/`의 `cli/`와 `core/proximity/files.py` 밖, `server/tools/data_collection/`, DB 스키마

### 다른 영역과 닿는 곳

| 계약 | 사용 또는 제공 | 상대 영역 |
|---|---|---|
| `tools/evaluation/runs.py`의 `load_labeled_runs` | 사용 | DB (같은 영역) |
| `tools/evaluation/split.py`의 `load_split` | 사용 | DB (같은 영역) |
| `core/proximity/common_fit.py`의 `LabeledRun`, `place_anchors`, `place_frames` | 사용 | 근접 추정 |
| `core/decision/pipeline.py`의 `boundary_probability` | 사용 | 판정 |
| `collect run`의 회차 기록 파일 형식 (`collection_id`, `spot`) | 사용 | 근접 추정 (`tools/data_collection`) |

### 만들지 않는 것

- 장소를 바꾼 시험(한 장소로 정하고 다른 장소로 시험). 분할 결과 하나만 받는다.
- 거리 순서에 자동 영점을 넣는 비교. 지금 서버의 거리 순서는 영점을 쓰지 않는다.
- "비슷함" 기준 값(#255)을 정하는 평가. requirements.md §4는 같은 데이터로 정한다고 하지만, 이 작업은 경계 확률 설정만 다룬다.

## 5. 예외 상황

| 상황 | 기대 동작 | 테스트 |
|---|---|---|
| 좌표표 파일이 없다 | `좌표표 파일이 없다: …`와 종료 코드 2 (`--split`·`--fit`과 같은 문구) | `test_FR09_evaluate_order_input_errors` |
| 좌표표가 UTF-8 TOML이 아니거나 형식이 틀렸다 (빠진 키, 모르는 키, 빈 `place_id`, 빈 `[nodes]`·`[spots]`, 좌표가 수 두 개가 아님, 유한하지 않음) | `좌표표 오류: …`와 종료 코드 2. 틀린 필드 경로를 쓴다 | `test_FR09_layout_rejects_bad_file`, `test_FR09_layout_rejects_unreadable_file` |
| 좌표표의 장소가 분할 결과의 장소와 다르다 | 종료 코드 2 | `test_FR09_evaluate_order_rejects_place_mismatch` |
| 분할의 회차가 기록 파일에 없다 (spot을 모른다) | `기록 파일에 회차 …가 없다`와 종료 코드 2 | `test_FR09_records_missing_run_rejected` |
| 같은 회차가 기록 파일 여러 곳에 다른 spot으로 있다 | 종료 코드 2 | `test_FR09_records_conflicting_spot_rejected` |
| 기록 파일의 줄이 JSON 객체가 아니거나 `collection_id`·`spot`이 없거나 빈 문자열이다 | `기록 파일 오류: 파일:줄: …`와 종료 코드 2 | `test_FR09_records_reject_bad_lines` |
| `--records`로 준 파일이 없다 | `기록 파일이 없다: …`와 종료 코드 2 | `test_FR09_evaluate_order_input_errors` |
| 회차의 spot이나 회차에 나온 기기가 좌표표에 없다 | 종료 코드 2. 없는 이름을 모두 쓴다 | `test_FR09_layout_missing_names_rejected` |
| 학습 회차에 1 m 이상 차이 나는 기기 쌍이 없어 σs를 추정할 수 없다 | `평가 실패: …`와 종료 코드 1 | `test_FR09_estimate_static_sigma_needs_pairs` (함수의 실패), `test_FR09_evaluate_order_fails_when_sigma_cannot_be_estimated` (같은 처리 경로의 메시지와 종료 코드) |
| σs 추정값이 탐색 범위의 끝이다 (모든 쌍을 맞혔거나 거의 다 틀렸다) | 계산은 하고, 보고서 note에 "탐색 범위 끝"을 쓴다 | `test_FR09_estimate_static_sigma_edge_noted` |
| 시험 회차에 평가 시점이 없다 | `평가 실패: …`와 종료 코드 1 | `test_FR09_evaluate_order_without_test_samples_fails` |
| 학습 회차로 σf를 구할 수 없거나(구간·기기마다 프레임이 2개 이상인 곳이 없다) σf가 0이다 | `평가 실패: …`와 종료 코드 1 | `test_FR09_evaluate_order_fails_when_frame_sigma_is_zero` |
| DB 파일이 없거나 스키마 버전이 다르다 | 종료 코드 2 (`evaluate-reask`와 같은 `open_read_db`) | `test_FR09_evaluate_order_input_errors` (DB 없음). 스키마 버전은 `open_read_db`의 공용 시험이 본다 |
| 분할의 회차를 DB에서 읽을 수 없다 (DB에 없는 회차) | 종료 코드 1 (`evaluate-reask`와 같다) | `test_FR09_evaluate_order_unreadable_runs_fail` |
| `--records`를 생략했는데 `var/records/`에 `*.jsonl`이 없다 | `기록 파일이 없다: …`와 종료 코드 2 | `test_FR09_evaluate_order_input_errors` |
| 적합 설정에 `max_boundary_error`가 없거나 0~1 밖이다 | `적합 설정 파일 오류: …`와 종료 코드 2 | `test_FR09_fit_file_reads_max_boundary_error` |
| 검증 회차에서 목표 오류 비율 이하인 후보가 없다 | 기준 확률은 "고르지 못함", 시험 행은 비우고 note에 이유를 쓴다. 종료 코드 0 | `test_FR09_choose_threshold_none_satisfies`, `test_FR09_order_rows_chosen_none_and_edge_notes`, `test_FR09_evaluate_order_without_chosen_threshold_still_exits_0` |
| matplotlib이 없다 | 기존 그래프처럼 경고만 하고 그래프를 건너뛴다. 보고서와 원자료는 쓴다 | `test_FR09_order_plot_skipped_without_matplotlib` |
| 그래프를 그리다 예외가 난다 | stderr에 경고하고 종료 코드 0. 보고서는 이미 썼다 | `test_FR09_evaluate_order_plot_failure_is_only_a_warning` |
| 보고서가 이미 있다 | `--force` 없이는 종료 코드 2 | `test_FR09_evaluate_order_refuses_existing_report` |
| 두 기기 모두 비콘을 보지 못한 쌍 | 순서가 없으므로 지표와 추정에서 뺀다 | `test_FR09_order_pairs_skip_both_unheard` |
| 한 기기만 비콘을 보지 못한 쌍 | 지표에는 센다(못 본 기기가 뒤). 경계 확률은 1이라 σs 추정에서는 뺀다 | `test_FR09_order_pairs_one_unheard` |

## 6. 결정

1. σs는 학습 회차 시점마다 거리 차이가 1 m 이상인 모든 기기 쌍(이웃한 쌍만이 아니다)으로, 경계 확률 식이 "앞선 기기가 실제로 더 가까웠나"를 가장 잘 맞히는 값(가능도 최대)으로 정한다 — "확률 0.9"가 실제로 열에 아홉 맞게 하려는 것이다. 거리-신호 곡선을 가정하지 않는다. 담당자가 2026-11-13에 정했다.
2. σf는 학습 회차에서, 겹치지 않는 `anchor_recent_sec` 구간마다 기기별 프레임이 구간 평균에서 벗어난 정도를 모은 표준편차다 (자유도 보정). 판정이 쓰는 "프레임 하나의 RSSI 표준편차"와 같은 뜻이다.
3. σ는 학습 회차, 기준 확률은 검증 회차, 지표 10은 시험 회차로 정한다 — 정하는 데 쓴 데이터로 채점하지 않는다. 담당자가 2026-11-13에 정했다.
4. 도구가 기준 확률을 고른다. 후보는 0.50부터 0.99까지 0.01 간격이다 — 후보 목록은 고르는 방법의 일부라 설정으로 두지 않는다. 판정이 쓰는 값 `order_boundary_min_prob`는 서버 설정에 둔다. 검증 회차에서 "통과 중 오류 비율 ≤ `max_boundary_error`"를 만족하는 후보 중 가장 낮은 값(대답을 가장 많이 하는 값)을 고른다. 통과한 경계가 하나도 없는 후보는 만족하지 않는 것으로 본다. 담당자가 2026-11-13에 정했다.
5. 기기 종류와 상관없이 근접 참여 기기의 모든 쌍을 비교한다 — 신호와 거리의 관계는 종류와 무관하고, 종류별로 나누면 쌍이 모자란다. 담당자가 2026-11-13에 정했다.
6. 평가 시점은 회차 시작부터 `anchor_recent_sec`가 지난 뒤 `rssi_frame_sec`마다다. 시점마다 서버처럼 최근 구간 평균으로 거리 순서를 세운다.
7. 근접 참여 기기는 장소의 앵커(`place_anchors`)로 근사한다 — 평가에는 rssi 보고 시각이 없다. 학습·검증 회차에는 학습+검증 회차의 앵커(`fit`과 같다), 시험 회차에는 시험 회차의 앵커(`eval`과 같다)를 쓴다. 시험 회차에만 나온 기기가 학습·검증 시점에 "못 본 기기"로 끼면 고르는 값이 시험 회차에 따라 바뀐다. 좌표표에는 두 앵커의 합집합이 모두 있어야 한다.
8. 좌표표는 TOML `server/config/layouts/<이름>.toml`이다. 최상위 `place_id`, 표 `[nodes]`(기기 ID → `[x, y]`), `[spots]`(spot → `[x, y]`). 단위는 m. 모르는 키는 거부한다. 담당자가 2026-11-13에 정했다.
9. spot은 회차 기록 파일에서 읽는다 — DB의 수집 회차에는 spot이 없고, 수집 도구와 DB를 고치지 않는다. `--records`를 생략하면 `var/records/*.jsonl`을 모두(이름 순) 읽는다. `--records`·`--layout`을 이름으로 주면 각각 `var/records/`, `config/layouts/`에서 찾고, `/`나 `\`가 있으면 실행 폴더 기준 경로다 (`--split`과 같은 규칙). 기록 파일의 `collection_id`·`spot`은 비어 있지 않은 문자열이어야 한다.
10. 정답 순서는 서는 위치와 기기 좌표의 유클리드 거리다. 거리 차이가 1 m 미만인 쌍은 지표와 σs 추정에서 모두 뺀다.
11. 경계 확률 표는 시점마다 관측 순서에서 이웃한 쌍(거리 선택자의 경계가 될 수 있는 쌍)으로 센다. "틀림"은 앞선 기기가 실제로 더 먼 경우다.
12. σs 탐색은 0 dB부터 15 dB까지 0.05 dB 간격의 격자다 — 결과가 결정적이다. 탐색 범위는 측정 방법의 일부라 설정으로 두지 않는다.
13. 명령 형식은 `nearpilot evaluate-order --split FILE --layout NAME [--fit FILE] [--records PATH ...] [--out NAME] [--force]`다. `--fit`을 생략하면 `split`·`eval`처럼 `<장소>.toml`을 읽는다. 입력 오류와 이미 있는 보고서는 종료 코드 2, 계산 실패와 DB에서 회차를 읽지 못한 경우는 1이다 (`evaluate-reask`와 같다). σf가 0이면 경계 확률을 계산할 수 없어 계산 실패다.
14. 보고서 행의 `metric` 이름: `m10_frame_sigma_db`, `m10_static_sigma_db`, `m10_pair_order_agreement`, `m10_boundary_min_prob`(고른 기준 확률), `m10_boundary_pass_rate`, `m10_boundary_error_rate`(시험 회차). `m10_boundary_error_rate`만 `max_boundary_error`와 비교해 통과 칸을 쓴다(목표와 같으면 통과). 나머지는 비운다 — 순서 일치율의 기준은 미결이다 (#279). 값을 낼 수 없으면 값을 비우고 note를 쓴다: 채점한 쌍이 없으면 "채점한 쌍 없음", 시험 경계가 없으면 "채점한 경계 없음", 통과한 경계가 없으면 "통과한 경계 없음". 행마다 `place_id`, `split_file`, `layout_file`을 더한다 (`evaluate-reask`의 출처 열처럼).
15. 목표 오류 비율은 적합 설정 `[acceptance] max_boundary_error`(0 이상 1 이하)다 — 평가의 통과 기준을 한 파일에 둔다. 적합 설정 로더는 선택 키를 두지 않으므로 필수 키다. 담당자가 2026-11-13에 정했다.
16. 그래프는 `{이름}-order.png` 한 장이다. 가로축은 기준 확률 후보, 세로축은 비율이고, 검증·시험의 통과 비율과 오류 비율 네 곡선, 목표선, 고른 값을 그린다. 기존 그래프처럼 matplotlib이 없으면 건너뛰고, 제목과 축 이름은 영어다. 담당자가 2026-11-13에 정했다.
17. 원자료 JSON은 후보마다 검증·시험의 통과 비율과 오류 비율, 경계 수를 모두 담는다.
18. 모듈은 `tools/evaluation/layout.py`(좌표표·기록 파일 읽기), `tools/evaluation/order.py`(시점별 순서, 쌍 채점, σ 추정, 기준 확률 고르기, 보고서 행)이고, 그래프는 `plots.py`의 `write_order_plot`이다. 거리 순서의 창과 σf의 구간은 서버처럼 서버 수신 시각(`received_at`) 기준이다.
19. 좌표표 기본 폴더는 CLI 기본 폴더(`Dirs`)의 `layouts`(`config/layouts/`)다. #269가 넣은 "공통 모델은 배치 파일을 받지 않는다" 단언은 이 폴더가 평가 전용 좌표표가 되면서 폴더 위치 단언으로 바꿨다. 서버와 공통 모델은 여전히 좌표를 읽지 않는다.
20. 검증·시험 두 표가 모두 비어 그릴 것이 없으면 그래프를 경고 없이 건너뛴다 (기존 `write_plots`와 같다). 보고서 CSV의 `sample_count`는 시험 회차의 평가 시점 수다.

## 7. 가정

- 목표 오류 비율의 수치는 미결이다. 예시 설정의 초기값은 0.05다 (지표 8의 정면 오선택 상한과 같은 값) — #279
- 지표 10의 통과 조건은 미결이다 (requirements.md §4). 이 작업은 "시험 회차의 통과한 경계 중 순서가 틀린 비율 ≤ `max_boundary_error`"를 통과로 가정하고 `m10_boundary_error_rate` 행에만 통과 칸을 쓴다. 팀이 다른 조건으로 정하면 `order_rows`의 통과 칸만 바꾼다 — #279
- 순서 일치율의 통과 조건은 미결이다. 보고서는 값만 쓴다 — #279
- 수집 회차 기록 파일의 형식(`collection_id`, `spot` 키)은 `specs/core-proximity-data-collection`이 정한 대로다.

## 8. 테스트

| 요구사항 | 테스트 | 확인하는 것 |
|---|---|---|
| FR-09 | `test_FR09_layout_loads_nodes_and_spots` | 좌표표를 읽어 장소, 기기·서는 위치 좌표를 돌려준다 |
| FR-09 | `test_FR09_layout_rejects_bad_file` | 빠진 키, 모르는 키, 빈 값, 좌표 형식 오류를 필드 경로와 함께 거부 |
| FR-09 | `test_FR09_layout_rejects_unreadable_file` | 없는 파일, TOML 아님, UTF-8 아님을 거부 |
| FR-09 | `test_FR09_records_reject_bad_lines` | JSON 객체가 아닌 줄, 키가 없거나 빈 문자열인 줄을 파일:줄과 함께 거부 |
| FR-09 | `test_FR09_records_map_run_to_spot` | 기록 파일 여러 개에서 회차 ID → spot을 모은다 |
| FR-09 | `test_FR09_records_missing_run_rejected` | 분할의 회차가 기록에 없으면 거부 |
| FR-09 | `test_FR09_records_conflicting_spot_rejected` | 같은 회차의 spot이 다르면 거부 |
| FR-09 | `test_FR09_layout_missing_names_rejected` | 좌표표에 없는 spot·기기를 모두 알린다 |
| FR-16 | `test_FR16_order_points_tie_uses_node_id` | 정렬 값이 같으면 받은 순서와 상관없이 기기 ID 순서다 |
| FR-16 | `test_FR16_order_at_matches_server_rule` | 평가의 거리 순서가 서버 `distance_order`와 같은 결과다 (구간, 비콘, 유한값, 같은 값은 기기 ID, 못 본 기기는 끝, 프레임 수) |
| FR-09 | `test_FR09_order_pairs_skip_both_unheard` | 둘 다 못 본 쌍은 뺀다 |
| FR-09 | `test_FR09_order_pairs_one_unheard` | 한쪽만 못 본 쌍은 지표에 세고 σs 추정에서 뺀다 |
| FR-09 | `test_FR09_order_pairs_skip_under_one_meter` | 거리 차이 1 m 미만 쌍은 뺀다 |
| FR-09 | `test_FR09_estimate_frame_sigma` | 알려진 표준편차로 만든 합성 프레임에서 σf를 되찾는다 |
| FR-09 | `test_FR09_estimate_static_sigma_recovers_truth` | 알려진 σs로 만든 합성 쌍에서 σs를 되찾는다 |
| FR-09 | `test_FR09_estimate_static_sigma_needs_pairs` | 쌍이 없으면 실패 |
| FR-09 | `test_FR09_estimate_static_sigma_edge_noted` | 탐색 범위 끝이면 표시한다 |
| FR-09 | `test_FR09_pair_agreement_and_boundary_table` | 손으로 만든 시점들에서 순서 일치율, 후보별 통과·오류 비율 |
| FR-09 | `test_FR09_choose_threshold_lowest_within_target` | 목표 이하인 후보 중 가장 낮은 값을 고른다. 통과한 경계가 없는 후보는 고르지 않는다 |
| FR-09 | `test_FR09_choose_threshold_none_satisfies` | 만족하는 후보가 없으면 고르지 못함 |
| FR-09 | `test_FR09_fit_file_reads_max_boundary_error` | 적합 설정이 `max_boundary_error`를 읽고, 없거나 범위 밖이면 거부 |
| FR-09 | `test_FR09_order_plot_written` | 그래프 파일을 쓴다 |
| FR-09 | `test_FR09_order_plot_skipped_without_matplotlib` | matplotlib이 없으면 건너뛴다 |
| FR-09 | `test_FR09_order_rows` | 보고서 행 이름, 오류 비율 행만 통과 칸을 쓰고 나머지는 비어 있음. 오류 비율이 목표와 같으면 통과 |
| FR-09 | `test_FR09_evaluate_order_writes_report` | 명령이 학습으로 추정하고 검증으로 고르고 시험으로 재서 보고서·원자료·그래프를 쓴다. 시험 회차는 정하는 데 쓰지 않는다 |
| FR-09 | `test_FR09_evaluate_order_test_only_device_does_not_change_choice` | 시험 회차에만 나온 기기가 σ와 고른 기준 확률을 바꾸지 않는다 |
| FR-09 | `test_FR09_evaluate_order_rejects_place_mismatch` | 장소가 다르면 종료 코드 2 |
| FR-09 | `test_FR09_evaluate_order_without_test_samples_fails` | 시험 시점이 없으면 종료 코드 1 |
| FR-09 | `test_FR09_evaluate_order_refuses_existing_report` | 이미 있는 보고서는 `--force` 없이 거부 |
| FR-09 | `test_FR09_evaluate_order_input_errors` | 기록 파일에 없는 회차, 좌표표에 없는 이름, 기록 파일 없음은 명령에서 종료 코드 2 |
| FR-09 | `test_FR09_evaluate_order_fails_when_frame_sigma_is_zero` | σf가 0이면 종료 코드 1 |
| FR-09 | `test_FR09_evaluate_order_unreadable_runs_fail` | DB에서 회차를 읽지 못하면 종료 코드 1 |
| FR-09 | `test_FR09_evaluate_order_plot_failure_is_only_a_warning` | 그래프 예외는 경고만 하고 종료 코드 0 |
| FR-09 | `test_FR09_layout_example_is_valid` | `config/layouts/` 예시가 읽힌다 |

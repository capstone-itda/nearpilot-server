# 서버 지표 평가 — 시뮬레이터 하네스, 지표 1·2·3·4·5·6·7·9, 부하 생성기, 그래프

- 담당: dnnals (DB)
- 요구사항: NFR-13, NFR-01, NFR-03, NFR-04, NFR-05, NFR-06, NFR-07, FR-07, FR-20, FR-23
- 따르는 결정: 2026-10-26 담당자 결정 D8 (임계값과 부하 클라이언트 수는 측정 뒤 정한다)

## 1. 왜

- NFR-13은 평가 표와 그래프를 스크립트로 만들라고 한다. 지금 스크립트가 있는 지표는 8(근접 모델)뿐이다.
- 지표 3·5·6·7·9는 실제 기기 없이 서버와 시뮬레이터로 잴 수 있는데, 잴 도구가 없다. 지표 3은 부하 생성기가 필요하다 (`requirements.md` §4).
- 지표 1·4는 실기기 측정이 기준이다. 실기기 전에 같은 절차를 시뮬레이터로 돌려 볼 도구가 없다. 지표 2는 수집 데이터로 곡선을 그릴 도구가 없다.
- 그래프가 없다. `db-evaluation-foundation`이 matplotlib 추가를 후속으로 미뤘다.

## 2. 무엇

- 입력: 평가 설정 TOML(`server/config/eval/server-metrics.example.toml` 형식), 지표 2는 분할 파일과 모델 파일
- 출력: `var/reports/`의 보고서 CSV·Markdown (기존 `write_report` 형식, 지표마다 행), 그래프 PNG, 원자료 JSON
- 바뀌는 상태: 평가가 만든 임시 폴더의 DB·브로커·시뮬레이터 상태뿐이다. 운영 DB를 열지 않는다.
- 약속:
  - `nearpilot evaluate-server <설정>`이 임시 폴더에 Mosquitto, `nearpilot serve`, 시뮬레이터 기기를 띄우고 지표를 잰 뒤 모두 끈다.
  - 지표 3은 동시 클라이언트 요청과 재전송을 보내고 이중 점유와 같은 요청의 중복 실행을 센다 (NFR-03).
  - 지표 5·6은 시나리오마다 정한 횟수를 돌려 범위 밖 조작, 자동 반납, 승인 없는 복구, 복구 성공률을 센다 (NFR-04, NFR-07).
  - 지표 7은 서버 판정 지연의 median·p95를 잰다. 호스트 쪽(발화→안내, 3종 일치)은 행만 두고 미측정으로 쓴다 (NFR-01, NFR-09).
  - 지표 9는 무작위 50세트에서 `allocate`의 배정을 완전 탐색 기준해와 비교한다 (FR-23).
  - 지표 1·4는 시뮬레이터 RSSI로 같은 절차를 돌린다. 보고서 note에 "시뮬레이터 측정"을 쓴다.
  - 지표 2는 시험 회차의 근접 확률로 비용 비율별 재질문률·오작동률 곡선을 만든다 (NFR-06).
  - 통과 기준은 설정 파일에서 읽는다. 코드 상수로 쓰지 않는다.
  - 실패 0건이면 95% 신뢰구간 상한을 적는다 (`write_report`가 한다).

## 3. 제약

- 운영 DB, 운영 브로커, 실제 기기에 접속하지 않는다. 포트는 비어 있는 localhost 포트를 고른다.
- `tools/`는 `tests/`를 import하지 않는다. 시드는 `nearpilot seed`로 한다.
- 서버를 고치지 않는다. 평가는 MCP·REST·MQTT 외부 계약과 읽기 전용 DB 연결로만 잰다 (NFR-07, NFR-13).
- 임계값과 시간 값은 설정에서 받는다 (AGENTS.md 코딩 규칙).
- 같은 시드와 설정이면 시나리오 순서와 무작위 입력이 같다. 시드는 보고서 머리말과 원자료에 적는다 (NFR-13).
- Mosquitto가 없으면 서버 지표 명령은 실행하지 않고 오류를 낸다. 테스트는 건너뛴다.
- matplotlib은 선택 의존성 `eval`로 더한다. 없으면 그래프만 건너뛰고 경고한다.

## 4. 경계

### 고치는 곳
- `server/tools/evaluation/`
- `server/src/nearpilot/cli/` (`evaluate-server`, `evaluate-reask` 명령 등록)
- `server/config/eval/`, `server/config/README.md`
- `server/pyproject.toml` (선택 의존성 `eval`)
- `server/tests/tools/`, `server/tests/cli/`

### 고치지 않는 곳
- 항상: `docs/`, `AGENTS.md` (Gotchas 절은 제외), `server/src/nearpilot/shared/`, `.github/`
- 이 작업: `server/src/nearpilot/`의 `cli/` 밖, `server/tools/node_sim/`

### 다른 영역과 닿는 곳
| 계약 | 사용 또는 제공 | 상대 영역 |
|---|---|---|
| MCP 도구 여섯 개 (§5 MCP) | 사용 | API |
| 계정·승인·복구 REST (§5 REST) | 사용 | API |
| `nearpilot serve`, `nearpilot seed` | 사용 | 상태 관리 |
| `tools.node_sim` (`SimNode`, `SimDevice`, `Scenario`) | 사용 | IOT |
| `core.allocation.allocator.allocate` | 사용 (지표 9) | 판정 |
| `tools.evaluation` 기존 모듈 (`report`, `proximity`, `split`, `runs`, `db`) | 사용 | DB |

### 만들지 않는 것
- 실기기 측정과 실제 RSSI 수집. 지표 1·2·4·8의 최종 값은 실기기 수집 뒤에 잰다.
- 지표 7의 호스트 쪽 측정(발화→안내 지연, 3종 호스트 일치). LLM 호스트를 돌려야 한다.
- 대표 4장면 50회 리허설 기록.
- 대시보드 문구의 새 지표 이름. 모르는 지표 이름은 대시보드가 그대로 보인다.

## 5. 예외 상황

| 상황 | 기대 동작 | 테스트 |
|---|---|---|
| Mosquitto 실행 파일이 없다 | 시작 전에 오류와 종료 코드 1. 아무 프로세스도 띄우지 않는다 | `test_NFR13_harness_requires_mosquitto` |
| 서버가 제한 시간 안에 뜨지 않는다 | 띄운 프로세스를 모두 끄고 서버 로그 끝을 오류에 담는다 | `test_NFR13_harness_startup_timeout_cleans_up` |
| 측정 중 예외가 났다 | 브로커, 서버, 시뮬레이터를 모두 끈다. 그때까지의 원자료를 남긴다 | `test_NFR13_harness_cleans_up_on_error` |
| 보고서가 이미 있다 | `--force` 없이는 쓰지 않는다 (기존 `write_report` 규칙) | `test_NFR13_evaluate_server_existing_report_requires_force` |
| 설정에 통과 기준이나 횟수가 없거나 틀렸다 | 서버를 띄우기 전에 종료 코드 2 | `test_NFR13_evaluate_server_rejects_bad_config` |
| SQLite 잠금 오류로 요청이 실패했다 | 지표 3에서 따로 센다. 이중 점유와 중복 실행 판정은 저장된 결과로 한다 | `test_NFR03_load_counts_lock_errors` |
| matplotlib이 없다 | 그래프를 건너뛰고 경고한다. 보고서는 쓴다 | `test_NFR13_plots_skip_without_matplotlib` |
| 지표 2의 시험 회차에 기기 라벨이 없다 | 곡선 행을 비우고 note에 이유를 쓴다 | `test_NFR06_reask_curve_without_labels` |

## 6. 결정

- 하네스는 실제 프로세스를 쓴다: 임시 Mosquitto, `nearpilot serve` 하위 프로세스, 같은 프로세스 안의 `SimDevice`. 시나리오는 시뮬레이터의 `Scenario`를 바꿔 만든다 — 담당자 위임 결정. 테스트 도우미(`tests/`)를 쓰지 않고 운영과 같은 경로를 잰다.
- 명령은 `nearpilot evaluate-server <설정> [--metrics 1,3,...] [--seed N] [--out NAME] [--force]`와 `nearpilot evaluate-reask <PLACE/VERSION> --split FILE [--out NAME] [--force]`다.
- 보고서 행의 `metric` 이름: `m1_wrong_device_rate`, `m3_double_occupancy`, `m3_duplicate_execution`, `m3_lock_errors`, `m4_cross_user_malfunction`, `m4_success_rate`, `m5_out_of_scope`, `m5_untrusted_candidates`, `m5_valid_approval_success`, `m6_auto_release`, `m6_foreign_release`, `m6_release_during_fault`, `m6_unapproved_recovery`, `m6_recovery_success`, `m7_server_latency_median`, `m7_server_latency_p95`, `m7_host_latency`, `m7_host_agreement`, `m9_allocation_match`, `m2_reask_curve`.
- 지표 7의 서버 판정 지연은 클라이언트가 MCP `request_use`를 보내고 응답을 받기까지의 시간이다. 같은 PC의 localhost라 네트워크 지연은 작다. note에 측정 방법을 쓴다.
- 지표 3의 이중 점유는 읽기 전용 DB 연결로 같은 기기의 `use_session` 구간이 겹치는지 본다. 중복 실행은 시뮬레이터의 명령 처리 기록에서 같은 `request_id`의 구동이 2회 이상인지 본다.
- 지표 9의 기준해는 순열 완전 탐색이다: 배정 수 최대, 그 안에서 비용 합 최소. `allocate` 결과와 배정 수, 비용 합(허용 오차 `COST_TOLERANCE`)이 같으면 일치다.
- 지표 2는 시험 회차 표본의 최대 확률 기기가 맞는지와 그 확률로, 비용 비율 r마다 확률 임계값 `r/(1+r)` 아래면 재질문으로 보고 재질문률과 오작동률을 낸다. 비율 목록은 설정에서 받는다.
- 그래프는 지표 7 지연 분포, 지표 2 곡선, 지표별 통과 막대 세 장이다.
- matplotlib을 선택 의존성 `eval`로 더한다 — 2026-10-26 담당자 위임 결정 (`db-evaluation-foundation` 4절의 후속).

## 7. 가정

- 지표 1·4의 시뮬레이터 RSSI는 실제 전파를 흉내 내지 않는다. 절차와 서버 경로만 확인한다. 실기기 수집 뒤 같은 명령에 실측 설정을 넣는다.
- 부하 클라이언트 수와 통과 기준 수치는 D8에 따라 측정 뒤 다시 정한다. 예시 설정은 `requirements.md` §4의 값을 쓴다.
- 지표 6의 복구는 `api-rest-recovery`의 복구 REST를 쓴다. 이 작업은 그 브랜치 위에서 한다.

## 8. 테스트

| 요구사항 | 테스트 | 확인하는 것 |
|---|---|---|
| NFR-13 | `test_NFR13_harness_*` | 하네스의 시작, 정리, 실패 처리 |
| NFR-13 | `test_NFR13_evaluate_server_*` | 명령 인자, 설정 검증, 보고서 쓰기 |
| NFR-03 | `test_NFR03_load_*` | 부하 생성, 이중 점유·중복 실행 판정, 잠금 오류 집계 |
| NFR-04, FR-07, FR-20 | `test_NFR04_metric6_*` | 지표 6 시나리오와 집계 |
| NFR-07 | `test_NFR07_metric5_*` | 지표 5 시나리오와 집계 |
| NFR-01 | `test_NFR01_metric7_*` | 지연 집계(median, p95)와 호스트 행 미측정 |
| FR-23 | `test_FR23_metric9_*` | 기준해와 비교, 불일치 검출 |
| NFR-05 | `test_NFR05_metric1_*`, `test_NFR05_metric4_*` | 시뮬레이터 절차와 집계 |
| NFR-06 | `test_NFR06_reask_*` | 곡선 계산과 빈 라벨 처리 |
| NFR-13 | `test_NFR13_plots_*` | 그래프 파일 생성과 matplotlib 없는 경우 |

- 집계 함수는 원자료를 넣어 단위 테스트한다. 실제 프로세스를 띄우는 테스트는 횟수를 줄인 설정으로 지표마다 하나만 둔다. Mosquitto가 없으면 건너뛴다.

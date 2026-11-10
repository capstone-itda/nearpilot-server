# 배치 없는 공통 근접 모델과 자동 영점

- 담당: dnnals (근접 추정. 이 작업에 필요한 shared, DB, 판정, runtime, CLI, 평가 도구 연결 포함)
- 요구사항: FR-05, FR-16, FR-17, FR-22, FR-23, NFR-02
- 따르는 결정: #252 (decision PR #257). 남은 미결은 #258.

## 1. 왜

- 근접 모델 파일은 장소마다 있고, 기기마다 좌표, 기기 앞 위치, 경로손실 값 `A`, `n`, `σ`를 담는다. 소유자가 기기를 등록하고 신뢰해도, 누군가 좌표를 재고 장소별 모델을 만들기 전에는 근접 지정을 쓸 수 없다.
- 모델 파일에 없는 `trusted` 기기가 장소에 하나라도 있으면 `posterior`가 `ModelMismatchError`로 실패한다. 기기 하나를 더 놓으면 그 장소의 근접 지정이 멈춘다.
- 기기마다 안테나와 케이스가 달라 같은 거리에서도 RSSI가 일정하게 다르다. 지금은 그 차이를 기기별 `A`로 흡수한다. 좌표 없이 공통 값을 쓰면 그 차이를 다른 방법으로 지워야 한다.
- 전체 상태는 장소의 모든 `trusted` 기기다. 보고를 멈춘 기기도 상태로 남는다.

## 2. 무엇

- 입력:
  - 공통 모델 파일 하나. 설정 `[proximity] model_version`이 고른다.
  - 신뢰 기기의 `rssi` 프레임과 보고(빈 보고 포함), 기기 선언의 `vendor`
  - DB에 저장한 자동 영점 개정
- 출력:
  - `ProximityResult.posteriors`는 근접 참여 후보만 담는다. `ProximityResult.unzeroed`는 그중 자동 영점이 없는 기기다.
  - `ProximityResult.calib_version`은 판정에 적용한 자동 영점의 개정 번호다.
  - 근접 지정은 사후확률 1등에 자동 영점이 없으면 `ASK`다. 전역 할당은 자동 영점이 없는 기기를 배정하지 않는다.
  - 합성 공통 모델 파일 `synthetic-2.json`. 시드를 고정한 생성기와 공통 적합 함수로 만든다.
- 바뀌는 상태:
  - DB 테이블 `auto_zero` (마이그레이션 `0013`). 행을 더하기만 한다.
  - 근접 추정의 자동 영점 표본과 기기별 자동 영점 (메모리)
- 약속:
  - 모델 파일에 장소 ID, 기기 ID, 좌표, 앵커별 파라미터가 없다. 소유자의 일은 기기 등록과 신뢰뿐이다.
  - 사후확률 출력, 근접 임계값, HMM, 우도 온도는 그대로다.
  - 합성 모델은 합성임을 파일 안에 표시한다. 실제 정확도를 검증했다고 쓰지 않는다.
  - 명시 지정과 거리 선택자의 판정은 이전과 같다.

## 3. 제약

- 전체 상태는 근접 참여 기기 앞 상태들과 "어느 기기 앞도 아님" 상태다. 근접 참여 기기는 `at − anchor_recent_sec` 이후에 `rssi` 보고(빈 보고 포함)를 보낸 장소의 `trusted` 기기다 (FR-16, §1.2).
- 근접 참여 기기가 아닌 후보는 근접 지정과 전역 할당의 후보가 아니다 (FR-05).
- 근접 지정과 전역 할당은 자동 영점을 찾은 기기만 후보로 쓴다. 사후확률이 가장 큰 기기에 자동 영점이 없으면 다른 후보를 고르지 않고 `ASK`다 (FR-17, FR-23).
- 거리 순서에는 자동 영점이 없는 근접 참여 기기도 선다 (FR-16).
- 근접 추정은 좌표와 기기 배치를 쓰지 않는다. 공통 파라미터는 공통 모델 파일에서 읽는다 (FR-16).
- 자동 영점은 장소의 관측으로 찾는다. 라벨과 소유자 입력을 쓰지 않는다 (FR-16).
- `vendor.model`이나 `vendor.firmware`가 바뀌면 그 기기의 자동 영점을 버리고 다시 찾는다. 버린 사실도 DB에 남긴다. 재부팅과 동작 선언 변경은 자동 영점을 유지한다 (FR-16).
- `vendor`는 자동 영점을 버리는 조건으로만 쓴다. 판정 분기에 쓰지 않는다 (FR-16).
- DB는 자동 영점과 개정 번호를 저장한다. 판정 기록이 참조하는 개정을 지우지 않는다. 서버를 다시 시작해도 저장한 자동 영점을 쓴다 (FR-16, FR-22).
- 판정 기록의 개정 번호로 그 판정에 적용한 기기별 자동 영점을 다시 찾을 수 있다 (FR-22).
- 후보 확률을 다시 정규화하지 않는다. 후보 밖 확률을 후보에 더하지 않는다 (FR-16).
- 공개 데이터의 숫자와 팀 보드나 케이스의 값을 모델 값과 생성기 값으로 쓰지 않는다 (#252).
- 실측 데이터와 합성 데이터는 같은 공통 적합 함수로 모델을 만든다 (#252).
- 파일 내용을 바꾸면 새 버전을 만든다. 같은 버전의 파일을 덮어쓰지 않는다. 판정 기록이 참조하는 옛 파일을 지우지 않는다 (§1.2).
- 시간 값과 기준 값은 코드 상수로 쓰지 않는다. 설정이나 모델 파일에서 받는다 (AGENTS.md).

## 4. 경계

### 고치는 곳

- `server/src/nearpilot/shared/`
  - `ProximityResult.unzeroed`
  - `AutoZero`, `AutoZeroState`, `AutoZeroStore` Protocol
  - `ProximityEstimator` 문서 문자열 (전체 상태와 결과의 뜻)
- `server/src/nearpilot/db/` — 마이그레이션 `0013`, `AutoZeroStore` 구현
- `server/src/nearpilot/core/proximity/` — 공통 모델 파일, 관측 모델, 자동 영점, 공통 적합, 합성 생성기, 추정기. 장소별 모델과 배치 파일 코드를 지운다.
- `server/src/nearpilot/core/decision/pipeline.py` — 근접 지정과 전역 할당의 자동 영점 규칙
- `server/src/nearpilot/core/dashboard/queries.py` — 모델과 보정 버전의 출처
- `server/src/nearpilot/runtime/` — 설정 로더, 조립, 복원, `ThreadDatabase.auto_zero_store`
- `server/src/nearpilot/cli/` — `fit`, `eval`, `evaluate-reask`
- `server/tools/evaluation/` — 근접 표본 수집, 평가 harness, 평가 서버 설정, 지표 3 장면(결정 21), `EXPECTED_SCHEMA_VERSION`
- `server/tools/node_lab/` — 실험실 모델 파일과 설정(`setup.py`), 시연의 대상 지정(`session.py`, 결정 20)
- `server/config/` — 설정 예시, `final-demo` 설정과 모델, README
- `specs/core-proximity-estimator/`, `specs/core-proximity-model-fit/` — 지운다. 이전 모델과 바꾼 이유는 `docs/adr/0001-proximity-common-model.md`에 남긴다 (담당자 지시)
- `specs/core-decision-pipeline/spec.md` — 지운 spec을 가리키던 참조만
- `specs/core-ops-cli/spec.md`, `specs/core-server-assembly/spec.md`, `specs/db-evaluation-server-metrics/spec.md`, `specs/core-dashboard-queries/spec.md` — 머리의 상태 줄만 (바뀐 부분을 이 spec으로 가리킨다)
- `docs/architecture.md`, `docs/requirements.md` — 옛 배치 모델을 가리키던 표현을 공통 모델로 맞춘다. 뜻은 #252 그대로다 (담당자 지시)
- `docs/adr/0001-proximity-common-model.md` — 새로 쓴다. `docs/sensor-removal-review.md` — 지운 spec의 링크만
- 각 계층의 테스트

### 고치지 않는 곳

- 항상: `docs/` (위의 파일은 제외), `AGENTS.md` (Gotchas 절과 ADR을 더한 두 표의 행은 제외), `server/pyproject.toml`, `.github/`
- 이 작업: 거리 순서의 계산(`distance_order`), 명시 지정과 거리 선택자의 판정 규칙, 실행 직전 재검사, IOT, 펌웨어, 시뮬레이터, 대시보드 화면, 수집 도구(`tools/data_collection`), 분할 도구

`docs/architecture.md` §4 "근접 사후확률의 범위"가 `shared` 접점의 모양을 근접 추정 spec에 맡겼다. 그래서 이 작업은 `shared`를 고친다.

### 다른 영역과 닿는 곳

| 계약 | 사용 또는 제공 | 상대 영역 |
|---|---|---|
| `ProximityEstimator.posterior` → `ProximityResult.posteriors`, `unzeroed`, `calib_version` | 제공 | 근접 추정 → 판정 |
| `ProximityEstimator.observe`, `observe_report` | 제공 | 상태 관리 → 근접 추정 |
| `AutoZeroStore.auto_zeros`, `AutoZeroStore.save_auto_zero` | 제공 / 사용 | DB → 근접 추정 |
| `Repository.save_decision`의 `DecisionRecord.calib_version` | 사용 | 판정 → DB |
| `NodeInfo.descriptor.vendor` | 사용 | DB → 근접 추정 |

### 만들지 않는 것

- 장소를 나눈 지표 8 시험 도구 (학습 장소와 시험 장소를 바꿔 두 번 시험). 실측 뒤에 만든다 (#252).
- 실측 데이터로 만든 공통 모델과 지표 8 채점
- 찾은 자동 영점의 갱신. 찾은 뒤에는 `vendor`가 바뀌기 전까지 고정한다 (결정 5).
- 비콘을 못 들은 근접 참여 기기를 증거로 쓰는 것 (결정 3)
- 실행 직전 재검사의 자동 영점 규칙. 재검사는 지금처럼 임계값만 본다 (FR-18).
- 대시보드 화면의 자동 영점 표시. 기존 `calibrationVersion` 칸의 값만 바뀐다.
- `auto_zero` 행의 보존 정리. 행을 지우지 않는다.

## 5. 예외 상황

| 상황 | 기대 동작 | 테스트 |
|---|---|---|
| 모델 파일에 장소 ID, 기기 항목, 좌표 같은 모르는 키가 있다 | `ModelFileError`. 서버가 시작하지 않는다 | `test_FR16_common_model_rejects_layout_keys` |
| 모델 파일의 값이 범위 밖이다 (σ ≤ 0, stay ∉ (0, 1), `min_bins` < 1, 이름과 파일 이름이 다름) | `ModelFileError` | `test_FR16_common_model_rejects_bad_values` |
| 설정의 모델 파일이 없다 | `ModelMismatchError`. 서버가 시작하지 않는다 | `test_FR16_config_single_model_version` |
| 장소의 `trusted` 기기가 최근 구간 안에 보고하지 않았다 | 그 기기는 상태가 아니다. 후보여도 `posteriors`에 없다. 오류가 아니다 | `test_FR16_full_state_is_participants` |
| 근접 참여 기기가 하나도 없다 | `posteriors`가 비고 판정은 `ASK` | `test_FR16_no_participant_empty_posteriors` |
| 근접 참여 기기 중 일부만 자동 영점이 있다 | 없는 기기는 보정 없이 계산하고 `unzeroed`에 넣는다 | `test_FR16_unzeroed_marks_candidates` |
| 사후확률 1등에 자동 영점이 없다 (`nearest:`) | `ASK`·`LOW_CONFIDENCE`. 2등을 고르지 않는다. 감사 기록에 `unzeroed`를 남긴다 | `test_FR17_top_unzeroed_asks` |
| `any:` 요청의 1등에 자동 영점이 없다 | `ASK`·`LOW_CONFIDENCE` | `test_FR23_any_top_unzeroed_asks` |
| `any:` 요청에서 자동 영점이 없는 기기가 임계값을 넘는다 (1등은 아니다) | 그 기기를 허용쌍에 넣지 않는다 | `test_FR23_any_excludes_unzeroed` |
| 표본이 `min_bins`보다 적다 | 자동 영점을 찾지 않는다. DB에 쓰지 않는다 | `test_FR16_auto_zero_needs_min_bins` |
| 칸에서 1등이 아닌 기기가 둘 미만이다 (기기가 1~2대인 장소 포함) | 그 칸은 표본이 아니다. 그런 장소는 자동 영점을 찾지 못해 근접 지정이 `ASK`다. 명시 지정과 거리 선택자는 쓸 수 있다 | `test_FR16_auto_zero_bin_needs_two_anchors` |
| 그 기기가 칸의 1등이다 (같은 값의 1등 포함) | 그 칸은 그 기기의 표본이 아니다 | `test_FR16_auto_zero_excludes_top_bins` |
| 칸이 닫힌 뒤 그 칸의 프레임이 늦게 온다 | 자동 영점 표본에 넣지 않는다. 사후확률 계산에는 쓴다 | `test_FR16_auto_zero_late_frame_ignored` |
| `vendor.model`이나 `vendor.firmware`가 바뀌었다 | 버림 행을 DB에 쓰고 자동 영점을 지운다. 모으던 표본도 지운다. 다시 모은다 | `test_FR16_vendor_change_discards_auto_zero` |
| 기기가 재부팅했다 (새 `session_id`, 같은 `vendor`) | 자동 영점을 유지한다 | `test_FR16_reboot_keeps_auto_zero` |
| 서버를 다시 시작했다 | DB의 최신 자동 영점을 쓴다. `vendor`가 바뀐 기기는 첫 관측에서 버린다 | `test_FR16_restart_loads_auto_zero` |
| 자동 영점 저장이 실패한다 | 예외를 올리지 않는다. 자동 영점을 쓰지 않고 표본을 남겨 다음 칸에서 다시 저장한다 | `test_FR16_auto_zero_store_failure_retries` |
| 버림 저장이 실패한다 | 예외를 올리지 않는다. 메모리의 자동 영점은 지운다. 재시작하면 `vendor` 비교가 다시 버린다 | `test_FR16_discard_store_failure_drops_zero` |
| `auto_zeros(revision=R)`의 R이 없는 개정이다 (R < 0) | `ValueError` | `test_FR22_auto_zero_revision_snapshot` |
| 보존 정리가 돈다 | `auto_zero` 행을 지우지 않는다 | `test_FR22_auto_zero_rows_survive_cleanup` |
| `ProximityResult.unzeroed`에 `posteriors`에 없는 기기가 있다 | `ValueError` | `test_FR16_result_unzeroed_subset` |
| 수집 전용 모드다 (모델 없음) | `posterior`는 `ModelMismatchError`. 자동 영점 표본을 모으지 않는다 | `test_FR16_collect_only_no_model` |
| 적합 입력의 한 역할(앞, 옆, 없음)에 표본이 둘보다 적거나 표준편차가 0이다 | `FitError` | `test_FR16_common_fit_rejects_thin_roles` |
| `fit`의 분할 결과 둘이 같은 장소다 | 종료 코드 `EXIT_INVALID` | `test_FR16_cli_fit_rejects_duplicate_place` |

## 6. 결정

1. 관측 모델은 "앞 기기만 튄다"다. 칸마다 보정한 RSSI `r'_a = r_a − z_a`에 아래 정규분포를 쓴다. 공통 값 여섯 개(`front`, `other`, `none`의 평균과 표준편차)만 쓰고 기기 수와 상관없다 — 배치 없이 장소마다 같은 식을 쓸 수 있다. 담당자가 2026-11-09에 골랐다.
   - 기기 i 앞: 앵커 i는 `front`, 다른 앵커는 `other`
   - 어느 기기 앞도 아님: 모든 앵커가 `none`
   - 자동 영점이 없는 앵커는 `z_a = 0`이다.
2. HMM 전이(`stay`), 관측 구간(`window_sec`), 칸(`rssi_frame_sec`), 처음 분포(균등), 우도 온도 τ와 칸 평균은 이전 추정기와 같다. 상태 목록만 근접 참여 기기로 바뀐다.
3. 칸에 값이 없는 앵커는 증거로 쓰지 않는다 — 이전과 같다. 담당자가 2026-11-09에 골랐다. "못 들음"을 증거로 쓰는 것은 실측 뒤에 다시 본다.
4. 자동 영점을 찾는 방법. 담당자가 2026-11-09에 골랐다.
   - 칸은 `frame_ts`를 `rssi_frame_sec`로 나눈 절대 칸이다. 장소와 비콘마다 칸 안의 앵커별 평균 RSSI를 낸다.
   - 값이 가장 큰 앵커는 같은 값이 여럿이어도 모두 1등이다. 1등은 표본에서도 비교 기준에서도 뺀다 — 방문자가 그 기기 앞에 있을 가능성이 크다 (#252의 "멀리 있을 때를 기준"에 가깝다).
   - 칸에 1등이 아닌 앵커가 둘 이상이면, 그 앵커 a마다 `r_a − mean(1등이 아닌 앵커들의 값, a 포함)`을 a의 표본으로 더한다 — 1등 값이 기준에 섞이면 기기가 3대인 방에서 기기끼리의 차이가 1.5배로 부푼다. 자기를 포함한 평균이라 앵커 수와 상관없이 차이가 부풀지 않는다. 담당자가 2026-11-09에 2단계 보고를 보고 정했다.
   - 그래서 기기가 1~2대인 장소는 자동 영점을 찾지 못한다.
   - 값은 보정하지 않은 값이다 — 찾은 순서가 결과를 바꾸지 않는다.
   - 표본이 모델 파일의 `auto_zero.min_bins` 이상이면 표본의 중앙값을 자동 영점으로 찾는다. 짝수 개면 가운데 두 값의 평균이다.
   - 같은 장소, 같은 비콘의 더 늦은 칸(번호가 2 이상 큰 칸)의 프레임이 오면 칸을 닫는다. 닫은 칸의 늦은 프레임은 표본에 넣지 않는다.
   - 표본은 메모리에만 둔다. 찾기 전에 서버를 다시 시작하면 처음부터 모은다. `restore`가 넘긴 프레임도 표본이 된다.
   - 적합(`fit`), 평가(`eval`, `evaluate-reask`), 합성 생성은 같은 함수로 장소의 모든 회차 프레임에서 자동 영점을 먼저 찾는다. 라벨은 쓰지 않는다. 표본이 모자란 앵커는 `z = 0`이다.
5. 찾은 자동 영점은 `vendor`가 바뀌기 전까지 고정한다 — 개정이 드물고 판정 재현이 쉽다. 담당자가 2026-11-09에 골랐다.
6. 개정 번호는 서버 전체에 하나다. 담당자가 2026-11-09에 골랐다.
   - `auto_zero` 행 하나가 개정 하나다. 행: `revision`(INTEGER PK AUTOINCREMENT), `node_id`(FK `node`), `zero_db`(REAL, 버림이면 NULL), `vendor_model`, `vendor_firmware`(TEXT, 선언이 없으면 NULL), `recorded_at`.
   - 개정 R의 기기별 자동 영점은 기기마다 `revision ≤ R`인 가장 최근 행이다. 그 행이 버림이면 자동 영점이 없다.
   - `ProximityResult.calib_version`은 계산할 때의 최신 개정 번호의 10진 문자열이다. 개정이 없으면 `"0"`이다.
   - 행을 고치거나 지우지 않는다. 판정 기록이 참조하는 개정은 저절로 남는다.
7. `shared` 접점. 담당자가 2026-11-09에 골랐다.
   ```python
   @dataclass(frozen=True, slots=True)
   class AutoZero:
       node_id: str
       zero_db: float | None        # None 이면 버림
       vendor_model: str | None
       vendor_firmware: str | None
       revision: int
       recorded_at: datetime

   @dataclass(frozen=True, slots=True)
   class AutoZeroState:
       revision: int                 # 이 상태의 개정 번호. 행이 없으면 0
       zeros: Mapping[str, AutoZero] # node_id → revision 이하의 가장 최근 행 (버림 포함)

   @runtime_checkable
   class AutoZeroStore(Protocol):
       def auto_zeros(self, revision: int | None = None) -> AutoZeroState: ...
       def save_auto_zero(self, node_id: str, zero_db: float | None, vendor_model: str | None,
                          vendor_firmware: str | None, recorded_at: datetime) -> int: ...  # 새 개정 번호

   # ProximityResult 에 더한다. posteriors 키의 부분집합이어야 한다
   unzeroed: frozenset[str] = frozenset()
   ```
   - 기본값이 빈 집합이라 기존 fake와 기록은 "모두 자동 영점 있음"으로 읽힌다.
   - `posterior`는 근접 참여 후보만 `posteriors`에 넣는다. 판정은 `posteriors`에 없는 후보를 근접 지정과 전역 할당에서 쓰지 않는다 — 판정이 근접 참여 여부를 따로 묻지 않는다.
   - `SqliteRepository`가 `AutoZeroStore`를 구현한다. `ThreadDatabase.auto_zero_store()`가 범위 위임 객체를 준다.
8. 근접 지정과 전역 할당의 `ASK` 사유는 `LOW_CONFIDENCE`다. 새 사유 코드를 만들지 않는다 — 호스트에게는 "더 가까이"와 같은 안내다. ③ 감사 기록에 `unzeroed` 목록을 남긴다.
9. 모델 파일은 `{model_root}/{model_version}.json`이다. 설정은 `[proximity]`의 `model_root`, `model_version`이다. 장소별 `[proximity.models]` 표는 받지 않는다. 형식:
   ```json
   {
     "model_version": "synthetic-2",
     "window_sec": 3.5,
     "transition": {"stay": 0.9},
     "calibration": {"method": "none"},
     "observation": {
       "front": {"mean": -62.0, "sigma": 4.1},
       "other": {"mean": -72.0, "sigma": 5.0},
       "none": {"mean": -74.0, "sigma": 4.5}
     },
     "auto_zero": {"min_bins": 120},
     "validation": {"source": "SYNTHETIC_NOT_MEASURED", "quality_verified": false}
   }
   ```
   - `validation`만 선택이다. 모르는 키와 빠진 키를 거부한다. `calibration`은 이전처럼 `none` 또는 `temperature`와 `tau`다.
   - 위 수치는 형식의 예다. 실제 값은 적합 결과다.
10. 합성 생성기 `core/proximity/synthetic.py`. 시드를 고정한 `random.Random`만 쓴다.
    - 방 6곳. 방마다 기기 3~7대를 벽 위 무작위 위치에 서로 1 m 이상 떨어뜨려 놓는다. 방 크기는 가로 4~8 m, 세로 3~6 m에서 고른다.
    - 기기 앞은 기기에서 방 안쪽으로 0.5 m다. "어느 기기 앞도 아님"은 모든 기기 앞에서 1.5 m 이상 떨어진 방 안의 무작위 위치다.
    - RSSI는 이전 합성 모델의 값으로 만든다: `-59 − 20·log10(max(d, 0.5)) + z + N(0, 4)`. 기기마다 영점 차이 `z`를 [-6, 6] dB에서 고른다.
    - 라벨마다(기기 앞, 없음) 학습 회차 5개, 검증 회차 2개를 만든다. 회차는 30칸이고 칸마다 앵커마다 프레임 하나다.
    - 적합 후보: τ ∈ {1}, `stay` ∈ {0.9}, `window_sec` ∈ {3.5}, `min_bins` 120. HMM 값은 이전 추정기의 값으로 고정한다 — 생성 회차에 이동이 없어서, 후보를 넓히면(τ {0.5, 1, 2}, `stay` {0.8, 0.9, 0.95}, `window_sec` {2.5, 3.5, 5}) 전환이 느린 격자 끝(τ 2, `stay` 0.95, `window_sec` 5)을 골랐다 (NFR-02). 담당자가 2026-11-09에 3단계 결과를 보고 정했다.
    - 생성기 값은 이전 합성 모델(`synthetic-1`)의 값과 그 값을 넓힌 범위다. 공개 데이터나 팀 기기의 값이 아니다.
    - 모델 파일의 `validation`에 `source: SYNTHETIC_NOT_MEASURED`, `quality_verified: false`, 생성기 설정 전체, 고른 조합의 검증 손실을 쓴다. 배치는 쓰지 않는다.
    - 시드는 252다. `server/config/final-demo/models/synthetic-2.json`이 그 결과이고, 시험이 같은 시드로 다시 만들어 같은지 본다.
11. 공통 적합은 장소별 회차 묶음을 받는다. 장소의 앵커는 그 장소 회차의 프레임에 나온 기기와 라벨 기기다 — 배치 파일 없이 정한다. 평가도 같은 앵커를 근접 참여 기기로 쓴다 (기록에 보고 시각이 없다).
12. 역할 표본은 학습 회차의 칸 평균을 자동 영점으로 보정한 값이다. 라벨이 기기 i이면 앵커 i의 값은 `front`, 다른 앵커의 값은 `other`, 라벨이 `none`이면 모든 값이 `none`이다. 평균과 모표준편차를 `math.fsum`으로 낸다. τ, `stay`, `window_sec`는 이전처럼 검증 평균 로그 손실이 가장 작은 조합이다.
13. `nearpilot fit`은 `--split`을 여러 번 받는다 (장소마다 하나). 배치 인자를 지운다. 적합 설정 `[fit]`에서 `calib_version`, `min_distance_m`을 지우고 `auto_zero_min_bins`를 더한다. `eval`과 `evaluate-reask`는 `PLACE/VERSION` 대신 `VERSION`을 받고 장소는 분할 결과에서 읽는다. 보고서의 `calib_version`은 `"auto-zero"`다 — 평가는 시험 회차에서 자동 영점을 찾는다.
14. 대시보드의 `modelVersion`은 공통 모델 버전, `calibrationVersion`은 조회 순간의 최신 개정 번호다. 모든 장소가 같은 값이다. 수집 전용 모드면 둘 다 모델 없음이다.
15. 근접 추정기는 DB 쓰기를 잠금 밖에서 한다. 저장 중인 기기는 표시해서 같은 기기를 두 번 저장하지 않는다. 쓰기가 끝나면 잠금 안에서 개정 번호를 반영한다.
16. 옛 `synthetic-1.json`(final-demo)은 지우지 않는다 — 판정 기록이 참조할 수 있다. 서버는 읽지 않는다. `core-proximity-estimator`, `core-proximity-model-fit` spec은 지우고, 이전 모델과 바꾼 이유를 ADR 0001에 남긴다. 담당자가 2026-11-10에 정했다.
17. 평가 harness의 근접 준비 확인은 DB에서 보고 시각을 읽을 수 없다. 최근 구간 안에 받은 프레임이 있는 `trusted` 기기를 근접 참여 기기로 근사한다. 대상이 `unzeroed`면 준비되지 않은 것이다.
    - harness는 시작할 때 한 번 `trusted` 기기마다 자동 영점 0 dB 행을 DB에 쓰고(`vendor`는 기기 선언 그대로) 서버를 다시 띄운다. 지표를 재는 동안은 읽기 전용 연결만 쓴다 (NFR-13). 평가 장면은 방문자를 한 기기 앞에 세워서, 늘 1등인 기기는 자동 영점 표본을 얻지 못한다. 시뮬레이터 기기는 영점 차이가 없다. 담당자가 2026-11-09에 5단계 보고를 보고 정했다.
18. `fit`의 적합 설정 기본 파일은 `fit/common.toml`이다 — 공통 모델은 장소 이름이 없다. `eval`은 통과 기준을 기존처럼 `fit/<장소>.toml`에서 읽는다.
19. 새 코드는 옛 코드 옆의 새 모듈(`common_model.py`, `autozero.py`, `posterior.py`, `common_fit.py`, `synthetic.py`)에 두고, 서버와 도구를 옮긴 뒤 옛 `model.py`, `filter.py`, `fit.py`와 배치 파일 읽기를 지운다 — 단계마다 전체 시험이 통과한다.
20. node_lab 기본 기기는 조명 둘이라 자동 영점을 찾지 못한다 (결정 4). 시연(`demo`)은 `nearest:` 대신 사후확률이 가장 큰 기기를 명시 지정해 요청한다. 담당자가 2026-11-09에 정했다.
21. 평가 지표 3의 장면은 모든 방문자가 첫 `trusted` 사물함 하나에만 near 값으로 들리게 한다. 사물함마다 같은 값으로 들리면 공통 모델의 사후확률이 사물함 수만큼 나뉘어 임계값을 넘는 요청이 드물고, 점유 경합을 만들지 못한다. 담당자가 2026-11-09에 정했다.

## 7. 가정

없음

## 8. 테스트

| 요구사항 | 테스트 | 확인하는 것 |
|---|---|---|
| FR-16 | `test_FR16_common_model_loads` | 형식 예의 파일을 읽는다. 읽은 모델은 바꿀 수 없다 |
| FR-16 | `test_FR16_common_model_rejects_layout_keys` | `place_id`, `nodes`, `none_points`, `calib_version` 키를 거부한다 |
| FR-16 | `test_FR16_common_model_rejects_bad_values` | 범위 밖 값과 이름 불일치를 거부한다 |
| FR-16 | `test_FR16_full_state_is_participants` | 보고하지 않은 `trusted` 기기는 상태와 결과에 없다 |
| FR-16 | `test_FR16_no_participant_empty_posteriors` | 참여 기기가 없으면 빈 결과다 |
| FR-16 | `test_FR16_posteriors_not_renormalized` | 후보 합이 1 이하이고 후보 밖 확률을 더하지 않는다 |
| FR-16 | `test_FR16_front_device_highest_posterior` | 합성 장소에서 기기 앞에 선 방문자의 1등이 그 기기다 |
| FR-16 | `test_FR16_auto_zero_corrects_offset` | 영점 차이가 큰 기기 앞에서 보정 뒤 1등이 맞는다 |
| FR-16 | `test_FR16_auto_zero_needs_min_bins` | 표본이 모자라면 찾지 않는다 |
| FR-16 | `test_FR16_auto_zero_bin_needs_two_anchors` | 1등이 아닌 앵커가 하나 이하인 칸은 표본이 아니다 |
| FR-16 | `test_FR16_auto_zero_excludes_top_bins` | 1등(같은 값 포함)은 표본도 기준도 아니다 |
| FR-16 | `test_FR16_auto_zero_late_frame_ignored` | 닫은 칸의 늦은 프레임은 표본에 없다 |
| FR-16 | `test_FR16_auto_zero_saved_with_revision` | 찾으면 DB에 쓰고 결과의 `calib_version`이 그 개정이다 |
| FR-16 | `test_FR16_unzeroed_marks_candidates` | 자동 영점이 없는 참여 후보가 `unzeroed`에 있다 |
| FR-16 | `test_FR16_vendor_change_discards_auto_zero` | `vendor` 변경이 버림 행을 쓰고 다시 모은다 |
| FR-16 | `test_FR16_reboot_keeps_auto_zero` | 새 연결, 같은 `vendor`는 유지한다 |
| FR-16 | `test_FR16_restart_loads_auto_zero` | 시작할 때 DB의 자동 영점을 쓴다 |
| FR-16 | `test_FR16_auto_zero_store_failure_retries` | 저장 실패를 삼키고 다음 칸에 다시 저장한다 |
| FR-16 | `test_FR16_discard_store_failure_drops_zero` | 버림 저장이 실패해도 자동 영점을 쓰지 않는다 |
| FR-16 | `test_FR16_result_unzeroed_subset` | `unzeroed` ⊄ `posteriors`면 `ValueError` |
| FR-16 | `test_FR16_collect_only_no_model` | 모델이 없으면 `posterior`가 실패하고 표본을 모으지 않는다 |
| FR-16 | `test_FR16_common_fit_no_layout` | 배치 없이 여러 장소의 회차로 모델을 만든다. 파일에 장소와 기기 ID가 없다 |
| FR-16 | `test_FR16_common_fit_rejects_thin_roles` | 역할 표본이 모자라면 `FitError` |
| FR-16 | `test_FR16_fit_uses_runtime_auto_zero` | 적합과 추정기가 같은 프레임에서 같은 자동 영점을 낸다 |
| FR-16 | `test_FR16_synthetic_model_reproducible` | 시드 252로 다시 만든 모델이 `synthetic-2.json`과 같다 |
| FR-16 | `test_FR16_synthetic_model_marked` | 합성 표시와 생성기 설정이 있고 배치가 없다 |
| FR-16 | `test_FR16_config_single_model_version` | `[proximity] model_version`만 받는다. 파일이 없으면 시작하지 않는다 |
| FR-16 | `test_FR16_dashboard_shows_auto_zero_revision` | 대시보드의 보정 버전이 최신 개정이다 |
| FR-16 | `test_FR16_runtime_saves_auto_zero_to_db` | 실제 서버 조립의 추정기가 찾은 자동 영점을 DB에 쓴다 |
| FR-16 | `test_FR16_dashboard_calibration_follows_runtime_revision` | 실제 서버의 대시보드 보정 버전이 DB의 최신 개정 번호다 |
| FR-16 | `test_NFR13_harness_ready_rejects_unzeroed_target` | 평가 harness의 준비 확인이 자동 영점이 없는 대상을 준비되지 않은 것으로 본다 (결정 17) |
| FR-16 | `test_FR16_cli_fit_multiple_places` | `fit`이 장소 둘의 분할로 공통 모델을 쓴다 |
| FR-16 | `test_FR16_cli_fit_rejects_duplicate_place` | 같은 장소의 분할 둘은 거부한다 |
| FR-16 | `test_FR16_cli_eval_common_model` | `eval VERSION`이 시험 회차의 자동 영점으로 평가한다 |
| FR-05 | `test_FR05_non_participant_not_nearest_candidate` | 보고하지 않은 후보는 근접 지정으로 고르지 않는다 |
| FR-17 | `test_FR17_top_unzeroed_asks` | 1등에 자동 영점이 없으면 `ASK`, 2등을 고르지 않는다 |
| FR-17 | `test_FR17_zeroed_top_uses_threshold` | 1등에 자동 영점이 있으면 이전처럼 임계값을 본다 |
| FR-23 | `test_FR23_any_excludes_unzeroed` | 자동 영점이 없는 기기를 배정하지 않는다 |
| FR-23 | `test_FR23_any_top_unzeroed_asks` | `any:`의 1등에 자동 영점이 없으면 `ASK` |
| FR-22 | `test_FR22_migration_0013_auto_zero` | 스키마 버전 13, `auto_zero` 테이블 |
| FR-22 | `test_FR22_auto_zero_revision_snapshot` | 개정 R의 상태가 기기별로 R 이하의 가장 최근 행이다 |
| FR-22 | `test_FR22_auto_zero_rows_survive_cleanup` | 보존 정리가 행을 지우지 않는다 |
| FR-22 | `test_FR22_calib_version_recorded` | 판정 기록의 `calib`이 결과의 개정 번호다 |
| NFR-02 | `test_NFR02_switch_within_two_seconds` | 이동 뒤 2초 안에 대상이 바뀐다 (합성 경로). strict xfail로 둔다 — 시험용 공통 모델(`window_sec` 3, `stay` 0.9)로 시드 20개 중 7개가 2초를 넘는다. 이전 모델도 같은 이유의 xfail이었다. 담당자가 2026-11-09에 정했다 |
| NFR-02 | `test_NFR02_flicker_reduced` | 공통 모델로도 정지 중 깜빡임이 기준선보다 80% 줄어든다 (합성 경로) |

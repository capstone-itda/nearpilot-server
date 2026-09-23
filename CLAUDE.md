# NearPilot 서버

AI 호스트(MCP)와 ESP32 노드 사이에서 **실행 허가를 판정하는 단일 프로세스 서버**.
MCP/REST/WebSocket API + 판정 코어 + SQLite + MQTT 게이트웨이로 구성된다.

4인 캡스톤 팀이 계층 하나씩(API · CORE · DB · IOT) 맡는다.

## 문서 권위 순서

내용이 서로 어긋나면 **위쪽이 이긴다.**

1. `bedrock/NearPilot_요구사항분석서.md` — 원본 요구사항. 모든 문서의 출처
2. `docs/prd.md` — 서버 범위와 인수 조건
3. `docs/architecture.md` — 저장소 구조·계층 계약·분담
4. `docs/intent.md` — 4인이 합의한 결정 기록 (아래 "문서 수정 규칙" 참고)

구현 판단이 필요하면 추측하지 말고 위 문서에서 근거를 찾는다.
근거가 없으면 만들어내지 말고 미결 사항으로 남긴다.

## 개발 명령

```bash
pip install -e ".[dev]"   # 개발 설치
pytest                    # 전체 테스트
pytest tests/core         # 계층 하나만
```

`pyproject.toml` 에 `pythonpath = ["src"]` 가 있어 설치 없이도 테스트는 돈다.
**서버 실행 진입점과 Mosquitto 기동 방법은 아직 정해지지 않았다.** 정해지면 이 절에 추가한다.

## 계층 구조와 소유권

```
api  ──▶  core  ──▶  db
            │  ◀──  iot      (iot → core 이벤트, core → iot 실행 명령)
모든 계층 ──▶ shared
```

코드는 `src/nearpilot/<계층>/`, 테스트는 `tests/<계층>/` 에 둔다.
**각자 자기 계층 폴더 안에서만 작업한다.** 다른 계층을 고쳐야 하면 그 담당자에게 요청한다.
`shared/` 와 `tests/integration/` 은 4인 공동 소유다.

### 의존 규칙 (architecture.md §3)

1. **api 는 db 를 직접 부르지 않는다.** 예외는 토큰 → 계정 식별 하나뿐이다
2. **판정은 core 만 한다.** api·iot 는 결과를 전달할 뿐 허가 여부를 바꾸지 않는다 (NFR-07)
3. **iot 는 core 의 OK 없이 노드에 실행 명령을 보내지 않는다** (FR-06)
4. **계층끼리는 구현이 아니라 `shared` 인터페이스에만 의존한다.** 그래야 가짜 구현(fake)으로
   다른 계층 없이 개발·테스트할 수 있다
5. **db 쓰기는 한 경로로만 한다.** ⑤⑥ 검사와 RESERVED 생성은 `BEGIN IMMEDIATE` 트랜잭션
   하나에서 처리한다 (FR-19, NFR-03)

### 응답 변환 (자주 놓침)

판정 결과를 호스트에 돌려줄 때 **사용자 ID 와 점유 주체를 반드시 제거**한다 (FR-11, NFR-08).
`get_device_state`, `get_use_status` 도 마찬가지다. 본인 `use_id` 만 조회된다 (FR-21).

## 문서 수정 규칙

- `docs/intent.md` 는 **4인 공유 문서**다. 수정은 PR 로 올리고 **전원 리뷰**를 받는다
- `src/nearpilot/shared/` 변경도 **전원 리뷰**다. 계층 폴더 변경은 담당자 리뷰로 충분하다
- 새 intent 문서는 `docs/templates/intent.md` 를 복사해 시작한다. **6개 절을 바꾸지 않는다**
- 규칙 원문은 `docs/templates/intent.md` 헤더 주석에 있다. 이 파일은 요약일 뿐이다

> 무료 비공개 레포라 브랜치 룰셋·CODEOWNERS 가 잠겨 있다.
> **GitHub 가 막아주지 않으므로 4인이 합의로 지킨다.** 리뷰어는 수동으로 지정한다.

### Claude 가 지킬 것

- `docs/intent.md`, `bedrock/`, `docs/prd.md`, `docs/architecture.md` 는 **직접 수정하지 않는다.**
  초안을 제시하고 사람이 PR 로 올리게 한다
- 담당자가 정해진 계층 폴더를 요청 없이 건드리지 않는다

## 컨벤션

- 브랜치는 계층 단위: `api/…`, `core/…`, `db/…`, `iot/…`, 공동 계약은 `shared/…`
- 커밋·PR·테스트 이름에 요구사항 ID를 적는다 — 예: `FR-19 원자적 점유`
- **FR 하나당 pytest 케이스를 최소 하나** 둔다 (분석서 부록 D)
- 계층을 합친 시나리오는 `tests/integration/` 에 공동으로 작성한다

## 함정

- **`mcp` 2.x 는 `FastMCP` 가 `MCPServer` 로 이름이 바뀌었다.** 1.x 예제 코드를 그대로 쓰지 말 것
- **`paho-mqtt` 2.x 는 생성자에 `CallbackAPIVersion` 을 요구한다.** 콜백 시그니처도 1.x와 다르다
- SQLite 는 WAL 모드다. `-wal` / `-shm` 파일이 생기며 커밋하지 않는다 (`.gitignore` 처리됨)
- 시연 환경이 **Windows 노트북**이다. POSIX 전용 기능에 의존하지 않는다
- MQTT 는 독립 AP 내부 폐쇄망을 전제로 한다 (NFR-12)

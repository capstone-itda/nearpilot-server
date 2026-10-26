# NearPilot 운영 대시보드

2026-10-26에 센서 기반 물리 검증을 범위에서 제외했다 (`docs/requirements.md` FR-07·FR-12A·FR-12B·FR-20).
아래는 현재 화면과 어댑터의 실행법이다. 화면과 목 데이터, 서버 조회 문구에 새 표시 기준을 반영했다.
실행 성공은 제어 신호 출력 완료이고, 반납 완료는 논리적 점유 해제다. 화면이 실제 열림이나 켜짐을 확인한 것처럼 안내하지 않는다.
`core/dashboard`의 문구, 목 장면, 서버 어댑터와 화면을 함께 점검한다. 대시보드의 보기 전용 범위는 유지한다.

운영·시연 모니터. 서버에 쓰지 않는다. spec: `specs/dashboard-monitor/spec.md`

```bash
cd dashboard
npm install
npm run dev        # 개발 서버
npm run build      # 정적 빌드 (dist/)
npm test           # Vitest
npx playwright test  # 화면 검사 (시스템 Chrome 사용)
```

## 주소 값

데이터 소스는 둘이다. 기본은 목 어댑터이고, `source=server`이면 서버 어댑터다 (FR-12A, FR-12B).

| 값 | 뜻 |
|---|---|
| `source` | `server`이면 목 데이터 대신 NearPilot 서버에 붙는다 (아래 "서버에 붙여 열기"). 이때 `scene`은 쓰지 않는다 |
| `scene` | 목 장면: `idle`, `ask`, `judging`, `flow`, `done`, `fault`, `approval`, `later`, `side-room`, `offline`, `play`(장면 사이의 이벤트를 차례로 한 번 보냄), `live`(이벤트가 시각 순서대로 흐름. 판정은 실제처럼 빠르게, 실행 결과는 2초 이상 기다림, 조용한 구간은 빨리 감음) |
| `panel` | 처음 열 패널: `uses`, `approvals`, `audit`, `history`, `trust`, `device:<기기 ID>`, `request:<요청 ID>` |
| `view` | 설정 화면: `layout`, `phones`, `check` |
| `theme` | `dark`, `light` |

## 서버에 붙여 열기

서버 어댑터는 서버의 조회 REST(`/dashboard/...`)와 모니터링 WebSocket(`/dashboard/stream`)을 읽기만 한다. 서버에 쓰는 요청은 없다.

1. 서버 설정 파일 `server/config/server.toml`에 `[dashboard]` 섹션을 둔다. 키 6개가 모두 필수이고, 섹션이 없으면 서버는 대시보드를 열지 않는다. 키와 예시 값은 `server/config/README.md`의 "서버 실행 설정"과 `server.example.toml`을 본다.
2. `server/`에서 서버를 켠다: `nearpilot serve` (기본 주소는 설정의 `[http]`, 예시는 `127.0.0.1:8000`).
3. `dashboard/`에서 `npm run dev`로 연다. 빌드 결과로 보려면 `npm run build` 뒤 `npm run preview`다.
4. 브라우저에서 `http://localhost:5173/?source=server`를 연다 (`preview`는 4173).
5. 소유자 토큰을 넣는다. 소유자 토큰은 `nearpilot seed`가 소유자 계정을 등록할 때 한 번만 화면에 나온다. 명령은 `server/config/README.md`의 "운영 명령"을 본다.

- 개발·미리보기 서버가 `/dashboard`로 오는 요청과 WebSocket을 서버로 넘긴다 (`vite.config.ts`의 `proxy`). 그래서 서버에 CORS를 열지 않는다. 서버 주소가 기본(`http://127.0.0.1:8000`)과 다르면 `NEARPILOT_SERVER=http://<주소>:<포트> npm run dev`로 바꾼다.
- 토큰은 이 탭의 세션 저장소에만 두고, 주소·화면·콘솔에 쓰지 않는다. 탭을 닫으면 사라지고, 새로 고침하면 이어서 쓴다. 서버가 토큰을 거부하면(조회 401·403, 스트림 4401·4403) 토큰을 지우고 이유와 함께 입력 화면으로 돌아간다.
- 스트림이 끊기면(1011, 1013, 4408, 네트워크 끊김) 1초부터 두 배씩, 최대 10초 간격으로 다시 연결하고, 연결되면 서버가 보내는 지금 상태로 이어 간다. 끊김 안내 띠의 "다시 연결" 버튼은 기다리는 재연결을 바로 한다.
- 시각은 이 PC의 시간대로 보인다. 서버가 UTC로 보내도 지역 시각으로 바꿔 그린다.
- 평가 리포트가 없는 서버(`var/reports/`가 비어 있음)는 신뢰도 패널의 사전 평가 칸에 "평가 리포트 없음"을 보인다.

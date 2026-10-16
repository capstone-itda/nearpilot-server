# NearPilot 운영 대시보드

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

지금 데이터 소스는 목 어댑터 하나다. 서버 어댑터는 대시보드 조회 REST와 모니터링 WebSocket 형식이 정해진 뒤 만든다 (FR-12A, FR-12B).

| 값 | 뜻 |
|---|---|
| `scene` | 목 장면: `idle`, `ask`, `judging`, `flow`, `done`, `fault`, `approval`, `later`, `offline`, `play`(장면 사이의 이벤트를 차례로 한 번 보냄), `live`(이벤트가 시각 순서대로 흐름. 요청 하나의 이벤트는 하나씩 약 0.9초 간격으로 느리게, 조용한 구간은 빨리 감음) |
| `panel` | 처음 열 패널: `uses`, `approvals`, `audit`, `history`, `trust`, `device:<기기 ID>`, `request:<요청 ID>` |
| `view` | 설정 화면: `layout`, `phones`, `check` |
| `theme` | `dark`, `light` |

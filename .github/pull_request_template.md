<!--
제목: 타입(계층): 요약
  타입 intent | feat | fix | docs | test | refactor | chore,  계층 api | core | db | iot | shared | app | dashboard | firmware (생략 가능)
  예) feat(core): FR-19 원자적 점유   ·   docs: FR-03 승인 알림 흐름
브랜치: intent|feat|fix/{계층}-{요약},  문서는 docs/{요약}.  intent PR 은 docs/intent/{요약}.md 하나만 담는다.
위 형식은 CI "PR 규칙 / 형식 검사" 가 확인한다.
-->

## 왜

**요구사항 ID:** <!-- 예: FR-19, NFR-03 / 없으면 "없음" -->

<!-- 무엇이 문제였나. 해결책이 아니라 바꾸게 된 이유를 쓴다. -->

## 무엇을

<!-- 바뀐 내용을 파일·절 단위로. diff 를 열기 전에 범위를 알 수 있게 쓴다. -->

## 리뷰 범위

가장 엄격한 하나에 체크하고 **Reviewers 에 직접 지정**한다. 규칙 원문은 `docs/architecture.md` §8.

- [ ] 역할별 담당 코드·테스트만 (`architecture.md` §1~2의 담당 범위) — 해당 담당자 1인
- [ ] `docs/prd.md` · `docs/architecture.md` · `docs/intent/` — 작성자 외 2인 승인
- [ ] `server/src/nearpilot/shared/` — 작성자 외 3인 승인 (4인 전원)
- [ ] 그 외 (빌드 설정, 기타 문서, 도구 등) — 담당자 1인

> CI "PR 규칙 / 승인 검사" 가 문서·shared 의 승인 수를 센다.
> 무료 플랜이라 검사가 실패해도 머지 버튼은 막히지 않는다. **머지 전에 초록불을 직접 확인한다.**

## 확인한 것

해당하지 않는 묶음은 지운다.

**코드**
- [ ] `pytest` 통과
- [ ] 바뀐 FR 마다 테스트 케이스가 하나 이상 있다
- [ ] 다른 담당자의 범위를 건드리지 않았다 (건드렸다면 조율 내용과 이유를 적는다)
- [ ] 판정 응답에 사용자 ID·점유 주체가 들어가지 않는다 (FR-11, NFR-08)

**문서**
- [ ] 상위 문서와 어긋나지 않는다 (prd > architecture > intent)
- [ ] 근거 없는 결정은 본문 대신 미결 사항(PRD §7, intent Open questions)에 적었다

## 리뷰어에게

<!-- 미결 사항, 후속 작업, 특히 봐줬으면 하는 부분. 없으면 절째 지운다. -->

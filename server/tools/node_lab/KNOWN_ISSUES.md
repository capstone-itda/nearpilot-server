# 시험 중 발견한 기존 서버의 후속 작업 초안

이 문서는 이슈 초안이다. 이슈와 PR을 만들지 않았다.
시험 도구는 아래 CORE·DB 동작을 바꾸지 않는다.

## FR-07·FR-19 공용형 결과 재인계의 실행 문맥 불일치

### 재현

1. 새 sim 시험 폴더에서 `python -m tools.node_lab demo`를 실행한다.
2. `decision=OK`, `command_result=succeeded`와 기기의 동작을 확인한다.
3. `server_diagnostic`에서 `CORE handover failed ... method=on_result ... error=ValueError`를 확인한다.

### 근거

- `server/src/nearpilot/db/repositories/repository.py`의 open 공용형 `reserve_if_free`는 reserved_at 없는 결과를 반환한다.
- `server/src/nearpilot/core/decision/pipeline.py`는 이 값을 ExecutionContext에 넣는다.
- `server/src/nearpilot/db/repositories/execution_store.py`의 `link_command`는 공용형에도 요청 수신 시각을 reserved_at으로 저장한다.
- 같은 파일의 `execution_context`는 이 시각을 복원한다.
- `server/src/nearpilot/core/lifecycle/execution.py`의 `_attempt`는 기존 문맥과 다른 문맥을 거부한다.

### 영향과 요청할 결정

게이트웨이 경로의 성공 저장과 서버의 결과 재인계는 구분해야 한다.
시험은 성공을 저장해도 이 경고를 숨기지 않는다. 결과 인계의 누적 재시도를 확인해야 한다.
팀은 공용형 reserved_at의 생성·저장·복원 값을 맞출 위치를 정해야 한다.
수정 뒤 실제 브로커의 accepted·running·최종 결과를 중복 인계해도 문맥 충돌이 없는지 검증한다.

## FR-13·FR-18 시작 뒤 발견한 기기의 실행 가능 목록

기존 `RestoreService.restore`는 최초 inventory를 읽은 뒤 `_loaded=True`를 유지한다.
서버 시작 뒤 새로 발표한 기기는 `restore_not_loaded`로 남을 수 있다.
시험 도구는 새 기기를 등록한 뒤 기존 Runtime의 정상 종료·재시작으로 목록을 읽는다.
운영 서버의 동적 발견을 지원할지는 별도 변경으로 정한다.

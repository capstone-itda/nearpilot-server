# 실제 기기의 관측과 명령 연결 확인

- 확인 날짜: 2026-10-23이다.
- 기기는 ESP32-WROOM-32E 한 대다. 출력은 시리얼이다. 외부 센서는 없다.
- Android가 시험 비콘을 송출한다. 노트북과 기기는 휴대폰 핫스팟에 연결한다.
- 앱 인증, REST와 WebSocket은 사용하지 않았다. 기존 Runtime과 CORE를 시험 콘솔에서 호출했다.
- 설정과 원본 로그는 로컬 `server/var/node-lab-hotspot/`에 있다. 비밀번호는 Git에서 제외한다.

## 시각 실측

Windows NTP 응답의 leap 0과 stratum 5를 확인했다. ESP32에서 `CLOCK_SYNCED`를 확인했다.
서비스 재시작 뒤 미동기화 응답은 관리자 재동기화로 복구했다.

| 조건 | 기간 | 유효 표본 | USB 지연을 포함한 오차 구간의 최대 절댓값 |
|---|---|---|---|
| 보고 차단 상태에서 재부팅 | 150초 | 40개 | 0.0226713초 |
| 상태와 RSSI 보고, 실행 요청을 포함한 재부팅 | 95초 | 18개 | 0.6391453초 |

두 번째 구간의 큰 값은 측정 응답의 대기 시간까지 포함한다. 실제 시계 차이와 같다고 단정하지 않는다.
명시한 여유 0.25초를 더하고 올림해서 최종 시험 설정은 양쪽 모두 1초다.
초기 연결 요청 때는 첫 실측에 따른 0.3초를 사용했다. 부하 실측 뒤 최종 값을 넓혔다.
이 수치는 같은 환경의 짧은 시험에만 적용한다. 운영용 장시간 오차 보증이 아니다.
첫 실측에서 동기화 경과 시간이 다시 감소했다. 재동기화를 확인했다.
원본은 `clock-measurement-1.jsonl`, `clock-measurement-enabled.jsonl`, `clock-applied.json`이다.

## 실제 실행 경로

1. 기기는 Android 비콘의 RSSI를 수신했다.
2. 서버는 실제 RSSI와 물리 상태를 저장했다. 실행 가능 여부가 `ready=true`로 바뀌었다.
3. 서버는 합성 모델 `synthetic-1`로 확률을 계산했다. 실측 정확도 검증은 아니다.
4. 휴대폰을 가까이 옮긴 뒤 확률이 실행 기준 0.75를 넘었다.
5. 시험 콘솔은 `on`을 기존 CORE에 요청했다. 요청의 `p_max`는 약 0.90이었다.
6. 서버는 명령을 전송했다. 기기는 아래 로그로 명령을 접수하고 시리얼로 처리했다.
7. 기기는 물리 완료 근거가 없어 `unknown`을 보고했다. 서버는 기기를 `FAULT`로 격리했다.
8. 이후 `off` 요청은 `DENY / NO_CANDIDATE`였다. 장애 기기를 다시 실행하지 않았다.

```text
COMMAND_RECEIVED command_id=d6959eba-d2a8-4175-a831-835ac240d69c
COMMAND_RESULT command_id=d6959eba-d2a8-4175-a831-835ac240d69c state=accepted
COMMAND_RESULT command_id=d6959eba-d2a8-4175-a831-835ac240d69c state=running
COMMAND_HANDLED command_id=d6959eba-d2a8-4175-a831-835ac240d69c action=on output=serial_only physical_completion=unverified
COMMAND_RESULT command_id=d6959eba-d2a8-4175-a831-835ac240d69c state=unknown
```

서버 `command_result`도 같은 명령 ID와 `status=unknown`을 기록했다.
최종 요청 응답은 `DENY / EXEC_UNKNOWN`이다. 명령 전송 전 거부와 구분한다.
실제 조명 제어와 물리 성공은 확인하지 않았다. 센서 없이 성공 결과를 만들지 않았다.

## 남은 검증과 기존 문제

- 운영용 시각 오차에는 장시간, 시각원 단절과 변경 시험이 더 필요하다.
- 실제 근접 품질에는 실측 모델과 기기 두 대 이상의 비교가 더 필요하다.
- 시리얼 처리 뒤 물리 성공에는 출력 장치와 완료 관측 수단이 필요하다.
- 공용형 결과 재인계의 기존 `ValueError`도 재현됐다. 원인은 `KNOWN_ISSUES.md`를 따른다.
- 결과 재인계 경고를 완료 성공으로 숨기지 않았다. 시험 DB의 `FAULT`를 강제로 풀지 않았다.

## 소프트웨어 검증

- 전체 서버 검사: `2737 passed, 2 skipped, 1 xfailed`다. 실제 Mosquitto를 포함한다.
- 두 건은 Windows 파일명 제약으로 건너뛴다. 예상 실패는 기존 합성 모델 검사다.
- 펌웨어 `esp32dev` 빌드와 실제 COM6 업로드를 확인했다.
- 시각 구간 검사는 USB 지연 포함, 서버 시계 점프와 응답 지연 초과를 검증한다.

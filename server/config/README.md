# MQTT 실행 설정

서버와 ESP32는 같은 독립 AP의 Wi-Fi를 사용하고 MQTT 3.1.1로 통신한다.
휴대폰의 BLE 비콘을 ESP32가 수신하고 RSSI를 MQTT로 보고한다. 외부 인터넷은 필요 없다.

`server/`에서 `mosquitto -c config/mosquitto.conf`로 시작한다.
예시는 localhost에만 바인딩한다. 실제 기기를 연결할 때 listener의 주소를 서버의 AP 주소로 바꾼다.

`mosquitto.acl.example`의 기기별 계정과 토픽 ID를 실제 기기 목록에 맞춘다.
기기마다 다른 계정을 만들고 서버 전용 계정을 따로 쓴다. 각 계정은 명시한 토픽에만 접근한다.
기기 계정에 명령 쓰기 권한이나 다른 기기의 읽기·쓰기 권한을 추가하지 않는다.

브로커의 암호화된 비밀번호 파일은 `mosquitto_passwd -c config/.env.mosquitto-passwords nearpilot-server`로 만들고,
기기 계정은 `-c` 없이 같은 파일에 추가한다. 비밀번호는 대화형 입력으로 받는다.
이 파일과 서버 접속용 원문 비밀번호 파일(`.env.mqtt-password` 등)은 기존 `.env.*` ignore 규칙을 따른다.
둘은 서로 다른 파일이다. 실제 비밀번호 파일은 커밋하지 않는다.

IOT `MqttConfig`에는 주소·포트·클라이언트 ID·서버 계정·원문 비밀번호 파일 경로와
keepalive, 접속·시작·종료 제한 시간, 재접속 최소·최대 대기 시간을 모두 전달한다.
시간 값은 초 단위이고 임의 기본값이 없다. 종료한 연결 객체는 새로 만든다.
`start()`가 성공해야 서버 조립이 복원과 요청 접수를 진행할 수 있다.
브로커 접속만으로 기기 온라인·현재 연결·실행 가능 여부를 확정하지 않는다.

`on_report`와 `on_connection_lost`는 후속 IOT 작업에 짧게 인계하는 콜백이다.
이 콜백에서 CORE를 직접 호출하거나 명령 결과를 기다리지 않는다.
보고의 검증·재인계와 명령의 결과 대기는 서버 조립(`nearpilot.runtime`)이 연결한다.

실제 브로커 테스트는 `MOSQUITTO_EXECUTABLE`과 `MOSQUITTO_PASSWD_EXECUTABLE`에 실행 파일 경로를 주거나
두 명령을 PATH에 둔 뒤 `pytest tests/iot/test_broker_acl.py`로 실행한다.
테스트는 localhost의 임시 포트·임시 자격정보만 사용한다.

# 서버 실행 설정

`server.example.toml`을 `server.toml`로 복사해서 배포 값을 채운다. 운영용 `server.toml`은 커밋하지 않는다.
`server/`에서 `python -m nearpilot --config config/server.toml`로 실행한다. 단일 프로세스·단일 worker다.

- 상대 경로는 설정 파일이 있는 디렉터리 기준이다. 알 수 없는 키나 빠진 필수 키는 시작을 막는다.
- `[settings]`는 운영 수치(`Settings`)다. `version`만 필수이고 빠진 키는 시연용 초기값을 쓴다.
  미설정 임계값(스피커, 명시 지정)은 쓰지 않는다. 비워 두면 그 요청은 실행을 허가하지 않는다.
- `[runtime]`은 주기 작업 간격, SQLite 대기시간과 종료 대기 제한이다. 모두 선택이다.
- `[proximity.models]`에 장소별 모델 버전을 명시한다. 파일은 `<model_root>/<place_id>/<model_version>.json`이다.
- `[mqtt]`와 `[delivery]`는 모든 키가 필수다. `password_file`은 서버 계정의 원문 비밀번호 파일이다.
- `[delivery.clock_skew_sec]`에 없는 기기의 관측과 검사는 쓰지 않는다. 0은 같은 PC의 시뮬레이터에만 쓴다.
- 설정 변경은 재시작으로 적용한다. 진행 중인 요청과 점유는 저장한 설정 스냅샷을 그대로 쓴다.

시작은 설정·모델 검증, DB 준비, 근접 관측 복원, MQTT 구독 확인, 재시작 복원, 보존기간 정리 순서다.
하나라도 실패하면 요청을 받지 않고 끝낸다. 종료할 때 제한 시간을 넘긴 작업은 오류로 알리고,
미완료 기록은 다음 시작의 복원에서 다시 확인한다.

# MQTT 기기 시뮬레이터

실제 ESP32 없이 MQTT v1 계약을 확인하는 가상 기기다. 계약과 결정은 `specs/iot-node-simulator/spec.md`에 있다.
가상 완료와 가상 관측은 실제 센서의 검증 완료가 아니다.

## 실행

`server/`에서 실행한다. 상대 경로는 실행 위치를 기준으로 읽는다.

1. Mosquitto를 시작한다: `mosquitto -c config/mosquitto.conf`
2. 기기 계정의 ACL을 만든다: `python -m tools.node_sim acl --config tools/node_sim/fleet.example.json`
   출력을 브로커가 읽는 ACL 파일에 붙인다. 서버 계정 절은 `config/mosquitto.acl.example`을 따른다.
3. 기기마다 `mosquitto_passwd config/.env.mosquitto-passwords <계정>`으로 비밀번호를 추가한다.
   같은 비밀번호를 `password_file`에 쓴 경로(`config/.env.node-<기기>`)에 한 줄로 저장한다. 이 파일은 커밋하지 않는다.
4. 시작한다: `python -m tools.node_sim run --config tools/node_sim/fleet.example.json`
   `--node <기기 ID>`를 반복해 일부 기기만 실행할 수 있다.

실행 중에는 `door <기기 ID> open|close`로 사물함 문을 조작하고 `quit`으로 끝낸다.
열린 문은 잠금이 풀린 사물함에서만 열 수 있다. 문을 닫으면 다시 잠근다.

## 설정

| 키 | 뜻 |
|---|---|
| `broker` | 주소·포트·keepalive·재접속 최소·최대 대기다. 시간 값은 모두 필수다 |
| `settings` | `nearpilot.shared.config.Settings`의 덮어쓸 값이다. 상태·RSSI 보고 주기에 쓴다 |
| `devices[]` | 기기 ID·장소·기능·점유 방식·계정·비밀번호 파일·명령 기록 파일·시나리오다 |

예시는 장소 `place-a`의 사물함 3개·조명 3개·스피커 1개와 `place-b`의 무드등 1개다 (NFR-10).

`scenario`의 키는 다음과 같다. `actuation_sec`만 필수다.

| 키 | 뜻 |
|---|---|
| `actuation_sec` | 구동 시작부터 최종 결과까지의 시간 |
| `final` | 최종 결과 `succeeded`·`failed`·`unknown` |
| `drop_final_result` | 최종 결과를 저장하되 보고하지 않는다 (결과 유실) |
| `result_delay_sec` | 최종 결과의 보고를 늦춘다 |
| `light_observable` | `false`면 `light_on=null`이고 조명 명령은 `unknown`으로 끝난다 (IR 무드등) |
| `unmeasurable` | 측정하지 못하는 검사 항목. 근거에서 빠지고 결과는 `unknown`이다 |
| `sensor_fault` | 상태의 `sensor_fault=true`와 검사 결과 `fault` |
| `storage_error` | 명령 기록 저장이 실패한다. 구동하지 않는다 |
| `beacons` | 프레임마다 보고할 비콘 `uuid`·`major`·`minor`·`rssi` |

명령 기록 파일은 기기마다 다르다. 같은 파일로 새로 시작하면 재시작을 재현한다.
결과를 증명하지 못한 명령은 재시작 뒤 `unknown`으로 보고하고 다시 구동하지 않는다.

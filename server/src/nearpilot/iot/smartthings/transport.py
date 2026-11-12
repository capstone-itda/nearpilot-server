"""SmartThings REST 호출의 좁은 접점 (specs/iot-smartthings-adapter, specs/iot-smartthings-companion).

실제 HTTP 구현은 `iot/smartthings/http.py`다. 테스트는 이 `Protocol`을 만족하는 fake도 쓴다.
오류 메시지에는 토큰과 `deviceId`를 담지 않는다 (NFR-08).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol, runtime_checkable


class SmartThingsTransportError(Exception):
    """SmartThings 호출이 실패했다. 명령 전송에서는 보내지 못한 것으로 본다."""


class SmartThingsNotSent(SmartThingsTransportError):
    """명령을 확실히 보내지 못했다: 연결 실패, 401, 그 밖의 4xx, 토큰 없음 (spec 결정 14)."""


class SmartThingsMaybeSent(SmartThingsTransportError):
    """명령을 보냈을 수 있다: 응답 시간 초과, 5xx. 확인 조회를 계속한다 (spec 결정 14)."""


@runtime_checkable
class SmartThingsTransport(Protocol):
    """기기 하나에 대한 명령 전송과 속성 조회. 기기 구성요소는 `main`이다."""

    def post_command(self, device_id: str, capability: str, command: str,
                     arguments: Sequence[Any]) -> None: ...

    def get_attribute(self, device_id: str, capability: str, attribute: str) -> Any: ...

"""SmartThings REST 호출의 좁은 접점 (specs/iot-smartthings-adapter).

실제 HTTP 구현은 이 작업의 범위가 아니다(OAuth 작업이 토큰 주입과 함께 만든다).
테스트는 이 `Protocol`을 만족하는 fake만 쓴다.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable


class SmartThingsTransportError(Exception):
    """SmartThings 호출이 실패했다. 전송 실패와 확인 조회 실패를 구분하지 않는다."""


@runtime_checkable
class SmartThingsTransport(Protocol):
    """기기 하나에 대한 명령 전송과 속성 조회."""

    def post_command(self, device_id: str, capability: str, command: str,
                      arguments: Mapping[str, Any] | None) -> None: ...

    def get_attribute(self, device_id: str, capability: str, attribute: str) -> Any: ...

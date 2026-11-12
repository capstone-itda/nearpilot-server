"""SmartThings REST transport over urllib (FR-06·FR-07, specs/iot-smartthings-companion 결정 14·15).

- 요청과 응답 본문은 UTF-8로 쓰고 읽는다 (`architecture.md` "명령 전송과 결과 확인").
- 접근 토큰은 요청마다 토큰 파일(JSON 객체의 `access_token`)에서 다시 읽는다. 다른 작업이 같은 파일을
  교체 저장하면 재시작 없이 반영된다.
- 오류 분류: 연결 실패, 401, 그 밖의 4xx, 토큰 없음은 "보내지 못함"(`SmartThingsNotSent`),
  응답 시간 초과와 5xx는 "보냈을 수 있음"(`SmartThingsMaybeSent`)이다. 명령 응답 본문은 쓰지 않는다.
- 토큰과 `deviceId`는 예외 메시지, 로그, 예외 연쇄에 남기지 않는다 (NFR-08). urllib 예외는 URL에
  `deviceId`를 담으므로 `from None`으로 끊는다.
"""

from __future__ import annotations

import json
import math
import socket
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from nearpilot.iot.smartthings.transport import (
    SmartThingsMaybeSent, SmartThingsNotSent, SmartThingsTransportError,
)

API_BASE = "https://api.smartthings.com/v1"
COMPONENT = "main"

Opener = Callable[..., Any]  # urllib.request.urlopen(request, timeout=...) 와 같은 모양


class HttpSmartThingsTransport:
    """`SmartThingsTransport`의 HTTP 구현."""

    def __init__(self, *, token_file: Path, request_timeout_sec: float, api_base: str = API_BASE,
                 opener: Opener = urllib.request.urlopen):
        if type(request_timeout_sec) not in (int, float) or not math.isfinite(request_timeout_sec) \
                or request_timeout_sec <= 0:
            raise ValueError("request_timeout_sec 는 양수여야 한다")
        self._token_file = Path(token_file)
        self._timeout = request_timeout_sec
        self._base = api_base.rstrip("/")
        self._open = opener

    def post_command(self, device_id: str, capability: str, command: str, arguments: Sequence[Any]) -> None:
        body = {"commands": [{"component": COMPONENT, "capability": capability, "command": command,
                              "arguments": list(arguments)}]}
        data = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self._request("POST", f"/devices/{_quote(device_id)}/commands", data, what=f"{capability}.{command}")

    def get_attribute(self, device_id: str, capability: str, attribute: str) -> Any:
        path = f"/devices/{_quote(device_id)}/components/{COMPONENT}/capabilities/{_quote(capability)}/status"
        raw = self._request("GET", path, None, what=f"{capability} status")
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise SmartThingsTransportError(f"SmartThings {capability} 상태 응답이 JSON 이 아니다") from None
        if not isinstance(body, dict):
            raise SmartThingsTransportError(f"SmartThings {capability} 상태 응답 형식이 다르다")
        entry = body.get(attribute)
        if entry is None:
            return None
        if not isinstance(entry, dict):
            raise SmartThingsTransportError(f"SmartThings {capability}.{attribute} 형식이 다르다")
        return entry.get("value")

    # ── 공통 ─────────────────────────────────────────────────────

    def _token(self) -> str:
        try:
            data = json.loads(self._token_file.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError):
            raise SmartThingsNotSent("SmartThings 토큰 파일을 읽지 못했다") from None
        token = data.get("access_token") if isinstance(data, dict) else None
        if not isinstance(token, str) or not token.strip():
            raise SmartThingsNotSent("SmartThings 토큰 파일에 access_token 이 없다")
        return token.strip()

    def _request(self, method: str, path: str, data: bytes | None, *, what: str) -> bytes:
        headers = {"Authorization": f"Bearer {self._token()}", "Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json; charset=utf-8"
        request = urllib.request.Request(self._base + path, data=data, method=method, headers=headers)
        try:
            with self._open(request, timeout=self._timeout) as response:
                return response.read()
        except urllib.error.HTTPError as error:
            status = error.code
            error.close()
            if status == 401:
                raise SmartThingsNotSent(f"SmartThings {what}: 인증 실패(401)") from None
            if 400 <= status < 500:
                raise SmartThingsNotSent(f"SmartThings {what}: 요청 거부({status})") from None
            raise SmartThingsMaybeSent(f"SmartThings {what}: 서버 오류({status})") from None
        except urllib.error.URLError as error:
            if isinstance(error.reason, (TimeoutError, socket.timeout)):
                raise SmartThingsMaybeSent(f"SmartThings {what}: 응답 시간 초과") from None
            raise SmartThingsNotSent(f"SmartThings {what}: 연결 실패") from None
        except (TimeoutError, socket.timeout):
            raise SmartThingsMaybeSent(f"SmartThings {what}: 응답 시간 초과") from None
        except OSError:
            # 연결 뒤 끊김 등. 요청이 나갔는지 알 수 없다.
            raise SmartThingsMaybeSent(f"SmartThings {what}: 통신 오류") from None


def _quote(value: str) -> str:
    return urllib.parse.quote(value, safe="")

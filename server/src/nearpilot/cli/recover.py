"""`nearpilot recover`: 소유자가 FAULT 복구 REST 두 경로를 부른다 (specs/api-rest-recovery/spec.md 6절 "복구 명령").

  recover status  NODE_ID              GET  /admin/nodes/{node_id}/recovery
  recover approve NODE_ID RECOVERY_ID  POST /admin/nodes/{node_id}/recovery/{recovery_id}/approve

DB를 열지 않고 HTTP만 쓴다. 준비 판단과 승인 결과는 서버가 정하고, 명령은 응답을 사람이 읽는 줄로 옮긴다 (NFR-07).
토큰은 `--token-file`로만 받는다. 출력, 오류 문구, 예외 메시지에 쓰지 않는다 (NFR-08).
종료 코드: 0 성공, 1 서버가 거부했거나 복구가 끝나지 않았거나 서버에 연결하지 못했다, 2 입력 오류.
"""

from __future__ import annotations

import argparse
import http.client
import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import quote

from nearpilot.cli import EXIT_FAILED, EXIT_INVALID, EXIT_OK
from nearpilot.cli.common import CliError, load_cli_config

_TIMEOUT_SEC = 10  # 요청 하나를 기다리는 시간. 서버의 운영 수치가 아니라 이 명령이 서버를 기다리는 한계다
_NONE = "없음"


def register(subparsers: argparse._SubParsersAction, parent: argparse.ArgumentParser) -> None:
    parser = subparsers.add_parser(
        "recover", parents=[parent], help="FAULT 기기의 복구 상태를 보고 승인한다",
        description="서버의 복구 REST를 부른다. DB를 열지 않는다. 소유자 토큰은 --token-file 로 받는다.")
    actions = parser.add_subparsers(dest="recover_command", required=True)

    status = actions.add_parser(
        "status", parents=[parent], help="복구 준비 상태를 본다",
        description="기기의 복구 준비 상태를 본다. 상태를 바꾸지 않는다. ready 이면 실행할 approve 명령을 함께 쓴다.")
    status.add_argument("node_id", metavar="NODE_ID", help="복구할 기기 ID")
    _add_server_options(status)
    status.set_defaults(handler=_status)

    approve = actions.add_parser(
        "approve", parents=[parent], help="복구를 승인한다",
        description="status 가 쓴 복구 ID로 복구를 승인한다. 종료 코드 0 은 복구 완료다. "
                    "1 은 서버가 거부했거나 아직 완료가 아니거나 연결하지 못한 것이다.")
    approve.add_argument("node_id", metavar="NODE_ID", help="복구할 기기 ID")
    approve.add_argument("recovery_id", metavar="RECOVERY_ID", help="status 가 쓴 복구 ID")
    _add_server_options(approve)
    approve.set_defaults(handler=_approve)


def _add_server_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--token-file", metavar="PATH", required=True,
                        help="소유자 토큰이 든 파일. 토큰을 명령줄 인자로 받지 않는다 (셸 기록에 남는다)")
    parser.add_argument("--server", metavar="URL", default=None,
                        help="서버 주소 (예: http://127.0.0.1:8000). 없으면 설정 파일의 [http]로 만든다")


def server_url(args: argparse.Namespace) -> str:
    """요청을 보낼 서버 주소. `--server`가 있으면 끝의 `/`만 지워 그대로 쓰고, 없으면 설정의 `[http]`로 만든다.

    설정의 `host`가 `0.0.0.0`(모든 주소에서 받는다)이면 접속할 수 없는 주소라서 이 컴퓨터의 `127.0.0.1`로 부른다.
    `--server`가 http(s) 주소가 아니거나 설정이 잘못되면 `CliError`(종료 코드 2)다.
    """
    if args.server is not None:
        if not args.server.lower().startswith(("http://", "https://")):
            raise CliError(f"--server 는 http:// 나 https:// 로 시작하는 주소여야 한다: {args.server}", EXIT_INVALID)
        return args.server.rstrip("/")
    http_config = load_cli_config(args.config, require_models=False).http
    host = "127.0.0.1" if http_config.host == "0.0.0.0" else http_config.host
    return f"http://{host}:{http_config.port}"


def _status(args: argparse.Namespace) -> int:
    token = _read_token(args.token_file)
    base = server_url(args)
    status, body = _send("GET", base, f"/admin/nodes/{_segment(args.node_id)}/recovery", token)
    body = _accepted(status, body, (200,))
    recovery_id = body.get("recovery_id")
    print(f"상태: {body.get('status')}")
    print(f"복구 ID: {recovery_id or _NONE}")
    print(f"부족한 조건: {', '.join(body.get('blockers') or ()) or _NONE}")
    if body.get("status") == "ready" and recovery_id:
        print(f"nearpilot recover approve {args.node_id} {recovery_id} --token-file {args.token_file}")
    return EXIT_OK


def _approve(args: argparse.Namespace) -> int:
    token = _read_token(args.token_file)
    base = server_url(args)
    path = f"/admin/nodes/{_segment(args.node_id)}/recovery/{_segment(args.recovery_id)}/approve"
    status, body = _send("POST", base, path, token)
    body = _accepted(status, body, (200, 202))
    if status == 202:  # 저장 결과를 확인하는 중이다. 완료가 아니다
        raise CliError(
            f"복구는 아직 완료가 아니다. 서버가 저장 결과를 확인하는 중이다 (reason_code: {body.get('reason_code')}). "
            "잠시 뒤 같은 approve 명령을 다시 실행한다", EXIT_FAILED)
    print("복구를 완료했다")
    print(f"완료 시각: {body.get('completed_at') or _NONE}")
    return EXIT_OK


def _segment(value: str) -> str:
    """경로의 한 칸에 넣을 값. `/`도 인코딩한다."""
    return quote(value, safe="")


def _read_token(path: str) -> str:
    """토큰 파일을 읽는다. 앞뒤 공백은 지운다. 쓸 수 없는 파일이면 `CliError`(종료 코드 2)이고 요청을 보내지 않는다.

    토큰 하나만 든 파일이어야 한다. 서버는 공백이 든 토큰을 받지 않고, 줄바꿈이나 한글이 든 값은 HTTP 헤더에 쓸 수 없다.
    헤더에 쓸 수 없는 값은 표준 라이브러리가 값을 그대로 담은 예외(`ValueError`)를 내므로 보내기 전에 걸러 낸다.
    """
    try:
        token = Path(path).read_text(encoding="utf-8-sig").strip()  # 메모장이 붙이는 BOM을 허용한다
    except UnicodeDecodeError:
        raise CliError(f"토큰 파일이 UTF-8 텍스트가 아니다: {path}", EXIT_INVALID) from None
    except OSError:
        raise CliError(f"토큰 파일을 읽을 수 없다: {path}", EXIT_INVALID) from None
    if not token:
        raise CliError(f"토큰 파일이 비어 있다: {path}", EXIT_INVALID)
    if not all("!" <= char <= "~" for char in token):  # 공백, 제어 문자, ASCII 밖의 글자가 없는 보이는 문자
        raise CliError(f"토큰 파일에는 공백과 줄바꿈이 없는 ASCII 토큰 하나만 있어야 한다: {path}", EXIT_INVALID)
    return token


def _send(method: str, base: str, path: str, token: str) -> tuple[int, dict[str, Any] | None]:
    """요청을 보내고 (상태 코드, JSON 객체인 본문)을 돌려준다. 본문이 JSON 객체가 아니면 None이다.

    4xx·5xx도 상태 코드와 본문으로 돌려준다. 서버와 통신하지 못하면 `CliError`(종료 코드 1)다.
    """
    request = urllib.request.Request(base + path, method=method, headers={"Authorization": f"Bearer {token}"})
    try:
        try:
            with urllib.request.urlopen(request, timeout=_TIMEOUT_SEC) as response:
                status, raw = response.status, response.read()
        except urllib.error.HTTPError as error:  # 4xx, 5xx. URLError의 하위 클래스라서 먼저 받는다
            with error:  # HTTPError는 응답이다. 읽은 뒤 닫는다
                status, raw = error.code, error.read()
    except (urllib.error.URLError, http.client.HTTPException, OSError) as error:
        # URLError는 원인을 reason에 담는다. 원인 문구에 토큰은 들어 있지 않다
        raise CliError(f"서버에 연결하지 못했다: {base} ({getattr(error, 'reason', error)})", EXIT_FAILED) from None
    try:
        body = json.loads(raw)
    except ValueError:  # JSON이 아니거나 UTF-8이 아니다
        return status, None
    return status, body if isinstance(body, dict) else None


def _accepted(status: int, body: dict[str, Any] | None, accepted: tuple[int, ...]) -> dict[str, Any]:
    """정상 응답의 본문을 돌려준다. 4xx·5xx나 읽을 수 없는 응답은 `CliError`(종료 코드 1)다.

    4xx·5xx는 본문의 `error` 사유 코드를 한 줄에 쓴다. 본문이 JSON이 아니거나 사유가 없으면 상태 코드만 쓴다.
    """
    if status >= 400:
        reason = body.get("error") if body else None
        suffix = f": {reason}" if isinstance(reason, str) else ""
        raise CliError(f"서버가 요청을 처리하지 못했다 (HTTP {status}){suffix}", EXIT_FAILED)
    if status not in accepted or body is None:
        raise CliError(f"서버 응답을 해석하지 못했다 (HTTP {status})", EXIT_FAILED)
    return body

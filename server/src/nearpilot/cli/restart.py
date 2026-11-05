"""`nearpilot restart`: 소유자가 기기 원격 재시작 REST를 부른다 (#235, specs/iot-node-restart).

  restart NODE_ID   POST /admin/nodes/{node_id}/restart

DB를 열지 않고 HTTP만 쓴다. 소유권과 발행 결과는 서버가 정한다 (NFR-07).
성공은 브로커 발행이다. 재시작 완료가 아니다. 완료는 기기의 새 연결(대시보드, 서버 로그)로 확인한다.
토큰은 `--token-file`로만 받고 출력에 쓰지 않는다 (NFR-08).
종료 코드: 0 발행, 1 서버가 거부했거나 연결하지 못했다, 2 입력 오류.
"""

from __future__ import annotations

import argparse

from nearpilot.cli import EXIT_OK
from nearpilot.cli.recover import _accepted, _add_server_options, _read_token, _segment, _send, _unreadable, server_url


def register(subparsers: argparse._SubParsersAction, parent: argparse.ArgumentParser) -> None:
    parser = subparsers.add_parser(
        "restart", parents=[parent], help="기기를 원격으로 재시작한다",
        description="서버가 기기의 현재 MQTT 연결에 재시작 요청을 보낸다. 장소 소유자만 쓸 수 있다. "
                    "종료 코드 0 은 요청을 보냈다는 뜻이고 재시작 완료가 아니다.")
    parser.add_argument("node_id", metavar="NODE_ID", help="재시작할 기기 ID")
    _add_server_options(parser)
    parser.set_defaults(handler=_restart)


def _restart(args: argparse.Namespace) -> int:
    token = _read_token(args.token_file)
    base = server_url(args)
    status, body = _send("POST", base, f"/admin/nodes/{_segment(args.node_id)}/restart", token)
    body = _accepted(status, body, (202,))
    if not isinstance(body.get("restart_id"), str):
        raise _unreadable(status)
    print("재시작 요청을 보냈다. 재시작 완료가 아니다")
    print(f"재시작 ID: {body['restart_id']}")
    print(f"만료 시각: {body.get('expires_at')}")
    print("기기가 새 연결로 발표하면 재시작이 끝난 것이다 (대시보드나 시리얼 로그의 NODE_READY)")
    return EXIT_OK

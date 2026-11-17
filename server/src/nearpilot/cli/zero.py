"""`nearpilot zero`: 소유자가 측정 영점(#307)과 영점 재탐색(#305) REST를 부른다
(specs/core-proximity-common-model/spec.md 6절 결정 23~25).

  zero measure NODE_ID          POST /admin/nodes/{node_id}/zero/measure
  zero reset NODE_ID            POST /admin/nodes/{node_id}/zero/reset
  zero reset --place PLACE_ID   POST /admin/places/{place_id}/zero/reset

DB를 열지 않고 HTTP만 쓴다. 소유권, 측정 값, 저장은 서버가 정한다 (NFR-07).
측정은 서버가 설정의 측정 시간만큼 기다린 뒤 응답한다. 명령은 그보다 `_MARGIN_SEC` 더 기다린다.
토큰은 `--token-file`로만 받고 출력에 쓰지 않는다 (NFR-08).
종료 코드: 0 성공, 1 서버가 거부했거나 연결하지 못했다, 2 입력 오류.
"""

from __future__ import annotations

import argparse

from nearpilot.cli import EXIT_FAILED, EXIT_INVALID, EXIT_OK
from nearpilot.cli.common import CliError, load_cli_config
from nearpilot.cli.recover import _accepted, _add_server_options, _read_token, _segment, _send, _unreadable, server_url
from nearpilot.shared.config import Settings

_MARGIN_SEC = 20  # 측정 시간 뒤에 서버의 계산과 저장을 기다리는 여유. 서버의 운영 수치가 아니라 이 명령의 한계다


def register(subparsers: argparse._SubParsersAction, parent: argparse.ArgumentParser) -> None:
    parser = subparsers.add_parser(
        "zero", parents=[parent], help="기기별 영점을 재거나 버린다",
        description="서버의 영점 REST를 부른다. DB를 열지 않는다. 장소 소유자만 쓸 수 있다.")
    actions = parser.add_subparsers(dest="zero_command", required=True)

    measure = actions.add_parser(
        "measure", parents=[parent], help="측정 영점을 잰다",
        description="소유자 비콘을 거치대에 고정해 기기에서 설정의 측정 거리(zero_measure_distance_m)에 두고 실행한다. "
                    "서버가 측정 시간(zero_measure_sec) 동안 그 기기가 들은 RSSI로 측정 영점을 저장한다. "
                    "측정하는 동안 비콘과 기기 사이에 서지 않는다.")
    measure.add_argument("node_id", metavar="NODE_ID", help="잴 기기 ID")
    _add_server_options(measure)
    measure.set_defaults(handler=_measure)

    reset = actions.add_parser(
        "reset", parents=[parent], help="영점을 버리고 자동 영점을 다시 찾게 한다",
        description="기기 하나나 장소의 모든 기기의 영점(측정 영점 포함)을 버린다. 서버가 장소의 관측으로 자동 영점을 다시 찾는다. "
                    "다시 찾을 때까지 그 기기는 영점이 없는 기기로 판정한다.")
    target = reset.add_mutually_exclusive_group(required=True)
    target.add_argument("node_id", metavar="NODE_ID", nargs="?", help="영점을 버릴 기기 ID")
    target.add_argument("--place", metavar="PLACE_ID", default=None, help="모든 기기의 영점을 버릴 장소 ID")
    _add_server_options(reset)
    reset.set_defaults(handler=_reset)


def _measure_sec(args: argparse.Namespace) -> float:
    """설정 파일의 측정 시간. 설정 파일을 읽지 못하면 기본값을 쓴다 (`--server`로 다른 서버를 부를 수 있다)."""
    try:
        return load_cli_config(args.config, require_models=False).settings.zero_measure_sec
    except CliError:
        if args.server is None:
            raise
        return Settings().zero_measure_sec


def _measure(args: argparse.Namespace) -> int:
    token = _read_token(args.token_file)
    base = server_url(args)
    wait = _measure_sec(args)
    print(f"측정 중이다. 약 {wait:g}초 동안 비콘과 기기를 움직이지 않는다")
    path = f"/admin/nodes/{_segment(args.node_id)}/zero/measure"
    status, body = _send("POST", base, path, token, timeout=wait + _MARGIN_SEC)
    if status == 422 and body is not None and body.get("error") == "NOT_ENOUGH_FRAMES":
        raise CliError(
            f"측정 구간에 그 기기가 들은 비콘 프레임이 모자라다 (프레임 {body.get('frames')}개). "
            "비콘이 켜져 있고 서버에 등록한 활성 비콘인지, 기기가 rssi를 보고하는지 확인한다", EXIT_FAILED)
    body = _accepted(status, body, (200,))
    if not isinstance(body.get("zero_db"), (int, float)) or not isinstance(body.get("revision"), int):
        raise _unreadable(status)
    print(f"측정 영점: {body['zero_db']:+.2f} dB (개정 {body['revision']})")
    print(f"중앙값: {body.get('median_dbm'):.1f} dBm, 기준값: {body.get('reference_dbm')} dBm, "
          f"측정 거리: {body.get('distance_m')} m, 프레임 {body.get('frames')}개")
    return EXIT_OK


def _reset(args: argparse.Namespace) -> int:
    token = _read_token(args.token_file)
    base = server_url(args)
    if args.place is not None:
        path = f"/admin/places/{_segment(args.place)}/zero/reset"
    elif args.node_id:
        path = f"/admin/nodes/{_segment(args.node_id)}/zero/reset"
    else:
        raise CliError("NODE_ID 나 --place 중 하나를 쓴다", EXIT_INVALID)
    status, body = _send("POST", base, path, token)
    body = _accepted(status, body, (200,))
    nodes = body.get("nodes")
    if not isinstance(nodes, list) or not isinstance(body.get("revision"), int):
        raise _unreadable(status)
    print(f"영점을 버렸다: {', '.join(map(str, nodes))} (개정 {body['revision']})")
    print("서버가 장소의 관측으로 자동 영점을 다시 찾는다. 찾을 때까지 그 기기는 영점이 없는 기기로 판정한다")
    return EXIT_OK

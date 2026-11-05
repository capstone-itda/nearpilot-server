"""`nearpilot provision`: 시험 폴더의 기기 하나에 USB로 접속 설정을 주입한다 (specs/core-ops-setup/spec.md).

`tools/node_lab/provision.py`의 `build_values`·`run`을 부른다. 주입 절차, 기록(`provision.jsonl`)과 다시 읽은 값의 확인은 그 도구와 같다.
`--dir`이 필요하다. `--time-server`를 생략하면 시험 폴더 `server.toml`의 `[mqtt].host`다.
pyserial이 없으면 포트를 열지 않고 설치 명령을 안내한다. pyserial은 서버 의존성이 아니다.
종료 코드: 주입하고 다시 읽은 값이 맞으면 0, 기기가 거부하거나 응답하지 않거나 다시 읽은 값이 다르면 1,
`--dir`이 없거나 시험 폴더의 값이 잘못됐거나 pyserial이 없으면 2.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from nearpilot.cli import EXIT_FAILED, EXIT_INVALID, EXIT_OK
from nearpilot.cli.common import CliError
from tools.node_lab import provision as provision_tool


def register(subparsers: argparse._SubParsersAction, parent: argparse.ArgumentParser) -> None:
    parser = subparsers.add_parser(
        "provision", parents=[parent], help="기기 하나에 USB로 Wi-Fi·MQTT·시각원 설정을 주입한다",
        description="시험 폴더(--dir)의 값으로 기기 하나에 USB 시리얼로 접속 설정을 주입하고 다시 읽어 확인한다. "
                    "pyserial이 필요하다.")
    parser.add_argument("--node", required=True, help="기기 ID. 시험 폴더 lab.json의 nodes에 있어야 한다")
    parser.add_argument("--port", required=True, help="시리얼 포트 (예: COM6)")
    parser.add_argument("--time-server", default=None, help="시각원 주소. 기본값: 시험 폴더 server.toml의 [mqtt].host")
    parser.add_argument("--time-trust-sec", type=float, required=True, help="시각 신뢰 기간(초)")
    parser.add_argument("--time-resync-sec", type=float, required=True, help="재동기화 간격(초)")
    parser.add_argument("--wifi-file", type=Path, default=None, help="기본값: 시험 폴더의 .env.wifi.json")
    parser.add_argument("--timeout", type=float, default=10.0, help="기기 응답 한도(초)")
    parser.add_argument("--begin-interval", type=float, default=1.0,
                        help="부팅 중 잃은 CONFIG_BEGIN을 다시 보내는 간격(초)")
    parser.set_defaults(handler=_provision)


def _provision(args: argparse.Namespace) -> int:
    if getattr(args, "dir", None) is None:
        raise CliError("--dir로 시험 폴더를 준다", EXIT_INVALID)
    try:
        import serial  # noqa: F401  포트를 열기 전에 확인한다
    except ImportError:
        raise CliError("pyserial이 필요하다: pip install pyserial", EXIT_INVALID) from None
    folder = Path(args.dir).resolve()
    try:
        time_server = args.time_server or provision_tool.default_time_server(folder)
        values = provision_tool.build_values(folder, args.node, time_server=time_server,
                                             trust_sec=args.time_trust_sec, resync_sec=args.time_resync_sec,
                                             wifi_file=args.wifi_file)
    except (OSError, KeyError, ValueError) as error:  # tomllib·JSON 오류도 ValueError다
        raise CliError(f"시험 폴더의 값이 잘못됐다: {error}", EXIT_INVALID) from None
    ok = provision_tool.run(folder, args.node, args.port, values, timeout=args.timeout,
                            begin_interval=args.begin_interval)
    return EXIT_OK if ok else EXIT_FAILED

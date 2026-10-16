"""운영 명령 `nearpilot` (specs/core-ops-cli/spec.md 6절 "명령과 파일").

`pip install -e .` 뒤 `nearpilot <하위 명령>`으로 실행한다. 하위 명령은 모듈마다 `register`로 파서에 붙고,
`handler(args) -> int`를 건다. `main`은 종료 코드를 돌려주고 `sys.exit`를 부르지 않는다.
종료 코드: 0 성공, 1 실행 중 거부·실패, 2 입력 오류, 130 Ctrl+C. 기존 수집 도구와 같다.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_INVALID = 2
EXIT_INTERRUPTED = 130
DEFAULT_CONFIG = "config/server.toml"  # 실행 폴더 기준


def build_parser() -> argparse.ArgumentParser:
    """`--config`를 하위 명령 앞뒤 어디에 써도 되는 파서. 하위 명령은 필수다."""
    # 하위 명령 모듈이 위의 종료 코드를 가져다 쓰므로 상수를 정한 뒤에 가져온다.
    from nearpilot.cli import collect, seed, serve, split

    parser = argparse.ArgumentParser(prog="nearpilot", description="NearPilot 서버를 실행하고 운영한다.")
    parser.add_argument("--config", metavar="PATH", default=DEFAULT_CONFIG,
                        help=f"서버 TOML 설정 파일 (기본 {DEFAULT_CONFIG}, 실행 폴더 기준)")
    # 하위 명령 뒤의 --config. 쓰지 않았을 때(SUPPRESS) 앞쪽 값을 덮어쓰지 않는다.
    parent = argparse.ArgumentParser(add_help=False)
    parent.add_argument("--config", metavar="PATH", default=argparse.SUPPRESS,
                        help="서버 TOML 설정 파일. 하위 명령 앞에 써도 된다")
    subparsers = parser.add_subparsers(dest="command", required=True)
    serve.register(subparsers, parent)
    seed.register(subparsers, parent)
    collect.register(subparsers, parent)
    split.register(subparsers, parent)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """명령줄을 읽어 하위 명령을 실행하고 종료 코드를 돌려준다. `argv`가 None이면 `sys.argv[1:]`을 읽는다."""
    from nearpilot.cli.common import CliError

    try:
        args = build_parser().parse_args(argv)
    except SystemExit as exit_request:  # argparse가 --help에는 0으로, 인자 오류에는 2로 끝낸다
        return exit_request.code
    try:
        return args.handler(args)
    except CliError as error:
        print(error.message, file=sys.stderr)
        return error.exit_code
    except KeyboardInterrupt:
        print("Ctrl+C로 멈췄다.", file=sys.stderr)
        return EXIT_INTERRUPTED

"""`nearpilot serve`: 서버를 실행한다 (specs/core-ops-cli/spec.md 6절 "serve").

`python -m nearpilot`과 같은 단일 프로세스·단일 worker 서버다.
`--collect-only`는 근접 모델 없이 시작해서 RSSI와 기기 보고만 저장한다. MCP를 열지 않는다 (FR-06).
수집 전용은 명령 옵션으로만 켠다. 설정 파일에 두지 않는다.
"""

from __future__ import annotations

import argparse
import logging

import uvicorn

from nearpilot.cli import EXIT_OK
from nearpilot.cli.common import load_cli_config
from nearpilot.runtime import Runtime, ServerConfig, create_app


def register(subparsers: argparse._SubParsersAction, parent: argparse.ArgumentParser) -> None:
    parser = subparsers.add_parser("serve", parents=[parent], help="서버를 실행한다",
                                   description="서버를 실행한다. 설정이 잘못되면 요청을 받지 않고 끝낸다.")
    parser.add_argument("--collect-only", action="store_true",
                        help="근접 모델 없이 시작한다. RSSI와 기기 보고를 저장하고 MCP는 열지 않는다")
    parser.set_defaults(handler=_serve)


def _serve(args: argparse.Namespace) -> int:
    # 모델 표가 필요한 것은 일반 serve뿐이다. 수집 전용은 첫 모델을 만들기 전에도 뜬다.
    config = load_cli_config(args.config, require_models=not args.collect_only)
    return run_server(config, collect_only=args.collect_only)


def run_server(config: ServerConfig, *, collect_only: bool) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    app = create_app(Runtime(config, collect_only=collect_only))
    uvicorn.run(app, host=config.http.host, port=config.http.port, workers=1, lifespan="on",
                log_config=None,
                timeout_graceful_shutdown=max(1, int(config.runtime.shutdown_timeout_sec)))
    return EXIT_OK

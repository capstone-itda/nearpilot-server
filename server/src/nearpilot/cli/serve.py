"""`nearpilot serve`: 서버를 실행한다 (specs/core-ops-cli/spec.md 6절 "serve").

`python -m nearpilot`과 같은 단일 프로세스·단일 worker 서버다.
`--collect-only`는 근접 모델 없이 시작해서 RSSI와 기기 보고만 저장한다. MCP를 열지 않는다 (FR-06).
수집 전용은 명령 옵션으로만 켠다. 설정 파일에 두지 않는다.
"""

from __future__ import annotations

import argparse
import logging

import uvicorn

from nearpilot.cli import EXIT_FAILED, EXIT_OK
from nearpilot.cli.common import CliError, load_cli_config
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
    # 새로 받은 server/에도 var/가 없다. DB와 감사 JSONL의 폴더는 serve가 처음 시작할 때 만든다.
    for folder in {config.database.path.parent, config.database.audit_jsonl.parent}:
        folder.mkdir(parents=True, exist_ok=True)
    app = create_app(Runtime(config, collect_only=collect_only))
    try:
        uvicorn.run(app, host=config.http.host, port=config.http.port, workers=1, lifespan="on",
                    log_config=None,
                    timeout_graceful_shutdown=max(1, int(config.runtime.shutdown_timeout_sec)))
    except SystemExit as exit_request:  # uvicorn은 시작에 실패하면 sys.exit(3)을 부른다
        if exit_request.code not in (None, 0):
            raise CliError("서버를 시작하지 못했다. 위의 로그를 본다", EXIT_FAILED) from None
    return EXIT_OK

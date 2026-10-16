"""서버 실행 진입점: `server/`에서 `python -m nearpilot --config config/server.toml`.

단일 프로세스와 단일 worker 로 실행한다. 설정이 잘못되면 요청을 받지 않고 끝낸다.
"""

from __future__ import annotations

import argparse
import logging
import sys

import uvicorn

from nearpilot.runtime import ConfigError, Runtime, create_app, load_config

log = logging.getLogger("nearpilot")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m nearpilot")
    parser.add_argument("--config", required=True, help="서버 TOML 설정 파일")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        config = load_config(args.config)
    except ConfigError as error:
        log.error("설정 오류: %s", error)
        return 2
    app = create_app(Runtime(config))
    uvicorn.run(app, host=config.http.host, port=config.http.port, workers=1, lifespan="on",
                log_config=None,
                timeout_graceful_shutdown=max(1, int(config.runtime.shutdown_timeout_sec)))
    return 0


if __name__ == "__main__":
    sys.exit(main())

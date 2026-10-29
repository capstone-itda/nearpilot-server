"""`nearpilot dashboard`: 대시보드 개발 서버(`npm run dev`)를 띄운다 (specs/core-ops-cli/spec.md 6절 "시험 폴더와 실행 보조 명령").

대시보드 폴더에서 `npm run dev -- --host 0.0.0.0`을 실행한다 — 같은 AP의 다른 기기에서도 대시보드를 연다 (NFR-12).
환경 변수 `NEARPILOT_SERVER=http://127.0.0.1:<[http].port>`를 더하고 다른 환경 변수는 그대로 넘긴다 (`dashboard/vite.config.ts`의 프록시가 읽는다).
대시보드 폴더는 `--dashboard-dir`로 받고, 없으면 패키지 위치에서 찾은 저장소의 `dashboard/`다 (`pip install -e`로 설치한 경우).
`npm install`은 하지 않는다. `npm`은 `shutil.which`로 찾는다 (Windows는 `npm.cmd`이고 찾은 경로를 그대로 실행한다).
서버 상태를 바꾸지 않는다. 출력은 터미널에 그대로 둔다.
종료 코드: Ctrl+C 130, npm이 0이 아닌 값으로 끝나면 1, 대시보드 폴더·`node_modules`·`npm`이 없으면 2.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
from pathlib import Path

import nearpilot
from nearpilot.cli import EXIT_FAILED, EXIT_INVALID, EXIT_OK
from nearpilot.cli.common import CliError, load_cli_config


def register(subparsers: argparse._SubParsersAction, parent: argparse.ArgumentParser) -> None:
    parser = subparsers.add_parser(
        "dashboard", parents=[parent], help="대시보드 개발 서버(npm run dev)를 띄운다",
        description="대시보드 폴더에서 npm run dev를 실행하고 Ctrl+C까지 기다린다. 설정의 [http].port 서버로 요청을 넘기고, "
                    "같은 AP의 다른 기기에서도 열린다. npm install은 하지 않는다.")
    parser.add_argument("--dashboard-dir", metavar="PATH", default=None,
                        help="대시보드 폴더 (실행 폴더 기준). 없으면 패키지 위치에서 찾은 저장소의 dashboard/")
    parser.set_defaults(handler=_dashboard)


def default_dashboard_dir() -> Path:
    """패키지 위치에서 찾은 저장소의 dashboard/ (pip install -e로 설치한 경우).

    `nearpilot/__init__.py`는 `<저장소>/server/src/nearpilot/`에 있다.
    """
    return Path(nearpilot.__file__).resolve().parents[3] / "dashboard"


def _dashboard(args: argparse.Namespace) -> int:
    config = load_cli_config(args.config)
    folder = Path(args.dashboard_dir).resolve() if args.dashboard_dir is not None else default_dashboard_dir()
    if not (folder / "package.json").is_file():
        raise CliError(f"대시보드 폴더가 아니다: {folder}. --dashboard-dir로 준다", EXIT_INVALID)
    if not (folder / "node_modules").is_dir():
        raise CliError(f"대시보드 의존성이 없다. {folder}에서 npm install을 먼저 실행한다", EXIT_INVALID)
    npm = shutil.which("npm")
    if npm is None:
        raise CliError("npm 실행 파일이 필요하다. Node.js를 설치하고 PATH에 추가한다", EXIT_INVALID)
    env = {**os.environ, "NEARPILOT_SERVER": f"http://127.0.0.1:{config.http.port}"}
    # Ctrl+C(KeyboardInterrupt)는 그대로 올린다. main이 130으로 바꾼다
    result = subprocess.run([npm, "run", "dev", "--", "--host", "0.0.0.0"], cwd=folder, env=env)
    return EXIT_OK if result.returncode == 0 else EXIT_FAILED

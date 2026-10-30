"""`nearpilot sim`: 시험 폴더의 가상 기기를 띄운다 (specs/core-ops-cli/spec.md 6절 "시험 폴더와 실행 보조 명령").

설정 폴더의 `fleet.json`을 `tools.node_sim.__main__.main(["run", "--config", …])`에 넘기고, 종료 코드는 그 함수의 값이다.
설정 폴더에 `lab.json`이 있고 `mode`가 `sim`일 때만 띄운다 — 실기기 시험 폴더에서 가상 기기가 같은 기기 ID로 붙지 않게 한다.
아니면 기기를 띄우지 않고 종료 코드 2다. 서버 상태를 바꾸지 않는다.
"""

from __future__ import annotations

import argparse
import json

from nearpilot.cli import EXIT_INVALID
from nearpilot.cli.common import CliError, load_cli_config
from tools.node_sim.__main__ import main as node_sim_main


def register(subparsers: argparse._SubParsersAction, parent: argparse.ArgumentParser) -> None:
    parser = subparsers.add_parser(
        "sim", parents=[parent], help="시험 폴더의 가상 기기를 띄운다 (lab.json의 mode가 sim일 때만)",
        description="설정 폴더의 fleet.json으로 가상 기기를 띄우고 quit 입력이나 Ctrl+C까지 기다린다. "
                    "설정 폴더의 lab.json에서 mode가 sim일 때만 띄운다. --dir로 시험 폴더를 준다.")
    parser.set_defaults(handler=_sim)


def _sim(args: argparse.Namespace) -> int:
    config = load_cli_config(args.config)
    folder = config.source.parent
    lab = folder / "lab.json"
    if not lab.is_file():
        raise CliError(f"lab.json이 없다: {lab}. 가상 기기는 node_lab init --mode sim 폴더에서만 띄운다", EXIT_INVALID)
    try:
        options = json.loads(lab.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:  # JSONDecodeError와 UnicodeDecodeError는 ValueError다
        raise CliError(f"lab.json을 읽을 수 없다: {lab}", EXIT_INVALID) from error
    mode = options.get("mode") if isinstance(options, dict) else None
    if mode != "sim":
        raise CliError(f"가상 기기는 mode가 sim인 시험 폴더에서만 띄운다: mode={mode}", EXIT_INVALID)
    fleet = folder / "fleet.json"
    if not fleet.is_file():
        raise CliError(f"fleet.json이 없다: {fleet}", EXIT_INVALID)
    return node_sim_main(["run", "--config", str(fleet)])
